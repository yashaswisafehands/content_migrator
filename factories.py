import csv
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple
import uuid

import requests

from configs import _get_assets_base_url, POST_ASSET, JWT_TOKEN
from text_utils import clean_and_resolve_title, clean_metadata_text, format_mobile_markdown
from data_models import LanguageData, ModuleData, ResourcePostRequestData
from md_converter_new import convert_action_card_to_markdown_files
from path_utils import (
    ensure_parent_dir,
    get_config_file,
    get_mappings_file,
    get_temp_directory,
    get_processed_file,
)


class DataFactory:
    """Factory class for creating data structures from Cosmos DB documents."""

    # Class variables for CSV data caching
    _country_mapping: Optional[Dict[str, str]] = None
    _language_mapping: Optional[Dict[str, str]] = None

    # Asset caching to avoid duplicate uploads
    _asset_cache: Dict[str, Tuple[Optional[str], str]] = {}
    _asset_mapping_path = get_mappings_file("asset_mapping.csv")
    _mapping_loaded: bool = False

    @classmethod
    def _load_asset_mapping(cls) -> None:
        """Load asset URL to (asset_id, asset_type) mapping from CSV."""
        if not cls._asset_mapping_path.exists():
            return

        try:
            with cls._asset_mapping_path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                for i, row in enumerate(reader):
                    if len(row) < 3:
                        continue
                    # Skip header row if present
                    if i == 0 and row[0] == "asset_url":
                        continue
                    asset_url = row[0]
                    asset_id = row[1]
                    asset_type = row[2]
                    if asset_url and asset_id:
                        cls._asset_cache[asset_url] = (asset_id, asset_type)
            print(f"Loaded {len(cls._asset_cache)} asset mappings from file")
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Could not load asset mapping file: {e}")

    @classmethod
    def _save_asset_mapping(cls) -> None:
        """Deprecated: avoid overwriting mapping file; we append per upload."""
        return

    @classmethod
    def _load_country_mapping(cls) -> Dict[str, str]:
        """Load country code to country name mapping from CSV."""
        if cls._country_mapping is None:
            cls._country_mapping = {}
            try:
                with get_config_file("country_code.csv").open(
                    "r",
                    encoding="utf-8",
                    errors="replace",
                ) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        cls._country_mapping[row["country_code"]] = row["country"]
            except FileNotFoundError:
                print("Warning: country_code.csv not found")
        return cls._country_mapping

    @classmethod
    def _load_language_mapping(cls) -> Dict[str, str]:
        """Load language name to autonym script mapping from CSV."""
        if cls._language_mapping is None:
            cls._language_mapping = {}
            try:
                with get_config_file("language_name.csv").open(
                    "r",
                    encoding="utf-8",
                    errors="replace",
                ) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        cls._language_mapping[row["language_name"]] = row[
                            "autonym_script"
                        ]
            except FileNotFoundError:
                print("Warning: language_name.csv not found")
        return cls._language_mapping

    @classmethod
    def _find_closest_language_match(cls, description: str) -> Optional[str]:
        """Find closest language match using regex pattern matching."""
        mapping = cls._load_language_mapping()

        # Try exact match first
        if description in mapping:
            return mapping[description]

        # Try case-insensitive match
        description_lower = description.lower()
        for lang_name, autonym in mapping.items():
            if lang_name.lower() == description_lower:
                return autonym

        # Try partial matches and regex patterns
        for lang_name, autonym in mapping.items():
            # Remove special characters and compare
            clean_lang = re.sub(r"[^\w\s]", "", lang_name.lower())
            clean_desc = re.sub(r"[^\w\s]", "", description_lower)

            if clean_lang in clean_desc or clean_desc in clean_lang:
                return autonym

        return None

    @classmethod
    def create_language_data(cls, cosmos_doc: Dict) -> LanguageData:
        """Create LanguageData from Cosmos DB language document."""
        country_mapping = cls._load_country_mapping()

        # Extract basic fields
        description = cosmos_doc.get("description", "")
        asset_version = cosmos_doc.get("assetVersion", "")
        country_code = cosmos_doc.get("countryCode", "")

        # Get country name from mapping
        country_name = country_mapping.get(country_code, country_code)

        # Get autonym script using closest match
        autonym_script = cls._find_closest_language_match(description)
        if not autonym_script:
            print(f"Warning: No autonym script found for " f"language '{description}'")
            autonym_script = description  # Fallback to description

        # Parse asset_version into image_prefix and video_prefix
        # Format: "image_prefix, video_prefix"
        image_prefix = "africa"
        video_prefix = ""

        if asset_version:
            parts = [p.strip() for p in asset_version.split(",")]
            if len(parts) >= 1:
                image_prefix = parts[0]
            if len(parts) >= 2:
                video_prefix = parts[1]

        # Upload icon if present
        icon_path = cosmos_doc.get("icon")

        icon_asset_id = None
        if icon_path:
             # Use a generic language ID or none for icon storage
             icon_asset_id = cls._download_and_upload_icon(icon_path, "")

        return LanguageData(
            language_name=description,
            autonym_script=autonym_script,
            region=image_prefix,
            image_prefix=image_prefix,
            video_prefix=video_prefix,
            country_code=country_code,
            country=country_name,
            latitude=cosmos_doc.get("latitude", 0.0),
            longitude=cosmos_doc.get("longitude", 0.0),
            learning_platform=cosmos_doc.get("learningPlatform", True),
            created_by=cosmos_doc.get("LastUpdatedBy", "System"),
            icon=icon_asset_id,
        )

    # Cache for media config lookups (language_id -> (image_prefix, video_prefix))
    _media_config_cache: Dict[str, Tuple[str, str]] = {}

    @classmethod
    def _get_media_config(cls, language_id: str) -> Tuple[str, str]:
        """Get (image_prefix, video_prefix) from language ID. Uses cache for performance."""
        # Check cache first
        if language_id in cls._media_config_cache:
            return cls._media_config_cache[language_id]
        
        image_prefix = ""
        video_prefix = ""
        
        # Global Override
        if not language_id or language_id == "global":
            result = ("africa", "english WHO")
            cls._media_config_cache[language_id or "global"] = result
            return result

        # 1. Try processed languages.csv (preferred for metadata)
        try:
            with get_processed_file("languages.csv").open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cid = row.get("cosmos_id")
                    if cid == language_id:
                        # Grab prefixes if available, falling back to 'region' column
                        img = (row.get("image_prefix") or "").strip()
                        if not img:
                            img = (row.get("region") or "").strip()
                            
                        vid = (row.get("video_prefix") or "").strip()

                        if img:
                            image_prefix = img

                        if vid:
                            video_prefix = vid
                        elif img:
                             video_prefix = img
                        
                        result = (image_prefix, video_prefix)
                        cls._media_config_cache[language_id] = result
                        return result
        except Exception:
            from error_logger import log_error
            log_error("Captured Exception")
            pass

        # 2. Try language_mapping.csv (fallback, likely only region)
        try:
            with get_mappings_file("language_mapping.csv").open(
                "r", encoding="utf-8"
            ) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (
                        row.get("cosmos_id") == language_id
                        or row.get("lme_language_id") == language_id
                    ):
                        reg = row.get("region", "africa")
                        result = (reg, reg)
                        cls._media_config_cache[language_id] = result
                        return result
        except Exception:
            from error_logger import log_error
            log_error("Captured Exception")
            pass

        result = (image_prefix, video_prefix)
        if (not image_prefix and not video_prefix) and language_id != "global" and language_id:
            print(f"  ⚠️ Warning: No media config (prefixes) found for language {language_id} in CSVs! Asset URLs may be malformed.")
            
        cls._media_config_cache[language_id] = result
        return result

    @classmethod
    def _get_region_from_language_id(cls, language_id: str) -> str:
        """Get region (image prefix) from language ID."""
        img, _ = cls._get_media_config(language_id)
        return img

    @classmethod
    def download_media(cls, url: str, save_path: str) -> bool:
        """Download media from URL to local path."""
        try:
            response = requests.get(url, stream=True)
            response.raise_for_status()
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except requests.exceptions.RequestException as e:
            if response is not None and response.status_code == 404:
                 print(f"❌ 404 Not Found: {url}")
            else:
                 print(f"Download failed for {url}: {e}")
            return False

    @classmethod
    def get_or_upload_asset_with_local_cache(
        cls,
        source_url: Optional[str],
        local_path: str,
        media_type: str,
        upload_url: str,
        embedded_assets: Optional[List[str]] = None
    ) -> Optional[dict]:
        """Check cache, download if needed, upload, and update cache."""
        # Use source_url as key if available, else local_path
        asset_key = source_url if source_url else local_path

        # Load persistent asset mapping on first use
        if not cls._mapping_loaded:
            cls._load_asset_mapping()
            cls._mapping_loaded = True

        # Check in-memory cache
        if asset_key in cls._asset_cache:
            # _asset_cache stores (asset_id, asset_type) or just asset_id in some versions? 
            # In factories.py it says: _asset_cache: Dict[str, Tuple[Optional[str], str]]
            # But the utilities version might store just asset_id. keeping it consistent with factories.py
            cached_val = cls._asset_cache[asset_key]
            # Handle both tuple and string formats just in case
            if isinstance(cached_val, tuple):
                 return {"asset_id": cached_val[0]}
            return {"asset_id": cached_val}

        # Download if needed
        if source_url and not os.path.exists(local_path):
            print(f"Downloading: {os.path.basename(local_path)}")
            if not cls.download_media(source_url, local_path):
                return None
        
        # Upload
        if not os.path.exists(local_path):
             return None

        # Reuse existing _upload_asset logic but formatted for this flow? 
        # _upload_asset takes (file_path, asset_type). 
        # But _upload_asset in factories.py might constructs URL internally.
        # Let's see _upload_asset implementation. 
        # It takes (file_path, asset_type).
        
        # Actually I should allow _upload_asset to take an optional override URL or generic type.
        # For now, I will implement the upload logic here directly or wrap _upload_asset.
        
        # Let's just implement the upload part similar to utilities tool.py using requests 
        # OR adapt it to use the existing _upload_asset if possible.
        # existing _upload_asset: def _upload_asset(cls, file_path: str, asset_type: str) -> Optional[str]:
        # It calculates content_type and construct upload_url from configs.POST_ASSET formatted with tag.
        
        # The new method passes `upload_url` explicitly. 
        # I'll implement the upload explicitly here to support the `upload_url` arg.
        
        try:
             with open(local_path, "rb") as f:
                safe_name = os.path.basename(local_path)
                # Sanitize filename
                safe_name = re.sub(r"\s+", "_", safe_name)
                safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", safe_name)
                
                files = {"file": (safe_name, f, media_type)}
                data = {}
                if embedded_assets:
                    data["linked_asset_ids"] = ",".join(embedded_assets)

                headers = {}
                if JWT_TOKEN:
                    headers["Authorization"] = f"Bearer {JWT_TOKEN}"

                response = requests.post(upload_url, files=files, data=data, headers=headers)
                response.raise_for_status()
                response_json = response.json()
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Upload failed for {local_path}: {e}")
            return None

        if response_json and response_json.get("asset_id"):
            new_asset_id = response_json["asset_id"]
            # Update cache
            # Factories expects tuple (id, type)
            # We can infer type from media_type or just store generic
            cls._asset_cache[asset_key] = (new_asset_id, media_type)
            
            # Append to persistent mapping file
            ensure_parent_dir(cls._asset_mapping_path)
            need_header = (
                not cls._asset_mapping_path.exists()
                or cls._asset_mapping_path.stat().st_size == 0
            )
            try:
                with cls._asset_mapping_path.open(
                    "a", newline="", encoding="utf-8"
                ) as f:
                    writer = csv.writer(f)
                    if need_header:
                        writer.writerow(["asset_url", "asset_id", "asset_type"])
                    writer.writerow([asset_key, new_asset_id, media_type])
            except Exception as e:
                print(f"Warning: Could not save to asset_mapping file: {e}")
                
            print(f"CACHED: Saved new asset_id '{new_asset_id}' for path '{asset_key}'")
            return response_json
        
        return None

    @classmethod
    def _download_and_upload_icon(
        cls, icon_path: str, language_id: str = "", asset_name_prefix: Optional[str] = None, use_prefix: bool = True
    ) -> Optional[str]:
        """Download icon from assets URL and upload to LME."""
        return cls._download_and_upload_asset(icon_path, language_id, "icon", asset_name_prefix, use_prefix)

    @classmethod
    def _download_and_upload_video(
        cls, video_path: str, language_id: str = "", asset_name_prefix: Optional[str] = None, use_prefix: bool = True
    ) -> Optional[str]:
        """Download video from assets URL and upload to LME."""
        return cls._download_and_upload_asset(video_path, language_id, "video", asset_name_prefix, use_prefix)

    @classmethod
    def _construct_asset_url(
        cls, asset_path: str, language_id: str = "global", asset_type: str = "icon", use_prefix: bool = True
    ) -> str:
        """Construct the full authenticated URL for an asset matching utility behavior."""
        if asset_path and (asset_path.lower().startswith("http://") or asset_path.lower().startswith("https://")):
            return asset_path

        from urllib.parse import quote
        
        if not language_id:
            language_id = "global"
        
        # Get routing prefixes
        image_prefix_raw, video_prefix_raw = cls._get_media_config(language_id)
        image_prefix = image_prefix_raw.strip() if image_prefix_raw else ""
        video_prefix = video_prefix_raw.strip() if video_prefix_raw else ""
        
        # Determine file extension
        if asset_type == "video":
            ext = "mp4"
        elif asset_type in ("icon", "video_icon"):
            ext = "png"
        else:
            ext = "png"

        clean_path = asset_path.lstrip("/")
        
        _, path_ext = os.path.splitext(clean_path)
        if clean_path.lower().endswith(f".{ext}"):
            path_ext = ""
        else:
             path_ext = f".{ext}"
        
        # Common logic to strip prefix from path if it's already there to prevent doubling
        def strip_redundant_prefix(path: str, prefixes: list[str]) -> str:
            p_norm = path.replace("\\", "/").lstrip("/")
            sorted_prefixes = sorted([p for p in prefixes if p], key=len, reverse=True)
            for prefix in sorted_prefixes:
                if not prefix:
                    continue
                if p_norm.lower().startswith(f"{prefix.lower()}/"):
                    return p_norm[len(prefix) + 1 :]
            return p_norm

        prefixes_to_strip = [video_prefix, image_prefix, "India"]

        if asset_type == "video":
            if not use_prefix:
                path_no_ext = os.path.splitext(clean_path)[0]
                base_url = _get_assets_base_url().rstrip("/")
                safe_path = "/".join(quote(s) for s in path_no_ext.split("/"))
                return f"{base_url}/videos/{safe_path}.mp4"
            else:
                prefix = video_prefix or ""
                adjusted_path = strip_redundant_prefix(clean_path, prefixes_to_strip)
                path_no_ext = os.path.splitext(adjusted_path)[0]
                prefix_enc = quote(prefix)
                path_enc = "/".join(quote(p) for p in path_no_ext.split("/"))
                parts = [p for p in [prefix_enc, path_enc] if p]
                middle = "/".join(parts)
                base_url = _get_assets_base_url().rstrip("/")
                return f"{base_url}/videos/{middle}.mp4"

        elif asset_type == "video_icon":
            if not use_prefix:
                path_no_ext = os.path.splitext(clean_path)[0]
                base_url = _get_assets_base_url().rstrip("/")
                safe_path = "/".join(quote(s) for s in path_no_ext.split("/"))
                return f"{base_url}/videos/{safe_path}.png"
            else:
                prefix = video_prefix or ""
                adjusted_path = strip_redundant_prefix(clean_path, prefixes_to_strip)
                path_no_ext = os.path.splitext(adjusted_path)[0]
                prefix_enc = quote(prefix)
                path_enc = "/".join(quote(p) for p in path_no_ext.split("/"))
                parts = [p for p in [prefix_enc, path_enc] if p]
                middle = "/".join(parts)
                base_url = _get_assets_base_url().rstrip("/")
                return f"{base_url}/videos/{middle}.png"

        else:
            # Images/Icons
            prefix = image_prefix or ""
            adjusted_path = strip_redundant_prefix(clean_path, prefixes_to_strip)
            path_no_ext = os.path.splitext(adjusted_path)[0]
            prefix_enc = quote(prefix)
            path_enc = "/".join(quote(p) for p in path_no_ext.split("/"))
            parts = [p for p in [prefix_enc, path_enc] if p]
            middle = "/".join(parts)
            base_url = _get_assets_base_url().rstrip("/")
            return f"{base_url}/images/{middle}.png"

    @classmethod
    def _download_asset(
        cls, asset_path: str, language_id: str = "global", asset_type: str = "icon", asset_name_prefix: Optional[str] = None, use_prefix: bool = True
    ) -> Optional[str]:
        """Download asset from URL and return local file path."""
        if not asset_path:
            return None

        try:
            asset_url = cls._construct_asset_url(asset_path, language_id, asset_type, use_prefix)
            print(f"Downloading {asset_type} asset from {asset_url}")

            # Download the asset
            response = requests.get(asset_url)
            response.raise_for_status()

            # Determine extension for temp file
            _, ext = os.path.splitext(asset_path)
            if not ext:
                if asset_type == "video":
                    ext = ".mp4"
                elif asset_type == "icon":
                    ext = ".png"
                else:
                    ext = ".png"

            # Save temporarily
            base_name = os.path.basename(asset_path)
            name_without_ext = os.path.splitext(base_name)[0]
            
            if asset_name_prefix:
                temp_filename = f"{asset_name_prefix}{ext}"
            else:
                temp_filename = f"{name_without_ext}{ext}"
                
            # clean filename
            temp_path = temp_filename.replace("/", "_").replace("\\", "_").replace(" ", "_").replace(":", "_")
            with open(temp_path, "wb") as f:
                f.write(response.content)

            return temp_path

        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Error downloading {asset_type} asset {asset_url}: {e}")
            return None

    @classmethod
    def _upload_markdown_as_asset(
        cls, markdown_content: str, filename: str
    ) -> Optional[str]:
        """Upload markdown content as an asset file.

        Args:
            markdown_content: The markdown text content
            filename: Base filename for the asset (without extension)

        Returns:
            Asset ID if upload successful, None otherwise
        """
        if not markdown_content:
            return None

        # Create temporary .md file inside the shared temp directory
        temp_dir = get_temp_directory()
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_filename = f"{filename}.md"
        temp_path = temp_dir / temp_filename

        try:
            # Write markdown content to file
            with temp_path.open("w", encoding="utf-8") as f:
                f.write(markdown_content)

            # Upload as asset with tag "action-card"
            asset_id = cls._upload_asset(str(temp_path), "action-card")

            return asset_id

        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Error uploading markdown as asset {filename}: {e}")
            return None
        finally:
            # Clean up temp file
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception as cleanup_error:
                from error_logger import log_error
                log_error("Captured Exception", exc=cleanup_error)
                print(f"Cleanup failed for {temp_path}: {cleanup_error}")

    @classmethod
    def _upload_asset(cls, file_path: str, asset_type: str = "icon") -> Optional[str]:
        """Upload local file to LME and return asset_id."""
        if not file_path or not os.path.exists(file_path):
            return None

        try:
            if asset_type == "video":
                content_type = "video/mp4"
            elif asset_type in ("action-card", "drug", "procedure", "certificate"):
                content_type = "text/markdown"
            else:
                content_type = "image/png"

            upload_tag = asset_type
            upload_url = POST_ASSET.format(tag=upload_tag)

            import re as _re
            from unicodedata import normalize

            embedded_asset_ids = []
            if content_type == "text/markdown":
                with open(file_path, "r", encoding="utf-8") as md_f:
                    md_content = md_f.read()
                uuid_pattern = r'!\[[^\]]*\]\(([0-9a-f]{32})\)'
                embedded_asset_ids = _re.findall(uuid_pattern, md_content, _re.IGNORECASE)

            with open(file_path, "rb") as f:
                raw_name = os.path.basename(file_path)
                safe_name = (
                    normalize("NFKD", raw_name)
                    .encode("ascii", "ignore")
                    .decode("ascii")
                )
                safe_name = _re.sub(r"\s+", "_", safe_name)
                safe_name = _re.sub(r"[^A-Za-z0-9_.-]", "_", safe_name)
                files = {"file": (safe_name or "file.bin", f, content_type)}
                
                data = {}
                if embedded_asset_ids:
                    data["linked_asset_ids"] = ",".join(embedded_asset_ids)
                    print(f"  📎 Linking {len(embedded_asset_ids)} embedded asset(s) to markdown upload")

                headers = {}
                if JWT_TOKEN:
                    headers["Authorization"] = f"Bearer {JWT_TOKEN}"
                
                upload_response = requests.post(upload_url, files=files, data=data, headers=headers)
                upload_response.raise_for_status()

            upload_data = upload_response.json()
            asset_id = upload_data.get("asset_id")

            return asset_id

        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Error uploading asset {file_path}: {e}")
            return None

    @classmethod
    def _download_and_upload_asset(
        cls, asset_path: str, language_id: str = "global", asset_type: str = "icon", asset_name_prefix: Optional[str] = None, use_prefix: bool = True
    ) -> Optional[str]:
        """Download asset from URL and upload to LME."""
        if not asset_path:
            return None

        # Load persistent asset mapping on first use
        if not cls._mapping_loaded:
            cls._load_asset_mapping()
            cls._mapping_loaded = True

        # Construct the asset URL to check cache
        # Use simple construction without extensive error handling for cache key purpose,
        # but utilize the shared helper to ensure consistency.
        asset_url = cls._construct_asset_url(asset_path, language_id, asset_type, use_prefix)

        # Check cache first using asset URL as key
        if asset_url in cls._asset_cache:
            cached_asset_id, _ = cls._asset_cache[asset_url]
            if cached_asset_id:  # Make sure it's not a failed attempt
                print(f"Using cached asset_id: {cached_asset_id}")
                return cached_asset_id
            else:
                print(f"Skipping previously failed asset: {asset_url}")
                return None

        # Download asset
        temp_path = cls._download_asset(asset_path, language_id, asset_type, asset_name_prefix, use_prefix)
        
        if not temp_path and language_id and language_id != "global" and use_prefix:
             print(f"  → Local asset missing. Retrying with Global path for: {asset_path}")
             fallback_url = cls._construct_asset_url(asset_path, "global", asset_type, use_prefix)
             if fallback_url in cls._asset_cache:
                 cached_id, _ = cls._asset_cache[fallback_url]
                 if cached_id:
                     print(f"  → Found Global fallback in cache: {cached_id}")
                     return cached_id
            
             temp_path = cls._download_asset(asset_path, "global", asset_type, asset_name_prefix, use_prefix)
             if temp_path:
                 print(f"  → Successfully downloaded Global fallback.")
                 asset_url = fallback_url
        
        if not temp_path:
            cls._asset_cache[asset_url] = (
                None,
                asset_type,
            )  # Cache failed attempts
            return None

        try:
            # Upload asset
            asset_id = cls._upload_asset(temp_path, asset_type)

            # Clean up temp file
            os.remove(temp_path)

            if asset_id:
                # Cache the successful upload
                cls._asset_cache[asset_url] = (asset_id, asset_type)
                # Append to persistent mapping file
                ensure_parent_dir(cls._asset_mapping_path)
                need_header = (
                    not cls._asset_mapping_path.exists()
                    or cls._asset_mapping_path.stat().st_size == 0
                )
                with cls._asset_mapping_path.open(
                    "a", newline="", encoding="utf-8"
                ) as f:
                    writer = csv.writer(f)
                    if need_header:
                        writer.writerow(
                            [
                                "asset_url",
                                "asset_id",
                                "asset_type",
                            ]
                        )
                    writer.writerow([asset_url, asset_id, asset_type])
                print(f"Cached and saved asset_id for {asset_url}: {asset_id}")
                return asset_id
            else:
                cls._asset_cache[asset_url] = (None, asset_type)
                return None

        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Error processing asset {asset_path}: {e}")
            # Clean up temp file on error
            try:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception as cleanup_error:
                from error_logger import log_error
                log_error("Captured Exception", exc=cleanup_error)
                print(f"Cleanup failed for {temp_path}: {cleanup_error}")
            cls._asset_cache[asset_url] = (None, asset_type)
            return None

    @classmethod
    def get_asset_cache_stats(cls) -> Dict[str, int]:
        """Get statistics about the asset cache."""
        total = len(cls._asset_cache)
        successful = sum(1 for v in cls._asset_cache.values() if v[0] is not None)
        failed = total - successful
        return {
            "total_cached": total,
            "successful_uploads": successful,
            "failed_attempts": failed,
        }

    @classmethod
    def create_module_data(cls, cosmos_doc: Dict) -> ModuleData:
        """Create ModuleData from Cosmos DB module document."""
        # Extract basic fields
        title = cosmos_doc.get("description", "")
        description = cosmos_doc.get("description", "")

        # Process icon
        icon_path = cosmos_doc.get("icon", "")
        language_id = cosmos_doc.get("langId") or cosmos_doc.get("language_id", "")  # Get language ID
        icon_asset_id = (
            cls._download_and_upload_icon(icon_path, language_id) if icon_path else None
        )

        # Process videos - download and upload each video as asset
        video_paths = cosmos_doc.get("videos", [])
        video_asset_ids = []
        for video_path in video_paths:
            if video_path:  # Only process non-empty paths
                # Download and upload video
                video_asset_id = cls._download_and_upload_video(video_path, language_id, use_prefix=False)
                if video_asset_id:
                    video_asset_ids.append(video_asset_id)
                else:
                    print(f"Warning: Failed to process video asset: " f"{video_path}")

        return ModuleData(
            title=title,
            description=description,
            icon=icon_asset_id,
            created_by=cosmos_doc.get("LastUpdatedBy", "System"),
            videos=video_asset_ids,  # asset IDs
            action_cards=cosmos_doc.get("actionCards", []),
            practical_procedures=cosmos_doc.get("procedures", []),
            key_learning_points=cosmos_doc.get("keyLearningPoints", [])
            or cosmos_doc.get("key_learning_points", []),
            drugs=cosmos_doc.get("drugs", []),
        )

    @classmethod
    def _extract_questions_for_klp(
        cls, cosmos_doc: Dict, language_id: str, version_preference: Optional[str] = None
    ) -> List[Dict]:
        """Extract questions from KLP document and map to LME payload structure.
        
        Prioritizes version_preference if specified, otherwise embedded translations (translated > adapted > content).
        """
        questions_payload = []
        raw_questions = cosmos_doc.get("questions", [])

        for idx, q in enumerate(raw_questions):
            # 1. Resolve Question Text
            # Priority: version_preference -> translated -> adapted -> content
            q_obj = q.get("question", {})
            
            q_content = ""
            if version_preference:
                 q_content = q.get(version_preference, {}).get("content") or q.get("question", {}).get(version_preference) or ""
            
            if not q_content:
                 q_content = (
                     q.get("translated", {}).get("content") or
                     q.get("question", {}).get("translated") or
                     q.get("adapted", {}).get("content") or
                     q.get("question", {}).get("content") or 
                     ""
                 )

            # 2. Resolve Description
            q_desc = ""
            if version_preference:
                 q_desc = q.get("description", {}).get(version_preference) or ""
            
            if not q_desc:
                 q_desc = (
                      q.get("description", {}).get("translated") or
                      q.get("description", {}).get("content") or
                      ""
                 )
            
            icon_path = q.get("image") or q.get("icon")
            icon_asset_id = None
            if icon_path:
                icon_asset_id = cls._download_and_upload_asset(icon_path, language_id, "icon")
                if icon_asset_id:
                    print(f"  ✓ KLP question {idx+1} icon uploaded: {icon_asset_id[:8]}...")
                else:
                    print(f"  ⚠️  Failed to upload KLP question {idx+1} icon from: {icon_path}")

            answers_payload = []
            raw_answers = q.get("answers", [])
            for a_idx, ans in enumerate(raw_answers):
                # 3. Resolve Answer Value
                # Priority: value.<version_preference> -> value.translated -> value.adapted -> value.content
                val_obj = ans.get("value", {})
                
                val_str = ""
                if version_preference:
                     val_str = val_obj.get(version_preference) or ""
                
                if not val_str:
                     val_str = (
                         val_obj.get("translated") or 
                         val_obj.get("adapted") or 
                         val_obj.get("content") or
                         (str(val_obj) if not isinstance(val_obj, dict) else "")
                     )

                if not val_str.strip():
                    continue

                answers_payload.append({
                    "answer_id": str(uuid.uuid4()),
                    "value": val_str,
                    "correct": ans.get("correct") or ans.get("isCorrect", False),
                    "order": len(answers_payload)
                })

            q_type = q.get("quizzType", "singleCorrect")
            
            raw_link = q.get("link") or ""
            link_type = raw_link.split(":", 1)[0] if ":" in raw_link else None

            questions_payload.append({
                "question_id": q.get("key", str(uuid.uuid4())),
                "question": q_content,
                "quizz_type": q_type,
                "icon": icon_asset_id,
                "link": raw_link,
                "link_type": link_type,
                "show_toggle": q.get("showToggle", False),
                "essential": False,
                "description": q_desc,
                "order": idx,
                "answers": answers_payload
            })

        return questions_payload

    @classmethod
    def create_resource_data(
        cls, cosmos_doc: Dict, table_type: str, language_id: str = "",
        global_doc: Optional[Dict] = None,
        module_icon_asset_id: Optional[str] = None,  # NEW: Accept module icon
        translated_title: Optional[str] = None,      # NEW: Accept translated title override
        force_version_type: Optional[str] = None     # NEW: Explicitly force 'adapted' or 'translated'
    ) -> ResourcePostRequestData:
        """Create ResourcePostRequestData from Cosmos DB resource document.
        
        CRITICAL: Global (langId="") is BOTH identity AND original content.
        - global_doc provides title/description (identity + original content)
        - cosmos_doc provides markdown/asset content only
        """
        # Identity AND Content Source: ALWAYS Global if available
        identity_doc = global_doc if global_doc else cosmos_doc

        title = clean_and_resolve_title(
            identity_doc.get("title") or identity_doc.get("description", ""), 
            identity_doc.get("row_key", "")
        )
        
        # Override title if translated version provided
        if translated_title:
             title = translated_title
             
        description = clean_and_resolve_title(
            identity_doc.get("description", ""), 
            identity_doc.get("row_key", "")
        )
        # Also update description if translated title provided (usually they are same for resources)
        if translated_title:
            description = translated_title
        content = cosmos_doc.get("content", "")
        # Extract derived_from_id (source LME ID for adaptations)
        derived_from_id = cosmos_doc.get("derived_from_id") or cosmos_doc.get("derivedFromId")

        # Determine tag based on table type
        content_type_mapping = {
            "videos": "video",
            "actionCards": "action-card",
            "drugs": "drug",
            "procedures": "procedure",
            "key-learning-points": "key-learning-point",
            "keyLearningPoints": "key-learning-point",
        }
        tag_name = content_type_mapping.get(table_type, "unknown")

        # Get language_id and region from language mapping if available
        # CRITICAL FIX: If caller explicitly passes language_id="" (global content),
        # do NOT override it with document's internal IDs (which may be UUIDs, not language IDs).
        # Only use doc_lang_id if language_id was not explicitly set by caller.
        doc_lang_id = cosmos_doc.get("language_id") or cosmos_doc.get("langId")
        # Check if doc_lang_id looks like a UUID (not a valid Cosmos language ID)
        # Valid Cosmos langIds are typically empty string or short codes like "en", "hi-IN"
        import re as _uuid_check
        is_uuid_like = doc_lang_id and _uuid_check.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', doc_lang_id, _uuid_check.IGNORECASE)
        if is_uuid_like:
            # This is a document ID, not a language ID - ignore it
            pass
        elif doc_lang_id:
            # Only override if we have a valid-looking language ID and caller didn't explicitly set empty
            language_id = doc_lang_id
        region = (
            cls._get_region_from_language_id(language_id)
            if language_id
            else "africa"
        )

        # Process icon if present
        icon_path = cosmos_doc.get("icon") or cosmos_doc.get("image") or ""

        # CRITICAL: For drugs, inherit module icon if no specific icon exists
        if table_type in ("drugs",) and not icon_path and module_icon_asset_id:
            icon_asset_id = module_icon_asset_id
            print(f"  ✓ Drug inheriting module icon: {module_icon_asset_id[:8]}...")
        elif table_type == "videos":
            icon_asset_id = (
                cls._download_and_upload_asset(icon_path, language_id, "video_icon")
                if icon_path
                else None
            )
        else:
            icon_asset_id = (
                cls._download_and_upload_icon(icon_path, language_id) if icon_path else None
            )

        resolved_content_type = "translated" if language_id else "original"

        # For text-based resources (drugs/procedures/KLPs), handle accordingly
        questions = None
        
        if table_type in ("key-learning-points", "keyLearningPoints"):
            questions = cls._extract_questions_for_klp(
                cosmos_doc, language_id, version_preference=force_version_type
            )
            content = None  # Ensure content is None for KLP
            
        elif table_type in ("drugs", "procedures"):
            import json as _json
            from slug_utils import slugify

            raw_desc = cosmos_doc.get("description", "")
            title_candidate = title
            
            md_lines = []
            
            md_lines.append(f"# {title_candidate}")
            
            explicit_content = cosmos_doc.get("content")
            
            if raw_desc and raw_desc != title_candidate and raw_desc.lower() != "general":
                md_lines.append(raw_desc)
                
            # Removed '## Data' per user request

            
            # Convert content to Markdown using md_converter_new logic
            from md_converter_new import process_card
            
            # Asset version for image paths (default to region or empty)
            asset_version = region or ""
            
            # Handle cards
            cards = []
            # 'explicit_content' usually refers to 'content' key, but sometimes cards are at root (drugs/procedures)
            # Try getting cards from explicit_content first
            if explicit_content:
                if isinstance(explicit_content, dict) and "cards" in explicit_content:
                    cards = explicit_content["cards"]
                elif isinstance(explicit_content, list):
                    cards = explicit_content
            
            # If no cards found yet, check root of cosmos_doc (common for drugs/procedures)
            if not cards and "cards" in cosmos_doc:
                cards = cosmos_doc["cards"]

            # If still no cards, but the document itself has content/translated/adapted strings, treat the whole doc as a card
            if not cards and ("content" in cosmos_doc or "translated" in cosmos_doc or "adapted" in cosmos_doc):
                cards = [cosmos_doc]

            if force_version_type in ("translated", "adapted"):
                resolved_content_type = force_version_type
            elif language_id:
                has_translated = any(c.get("translated") and isinstance(c.get("translated"), dict) and c["translated"].get("blocks") for c in cards)
                has_adapted = any(c.get("adapted") and isinstance(c.get("adapted"), dict) and c["adapted"].get("blocks") for c in cards)
                if has_translated:
                    resolved_content_type = "translated"
                elif has_adapted:
                    resolved_content_type = "adapted"

            for card in cards:
                # Determine which version to process
                # If we're targeting a localized doc (e.g. drugs table with langId), the content is usually in 'translated' block
                # However, DataFactory usually receives the raw doc.
                # If 'allowed_versions' is passed implicitly via logic, we should respect it?
                # Actually, the factory doesn't receive 'allowed_versions' in this signature (it's inside kwargs or logic).
                # But wait, create_resource_data signature (line 1042) accepts allowed_versions.
                
                # Check for translated content first if this is a localized run
                # We can heuristic check if 'translated' key exists and has data
                # But safer is to check allowed_versions from caller
                
                version_key = "content"
                if force_version_type in ("translated", "adapted"):
                     if card.get(force_version_type) and card[force_version_type].get("blocks"):
                          version_key = force_version_type
                     # Fallback gracefully if forced version doesn't exist
                     elif force_version_type == "translated" and card.get("adapted") and card["adapted"].get("blocks"):
                          version_key = "adapted"
                     elif force_version_type == "adapted" and card.get("translated") and card["translated"].get("blocks"):
                          version_key = "translated"
                # Heuristic fallback: if doc has language_id, prefer translated
                elif language_id and card.get("translated") and card["translated"].get("blocks"):
                     version_key = "translated"
                elif language_id and card.get("adapted") and card["adapted"].get("blocks"):
                     version_key = "adapted"
                     
                processed = process_card(card, version_key, asset_version)
                if processed["md_text"]:
                    md_lines.append(processed["md_text"])
                
                # Note: Assets are uploaded nicely by process_card but we might want to track them?
                # For simplicity in this factory, process_card handles the asset upload side-effect.
            
            text_content = "\n\n".join(md_lines).strip()
            
            # Apply strict mobile formatting
            text_content = format_mobile_markdown(title_candidate, text_content)
            
            # DISTINCT ASSET FILENAME LOGIC
            safe_base = slugify(title_candidate) or table_type
            asset_filename = f"{table_type}-{safe_base}"
            if resolved_content_type != "original":
                asset_filename += f"-{resolved_content_type}"
            
            asset_id = cls._upload_markdown_as_asset(text_content, asset_filename)
            
            if asset_id:
                content = asset_id
            else:
                print(f"Warning: failed to create markdown asset for {table_type} '{title}'")

        return ResourcePostRequestData(
            title=title,
            description=description,
            icon=icon_asset_id,
            content=content,
            language_id=language_id,
            region=region,
            content_type=resolved_content_type,
            created_by=cosmos_doc.get("LastUpdatedBy", "System"),
            questions=questions,
            derived_from_id=derived_from_id,
        )

    @classmethod
    def get_action_card_data(
        cls,
        cosmos_client,
        container,
        action_card_key: str,
        module_language_id: str = "",
    ) -> Optional[Dict]:
        """Fetch action-card data from Cosmos DB by key.

        Args:
            cosmos_client: Azure Cosmos DB client
            container: Cosmos DB container
            action_card_key: The key of the action card to fetch
            module_language_id: Language ID to fetch (empty string for global)

        Returns:
            Dict containing the action-card document, or None if not found
        """
        if not action_card_key:
            return None

        try:
            # Try 'action-cards' first (hyphenated)
            query = (
                "SELECT TOP 1 * FROM c WHERE c._table='action-cards' "
                f"AND c.langId='{module_language_id}' "
                f"AND c.key='{action_card_key}' "
                f"ORDER BY c._ts DESC"
            )
            results = list(
                container.query_items(query=query, enable_cross_partition_query=True)
            )

            if results:
                return results[0]
            
            # Fallback to 'actionCards' (camelCase)
            query = (
                "SELECT TOP 1 * FROM c WHERE c._table='actionCards' "
                f"AND c.langId='{module_language_id}' "
                f"AND c.key='{action_card_key}' "
                f"ORDER BY c._ts DESC"
            )
            results = list(
                container.query_items(query=query, enable_cross_partition_query=True)
            )

            if results:
                return results[0]
            else:
                return None

        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Error fetching action-card with key '{action_card_key}': {e}")
            return None

    @classmethod
    def create_action_card_resources(
        cls,
        action_card_doc: Dict,
        *,
        global_doc: Optional[Dict] = None,
        language_id: str = "",
        allowed_versions: Optional[List[str]] = None,
        resource_type: str = "action-card",
        module_icon_asset_id: Optional[str] = None,  # NEW: Accept module icon
        screens_container = None  # NEW: Pass cosmos DB screens container to fetch translated headings
    ) -> list:
        """Create resource data from action-card document (or similar structured docs like drugs/procedures).

        Converts content to markdown and creates ResourcePostRequestData objects.
        
        CRITICAL: Global (langId="") is BOTH identity AND original content.
        - global_doc provides title/description (identity + original)
        - action_card_doc provides markdown content only

        Args:
            action_card_doc: The document from Cosmos DB (for markdown generation)
            global_doc: The Global document (langId="") for identity AND original content
            language_id: Explicit language ID if available
            allowed_versions: List of versions to process
            resource_type: Type of resource (action-card, drug, procedure) for asset tagging
        """
        resources = []

        # Extract basic info from GLOBAL (identity + original content)
        # Use global_doc for title/description if explicitly passed, else action_card_doc
        identity_doc = global_doc if global_doc else action_card_doc
        description = identity_doc.get("description") or identity_doc.get("title") or "Untitled Resource"
        
        # Icon from global or action_card_doc
        icon_path = action_card_doc.get("icon") or action_card_doc.get("image") or ""
        if not icon_path and global_doc:
            icon_path = global_doc.get("icon") or global_doc.get("image") or ""

        # CRITICAL: For drugs/procedures, inherit module icon if no specific icon
        skip_icon_upload = False
        if resource_type in ("drug", "procedure") and not icon_path and module_icon_asset_id:
            icon_asset_id = module_icon_asset_id
            print(f"  ✓ {resource_type.title()} inheriting module icon: {module_icon_asset_id[:8]}...")
            skip_icon_upload = True
        
        # Try to find language ID in doc, fallback to passed ID
        # CRITICAL FIX: Detect UUID-like IDs (document IDs) and don't use them as language IDs
        doc_lang_id = action_card_doc.get("langId") or action_card_doc.get("language_id")
        import re as _uuid_check
        is_uuid_like = doc_lang_id and _uuid_check.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', doc_lang_id, _uuid_check.IGNORECASE)
        if is_uuid_like:
            # This is a document ID, not a language ID - use caller's language_id
            lang_id = language_id
        else:
            lang_id = doc_lang_id or language_id
        country_code = action_card_doc.get("countryCode", "")
        # Prefer human-readable language name fields; do NOT fall back to lang_id
        language_name = (
            action_card_doc.get("languageName")
            or action_card_doc.get("language_name")
            or action_card_doc.get("langName")
            or action_card_doc.get("lang_name")
            or action_card_doc.get("language")
        )
        created_by = action_card_doc.get("LastUpdatedBy", "System")

        # Get region from language ID
        region = cls._get_region_from_language_id(lang_id)

        # Process icon if present
        if not skip_icon_upload:
            icon_asset_id = (
                cls._download_and_upload_icon(icon_path, lang_id) if icon_path else None
            )

        # Create markdown files to temp_data for each version using converter
        # CONTENT RULE: Use Local Doc for Markdown Generation
        temp_dir = get_temp_directory()
        temp_dir.mkdir(parents=True, exist_ok=True)

        # FETCH CHAPTER HEADINGS FROM SCREENS TABLE
        if screens_container and lang_id:
            chapters = action_card_doc.get("chapters", [])
            for chapter in chapters:
                chapter_key = chapter.get("key")
                if chapter_key:
                    screen_key = f"chapter:{chapter_key}"
                    try:
                        query = "SELECT * FROM c WHERE c._table = 'screens' AND c.key = @key AND c.langId = @langId"
                        parameters = [
                            {"name": "@key", "value": screen_key},
                            {"name": "@langId", "value": lang_id}
                        ]
                        screens = list(screens_container.query_items(
                            query=query, parameters=parameters, enable_cross_partition_query=True
                        ))
                        if screens:
                            screen_doc = screens[0]
                            chapter["screen_translated_title"] = screen_doc.get("translated")
                            chapter["screen_adapted_title"] = screen_doc.get("adapted")
                            chapter["screen_content_title"] = screen_doc.get("content")
                    except Exception as e:
                        print(f"  ⚠ Failed to fetch screen for chapter {chapter_key}: {e}")

        md_files = convert_action_card_to_markdown_files(
            action_card_doc,
            output_dir=str(temp_dir),
            region=region,
            country_code=country_code,
            language_name=language_name,
            allowed_versions=allowed_versions,
        )

        allowed = set(allowed_versions or ("original", "adapted", "translated"))

        # Create resources for each version that has a markdown file
        for version_name, md_path in md_files.items():
            if version_name not in allowed:
                continue
            # Upload markdown file as asset (do not delete local file)
            # Use the specific resource_type for tagging (action-card, drug, procedure)
            content_asset_id = cls._upload_asset(md_path, resource_type)
            if not content_asset_id:
                print(
                    f"Warning: Failed to upload markdown asset for "
                    f"{description} ({version_name}) from {md_path}"
                )
                continue

            # Determine language_id for this version
            if version_name == "original":
                # Original content - use empty lang_id or the document's
                # lang_id if it's the original
                resource_lang_id = "" if not lang_id else lang_id
            else:
                # Adapted/translated versions use the document's lang_id
                resource_lang_id = lang_id

            # Create resource title - CLEAN
            resource_title = clean_and_resolve_title(description, action_card_doc.get("key", ""))

            # Map version to content_type expected by API
            if version_name in ("original", "adapted", "translated"):
                content_type = version_name
            else:
                # Default to 'original' if unexpected key
                content_type = "original"
            
            # Description - CLEAN
            clean_desc = clean_and_resolve_title(description, action_card_doc.get("key", ""))
            long_desc = clean_desc

            # Create resource
            resource = ResourcePostRequestData(
                title=resource_title,
                description=long_desc,
                icon=icon_asset_id,
                content=content_asset_id,  # Asset ID instead of text
                language_id=resource_lang_id,
                region=region,
                content_type=content_type,
                created_by=created_by,
            )

            print(f"Created action-card resource: {resource}")

            resources.append(resource)

        return resources

    @classmethod
    def _convert_card_to_markdown(cls, card: Dict, version_key: str) -> str:
        """Convert a card's Draft.js content to markdown.

        Args:
            card: Card dictionary with content, adapted, translated fields
            version_key: Which version to extract ('content', 'adapted',
                        'translated')

        Returns:
            Markdown string representation of the card content
        """
        content = card.get(version_key)
        if not content or not isinstance(content, dict):
            return ""

        card_type = card.get("type", "paragraph")
        blocks = content.get("blocks", [])

        if not blocks:
            return ""

        markdown_lines = []

        for block in blocks:
            text = block.get("text", "").strip()
            if not text:
                continue

            # Apply inline styles
            inline_styles = block.get("inlineStyleRanges", [])
            styled_text = cls._apply_inline_styles(text, inline_styles)

            # Apply block-level formatting based on card type
            if card_type == "header":
                markdown_text = f"# {styled_text}"
            elif card_type == "subheader":
                markdown_text = f"## {styled_text}"
            elif card_type == "ul" or block.get("type") == "unordered-list-item":
                markdown_text = f"- {styled_text}"
            elif card_type == "ol" or block.get("type") == "ordered-list-item":
                # For ordered lists, we'd need to track numbering, but for
                # simplicity:
                markdown_text = f"1. {styled_text}"
            elif card_type == "important_text":
                markdown_text = f"> {styled_text}"
            else:
                markdown_text = styled_text

            markdown_lines.append(markdown_text)

        return "\n".join(markdown_lines)

    @classmethod
    def _apply_inline_styles(cls, text: str, style_ranges: list) -> str:
        """Apply inline styles (bold, italic, etc.) to text.

        Args:
            text: Original text
            style_ranges: List of style range objects with offset, length,
                         style

        Returns:
            Text with markdown formatting applied
        """
        if not style_ranges:
            return text

        # Sort by offset in reverse order to avoid index shifting
        style_ranges.sort(key=lambda r: r.get("offset", 0), reverse=True)

        styled_text = text

        for style_range in style_ranges:
            offset = style_range.get("offset", 0)
            length = style_range.get("length", 0)
            style = style_range.get("style", "")

            if offset < 0 or offset + length > len(styled_text):
                continue

            # Extract the text segment
            segment = styled_text[offset : offset + length]

            # Apply style
            if style == "BOLD":
                styled_segment = f"**{segment}**"
            elif style == "ITALIC":
                styled_segment = f"*{segment}*"
            elif style == "UNDERLINE":
                # HTML since markdown doesn't have underline
                styled_segment = f"<u>{segment}</u>"
            elif style == "STRIKETHROUGH":
                styled_segment = f"~~{segment}~~"
            else:
                styled_segment = segment

            # Replace in text
            styled_text = (
                styled_text[:offset] + styled_segment + styled_text[offset + length :]
            )

        return styled_text

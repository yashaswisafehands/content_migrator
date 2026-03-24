import csv
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import requests
from azure.cosmos import ContainerProxy

from configs import LME_BASE_URL, JWT_TOKEN, _get_assets_base_url
from factories import DataFactory
from path_utils import ensure_parent_dir, get_mappings_file
from md_converter_new import convert_about_to_md_versions


class SettingsScreenMigrator:
    """
    Migrator for Settings Screen data.
    
    Flow:
    1. POST original content (English/GLOBAL) first → get data_id
    2. PATCH translated content for each language with derived_from_id linking to original
    
    Payload structure:
    {
        "slug": "settings_screen",
        "data": [
            {"key": "upq-thankyou", "content": "asset_id.md", "icon": null},
            {"key": "developers", "content": "asset_id.md", "icon": "icon_asset_id"},
            ...
        ],
        "content_type": "original" | "translated",
        "region": "GLOBAL" | "india",
        "created_by": "System"
    }
    """
    
    # CSV file to store screen data mappings (stored in mappings folder for Stage 2)
    MAPPING_FILENAME = "settings_screen_data_mapping.csv"
    
    def __init__(self, container: ContainerProxy, language_mapping: Dict[str, Dict[str, str]]):
        self.container = container
        self.language_mapping = language_mapping
        
        # Setup logging
        self.logger = logging.getLogger("SettingsScreenMigrator")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        
        # Store original data_id for linking translations
        self._original_data_id: Optional[str] = None
        
        # Cache: maps lme_language_id -> data_id (loaded from CSV)
        self._data_id_cache: Dict[str, str] = {}
        
        # Load existing mappings from CSV
        self._load_mapping_from_csv()

    def _load_mapping_from_csv(self):
        """Load existing screen data mappings from CSV (for translated screen linking)."""
        mapping_path = get_mappings_file(self.MAPPING_FILENAME)
        if not mapping_path.exists():
            return
        
        try:
            with open(mapping_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    lme_lang_id = row.get("lme_language_id", "")
                    data_id = row.get("data_id", "")
                    if lme_lang_id and data_id:
                        self._data_id_cache[lme_lang_id] = data_id
            
            if self._data_id_cache:
                self.logger.info(f"Loaded {len(self._data_id_cache)} screen data mappings from CSV")
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.warning(f"Error loading screen data mapping: {e}")

    def _save_mapping_to_csv(self, lme_language_id: str, data_id: str, content_type: str = "original"):
        """Save screen data mapping to CSV (called after successful POST)."""
        mapping_path = get_mappings_file(self.MAPPING_FILENAME)
        ensure_parent_dir(mapping_path)
        
        file_exists = mapping_path.exists()
        
        try:
            with open(mapping_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["lme_language_id", "data_id", "content_type"])
                writer.writerow([lme_language_id, data_id, content_type])
            
            # Update in-memory cache
            self._data_id_cache[lme_language_id] = data_id
            self.logger.info(f"Saved screen data mapping: {lme_language_id} -> {data_id}")
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.error(f"Error saving screen data mapping: {e}")

    def get_original_data_id(self) -> Optional[str]:
        """
        Get the original (GLOBAL) data_id for linking translated content.
        Reads from cache (loaded from CSV).
        """
        return self._data_id_cache.get("GLOBAL")

    def _fetch_existing_data_id(self, language_id: str) -> Optional[str]:
        """
        Fetch existing data_id from API for a language.
        GET /languages/{language_id}/settings_screen_data
        """
        # Check cache first
        if language_id in self._data_id_cache:
            cached = self._data_id_cache[language_id]
            self.logger.info(f"Using cached data_id for {language_id}: {cached}")
            return cached
        
        url = f"{LME_BASE_URL}/languages/{language_id}/settings_screen_data"
        headers = {"Authorization": f"Bearer {JWT_TOKEN}"}
        
        try:
            resp = requests.get(url, headers=headers)
            
            if resp.status_code == 200:
                data = resp.json()
                
                # Response can be a list or single object
                if isinstance(data, list) and len(data) > 0:
                    data_id = data[0].get("data_id")
                elif isinstance(data, dict):
                    data_id = data.get("data_id")
                else:
                    data_id = None
                
                if data_id:
                    self._data_id_cache[language_id] = data_id
                    self.logger.info(f"Found existing data_id for {language_id}: {data_id}")
                    return data_id
            
            return None
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.error(f"Error fetching existing data_id: {e}")
            return None

    def _fetch_about_sections(self, cosmos_lang_id: str) -> List[Dict[str, Any]]:
        """Fetch all about sections for a language from Cosmos DB."""
        query = f"SELECT * FROM c WHERE c._table='about' AND c.langId='{cosmos_lang_id}' ORDER BY c._ts DESC"
        
        try:
            items = list(self.container.query_items(
                query=query,
                enable_cross_partition_query=True
            ))
            
            # Deduplicate by section (keep latest)
            seen_sections = set()
            unique_items = []
            for doc in items:
                section = doc.get("section", "about")
                if section not in seen_sections:
                    seen_sections.add(section)
                    unique_items.append(doc)
            
            return unique_items
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.error(f"Error fetching about sections: {e}")
            return []

    def _find_icon_in_doc(self, doc: Dict[str, Any], version: str = "translated") -> Optional[str]:
        """Find the first image in the document and return its source path."""
        for chapter in doc.get("chapters", []):
            for card in chapter.get("cards", []):
                if card.get("type") == "image":
                    # Check version first, then fallback
                    content = card.get(version) or card.get("adapted") or card.get("content") or {}
                    src = content.get("src")
                    if src:
                        return src
        return None

    def _upload_icon(self, icon_src: str, region: str = "") -> Optional[str]:
        """Download and upload an icon, returning the asset_id."""
        try:
            # If icon_src is already a full URL, use it directly
            if icon_src.startswith("http://") or icon_src.startswith("https://"):
                img_url = icon_src
                # Ensure it ends with .png
                if not img_url.lower().endswith(".png"):
                    img_url = f"{img_url}.png"
                
                # Download and upload directly using the full URL
                asset_id = DataFactory._download_and_upload_asset(img_url, None, "icon")
                return asset_id
            
            # Otherwise, treat as relative path and construct URL
            if icon_src.startswith("/"):
                icon_src = icon_src[1:]
            
            # Construct path with region prefix
            path_parts = [p for p in [region, icon_src] if p]
            joined_path = "/".join(path_parts)
            
            base_url = _get_assets_base_url().rstrip("/")
            img_url = f"{base_url}/images/{joined_path}.png"
            
            # Download and upload using DataFactory
            asset_id = DataFactory._download_and_upload_asset(img_url, None, "icon")
            return asset_id
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.warning(f"Failed to upload icon {icon_src}: {e}")
            return None

    def _upload_markdown_content(self, doc: Dict[str, Any], version: str = "translated") -> Optional[str]:
        """Convert document to markdown and upload as asset, returning asset_id."""
        try:
            # Convert to markdown
            md_versions = convert_about_to_md_versions(doc)
            
            # Select version
            if version == "original":
                markdown_content = md_versions.get("content", "")
            else:
                markdown_content = md_versions.get("translated") or md_versions.get("content", "")
            
            if not markdown_content.strip():
                self.logger.warning(f"No markdown content for section '{doc.get('section', 'unknown')}'")
                return None
            
            # Replace any image paths with LME asset IDs before upload
            try:
                from md_converter_new import process_embedded_images
                markdown_content = process_embedded_images(markdown_content)
            except Exception as e:
                from error_logger import log_error
                log_error("Captured Exception", exc=e)
                self.logger.warning(f"process_embedded_images failed: {e}")
            
            # Write to temp file and upload
            section = doc.get("section", "about").replace(" ", "_").replace("-", "_")
            temp_dir = tempfile.mkdtemp()
            temp_file = os.path.join(temp_dir, f"{section}_{version}.md")
            
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(markdown_content)
            
            # Upload as asset
            asset_id = DataFactory._upload_asset(temp_file, "action-card")  # text/markdown
            
            # Cleanup
            try:
                os.remove(temp_file)
                os.rmdir(temp_dir)
            except:
                pass
            
            return asset_id
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.error(f"Failed to upload markdown: {e}")
            return None

    def _build_screen_data_items(
        self, 
        about_docs: List[Dict[str, Any]], 
        version: str = "translated",
        region: str = ""
    ) -> List[Dict[str, Any]]:
        """
        Build the data array for settings_screen_data payload.
        
        Each about document becomes one item:
        {
            "key": "section-name",
            "content": "asset_id.md",
            "icon": "icon_asset_id" or null
        }
        """
        data_items = []
        
        for doc in about_docs:
            section = doc.get("section", "about")
            self.logger.info(f"   Processing section: {section}")
            
            # Upload markdown content
            content_asset_id = self._upload_markdown_content(doc, version)
            if not content_asset_id:
                self.logger.warning(f"   Skipping section '{section}' - no content asset")
                continue
            
            # Find and upload icon (if exists)
            icon_src = self._find_icon_in_doc(doc, version)
            icon_asset_id = None
            if icon_src:
                icon_asset_id = self._upload_icon(icon_src, region)
            
            data_items.append({
                "key": section,
                "content": content_asset_id,
                "icon": icon_asset_id
            })
            
            self.logger.info(f"   ✅ {section}: content={content_asset_id}, icon={icon_asset_id}")
        
        return data_items

    def _post_original_screen_data(
        self, 
        language_id: str, 
        data_items: List[Dict[str, Any]],
        region: str = "GLOBAL",
        is_global: bool = False
    ) -> Optional[str]:
        """
        POST original screen data to LME API.
        Returns data_id for linking translations.
        
        For global content (is_global=True): POST /settings-screens/
        For language-specific: POST /languages/{language_id}/settings_screen_data
        """
        # Check cache first (use "GLOBAL" key for global content)
        cache_key = "GLOBAL" if is_global else language_id
        if cache_key in self._data_id_cache:
            cached = self._data_id_cache[cache_key]
            self.logger.info(f"Using cached data_id for {cache_key}: {cached}")
            return cached
        
        headers = {
            "Authorization": f"Bearer {JWT_TOKEN}",
            "Content-Type": "application/json"
        }
        
        if is_global:
            # Use /settings-screens/ endpoint for global content (no language_id in URL)
            url = f"{LME_BASE_URL}/settings-screens/"
            
            # Check if already exists
            existing = self._fetch_existing_global_data_id()
            if existing:
                return existing
            
            payload = {
                "data": data_items,
                "language_id": "",  # Empty for global content
                "region": region,
                "content_type": "original",
                "created_by": "System"
            }
        else:
            # Use /languages/{language_id}/settings_screen_data for language-specific
            url = f"{LME_BASE_URL}/languages/{language_id}/settings_screen_data"
            
            # Check if already exists
            existing = self._fetch_existing_data_id(language_id)
            if existing:
                return existing
            
            payload = {
                "data": data_items,
                "language_id": "",  # Empty for original content
                "region": region,
                "content_type": "original",
                "created_by": "System"
            }
        
        self.logger.info(f"POSTing original screen data to {url}")
        self.logger.debug(f"Payload: {json.dumps(payload, indent=2)}")
        
        try:
            resp = requests.post(url, json=payload, headers=headers)
            
            if resp.status_code in (200, 201):
                data = resp.json()
                data_id = data.get("data_id") or data.get("id")
                version_id = data.get("version_id")
                
                # Try to get version_id from versions array if not at top level
                if not version_id and "versions" in data and data["versions"]:
                    version_id = data["versions"][0].get("screen_data_version_id")
                
                self.logger.info(f"✅ Created original screen data: data_id={data_id}")
                
                # Save mapping to CSV with "GLOBAL" key for global content
                if data_id:
                    self._save_mapping_to_csv("GLOBAL" if is_global else language_id, data_id, "original")
                
                # Activate version — fallback GET if response didn't include version_id
                if data_id and not version_id:
                    self.logger.info(f"   → version_id missing from POST response, fetching via GET...")
                    try:
                        get_url = f"{LME_BASE_URL}/settings-screens/{data_id}"
                        g_resp = requests.get(get_url, headers=headers)
                        if g_resp.status_code == 200:
                            g_data = g_resp.json()
                            if g_data.get("versions"):
                                version_id = g_data["versions"][0].get("screen_data_version_id")
                            if not version_id:
                                curr = g_data.get("current_version") or {}
                                version_id = curr.get("screen_data_version_id")
                    except Exception as ve:
                        from error_logger import log_error
                        log_error("Captured Exception", exc=ve)
                        self.logger.warning(f"   Fallback GET for version_id failed: {ve}")

                if data_id and version_id:
                    self._set_current_version(data_id, version_id)
                elif data_id:
                    self.logger.warning(f"   ⚠️ No version_id found for data_id={data_id}, skipping activation")
                
                return data_id
            
            elif resp.status_code == 409 or "already exists" in resp.text.lower():
                self.logger.info("⚠️ Screen data already exists, fetching existing data_id...")
                if is_global:
                    return self._fetch_existing_global_data_id()
                else:
                    return self._fetch_existing_data_id(language_id)
            
            else:
                self.logger.error(f"❌ POST failed: {resp.status_code} - {resp.text}")
                return None
                
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.error(f"❌ Exception posting original: {e}")
            return None

    def _fetch_existing_global_data_id(self) -> Optional[str]:
        """
        Fetch existing global data_id from API.
        GET /settings-screens/
        """
        # Check cache first
        if "GLOBAL" in self._data_id_cache:
            cached = self._data_id_cache["GLOBAL"]
            self.logger.info(f"Using cached data_id for GLOBAL: {cached}")
            return cached
        
        url = f"{LME_BASE_URL}/settings-screens/"
        headers = {"Authorization": f"Bearer {JWT_TOKEN}"}
        
        try:
            resp = requests.get(url, headers=headers)
            
            if resp.status_code == 200:
                data = resp.json()
                
                # Find settings_screen slug
                if isinstance(data, list):
                    for item in data:
                        if item.get("slug") == "settings_screen":
                            data_id = item.get("data_id") or item.get("id")
                            if data_id:
                                self._data_id_cache["GLOBAL"] = data_id
                                self._save_mapping_to_csv("GLOBAL", data_id, "original")
                                self.logger.info(f"Found existing global data_id: {data_id}")
                                return data_id
            
            return None
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.error(f"Error fetching existing global data_id: {e}")
            return None

    def _patch_translated_screen_data(
        self, 
        language_id: str, 
        data_items: List[Dict[str, Any]],
        region: str,
        derived_from_id: str
    ) -> bool:
        """
        PATCH translated screen data to LME API.
        Creates a new translated version on the existing screen data node.
        
        PATCH /settings-screens/{data_id}
        
        Args:
            language_id: LME language ID for the translated content
            data_items: Screen data items (sections)
            region: Region for the content
            derived_from_id: The data_id of the original/GLOBAL screen data
        """
        # Use the derived_from_id as the data_id to patch
        # This creates a new translated VERSION on the same node
        url = f"{LME_BASE_URL}/settings-screens/{derived_from_id}"
        headers = {
            "Authorization": f"Bearer {JWT_TOKEN}",
            "Content-Type": "application/json"
        }
        
        # Payload matching ScreenDataUpdateRequestData schema
        payload = {
            "data": data_items,
            "language_id": language_id,
            "region": region,
            "content_type": "translated",
            "derived_from_id": derived_from_id,
            "updated_by": "System"
        }
        
        self.logger.info(f"PATCHing translated screen data to {url}")
        
        try:
            resp = requests.patch(url, json=payload, headers=headers)
            
            if resp.status_code in (200, 201):
                data = resp.json()
                data_id = data.get("data_id") or data.get("id")
                version_id = data.get("version_id")
                
                # Try to get version_id from versions array
                if not version_id and "versions" in data and data["versions"]:
                    version_id = data["versions"][0].get("screen_data_version_id")
                
                self.logger.info(f"✅ Created translated screen data for {region} (lang={language_id})")
                
                # Activate version — fallback GET if response didn't include version_id
                if data_id and not version_id:
                    self.logger.info(f"   → version_id missing from PATCH response, fetching via GET...")
                    try:
                        get_url = f"{LME_BASE_URL}/settings-screens/{data_id}"
                        g_resp = requests.get(get_url, headers=headers)
                        if g_resp.status_code == 200:
                            g_data = g_resp.json()
                            if g_data.get("versions"):
                                version_id = g_data["versions"][0].get("screen_data_version_id")
                            if not version_id:
                                curr = g_data.get("current_version") or {}
                                version_id = curr.get("screen_data_version_id")
                    except Exception as ve:
                        from error_logger import log_error
                        log_error("Captured Exception", exc=ve)
                        self.logger.warning(f"   Fallback GET for version_id failed: {ve}")

                if data_id and version_id:
                    self._set_current_version(data_id, version_id)
                elif data_id:
                    self.logger.warning(f"   ⚠️ No version_id found for data_id={data_id}, skipping activation")
                
                return True
            else:
                self.logger.error(f"❌ PATCH failed: {resp.status_code} - {resp.text}")
                return False
                
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.error(f"❌ Exception patching translated: {e}")
            return False

    def _get_existing_data_id(self, language_id: str) -> Optional[str]:
        """Get existing screen data_id for a language."""
        try:
            url = f"{LME_BASE_URL}/languages/{language_id}/settings_screen_data"
            headers = {"Authorization": f"Bearer {JWT_TOKEN}"}
            
            resp = requests.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            
            data = resp.json()
            
            if isinstance(data, list) and data:
                return data[0].get("data_id") or data[0].get("id")
            elif isinstance(data, dict):
                if "items" in data and data["items"]:
                    return data["items"][0].get("data_id") or data["items"][0].get("id")
                return data.get("data_id") or data.get("id")
            
            return None
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.error(f"Error getting existing data_id: {e}")
            return None

    def _set_current_version(self, data_id: str, version_id: str) -> bool:
        if os.environ.get("MIGRATE_ENV") == "devcontent":
            print(f"  → Skipping screen data activation for devcontent.")
            return True
        """Activate a screen data version using the settings-screen-specific endpoint."""
        try:
            url = f"{LME_BASE_URL}/settings-screens/versions/{version_id}/status"
            headers = {
                "Authorization": f"Bearer {JWT_TOKEN}",
                "Content-Type": "application/json"
            }
            payload = {"status": "active", "updated_by": "System"}

            resp = requests.patch(url, json=payload, headers=headers)
            if resp.status_code in (200, 201, 204):
                self.logger.info(f"   ✓ Activated screen data version: {version_id}")
                return True
            else:
                self.logger.warning(f"   Could not activate version {version_id}: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.warning(f"   Exception activating version: {e}")
            return False

    def migrate_settings_screen(self, base_cosmos_lang_id: str = None):
        """
        Main migration method for settings screen data.
        
        Flow:
        1. Check if GLOBAL original already exists (from --migrate-global)
        2. If yes: Skip Step 1, PATCH ALL languages as translated
        3. If no: POST original from base language, then PATCH remaining as translated
        
        Args:
            base_cosmos_lang_id: Cosmos ID for the base/English language.
                                If None, tries to find it from language_mapping.
        """
        self.logger.info("=" * 60)
        self.logger.info("Starting Settings Screen Migration")
        self.logger.info("=" * 60)
        
        # Check if GLOBAL original already exists (created via --migrate-global)
        global_data_id = self._data_id_cache.get("GLOBAL")
        
        if global_data_id:
            self.logger.info(f"✅ Found existing GLOBAL screen data: {global_data_id}")
            self.logger.info("   Skipping Step 1 (original already exists from global migration)")
            original_data_id = global_data_id
            base_cosmos_lang_id = None  # No base language - all languages are translated
        else:
            # No GLOBAL found - need to create original from base language
            self.logger.info("ℹ️ No GLOBAL screen data found, will create original from base language")
            
            # Find base language (English/GLOBAL or first available)
            if not base_cosmos_lang_id:
                # Try to find English in mapping first
                for cosmos_id, info in self.language_mapping.items():
                    if info.get("region", "").lower() == "global" or "english" in info.get("language_name", "").lower():
                        base_cosmos_lang_id = cosmos_id
                        break
                
                # If no English/GLOBAL found, use the first available language
                if not base_cosmos_lang_id and self.language_mapping:
                    base_cosmos_lang_id = next(iter(self.language_mapping.keys()))
                    self.logger.info(f"No English/GLOBAL found, using first available language: {base_cosmos_lang_id}")
            
            if not base_cosmos_lang_id:
                self.logger.error("No languages found in mapping")
                return
            
            base_lme_info = self.language_mapping.get(base_cosmos_lang_id, {})
            base_lme_id = base_lme_info.get("lme_language_id") or base_lme_info.get("lme_id")
            
            if not base_lme_id:
                self.logger.error(f"No LME ID found for base language {base_cosmos_lang_id}")
                return
            
            # Step 1: Fetch and POST original content
            self.logger.info("\n📌 Step 1: Posting ORIGINAL screen data")
            self.logger.info("-" * 40)
            
            base_about_docs = self._fetch_about_sections(base_cosmos_lang_id)
            if not base_about_docs:
                self.logger.error("No about sections found for base language")
                return
            
            self.logger.info(f"Found {len(base_about_docs)} about sections for base language")
            
            # Build original data items
            original_data_items = self._build_screen_data_items(
                base_about_docs, 
                version="original", 
                region=""
            )
            
            if not original_data_items:
                self.logger.error("No data items built for original content")
                return
            
            # POST original
            original_data_id = self._post_original_screen_data(base_lme_id, original_data_items)
            
            if not original_data_id:
                self.logger.error("Failed to create/get original screen data")
                return
            
            self.logger.info(f"Original data_id: {original_data_id}")
        
        self._original_data_id = original_data_id
        
        # Step 2: PATCH translated content for each language
        self.logger.info("\n📌 Step 2: Patching TRANSLATED screen data")
        self.logger.info("-" * 40)
        
        languages_patched = 0
        for cosmos_lang_id, lme_info in self.language_mapping.items():
            # Skip base language ONLY if we created original from it
            # If GLOBAL exists, we patch ALL languages as translated
            if base_cosmos_lang_id and cosmos_lang_id == base_cosmos_lang_id:
                self.logger.info(f"   Skipping {cosmos_lang_id} (used as base for original)")
                continue
            
            lme_lang_id = lme_info.get("lme_language_id") or lme_info.get("lme_id")
            region = lme_info.get("region", "")
            lang_name = lme_info.get("language_name", cosmos_lang_id)
            
            if not lme_lang_id:
                self.logger.warning(f"Skipping {lang_name}: No LME language ID")
                continue
            
            self.logger.info(f"\n🌐 Processing: {lang_name} (region={region})")
            
            # Fetch about sections for this language
            about_docs = self._fetch_about_sections(cosmos_lang_id)
            if not about_docs:
                self.logger.warning(f"No about sections for {lang_name}")
                continue
            
            self.logger.info(f"   Found {len(about_docs)} about sections")
            
            # Build translated data items
            translated_data_items = self._build_screen_data_items(
                about_docs, 
                version="translated", 
                region=region
            )
            
            if not translated_data_items:
                self.logger.warning(f"No data items for {lang_name}")
                continue
            
            # PATCH translated
            success = self._patch_translated_screen_data(
                lme_lang_id, 
                translated_data_items, 
                region, 
                original_data_id
            )
            if success:
                languages_patched += 1
        
        self.logger.info("\n" + "=" * 60)
        self.logger.info(f"Settings Screen Migration Complete! ({languages_patched} languages patched)")
        self.logger.info("=" * 60)

    # Legacy method for backward compatibility
    def migrate_about_screen(self):
        """Legacy method - calls migrate_settings_screen."""
        self.migrate_settings_screen()

    def migrate_global_about_screen(self):
        """
        Migrate global about screens (where langId is empty or not defined).
        This is called by GlobalContentMigrator for Stage 2.
        """
        self.logger.info("=" * 60)
        self.logger.info("Starting Global About Screen Migration")
        self.logger.info("=" * 60)
        
        # Query global about sections (empty or no langId)
        query = "SELECT * FROM c WHERE c._table='about' AND (NOT IS_DEFINED(c.langId) OR c.langId = '') ORDER BY c._ts DESC"
        
        try:
            items = list(self.container.query_items(
                query=query,
                enable_cross_partition_query=True
            ))
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            self.logger.error(f"Error querying global about sections: {e}")
            return
        
        if not items:
            self.logger.info("No global about sections found (this is normal if all content is language-specific)")
            return
        
        # Deduplicate by section (keep latest)
        seen_sections = set()
        unique_docs = []
        for doc in items:
            section = doc.get("section", "about")
            if section not in seen_sections:
                seen_sections.add(section)
                unique_docs.append(doc)
        
        self.logger.info(f"Found {len(unique_docs)} unique global about sections")
        
        # Build data items from original content
        data_items = self._build_screen_data_items(unique_docs, version="original", region="")
        
        if not data_items:
            self.logger.info("No data items to migrate for global about screens")
            return
        
        # POST the global about screens using /settings-screens/ endpoint
        # No language_id required for global content!
        data_id = self._post_original_screen_data(
            language_id="",  # Not used when is_global=True
            data_items=data_items, 
            region="GLOBAL",
            is_global=True  # Use /settings-screens/ endpoint
        )
        
        if data_id:
            self.logger.info(f"✅ Global about screens migrated successfully: data_id={data_id}")
        else:
            self.logger.warning("Failed to migrate global about screens")
        
        self.logger.info("=" * 60)
        self.logger.info("Global About Screen Migration Complete!")
        self.logger.info("=" * 60)

    def migrate_screen(self, key: str, slug: str):
        """Migrate a generic screen by key for all mapped languages."""
        self.logger.info(f"Starting migration of screen key='{key}' to slug='{slug}'...")
        
        for cosmos_lang_id, lme_info in self.language_mapping.items():
            lme_lang_id = lme_info.get("lme_language_id") or lme_info.get("lme_id")
            region = lme_info.get("region")
            
            if not lme_lang_id:
                self.logger.warning(f"Skipping screen '{key}' for {cosmos_lang_id}: Missing LME language ID")
                continue
            
            # Query for the screen data
            query = (
                f"SELECT * FROM c WHERE c._table='screens' "
                f"AND c.langId='{cosmos_lang_id}' "
                f"AND c.key = '{key}' "
                f"ORDER BY c._ts DESC OFFSET 0 LIMIT 1"
            )
            
            try:
                items = list(self.container.query_items(
                    query=query,
                    enable_cross_partition_query=True
                ))
                if not items:
                    self.logger.warning(f"No screen document found for key='{key}' lang='{cosmos_lang_id}'")
                    continue
                doc = items[0]
            except Exception as e:
                from error_logger import log_error
                log_error("Captured Exception", exc=e)
                self.logger.error(f"Error querying screen: {e}")
                continue
            
            # Build data item for this screen
            data_items = self._build_screen_data_items([doc], version="translated", region=region)
            
            if data_items:
                # For single screens, we can POST or PATCH depending on if original exists
                if self._original_data_id:
                    self._patch_translated_screen_data(lme_lang_id, data_items, region, self._original_data_id)
                else:
                    self._post_original_screen_data(lme_lang_id, data_items)
"""Resource migration functionality for Cosmos DB to LME migration."""

import csv
import re
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests
from azure.cosmos import CosmosClient

# NOTE: Posting is deferred; keep configs import removed.
from configs import (
    POST_RESOURCE, UPDATE_RESOURCE, ACTIVATE_RESOURCE_VERSION,
    JWT_TOKEN, LME_BASE_URL,
    POST_KLP, UPDATE_KLP, ACTIVATE_KLP_VERSION,
    DATABASE_NAME
)
from data_models import ResourcePostRequestData
from factories import DataFactory
from path_utils import ensure_parent_dir, get_mappings_file, get_processed_file, get_temp_directory
import time
from slug_utils import build_slug, merge_slug_parts, slugify
from text_utils import clean_and_resolve_title


class ResourceMigrator:
    """Handles migration of resources from Cosmos DB to LME."""

    def __init__(
        self,
        cosmos_client: CosmosClient,
        container,
        language_mapping: Dict[str, Dict[str, str]],
    ):
        self.cosmos_client = cosmos_client
        self.container = container
        self.language_mapping = language_mapping
        
        # HTTP session for connection pooling (performance optimization)
        self.session = requests.Session()
        if JWT_TOKEN:
            self.session.headers.update({"Authorization": f"Bearer {JWT_TOKEN}"})
        self.session.headers.update({"Content-Type": "application/json"})
        
        # slug -> resource_id
        self.resource_slug_mapping: Dict[str, str] = {}
        # cosmos_key -> slug (for linking resources to modules after queuing)
        self.cosmos_key_to_slug: Dict[str, str] = {}
        self._resource_mapping_path = get_mappings_file("resource_slug_mapping.csv")
        ensure_parent_dir(self._resource_mapping_path)
        # Queue file for resource POST payloads (sanitization step)
        # Requirements: resources.csv in processed_data
        self._resource_queue_path = get_processed_file("resources.csv")
        ensure_parent_dir(self._resource_queue_path)
        self.queued_slugs = set()
        self._load_queued_slugs()
        self._load_resource_slug_mapping()
        
        # KLP-specific paths (KLPs are separate from Resources in LME)
        self.klp_slug_mapping: Dict[str, str] = {}
        self._klp_mapping_path = get_mappings_file("klp_slug_mapping.csv")
        ensure_parent_dir(self._klp_mapping_path)
        


        self._klp_queue_path = get_processed_file("klps.csv")
        ensure_parent_dir(self._klp_queue_path)
        self.queued_klp_slugs = set()

        self._load_queued_klp_slugs()
        self._load_klp_slug_mapping()

    def _load_queued_slugs(self) -> None:
        """Load existing slugs from resources.csv to prevent duplicates."""
        if not self._resource_queue_path.exists():
            return
        try:
            with self._resource_queue_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if "slug" in row:
                        self.queued_slugs.add(row["slug"])
            print(f"Loaded {len(self.queued_slugs)} queued resource slugs")
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Could not load queued resource slugs: {e}")

    def _load_queued_klp_slugs(self) -> None:
        """Load existing slugs from klps.csv."""
        if not self._klp_queue_path.exists():
            return
        try:
            with self._klp_queue_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if "slug" in row:
                        slug = row["slug"]
                        self.queued_klp_slugs.add(slug)
            print(f"Loaded {len(self.queued_klp_slugs)} queued KLP slugs")
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Could not load queued KLP slugs: {e}")

    def _load_klp_slug_mapping(self) -> None:
        """Load existing KLP slug to klp_id mapping from CSV."""
        if not self._klp_mapping_path.exists():
            print("No existing KLP slug mapping found, starting fresh")
            return
        try:
            with self._klp_mapping_path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self.klp_slug_mapping[row["slug"]] = row["klp_id"]
            print(f"Loaded {len(self.klp_slug_mapping)} existing KLP mappings")
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Could not load KLP slug mapping: {e}")

    def _append_klp_slug_mapping(self, slug: str, klp_id: str) -> None:
        """Append a single slug->klp_id mapping to CSV (no overwrite)."""
        ensure_parent_dir(self._klp_mapping_path)
        need_header = not self._klp_mapping_path.exists() or self._klp_mapping_path.stat().st_size == 0
        with self._klp_mapping_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if need_header:
                writer.writerow(["slug", "klp_id"])
            writer.writerow([slug, klp_id])

    @staticmethod
    def _extract_resource_id_from_response(
        response: requests.Response,
    ) -> Optional[str]:
        """Try multiple ways to extract a resource ID from an API response.

        Priority:
        1) JSON fields: resource_id, id, resource.id
        2) Headers: Location or Content-Location (last non-empty segment)
        """
        # Try JSON
        try:
            data = response.json() if response.content else {}
            rid = (
                data.get("resource_id")
                or data.get("id")
                or (data.get("resource") or {}).get("id")
                or data.get("klp_id")
            )
            if rid:
                return rid
        except ValueError:
            # Not JSON or empty
            pass

        # Try headers
        headers = response.headers or {}
        loc = headers.get("Location") or headers.get("Content-Location")
        if loc:
            # Take last non-empty path segment
            cleaned = loc.rstrip("/")
            if cleaned:
                parts = [p for p in cleaned.split("/") if p]
                if parts:
                    return parts[-1]
        return None

    def _resolve_content_type(self, cosmos_lang_id: str) -> str:
        """
        Cosmos → LME content type mapping.

        Global (langId="")     → original
        WHO English (en-WHO)   → translated
        All other languages   → translated
        """
        if not cosmos_lang_id:
            return "original"
        return "translated"


    @staticmethod
    def _extract_version_id_from_response(
        response: requests.Response,
    ) -> Optional[str]:
        """Extract resource_version_id or klp_version_id from API response.
        
        IMPORTANT: For PATCH responses that create translated versions, the
        `current_original_version` contains the already-active original version.
        We must prioritize the `versions` array (which contains the newly created
        translated version) to ensure we activate the correct version.
        """
        try:
            data = response.json() if response.content else {}

            
            # PRIORITY: Check versions array FIRST — pick the latest version
            # (highest version number). For PATCH responses creating translated
            # versions, the new version appears here. Checking
            # current_original_version first would return the already-active
            # original and cause the translated version to remain in draft.
            versions = data.get("versions")
            if versions and isinstance(versions, list) and len(versions) > 0:
                # Sort by version number descending to get the latest
                best = max(
                    versions,
                    key=lambda v: float(v.get("version", 0)) if isinstance(v, dict) else 0,
                )
                if isinstance(best, dict):
                    vid = best.get("resource_version_id") or best.get("klp_version_id")
                    if vid:
                        return vid
            
            # Fallback: Check translated/adapted draft versions
            for draft_key in ("draft_translated_versions", "draft_adapted_versions"):
                drafts = data.get(draft_key)
                if drafts and isinstance(drafts, list) and len(drafts) > 0:
                    best_draft = max(
                        drafts,
                        key=lambda v: float(v.get("version", 0)) if isinstance(v, dict) else 0,
                    )
                    if isinstance(best_draft, dict):
                        vid = best_draft.get("resource_version_id") or best_draft.get("klp_version_id")
                        if vid:
                            return vid

            # ROOT FALLBACK: Check for direct version_id fields
            # This is intentionally placed below the versions arrays, because on PATCH the
            # root ID might refer to the older, currently active original version.
            version_id = (
                data.get("resource_version_id")
                or data.get("klp_version_id")
                or data.get("version_id")
            )
            if version_id:
                return version_id
            
            # Fallback: current_original_version (POST of a brand-new resource)
            current_original = data.get("current_original_version")
            if current_original and isinstance(current_original, dict):
                vid = current_original.get("resource_version_id") or current_original.get("klp_version_id")
                if vid:
                    return vid
            
            # KLP structure - check current_version
            current_version = data.get("current_version")
            if current_version and isinstance(current_version, dict):
                vid = current_version.get("klp_version_id")
                if vid:
                    return vid
            
            return None
        except (ValueError, IndexError, TypeError):
            return None

    def _activate_version(self, version_id: str, is_klp: bool = False) -> bool:
        if os.environ.get("MIGRATE_ENV") == "devcontent":
            print(f"  → Skipping resource/KLP activation for devcontent.")
            return True
        """Activate a resource or KLP version by calling status endpoint.
        
        Args:
            version_id: The resource_version_id or klp_version_id to activate
            is_klp: True if this is a KLP version, False for resource version
            
        Returns:
            True if activation succeeded, False otherwise
        """
        if not version_id:
            return False
        
        if is_klp:
            url = ACTIVATE_KLP_VERSION.format(version_id=version_id)
        else:
            url = ACTIVATE_RESOURCE_VERSION.format(version_id=version_id)
        

        
        payload = {"status": "active", "updated_by": "System"}
        
        try:
            response = self.session.patch(url, json=payload)
            if response.status_code in (200, 204):
                print(f"  ✓ Activated version: {version_id}")
                return True
            else:
                print(f"  ⚠ Failed to activate version {version_id}: {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            print(f"  ⚠ Error activating version {version_id}: {e}")
            return False

    def _upload_asset(self, file_path: str, asset_type: str) -> Optional[str]:
        """Upload asset using DataFactory."""
        return DataFactory._upload_asset(file_path, asset_type)

    def migrate_module_resources(self, module_doc: Dict, global_module_title: str = None) -> Dict[str, List[str]]:
        """Prepare all resources within a module and return mapping of
        resource types to slugs. Payloads are queued to CSV;

        Args:
            module_doc: The module document from Cosmos DB
            global_module_title: The English (global) module title for consistent naming

        Returns:
            Dict mapping resource types to lists of migrated resource IDs
        """
        resource_ids = {
            "videos": [],
            "actionCards": [],
            "procedures": [],
            "drugs": [],
            "keyLearningPoints": [],
        }

        # Load existing resource slug mapping
        self._load_resource_slug_mapping()

        # Videos
        video_ids = self._migrate_video_resources(module_doc, global_module_title=global_module_title)
        resource_ids["videos"] = video_ids

        # Migrate action cards
        action_card_ids = self._migrate_action_card_resources(module_doc)
        resource_ids["actionCards"] = action_card_ids

        # Procedures
        procedure_ids = self._migrate_procedure_resources(module_doc)
        resource_ids["procedures"] = procedure_ids

        # Get module icon for drugs (drugs inherit the module's icon)
        module_icon_asset_id = None
        module_icon_path = module_doc.get("icon") or module_doc.get("iconPath")
        if module_icon_path:
            cosmos_lang_id = module_doc.get("language_id") or module_doc.get("langId") or ""
            # IMPORTANT: Use asset_type="icon" ensuring it uses the images prefix, not videos
            module_icon_asset_id = self._get_or_create_icon_asset(module_icon_path, cosmos_lang_id, asset_type="icon")

            # FALLBACK: If "english WHO" path failed (404), try "africa" region
            if not module_icon_asset_id and "english%20WHO" in module_icon_path:
                print(f"  ℹ️  Primary icon failed. Trying fallback to 'africa' region...")
                fallback_path = module_icon_path.replace("english%20WHO", "africa")
                module_icon_asset_id = self._get_or_create_icon_asset(fallback_path, cosmos_lang_id, asset_type="icon")

            if module_icon_asset_id:
                print(f"  ✓ Module icon uploaded for drugs: {module_icon_asset_id[:8]}...")
            else:
                print(f"  ⚠️  CRITICAL: Failed to upload module icon from: {module_icon_path}")
                print(f"      → All drugs in this module will have missing icons!")
        else:
            print(f"  ℹ️  No module icon path found (checked: icon, iconPath fields)")

        # Drugs (pass module icon since drugs inherit their parent module's icon)
        drug_ids = self._migrate_drug_resources(module_doc, module_icon_asset_id)
        resource_ids["drugs"] = drug_ids
        
        # Key Learning Points
        klp_slugs = self._migrate_key_learning_point_resources(module_doc)
        resource_ids["keyLearningPoints"] = klp_slugs

        # Note: Mapping is persisted incrementally on creation;
        # no overwrite here

        return resource_ids

    def _migrate_video_resources(self, module_doc: Dict, global_module_title: str = None) -> List[str]:
        """Queue video resources for a module.
        
        IMPORTANT: Videos are language-specific only — no global originals exist.
        - Global modules (langId=""): Skip entirely, return empty list
        - Localized modules: POST directly as content_type="translated"
        
        Args:
            module_doc: The module document from Cosmos DB
            global_module_title: The English (global) module title for consistent naming
        """
        video_paths = module_doc.get("videos", []) or []
        cosmos_lang_id = module_doc.get("langId") or ""

        # ──────────────────────────────────────────────────────────
        # SKIP videos for global modules — no master global video data exists.
        # Videos will only be created per-language when localized modules are processed.
        # ──────────────────────────────────────────────────────────
        if not cosmos_lang_id:
            print("  ℹ️  Skipping videos for global module (no global video originals)")
            return []

        lme_lang_id, region = self._map_language_info(cosmos_lang_id)
        slugs: List[str] = []

        if video_paths:
            print(f"  → Migrating {len(video_paths)} videos for module")

        lang_info = self.language_mapping.get(cosmos_lang_id, {})
        # Slugify the language name so "India - Hindi" → "india-hindi" (not "india_-_hindi")
        raw_lang_name = lang_info.get("name", cosmos_lang_id)
        lang_name = slugify(raw_lang_name)
        
        # Use the global English module title for consistent naming across all languages.
        # This ensures video names like 'india-hindi_prolonged-labour_intro'
        # instead of translated module names (e.g. Hindi/Arabic text).
        raw_module_title = global_module_title or module_doc.get("title") or module_doc.get("description") or "module"
        module_name = slugify(raw_module_title)
        
        for video_path in video_paths:
            if not video_path:
                continue
                
            video_name = slugify(self._extract_display_title(video_path))
            asset_prefix = f"{lang_name}_{module_name}_{video_name}"
            
            video_asset_id = self._get_or_create_video_asset(video_path, cosmos_lang_id, asset_prefix, use_prefix=False)
            icon_asset_id = self._get_or_create_icon_asset(video_path, cosmos_lang_id, "video_icon", asset_prefix, use_prefix=False)
            if not video_asset_id:
                print(f"Skipping video '{video_path}' - missing asset upload")
                continue

            # Display Title (Simple/Clean): Use filename only — used as description fallback
            display_fallback = self._extract_display_title(video_path)
            
            # Translation Lookup
            # Key format: video:{video_path} (SINGULAR 'video' per user request)
            video_key = f"video:{video_path}"
            translated_text = self._get_screen_translation(video_key, cosmos_lang_id)
            
            # Title: dynamic naming convention (lang_name_module_name_video_name)
            # LME generates slug from title, so unique titles = unique slugs
            video_title = asset_prefix

            # Description: translated text -> display fallback
            video_description = translated_text if translated_text else display_fallback

            resource_data = ResourcePostRequestData(
                title=video_title,
                description=video_description,
                icon=icon_asset_id,
                content=video_asset_id,
                language_id=lme_lang_id or "",
                region=region,
                # ──────────────────────────────────────────────────────────
                # ALWAYS "translated" — videos have no global original to derive from.
                # They are posted directly for each language module.
                # ──────────────────────────────────────────────────────────
                content_type="translated",
                created_by=module_doc.get("LastUpdatedBy", "System"),
            )
            
            # Slug source = asset_prefix (matches title sent to LME, so LME-generated slug will align)
            # No video_path for slug — title already encodes lang + module + video context
            slug = self._create_or_update_resource(
                resource_data, "video", cosmos_language_id=cosmos_lang_id, slug_title_source=asset_prefix
            )
            if slug:
                slugs.append(slug)
        return slugs

    def _migrate_procedure_resources(self, module_doc: Dict) -> List[str]:
        """Queue procedure resources referenced by module.
        
        Same pattern as drugs - screens for description only.
        """
        procedure_keys = module_doc.get("procedures", []) or []
        cosmos_lang_id = module_doc.get("langId") or ""
        lme_lang_id, region = self._map_language_info(cosmos_lang_id)
        slugs: List[str] = []

        if procedure_keys:
            print(f"  → Migrating {len(procedure_keys)} procedures for module")

        for key in procedure_keys:
            if not key:
                continue
                
            # ──────────────────────────────────────────────────────────
            # LOCALIZED ENFORCEMENT DOCTRINE
            # ──────────────────────────────────────────────────────────
            if cosmos_lang_id:
                proc_doc = self._fetch_table_doc("procedures", key, cosmos_lang_id)
                if not proc_doc:
                    print(f"  ⚠ SKIP: Localized procedure '{key}' not found in {cosmos_lang_id}.")
                    continue
                # For localized docs, identity comes from global parent, but we do NOT fallback content
                global_doc = self._fetch_table_doc("procedures", key, "")
                if not global_doc:
                    print(f"  ⚠ STRUCTURAL VIOLATION: Global parent missing for localized procedure '{key}'. Skipping.")
                    continue
                identity_doc = global_doc
                content_doc = proc_doc
                is_standalone = False # Standalone concept is abolished under strict invariant
            else:
                # Global Module Parsing
                global_doc = self._fetch_table_doc("procedures", key, "")
                if not global_doc:
                    print(f"  ⚠ SKIP: Global procedure '{key}' not found in Cosmos.")
                    continue
                identity_doc = global_doc
                content_doc = global_doc
                is_standalone = False

            # 3. Generate markdown from cards
            resources = DataFactory.create_action_card_resources(
                content_doc,
                global_doc=identity_doc,
                language_id=cosmos_lang_id, 
                resource_type="procedure",
                screens_container=self.cosmos_client.get_database_client(DATABASE_NAME).get_container_client("screens")
            )
            
            valid_resources = self._select_valid_resource_versions(resources, cosmos_lang_id)
            
            if not valid_resources:
                print(f"  ⚠ Warning: No valid Markdown payload created for procedure '{key}'")
                continue

            # 4. SLUG SOURCE - From Global TITLE (not description)
            from text_utils import clean_and_resolve_title
            
            global_title = identity_doc.get("title") or identity_doc.get("description") or key
            slug_title_source = clean_and_resolve_title(global_title, key)
            original_title = clean_and_resolve_title(global_title, key)

            # 5. SCREENS TABLE - For DESCRIPTION only
            if cosmos_lang_id:
                trans_key = f"procedure:{key}"
                translated_desc = self._get_screen_translation(trans_key, cosmos_lang_id)
            else:
                translated_desc = None
                
            for res in valid_resources:
                if cosmos_lang_id:
                    if translated_desc:
                        if res == valid_resources[-1]:
                            print(f"  ✓ Screens translation for procedure description: '{translated_desc[:50]}...'")
                        res.description = translated_desc
                        res.title = original_title  # Keep original
                    else:
                        if res == valid_resources[-1]:
                            print(f"  ℹ️  No screens translation found for 'procedure:{key}', using original")
                        res.title = original_title
                        res.description = original_title
                else:
                    res.title = original_title
                    res.description = original_title

                # 6. Set LME metadata
                res.language_id = lme_lang_id or ""
                res.region = region
                if not cosmos_lang_id:
                    res.content_type = "original"
                
                slug = self._create_or_update_complex_resource(
                    res, 
                    key, 
                    "procedure", 
                    cosmos_language_id=cosmos_lang_id,
                    slug_title_source=slug_title_source,
                    standalone=is_standalone,
                )
                if slug and slug not in slugs:
                    slugs.append(slug)

        return slugs

    def _migrate_drug_resources(self, module_doc: Dict, module_icon_asset_id: Optional[str] = None) -> List[str]:
        """Queue drug resources referenced by module.
        
        CORRECTED LOGIC:
        1. Slug: From Global Drug TITLE (not description)
        2. Markdown: From drug document's cards[].translated blocks (via DataFactory)
        3. Screens Table: "translated" field → DESCRIPTION only (title stays original)
        4. Language ID: Always passed through if available
        5. Icon: Inherited from parent module (drugs don't have their own icons)
        """
        drug_keys = module_doc.get("drugs", []) or []
        cosmos_lang_id = module_doc.get("language_id") or module_doc.get("langId") or ""
        lme_lang_id, region = self._map_language_info(cosmos_lang_id)

        slugs: List[str] = []

        if drug_keys:
            print(f"  → Migrating {len(drug_keys)} drugs for module")

        for key in drug_keys:
            if not key:
                continue
                
            # ──────────────────────────────────────────────────────────
            # LOCALIZED ENFORCEMENT DOCTRINE
            # ──────────────────────────────────────────────────────────
            if cosmos_lang_id:
                content_doc = self._fetch_table_doc("drugs", key, cosmos_lang_id)
                if not content_doc:
                    content_doc = self._fetch_table_doc("Drugs", key, cosmos_lang_id)
                
                if not content_doc:
                    print(f"  ⚠ SKIP: Localized drug '{key}' not found in {cosmos_lang_id}.")
                    continue
                    
                global_doc = self._fetch_table_doc("drugs", key, "")
                if not global_doc:
                    global_doc = self._fetch_table_doc("Drugs", key, "")
                    
                if not global_doc:
                    print(f"  ⚠ STRUCTURAL VIOLATION: Global parent missing for localized drug '{key}'. Skipping.")
                    continue
                    
                identity_doc = global_doc
                is_standalone = False
            else:
                global_doc = self._fetch_table_doc("drugs", key, "")
                if not global_doc:
                    global_doc = self._fetch_table_doc("Drugs", key, "")
                
                if not global_doc:
                    print(f"  ⚠ SKIP: Global drug '{key}' not found in Cosmos.")
                    continue
                    
                identity_doc = global_doc
                content_doc = global_doc
                is_standalone = False

            # 3. Generate Markdown from content_doc's cards[].translated structure
            # DataFactory.create_action_card_resources handles this correctly
            resources = DataFactory.create_action_card_resources(
                content_doc,
                global_doc=identity_doc,
                language_id=cosmos_lang_id, 
                resource_type="drug",
                module_icon_asset_id=module_icon_asset_id,  # PASS MODULE ICON
                screens_container=self.cosmos_client.get_database_client(DATABASE_NAME).get_container_client("screens")
            )
            
            valid_resources = self._select_valid_resource_versions(resources, cosmos_lang_id)
            
            if not valid_resources:
                print(f"Warning: Failed to create resource for drug '{key}'")
                continue

            # 4. SLUG SOURCE - From Global TITLE (CRITICAL FIX: not description)
            from text_utils import clean_and_resolve_title
            
            # Try title first, fallback to description if title missing
            global_title = identity_doc.get("title") or identity_doc.get("description") or key
            slug_title_source = clean_and_resolve_title(global_title, key)
            original_title = clean_and_resolve_title(global_title, key)
            
            # 5. SCREENS TABLE - For DESCRIPTION only (NOT title)
            if cosmos_lang_id:
                trans_key = f"drug:{key}"
                translated_desc = self._get_screen_translation(trans_key, cosmos_lang_id)
            else:
                translated_desc = None
                
            for res in valid_resources:
                if cosmos_lang_id:
                    if translated_desc:
                        if res == valid_resources[-1]:
                            print(f"  ✓ Screens translation for drug description: '{translated_desc[:50]}...'")
                        # CRITICAL: Only update DESCRIPTION, keep title as original
                        res.description = translated_desc
                        res.title = original_title
                    else:
                        # No screens translation - use original for both
                        if res == valid_resources[-1]:
                            print(f"  ℹ️  No screens translation found for 'drug:{key}', using original")
                        res.title = original_title
                        res.description = original_title
                else:
                    # Global module - use original for both
                    res.title = original_title
                    res.description = original_title
                
                # 6. Ensure LME metadata is set correctly
                res.language_id = lme_lang_id or ""
                res.region = region
                if not cosmos_lang_id:
                    res.content_type = "original"
                
                # 7. Set module icon for drug (drugs inherit parent module's icon)
                if module_icon_asset_id:
                    res.icon = module_icon_asset_id

                # 8. Post the resource — pass standalone flag
                slug = self._create_or_update_complex_resource(
                    res,
                    key,
                    "drug", 
                    cosmos_language_id=cosmos_lang_id,
                    slug_title_source=slug_title_source,
                    standalone=is_standalone,
                )
                
                if slug and slug not in slugs:
                    slugs.append(slug)

        return slugs

    def _resolve_link_ref(self, link: str, default_region: str = "india") -> str:
        """Resolve a resource link reference (video:/..., drug:..., procedure:..., or res-slug) to a UUID."""
        if not link or not isinstance(link, str):
            return link

        resolved_id = None

        if link.startswith("res-"):
            if link in self.resource_slug_mapping:
                return self.resource_slug_mapping[link]
            return None

        # Handle video:/... (Path based)
        if link.startswith("video:/"):
            path = link[len("video:/"):]
            
            # Path format: "{lang_or_module}/{...}/{video_name}"
            # e.g., "french/Post partum hemorrage/intro" or "Post partum hemorrage/intro"
            # Video slugs (created by _migrate_video_resources) are:
            #   res-video-{lang_name}-{global_module_title}-{video_name}
            # The module segment in the CMS path may have typos vs the Cosmos doc title,
            # so we must NOT rely on the module segment for exact matching.
            
            parts = [p for p in path.split("/") if p.strip()]
            if not parts:
                return None
            
            # Determine lang_slug and filename_slug from path segments
            filename_slug = slugify(parts[-1]) if parts else ""
            
            # The first segment might be a language name (e.g., "french") or a module name
            first_slug = slugify(parts[0]) if parts else ""
            
            # Middle segments (between first and last) — used for fuzzy scoring
            middle_slugs = [slugify(p) for p in parts[1:-1] if slugify(p)] if len(parts) > 2 else []
            
            # Build the old-style full_path_slug for backward-compatible exact match
            full_path_slug = "-".join(slugify(p) for p in parts if slugify(p))
            
            # ── Strategy 1: Exact match (backward compat) ──
            # Try res-video-{full_path_slug} — works when CMS path spelling == Cosmos doc title
            exact_candidate = f"res-video-{full_path_slug}"
            if exact_candidate in self.resource_slug_mapping:
                return self.resource_slug_mapping[exact_candidate]
            
            # ── Strategy 2: Anchor both ends (handles typos in module name) ──
            # Match: startswith("res-video-{lang_slug}-") AND endswith("-{filename_slug}")
            # This skips the module name portion entirely, resolving spelling mismatches.
            prefix = f"res-video-{first_slug}-"
            suffix = f"-{filename_slug}"
            anchored = [s for s in self.resource_slug_mapping
                        if s.startswith(prefix) and s.endswith(suffix)]
            
            if len(anchored) == 1:
                return self.resource_slug_mapping[anchored[0]]
            elif len(anchored) > 1:
                # Multiple matches (e.g., two modules both have "intro" video for same lang).
                # Score by how many middle path segments appear in the slug body.
                def score(slug_candidate):
                    body = slug_candidate[len(prefix):-len(suffix)] if len(suffix) > 1 else slug_candidate[len(prefix):]
                    tokens = [t for seg in middle_slugs for t in seg.split("-") if t]
                    return sum(1 for t in tokens if t and t in body)
                
                anchored.sort(key=score, reverse=True)
                top_score = score(anchored[0])
                second_score = score(anchored[1]) if len(anchored) > 1 else -1
                if top_score > 0 and top_score > second_score:
                    return self.resource_slug_mapping[anchored[0]]
                # If tied, try the old suffix match with full_path_slug as tiebreaker
                full_suffix = f"-{full_path_slug}"
                for s in anchored:
                    if s.endswith(full_suffix):
                        return self.resource_slug_mapping[s]
                # Give up gracefully — take the first
                return self.resource_slug_mapping[anchored[0]]
            
            # ── Strategy 3: Filename-only suffix fallback ──
            # Broader search across all languages — used when first segment wasn't a lang name
            filename_suffix = f"-{filename_slug}"
            filename_matches = [s for s in self.resource_slug_mapping 
                              if s.endswith(filename_suffix) and s.startswith("res-video-")]
            
            if filename_matches:
                # Narrow by region if available
                if default_region:
                    regional = [s for s in filename_matches if f"-{default_region}-" in s]
                    if len(regional) == 1:
                        return self.resource_slug_mapping[regional[0]]
                    if regional:
                        filename_matches = regional
                
                if len(filename_matches) == 1:
                    return self.resource_slug_mapping[filename_matches[0]]
                
                # Score by middle segments as a last resort
                def score_fallback(slug_candidate):
                    all_segs = [slugify(p) for p in parts if slugify(p)]
                    tokens = [t for seg in all_segs for t in seg.split("-") if t]
                    return sum(1 for t in tokens if t and t in slug_candidate)
                
                filename_matches.sort(key=score_fallback, reverse=True)
                if score_fallback(filename_matches[0]) > 0:
                    return self.resource_slug_mapping[filename_matches[0]]
            
            print(f"Warning: Could not resolve video link '{link}'. "
                  f"Path segments: {parts}, tried prefix='{prefix}', suffix='{suffix}'")
            return None

        # Handle specific types: drug, procedure, action-card
        # Pattern: type:identifier (identifier might have timestamp suffix like _123456789)
        match = re.match(r"^(drug|procedure|action-card):(.+)$", link)
        if match:
            res_type = match.group(1)
            raw_id = match.group(2)
            
            # Strip timestamp suffix (e.g., _1757685337059 or -1757685337059)
            # We assume suffixes of 10+ digits are timestamps
            base_id = re.sub(r"[_-]\d{10,}$", "", raw_id)
            
            slug_tail = slugify(base_id)
            
            # Construct candidates
            candidates = []
            target_slug_type = res_type
            
            # Formats: res-{type}-{slug}
            base_slug = f"res-{target_slug_type}-{slug_tail}"
            
            if default_region:
                candidates.append(f"res-{target_slug_type}-{default_region}-{slug_tail}")
            
            candidates.append(base_slug)
            
            for slug in candidates:
                if slug in self.resource_slug_mapping:
                    return self.resource_slug_mapping[slug]
            
            # ── Suffix-based fallback ──
            # Slugs in the mapping may include a module name prefix that the link doesn't have.
            # E.g., link = "action-card:identify-cause_..." → slug_tail = "identify-cause"
            # but mapping has "res-action-card-maternal-sepsis-identify-cause"
            suffix = f"-{slug_tail}"
            type_prefix = f"res-{target_slug_type}-"
            suffix_matches = [s for s in self.resource_slug_mapping
                              if s.startswith(type_prefix) and s.endswith(suffix)]
            
            if len(suffix_matches) == 1:
                print(f"  ✓ Resolved {res_type} link '{link}' via suffix match → {suffix_matches[0]}")
                return self.resource_slug_mapping[suffix_matches[0]]
            elif len(suffix_matches) > 1:
                # Multiple matches — try narrowing by region
                if default_region:
                    regional = [s for s in suffix_matches if f"-{default_region}-" in s]
                    if len(regional) == 1:
                        print(f"  ✓ Resolved {res_type} link '{link}' via regional suffix match → {regional[0]}")
                        return self.resource_slug_mapping[regional[0]]
                    if regional:
                        suffix_matches = regional
                
                # Still multiple — use first match as best effort
                print(f"  ⚠ Ambiguous {res_type} link '{link}', {len(suffix_matches)} suffix matches: {suffix_matches}. Using first.")
                return self.resource_slug_mapping[suffix_matches[0]]
            
            # ── Substring containment fallback ──
            # Handles cases like "counsellingind" vs "counselling-ind" where
            # hyphenation differs but the characters are the same.
            slug_tail_nohyphens = slug_tail.replace("-", "")
            contains_matches = [s for s in self.resource_slug_mapping 
                                if s.startswith(type_prefix) and 
                                slug_tail_nohyphens in s.replace("-", "")]
            if len(contains_matches) == 1:
                print(f"  ✓ Resolved {res_type} link '{link}' via substring match → {contains_matches[0]}")
                return self.resource_slug_mapping[contains_matches[0]]
            elif len(contains_matches) > 1:
                if default_region:
                    regional = [s for s in contains_matches if f"-{default_region}-" in s]
                    if len(regional) == 1:
                        print(f"  ✓ Resolved {res_type} link '{link}' via regional substring match → {regional[0]}")
                        return self.resource_slug_mapping[regional[0]]
                # Use first match
                print(f"  ⚠ Ambiguous {res_type} link '{link}', {len(contains_matches)} substring matches. Using first: {contains_matches[0]}")
                return self.resource_slug_mapping[contains_matches[0]]
            
            # ── Token-overlap fallback ──
            # For edge cases where the key name differs (e.g., "procedures-by-also" vs 
            # "procedure-as-by-also"). Score by token overlap.
            slug_tokens = set(slug_tail.split("-"))
            if len(slug_tokens) >= 2:
                type_candidates = [s for s in self.resource_slug_mapping 
                                   if s.startswith(type_prefix)]
                
                def token_score(candidate):
                    candidate_tokens = set(candidate[len(type_prefix):].split("-"))
                    return len(slug_tokens & candidate_tokens)
                
                scored = [(s, token_score(s)) for s in type_candidates]
                scored.sort(key=lambda x: x[1], reverse=True)
                
                # Only accept if the top match has high overlap (>= 60% of slug tokens)
                min_score = max(2, int(len(slug_tokens) * 0.6))
                if scored and scored[0][1] >= min_score:
                    best = scored[0][0]
                    print(f"  ✓ Resolved {res_type} link '{link}' via token overlap ({scored[0][1]}/{len(slug_tokens)} tokens) → {best}")
                    return self.resource_slug_mapping[best]
            
            print(f"Warning: Could not resolve {res_type} link '{link}'. Candidates: {candidates}")
            return None
        
        # Unrecognized link format - return None to strip from payload
        print(f"Warning: Unrecognized link format '{link}', skipping")
        return None

    def _convert_link_to_slug(self, link: str) -> Optional[str]:
        """Convert a path-style link to its corresponding resource slug.
        
        Used during KLP queue step to store slugs instead of paths.
        This enables direct lookup during POST without complex parsing.
        
        Note: Slugs are region/language independent to ensure different
        regions create versions of the same resource, not new resources.
        
        Args:
            link: Path-style link like 'video:/Hypertension/definitions' or 'drug:labetalol_123'
            
        Returns:
            Resource slug like 'res-video-hypertension-definitions' or None
        """
        if not link or not isinstance(link, str):
            return None
        
        # Handle video:/path links
        if link.startswith("video:/"):
            path = link[len("video:/"):]
            title = self._extract_title_from_video_path(path)
            filename = path.split("/")[-1]
            filename_slug = slugify(filename)
            title_slug = slugify(title)
            slug_tail = merge_slug_parts(title_slug, filename_slug)
            return build_slug("res", "video", slug_tail)
        
        # Handle drug:id, procedure:id, action-card:id links
        match = re.match(r"^(drug|procedure|action-card):(.+)$", link)
        if match:
            tag = match.group(1)
            raw_id = match.group(2)
            # Strip timestamp suffix (e.g., _1757685337059)
            base_id = re.sub(r"[_-]\d{10,}$", "", raw_id)
            slug_tail = slugify(base_id)
            return build_slug("res", tag, slug_tail)
        
        # Unrecognized format
        return None

    def _migrate_key_learning_point_resources(self, module_doc: Dict) -> List[str]:
        """Queue key learning point resources referenced by module.
        
        Matches legacy behavior but enforces GLOBAL IDENTITY:
        1. Fetches Global Doc (langId="") for Slug/Title.
        2. Extracted KLP data (Questions) comes from Local Doc.
        3. Generates slug from Global Title.
        """
        klp_keys = (
            module_doc.get("keyLearningPoints", [])
            or module_doc.get("key_learning_points", [])
            or []
        )
        cosmos_lang_id = module_doc.get("language_id") or module_doc.get("langId") or ""
        lme_lang_id, region = self._map_language_info(cosmos_lang_id)
        slugs: List[str] = []
        
        for key in klp_keys:
            if not key:
                continue
            
            # ──────────────────────────────────────────────────────────
            # LOCALIZED ENFORCEMENT DOCTRINE
            # ──────────────────────────────────────────────────────────
            if cosmos_lang_id:
                klp_doc = self._fetch_table_doc("key-learning-points", key, cosmos_lang_id)
                if not klp_doc:
                    klp_doc = self._fetch_table_doc("keyLearningPoints", key, cosmos_lang_id)
                
                if not klp_doc:
                    print(f"  ⚠ SKIP: Localized KLP '{key}' not found in {cosmos_lang_id}.")
                    continue
                    
                global_doc = self._fetch_table_doc("key-learning-points", key, "")
                if not global_doc:
                    global_doc = self._fetch_table_doc("keyLearningPoints", key, "")
                    
                if not global_doc:
                    print(f"  ⚠ STRUCTURAL VIOLATION: Global parent missing for localized KLP '{key}'. Skipping.")
                    continue
                    
                identity_doc = global_doc
            else:
                global_doc = self._fetch_table_doc("key-learning-points", key, "")
                if not global_doc:
                    global_doc = self._fetch_table_doc("keyLearningPoints", key, "")
                    
                if not global_doc:
                    print(f"  ⚠ SKIP: Global KLP '{key}' not found in Cosmos.")
                    continue
                    
                identity_doc = global_doc
                klp_doc = global_doc
            
            # 3. Extract Data & Predict Slug from IDENTITY (Global)
            # Legacy logic uses level in slug: backend_build_slug(level, title)
            level = str(identity_doc.get("level", "1"))
            title = identity_doc.get("title") or identity_doc.get("description") or "Untitled"
            
            # Clean title for slug using centralized logic
            cleaned_title = clean_and_resolve_title(title, key)
            
            title_slug = slugify(cleaned_title)
            # Format: res-key-learning-point-{level}-{title}
            # Note: Explicitly use "klp" or "key-learning-point"?
            # Existing code used "klp" in build_slug call: slug = build_slug("klp", level, title_slug)
            slug = build_slug("klp", level, title_slug)
            
            # 4. Create Resource Data from GLOBAL doc & LOCAL doc
            
            if cosmos_lang_id:
                version_types = ["translated", "adapted"]
                created_versions = []
                for v_type in version_types:
                    resource_data = DataFactory.create_resource_data(
                        klp_doc, 
                        "key-learning-points", 
                        language_id=cosmos_lang_id,
                        global_doc=identity_doc,
                        force_version_type=v_type
                    )
                    resource_data.language_id = lme_lang_id or ""
                    resource_data.region = region
                    resource_data.level = level
                    
                    if cosmos_lang_id:
                        trans_key = f"key-learning-point:{key}"
                        translated_desc = self._get_screen_translation(trans_key, cosmos_lang_id)
                        if translated_desc:
                            resource_data.description = translated_desc

                    questions = getattr(resource_data, 'questions', []) or []
                    # Check if there are actually any questions for this version
                    has_valid_questions = any(q.get("question") or q.get("answers") for q in questions)
                    
                    if has_valid_questions:
                        for q in questions:
                            if "link" in q:
                                if not q["link"] or not isinstance(q["link"], str):
                                    del q["link"]
                                    q["link_type"] = None
                        
                        self._queue_klp_post(
                            slug=slug,
                            title=resource_data.title,
                            description=resource_data.description or "",
                            level=level,
                            content_type=v_type, # explicitly set
                            language_id=lme_lang_id or "",
                            region=region,
                            created_by=resource_data.created_by or "System",
                            cosmos_language_id=cosmos_lang_id,
                            questions=questions,
                            derived_from_id=getattr(resource_data, "derived_from_id", None),
                        )
                        created_versions.append(v_type)
                
                if created_versions:
                    slugs.append(slug)
                else:
                    print(f"  ⚠ SKIP: No valid questions found in {cosmos_lang_id} for KLP '{key}'.")
            else:
                resource_data = DataFactory.create_resource_data(
                    klp_doc, 
                    "key-learning-points", 
                    language_id="",
                    global_doc=identity_doc
                )
                
                resource_data.language_id = lme_lang_id or ""
                resource_data.region = region
                resource_data.level = level
                
                questions = getattr(resource_data, 'questions', []) or []
                for q in questions:
                    if "link" in q:
                        if not q["link"] or not isinstance(q["link"], str):
                            del q["link"]
                            q["link_type"] = None

                self._queue_klp_post(
                    slug=slug,
                    title=resource_data.title,
                    description=resource_data.description or "",
                    level=level,
                    content_type="original",
                    language_id=lme_lang_id or "",
                    region=region,
                    created_by=resource_data.created_by or "System",
                    cosmos_language_id=cosmos_lang_id,
                    questions=questions,
                    derived_from_id=getattr(resource_data, "derived_from_id", None),
                )
                slugs.append(slug)

        return slugs

    def _fetch_table_doc(self, table: str, key: str, lang_id: str) -> Optional[Dict]:
        """Fetch a resource-like document from Cosmos by table and key/id, filtering by langId.
        
        When lang_id is empty, explicitly fetches the global/master document 
        (where langId is empty, undefined, or 'global').
        When lang_id is set, fetches the document for that specific language.
        """
        try:
            if lang_id:
                lang_clause = f" AND c.langId='{lang_id}'"
            else:
                # Explicitly fetch global/master document only
                lang_clause = " AND (NOT IS_DEFINED(c.langId) OR c.langId = '')"
            
            # Attempt by id first
            query = (
                f"SELECT TOP 1 * FROM c WHERE c._table='{table}' AND c.id='{key}'{lang_clause} ORDER BY c._ts DESC"
            )
            results = list(
                self.container.query_items(query=query, enable_cross_partition_query=True)
            )
            if results:
                return results[0]
            
            # Fallback by key field if exists
            query2 = (
                f"SELECT TOP 1 * FROM c WHERE c._table='{table}' AND c.key='{key}'{lang_clause} ORDER BY c._ts DESC"
            )
            results2 = list(
                self.container.query_items(query=query2, enable_cross_partition_query=True)
            )
            if results2:
                return results2[0]
            
            # NO GLOBAL FALLBACK: If a language-specific document is not found,
            # return None. The caller decides whether to skip or handle missing data.
            # Previously, this would silently fall back to Global (langId=""), causing
            # language manifests to include resources that don't belong to that language
            # with wrong-language content in 'translated' fields.
            if lang_id:
                print(f"  ℹ️  No {table} document found for key '{key}' with langId='{lang_id}'.")

            return None
        except Exception as exc:
            from error_logger import log_error
            log_error("Captured Exception", exc=exc)
            print(f"Error fetching {table} '{key}': {exc}")
            return None

    def _get_screen_translation(self, base_key: str, lang_id: str) -> Optional[str]:
        """Fetch translated description from screens table using fuzzy match.
        
        Args:
            base_key: Base key like 'drug:betamethasone'
            lang_id: Language ID to query for
            
        Returns:
            Translated content/description or None
        """
        if not base_key or not lang_id:
            return None
            
        try:
            # Use STARTSWITH to handle timestamp suffixes (e.g. drug:beta_123456)
            query = (
                f"SELECT TOP 1 * FROM c WHERE c._table='screens' "
                f"AND c.langId='{lang_id}' "
                f"AND STARTSWITH(c.key, '{base_key}') "
                f"ORDER BY c._ts DESC"
            )
            
            results = list(
                self.container.query_items(query=query, enable_cross_partition_query=True)
            )
            
            if results:
                # If multiple matches (rare for exact prefix), pick most recent
                doc = sorted(results, key=lambda x: x.get("_ts", 0), reverse=True)[0]
                
                # Priority: translated -> adapted -> content
                # Note: 'translated' often holds the title/short desc in screens table
                val = doc.get("translated") or doc.get("adapted") or doc.get("content")
                return val
                
            return None
            
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Error fetching screen translation for {base_key}: {e}")
            return None

    def _get_resource_by_link(self, link: str) -> Optional[Dict]:
        """Fetch the original global resource document from a link string.
        
        This serves as a fallback when a localized version isn't found.
        Parses links like 'drug:betamethasone' or 'res-drug-...' to query the correct table.
        """
        if not link:
            return None
            
        try:
            # Parse link to determine table and key
            table_name = None
            key = None
            
            # Case 1: Standard id links (drug:abc)
            match = re.match(r"^(drug|procedure|action-card):(.+)$", link)
            if match:
                tag = match.group(1)
                key = match.group(2)
                type_map = {
                    "drug": "drugs",
                    "procedure": "procedures", 
                    "action-card": "action-cards"
                }
                table_name = type_map.get(tag)
                
            # Case 2: Slug links (res-drug-...) - harder to map, usually requires search
            # But the caller usually passes raw links.
            
            if table_name and key:
                # Query for GLOBAL doc (langId = '')
                # Note: 'action-cards' table in Cosmos is sometimes 'actionCards' or 'action-cards'
                # We use _fetch_table_doc helper if available, or raw query
                
                # Try explicit query
                query = (
                    f"SELECT TOP 1 * FROM c WHERE c._table='{table_name}' "
                    f"AND c.key='{key}' "
                    f"AND c.langId='' "  # Global only
                    f"ORDER BY c._ts DESC"
                )
                results = list(self.container.query_items(query=query, enable_cross_partition_query=True))
                if results:
                     return results[0]
                     
                # Fallback for action-cards table name variance
                if table_name == "action-cards":
                     query = query.replace("'action-cards'", "'actionCards'")
                     results = list(self.container.query_items(query=query, enable_cross_partition_query=True))
                     if results:
                         return results[0]

            return None
            
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Error in _get_resource_by_link for {link}: {e}")
            return None

    def _get_localized_resource_doc(self, table: str, key: str, lang_id: str) -> Optional[Dict]:
        """Fetch the localized version of a resource document (e.g. drugs table with langId)."""
        if not table or not key or not lang_id:
            return None
            
        try:
            query = (
                f"SELECT TOP 1 * FROM c WHERE c._table='{table}' "
                f"AND c.key='{key}' "
                f"AND c.langId='{lang_id}' "
                f"ORDER BY c._ts DESC"
            )
            results = list(
                self.container.query_items(query=query, enable_cross_partition_query=True)
            )
            if results:
                print(f"  ✓ Found localized document for {key} (lang: {lang_id})")
                return results[0]
            return None
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Error fetching localized doc for {key}: {e}")
            return None

    def _migrate_action_card_resources(self, module_doc: Dict) -> List[str]:
        """Migrate action card resources - screens for description only."""
        module_language_id = module_doc.get("language_id") or module_doc.get("langId") or ""
        is_global_module = not module_language_id
        action_card_keys = module_doc.get("actionCards", [])
        migrated_action_card_ids = []

        if action_card_keys:
            print(f"  → Migrating {len(action_card_keys)} action cards for module")

        for action_card_key in action_card_keys:
            if not action_card_key:
                continue
                
            # ──────────────────────────────────────────────────────────
            # LOCALIZED ENFORCEMENT DOCTRINE
            # ──────────────────────────────────────────────────────────
            if module_language_id:
                # Use DataFactory.get_action_card_data which exists in factories.py
                action_card_doc = DataFactory.get_action_card_data(
                    self.cosmos_client, 
                    self.container, 
                    action_card_key, 
                    module_language_id
                )
                if not action_card_doc:
                    print(f"  ⚠ SKIP: Localized action card '{action_card_key}' not found in {module_language_id}.")
                    continue
                    
                global_doc = self._fetch_table_doc("action-cards", action_card_key, "")
                if not global_doc:
                    global_doc = self._fetch_table_doc("actionCards", action_card_key, "")
                    
                if not global_doc:
                    print(f"  ⚠ STRUCTURAL VIOLATION: Global parent missing for localized action card '{action_card_key}'. Skipping.")
                    continue
                    
                identity_doc = global_doc
                is_standalone = False
            else:
                global_doc = self._fetch_table_doc("action-cards", action_card_key, "")
                if not global_doc:
                    global_doc = self._fetch_table_doc("actionCards", action_card_key, "")
                    
                if not global_doc:
                    print(f"  ⚠ SKIP: Global action card '{action_card_key}' not found in Cosmos.")
                    continue
                    
                identity_doc = global_doc
                action_card_doc = global_doc
                is_standalone = False

            if action_card_doc:
                cosmos_lang_id = action_card_doc.get("langId", "")
                
                # Map language using the mapping helper
                lme_lang_id, region = self._map_language_info(cosmos_lang_id)

                # 3. Generate markdown from cards using DataFactory
                action_card_resources = DataFactory.create_action_card_resources(
                    action_card_doc,
                    global_doc=identity_doc,
                    language_id=cosmos_lang_id,
                    allowed_versions=(["original"] if is_global_module else None),
                    resource_type="action-card",
                    screens_container=self.cosmos_client.get_database_client(DATABASE_NAME).get_container_client("screens")
                )

                valid_resources = self._select_valid_resource_versions(
                    action_card_resources, 
                    cosmos_lang_id
                )
                
                if not valid_resources:
                    print(f"Warning: No resource created for action-card '{action_card_key}'")
                    continue

                # 4. Original title from global (TITLE field, not description)
                from text_utils import clean_and_resolve_title
                
                # CRITICAL FIX: Use title field first, fallback to description
                global_title = (
                    identity_doc.get("title") or 
                    identity_doc.get("description") or 
                    action_card_key
                )
                slug_title_source = clean_and_resolve_title(global_title, action_card_key)
                original_title = clean_and_resolve_title(global_title, action_card_key)

                # 5. SCREENS TABLE - For DESCRIPTION only (NOT title)
                if cosmos_lang_id:
                    trans_key = f"action-card:{action_card_key}"
                    translated_desc = self._get_screen_translation(trans_key, cosmos_lang_id)
                else:
                    translated_desc = None

                for res in valid_resources:
                    if cosmos_lang_id:
                        if translated_desc:
                            if res == valid_resources[-1]:
                                print(f"  ✓ Screens translation for action-card description: '{translated_desc[:50]}...'")
                            # CRITICAL: Update description only, keep title original
                            res.description = translated_desc
                            res.title = original_title
                        else:
                            # No screens translation - use original for both
                            if res == valid_resources[-1]:
                                print(f"  ℹ️  No screens translation found for '{trans_key}', using original")
                            res.title = original_title
                            res.description = original_title
                    else:
                        # Global module - use original for both
                        res.title = original_title
                        res.description = original_title

                    # 6. Set LME metadata (ensure language_id is set)
                    res.language_id = lme_lang_id or ""
                    res.region = region
                    if not cosmos_lang_id:
                        res.content_type = "original"

                    # 7. Create/update resource — pass standalone flag
                    resource_id = self._create_or_update_complex_resource(
                        res, 
                        action_card_key, 
                        "action-card", 
                        cosmos_language_id=cosmos_lang_id,
                        slug_title_source=slug_title_source,  # Global title for slug
                        standalone=is_standalone,
                    )
                    
                    if resource_id and resource_id not in migrated_action_card_ids:
                        migrated_action_card_ids.append(resource_id)
            else:
                print(f"Warning: Could not fetch action card with key '{action_card_key}'")

        return migrated_action_card_ids

    def _select_valid_resource_versions(
        self, resources: List[ResourcePostRequestData], cosmos_lang_id: str
    ) -> List[ResourcePostRequestData]:
        """Select all valid versions to migrate for a resource.
        
        For global modules: returns only 'original'.
        For localized modules: returns 'translated' and 'adapted' (discarding 'original' fallback).
        """
        if not resources:
            return []
            
        # If global module, just take original
        if not cosmos_lang_id:
            for r in resources:
                if r.content_type == "original":
                    return [r]
            # Fallback to first if explicit original missing
            return [resources[0]]
            
        # Localized module: we want both translated and adapted if they exist
        valid_versions = []
        for r in resources:
            if r.content_type in ("translated", "adapted"):
                valid_versions.append(r)
                
        # If we somehow found neither, return what we have (fallback)
        if not valid_versions:
            return [resources[0]]
            
        return valid_versions

    def _get_or_create_video_asset(
        self, video_path: str, language_id: str, asset_name_prefix: Optional[str] = None, use_prefix: bool = True
    ) -> Optional[str]:
        """Get existing video asset or create new one."""
        return DataFactory._download_and_upload_video(video_path, language_id, asset_name_prefix, use_prefix)

    def _get_or_create_icon_asset(
        self, video_path: str, language_id: str, asset_type: str = "video_icon", asset_name_prefix: Optional[str] = None, use_prefix: bool = True
    ) -> Optional[str]:
        """Get existing video icon asset or create new one."""
        return DataFactory._download_and_upload_asset(
            video_path, language_id, asset_type, asset_name_prefix, use_prefix
        )

    def _extract_display_title(self, video_path: str) -> str:
        """Extract display title from filename only (User Request)."""
        filename = video_path.split("/")[-1]
        # Remove extension
        if "." in filename:
            filename = filename.rsplit(".", 1)[0]
        # Replace underscores and title case
        return filename.replace("_", " ").title()

    def _extract_title_from_video_path(self, video_path: str, context_title: str = None) -> str:
        """Extract title from video path, remove region info and
        underscores.
        
        Args:
            video_path: Path to video file
            context_title: Optional module title to strip from result if present
        """
        # Remove leading slash if present
        path = video_path.lstrip("/")

        # Split by '/' and find the meaningful part
        parts = path.split("/")

        # Look for patterns like "/english WHO/Post partum hemorrage/intro"
        # Keep only "Post partum hemorrage/intro"
        meaningful_parts = parts[1:] if len(parts) > 1 else parts

        title = " - ".join(meaningful_parts)
        title = title.replace("_", " ")



        # Context-aware cleanup: remove module title if it appears at the start
        if context_title:
             # Normalize for case-insensitive comparison
             clean_context = context_title.strip()
             if title.lower().startswith(clean_context.lower()):
                 # Strip the context and any leading separators
                 title = title[len(clean_context):].strip(" -")
                 
                 # If title became empty (e.g. video was named exactly same as module), revert?
                 # Or keep it empty and let fallback handle it? 
                 # Usually there's a filename part left like "intro".
                 if not title:
                     # Fallback: just use the last part of meaningful parts if available
                     if meaningful_parts:
                         title = meaningful_parts[-1].replace("_", " ")

        return title

    def _create_or_update_resource(
        self,
        resource_data: ResourcePostRequestData,
        tag: str,
        video_path: str = "",
        *,
        cosmos_language_id: str = "",
        slug_title_source: str = None,
        standalone: bool = False,
    ) -> Optional[str]:
        """Queue a resource payload and return the computed slug.
        
        Args:
            standalone: If True, this resource has no global original.
                        Passed through to CSV so Stage 2 allows direct POST.
        """
        # Generate slug from title and optional video filename via
        # strict slugify.
        # Use slug_title_source if provided (for uniqueness), else display title
        source_title = slug_title_source if slug_title_source else resource_data.title
        
        # Skip version words in the slug.
        cleaned_title = re.sub(
            r"\s*\((?:adapted|translated|original)\)\s*",
            " ",
            source_title,
            flags=re.IGNORECASE,
        ).strip()
        title_slug = slugify(cleaned_title)
        slug_tail = title_slug
        if video_path:
            video_filename = video_path.split("/")[-1]
            video_slug = slugify(video_filename)
            slug_tail = merge_slug_parts(title_slug, video_slug)
        slug = build_slug("res", tag, slug_tail)
        # Translate cosmos language id (incoming) to LME id if mapping known.
        incoming_lang = resource_data.language_id
        if incoming_lang and incoming_lang in self.language_mapping:
            entry = self.language_mapping[incoming_lang]
            resource_data.language_id = entry.get("lme_language_id", "") or ""
            resource_data.region = entry.get("region", resource_data.region or "africa")
        elif incoming_lang and incoming_lang not in self.language_mapping:
            # Stage 1: mapping not yet present; blank out to avoid wrong cosmos id in CSV.
            resource_data.language_id = ""
        resource_data.content_type = self._resolve_content_type(cosmos_language_id)
        self._queue_resource_post(tag, slug, resource_data, cosmos_language_id, standalone=standalone)
        print(
            "Queued %s resource payload for slug '%s' -> %s"
            % (tag, slug, self._resource_queue_path)
        )
        return slug

    def _create_or_update_complex_resource(
        self,
        resource_data: ResourcePostRequestData,
        key: str,
        tag: str,
        cosmos_language_id: str = "",
        slug_title_source: str = None,
        standalone: bool = False,
    ) -> Optional[str]:
        """Queue a complex resource payload (action-card, drug, procedure) with slug tracking.
        
        Args:
            standalone: If True, this resource exists only in a localized module
                        and has no global original. Passed through to CSV.
        """

        # Generate slug from title (or explicit source)
        # For resource slug, do not include version words
        source_title = slug_title_source if slug_title_source else resource_data.title
        cleaned_title = clean_and_resolve_title(source_title, key)
        title_slug = slugify(cleaned_title)
        slug = build_slug("res", tag, title_slug)

        # Track cosmos_key -> slug for module resource population
        if key:
            self.cosmos_key_to_slug[key] = slug

        if standalone:
            print(f"Queuing STANDALONE {tag}: {slug} (Source: '{source_title}') — no global original")
        else:
            print(f"Queuing {tag}: {slug} (Source: '{source_title}')")
        self._queue_resource_post(tag, slug, resource_data, cosmos_language_id, standalone=standalone)
        return slug

    def _load_resource_slug_mapping(self) -> None:
        """Load existing resource slug to resource ID mapping from CSV."""
        mapping_path = self._resource_mapping_path

        if not mapping_path.exists():
            print("No existing resource slug mapping found, starting fresh")
            return

        try:
            with mapping_path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self.resource_slug_mapping[row["slug"]] = row["resource_id"]
            print(
                f"Loaded {len(self.resource_slug_mapping)} existing "
                f"resource mappings"
            )
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Could not load resource slug mapping: {e}")

    def _save_resource_slug_mapping(self) -> None:
        """Save the resource slug to resource ID mapping to CSV."""
        # Deprecated: avoid overwriting mapping file
        pass

    def _append_resource_slug_mapping(
        self,
        slug: str,
        resource_id: str,
    ) -> None:
        """Append a single slug->resource_id mapping to CSV (no overwrite)."""
        mapping_path = self._resource_mapping_path
        ensure_parent_dir(mapping_path)
        need_header = not mapping_path.exists() or mapping_path.stat().st_size == 0
        with mapping_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if need_header:
                writer.writerow(["slug", "resource_id"])
            writer.writerow([slug, resource_id])

    def get_resource_mapping(self) -> Dict[str, str]:
        """Get the current resource slug mapping."""
        return self.resource_slug_mapping

    def _queue_resource_post(
        self,
        tag: str,
        slug: str,
        resource_data: ResourcePostRequestData,
        cosmos_language_id: str = "",
        standalone: bool = False,
    ) -> None:
        """Append a resource POST payload to processed_data CSV.
        
        Args:
            standalone: If True, this resource exists only in a localized module
                        and has no global original. Stage 2 will allow it to POST
                        directly instead of being blocked by the translated-skip rule.
        """
        if slug in self.queued_slugs:
            if not cosmos_language_id:
                print(f"Skipping duplicate global queue for '{slug}' (already queued with language-specific version)")
                return
            print(f"Overwriting previous queue for '{slug}' with language-specific version (lang: {cosmos_language_id})")

        path = self._resource_queue_path
        ensure_parent_dir(path)
        need_header = (not path.exists()) or (path.stat().st_size == 0)
        payload = resource_data.dict()
        
        questions_str = ""
        questions_data = payload.get("questions")
        if questions_data:
             questions_str = json.dumps(questions_data)

        row = {
            "tag": tag,
            "slug": slug,
            "title": payload.get("title") or "",
            "description": payload.get("description") or "",
            "icon": payload.get("icon") or "",
            "content": payload.get("content") or "",
            "language_id": payload.get("language_id") or "",
            "region": payload.get("region") or "",
            "content_type": payload.get("content_type") or "",
            "created_by": payload.get("created_by") or "System",
            "cosmos_language_id": cosmos_language_id or "",
            "questions": questions_str,
            "level": payload.get("level") or "",
            # ──────────────────────────────────────────────────────────
            # NEW: standalone flag — "true" if resource has no global original.
            # Existing resources get "" (falsy) so behavior is unchanged.
            # ──────────────────────────────────────────────────────────
            "standalone": "true" if standalone else "",
        }
        fieldnames = list(row.keys())
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if need_header:
                writer.writeheader()
            writer.writerow(row)
        
        self.queued_slugs.add(slug)

    def _queue_klp_post(
        self,
        slug: str,
        title: str,
        description: str,
        level: str,
        content_type: str,
        language_id: str,
        region: str,
        created_by: str,
        cosmos_language_id: str,
        questions: list,
        derived_from_id: str = None,
    ) -> None:
        """Append a KLP POST payload to processed_data/klps.csv.

        KLPs are separate from Resources in LME and use the /klps/ endpoint.

        Each language version of a KLP is appended as a separate row.
        The global (original) row is written first, and subsequent
        translated versions are appended after it.  post_klps_from_csv()
        sorts by content_type priority (original -> adapted -> translated)
        so the original is always POSTed first, followed by PATCHes for
        each translation — matching how _queue_resource_post works.
        """
        # Guard: skip duplicate global rows for the same slug
        if slug in self.queued_klp_slugs:
            if not cosmos_language_id:
                print(f"Skipping duplicate global queue for KLP '{slug}' (already queued)")
                return
            print(f"Appending translated version for KLP '{slug}' (lang: {cosmos_language_id})")

        path = self._klp_queue_path
        ensure_parent_dir(path)
        need_header = (not path.exists()) or (path.stat().st_size == 0)

        questions_str = ""
        if questions:
            questions_str = json.dumps(questions)

        row = {
            "slug": slug,
            "title": title,
            "description": description or "",
            "level": level,
            "content_type": content_type or "original",
            "language_id": language_id or "",
            "region": region or "",
            "created_by": created_by or "System",
            "cosmos_language_id": cosmos_language_id or "",
            "questions": questions_str,
            "derived_from_id": derived_from_id or "",
        }

        fieldnames = list(row.keys())
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if need_header:
                writer.writeheader()
            writer.writerow(row)

        self.queued_klp_slugs.add(slug)
        print(f"Queued KLP to klps.csv: {slug}")

    def _ensure_linked_resources_exist(self, default_region: str = "africa") -> None:
        """Pre-process KLPs to create placeholder resources for unresolved links. (FORBIDDEN)"""
        pass
    
    def _create_resource_from_link(self, link: str, default_region: str = "africa", cosmos_language_id: str = None, lme_language_id: str = None) -> Optional[str]:
        """Create a placeholder resource from a link and return the resource ID."""
        if not link:
            return None
        
        tag = None
        title = ""
        slug = ""
        table_name = None
        raw_id = None
        
        # Parse video:/path links
        video_asset_id = None
        video_icon_asset_id = None
        if link.startswith("video:/"):
            tag = "video"
            path = link[len("video:/"):]
            title = self._extract_title_from_video_path(path)
            filename = path.split("/")[-1]
            filename_slug = slugify(filename)
            title_slug = slugify(title)
            slug_tail = merge_slug_parts(title_slug, filename_slug)
            slug = f"res-video-{default_region}-{slug_tail}"

            # Upload the actual video asset using the correct prefix
            # For global migration: video prefix = "english WHO"
            # For localized: use the language-specific prefix from _get_media_config
            video_lang = cosmos_language_id or "global"
            asset_prefix = f"{slugify(title)}"
            video_asset_id = self._get_or_create_video_asset(
                path, video_lang, asset_prefix, use_prefix=True
            )
            video_icon_asset_id = self._get_or_create_icon_asset(
                path, video_lang, "video_icon", asset_prefix, use_prefix=True
            )
            if video_asset_id:
                print(f"  ✓ Uploaded video asset for '{path}' → {video_asset_id}")
            else:
                print(f"  ⚠ Could not upload video asset for '{path}', creating placeholder without content")
        
        # Parse 'res-...' slugs (already converted in Stage 1)
        elif link.startswith("res-"):
            match = re.match(r"^res-(video|drug|procedure|action-card)-(.+)$", link)
            if match:
                tag = match.group(1)
                slug_tail = match.group(2)
                title = slug_tail.replace("-", " ").title()
                slug = link
                
        # Parse drug:id and procedure:id links
        else:
            match = re.match(r"^(drug|procedure|action-card):(.+)$", link)
            if match:
                tag = match.group(1)
                raw_id = match.group(2)
                # Strip timestamp suffix
                base_id = re.sub(r"[_-]\d{10,}$", "", raw_id)
                title = base_id.replace("-", " ").replace("_", " ").title()
                slug_tail = slugify(base_id)
                slug = f"res-{tag}-{default_region}-{slug_tail}"
                
                if tag == "drug":
                    table_name = "drugs"
                elif tag == "procedure":
                    table_name = "procedures"
                elif tag == "action-card":
                    table_name = "action-cards"
        
        if not tag or not slug:
            print(f"  ⚠ Could not parse link format: {link}")
            return None
            
        # Localized Document Fetching Logic
        # Only applicable if we have a table to query (drugs/procedures) and a target language
        source_doc = None
        if cosmos_language_id and table_name:
            source_doc = self._get_localized_resource_doc(table_name, raw_id, cosmos_language_id)
             
        # NO GLOBAL FALLBACK: If we have a target language and can't find the localized doc,
        # skip this resource entirely. Only use _get_resource_by_link for non-language-specific cases.
        if not source_doc and cosmos_language_id:
            print(f"  ℹ️  No localized resource found for '{link}' (lang: {cosmos_language_id}). Skipping (no global fallback).")
            return None
        
        # For non-language-specific (global) migration, try fetching by link
        if not source_doc:
            source_doc = self._get_resource_by_link(link)

        if not source_doc and tag != "video":
            print(f"  ⚠ Could not find resource document for {link}")
            return None
        
        # Generate Markdown / Resource Data
        if source_doc:
            resource_data = DataFactory.create_resource_data(source_doc, table_type=tag)
        else:
            # Create placeholder data — for videos, include the uploaded asset content
            resource_data = ResourcePostRequestData(
                title=title,
                description=title,
                icon=video_icon_asset_id if tag == "video" else None,
                content=video_asset_id if tag == "video" else "", 
                questions=[]
            )

        # Override Title/Description from SCREENS table
        if cosmos_language_id:
            # Construct screen key base (e.g. drug:betamethasone)
            # Use raw_id if available, else try to derive from link
            screen_key = None
            if raw_id:
                screen_key = f"{tag}:{raw_id}"
            elif tag in ["drug", "procedure", "action-card"]:
                 # heuristic fallback
                 screen_key = link
            
            if screen_key:
                trans_desc = self._get_screen_translation(screen_key, cosmos_language_id)
                if trans_desc:
                    print(f"  ✓ Applied screen translation for title: {trans_desc[:30]}...")
                    resource_data.title = trans_desc
                    resource_data.description = trans_desc

        # Check if already exists in mapping (idempotency)
        if slug in self.resource_slug_mapping:
            return self.resource_slug_mapping[slug]
        
        # Create payload
        payload = resource_data.model_dump()
        payload["content_type"] = "translated" if cosmos_language_id else "original"
        payload["region"] = default_region
        payload["created_by"] = source_doc.get("LastUpdatedBy", "System") if source_doc else "System"
        
        if cosmos_language_id:
             payload["cosmos_language_id"] = cosmos_language_id
        
        if lme_language_id:
             payload["language_id"] = lme_language_id
        
        # Final fallback for title
        if not payload.get("title"):
             payload["title"] = title
             
        # POST to API
        api_url = POST_RESOURCE.format(tag=tag)
        try:
            response = self.session.post(api_url, json=payload)
            
            # Handle 409 conflict (already exists)
            if response.status_code == 409:
                resource_id = self._fetch_resource_id_by_slug(slug, tag)
                if resource_id:
                    self.resource_slug_mapping[slug] = resource_id
                    self._append_resource_slug_mapping(slug, resource_id)
                    print(f"  ✓ Found existing resource '{slug}' -> {resource_id}")
                    return resource_id
                return None
            
            response.raise_for_status()
            
            # Extract resource ID
            resource_id = self._extract_resource_id_from_response(response)
            if resource_id:
                self.resource_slug_mapping[slug] = resource_id
                self._append_resource_slug_mapping(slug, resource_id)
                print(f"  ✓ Created placeholder '{slug}' -> {resource_id}")
                
                # Activate version
                version_id = self._extract_version_id_from_response(response)
                if version_id:
                    self._activate_version(version_id, is_klp=False)
                
                return resource_id
            else:
                print(f"  ⚠ No resource_id in response for {slug}")
                return None
                
        except requests.exceptions.RequestException as e:
            print(f"  ✗ Failed to create placeholder for '{link}': {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"    Response: {e.response.text[:200]}")
            return None

    def post_klps_from_csv(self) -> None:
        """Read processed klps.csv and POST to /klps/ API, updating mapping."""
        path = self._klp_queue_path
        
        # Fetch existing KLPs first to avoid duplicates (O(1) local cache building vs O(N) per resource fallback)
        self._fetch_existing_klps()
        
        if not path.exists():
            print("No klps.csv found to post.")
            return
        
        with path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        
        if not rows:
            print("klps.csv is empty.")
            return
        
        print(f"Posting {len(rows)} KLPs from klps.csv...")
        
        lme_to_entry: Dict[str, Dict[str, str]] = {}
        for cosmos_id, entry in (self.language_mapping or {}).items():
            lme_id = entry.get("lme_language_id")
            if lme_id:
                lme_to_entry[lme_id] = entry
        cosmos_to_region = self._load_processed_languages_regions()
        
        priority = {"original": 0, "adapted": 1, "translated": 2}
        rows.sort(key=lambda r: priority.get(r.get("content_type", ""), 99))
        
        for row in rows:
            slug = row.get("slug", "").strip()
            if not slug:
                continue
            
            # Resolve language_id and region
            raw_lang = row.get("language_id", "").strip()
            cosmos_lang = row.get("cosmos_language_id", "").strip()
            final_lang = ""
            final_region = row.get("region") or "africa"
            
            if raw_lang:
                if raw_lang in self.language_mapping:
                    entry = self.language_mapping[raw_lang]
                    final_lang = entry.get("lme_language_id", "") or ""
                    final_region = entry.get("region") or final_region
                elif raw_lang in lme_to_entry:
                    final_lang = raw_lang
                    final_region = lme_to_entry[raw_lang].get("region") or final_region
            
            # 1. Base Mapping Lookup (Check 2): Get Language ID and Default Region
            if not final_lang and cosmos_lang and cosmos_lang in self.language_mapping:
                entry = self.language_mapping[cosmos_lang]
                final_lang = entry.get("lme_language_id", "")
                final_region = entry.get("region", final_region)
                if final_lang:
                    print(f"  → Recovered missing Language ID for KLP via mapping: {final_lang}")

                # 2. Processed Data Override (Check 1): Override Region if specific assignment exists in languages.csv
                if cosmos_lang and cosmos_to_region.get(cosmos_lang):
                    final_region = cosmos_to_region[cosmos_lang] or final_region

            
            content_type = row.get("content_type", "").strip()
            if content_type not in {"original", "adapted", "translated"}:
                content_type = "translated" if final_lang else "original"
            
            # Parse questions JSON
            questions = []
            questions_str = row.get("questions", "")
            if questions_str:
                try:
                    questions = json.loads(questions_str)
                except Exception as e:
                    from error_logger import log_error
                    log_error("Captured Exception", exc=e)
                    print(f"Error parsing questions for {slug}: {e}")
            
            # Resolve raw links to Resource UUIDs
            # The LME backend expects link to be a Resource UUID (creates LINKS_TO relationship)
            # link_type is stored as a string property on the KLPQuestion node
            for q in questions:
                if "link" not in q or not q["link"]:
                    continue

                raw_link = q["link"]

                # Derive link_type from the raw prefix before we overwrite the link value
                if ":" in raw_link:
                    q["link_type"] = raw_link.split(":", 1)[0]

                # Step 1: Try to resolve to an existing Resource UUID
                resource_id = self._resolve_link_ref(raw_link, default_region=final_region)
                if resource_id:
                    q["link"] = resource_id
                    print(f"  ✓ Resolved KLP link '{raw_link}' → {resource_id}")
                    continue

                # Step 2: Resource not found – try to create it on-the-fly
                created_id = self._create_resource_from_link(
                    raw_link,
                    default_region=final_region,
                    cosmos_language_id=cosmos_lang,
                    lme_language_id=final_lang,
                )
                if created_id:
                    q["link"] = created_id
                    print(f"  ✓ Created & linked KLP resource for '{raw_link}' → {created_id}")
                    continue

                # Step 3: All resolution failed – remove link to avoid API error
                print(f"  ⚠ Warning: Link '{raw_link}' could not be resolved or created, removing from question")
                del q["link"]
                if "link_type" in q:
                    q["link_type"] = None

            
            # Build KLP payload matching KeyLearningPointCreateRequest schema
            payload = {
                "title": row.get("title", ""),
                "description": row.get("description") or None,
                "level": row.get("level", "1"),
                "content_type": content_type,
                "created_by": row.get("created_by") or "System",
                "language_id": final_lang or None,
                "region": final_region or None,
                "questions": questions,
                "derived_from_id": row.get("derived_from_id") or None,
            }
            
            # Check if KLP exists
            existing_id = self.klp_slug_mapping.get(slug)
            if existing_id:
                api_url = UPDATE_KLP.format(klp_id=existing_id)
                method = "PATCH"
                action_desc = f"Update KLP {slug}"
                
                # Filter immutable fields for PATCH
                # RULES (from LME team):
                # 1. language_id is REQUIRED for translated versions
                # 2. content_type is REQUIRED (and should be "translated" if lang exists)
                # 3. created_by, derived_from_id must be EXCLUDED
                excluded_fields = {"created_by", "derived_from_id"}
                
                request_payload = {k: v for k, v in payload.items() if k not in excluded_fields}
                
                # Only override to "translated" if content_type is not already "adapted"
                if request_payload.get("language_id") and request_payload.get("content_type") != "adapted":
                     request_payload["content_type"] = "translated"
            else:
                api_url = POST_KLP
                method = "POST"
                action_desc = f"Create KLP {slug}"
                request_payload = payload
            
            try:
                response = self.session.request(method, api_url, json=request_payload)
                
                # Fallback to POST if PATCH returns 404
                if response.status_code == 404 and method == "PATCH":
                    if content_type == "translated" or request_payload.get("language_id"):
                        print(f"  ⚠ KLP {slug} not found (404). Skipping translated version creation (No Identity).")
                        continue
                    
                    print(f"  ⚠ KLP {slug} not found on server (404), falling back to POST...")
                    method = "POST"
                    api_url = POST_KLP
                    request_payload = payload  # Use full payload for POST
                    response = self.session.request(method, api_url, json=request_payload)
                
                response.raise_for_status()
                
            except requests.exceptions.RequestException as e:
                # Handle 409 Conflict - KLP exists but not in our mapping
                if getattr(e, "response", None) is not None and e.response.status_code == 409 and method == "POST":
                    print(f"  → Slug '{slug}' already exists in LME, fetching existing ID...")
                    # Try to fetch just this KLP
                    existing_id = self._fetch_resource_id_by_slug(slug, "key-learning-point")
                    if existing_id:
                        self.klp_slug_mapping[slug] = existing_id
                        self._append_klp_slug_mapping(slug, existing_id)
                        
                        api_url = UPDATE_KLP.format(klp_id=existing_id)
                        method = "PATCH"
                        action_desc = f"Update KLP {slug} ({existing_id})"
                        
                        try:
                            response = self.session.request(method, api_url, json=payload)
                            response.raise_for_status()
                            # Success, break out to process response
                        except Exception as retry_exc:
                            from error_logger import log_error
                            log_error("Captured Exception", exc=retry_exc)
                            print(f"Failed to {method} KLP {slug} after retry: {retry_exc}")
                            continue
                    else:
                        print(f"  ⚠ Could not fetch existing ID for '{slug}', skipping")
                        continue
                else:
                    print(f"Failed to {method} KLP {slug}: {e}")
                    if hasattr(e, "response") and e.response is not None:
                         print(f"Response Error: {e.response.text}")
                    continue
            
            # Extract KLP ID and version from response
            try:
                data = response.json()
                klp_id = data.get("klp_id") or data.get("id")
                version_id = None
                # First try versions array (contains only the newly created version)
                versions = data.get("versions", [])
                if versions and isinstance(versions, list) and len(versions) > 0:
                    best = max(versions, key=lambda v: float(v.get("version", 0)) if isinstance(v, dict) else 0)
                    version_id = best.get("klp_version_id") if isinstance(best, dict) else None
                
                # Check draft adapted/translated versions
                if not version_id:
                    for draft_key in ("draft_translated_versions", "draft_adapted_versions"):
                        drafts = data.get(draft_key)
                        if drafts and isinstance(drafts, list) and len(drafts) > 0:
                            best_draft = max(drafts, key=lambda v: float(v.get("version", 0)) if isinstance(v, dict) else 0)
                            version_id = best_draft.get("klp_version_id") if isinstance(best_draft, dict) else None
                            if version_id:
                                break

                # Fallback to other locations
                if not version_id:
                    version_id = (
                        data.get("klp_version_id")
                        or data.get("version_id")
                        or (data.get("current_original_version") or {}).get("klp_version_id")
                    )
                
                if klp_id:
                    self.klp_slug_mapping[slug] = klp_id
                    self._append_klp_slug_mapping(slug, klp_id)
                    action_verb = "Updated" if method == "PATCH" else "Created"
                    print(f"{action_verb} KLP '{slug}' -> {klp_id}")
                    
                    # Activate the version if we got a version_id
                    if version_id:
                        self._activate_version(version_id, is_klp=True)
                    else:
                        print(f"  ⚠ No version_id found for KLP '{slug}', cannot activate")
                else:
                    print(f"Warning: No klp_id in response for {slug}")
            except Exception as e:
                from error_logger import log_error
                log_error("Captured Exception", exc=e)
                print(f"Error parsing response for {slug}: {e}")

    def _fetch_existing_klps(self) -> None:
        """Fetch existing KLPs from API and populate slug mapping."""
        print("Fetching existing KLPs from API...")
        base = LME_BASE_URL.rstrip("/")
        url = f"{base}/klps/"
        
        try:
            # Use paginated helper to get all items
            # User Request: Keep KLPs safe at 200 limit
            count = 0
            for item in self._get_all_items_paginated(url, limit=200):
                slug = item.get("slug")
                klp_id = item.get("klp_id") or item.get("id")
                if slug and klp_id:
                    self.klp_slug_mapping[slug] = klp_id
                    count += 1
            print(f"Fetched {count} existing KLPs from API")
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Error fetching KLPs: {e}")

    def post_resources_from_csv(self) -> None:
        """Read processed resources.csv and POST to API, updating mapping."""
        path = self._resource_queue_path
        
        # Populate mapping from API first to avoid duplicates (O(1) local cache building)
        self._fetch_existing_resources()

        if not path.exists():
            print("No resources.csv found to post.")
            return
        with path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            # Build reverse map from LME language id -> mapping entry to recover region
            lme_to_entry: Dict[str, Dict[str, str]] = {}
            for cosmos_id, entry in (self.language_mapping or {}).items():
                lme_id = entry.get("lme_language_id")
                if lme_id:
                    lme_to_entry[lme_id] = entry
            # Build cosmos_id -> region from processed languages.csv to override region
            cosmos_to_region = self._load_processed_languages_regions()

            # Prepare enriched rows with computed final_lang/region/content_type and priority
            enriched = []
            priority = {"original": 0, "adapted": 1, "translated": 2}
            for idx, row in enumerate(rows):
                raw_lang = (row.get("language_id") or "").strip()
                cosmos_lang = (row.get("cosmos_language_id") or "").strip()
                final_lang = ""
                final_region = row.get("region") or "africa"
                if raw_lang:
                    if raw_lang in self.language_mapping:
                        entry = self.language_mapping[raw_lang]
                        final_lang = entry.get("lme_language_id", "") or ""
                        final_region = entry.get("region") or final_region
                    elif raw_lang in lme_to_entry:
                        entry = lme_to_entry[raw_lang]
                        final_lang = raw_lang
                        final_region = entry.get("region") or final_region
                # 1. Base Mapping Lookup (Check 2): Get Language ID and Default Region
                if not final_lang and cosmos_lang and cosmos_lang in self.language_mapping:
                    entry = self.language_mapping[cosmos_lang]
                    final_lang = entry.get("lme_language_id", "")
                    final_region = entry.get("region", final_region)
                    if final_lang:
                        print(f"  → Recovered missing Language ID for Resource via mapping: {final_lang}")

                # 2. Processed Data Override (Check 1): Override Region if specific assignment exists in languages.csv
                if cosmos_lang and cosmos_to_region.get(cosmos_lang):
                    final_region = cosmos_to_region[cosmos_lang] or final_region

                row_ct = (row.get("content_type") or "").strip()
                if row_ct not in {"original", "adapted", "translated"}:
                    row_ct = "translated" if final_lang else "original"
                enriched.append(
                    {
                        "row": row,
                        "final_lang": final_lang,
                        "final_region": final_region,
                        "cosmos_lang": cosmos_lang,
                        "content_type": row_ct,
                        "priority": priority.get(row_ct, 99),
                        "index": idx,
                    }
                )

            # Sort logic: 
            # 1. Content Type Priority (original -> adapted -> translated)
            # 2. Resource Type Priority (Dependencies first: video, drug, procedure -> then KLP)
            # 3. Original Index (stable sort)

            # Define type priority (lower is earlier)
            type_priority = {
                 "video": 0,
                 "drug": 1,
                 "procedure": 2,
                 "action-card": 3,
                 "key-learning-point": 10
            }

            def get_sort_key(e):
                row = e["row"]
                tag = (row.get("tag") or "").strip()
                # Default to highet priority number (last) if unknown
                t_prio = type_priority.get(tag, 5) 
                
                # Special case: KLPs should be last
                if tag == "key-learning-point":
                    t_prio = 10
                
                return (e["priority"], t_prio, e["index"])

            enriched.sort(key=get_sort_key)

            for e in enriched:
                row = e["row"]
                tag = (row.get("tag") or "").strip()
                slug = (row.get("slug") or "").strip()
                
                questions = None
                questions_str = row.get("questions")
                if questions_str:
                    try:
                        questions = json.loads(questions_str)
                    except Exception as err:
                        from error_logger import log_error
                        log_error("Captured Exception", exc=err)
                        print(f"Error parsing questions for {slug}: {err}")

                if questions:
                    # Resolve links in questions - remove unresolved links
                    res_cosmos_lang = e.get("cosmos_lang") or ""
                    res_final_region = e.get("final_region") or "india"
                    res_final_lang = e.get("final_lang") or ""
                    for q in questions:
                        if "link" in q:
                            link_val = q.get("link") or ""
                            if not link_val:
                                continue

                            # Derive link_type from the raw prefix
                            if ":" in link_val:
                                q["link_type"] = link_val.split(":", 1)[0]

                            # Step 1: Try to resolve to an existing Resource UUID
                            resolved = self._resolve_link_ref(link_val, default_region=res_final_region)
                            if resolved:
                                q["link"] = resolved
                                print(f"  ✓ Resolved resource KLP link '{link_val}' → {resolved}")
                                continue

                            # Step 2: Try to create the Resource on-the-fly
                            created_id = self._create_resource_from_link(
                                link_val,
                                default_region=res_final_region,
                                cosmos_language_id=res_cosmos_lang,
                                lme_language_id=res_final_lang,
                            )
                            if created_id:
                                q["link"] = created_id
                                print(f"  ✓ Created & linked resource KLP for '{link_val}' → {created_id}")
                                continue

                            # Step 3: All resolution failed – remove link
                            print(f"  ⚠ Warning: Link '{link_val}' could not be resolved or created, removing")
                            del q["link"]
                            if "link_type" in q:
                                q["link_type"] = None

                # Fallback: API requires language_id for translated, and region for adapted.
                r_ct = row.get("content_type") or e["content_type"]
                r_lang = e["final_lang"] or None
                r_region = e["final_region"] or None
                
                if r_ct == "translated" and not r_lang:
                     print(f"Warning: '{slug}' is {r_ct} but has no language_id. Defaulting to 'original'.")
                     r_ct = "original"
                elif r_ct == "adapted" and not r_region:
                     print(f"Warning: '{slug}' is {r_ct} but has no region. Defaulting to 'original'.")
                     r_ct = "original"

                payload = ResourcePostRequestData(
                    title=row.get("title") or "",
                    description=row.get("description") or "",
                    icon=(row.get("icon") or None) or None,
                    content=row.get("content") or None,
                    language_id=r_lang,
                    region=e["final_region"],
                    content_type=r_ct,
                    created_by=row.get("created_by") or "System",
                    questions=questions,
                    level=row.get("level") or None,
                )
                
                # Special handling for KLPs (Key Learning Points) as Nodes
                if tag == "key-learning-point":
                    existing_id = self.resource_slug_mapping.get(slug)
                    if existing_id:
                        # Update KLP
                        api_url = UPDATE_KLP.format(klp_id=existing_id)
                        method = "PATCH"
                        action_desc = f"Update KLP {slug} ({existing_id})"
                    else:
                        # Create KLP
                        api_url = POST_KLP
                        method = "POST"
                        action_desc = f"Create KLP {slug}"
                    
                    # Construct KLP-specific payload
                    # Remove fields that are not in KeyLearningPointCreateRequest/UpdateRequest if necessary
                    # For now using dict() and we'll see if we need to filter. 
                    # KLP Payload: title, description, level, content_type, etc. 
                    # It does NOT take 'content' or 'icon' in the root.
                    # FIX: Explicitly construct dict to ensure questions are included and structure matches API
                    klp_payload = {
                        "title": row.get("title") or "",
                        "description": row.get("description") or None,
                        "level": row.get("level") or "1",
                        "content_type": r_ct,
                        "language_id": r_lang,
                        "region": e["final_region"],
                        "questions": questions if questions else [],
                        "created_by": row.get("created_by") or "System"
                    }
                    # Use this payload for request
                    request_payload = klp_payload
                    
                else:
                    # Generic Resource Handling
                    # Check if resource already exists in mapping
                    existing_resource_id = self.resource_slug_mapping.get(slug)
                    if existing_resource_id:
                        # Update existing resource
                        api_url = UPDATE_RESOURCE.format(resource_id=existing_resource_id)
                        method = "PATCH"
                        action_desc = f"Update resource {slug} ({existing_resource_id})"
                    else:
                        # Create new resource
                        api_url = POST_RESOURCE.format(tag=tag)
                        method = "POST"
                        action_desc = f"Create resource {slug}"
                    
                    # STRICT RULE: Secondary/Translated content MUST NOT create new identity
                    # UNLESS it's a standalone resource (no global counterpart) or a video
                    # (videos never have global originals — they're language-specific).
                    if method == "POST" and (r_lang or r_ct == "translated"):
                        is_standalone = (row.get("standalone") or "").strip().lower() == "true"
                        is_video = (tag == "video")
                        if not is_standalone and not is_video:
                            print(f"Skipping resource '{slug}' ({r_ct}) - No Global identity found for translation.")
                            continue
                        else:
                            print(f"  → POSTing {'standalone' if is_standalone else 'video'} resource '{slug}' directly (no global original)")
                    
                    # Exclude KLP-specific fields for generic resources to match schema
                    request_payload = payload.dict(exclude={"questions", "level", "derived_from_id"})



                try:
                    response = self.session.request(method.upper(), api_url, json=request_payload)
                    if response.status_code == 404 and method == "PATCH":
                        # Fallback to POST
                        if tag == "key-learning-point":
                             method = "POST"
                             api_url = POST_KLP
                        else:
                             method = "POST"
                             api_url = POST_RESOURCE.format(tag=tag)
                        response = self.session.request(method, api_url, json=request_payload)
                    
                    # Handle 409 Conflict - resource exists but not in our mapping
                    if response.status_code == 409 and method == "POST":
                        print(f"  → Slug '{slug}' already exists in LME, fetching existing ID...")
                        existing_id = self._fetch_resource_id_by_slug(slug, tag)
                        if existing_id:
                            # Update our mapping and retry with PATCH
                            self.resource_slug_mapping[slug] = existing_id
                            self._append_resource_slug_mapping(slug, existing_id)
                            if tag == "key-learning-point":
                                api_url = UPDATE_KLP.format(klp_id=existing_id)
                            else:
                                api_url = UPDATE_RESOURCE.format(resource_id=existing_id)
                            method = "PATCH"
                            response = self.session.request(method, api_url, json=request_payload)
                        else:
                            print(f"  ⚠ Could not fetch existing ID for '{slug}', skipping")
                            continue
                    
                    response.raise_for_status()
                
                except requests.exceptions.RequestException as e:
                     print(f"Failed to {method} {slug}: {e}")
                     if getattr(e, "response", None) is not None:
                        print(f"Response Error Body: {e.response.text}")
                     continue
                if not response:
                    continue
                rid = self._extract_resource_id_from_response(response)
                version_id = self._extract_version_id_from_response(response)
                
                if rid:
                    self.resource_slug_mapping[slug] = rid
                    self._append_resource_slug_mapping(slug, rid)
                    action_verb = "Updated" if method == "PATCH" else "Created"
                    print(f"{action_verb} resource '{slug}' [{e['content_type']}] -> {rid}")
                    
                    # Activate the version if we got a version_id
                    is_klp = (tag == "key-learning-point")
                    if version_id:
                        self._activate_version(version_id, is_klp=is_klp)
                    else:
                        print(f"  ⚠ No version_id found for '{slug}', cannot activate")
                else:
                    print(f"❌ Failed to get resource ID for '{slug}' after successful {method} request.")
                    print(f"Response status: {response.status_code}, body: {response.text}")


    def _get_all_items_paginated(self, url: str, limit: int = 200) -> Any:
        """Yield items from a paginated API endpoint."""
        offset = 0
        while True:
            params = {"limit": limit, "offset": offset}
            try:
                resp = self.session.get(url, params=params)
                if resp.status_code != 200:
                    print(f"  ⚠ Failed paginated fetch: {resp.status_code} {resp.text[:100]}")
                    break
                data = resp.json()
                # Handle list or dict with 'items'
                items = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                if not items:
                    break
                
                for item in items:
                    yield item
                
                if len(items) < limit:
                    break
                    
                offset += limit
            except Exception as e:
                from error_logger import log_error
                log_error("Captured Exception", exc=e)
                print(f"  ⚠ Error in paginated fetch: {e}")
                break

    def _fetch_existing_resources(self) -> None:
        """Fetch existing resources from API and populate slug mapping."""
        # Ensure clean URL joining
        base = LME_BASE_URL.rstrip("/")
        # Critical Perf Fix: Removing ?include_versions=true stops LME from serializing megabytes of draft HTML content string arrays per resource, dropping fetch time from 30+ seconds to < 2 seconds.
        url = f"{base}/resources/"
            
        try:
            # Use paginated helper to get all items
            # User Request: Speed up resources (1000) but keep KLPs safe (200)
            count = 0
            for item in self._get_all_items_paginated(url, limit=1000):
                slug = item.get("slug")
                rid = item.get("resource_id") or item.get("id")
                if slug and rid:
                    self.resource_slug_mapping[slug] = rid
                    count += 1
            print(f"Loaded {count} existing resource mappings from API.")
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Error fetching existing resources: {e}")

    def _fetch_resource_id_by_slug(self, slug: str, tag: str) -> Optional[str]:
        """Fetch resource ID by slug from LME API.
        
        Used when we get a 409 conflict but don't have the ID in our local mapping.
        """
        # First check if we already have it in mapping
        if slug in self.resource_slug_mapping:
            return self.resource_slug_mapping[slug]
        
        base = LME_BASE_URL.rstrip("/")
        
        # Try fetching resources and find by slug
        if tag == "key-learning-point":
            url = f"{base}/klps/"
            limit = 200
        else:
            # Also removed include_versions from the fallback 409 fetch mechanism for extra safety.
            url = f"{base}/resources/"
            limit = 1000
        
        try:
            # Use paginated fetch to filter client-side if server doesn't support filter by slug
            # This is slow but robust for 409 resolution
            for item in self._get_all_items_paginated(url, limit=limit):
                item_slug = item.get("slug")
                if item_slug == slug:
                    rid = item.get("resource_id") or item.get("klp_id") or item.get("id")
                    if rid:
                         return rid
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Warning: Error fetching resource by slug: {e}")
        
        return None

    def _load_processed_languages_regions(self) -> Dict[str, str]:
        """Load cosmos_id -> region map from processed languages.csv if present."""
        result: Dict[str, str] = {}
        try:
            lang_csv = get_processed_file("languages.csv")
            if not lang_csv.exists():
                return result
            with lang_csv.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cosmos_id = (row.get("cosmos_id") or "").strip()
                    region = (row.get("region") or "").strip()
                    if cosmos_id and region:
                        result[cosmos_id] = region
        except Exception as exc:
            from error_logger import log_error
            log_error("Captured Exception", exc=exc)
            print(f"Warning: could not load processed languages.csv for regions: {exc}")
        return result

    # ------------------------------
    # Retry helper
    # ------------------------------
    def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        json: Dict[str, Any],
        headers: Dict[str, str],
        entity_desc: str,
        attempts: int = 3,
        base_delay: float = 1.0,
    ):
        for attempt in range(1, attempts + 1):
            try:
                resp = requests.request(method.upper(), url, json=json, headers=headers)
                resp.raise_for_status()
                return resp
            except requests.exceptions.RequestException as exc:
                if attempt == attempts:
                    print(f"❌ Failed to {method} {entity_desc}: {exc}")
                    if getattr(exc, "response", None) is not None:
                        print(f"Response Error Body: {exc.response.text}")
                    # Log payload for debugging
                    print(f"Payload: {json}")
                    return None
                delay = base_delay * (2 ** (attempt - 1))
                print(
                    f"Retry {attempt}/{attempts} for {entity_desc} after error: {exc}. Waiting {delay:.1f}s"
                )
                if getattr(exc, "response", None) is not None and attempt == 1:
                     print(f"Response Error Body (Attempt 1): {exc.response.text}")
                time.sleep(delay)

    # ------------------------------
    # Language mapping helper
    # ------------------------------
    def _map_language_info(self, cosmos_lang_id: str) -> tuple[Optional[str], str]:
        """Return (lme_language_id, region) for a Cosmos language id if mapped.

        If cosmos_lang_id is empty or not found, returns (None, "africa").
        """
        default_region = "africa"
        print(f"Mapping language:   {cosmos_lang_id}")
        if not cosmos_lang_id:
            return None, default_region
        entry = self.language_mapping.get(cosmos_lang_id)
        if not entry:
            return None, default_region
        lme_id = entry.get("lme_language_id") or None
        region = entry.get("region") or default_region
        return lme_id, region

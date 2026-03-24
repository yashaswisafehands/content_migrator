"""Module migration functionality for Cosmos DB to LME migration."""

import csv
import os
from typing import Any, Dict, List

import requests
from azure.cosmos import CosmosClient

from configs import PATCH_MODULE, POST_MODULE, JWT_TOKEN, ACTIVATE_MODULE_VERSION
import time
from csv_resource_map import load_module_resource_map
from data_models import ModuleData
from factories import DataFactory
from path_utils import ensure_parent_dir, get_mappings_file, get_processed_file
from resource_migrator import ResourceMigrator
from slug_utils import slugify
from bundle_loader import _download_content_bundle


class ModuleMigrator:
    """Handles migration of modules from Cosmos DB to LME."""

    def __init__(
        self,
        cosmos_client: CosmosClient,
        container,
        language_mapping: Dict[str, Dict[str, str]],
        module_resource_map: Dict[str, Dict[str, list]] = None,
    ):
        self.cosmos_client = cosmos_client
        self.container = container
        self.language_mapping = language_mapping
        self.resource_migrator = ResourceMigrator(
            cosmos_client, container, language_mapping
        )
        if module_resource_map is not None:
            self.module_resource_map = module_resource_map
        else:
            csv_path = os.path.join(os.path.dirname(__file__), "module_resource_summary.csv")
            self.module_resource_map = load_module_resource_map(csv_path)
        self.module_slug_mapping: Dict[str, str] = {}
        self._module_queue_path = get_processed_file("modules.csv")
        ensure_parent_dir(self._module_queue_path)
        self.queued_slugs = set()
        self._queued_module_data: Dict[str, Dict[str, str]] = {}
        self._load_queued_slugs()

    def _load_queued_slugs(self) -> None:
        """Load existing slugs and module data from modules.csv for merging."""
        if not self._module_queue_path.exists():
            return
        try:
            with self._module_queue_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    slug = row.get("slug", "")
                    if slug:
                        self.queued_slugs.add(slug)
                        # Store full module data for potential merging
                        self._queued_module_data[slug] = row.copy()
            print(f"Loaded {len(self.queued_slugs)} queued module slugs")
        except Exception as e:
            print(f"Warning: Could not load queued module slugs: {e}")

    def migrate_all_modules(self, language_filter: str = None) -> None:
        """Queue module payloads only (no API calls).

        Reads languages.csv in processed_data, processes 'global' first,
        then each remaining cosmos_id. Creates/updates modules.csv.
        """
        print("🚀 Starting module migration (queue stage)...")
        self._load_module_slug_mapping()
        target_ids = self._get_target_language_cosmos_ids()
        
        if language_filter:
            print(f"Filtering migration for language ID: {language_filter}")
            # Filter existing list or use the provided ID directly if valid
            if language_filter in target_ids:
                target_ids = [language_filter]
            else:
                # If not in the CSV list, just attempt it directly
                target_ids = [language_filter]

        print(f"Processing languages: {target_ids}")
        queued = 0
        for cid in target_ids:
            label = "global" if cid == "global" else cid
            
            if cid == "global" or cid == "en":
                # For global/English modules, use Cosmos DB and module_resource_summary.csv
                modules = self._get_modules_for_language(cid)
                print("Found %d modules from Cosmos for %s" % (len(modules), label))
                for module_doc in modules:
                    try:
                        self._migrate_single_module(module_doc)
                        queued += 1
                    except Exception as exc:
                        name = module_doc.get("description") or module_doc.get(
                            "id", "unknown"
                        )
                        print(
                            "❌ Error preparing module %s (%s): %s"
                            % (name, label, exc)
                        )
            else:
                # For translated languages, download content-bundle.json and bypass Cosmos DB
                print(f"Downloading content-bundle.json for translated language '{cid}'...")
                try:
                    bundle = _download_content_bundle(cid)
                    bundle_modules = bundle.get("modules", [])
                    print(f"Found {len(bundle_modules)} modules from content-bundle.json for {label}")
                    
                    global_module_keys = list(self.module_resource_map.keys())
                    
                    # Create the translated summary CSV
                    translated_dir = os.path.join(os.path.dirname(__file__), "data", "translated_module_data")
                    os.makedirs(translated_dir, exist_ok=True)
                    csv_path = os.path.join(translated_dir, f"translated_module_resource_summary_{cid}.csv")
                    
                    print(f"Writing translated module summary to {csv_path}...")
                    try:
                        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                            writer = csv.writer(f)
                            writer.writerow(["module_name", "module_key", "actionCards", "procedures", "videos", "drugs", "keyLearningPoints", "total_resources"])
                            for b_mod in bundle_modules:
                                mod_id = b_mod.get("id")
                                if mod_id not in global_module_keys:
                                    continue
                                ac = ", ".join(b_mod.get("actionCards", []))
                                proc = ", ".join(b_mod.get("procedures", []))
                                vid = ", ".join(b_mod.get("videos", []))
                                drugs = ", ".join(b_mod.get("drugs", []))
                                klp = ", ".join(b_mod.get("keyLearningPoints", []))
                                total_res = len(b_mod.get("actionCards", [])) + len(b_mod.get("procedures", [])) + len(b_mod.get("videos", [])) + len(b_mod.get("drugs", [])) + len(b_mod.get("keyLearningPoints", []))
                                writer.writerow([b_mod.get("description", ""), mod_id, ac, proc, vid, drugs, klp, total_res])
                    except Exception as e:
                        print(f"  ⚠️ Could not write translated module CSV: {e}")

                    for b_mod in bundle_modules:
                        mod_id = b_mod.get("id")
                        if mod_id not in global_module_keys:
                            print(f"  ⚠️ Skipping module '{mod_id}' as it is not in the global module_resource_summary.csv")
                            continue

                        # Query Cosmos DB for the localized module document to get its English title
                        # The module key in the bundle includes a timestamp (e.g., "low-birth-weight_1536047907931")
                        # We try the full key first, then the clean key without timestamp
                        english_title = "Unknown"
                        for try_key in [mod_id, mod_id.split("_")[0]]:
                            query = f"SELECT TOP 1 c.description, c.title FROM c WHERE c._table='modules' AND c.key='{try_key}' AND c.langId='{cid}'"
                            results = list(self.container.query_items(query=query, enable_cross_partition_query=True))
                            if results and (results[0].get("description") or results[0].get("title")):
                                english_title = results[0].get("description") or results[0].get("title")
                                print(f"  → Found English title from Cosmos (key='{try_key}'): '{english_title}'")
                                break
                            
                        # Construct a synthetic module doc that _migrate_single_module expects
                        module_doc = {
                            "id": mod_id,
                            "key": mod_id, # Usually id acts as key in the bundle
                            "langId": cid,
                            "language_id": cid,
                            "bundle_desc": b_mod.get("description"), # Save the bundle description as a fallback for translation
                            # The bundle's icon is full URL, but drug migrator looks at icon or iconPath
                            "icon": b_mod.get("icon"), 
                            "iconPath": b_mod.get("icon"),
                            # Direct full resource arrays
                            "actionCards": b_mod.get("actionCards", []),
                            "procedures": b_mod.get("procedures", []),
                            "drugs": b_mod.get("drugs", []),
                            "keyLearningPoints": b_mod.get("keyLearningPoints", []),
                            "videos": b_mod.get("videos", []),
                            # Provide the English title as description so DataFactory sets title = english_title
                            "description": english_title,
                            "LastUpdatedBy": "System",
                        }
                        try:
                            self._migrate_single_module(module_doc)
                            queued += 1
                        except Exception as exc:
                            print(f"❌ Error preparing module from bundle {mod_id} ({label}): {exc}")
                except Exception as e:
                    print(f"❌ Failed to process content-bundle.json for {cid}: {e}")
                    
        print("Queued module payloads: %d" % queued)

    def _get_all_modules(self, is_global: bool) -> List[Dict]:
        """Retrieve module documents from Cosmos DB using static module key list."""
        # Enforce module_resource_summary.csv as the single authoritative source of global module identity
        module_keys = list(self.module_resource_map.keys())
        
        if is_global:
            lang_clause = "AND (NOT IS_DEFINED(c.langId) OR c.langId = '')"
        else:
            lang_clause = "AND IS_DEFINED(c.langId) AND c.langId != ''"

        modules = []
        for module_key in module_keys:
            query = (
                f"SELECT * FROM c WHERE c._table='modules' "
                f"AND c.key='{module_key}' {lang_clause}"
            )
            results = list(
                self.container.query_items(query=query, enable_cross_partition_query=True)
            )
            modules.extend(results)
        
        return modules
    def _migrate_single_module(self, module_doc: Dict) -> None:
        """Queue a single module's payload and related resource slugs."""
        description = module_doc.get("description", "Unknown")
        cosmos_id = module_doc.get("id")

        cosmos_lang_id = module_doc.get("langId") or module_doc.get("language_id") or ""
        module_key = module_doc.get("key") or module_doc.get("id")  # Get module key
        
        resource_source_doc = module_doc.copy()

        # ──────────────────────────────────────────────────────────────
        # RESOURCE RESOLUTION
        # ──────────────────────────────────────────────────────────────
        if not cosmos_lang_id:
            # GLOBAL module: Use module_resource_summary.csv as the authoritative source
            # for payload creation, per invariant doctrine.
            # Global modules must NOT contain videos.
            print(f"[ModuleMigrator] Overriding Cosmos structure for GLOBAL module '{module_key}' using CSV.")
            resource_source_doc["videos"] = []
            
            if module_key in self.module_resource_map:
                csv_resources = self.module_resource_map[module_key]
                for rtype, rlist in csv_resources.items():
                    if rtype != "videos":
                        resource_source_doc[rtype] = rlist
                    print(f"  {rtype}: {len(rlist)} keys overridden from CSV")
            else:
                 print(f"  ⚠️ GLOBAL module '{module_key}' not found in CSV. Using Cosmos fallback.")
        else:
            # TRANSLATED module: Cosmos document already has the correct
            # resource lists for this language. No CSV override needed.
            print(f"[ModuleMigrator] Using Cosmos doc resource lists for TRANSLATED module '{module_key}' (lang: {cosmos_lang_id})")
            for rtype in ["actionCards", "procedures", "drugs", "keyLearningPoints", "videos"]:
                items = resource_source_doc.get(rtype) or []
                if rtype == "keyLearningPoints" and not items:
                    items = resource_source_doc.get("key_learning_points") or []
                    resource_source_doc["keyLearningPoints"] = items
                print(f"  {rtype}: {len(items)} keys")

        # IDENTITY RESOLUTION (Global vs Local) — needed BEFORE resource migration
        # so that video naming uses the English module title, not a translated one.
        slug_source_title = None
        if cosmos_lang_id:
            module_key_for_lookup = module_doc.get("key")
            if module_key_for_lookup:
                # Strip the timestamp from the bundle key to get the clean global key
                clean_key = module_key_for_lookup.split("_")[0]
                query = f"SELECT TOP 1 * FROM c WHERE c._table='modules' AND c.key='{clean_key}' AND (NOT IS_DEFINED(c.langId) OR c.langId = '')"
                results = list(self.container.query_items(query=query, enable_cross_partition_query=True))
                if results and (results[0].get("title") or results[0].get("description")):
                    global_module_doc = results[0]
                    slug_source_title = global_module_doc.get("title") or global_module_doc.get("description")
                    print(f"  → Resolved Global Identity: '{slug_source_title}'")
                elif clean_key in self.module_resource_map:
                    slug_source_title = clean_key.replace("-", " ").title()
                    print(f"  → Resolved Global Identity (fallback from clean key): '{slug_source_title}'")
                else:
                    slug_source_title = clean_key.replace("-", " ").title()
                    print(f"  ⚠ STRUCTURAL VIOLATION: Global identity document not found in Cosmos for localized module '{clean_key}'. Proceeding with placeholder title: {slug_source_title}")
        else:
            slug_source_title = module_doc.get("title") or module_doc.get("description") or module_key.replace("-", " ").title()

        # Step 1: Queue all resources within this module first
        print("  Step 1: Preparing module resources (queue)...")
        resource_slugs = self.resource_migrator.migrate_module_resources(resource_source_doc, global_module_title=slug_source_title)

        # Step 2: Create base module payload
        print("  Step 2: Queuing module payload with resource slugs...")
        module_data = DataFactory.create_module_data(module_doc)

        # TRANSLATION INJECTION (Screens Table)
        # Requirement: Use screens table for translated description
        # Key format: module:<key> OR module:<id>
        # We try both to be robust against data inconsistencies
        if cosmos_lang_id:
            keys_to_try = []
            if module_key:
                keys_to_try.append(module_key)
            
            # If ID is different from Key, try ID as well
            mod_id = module_doc.get("id")
            if mod_id and mod_id != module_key:
                keys_to_try.append(mod_id)
            
            found_trans = None
            for k in keys_to_try:
                trans_key = f"module:{k}"
                found_trans = self.resource_migrator._get_screen_translation(
                    trans_key, cosmos_lang_id
                )
                if found_trans:
                    print(f"  → Found translation for module using key '{trans_key}'")
                    break
            
            if found_trans:
                # Replace description with translation
                module_data.description = found_trans
            else:
                # Fallback to bundle description if screens table doesn't have it
                bundle_desc = module_doc.get("bundle_desc")
                if bundle_desc:
                    print(f"  → Using bundle description fallback for translated module")
                    module_data.description = bundle_desc

        # Reset media lists to use resource slugs (videos/procedures/drugs TBD)
        module_data.videos = resource_slugs.get("videos", [])
        module_data.action_cards = resource_slugs.get("actionCards", [])
        module_data.practical_procedures = resource_slugs.get("procedures", [])
        module_data.drugs = resource_slugs.get("drugs", [])
        module_data.key_learning_points = resource_slugs.get("keyLearningPoints", [])

        # slug_source_title was already resolved above (before resource migration)
        # Defer language_id/region resolution to posting stage; store cosmos id
        self._queue_module_post(module_doc, module_data, slug_source_title=slug_source_title)

    def _create_module_data_with_resources(
        self,
        module_doc: Dict,
        resource_ids: Dict[str, List[str]],
    ) -> ModuleData:
        """Create module data with references to migrated resources."""
        # Create basic module data using factory
        module_data = DataFactory.create_module_data(module_doc)

        # Get language information from the module document
        language_id = module_doc.get("language_id", "")
        lme_language_id = None
        region = "africa"

        if language_id and language_id in self.language_mapping:
            # Look up language mapping
            mapping_data = self.language_mapping[language_id]
            lme_language_id = mapping_data["lme_language_id"]
            region = mapping_data["region"]

        # TRANSLATION INJECTION (Screens Table)
        # Requirement: Use screens table for translated description if available
        # Key format: module:<id>
        if language_id:
            # Try both key and id
            mod_key = module_doc.get("key")
            mod_id = module_doc.get("id")
            
            keys_to_try = []
            if mod_key:
                keys_to_try.append(mod_key)
            if mod_id and mod_id != mod_key:
                keys_to_try.append(mod_id)
            
            found_trans = None
            for k in keys_to_try:
                trans_key = f"module:{k}"
                found_trans = self.resource_migrator._get_screen_translation(
                    trans_key, language_id
                )
                if found_trans:
                    print(f"  → Found translation for module using key '{trans_key}'")
                    break
            
            if found_trans:
                module_data.description = found_trans

        # Set language and region information
        module_data.language_id = lme_language_id
        module_data.region = region

        # Override the resource arrays with migrated resource IDs
        # Note: The factory creates asset IDs for videos, but we now have
        # resource IDs. We need to update this to use the migrated
        # resource IDs instead
        module_data.videos = resource_ids.get("videos", [])
        module_data.action_cards = resource_ids.get("actionCards", [])
        module_data.practical_procedures = resource_ids.get("procedures", [])
        module_data.drugs = resource_ids.get("drugs", [])

        return module_data

    def _create_or_update_module(
        self,
        module_data: ModuleData,
        description: str,
    ) -> None:
        """Deprecated: posting happens in post_modules_from_csv()."""
        raise NotImplementedError(
            "Queue-first flow: use migrate_all_modules() and post_modules_from_csv()"
        )

    def _load_module_slug_mapping(self) -> None:
        """Load existing module slug to module ID mapping from CSV."""
        mapping_path = get_mappings_file("module_slug_mapping.csv")

        if not mapping_path.exists():
            print("No existing module slug mapping found, starting fresh")
            return

        try:
            with mapping_path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self.module_slug_mapping[row["slug"]] = row["module_id"]
            print("Loaded %d existing module mappings" % len(self.module_slug_mapping))
        except Exception as e:
            print(f"Warning: Could not load module slug mapping: {e}")

    def _save_module_slug_mapping(self) -> None:
        """Deprecated: avoid overwriting mapping file."""
        pass

    def _append_module_slug_mapping(self, slug: str, module_id: str) -> None:
        """Append single module slug mapping to CSV (no overwrite)."""
        output_path = get_mappings_file("module_slug_mapping.csv")
        ensure_parent_dir(output_path)
        need_header = not output_path.exists() or output_path.stat().st_size == 0
        with output_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if need_header:
                writer.writerow(["slug", "module_id"])
            writer.writerow([slug, module_id])

    def get_module_mapping(self) -> Dict[str, str]:
        """Get the current module slug mapping."""
        return self.module_slug_mapping

    def _activate_module_version(self, version_id: str) -> bool:
        if os.environ.get("MIGRATE_ENV") == "devcontent":
            print(f"  → Skipping module activation for devcontent.")
            return True
        """Activate a module version by calling status endpoint.
        
        Args:
            version_id: The module_version_id to activate
            
        Returns:
            True if activation succeeded, False otherwise
        """
        if not version_id:
            return False
        
        url = ACTIVATE_MODULE_VERSION.format(version_id=version_id)
        payload = {"status": "active", "updated_by": "System"}
        headers = {"Content-Type": "application/json"}
        if JWT_TOKEN:
            headers["Authorization"] = f"Bearer {JWT_TOKEN}"
        
        try:
            response = requests.patch(url, json=payload, headers=headers)
            if response.status_code in (200, 204):
                print(f"  ✓ Activated module version: {version_id}")
                return True
            else:
                print(f"  ⚠ Failed to activate module version {version_id}: {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            print(f"  ⚠ Error activating module version {version_id}: {e}")
            return False

    # ------------------------------
    # New helpers for queue/post flow
    # ------------------------------
    def _get_target_language_cosmos_ids(self) -> List[str]:
        """Read processed languages.csv and return cosmos IDs with 'global' first."""
        langs_path = get_processed_file("languages.csv")
        if not langs_path.exists():
            # Default to processing only global if languages.csv not present
            return ["global"]
        ids: List[str] = []
        with langs_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cid = (row.get("cosmos_id") or "").strip() or "global"
                ids.append(cid)
        # Ensure 'global' is first and unique ordering preserved
        seen = set()
        ordered: List[str] = []
        if "global" in ids:
            ordered.append("global")
            seen.add("global")
        for cid in ids:
            if cid not in seen:
                ordered.append(cid)
                seen.add(cid)
        return ordered

    def _get_modules_for_language(self, cosmos_lang_id: str) -> List[Dict]:
        """Look up modules for a specific language (or global) using CSV authoritative list."""
        # Enforce module_resource_summary.csv as the single authoritative source of global module identity
        module_keys = list(self.module_resource_map.keys())
        
        if cosmos_lang_id == "global":
            lang_clause = "AND (NOT IS_DEFINED(c.langId) OR c.langId = '')"
        else:
            lang_clause = f"AND c.langId = '{cosmos_lang_id}'"
        
        modules = []
        for module_key in module_keys:
            if not module_key:
                continue
            query = (
                f"SELECT TOP 1 * FROM c WHERE c._table='modules' "
                f"AND c.key='{module_key}' {lang_clause}"
            )
            results = list(
                self.container.query_items(query=query, enable_cross_partition_query=True)
            )
            if results:
                modules.append(results[0])
            elif cosmos_lang_id == "global":
                # Ensure the migration processes all modules, even if the global document is missing
                print(f"  ⚠️ Global document for '{module_key}' is missing in Cosmos DB. Proceeding with placeholder.")
                fallback_title = module_key.replace("-", " ").title()
                modules.append({
                    "id": module_key,
                    "key": module_key,
                    "title": fallback_title,
                    "description": fallback_title,
                    "icon": "",
                    "_table": "modules"
                })
        
        return modules

    def _queue_module_post(self, module_doc: Dict, module_data: ModuleData, slug_source_title: str = None) -> None:
        """Append or update a module payload in processed_data/modules.csv.

        If slug already exists, merge resource lists (videos, procedures, drugs, etc.)
        instead of skipping. Otherwise append new row.
        """
        path = self._module_queue_path
        ensure_parent_dir(path)

        cosmos_lang_id = (
            module_doc.get("langId")
            or module_doc.get("language_id")
            or ""
        )
        # Use explicit slug source if provided (Global Title), otherwise fallback to module data title
        title_for_slug = slug_source_title if slug_source_title else module_data.title
        slug = f"mod-{slugify(title_for_slug)}"

        # Fail-safe: if the localized Cosmos query failed to find the English title,
        # fallback to the Global Cosmos title which we just successfully resolved
        if slug_source_title and (not module_data.title or module_data.title == "Unknown"):
            module_data.title = slug_source_title

        # Serialize list fields as comma-separated slugs
        def _join(items: List[str]) -> str:
            return ",".join([str(x) for x in items if x])

        # Helper to merge comma-separated lists (preserving order, no duplicates)
        def _merge_lists(existing: str, new: str) -> str:
            existing_items = [x.strip() for x in existing.split(",") if x.strip()]
            new_items = [x.strip() for x in new.split(",") if x.strip()]
            # Add new items not already in existing
            merged = existing_items.copy()
            for item in new_items:
                if item not in merged:
                    merged.append(item)
            return ",".join(merged)

        new_row = {
            "slug": slug,
            "title": module_data.title or "",
            "description": module_data.description or "",
            "icon": module_data.icon or "",
            "created_by": module_data.created_by or "System",
            "language_cosmos_id": cosmos_lang_id,
            "videos": _join(module_data.videos),
            "action_cards": _join(module_data.action_cards),
            "practical_procedures": _join(module_data.practical_procedures),
            "key_learning_points": _join(module_data.key_learning_points),
            "drugs": _join(module_data.drugs),
        }

        # Fields to merge (resource lists)
        resource_fields = ["videos", "action_cards", "practical_procedures", "key_learning_points", "drugs"]

        if slug in self.queued_slugs:
            # Merge with existing module data
            existing_row = self._queued_module_data.get(slug, {})
            merged_row = existing_row.copy()
            
            # Update non-resource fields only if empty in existing
            for key in ["title", "description", "icon", "created_by"]:
                if not merged_row.get(key) and new_row.get(key):
                    merged_row[key] = new_row[key]
            
            # Merge resource lists
            for field in resource_fields:
                existing_val = existing_row.get(field, "")
                new_val = new_row.get(field, "")
                merged_row[field] = _merge_lists(existing_val, new_val)
            
            # Update the in-memory data
            self._queued_module_data[slug] = merged_row
            
            # Rewrite the entire CSV with updated data
            self._rewrite_modules_csv()
            print(f"  → Merged module '{slug}' with new resources")
        else:
            # Append new row
            need_header = (not path.exists()) or (path.stat().st_size == 0)
            fieldnames = list(new_row.keys())
            with path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if need_header:
                    writer.writeheader()
                writer.writerow(new_row)
            
            self.queued_slugs.add(slug)
            self._queued_module_data[slug] = new_row
            print("  → Queued module '%s' -> %s" % (new_row["title"], path))

    def _rewrite_modules_csv(self) -> None:
        """Rewrite modules.csv with current in-memory data."""
        path = self._module_queue_path
        if not self._queued_module_data:
            return
        
        # Get fieldnames from first row
        first_row = next(iter(self._queued_module_data.values()))
        fieldnames = list(first_row.keys())
        
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for slug in sorted(self._queued_module_data.keys()):
                writer.writerow(self._queued_module_data[slug])

    def post_modules_from_csv(self) -> None:
        """Read modules.csv, resolve resource and language mappings, POST/PATCH."""
        path = self._module_queue_path
        if not path.exists():
            print("No modules.csv found to post.")
            return

        # Normalize CSV schema on-the-fly to the latest expected header
        self._normalize_modules_csv_schema()

        # Load language mapping from file if not already in memory
        self._load_language_mapping_from_file()

        # Load resource slug mapping
        resource_map = self._load_resource_slug_mapping_for_modules()

        # Ensure module slug mapping is loaded
        self._load_module_slug_mapping()

        # Preview: List all modules to be processed
        print("\n📋 Modules to be processed:")
        with path.open("r", encoding="utf-8") as f:
            preview_reader = csv.DictReader(f)
            for idx, row in enumerate(preview_reader, 1):
                title = row.get("title") or "Untitled"
                slug = row.get("slug") or f"mod-{slugify(title)}"
                cosmos_id = (row.get("language_cosmos_id") or "").strip() or "global"
                existing_id = self.module_slug_mapping.get(slug)
                action = f"PATCH ({existing_id})" if existing_id else "POST (new)"
                label = "🌐 global" if cosmos_id == "global" else f"🌍 {cosmos_id[:8]}..."
                print(f"  {idx:>3}. [{action}] {title}  ({slug})  [{label}]")
        print()

        with path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                title = row.get("title") or "Untitled"
                slug = row.get("slug") or f"mod-{slugify(title)}"
                # Resolve LME Language ID and Region from CSV cosmos_id
                csv_cosmos_id = (row.get("language_cosmos_id") or "").strip() or "global"
                
                # Note: Default to None
                lme_lang_id = None
                region = "africa"

                if csv_cosmos_id and csv_cosmos_id != "global":
                    if csv_cosmos_id in self.language_mapping:
                        entry = self.language_mapping[csv_cosmos_id]
                        lme_lang_id = entry.get("lme_language_id")
                        region = entry.get("region", region)
                    else:
                        # Try fallback to lookup language by simple ID? Or just skip?
                        # If we don't have mapping, we can't translate correctly.
                        pass

                def _split(val: str) -> List[str]:
                    if not val:
                        return []
                    # accept comma/semicolon/pipe separated
                    for sep in (",", ";", "|"):
                        if sep in val:
                            return [s.strip() for s in val.split(sep) if s.strip()]
                    return [val.strip()] if val.strip() else []

                def _map_slugs(slug_list: List[str]) -> List[str]:
                    ids: List[str] = []
                    for s in slug_list:
                        rid = resource_map.get(s)
                        if rid:
                            ids.append(rid)
                        else:
                            # Enhanced logging to help debug mismatches
                            print(f"    ⚠️ [Module: {title}] Missing ID for slug '{s}' (Check mapping files!)")
                    return ids

                videos_slugs = _split(row.get("videos") or "")
                action_slugs = _split(row.get("action_cards") or "")
                proc_slugs = _split(row.get("practical_procedures") or "")
                drug_slugs = _split(row.get("drugs") or "")
                klp_slugs = _split(row.get("key_learning_points") or "")

                videos_ids = _map_slugs(videos_slugs)
                action_ids = _map_slugs(action_slugs)
                proc_ids = _map_slugs(proc_slugs)
                drug_ids = _map_slugs(drug_slugs)
                klp_ids = _map_slugs(klp_slugs)

                # DEBUG: Show resource resolution for this module
                print(f"\n  📋 Module '{title}' [{slug}] resource resolution:")
                print(f"     Videos:     {len(videos_slugs)} slugs → {len(videos_ids)} IDs")
                print(f"     ActionCards:{len(action_slugs)} slugs → {len(action_ids)} IDs")
                print(f"     Procedures: {len(proc_slugs)} slugs → {len(proc_ids)} IDs")
                print(f"     Drugs:      {len(drug_slugs)} slugs → {len(drug_ids)} IDs")
                print(f"     KLPs:       {len(klp_slugs)} slugs → {len(klp_ids)} IDs")
                if action_ids:
                    print(f"     (sample action_card ID: {action_ids[0]})")
                if drug_ids:
                    print(f"     (sample drug ID: {drug_ids[0]})")

                payload = {
                    "title": title,
                    "description": row.get("description") or "",
                    "icon": row.get("icon") or None,
                    "created_by": "MigrationScript",
                    "videos": videos_ids,
                    "action_cards": action_ids,
                    "practical_procedures": proc_ids,
                    "drugs": drug_ids,
                    "key_learning_points": klp_ids,
                }
                
                # Add translation fields if this is a localized module
                if lme_lang_id:
                    payload["language_id"] = lme_lang_id
                    payload["region"] = region
                    payload["content_type"] = "translated"
                else:
                    payload["content_type"] = "original"

                # Decide POST or PATCH
                if slug in self.module_slug_mapping:
                    existing_id = self.module_slug_mapping[slug]
                    url = PATCH_MODULE.format(module_id=existing_id)
                    method = "PATCH"
                else:
                    # STRICT RULE: Secondary/Translated content MUST NOT create new identity.
                    # If this is a translated module (language_cosmos_id present) and slug is missing -> SKIP.
                    if csv_cosmos_id and csv_cosmos_id != "global":
                        print(f"Skipping module '{title}' ({slug}) - Global identity not found (Secondary language {csv_cosmos_id}).")
                        continue
                    
                    url = POST_MODULE
                    method = "POST"

                headers = {"Content-Type": "application/json"}
                if JWT_TOKEN:
                    headers["Authorization"] = f"Bearer {JWT_TOKEN}"

                # DEBUG: Log the method, URL, and payload contents
                print(f"  🔄 {method} → {url}")
                print(f"     Payload resource counts: videos={len(videos_ids)}, action_cards={len(action_ids)}, "
                      f"procedures={len(proc_ids)}, drugs={len(drug_ids)}, klps={len(klp_ids)}")
                if lme_lang_id:
                    print(f"     language_id={lme_lang_id}, region={region}, content_type=translated")
                else:
                    print(f"     content_type=original (global)")

                resp = self._request_with_retry(
                    method=method,
                    url=url,
                    json=payload,
                    headers=headers,
                    entity_desc=f"module {title}",
                    slug=slug,  # Pass slug for 409 handling
                )
                if not resp:
                    print(f"  ❌ No response for module '{title}'")
                    continue
                
                # DEBUG: Log response status and body
                print(f"  📨 Response: {resp.status_code}")
                try:
                    data = resp.json() if resp.content else {}
                    # Show resource arrays in response
                    cv = data.get("current_version") or {}
                    if cv:
                        print(f"     Response current_version resources:")
                        print(f"       videos: {len(cv.get('videos', []))} items")
                        print(f"       action_cards: {len(cv.get('action_cards', []))} items")
                        print(f"       practical_procedures: {len(cv.get('practical_procedures', []))} items")
                        print(f"       drugs: {len(cv.get('drugs', []))} items")
                        print(f"       key_learning_points: {len(cv.get('key_learning_points', []))} items")
                except ValueError:
                    data = {}
                    print(f"     ⚠ Response body not valid JSON")
                
                # Extract module_id and version_id
                # IMPORTANT: For PATCH responses creating translated versions,
                # we must pick the latest version (highest version number) to
                # activate the newly-created translated version, not the original.
                mid = data.get("module_id")
                version_id = (
                    data.get("module_version_id")
                    or data.get("version_id")
                )
                if not version_id:
                    versions = data.get("versions") or []
                    if versions:
                        best = max(
                            versions,
                            key=lambda v: float(v.get("version", 0)) if isinstance(v, dict) else 0,
                        )
                        version_id = best.get("module_version_id") if isinstance(best, dict) else None
                if not version_id:
                    # Check draft translated versions
                    for draft_key in ("draft_translated_versions", "draft_adapted_versions"):
                        drafts = data.get(draft_key) or []
                        if drafts:
                            best_draft = max(
                                drafts,
                                key=lambda v: float(v.get("version", 0)) if isinstance(v, dict) else 0,
                            )
                            version_id = best_draft.get("module_version_id") if isinstance(best_draft, dict) else None
                            if version_id:
                                break
                if not version_id:
                    version_id = (data.get("current_version") or {}).get("module_version_id")
                
                if method == "POST" and mid:
                    self.module_slug_mapping[slug] = mid
                    self._append_module_slug_mapping(slug, mid)
                    print(f"Posted module '{title}' -> {mid}")
                elif method == "PATCH":
                    print(f"Updated module '{title}'")
                    # PATCH often doesn't return the version_id in the response.
                    # We MUST fetch the latest module details to get the version to activate.
                    if not version_id and slug in self.module_slug_mapping:
                        mid = self.module_slug_mapping[slug]
                        print(f"  → Fetching latest version for '{title}' to ensure activation...")
                        # Import here to avoid circular dependency if any (though Configs is safe)
                        from configs import LME_BASE_URL
                        get_url = f"{LME_BASE_URL.rstrip('/')}/modules/{mid}"
                        try:
                            g_headers = {"Content-Type": "application/json"}
                            if JWT_TOKEN:
                                g_headers["Authorization"] = f"Bearer {JWT_TOKEN}"
                            g_resp = requests.get(get_url, headers=g_headers)
                            if g_resp.status_code == 200:
                                g_data = g_resp.json()
                                # Try multiple paths to find the version ID
                                version_id = (
                                    g_data.get("current_version", {}).get("module_version_id")
                                    or (g_data.get("versions") or [{}])[0].get("module_version_id")
                                )
                                if version_id:
                                    print(f"  → Found latest version: {version_id}")
                        except Exception as e:
                            print(f"  ⚠ Error fetching updated module details: {e}")

                # Activate the version if we got a version_id
                if version_id:
                    self._activate_module_version(version_id)
                else:
                    print(f"  ⚠ No version_id found for module '{title}', cannot activate")

    def _normalize_modules_csv_schema(self) -> None:
        """Ensure modules.csv matches the latest schema.

        Expected columns:
        slug,title,description,icon,created_by,language_cosmos_id,
        videos,action_cards,practical_procedures,key_learning_points,drugs
        """
        path = self._module_queue_path
        try:
            with path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                original_fieldnames = reader.fieldnames or []
                needs_norm = (
                    "key_learning_points" not in (original_fieldnames or [])
                    or "language_id" in (original_fieldnames or [])
                    or "region" in (original_fieldnames or [])
                )
                if not needs_norm:
                    return
                rows = list(reader)
        except FileNotFoundError:
            return

        new_fieldnames = [
            "slug",
            "title",
            "description",
            "icon",
            "created_by",
            "language_cosmos_id",
            "videos",
            "action_cards",
            "practical_procedures",
            "key_learning_points",
            "drugs",
        ]

        def _copy(row: Dict[str, str], key: str) -> str:
            return (row.get(key) or "").strip()

        normalized: list[Dict[str, str]] = []
        for row in rows:
            normalized.append(
                {
                    "slug": _copy(row, "slug"),
                    "title": _copy(row, "title"),
                    "description": _copy(row, "description"),
                    "icon": _copy(row, "icon"),
                    "created_by": _copy(row, "created_by") or "System",
                    "language_cosmos_id": _copy(row, "language_cosmos_id"),
                    "videos": _copy(row, "videos"),
                    "action_cards": _copy(row, "action_cards"),
                    "practical_procedures": _copy(row, "practical_procedures"),
                    "key_learning_points": _copy(row, "key_learning_points"),
                    "drugs": _copy(row, "drugs"),
                }
            )

        ensure_parent_dir(path)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=new_fieldnames)
            writer.writeheader()
            writer.writerows(normalized)
        print("Normalized modules.csv to latest schema → %s" % path)

    def _load_resource_slug_mapping_for_modules(self) -> Dict[str, str]:
        """Load resource AND KLP slug->id mapping created by ResourceMigrator."""
        mapping: Dict[str, str] = {}
        
        # 1. Load Resources
        path_res = get_mappings_file("resource_slug_mapping.csv")
        if path_res.exists():
            try:
                with path_res.open("r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        s = row.get("slug") or ""
                        rid = row.get("resource_id") or ""
                        if s and rid:
                            mapping[s] = rid
            except Exception as e:
                print(f"Warning: Could not load resource slug mapping: {e}")
        else:
            print("Warning: resource_slug_mapping.csv not found")

        # 2. Load KLPs
        path_klp = get_mappings_file("klp_slug_mapping.csv")
        if path_klp.exists():
            try:
                with path_klp.open("r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        s = row.get("slug") or ""
                        rid = row.get("klp_id") or row.get("resource_id") or ""
                        if s and rid:
                            mapping[s] = rid
            except Exception as e:
                print(f"Warning: Could not load klp slug mapping: {e}")
        else:
            print("Warning: klp_slug_mapping.csv not found")
             
        return mapping

    def _load_language_mapping_from_file(self) -> None:
        """Populate self.language_mapping from processed language_mapping.csv if empty."""
        if self.language_mapping:
            return
        path = get_mappings_file("language_mapping.csv")
        if not path.exists():
            print("Warning: language_mapping.csv not found; language_id may be empty")
            return
        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cid = row.get("cosmos_id") or "global"
                    self.language_mapping[cid] = {
                        "lme_language_id": row.get("lme_language_id") or "",
                        "region": row.get("region") or "africa",
                    }
        except Exception as e:
            print(f"Warning: Could not load language mapping csv: {e}")
    
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
        slug: str = None,
        attempts: int = 3,
        base_delay: float = 1.0,
    ):
        for attempt in range(1, attempts + 1):
            try:
                resp = requests.request(method.upper(), url, json=json, headers=headers)
                
                # Handle 409 Conflict - module exists but not in our mapping
                if resp.status_code == 409 and method.upper() == "POST" and slug:
                    print(f"  → Module '{slug}' already exists in LME, fetching existing ID...")
                    existing_id = self._fetch_module_id_by_slug(slug)
                    if existing_id:
                        # Update mapping and retry with PATCH
                        self.module_slug_mapping[slug] = existing_id
                        self._append_module_slug_mapping(slug, existing_id)
                        url = PATCH_MODULE.format(module_id=existing_id)
                        resp = requests.request("PATCH", url, json=json, headers=headers)
                    else:
                        print(f"  ⚠ Could not fetch existing ID for module '{slug}', skipping")
                        return None
                
                resp.raise_for_status()
                return resp
            except requests.exceptions.RequestException as exc:
                if attempt == attempts:
                    print(f"❌ Failed to {method} {entity_desc}: {exc}")
                    return None
                delay = base_delay * (2 ** (attempt - 1))
                print(
                    f"Retry {attempt}/{attempts} for {entity_desc} after error: {exc}. Waiting {delay:.1f}s"
                )
                time.sleep(delay)

    def _fetch_module_id_by_slug(self, slug: str):
        """Fetch module ID by slug from LME API.
        
        Used when we get a 409 conflict but don't have the ID in our local mapping.
        """
        from configs import LME_BASE_URL
        
        # First check if we already have it
        if slug in self.module_slug_mapping:
            return self.module_slug_mapping[slug]
        
        base = LME_BASE_URL.rstrip("/")
        url = f"{base}/modules/"
        
        headers = {"Content-Type": "application/json"}
        if JWT_TOKEN:
            headers["Authorization"] = f"Bearer {JWT_TOKEN}"
        
        try:
            resp = requests.get(url, headers=headers, params={"limit": 5000})
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                for item in items:
                    item_slug = item.get("slug")
                    if item_slug == slug:
                        mid = item.get("module_id") or item.get("id")
                        if mid:
                            return mid
        except Exception as e:
            print(f"Warning: Error fetching module by slug: {e}")
        
        return None

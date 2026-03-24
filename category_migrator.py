import csv
import json
import os
from typing import Dict, List, Optional

import requests

from configs import LME_BASE_URL, JWT_TOKEN, UNIVERSAL_ACTIVATE_VERSION
from slug_utils import slugify
from path_utils import get_mappings_file

# Updated category → module mapping
CATEGORIES_DEFINITIONS = [
    {
        "description": "Sexual & reproductive health",
        "modules": [
            {"name": "Modern Contraception"},
            {"name": "Safe Abortion"},
            {"name": "Post Abortion Care"},
            {"name": "Female Genital Mutilation"},
        ]
    },
    {
        "description": "Pregnancy and birth complications",
        "modules": [
            {"name": "Hypertension"},
            {"name": "Prolonged Labour"},
            {"name": "Post Partum Hemorrhage"},
            {"name": "Manual Removal of Placenta"},
            {"name": "Maternal Sepsis"},
            {"name": "Gestational Diabetes Mellitus"},
        ]
    },
    {
        "description": "Maternal health",
        "modules": [
            {"name": "Antenatal Care"},
            {"name": "Postnatal Care"},
            {"name": "Normal Labour and Birth"},
            {"name": "Active Management of Third Stage Labour"},
            {"name": "Perinatal Mental Health"},
        ]
    },
    {
        "description": "Newborn health",
        "modules": [
            {"name": "Neonatal Resuscitation"},
            {"name": "Newborn Management"},
            {"name": "Low Birth Weight"},
            {"name": "Care of The Sick Newborn"},
        ]
    },
    {
        "description": "Infection prevention",
        "modules": [
            {"name": "Infection Prevention"},
        ]
    },
]

class CategoryMigrator:
    """Handles migration of categories (collections of modules) to LME."""

    def __init__(self):
        self.module_mapping: Dict[str, str] = {}
        self._load_module_mapping()
        # Cache for created categories: title.lower() -> category_id
        self.category_cache: Dict[str, str] = {}

    def _load_module_mapping(self) -> None:
        """Load module slug -> id mapping from CSV."""
        path = get_mappings_file("module_slug_mapping.csv")
        if not path.exists():
            print(f"Warning: {path} not found. Category migration may fail to find modules.")
            return

        try:
            with path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    slug = row.get("slug")
                    mid = row.get("module_id")
                    if slug and mid:
                        self.module_mapping[slug] = mid
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"Error loading module mapping: {e}")

    def _find_module_id(self, module_name: str) -> Optional[str]:
        """Find module_id by module name (via slug)."""
        # Construct slug: mod-{slugify(name)}
        slug = f"mod-{slugify(module_name)}"
        return self.module_mapping.get(slug)

    def migrate_categories(self, language_id: str, default_icon_id: Optional[str] = None):
        """Create/Update categories and link them to the given language."""
        print(f"Starting category migration for language {language_id}...")
        
        processed_category_ids: List[str] = []

        for category_def in CATEGORIES_DEFINITIONS:
            title = category_def.get("description")
            if not title:
                continue

            print(f"Processing category: {title}")
            module_ids = []
            
            # Resolve modules
            for module in category_def.get("modules", []):
                module_name = module.get("name")
                if module_name:
                    mod_id = self._find_module_id(module_name)
                    if mod_id:
                        module_ids.append(mod_id)
                        print(f"  Found module: {module_name} -> {mod_id}")
                    else:
                        print(f"  Module not found: {module_name} (slug: mod-{slugify(module_name)})")
            
            print(f"  Total modules found: {len(module_ids)}")

            category_slug = slugify(title)
            category_id = self._get_category_id_by_slug(category_slug)
            if not category_id:
                category_id = self._get_category_id_by_slug(f"cat-{category_slug}")
            
            if category_id:
                print(f"  Category exists: {category_id}, patching...")
                self._patch_category(category_id, module_ids)
            else:
                print(f"  Creating new category...")
                category_id = self._create_category(title, category_slug, module_ids, default_icon_id)
            
            if category_id:
                processed_category_ids.append(category_id)

        print(f"Category migration complete. {len(processed_category_ids)} categories processed.")

    def _get_category_id_by_slug(self, slug: str) -> Optional[str]:
        url = f"{LME_BASE_URL}/categories/"
        params = {"status_filter": "all", "include_versions": "true"}
        try:
            resp = requests.get(url, params=params, headers=self._headers())
            if resp.ok:
                data = resp.json()
                # Handle both list and paginated dict
                items = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                
                for c in items:
                    if c.get("slug") == slug:
                        return c.get("id") or c.get("category_id")
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"  Error fetching category by slug '{slug}': {e}")
        return None

    def _activate_latest_version(self, category_data: dict) -> None:
        """Extract latest version from LME response and activate via universal endpoint."""
        if os.environ.get("MIGRATE_ENV") == "devcontent":
            print(f"  → Skipping activation for devcontent.")
            return

        versions = category_data.get("versions", [])
        if not versions:
            # Fallback for older API versions that might not return 'versions' list
            current = category_data.get("current_version", {})
            version_id = current.get("category_version_id") or current.get("id")
        else:
            # Versions are sorted by version number descending in LME
            version_id = versions[0].get("category_version_id") or versions[0].get("id")

        if version_id:
            try:
                activate_url = UNIVERSAL_ACTIVATE_VERSION.format(version_id=version_id)
                resp = requests.patch(activate_url, headers=self._headers())
                resp.raise_for_status()
                print(f"  ✓ Activated category version: {version_id} (Universal)")
            except Exception as e:
                from error_logger import log_error
                log_error("Captured Exception", exc=e)
                print(f"  ⚠️ Failed to activate category version {version_id}: {e}")

    def _create_category(self, title: str, slug: str, module_ids: List[str], icon: Optional[str]) -> Optional[str]:
        url = f"{LME_BASE_URL}/categories/"
        payload = {
            "title": title,
            "slug": slug,
            "description": title,
            "modules": module_ids,
        }
        if icon:
            payload["icon"] = icon
            
        try:
            resp = requests.post(url, json=payload, headers=self._headers())
            
            if resp.status_code == 409:
                print(f"  Category '{slug}' already exists (409). Fetching existing ID...")
                # Retry fetch
                existing_id = self._get_category_id_by_slug(slug)
                if not existing_id:
                    existing_id = self._get_category_id_by_slug(f"cat-{slug}")
                    
                if existing_id:
                    print(f"  Found existing ID: {existing_id}. Patching modules...")
                    self._patch_category(existing_id, module_ids)
                    return existing_id
                else:
                    print(f"  Error: Category '{slug}' exists but could not retrieve ID (tried 'cat-' prefix too).")
                    return None

            resp.raise_for_status()
            data = resp.json()
            cat_id = data.get("category_id") or data.get("id")
            if cat_id:
                self._activate_latest_version(data)
            return cat_id
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"  Failed to create category: {e}")
            return None

    def _patch_category(self, category_id: str, module_ids: List[str]) -> None:
        url = f"{LME_BASE_URL}/categories/{category_id}"
        
        current_modules = []
        try:
            get_resp = requests.get(url, headers=self._headers())
            if get_resp.ok:
                current_modules = get_resp.json().get("modules", [])
        except Exception:
            from error_logger import log_error
            log_error("Captured Exception")
            pass
            
        # 2. Merge
        # Ensure unique
        new_set = set(current_modules)
        new_set.update(module_ids)
        merged_ids = list(new_set)
        
        payload = {"modules": merged_ids}
        try:
            resp = requests.patch(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            
            # activate using universal helper
            self._activate_latest_version(resp.json())
                
        except Exception as e:
            from error_logger import log_error
            log_error("Captured Exception", exc=e)
            print(f"  Failed to patch category: {e}")



    def _headers(self) -> Dict[str, str]:
        check = {}
        if JWT_TOKEN:
            check["Authorization"] = f"Bearer {JWT_TOKEN}"
        return check

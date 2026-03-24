"""Pre-stage utility: generate module_list.json from content-bundle(s).

Downloads the content-bundle JSON for each language in languages.csv,
extracts module IDs, and writes them to module_list.json.

URL pattern:
    https://sdacms.blob.core.windows.net/content/{langId}/content-bundle.json
"""

import csv
import json
import os
from typing import List, Set

import requests

from path_utils import get_processed_file

# BLOB_BASE is evaluated dynamically inside _download_content_bundle

def _get_language_ids_from_csv() -> List[str]:
    """Read languages.csv and return unique cosmos_id values."""
    langs_path = get_processed_file("languages.csv")
    if not langs_path.exists():
        print("⚠️  languages.csv not found, cannot generate module list")
        return []

    ids: Set[str] = set()
    with langs_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cid = (row.get("cosmos_id") or "").strip()
            if cid:
                ids.add(cid)
    return sorted(ids)


def _download_content_bundle(lang_id: str) -> dict:
    """Download content-bundle.json for a language from Azure Blob Storage."""
    env = os.environ.get("MIGRATE_ENV", "content")
    blob_base = f"https://sdacms.blob.core.windows.net/{env}"
    url = f"{blob_base}/{lang_id}/content-bundle.json"
    print(f"  ⬇ Downloading: {url}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _extract_module_keys(bundle: dict) -> List[str]:
    """Extract module IDs from a content-bundle JSON."""
    modules = bundle.get("modules", [])
    return [m["id"] for m in modules if "id" in m]


def generate_module_list() -> List[str]:
    """Main entry point: download bundles, extract modules, write module_list.json.

    Returns the final list of module keys.
    """
    lang_ids = _get_language_ids_from_csv()
    if not lang_ids:
        print("  No language IDs found — keeping existing module_list.json")
        return []

    all_module_keys: Set[str] = set()

    for lang_id in lang_ids:
        try:
            bundle = _download_content_bundle(lang_id)
            keys = _extract_module_keys(bundle)
            print(f"  ✓ {lang_id}: {len(keys)} modules found")
            all_module_keys.update(keys)
        except Exception as e:
            print(f"  ❌ Failed to process {lang_id}: {e}")

    if not all_module_keys:
        print("  ⚠️  No modules extracted — keeping existing module_list.json")
        return []

    # Write to module_list.json
    output_path = os.path.join(os.path.dirname(__file__), "module_list.json")
    sorted_keys = sorted(all_module_keys)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(sorted_keys, f, indent=2)

    print(f"  ✅ module_list.json updated: {len(sorted_keys)} modules")
    return sorted_keys


if __name__ == "__main__":
    print("=== Generating module_list.json from content bundles ===")
    keys = generate_module_list()
    if keys:
        for k in keys:
            print(f"  • {k}")

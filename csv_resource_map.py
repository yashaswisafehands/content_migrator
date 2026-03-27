import csv
import os
from typing import Dict, List


def load_module_resource_map(csv_path: str = None) -> Dict[str, Dict[str, List[str]]]:
    if csv_path is None:
        csv_path = os.path.join(os.path.dirname(__file__), "module_resource_summary.csv")

    if not os.path.exists(csv_path):
        print(f"[csv_resource_map] WARNING: CSV not found at '{csv_path}'. Returning empty map.")
        return {}

    resource_map: Dict[str, Dict[str, List[str]]] = {}
    resource_columns = ["actionCards", "procedures", "drugs", "keyLearningPoints"]

    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                module_key = (row.get("module_key") or "").strip()
                if not module_key:
                    continue

                entry: Dict[str, List[str]] = {}
                for col in resource_columns:
                    raw = (row.get(col) or "").strip()
                    if raw:
                        entry[col] = [k.strip() for k in raw.split(",") if k.strip()]
                    else:
                        entry[col] = []

                resource_map[module_key] = entry

        print(f"[csv_resource_map] Loaded {len(resource_map)} modules from CSV resource map.")
    except Exception as e:
        from error_logger import log_error
        log_error("Captured Exception", exc=e)
        print(f"[csv_resource_map] ERROR: Failed to parse CSV '{csv_path}': {e}")
        return {}

    return resource_map

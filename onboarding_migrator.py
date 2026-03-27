"""Onboarding Flow Migrator

Automates the creation and translation of onboarding flows for the LME API.

Can be used standalone (CLI) or imported by the migration pipeline
(global_migrator.py / main.py).

Standalone usage:
  # 1. Create the global (English) original
  python onboarding_migrator.py --mode global --dry-run

  # 2. Create a translated version (e.g. Marathi)
  python onboarding_migrator.py --mode translated \
      --cosmos-lang-id "mr" \
      --lme-lang-id "904e56726ee34730b9284fabd15ac833" \
      --dry-run

Pipeline usage (imported):
  from onboarding_migrator import OnboardingMigrator
  migrator = OnboardingMigrator()
  migrator.migrate_global()            # Stage 1 global
  migrator.migrate_translated("mr", "904e...")  # Stage 2 per-language
"""

import argparse
import copy
import csv
import json
import os
import sys
from typing import Any, Dict, List, Optional

import requests

from path_utils import get_processed_file, get_mappings_file, MAPPINGS_DIR, PROCESSED_DATA_DIR

# ── Constants ─────────────────────────────────────────────────────────────────

def _get_blob_base() -> str:
    return f"https://sdacms.blob.core.windows.net/{os.environ.get('MIGRATE_ENV', 'content')}"

BLOB_BASE = _get_blob_base()

# English WHO uses the "en" cosmos id for bundles
ENGLISH_COSMOS_LANG_ID = "en"

# ── Bundle question-id fragment → API question_id ─────────────────────────────
QUESTION_BUNDLE_KEY_MAP = {
    "QUESTION_1_SHARED_DEVICE":    "number-1",
    "QUESTION_2_NUMBER_OF_USERS":  "number-2",
    "QUESTION_3_COUNTRY":          "number-3",
    "QUESTION_4_HEALTHCARE_WORKER":"number-4",
    "QUESTION_5_PROFESSION":       "number-5",
    "QUESTION_6_EXPERIENCE":       "number-6",
    "QUESTION_7_WORKPLACE":        "number-7",
    "QUESTION_8_SOURCE":           "number-8",
}

# ── Answer-index map ──────────────────────────────────────────────────────────
# Maps each API answer_id → index in the content-bundle's answers[] array.
ANSWER_INDEX_MAP = {
    "QUESTION_1_SHARED_DEVICE": {
        "ANSWER_YES_SHARED":  0,
        "ANSWER_NO_SHARED":   1,
    },
    "QUESTION_2_NUMBER_OF_USERS": {
        "ANSWER_USERS_2_5":      0,
        "ANSWER_USERS_6_10":     1,
        "ANSWER_USERS_10_PLUS":  2,
    },
    "QUESTION_3_COUNTRY": {},
    "QUESTION_4_HEALTHCARE_WORKER": {
        "ANSWER_HEALTHCARE_YES": 0,
        "ANSWER_HEALTHCARE_NO":  1,
    },
    "QUESTION_5_PROFESSION": {
        "ANSWER_PROFESSION_PHYSICIAN": 0,
        "ANSWER_PROFESSION_MIDWIFE":   1,
        "ANSWER_PROFESSION_NURSE":     2,
        "ANSWER_PROFESSION_SBA":       3,
        "ANSWER_PROFESSION_STUDENT":   4,
    },
    "QUESTION_6_EXPERIENCE": {
        "ANSWER_EXP_STUDENT": 0,
        "ANSWER_EXP_LT1":    1,
        "ANSWER_EXP_1_5":    2,
        "ANSWER_EXP_6_10":   3,
        "ANSWER_EXP_11":     4,
    },
    "QUESTION_7_WORKPLACE": {
        "ANSWER_WORK_TERTIARY":   0,
        "ANSWER_WORK_SECONDARY":  1,
        "ANSWER_WORK_PRIMARY":    2,
        "ANSWER_WORK_UNIVERSITY": 3,
        "ANSWER_WORK_OTHER":      4,
    },
    "QUESTION_8_SOURCE": {
        "ANSWER_SOURCE_PRESERVICE":  0,
        "ANSWER_SOURCE_INSERVICE":   1,
        "ANSWER_SOURCE_STANDALONE":  2,
        "ANSWER_SOURCE_COLLEAGUE":   3,
        "ANSWER_SOURCE_EVENT":       4,
        "ANSWER_SOURCE_OTHER":       5,
        "ANSWER_SOURCE_PROGRAM":     6,
    },
}

# ── Graph structure template ──────────────────────────────────────────────────
GRAPH_TEMPLATE: List[Dict[str, Any]] = [
    {
        "question_id": "QUESTION_1_SHARED_DEVICE",
        "options_from": "neo4j",
        "order": 0,
        "user_specific": False,
        "answers": [
            {"answer_id": "ANSWER_NO_SHARED",  "value": "no",  "next_question_id": "QUESTION_3_COUNTRY"},
            {"answer_id": "ANSWER_YES_SHARED", "value": "yes", "next_question_id": "QUESTION_2_NUMBER_OF_USERS"},
        ],
    },
    {
        "question_id": "QUESTION_2_NUMBER_OF_USERS",
        "options_from": "neo4j",
        "order": 1,
        "user_specific": False,
        "answers": [
            {"answer_id": "ANSWER_USERS_10_PLUS", "value": "10_plus_users",  "next_question_id": "QUESTION_3_COUNTRY"},
            {"answer_id": "ANSWER_USERS_6_10",    "value": "6_to_10_users",  "next_question_id": "QUESTION_3_COUNTRY"},
            {"answer_id": "ANSWER_USERS_2_5",     "value": "2_to_5_users",   "next_question_id": "QUESTION_3_COUNTRY"},
        ],
    },
    {
        "question_id": "QUESTION_3_COUNTRY",
        "options_from": "asset",
        "order": 2,
        "user_specific": False,
        "answers": [
            {
                "answer_id": "ANSWER_COUNTRY_SELECT",
                "value": "country",
                "next_question_id": "QUESTION_4_HEALTHCARE_WORKER",
                "content": "e04269d595534762a1ff71262d296701",
            },
        ],
    },
    {
        "question_id": "QUESTION_4_HEALTHCARE_WORKER",
        "options_from": "neo4j",
        "order": 3,
        "user_specific": True,
        "answers": [
            {"answer_id": "ANSWER_HEALTHCARE_YES", "value": "yes", "next_question_id": "QUESTION_5_PROFESSION"},
            {"answer_id": "ANSWER_HEALTHCARE_NO",  "value": "no",  "next_question_id": None},
        ],
    },
    {
        "question_id": "QUESTION_5_PROFESSION",
        "options_from": "neo4j",
        "order": 4,
        "user_specific": True,
        "answers": [
            {"answer_id": "ANSWER_PROFESSION_STUDENT",   "value": "student",                       "next_question_id": "QUESTION_6_EXPERIENCE"},
            {"answer_id": "ANSWER_PROFESSION_SBA",       "value": "other_skilled_birth_attendant",  "next_question_id": "QUESTION_6_EXPERIENCE"},
            {"answer_id": "ANSWER_PROFESSION_NURSE",     "value": "nurse",                          "next_question_id": "QUESTION_6_EXPERIENCE"},
            {"answer_id": "ANSWER_PROFESSION_MIDWIFE",   "value": "midwife",                        "next_question_id": "QUESTION_6_EXPERIENCE"},
            {"answer_id": "ANSWER_PROFESSION_PHYSICIAN", "value": "physician",                      "next_question_id": "QUESTION_6_EXPERIENCE"},
        ],
    },
    {
        "question_id": "QUESTION_6_EXPERIENCE",
        "options_from": "neo4j",
        "order": 5,
        "user_specific": True,
        "answers": [
            {"answer_id": "ANSWER_EXP_11",      "value": "11_plus_years",    "next_question_id": "QUESTION_7_WORKPLACE"},
            {"answer_id": "ANSWER_EXP_6_10",    "value": "6_to_10_years",    "next_question_id": "QUESTION_7_WORKPLACE"},
            {"answer_id": "ANSWER_EXP_1_5",     "value": "1_to_5_years",     "next_question_id": "QUESTION_7_WORKPLACE"},
            {"answer_id": "ANSWER_EXP_LT1",     "value": "less_than_1_year", "next_question_id": "QUESTION_7_WORKPLACE"},
            {"answer_id": "ANSWER_EXP_STUDENT", "value": "student",          "next_question_id": "QUESTION_7_WORKPLACE"},
        ],
    },
    {
        "question_id": "QUESTION_7_WORKPLACE",
        "options_from": "neo4j",
        "order": 6,
        "user_specific": True,
        "answers": [
            {"answer_id": "ANSWER_WORK_OTHER",      "value": "other",                     "next_question_id": "QUESTION_8_SOURCE"},
            {"answer_id": "ANSWER_WORK_UNIVERSITY",  "value": "college_university",        "next_question_id": "QUESTION_8_SOURCE"},
            {"answer_id": "ANSWER_WORK_PRIMARY",     "value": "primary_health_facility",   "next_question_id": "QUESTION_8_SOURCE"},
            {"answer_id": "ANSWER_WORK_SECONDARY",   "value": "secondary_health_facility", "next_question_id": "QUESTION_8_SOURCE"},
            {"answer_id": "ANSWER_WORK_TERTIARY",    "value": "tertiary_hospital",         "next_question_id": "QUESTION_8_SOURCE"},
        ],
    },
    {
        "question_id": "QUESTION_8_SOURCE",
        "options_from": "neo4j",
        "order": 7,
        "user_specific": True,
        "answers": [
            {"answer_id": "ANSWER_SOURCE_PROGRAM",    "value": "program",              "next_question_id": None},
            {"answer_id": "ANSWER_SOURCE_OTHER",       "value": "other",                "next_question_id": None},
            {"answer_id": "ANSWER_SOURCE_EVENT",       "value": "conference_event",      "next_question_id": None},
            {"answer_id": "ANSWER_SOURCE_COLLEAGUE",   "value": "colleague_employer",    "next_question_id": None},
            {"answer_id": "ANSWER_SOURCE_STANDALONE",  "value": "standalone_training",   "next_question_id": None},
            {"answer_id": "ANSWER_SOURCE_INSERVICE",   "value": "in_service_training",   "next_question_id": None},
            {"answer_id": "ANSWER_SOURCE_PRESERVICE",  "value": "pre_service_education", "next_question_id": None},
        ],
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _download_bundle(cosmos_lang_id: str) -> dict:
    """Download content-bundle.json from Azure Blob Storage for a given language."""
    url = f"{BLOB_BASE}/{cosmos_lang_id}/content-bundle.json"
    print(f"  ⬇  Downloading bundle: {url}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _load_bundle_from_file(path: str) -> dict:
    """Load a content-bundle.json from a local file path (fallback)."""
    print(f"  📖 Loading bundle from file: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_bundle_question(bundle_onboarding: list, partial_key: str) -> Optional[dict]:
    """Find a question in the content-bundle array by its partial id string."""
    for q in bundle_onboarding:
        if partial_key in q.get("id", ""):
            return q
    return None


def _inject_labels(graph: list, bundle_onboarding: list) -> list:
    """Walk the graph template and inject question/answer labels from the bundle."""
    for node in graph:
        q_id = node["question_id"]
        partial_key = QUESTION_BUNDLE_KEY_MAP.get(q_id)
        if not partial_key:
            print(f"  ⚠️  No bundle mapping for {q_id}")
            continue

        bundle_q = _find_bundle_question(bundle_onboarding, partial_key)
        if not bundle_q:
            print(f"  ⚠️  Bundle question matching '{partial_key}' not found")
            continue

        # Set question label
        node["label"] = bundle_q.get("question", "")

        # Question 3 has no answers in the bundle; keep English fallback
        if q_id == "QUESTION_3_COUNTRY":
            for a in node["answers"]:
                if "label" not in a:
                    a["label"] = "Select country"
            continue

        bundle_answers = bundle_q.get("answers", [])
        answer_map = ANSWER_INDEX_MAP.get(q_id, {})

        for a in node["answers"]:
            a_id = a["answer_id"]
            idx = answer_map.get(a_id)
            if idx is not None and idx < len(bundle_answers):
                a["label"] = bundle_answers[idx]
            else:
                print(f"  ⚠️  Could not map {a_id} (idx={idx}, bundle has {len(bundle_answers)} answers)")
                a["label"] = a.get("label", f"[MISSING: {a_id}]")

    return graph


def _get_auth_headers() -> dict:
    """Build auth headers from environment or .env file."""
    token = os.environ.get("JWT_TOKEN") or os.environ.get("AUTH_TOKEN")
    internal_token = os.environ.get("INTERNAL_SECURITY_ACCESS_TOKEN")

    # Fallback: manually parse .env (handles Windows BOM/encoding issues)
    if (not token or not internal_token) and os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("JWT_TOKEN=") or line.startswith("AUTH_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("INTERNAL_SECURITY_ACCESS_TOKEN="):
                    internal_token = line.split("=", 1)[1].strip().strip('"').strip("'")

    headers = {"accept": "application/json", "origin": "migration-script"}
    if internal_token:
        headers["X-Internal-Sec-Access"] = internal_token
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ── Mapping file I/O ──────────────────────────────────────────────────────────

ONBOARDING_MAPPING_FILE = "onboarding_mapping.csv"


def _save_flow_id(flow_id: str, version_id: str) -> None:
    """Save the onboarding_flow_id to a CSV mapping file for Stage 2."""
    path = get_mappings_file(ONBOARDING_MAPPING_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["onboarding_flow_id", "version_id", "slug"])
        writer.writeheader()
        writer.writerow({
            "onboarding_flow_id": flow_id,
            "version_id": version_id,
            "slug": "primary",
        })
    print(f"  💾 Saved onboarding flow mapping → {path}")


def _load_flow_id() -> Optional[str]:
    """Load the onboarding_flow_id from the mapping file."""
    path = get_mappings_file(ONBOARDING_MAPPING_FILE)
    if not path.exists():
        print(f"  ⚠️  Mapping file not found: {path}")
        return None
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fid = row.get("onboarding_flow_id", "").strip()
            if fid:
                return fid
    return None


# ── Core class ────────────────────────────────────────────────────────────────

class OnboardingMigrator:
    """Handles onboarding flow migration for the pipeline.

    Can be used standalone or imported by global_migrator / main.py.
    """

    def __init__(self, api_base_url: str = None):
        from configs import LME_BASE_URL
        self.api_url = f"{api_base_url or LME_BASE_URL}/onboarding-flows"
        self.flow_id = None  # Populated dynamically

    # ── Bundle loading ────────────────────────────────────────────────────

    def _get_bundle(self, cosmos_lang_id: str, local_path: str = None) -> dict:
        """Get the content-bundle, downloading from Azure or reading a local file."""
        if local_path:
            return _load_bundle_from_file(local_path)
        return _download_bundle(cosmos_lang_id)

    def _extract_onboarding(self, bundle: dict) -> list:
        """Extract and validate the onboarding array from a content-bundle."""
        onboarding = bundle.get("onboarding", [])
        if not onboarding:
            raise ValueError("No 'onboarding' key found in bundle!")
        print(f"  Found {len(onboarding)} questions in bundle")
        return onboarding

    # ── Payload builders ──────────────────────────────────────────────────

    def _build_global_payload(self, onboarding: list) -> dict:
        """Build the POST payload for the global (original) onboarding flow."""
        graph = copy.deepcopy(GRAPH_TEMPLATE)
        graph = _inject_labels(graph, onboarding)
        return {
            "data": {
                "updated_by": "System",
                "questions": graph,
            },
            "language_id": None,
            "region": "GLOBAL",
            "content_type": "original",
            "created_by": "System",
        }

    def _build_translated_payload(self, onboarding: list, lme_language_id: str) -> dict:
        """Build the PATCH payload for a translated onboarding flow."""
        graph = copy.deepcopy(GRAPH_TEMPLATE)
        graph = _inject_labels(graph, onboarding)
        return {
            "data": {
                "updated_by": "System",
                "questions": graph,
            },
            "language_id": lme_language_id,
            "region": "GLOBAL",
            "content_type": "translated",
            "updated_by": "System",
        }

    # ── API calls ─────────────────────────────────────────────────────────

    def _post_global(self, payload: dict) -> Optional[str]:
        """POST the global original version and return the new flow_id."""
        url = f"{self.api_url}/"
        headers = _get_auth_headers()
        print(f"\n🚀  POSTing global original to {url}")

        if "Authorization" not in headers and "X-Internal-Sec-Access" not in headers:
            print("  ⚠️  WARNING: No auth tokens found. Request will likely fail with 401.")
        else:
            print("  ✅  Auth token found.")

        resp = requests.post(url, json=payload, headers=headers, allow_redirects=False)
        print(f"  Response: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            flow_id = data.get("onboarding_flow_id")
            version_id = ""
            versions = data.get("versions", [])
            if versions:
                version_id = versions[0].get("onboarding_flow_version_id", "")
            print(f"  ✅  Created onboarding flow: {flow_id}")
            return flow_id, version_id
        else:
            try:
                print(json.dumps(resp.json(), indent=2, ensure_ascii=False)[:500])
            except Exception:
                from error_logger import log_error
                log_error("Captured Exception")
                print(resp.text[:500])
            return None, None

    def _patch_translated(self, flow_id: str, payload: dict) -> bool:
        """PATCH the translated version onto the existing flow."""
        url = f"{self.api_url}/{flow_id}"
        headers = _get_auth_headers()
        print(f"\n🚀  PATCHing translated version to {url}")

        if "Authorization" not in headers and "X-Internal-Sec-Access" not in headers:
            print("  ⚠️  WARNING: No auth tokens found. Request will likely fail with 401.")
        else:
            print("  ✅  Auth token found.")

        resp = requests.patch(url, json=payload, headers=headers, allow_redirects=False)
        print(f"  Response: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            print(f"  ✅  Translated version patched successfully.")
            return True
        else:
            try:
                print(json.dumps(resp.json(), indent=2, ensure_ascii=False)[:500])
            except Exception:
                from error_logger import log_error
                log_error("Captured Exception")
                print(resp.text[:500])
            return False

    # ── High-level pipeline methods ───────────────────────────────────────

    def migrate_global(self, local_bundle_path: str = None) -> Optional[str]:
        """Stage 1 Global: Download English bundle, POST global flow, save mapping.

        Args:
            local_bundle_path: Optional local file path (for testing/override).

        Returns:
            The new onboarding_flow_id, or None on failure.
        """
        print("\n=== Onboarding: Migrating Global (Original) ===")
        bundle = self._get_bundle(ENGLISH_COSMOS_LANG_ID, local_bundle_path)
        onboarding = self._extract_onboarding(bundle)
        payload = self._build_global_payload(onboarding)

        flow_id, version_id = self._post_global(payload)
        if flow_id:
            self.flow_id = flow_id
            _save_flow_id(flow_id, version_id or "")
            return flow_id
        else:
            print("  ❌ Failed to create global onboarding flow.")
            return None

    def migrate_translated(
        self,
        cosmos_lang_id: str,
        lme_language_id: str,
        flow_id: str = None,
        local_bundle_path: str = None,
    ) -> bool:
        """Stage 2 Translated: Download translated bundle, PATCH onto existing flow.

        Args:
            cosmos_lang_id: Cosmos language key (e.g. "mr", "hi", "fr").
            lme_language_id: LME UUID for the language.
            flow_id: Onboarding flow ID to patch. If None, reads from mapping file.
            local_bundle_path: Optional local file path (for testing/override).

        Returns:
            True on success, False on failure.
        """
        # Resolve flow_id
        target_flow_id = flow_id or self.flow_id or _load_flow_id()
        if not target_flow_id:
            print(f"  ❌ No onboarding_flow_id available. Run global migration first.")
            return False

        print(f"\n=== Onboarding: Migrating Translated ({cosmos_lang_id}) ===")
        print(f"  Flow ID: {target_flow_id}")
        print(f"  LME Language ID: {lme_language_id}")

        bundle = self._get_bundle(cosmos_lang_id, local_bundle_path)
        onboarding = self._extract_onboarding(bundle)
        payload = self._build_translated_payload(onboarding, lme_language_id)

        return self._patch_translated(target_flow_id, payload)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Migrate onboarding flows from content-bundle.json to LME API"
    )
    parser.add_argument(
        "--mode", required=True, choices=["global", "translated"],
        help="'global' = POST original English, 'translated' = PATCH a language version"
    )
    parser.add_argument(
        "--cosmos-lang-id", default=None,
        help="Cosmos language ID to download bundle for (e.g. 'mr' for Marathi)"
    )
    parser.add_argument(
        "--lme-lang-id", default=None,
        help="LME Language UUID (required for --mode translated)"
    )
    parser.add_argument(
        "--bundle", default=None,
        help="Optional: local path to a content-bundle.json (overrides Azure download)"
    )
    parser.add_argument(
        "--flow-id", default=None,
        help="Optional: onboarding_flow_id to patch (overrides mapping file lookup)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print payload without sending to API"
    )
    args = parser.parse_args()

    migrator = OnboardingMigrator()

    if args.mode == "global":
        if args.dry_run:
            bundle = migrator._get_bundle(ENGLISH_COSMOS_LANG_ID, args.bundle)
            onboarding = migrator._extract_onboarding(bundle)
            payload = migrator._build_global_payload(onboarding)
            print("\n─── DRY RUN PAYLOAD ───")
            out = json.dumps(payload, indent=2, ensure_ascii=False)
            sys.stdout.buffer.write(out.encode("utf-8"))
            sys.stdout.buffer.write(b"\n")
            print("─── END PAYLOAD ───")
        else:
            migrator.migrate_global(local_bundle_path=args.bundle)

    elif args.mode == "translated":
        if not args.lme_lang_id:
            parser.error("--lme-lang-id is required for --mode translated")
        cosmos_id = args.cosmos_lang_id
        if not cosmos_id and not args.bundle:
            parser.error("--cosmos-lang-id or --bundle is required for --mode translated")

        if args.dry_run:
            bundle = migrator._get_bundle(cosmos_id, args.bundle)
            onboarding = migrator._extract_onboarding(bundle)
            payload = migrator._build_translated_payload(onboarding, args.lme_lang_id)
            print("\n─── DRY RUN PAYLOAD ───")
            out = json.dumps(payload, indent=2, ensure_ascii=False)
            sys.stdout.buffer.write(out.encode("utf-8"))
            sys.stdout.buffer.write(b"\n")
            print("─── END PAYLOAD ───")
        else:
            migrator.migrate_translated(
                cosmos_lang_id=cosmos_id,
                lme_language_id=args.lme_lang_id,
                flow_id=args.flow_id,
                local_bundle_path=args.bundle,
            )


if __name__ == "__main__":
    main()

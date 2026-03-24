"""Language migration functionality for Cosmos DB to LME migration."""

import csv
from typing import Any, Dict, List

import requests
from azure.cosmos import CosmosClient

from configs import POST_LANGUAGE, JWT_TOKEN
from factories import DataFactory
from path_utils import ensure_parent_dir, get_mappings_file, get_processed_file
import time


class LanguageMigrator:
    """Handles migration of languages from Cosmos DB to LME."""

    def __init__(
        self,
        cosmos_client: CosmosClient,
        container,
        auto_post: bool = False,
    ):
        self.cosmos_client = cosmos_client
        self.container = container
        # cosmos_id -> {"lme_language_id": str, "region": str}
        self.language_mapping: Dict[str, Dict[str, str]] = {}
        self.auto_post = auto_post
        languages_path = get_processed_file("languages.csv")
        ensure_parent_dir(languages_path)
        self._languages_path = languages_path
        self.languages_csv = str(languages_path)

    def migrate_all_languages(self, language_id: str = None) -> None:
        """Migrate languages from Cosmos DB to LME.
        
        Args:
            language_id: Optional Cosmos ID to filter migration to a single language.
        """
        print("Starting language migration...")

        # Get languages from Cosmos DB
        languages = self._get_all_languages(language_id)

        if not languages:
            print("No languages found in Cosmos DB")
            return

        print(f"Found {len(languages)} languages to migrate")

        # Migrate each language
        for language_doc in languages:
            try:
                self._migrate_single_language(language_doc)
            except Exception as e:
                desc = language_doc.get("description")
                print(f"Error migrating language {desc}: {e}")
                continue

        # Note: Mapping is persisted incrementally on creation;
        # no overwrite here

        print("Language migration completed!")

    def _get_all_languages(self, language_id: str = None) -> List[Dict]:
        """Retrieve language documents from Cosmos DB, optionally filtered."""
        query = "SELECT * FROM c WHERE c._table='languages'"
        if language_id:
            query += f" AND c.id='{language_id}'"
        languages = list(
            self.container.query_items(query=query, enable_cross_partition_query=True)
        )
        return languages

    def _migrate_single_language(self, language_doc: Dict) -> None:
        """Migrate a single language document."""
        description = language_doc.get("description", "Unknown")
        cosmos_id = language_doc.get("id")

        print(f"Migrating language: {description} (ID: {cosmos_id})")

        # Create language data using factory
        language_data = DataFactory.create_language_data(language_doc)

        # Persist payload for deferred processing
        self._write_language_payload(cosmos_id, language_data)
        print(f"Queued language payload -> {self._languages_path}")

        if self.auto_post:
            self._post_language_payload(
                cosmos_id, language_data.to_dict(), language_data.region
            )


    def _append_language_mapping(
        self, cosmos_id: str, lme_language_id: str, region: str
    ) -> None:
        """Append a single language mapping row to CSV (no overwrite)."""
        output_path = get_mappings_file("language_mapping.csv")
        ensure_parent_dir(output_path)
        need_header = not output_path.exists() or output_path.stat().st_size == 0
        with output_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if need_header:
                writer.writerow(["cosmos_id", "lme_language_id", "region"])
            writer.writerow([cosmos_id, lme_language_id, region])

    def load_language_mapping(self) -> None:
        """Load existing language mapping from CSV."""
        mapping_path = get_mappings_file("language_mapping.csv")

        if not mapping_path.exists():
            print("No existing language mapping found, starting fresh")
            return

        try:
            with mapping_path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self.language_mapping[row["cosmos_id"]] = {
                        "lme_language_id": row["lme_language_id"],
                        "region": row["region"],
                    }
            # Enrich with language_name from languages.csv
            if self._languages_path.exists():
                try:
                    with self._languages_path.open("r", encoding="utf-8") as lf:
                        lang_reader = csv.DictReader(lf)
                        for row in lang_reader:
                            cid = (row.get("cosmos_id") or "").strip()
                            lang_name = (row.get("language_name") or "").strip()
                            if cid and lang_name and cid in self.language_mapping:
                                self.language_mapping[cid]["name"] = lang_name
                except Exception:
                    pass
            mapping_count = len(self.language_mapping)
            print(f"Loaded {mapping_count} existing language mappings")
        except Exception as e:
            print(f"Warning: Could not load language mapping: {e}")

    def get_language_mapping(self) -> Dict[str, Dict[str, str]]:
        """Get the current language mapping."""
        return self.language_mapping

    def load_processed_languages_mapping(self) -> Dict[str, Dict[str, str]]:
        """Load cosmos_id -> (lme_language_id, region) from processed languages.csv.

        Used during Stage 1 when languages have not been posted yet, so
        lme_language_id may be blank but region is still required for
        downstream resource/module queuing.
        """

        if self.language_mapping:
            return self.language_mapping

        if not self._languages_path.exists():
            print("Warning: processed languages.csv not found; using default regions")
            return self.language_mapping

        try:
            with self._languages_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cosmos_id = (row.get("cosmos_id") or "").strip()
                    if not cosmos_id:
                        continue
                    self.language_mapping[cosmos_id] = {
                        "lme_language_id": row.get("lme_language_id", "").strip(),
                        "region": (row.get("region") or "africa").strip(),
                        "name": (row.get("language_name") or "").strip(),
                    }
        except Exception as exc:
            print(f"Warning: could not preload processed languages mapping: {exc}")

        return self.language_mapping

    def _write_language_payload(
        self,
        cosmos_id: str,
        language_data,
    ) -> None:
        """Append language payload to processed_data CSV for later posting."""

        ensure_parent_dir(self._languages_path)
        need_header = (
            not self._languages_path.exists()
            or self._languages_path.stat().st_size == 0
        )
        fieldnames = [
            "cosmos_id",
            "language_name",
            "autonym_script",
            "learning_platform",
            "country_code",
            "country",
            "region",
            "image_prefix",
            "video_prefix",
            "latitude",
            "longitude",
            "created_by",
            "icon",
        ]

        with self._languages_path.open(
            "a",
            newline="",
            encoding="utf-8",
        ) as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            if need_header:
                writer.writeheader()

            payload = {"cosmos_id": cosmos_id}
            payload.update(language_data.to_dict())
            # Explicitly add internal fields needed for metadata but excluded from API payload
            payload["image_prefix"] = language_data.image_prefix
            payload["video_prefix"] = language_data.video_prefix
            
            # Remove 'categories' from payload if it exists, as it's not in CSV schema
            payload.pop("categories", None)
            writer.writerow(payload)

    def post_languages_from_csv(self, skip_existing: bool = True) -> None:
        """Read processed language payloads and POST them to the API."""

        if not self._languages_path.exists():
            print("No processed language data found to post.")
            return

        try:
            rows = self._read_processed_languages()
        except UnicodeDecodeError as exc:
            print(
                f"Failed to decode {self._languages_path} using the "
                "supported fallbacks (utf-8, utf-8-sig, latin-1)."
            )
            raise exc

        for row in rows:
            cosmos_id = row.get("cosmos_id") or ""
            if skip_existing and cosmos_id in self.language_mapping:
                print("Skipping cosmos_id %s; mapping already exists" % cosmos_id)
                continue

            payload = self._coerce_language_payload(row)
            region = payload.get("region", "africa")

            try:
                self._post_language_payload(cosmos_id, payload, region)
            except requests.exceptions.RequestException as exc:
                name = payload.get("language_name", cosmos_id)
                print(f"Failed to post language {name}: {exc}")

    def _coerce_language_payload(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert CSV row values to appropriate types for the API."""

        def _coerce_bool(value: str) -> bool:
            if value is None:
                return True
            val = value.strip().lower()
            return val in {"true", "1", "yes", "y"}

        def _coerce_float(value: str) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        payload = {
            "language_name": (row.get("language_name") or "").strip(),
            "autonym_script": (row.get("autonym_script") or "").strip(),
            "learning_platform": _coerce_bool(row.get("learning_platform")),
            "country_code": (row.get("country_code") or "").strip(),
            "country": (row.get("country") or "").strip(),
            "region": (row.get("region") or "africa").strip(),
            "latitude": _coerce_float(row.get("latitude")),
            "longitude": _coerce_float(row.get("longitude")),
            "created_by": (row.get("created_by") or "System").strip(),
            "icon": row.get("icon") or None,
        }

        return payload

    def _post_language_payload(
        self,
        cosmos_id: str,
        payload: Dict[str, Any],
        region: str,
    ) -> None:
        """POST a single language payload and update mapping."""

        headers = {"Content-Type": "application/json"}
        if JWT_TOKEN:
            headers["Authorization"] = f"Bearer {JWT_TOKEN}"

        response = self._request_with_retry(
            method="POST",
            url=POST_LANGUAGE,
            json=payload,
            headers=headers,
            entity_desc=f"language {payload.get('language_name', cosmos_id)}",
        )
        if not response:
            return
        try:
            api_response = response.json() if response.content else {}
        except ValueError:
            api_response = {}

        if api_response.get("language_id"):
            lme_id = api_response["language_id"]
            self.language_mapping[cosmos_id] = {
                "lme_language_id": lme_id,
                "region": region,
            }
            self._append_language_mapping(cosmos_id, lme_id, region)

        posted_name = payload.get("language_name", cosmos_id)
        print(f"Successfully posted language: {posted_name}")

    def _read_processed_languages(self) -> List[Dict[str, str]]:
        """Read processed language rows with fallback encodings."""

        encodings = ("utf-8", "utf-8-sig", "latin-1")
        for encoding in encodings:
            try:
                with self._languages_path.open(
                    "r",
                    encoding=encoding,
                ) as csv_file:
                    return list(csv.DictReader(csv_file))
            except UnicodeDecodeError:
                continue

        # If no encoding succeeded, raise to caller for logging/handling
        message = "Unable to decode processed languages CSV with " "available encodings"
        raise UnicodeDecodeError("codec", b"", 0, 0, message)

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
        """Perform an HTTP request with exponential backoff.

        Returns response or None if all retries fail.
        """
        for attempt in range(1, attempts + 1):
            try:
                if method.upper() == "POST":
                    resp = requests.post(url, json=json, headers=headers)
                else:
                    resp = requests.request(method.upper(), url, json=json, headers=headers)
                resp.raise_for_status()
                return resp
            except requests.exceptions.RequestException as exc:
                if attempt == attempts:
                    print(f"❌ Failed to {method} {entity_desc}: {exc}")
                    if getattr(exc, "response", None) is not None:
                        print(f"Response Error Body: {exc.response.text}")
                    return None
                delay = base_delay * (2 ** (attempt - 1))
                print(
                    f"Retry {attempt}/{attempts} for {entity_desc} after error: {exc}. Waiting {delay:.1f}s"
                )
                if getattr(exc, "response", None) is not None and attempt == 1:
                     print(f"Response Error Body (Attempt 1): {exc.response.text}")
                time.sleep(delay)

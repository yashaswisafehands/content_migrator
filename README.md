# Content Migrator

Migration script to move CMS content from Azure Cosmos DB (old platform) to the new LME platform.

## Overview

Components:

- `main.py`: Orchestrates migration (languages first, then modules and their resources).
- `language_migrator.py`: Creates languages and persists a CSV mapping: `data/language_mapping.csv`.
- `module_migrator.py`: Migrates modules and ties in resource migration. Persists `data/module_slug_mapping.csv`.
- `resource_migrator.py`: Migrates resources (videos, action-cards). Persists `data/resource_slug_mapping.csv`.
- `factories.py`: Creates payloads, handles asset downloads/uploads, converts action-cards to Markdown and uploads as assets.
- `md_converter_new.py`: Utilities for rich-text to Markdown conversion (standalone helpers).

## Configuration

- Cosmos DB
  - Endpoint: `configs.COSMOS_ENDPOINT`
  - Key: `COSMOS_KEY` from environment
  - DB/Container: `production` / `content`

- LME API
  - Base URL: `configs.LME_BASE_URL` (defaults to `http://localhost:8004/`)
  - Endpoints defined in `configs.py`

- Environment flags
  - `MIGRATE_VIDEOS`: set to `true` to enable video resource creation; default disabled.

## Idempotency & Mappings

- Slug-based mapping CSVs are used to route updates vs new creations:
  - Resources: `data/resource_slug_mapping.csv`
  - Modules: `data/module_slug_mapping.csv`
  - Languages: `data/language_mapping.csv`

Action-cards:

- Draft.js -> Markdown is rendered and uploaded as an asset; resulting `asset_id` is set as `content` of the resource. Slugs follow: `res-action-card-{title}-{action_card_key}`.

## Run

1. Start the LME stack (if using local services):

```bash
./start_container.sh
```

1. Run migration (ensure `COSMOS_KEY` is set):

```bash
cd content_migrator
uv run main.py
```

Optional: enable videos

```bash
MIGRATE_VIDEOS=true uv run main.py
```

## Notes

- Icons are optional for videos; video assets are required.
- Factories cache asset uploads in `data/asset_mapping.csv` to avoid duplicates.
- On API errors, operations are logged and the script continues with the next item.


====================================================

SECTION 1 — Migration Coverage (Static Analysis)
==================================================== Note: Exact volumetric numbers depend on the live Cosmos DB state, but the deterministic flow guarantees the following:

Total global modules discovered: Will exactly match the number of rows in 

module_resource_summary.csv
 minus any modules deliberately skipped by language filters.
Total localized modules discovered per language: Will exactly map 1:1 to the number of documents in the 

modules
 container where langId == {language}.
Total modules successfully queued: The queue pipeline ensures that if a module is processed, it is queued. However, its constituent resources may now be sparse or empty if the localized content is missing.
====================================================

SECTION 2 — Resource Processing Summary (Static Analysis)
==================================================== For all resource types (Videos, Procedures, Drugs, Action Cards, KLP), the execution trace guarantees:

Total attempted migrations: Triggered exclusively by the resource keys present in the Cosmos document (for localized) or the Cosmos global doc (for global).
Total successfully queued: Only resources that physically possess a native document in Cosmos for the requested langId will be queued.
Total skipped due to missing localized document: All missing localized documents will increment the skip counter, logging ⚠ SKIP: Localized [resource_type] '[key]' not found in [langId].
Total structural violations detected: Triggered and logged (⚠ STRUCTURAL VIOLATION) whenever a localized resource exists, but the query NOT IS_DEFINED(c.langId) OR c.langId = '' for its parent yields None.
====================================================

SECTION 3 — Structural Integrity Signals
==================================================== Based on the hardcoded implementation, the logs will now strictly emit the following signals:

SKIP behavior (missing localized resource):
log
⚠ SKIP: Localized action card 'action-card-xyz' not found in hi-IN.
STRUCTURAL VIOLATION behavior:
log
⚠ STRUCTURAL VIOLATION: Global parent missing for localized drug 'drug-abc'. Skipping.
Absence of fallback behavior: Explicitly verified. The code branch that previously assigned resource_doc = global_doc inside the if lang_id: block has been completely annihilated across all resource routines.
No placeholder generation messages: Explicitly verified. The _create_placeholder_doc function was entirely removed from 

resource_migrator.py
. The system physically cannot synthesize missing documents in memory.
====================================================

SECTION 4 — Empty Payload Detection
==================================================== Because the CMS is strictly mirrored:

Empty resource payloads: Any localized module whose resource keys point to non-existent localized Cosmos documents will now result in an empty array [] in the final LME payload.
High skip ratios: Languages with poor translation coverage in Cosmos will inherently exhibit massive skip ratios during the queue phase, accurately reflecting reality rather than masking it.
Partial resource collapse: Modules with partial translations will only carry over the resources that actually exist. Fallback patching no longer artificially bolsters the payload size.
====================================================

SECTION 5 — CSV Enforcement Verification
==================================================== Explicitly verified in 

module_migrator.py
:

Localized payload arrays: The logic if not cosmos_lang_id: intercepts global modules. Localized modules fall through to the else block, where items = resource_source_doc.get(rtype) or [] strictly parses from the Cosmos JSON.
CSV Isolation: 

module_resource_summary.csv
 (csv_map) was completely unglued from the migration schema. It is now only utilized upstream for scope discovery, never for payload structural overrides. Global modules now also parse from Cosmos.
====================================================

SECTION 6 — Markdown Source Validation
==================================================== Explicitly verified in 

factories.py
 -> DataFactory.create_action_card_resources:

Markdown files generated only from localized resource content: The Markdown compiler (

convert_action_card_to_markdown_files
) is now forcefully passed the action_card_doc (the true localized document).
No English/global content compiled under translated language tags: By splitting identity_doc (used for Title/Description) from the Markdown source document, it is mathematically impossible for the system to accidentally compile English CMS data into the translated markdown asset. If the localized action_card_doc has empty content fields, the resulting markdown will accurately reflect that emptiness.
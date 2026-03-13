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

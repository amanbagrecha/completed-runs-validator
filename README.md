# Completed Runs Validator

Minimal local validation app for processed ETL run tar files in S3.

## Setup

```bash
uv sync
uv run python scripts/sync_runs.py
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` or the forwarded port URL.

## Behavior

- Uses Google Sheet `done_runs` as the run list.
- Indexes S3 tar files from `panoramic_clean/`, `batch2/`, and `batch-03/` through `batch-10/`.
- Does not rely on `tar_image_counts.csv`.
- Only extracts images for the run you open.
- Caches selected images under `data/cache/images/` as JPEG quality `75`.
- Reuses cached images on later views.
- Stores run metadata, selected images, and validations in `data/app.db`.

## Local Files

- `data/app.db`: SQLite database.
- `data/cache/images/`: compressed local image cache.

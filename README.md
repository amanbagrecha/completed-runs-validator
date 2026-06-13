# Completed Runs Validator

Minimal local validation app for processed ETL run tar files in S3.

## Setup

```bash
uv sync
uv run python scripts/sync_runs.py
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` or the forwarded port URL.

For a validator workstation, copy the Google service-account file to the repo
root as `vast-sheet-sync-32aedaa23a0f.json`. The file is intentionally ignored
by git. The service account must have edit access to the Google Sheet.

With OpenCode installed, restart OpenCode after cloning this repo and run:

```text
/start-validator
```

That command runs `bash scripts/start_validator_tmux.sh`, starts the server in
tmux, and prints the `/review` links.

## Behavior

- Uses the configured Google Sheet CSV export as the run list.
- Indexes S3 tar files from `panoramic_clean/`, `batch2/`, and `batch-03/` through `batch-10/`.
- Does not rely on `tar_image_counts.csv`.
- Only extracts images for the run you open.
- Caches selected images under `data/cache/images/` as JPEG quality `75`.
- Reuses cached images on later views.
- Stores run metadata, selected images, and validations in `data/app.db`.
- Writes completed run results back to app-owned Google Sheet columns named
  `compltd_*`; it does not modify the existing `validation` column.
- During sync and review, runs already marked `approved`/`retry` in the existing
  `validation` column, or `completed` in `compltd_status`, are skipped unless
  they were completed locally on this machine.

## Local Files

- `data/app.db`: SQLite database.
- `data/cache/images/`: compressed local image cache.

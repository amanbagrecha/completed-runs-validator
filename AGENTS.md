# Completed Runs Validator — Agent Guide

## Project Purpose

A local FastAPI web app for manually validating ETL pipeline runs. It cross-references a Google Sheet (list of completed runs) against S3-compatible buckets (`.tar` archives of panoramic images), then lets a human reviewer browse and pass/fail sample images from each run.

---

## Architecture Overview

```
app/
  main.py          # FastAPI app factory, mounts static files, initializes both datasets on startup
  config.py        # Shared paths plus per-dataset config for DB/cache/S3/batches
  db.py            # SQLite connection helper (context manager), init_db(db_path)
  routes.py        # Router factory mounted twice: Wasabi page + AWS legacy page
  schema.sql       # SQLite schema (CREATE TABLE IF NOT EXISTS — idempotent)
  services/
    sheets.py      # Fetches run list from Google Sheets CSV export
    s3_index.py    # Lists .tar objects in S3, parses run_id from filename
    sync.py        # Joins sheet + S3 data, upserts runs table, records sync
    image_cache.py # Streams tar from S3, samples images, resizes + caches as JPEG
    validations.py # Upserts image_validations rows (pass/fail)
  templates/
    index.html     # Single Jinja2 template; JS-driven UI
  static/
    app.js         # Frontend logic (fetch API calls, UI state)
    app.css        # Styles
data/
  app.db           # Wasabi dataset SQLite database
  aws_app.db       # AWS legacy dataset SQLite database
  cache/images/    # Wasabi cached JPEG thumbnails (run_id/vN/N.jpg)
  cache/aws-images/# AWS cached JPEG thumbnails (run_id/vN/N.jpg)
  server.log       # Uvicorn output
scripts/
  sync_runs.py           # Standalone sync script (`--dataset wasabi|aws`)
  generate_manifests.py  # Backfills missing `.manifest.json` files into AWS bucket
```

---

## Data Model

| Table | Purpose |
|---|---|
| `runs` | One row per ETL run; joined from sheet + S3 |
| `run_images` | Selected sample images for a run (up to `image_target_count`) |
| `image_validations` | One pass/fail decision per `run_image`; upsert on conflict |
| `sync_runs` | Log of each sync operation with counts |

**Run status** is computed at query time (not stored):
- `pending` — no images validated yet
- `partial` — some validated, not all
- `ready` — all active images validated, reviewer has not completed the run yet
- `pass` — reviewer completed the run and no active images failed
- `fail` — reviewer completed the run and at least one active image failed

---

## Datasets

The app now serves two isolated validation surfaces that share the same code path but keep separate databases and image caches.

| Dataset | Page | API Base | DB | Cache | Bucket | Profile |
|---|---|---|---|---|---|---|
| Wasabi Runs | `/` | `/api` | `data/app.db` | `data/cache/images/` | `pano-processed-runs` | `wasabi` |
| AWS Legacy Runs | `/aws` | `/aws/api` | `data/aws_app.db` | `data/cache/aws-images/` | `aipanoexport-batch2` | `s3` |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Wasabi HTML index |
| `GET` | `/aws` | AWS legacy HTML index |
| `POST` | `/api/sync` | Sync Wasabi dataset |
| `POST` | `/aws/api/sync` | Sync AWS legacy dataset |
| `GET` | `/api/runs?batch=&status=&run_id=&limit=&page=` | Paginated Wasabi run list |
| `GET` | `/aws/api/runs?batch=&status=&run_id=&limit=&page=` | Paginated AWS run list |
| `GET` | `/api/runs/{run_id}/images` | Load/cache Wasabi sample images |
| `GET` | `/aws/api/runs/{run_id}/images` | Load/cache AWS sample images |
| `POST` | `/api/runs/{run_id}/refresh-images` | Add more Wasabi images |
| `POST` | `/aws/api/runs/{run_id}/refresh-images` | Add more AWS images |
| `POST` | `/api/runs/{run_id}/complete` | Mark Wasabi run validation complete |
| `POST` | `/aws/api/runs/{run_id}/complete` | Mark AWS run validation complete |
| `GET` | `/api/images/{image_id}/file` | Serve cached Wasabi JPEG |
| `GET` | `/aws/api/images/{image_id}/file` | Serve cached AWS JPEG |
| `POST` | `/api/validations` | Submit Wasabi validations |
| `POST` | `/aws/api/validations` | Submit AWS validations |

---

## Key Configuration (`app/config.py`)

| Setting | Value | Meaning |
|---|---|---|
| `WASABI_DATASET` | `/`, `/api`, `data/app.db`, `data/cache/images/` | Current Wasabi-backed validator |
| `AWS_DATASET` | `/aws`, `/aws/api`, `data/aws_app.db`, `data/cache/aws-images/` | Legacy AWS-backed validator |
| `SHEET_CSV_URL` | Google Sheets export URL | Shared run manifest source |
| `DEFAULT_IMAGE_COUNT` | `6` | Images sampled per run by default |
| `JPEG_QUALITY` | `75` | Compression level for cached thumbnails |

---

## Image Sampling Logic (`services/image_cache.py`)

Images are selected deterministically using a SHA-256 hash seeded with `run_id:selection_version:member_name`. This means the same run always produces the same sample unless `selection_version` is bumped. The tar is streamed (not downloaded in full) using `mode="r|*"`.

A per-run threading `Lock` prevents concurrent requests from double-sampling the same run. Locks and cache directories are separated by dataset slug so `/` and `/aws` do not interfere with each other.

---

## Running the Server

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8123 --reload
```

The server is currently running in tmux session `compltd`. To attach:

```bash
tmux attach -t compltd
```

---

## Common Tasks

**Sync Wasabi runs:**
```
POST /api/sync
```

**Sync AWS legacy runs:**
```
POST /aws/api/sync
```

**View all runs for a Wasabi batch:**
```
GET /api/runs?batch=batch-11&status=all
```

**View all runs for an AWS batch:**
```
GET /aws/api/runs?batch=batch-10&status=all
```

**Backfill missing AWS manifests:**
```
uv run python scripts/generate_manifests.py --workers 1
```

**Attach to the backfill tmux session:**
```
tmux attach -t manifest-backfill
```

**Bump image count for a run on AWS page:**
```
POST /aws/api/runs/{run_id}/refresh-images
```

---

## Dependencies

- `fastapi` + `uvicorn` — web framework
- `jinja2` — HTML templating
- `boto3` — S3 access (requires AWS profiles `s3` and `wasabi`)
- `pillow` — image resizing/compression
- `uv` — package manager / runner

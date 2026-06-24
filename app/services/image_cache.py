from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from typing import TypedDict

from PIL import Image, ImageFile

from app.config import DEFAULT_IMAGE_COUNT, DatasetConfig, JPEG_QUALITY, MAX_CACHED_IMAGE_SIZE, ROOT_DIR
from app.db import get_conn, run_with_retry, write_conn
from app.services.s3_index import get_s3_client


ImageFile.LOAD_TRUNCATED_IMAGES = True

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
REVIEW_IMAGE_FRACTION = 0.10
PREFETCH_MAX_IMAGE_COUNT = 100
PREFETCH_MIN_IMAGE_COUNT = DEFAULT_IMAGE_COUNT * 2
PREFETCH_IMAGE_FRACTION = 0.05
PREFETCH_CHUNK_SIZE = DEFAULT_IMAGE_COUNT * 2
_RUN_LOCKS: dict[str, Lock] = {}
_RUN_LOCKS_LOCK = Lock()
_REVIEW_ENSURE_QUEUE: Queue[tuple[DatasetConfig, str, str]] = Queue()
_REVIEW_ENSURE_KEYS: set[str] = set()
_REVIEW_ENSURE_LOCK = Lock()
_REVIEW_ENSURE_WORKER_STARTED = False
_PREFETCH_QUEUE: Queue[tuple[DatasetConfig, str, str]] = Queue()
_PREFETCH_KEYS: set[str] = set()
_PREFETCH_LOCK = Lock()
_PREFETCH_WORKER_STARTED = False
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Concurrency / locking model
#
# All image caching does slow S3 network I/O and JPEG transcoding with NO SQLite
# connection open, then records the results in a single short ``write_conn``
# (BEGIN IMMEDIATE) transaction. This is what keeps the SQLite write lock from
# being held across the network -- the cause of the "database is locked" 500s
# that used to break draft submission under load.
#
# Per-run work is serialised in-process by ``_run_lock`` so two threads never
# select/cache the same run concurrently (and so a pre-assigned image_index is
# stable from read snapshot through the final write). This assumes a single
# server process; the UNIQUE(run_id, selection_version, image_index) constraint
# plus INSERT ... ON CONFLICT keeps multi-worker deployments from inserting
# duplicates even though the in-process lock would not span processes.
# ----------------------------------------------------------------------------


class ManifestFile(TypedDict):
    name: str
    size: int
    tar_offset: int


# A unit of caching work resolved entirely off the DB: the bytes for one image
# have been fetched + transcoded to ``cache_path`` on disk; the matching DB row
# is identified either by an existing row id (re-cache) or by (image_index,
# member_name) for a freshly selected image (insert).
class _CachedImage(TypedDict):
    image_index: int
    member_name: str
    cache_path: str
    image_id: int | None


def ensure_run_images(dataset: DatasetConfig, run_id: str) -> list[sqlite3.Row]:
    with get_conn(dataset.db_path) as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")
        target_count = int(run["image_target_count"] or DEFAULT_IMAGE_COUNT)
        version = int(run["selection_version"])

    _ensure_images(dataset, run_id, target_count)
    return _read_image_rows(dataset, run_id, version, target_count)


def ensure_run_images_to_count(
    dataset: DatasetConfig,
    run_id: str,
    target_count: int,
) -> list[sqlite3.Row]:
    with get_conn(dataset.db_path) as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")
        version = int(run["selection_version"])

    target_count = max(1, target_count)
    _ensure_images(dataset, run_id, target_count)
    return _read_image_rows(dataset, run_id, version, target_count)


def append_run_images(
    dataset: DatasetConfig,
    run_id: str,
    count: int = DEFAULT_IMAGE_COUNT,
) -> list[sqlite3.Row]:
    with _run_lock(dataset, run_id):
        with write_conn(dataset.db_path) as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                raise ValueError(f"Unknown run_id: {run_id}")
            current_target = int(run["image_target_count"] or DEFAULT_IMAGE_COUNT)
            version = int(run["selection_version"])
            new_target = current_target + count
            if run["total_image_count"] is not None:
                new_target = min(new_target, int(run["total_image_count"]))
            conn.execute("UPDATE runs SET image_target_count = ? WHERE run_id = ?", (new_target, run_id))

        # Lock is held across the ensure so the bumped target and the selection
        # stay consistent; the ensure itself does its network off-DB.
        _ensure_images_locked(dataset, run_id, new_target)

    return _read_image_rows(dataset, run_id, version, new_target)


def get_image_file_path(conn: sqlite3.Connection, image_id: int) -> Path | None:
    row = conn.execute("SELECT cache_path FROM run_images WHERE id = ?", (image_id,)).fetchone()
    if not row or not row["cache_path"]:
        return None
    path = ROOT_DIR / row["cache_path"]
    return path if path.exists() else None


def ensure_image_cached(dataset: DatasetConfig, image_id: int) -> Path | None:
    """Return the local file for an image, re-fetching it from S3 if the cache is gone.

    Cached JPEGs may be deleted to reclaim disk (e.g. after a run is validated) or may
    never have existed locally (rows merged from a validator DB). As long as the row
    still records a member_name, the exact image can be re-downloaded from the run's
    tar on demand, so revisiting a completed image still works.
    """
    with get_conn(dataset.db_path) as conn:
        row = conn.execute("SELECT * FROM run_images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            return None
        path = _existing_cache_file(row)
        if path is not None:
            return path
        if not row["member_name"]:
            return None
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (row["run_id"],)).fetchone()
        if not run:
            return None

    with _run_lock(dataset, row["run_id"]):
        with get_conn(dataset.db_path) as conn:
            row = conn.execute("SELECT * FROM run_images WHERE id = ?", (image_id,)).fetchone()
            if row is None:
                return None
            path = _existing_cache_file(row)
            if path is not None:
                return path
        # Fetch + transcode off-DB, then persist the cache_path in a short write.
        _recache_rows(dataset, run, [row])

    with get_conn(dataset.db_path) as conn:
        refreshed = conn.execute("SELECT * FROM run_images WHERE id = ?", (image_id,)).fetchone()
    return _existing_cache_file(refreshed) if refreshed else None


def _existing_cache_file(row: sqlite3.Row) -> Path | None:
    cache_path = row["cache_path"]
    if not cache_path:
        return None
    path = ROOT_DIR / cache_path
    return path if path.exists() else None


def ensure_manifest_image_count(dataset: DatasetConfig, run_id: str) -> int | None:
    with get_conn(dataset.db_path) as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")
        if run["total_image_count"] is not None:
            return int(run["total_image_count"])
        tar_key = run["tar_key"]

    client = get_s3_client(dataset)
    manifest = _try_fetch_manifest(client, dataset.s3_bucket, tar_key)
    if manifest is None:
        return None

    count = _manifest_image_count(manifest)
    with write_conn(dataset.db_path) as conn:
        conn.execute("UPDATE runs SET total_image_count = ? WHERE run_id = ?", (count, run_id))
    return count


def queue_run_image_prefetch(dataset: DatasetConfig, run_id: str) -> str:
    global _PREFETCH_WORKER_STARTED

    key = f"{dataset.slug}:{run_id}"
    with _PREFETCH_LOCK:
        if key in _PREFETCH_KEYS:
            return "already_queued"
        _PREFETCH_KEYS.add(key)
        if not _PREFETCH_WORKER_STARTED:
            Thread(target=_prefetch_worker, name="image-prefetch", daemon=True).start()
            _PREFETCH_WORKER_STARTED = True

    _PREFETCH_QUEUE.put((dataset, run_id, key))
    return "queued"


def queue_run_image_ensure(dataset: DatasetConfig, run_id: str) -> str:
    global _REVIEW_ENSURE_WORKER_STARTED

    key = f"{dataset.slug}:{run_id}"
    with _REVIEW_ENSURE_LOCK:
        if key in _REVIEW_ENSURE_KEYS:
            return "already_queued"
        _REVIEW_ENSURE_KEYS.add(key)
        if not _REVIEW_ENSURE_WORKER_STARTED:
            Thread(target=_review_ensure_worker, name="review-image-ensure", daemon=True).start()
            _REVIEW_ENSURE_WORKER_STARTED = True

    _REVIEW_ENSURE_QUEUE.put((dataset, run_id, key))
    return "queued"


def review_image_target(total_count: int | None, fallback_target: int | None = None) -> int:
    if total_count is None:
        return max(1, int(fallback_target or 1))
    return max(1, math.ceil(total_count * REVIEW_IMAGE_FRACTION))


def count_prefetched_images(conn: sqlite3.Connection, run_id: str) -> int:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM run_images
        WHERE run_id = ?
          AND selection_version = ?
          AND image_index >= ?
          AND cache_path IS NOT NULL
        """,
        (run_id, int(run["selection_version"]), int(run["image_target_count"] or DEFAULT_IMAGE_COUNT)),
    ).fetchone()
    return int(row["count"] if row else 0)


def _get_image_rows(
    conn: sqlite3.Connection,
    run_id: str,
    version: int,
    max_image_count: int | None = None,
) -> list[sqlite3.Row]:
    where = "WHERE ri.run_id = ? AND ri.selection_version = ?"
    params: list[str | int] = [run_id, version]
    if max_image_count is not None:
        where += " AND ri.image_index < ?"
        params.append(max_image_count)

    return list(
        conn.execute(
            f"""
            SELECT ri.*, iv.status, iv.notes
            FROM run_images ri
            LEFT JOIN image_validations iv ON iv.run_image_id = ri.id
            {where}
            ORDER BY ri.image_index
            """,
            params,
        )
    )


def _read_image_rows(
    dataset: DatasetConfig,
    run_id: str,
    version: int,
    max_image_count: int | None = None,
) -> list[sqlite3.Row]:
    with get_conn(dataset.db_path) as conn:
        return _get_image_rows(conn, run_id, version, max_image_count)


def _ensure_images(dataset: DatasetConfig, run_id: str, target_count: int) -> None:
    with _run_lock(dataset, run_id):
        _ensure_images_locked(dataset, run_id, target_count)


def _ensure_images_locked(dataset: DatasetConfig, run_id: str, target_count: int) -> None:
    """Ensure up to ``target_count`` images are selected and cached for the run.

    Caller must already hold ``_run_lock`` for this run. Reads a snapshot, does
    all S3 fetching + transcoding with no DB connection held, then persists the
    results in one short write transaction.
    """
    run, all_rows = _read_run_and_rows(dataset, run_id)
    version = int(run["selection_version"])

    if len(all_rows) >= target_count:
        active_rows = [row for row in all_rows if int(row["image_index"]) < target_count]
        missing = [row for row in active_rows if not _cached_path_exists(row)]
        if missing:
            _recache_rows(dataset, run, missing)
        return

    client = get_s3_client(dataset)
    manifest = _try_fetch_manifest(client, dataset.s3_bucket, run["tar_key"])
    if manifest is not None:
        _select_via_manifest(dataset, run, all_rows, target_count, manifest, client)
    else:
        _select_via_tar_stream(dataset, run, all_rows, target_count, client)


def _read_run_and_rows(dataset: DatasetConfig, run_id: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    with get_conn(dataset.db_path) as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")
        all_rows = _get_image_rows(conn, run_id, int(run["selection_version"]))
    return run, all_rows


def _prefetch_worker() -> None:
    while True:
        dataset, run_id, key = _PREFETCH_QUEUE.get()
        try:
            run_with_retry(lambda: _prefetch_run_images(dataset, run_id))
        except Exception:
            logger.warning("Background image prefetch failed for %s:%s", dataset.slug, run_id, exc_info=True)
        finally:
            with _PREFETCH_LOCK:
                _PREFETCH_KEYS.discard(key)
            _PREFETCH_QUEUE.task_done()


def _review_ensure_worker() -> None:
    while True:
        dataset, run_id, key = _REVIEW_ENSURE_QUEUE.get()
        should_requeue = False
        try:
            should_requeue = run_with_retry(lambda: _load_next_review_image(dataset, run_id))
        except Exception:
            logger.warning("Background review image load failed for %s:%s", dataset.slug, run_id, exc_info=True)
        finally:
            with _REVIEW_ENSURE_LOCK:
                _REVIEW_ENSURE_KEYS.discard(key)
            _REVIEW_ENSURE_QUEUE.task_done()
        if should_requeue:
            queue_run_image_ensure(dataset, run_id)


def _load_next_review_image(dataset: DatasetConfig, run_id: str) -> bool:
    with _run_lock(dataset, run_id):
        # Manifest count is self-contained (own conns; network off-DB).
        total_count = ensure_manifest_image_count(dataset, run_id)

        with get_conn(dataset.db_path) as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                return False
            current_target = int(run["image_target_count"] or 1)
            version = int(run["selection_version"])

        desired_target = review_image_target(
            int(total_count) if total_count is not None else None, current_target
        )
        if desired_target != current_target:
            with write_conn(dataset.db_path) as conn:
                conn.execute(
                    "UPDATE runs SET image_target_count = ? WHERE run_id = ?", (desired_target, run_id)
                )
            current_target = desired_target

        with get_conn(dataset.db_path) as conn:
            rows = _get_image_rows(conn, run_id, version, current_target)
        active_count = len(rows)
        cached_count = sum(1 for row in rows if _cached_path_exists(row))
        if active_count >= current_target and cached_count >= current_target:
            return False

        ensure_count = min(current_target, max(active_count, cached_count + 1, 1))
        _ensure_images_locked(dataset, run_id, ensure_count)

        with get_conn(dataset.db_path) as conn:
            refreshed = _get_image_rows(conn, run_id, version, current_target)
        refreshed_active_count = len(refreshed)
        refreshed_cached_count = sum(1 for row in refreshed if _cached_path_exists(row))
        return refreshed_active_count < current_target or refreshed_cached_count < current_target


def _prefetch_run_images(dataset: DatasetConfig, run_id: str) -> None:
    client = get_s3_client(dataset)
    with _run_lock(dataset, run_id):
        with get_conn(dataset.db_path) as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                return
            tar_key = run["tar_key"]

        manifest = _try_fetch_manifest(client, dataset.s3_bucket, tar_key)
        if manifest is None:
            return

        total_count = _manifest_image_count(manifest)
        with write_conn(dataset.db_path) as conn:
            conn.execute("UPDATE runs SET total_image_count = ? WHERE run_id = ?", (total_count, run_id))

        recommended_target = prefetch_target_for_total(total_count)
        if recommended_target is None:
            return

        # Cache in chunks so each chunk's rows become visible (and the prefetch
        # progress indicator advances) without one giant final commit. Every
        # chunk fetches off-DB and commits in a short write.
        while True:
            run, all_rows = _read_run_and_rows(dataset, run_id)
            current_target = int(run["image_target_count"] or DEFAULT_IMAGE_COUNT)
            prefetch_target = max(current_target, recommended_target)
            missing = [
                row
                for row in all_rows
                if int(row["image_index"]) < prefetch_target and not _cached_path_exists(row)
            ]
            if len(all_rows) >= prefetch_target and not missing:
                return

            chunk_target = min(prefetch_target, max(len(all_rows), current_target) + PREFETCH_CHUNK_SIZE)
            _select_via_manifest(dataset, run, all_rows, chunk_target, manifest, client, update_target=False)

            if chunk_target >= prefetch_target:
                return


def prefetch_target_for_total(total_count: int) -> int | None:
    target = min(PREFETCH_MAX_IMAGE_COUNT, max(PREFETCH_MIN_IMAGE_COUNT, math.ceil(total_count * PREFETCH_IMAGE_FRACTION)), total_count)
    return target if target > DEFAULT_IMAGE_COUNT else None


def _cached_path_exists(row: sqlite3.Row) -> bool:
    cache_path = row["cache_path"]
    return bool(cache_path and (ROOT_DIR / cache_path).exists())


def _manifest_key(tar_key: str) -> str:
    base = tar_key
    for ext in (".tar.gz", ".tar"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    return base + ".manifest.json"


def _try_fetch_manifest(client, bucket: str, tar_key: str) -> list[ManifestFile] | None:
    key = _manifest_key(tar_key)
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        data = json.loads(response["Body"].read())
        return data["files"]
    except client.exceptions.NoSuchKey:
        return None
    except Exception:
        return None


def _manifest_image_count(manifest: list[ManifestFile]) -> int:
    return sum(1 for item in manifest if _is_image_member(item["name"]))


def _fetch_image_range(client, bucket: str, tar_key: str, entry: ManifestFile) -> bytes:
    start = entry["tar_offset"]
    end = start + entry["size"] - 1
    response = client.get_object(Bucket=bucket, Key=tar_key, Range=f"bytes={start}-{end}")
    return response["Body"].read()


def _select_via_manifest(
    dataset: DatasetConfig,
    run: sqlite3.Row,
    existing_rows: list[sqlite3.Row],
    target_count: int,
    manifest: list[ManifestFile],
    client,
    update_target: bool = True,
) -> None:
    version = int(run["selection_version"])
    run_id = run["run_id"]
    next_index = len(existing_rows)
    existing_members = {row["member_name"] for row in existing_rows}

    fetched: list[_CachedImage] = []

    # Re-cache any previously selected images that lost their local file.
    manifest_by_name = {f["name"]: f for f in manifest}
    for row in existing_rows:
        if int(row["image_index"]) >= target_count or _cached_path_exists(row):
            continue
        entry = manifest_by_name.get(row["member_name"])
        if entry is None:
            raise RuntimeError(f"Member {row['member_name']} not found in manifest")
        image_bytes = _fetch_image_range(client, dataset.s3_bucket, run["tar_key"], entry)
        cache_path = _write_cache_file(dataset, run_id, version, int(row["image_index"]), image_bytes)
        fetched.append(
            {"image_index": int(row["image_index"]), "member_name": row["member_name"], "cache_path": cache_path, "image_id": int(row["id"])}
        )

    # Score all candidate images and pick the lowest-scoring N (deterministic sampling).
    needed = max(0, target_count - next_index)
    if needed:
        image_entries = [f for f in manifest if _is_image_member(f["name"]) and f["name"] not in existing_members]
        scored = sorted((_member_random_score(run, f["name"]), f) for f in image_entries)
        for _, entry in scored[:needed]:
            image_bytes = _fetch_image_range(client, dataset.s3_bucket, run["tar_key"], entry)
            cache_path = _write_cache_file(dataset, run_id, version, next_index, image_bytes)
            fetched.append(
                {"image_index": next_index, "member_name": entry["name"], "cache_path": cache_path, "image_id": None}
            )
            next_index += 1

    total_count = _manifest_image_count(manifest)
    shrink_to = next_index if (update_target and next_index < target_count) else None
    _persist_cached_images(dataset, run_id, version, total_count, fetched, shrink_to)


def _select_via_tar_stream(
    dataset: DatasetConfig,
    run: sqlite3.Row,
    existing_rows: list[sqlite3.Row],
    target_count: int,
    client,
) -> None:
    version = int(run["selection_version"])
    run_id = run["run_id"]
    next_index = len(existing_rows)
    existing_members = {row["member_name"] for row in existing_rows}
    missing_by_member = {
        row["member_name"]: row
        for row in existing_rows
        if int(row["image_index"]) < target_count and not _cached_path_exists(row)
    }
    image_member_count = 0
    needed = max(0, target_count - next_index)
    candidates: list[tuple[int, str, bytes]] = []
    worst_score: int | None = None

    fetched: list[_CachedImage] = []

    response = client.get_object(Bucket=dataset.s3_bucket, Key=run["tar_key"])
    with closing(response["Body"]):
        tar = tarfile.open(fileobj=response["Body"], mode="r|*")
        for member in tar:
            if not member.isfile() or not _is_image_member(member.name):
                continue
            image_member_count += 1

            if member.name in missing_by_member:
                row = missing_by_member.pop(member.name)
                source = tar.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Could not extract {member.name}")
                cache_path = _write_cache_file(dataset, run_id, version, int(row["image_index"]), source.read())
                fetched.append(
                    {"image_index": int(row["image_index"]), "member_name": member.name, "cache_path": cache_path, "image_id": int(row["id"])}
                )
                continue

            if member.name in existing_members or needed == 0:
                continue

            score = _member_random_score(run, member.name)
            if len(candidates) >= needed and worst_score is not None and score >= worst_score:
                continue

            source = tar.extractfile(member)
            if source is None:
                raise RuntimeError(f"Could not extract {member.name}")
            candidates.append((score, member.name, source.read()))
            candidates.sort(key=lambda item: item[0])
            del candidates[needed:]
            worst_score = candidates[-1][0] if candidates else None

    if missing_by_member:
        missing = ", ".join(missing_by_member)
        raise RuntimeError(f"Could not find cached image members in tar: {missing}")

    for _, member_name, image_bytes in candidates:
        cache_path = _write_cache_file(dataset, run_id, version, next_index, image_bytes)
        fetched.append(
            {"image_index": next_index, "member_name": member_name, "cache_path": cache_path, "image_id": None}
        )
        next_index += 1

    shrink_to = next_index if next_index < target_count else None
    _persist_cached_images(dataset, run_id, version, image_member_count, fetched, shrink_to)


def _recache_rows(
    dataset: DatasetConfig,
    run: sqlite3.Row,
    rows: list[sqlite3.Row],
) -> None:
    if not rows:
        return

    client = get_s3_client(dataset)
    manifest = _try_fetch_manifest(client, dataset.s3_bucket, run["tar_key"])
    version = int(run["selection_version"])
    run_id = run["run_id"]

    fetched: list[_CachedImage] = []

    if manifest is not None:
        manifest_by_name = {f["name"]: f for f in manifest}
        for row in rows:
            entry = manifest_by_name.get(row["member_name"])
            if entry is None:
                raise RuntimeError(f"Member {row['member_name']} not found in manifest")
            image_bytes = _fetch_image_range(client, dataset.s3_bucket, run["tar_key"], entry)
            cache_path = _write_cache_file(dataset, run_id, version, int(row["image_index"]), image_bytes)
            fetched.append(
                {"image_index": int(row["image_index"]), "member_name": row["member_name"], "cache_path": cache_path, "image_id": int(row["id"])}
            )
    else:
        wanted = {row["member_name"]: row for row in rows}
        response = client.get_object(Bucket=dataset.s3_bucket, Key=run["tar_key"])
        with closing(response["Body"]):
            tar = tarfile.open(fileobj=response["Body"], mode="r|*")
            for member in tar:
                if member.name not in wanted:
                    continue
                row = wanted.pop(member.name)
                source = tar.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Could not extract {member.name}")
                cache_path = _write_cache_file(dataset, run_id, version, int(row["image_index"]), source.read())
                fetched.append(
                    {"image_index": int(row["image_index"]), "member_name": member.name, "cache_path": cache_path, "image_id": int(row["id"])}
                )
                if not wanted:
                    break

        if wanted:
            missing = ", ".join(wanted)
            raise RuntimeError(f"Could not find cached image members in tar: {missing}")

    _persist_cached_images(dataset, run_id, version, None, fetched, None)


def _persist_cached_images(
    dataset: DatasetConfig,
    run_id: str,
    version: int,
    total_image_count: int | None,
    fetched: list[_CachedImage],
    shrink_target_to: int | None,
) -> None:
    """Record fetched-and-transcoded images in one short write transaction.

    All slow work (S3 fetch, JPEG transcode, disk write) has already happened
    off-DB; this only touches SQLite, so the write lock is held briefly.
    """
    if total_image_count is None and not fetched and shrink_target_to is None:
        return

    with write_conn(dataset.db_path) as conn:
        if total_image_count is not None:
            conn.execute(
                "UPDATE runs SET total_image_count = ? WHERE run_id = ?", (total_image_count, run_id)
            )
        for item in fetched:
            if item["image_id"] is not None:
                conn.execute(
                    """
                    UPDATE run_images
                    SET cache_path = ?, cached_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (item["cache_path"], item["image_id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO run_images (run_id, selection_version, image_index, member_name, cache_path, cached_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(run_id, selection_version, image_index) DO UPDATE SET
                        member_name = excluded.member_name,
                        cache_path = excluded.cache_path,
                        cached_at = excluded.cached_at
                    """,
                    (run_id, version, item["image_index"], item["member_name"], item["cache_path"]),
                )
        if shrink_target_to is not None:
            conn.execute(
                "UPDATE runs SET image_target_count = ? WHERE run_id = ?", (shrink_target_to, run_id)
            )


def _member_random_score(run: sqlite3.Row, member_name: str) -> int:
    seed_input = f"{run['run_id']}:{run['selection_version']}:{member_name}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(seed_input).digest()[:8], "big")


def _run_lock(dataset: DatasetConfig, run_id: str) -> Lock:
    lock_key = f"{dataset.slug}:{run_id}"
    with _RUN_LOCKS_LOCK:
        lock = _RUN_LOCKS.get(lock_key)
        if lock is None:
            lock = Lock()
            _RUN_LOCKS[lock_key] = lock
        return lock


def _write_cache_file(
    dataset: DatasetConfig,
    run_id: str,
    version: int,
    image_index: int,
    image_bytes: bytes,
) -> str:
    """Transcode raw image bytes to a capped JPEG on disk; return its cache_path.

    Pure disk + CPU work, no DB access -- callers persist the returned path in a
    short write transaction afterwards.
    """
    absolute_path = dataset.cache_dir / run_id / f"v{version}" / f"{image_index + 1}.jpg"
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(image_bytes)) as image:
        # draft() lets libjpeg scale during decode (powers of two), so an 8k×4k pano is
        # decoded straight to ~MAX_CACHED_IMAGE_SIZE instead of a ~96 MiB full raster.
        # This is the change that keeps RAM from ballooning under concurrent decodes.
        image.draft("RGB", MAX_CACHED_IMAGE_SIZE)
        image = image.convert("RGB")
        # draft only lands on power-of-two scales, so clamp to the exact cap afterwards.
        image.thumbnail(MAX_CACHED_IMAGE_SIZE)
        image.save(
            absolute_path,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
        )

    return absolute_path.relative_to(ROOT_DIR).as_posix()


def _is_image_member(name: str) -> bool:
    return name.lower().endswith(IMAGE_SUFFIXES)

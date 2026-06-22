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
from app.db import get_conn, run_with_retry
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


class ManifestFile(TypedDict):
    name: str
    size: int
    tar_offset: int


def ensure_run_images(conn: sqlite3.Connection, dataset: DatasetConfig, run_id: str) -> list[sqlite3.Row]:
    with _run_lock(dataset, run_id):
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")

        target_count = int(run["image_target_count"] or DEFAULT_IMAGE_COUNT)
        return _ensure_target_images(conn, dataset, run, target_count)


def ensure_run_images_to_count(
    conn: sqlite3.Connection,
    dataset: DatasetConfig,
    run_id: str,
    target_count: int,
) -> list[sqlite3.Row]:
    with _run_lock(dataset, run_id):
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")

        return _ensure_target_images(conn, dataset, run, max(1, target_count))


def append_run_images(
    conn: sqlite3.Connection,
    dataset: DatasetConfig,
    run_id: str,
    count: int = DEFAULT_IMAGE_COUNT,
) -> list[sqlite3.Row]:
    with _run_lock(dataset, run_id):
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")

        current_target = int(run["image_target_count"] or DEFAULT_IMAGE_COUNT)
        new_target = current_target + count
        if run["total_image_count"] is not None:
            new_target = min(new_target, int(run["total_image_count"]))
        conn.execute("UPDATE runs SET image_target_count = ? WHERE run_id = ?", (new_target, run_id))
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _ensure_target_images(conn, dataset, run, new_target)


def get_image_file_path(conn: sqlite3.Connection, image_id: int) -> Path | None:
    row = conn.execute("SELECT cache_path FROM run_images WHERE id = ?", (image_id,)).fetchone()
    if not row or not row["cache_path"]:
        return None
    path = ROOT_DIR / row["cache_path"]
    return path if path.exists() else None


def ensure_image_cached(conn: sqlite3.Connection, dataset: DatasetConfig, image_id: int) -> Path | None:
    """Return the local file for an image, re-fetching it from S3 if the cache is gone.

    Cached JPEGs may be deleted to reclaim disk (e.g. after a run is validated) or may
    never have existed locally (rows merged from a validator DB). As long as the row
    still records a member_name, the exact image can be re-downloaded from the run's
    tar on demand, so revisiting a completed image still works.
    """
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
        row = conn.execute("SELECT * FROM run_images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            return None
        path = _existing_cache_file(row)
        if path is not None:
            return path
        _cache_existing_images(conn, dataset, run, [row])
        conn.commit()

    refreshed = conn.execute("SELECT * FROM run_images WHERE id = ?", (image_id,)).fetchone()
    return _existing_cache_file(refreshed) if refreshed else None


def _existing_cache_file(row: sqlite3.Row) -> Path | None:
    cache_path = row["cache_path"]
    if not cache_path:
        return None
    path = ROOT_DIR / cache_path
    return path if path.exists() else None


def ensure_manifest_image_count(conn: sqlite3.Connection, dataset: DatasetConfig, run_id: str) -> int | None:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError(f"Unknown run_id: {run_id}")
    if run["total_image_count"] is not None:
        return int(run["total_image_count"])

    client = get_s3_client(dataset)
    manifest = _try_fetch_manifest(client, dataset.s3_bucket, run["tar_key"])
    if manifest is None:
        return None
    return _store_manifest_image_count(conn, run_id, manifest)


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


def _ensure_target_images(
    conn: sqlite3.Connection,
    dataset: DatasetConfig,
    run: sqlite3.Row,
    target_count: int,
) -> list[sqlite3.Row]:
    version = int(run["selection_version"])
    all_rows = _get_image_rows(conn, run["run_id"], version)
    if len(all_rows) < target_count:
        _select_and_cache_images(conn, dataset, run, all_rows, target_count)
    else:
        active_rows = _get_image_rows(conn, run["run_id"], version, target_count)
        missing = [row for row in active_rows if not _cached_path_exists(row)]
        if missing:
            _cache_existing_images(conn, dataset, run, missing)

    return _get_image_rows(conn, run["run_id"], version, target_count)


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
    with get_conn(dataset.db_path) as conn:
        with _run_lock(dataset, run_id):
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                return False

            total_count = run["total_image_count"]
            if total_count is None:
                total_count = ensure_manifest_image_count(conn, dataset, run_id)
                run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if not run:
                    return False

            current_target = int(run["image_target_count"] or 1)
            desired_target = review_image_target(int(total_count) if total_count is not None else None, current_target)
            if desired_target != current_target:
                conn.execute("UPDATE runs SET image_target_count = ? WHERE run_id = ?", (desired_target, run_id))
                conn.commit()
                run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if not run:
                    return False
                current_target = int(run["image_target_count"] or 1)

            if not run:
                return False

            version = int(run["selection_version"])
            rows = _get_image_rows(conn, run_id, version, current_target)
            active_count = len(rows)
            cached_count = sum(1 for row in rows if _cached_path_exists(row))
            if active_count >= current_target and cached_count >= current_target:
                return False

            ensure_count = min(current_target, max(active_count, cached_count + 1, 1))
            _ensure_target_images(conn, dataset, run, ensure_count)

            refreshed = _get_image_rows(conn, run_id, version, current_target)
            refreshed_active_count = len(refreshed)
            refreshed_cached_count = sum(1 for row in refreshed if _cached_path_exists(row))
            return refreshed_active_count < current_target or refreshed_cached_count < current_target


def _prefetch_run_images(dataset: DatasetConfig, run_id: str) -> None:
    client = get_s3_client(dataset)
    with get_conn(dataset.db_path) as conn:
        with _run_lock(dataset, run_id):
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                return

            manifest = _try_fetch_manifest(client, dataset.s3_bucket, run["tar_key"])
            if manifest is None:
                return

            total_count = _store_manifest_image_count(conn, run_id, manifest)
            recommended_target = prefetch_target_for_total(total_count)
            if recommended_target is None:
                return

    while True:
        with get_conn(dataset.db_path) as conn:
            with _run_lock(dataset, run_id):
                run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if not run:
                    return

                current_target = int(run["image_target_count"] or DEFAULT_IMAGE_COUNT)
                prefetch_target = max(current_target, recommended_target)
                all_rows = _get_image_rows(conn, run_id, int(run["selection_version"]))
                missing = [
                    row
                    for row in all_rows
                    if int(row["image_index"]) < prefetch_target and not _cached_path_exists(row)
                ]
                if len(all_rows) >= prefetch_target and not missing:
                    return

                chunk_target = min(prefetch_target, max(len(all_rows), current_target) + PREFETCH_CHUNK_SIZE)
                _select_via_manifest(conn, dataset, run, all_rows, chunk_target, manifest, client, update_target=False)

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


def _store_manifest_image_count(conn: sqlite3.Connection, run_id: str, manifest: list[ManifestFile]) -> int:
    count = sum(1 for item in manifest if _is_image_member(item["name"]))
    conn.execute("UPDATE runs SET total_image_count = ? WHERE run_id = ?", (count, run_id))
    conn.commit()
    return count


def _fetch_image_range(client, bucket: str, tar_key: str, entry: ManifestFile) -> bytes:
    start = entry["tar_offset"]
    end = start + entry["size"] - 1
    response = client.get_object(Bucket=bucket, Key=tar_key, Range=f"bytes={start}-{end}")
    return response["Body"].read()


def _select_and_cache_images(
    conn: sqlite3.Connection,
    dataset: DatasetConfig,
    run: sqlite3.Row,
    existing_rows: list[sqlite3.Row],
    target_count: int,
) -> None:
    client = get_s3_client(dataset)
    manifest = _try_fetch_manifest(client, dataset.s3_bucket, run["tar_key"])

    if manifest is not None:
        _store_manifest_image_count(conn, run["run_id"], manifest)
        _select_via_manifest(conn, dataset, run, existing_rows, target_count, manifest, client)
    else:
        _select_via_tar_stream(conn, dataset, run, existing_rows, target_count, client)


def _select_via_manifest(
    conn: sqlite3.Connection,
    dataset: DatasetConfig,
    run: sqlite3.Row,
    existing_rows: list[sqlite3.Row],
    target_count: int,
    manifest: list[ManifestFile],
    client,
    update_target: bool = True,
) -> None:
    version = int(run["selection_version"])
    next_index = len(existing_rows)
    existing_members = {row["member_name"] for row in existing_rows}
    missing_by_member = {
        row["member_name"]: row
        for row in existing_rows
        if int(row["image_index"]) < target_count and not _cached_path_exists(row)
    }

    # Re-cache any previously selected images that lost their local file
    for member_name, row in missing_by_member.items():
        entry = next((f for f in manifest if f["name"] == member_name), None)
        if entry is None:
            raise RuntimeError(f"Member {member_name} not found in manifest")
        image_bytes = _fetch_image_range(client, dataset.s3_bucket, run["tar_key"], entry)
        _cache_image_bytes(
            conn,
            dataset,
            image_bytes,
            run["run_id"],
            version,
            int(row["image_index"]),
            int(row["id"]),
        )

    needed = max(0, target_count - next_index)
    if needed == 0:
        return

    # Score all candidate images and pick the lowest-scoring N (deterministic sampling)
    image_entries = [f for f in manifest if _is_image_member(f["name"]) and f["name"] not in existing_members]
    scored = sorted((_member_random_score(run, f["name"]), f) for f in image_entries)
    candidates = scored[:needed]

    for _, entry in candidates:
        image_id = _insert_image_row(conn, run["run_id"], version, next_index, entry["name"])
        conn.commit()
        image_bytes = _fetch_image_range(client, dataset.s3_bucket, run["tar_key"], entry)
        _cache_image_bytes(conn, dataset, image_bytes, run["run_id"], version, next_index, image_id)
        next_index += 1

    if update_target and next_index < target_count:
        conn.execute("UPDATE runs SET image_target_count = ? WHERE run_id = ?", (next_index, run["run_id"]))


def _select_via_tar_stream(
    conn: sqlite3.Connection,
    dataset: DatasetConfig,
    run: sqlite3.Row,
    existing_rows: list[sqlite3.Row],
    target_count: int,
    client,
) -> None:
    version = int(run["selection_version"])
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
                _cache_image_bytes(
                    conn,
                    dataset,
                    source.read(),
                    run["run_id"],
                    version,
                    int(row["image_index"]),
                    int(row["id"]),
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

    conn.execute("UPDATE runs SET total_image_count = ? WHERE run_id = ?", (image_member_count, run["run_id"]))

    if missing_by_member:
        missing = ", ".join(missing_by_member)
        raise RuntimeError(f"Could not find cached image members in tar: {missing}")

    for _, member_name, image_bytes in candidates:
        image_id = _insert_image_row(conn, run["run_id"], version, next_index, member_name)
        conn.commit()
        _cache_image_bytes(conn, dataset, image_bytes, run["run_id"], version, next_index, image_id)
        existing_members.add(member_name)
        next_index += 1

    if next_index < target_count:
        conn.execute("UPDATE runs SET image_target_count = ? WHERE run_id = ?", (next_index, run["run_id"]))


def _cache_existing_images(
    conn: sqlite3.Connection,
    dataset: DatasetConfig,
    run: sqlite3.Row,
    rows: list[sqlite3.Row],
) -> None:
    if not rows:
        return

    client = get_s3_client(dataset)
    manifest = _try_fetch_manifest(client, dataset.s3_bucket, run["tar_key"])
    version = int(run["selection_version"])

    if manifest is not None:
        manifest_by_name = {f["name"]: f for f in manifest}
        for row in rows:
            entry = manifest_by_name.get(row["member_name"])
            if entry is None:
                raise RuntimeError(f"Member {row['member_name']} not found in manifest")
            image_bytes = _fetch_image_range(client, dataset.s3_bucket, run["tar_key"], entry)
            _cache_image_bytes(
                conn,
                dataset,
                image_bytes,
                run["run_id"],
                version,
                int(row["image_index"]),
                int(row["id"]),
            )
        return

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
            _cache_image_bytes(
                conn,
                dataset,
                source.read(),
                run["run_id"],
                version,
                int(row["image_index"]),
                int(row["id"]),
            )
            if not wanted:
                break

    if wanted:
        missing = ", ".join(wanted)
        raise RuntimeError(f"Could not find cached image members in tar: {missing}")


def _insert_image_row(conn: sqlite3.Connection, run_id: str, version: int, image_index: int, member_name: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO run_images (run_id, selection_version, image_index, member_name)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, version, image_index, member_name),
    )
    return int(cursor.lastrowid)


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


def _cache_image_bytes(
    conn: sqlite3.Connection,
    dataset: DatasetConfig,
    image_bytes: bytes,
    run_id: str,
    version: int,
    image_index: int,
    image_id: int,
) -> None:
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

    relative_path = absolute_path.relative_to(ROOT_DIR).as_posix()
    conn.execute(
        """
        UPDATE run_images
        SET cache_path = ?, cached_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (relative_path, image_id),
    )


def _is_image_member(name: str) -> bool:
    return name.lower().endswith(IMAGE_SUFFIXES)

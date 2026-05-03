from __future__ import annotations

import hashlib
import io
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path
from threading import Lock

from PIL import Image, ImageFile

from app.config import CACHE_DIR, DEFAULT_IMAGE_COUNT, JPEG_QUALITY, ROOT_DIR, S3_BUCKET
from app.services.s3_index import get_s3_client


ImageFile.LOAD_TRUNCATED_IMAGES = True

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
_RUN_LOCKS: dict[str, Lock] = {}
_RUN_LOCKS_LOCK = Lock()


def ensure_run_images(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    with _run_lock(run_id):
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")

        version = int(run["selection_version"])
        target_count = int(run["image_target_count"] or DEFAULT_IMAGE_COUNT)
        rows = _get_image_rows(conn, run_id, version)

        if len(rows) < target_count:
            _select_and_cache_images(conn, run, rows, target_count)
        else:
            missing = [row for row in rows if not _cached_path_exists(row)]
            if missing:
                _cache_existing_images(conn, run, missing)

        return _get_image_rows(conn, run_id, version)


def append_run_images(conn: sqlite3.Connection, run_id: str, count: int = DEFAULT_IMAGE_COUNT) -> list[sqlite3.Row]:
    with _run_lock(run_id):
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")

        current_target = int(run["image_target_count"] or DEFAULT_IMAGE_COUNT)
        new_target = current_target + count
        conn.execute("UPDATE runs SET image_target_count = ? WHERE run_id = ?", (new_target, run_id))
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        rows = _get_image_rows(conn, run_id, int(run["selection_version"]))
        _select_and_cache_images(conn, run, rows, new_target)
        return _get_image_rows(conn, run_id, int(run["selection_version"]))


def get_image_file_path(conn: sqlite3.Connection, image_id: int) -> Path | None:
    row = conn.execute("SELECT cache_path FROM run_images WHERE id = ?", (image_id,)).fetchone()
    if not row or not row["cache_path"]:
        return None
    path = ROOT_DIR / row["cache_path"]
    return path if path.exists() else None


def _get_image_rows(conn: sqlite3.Connection, run_id: str, version: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT ri.*, iv.status
            FROM run_images ri
            LEFT JOIN image_validations iv ON iv.run_image_id = ri.id
            WHERE ri.run_id = ? AND ri.selection_version = ?
            ORDER BY ri.image_index
            """,
            (run_id, version),
        )
    )


def _cached_path_exists(row: sqlite3.Row) -> bool:
    cache_path = row["cache_path"]
    return bool(cache_path and (ROOT_DIR / cache_path).exists())


def _select_and_cache_images(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    existing_rows: list[sqlite3.Row],
    target_count: int,
) -> None:
    version = int(run["selection_version"])
    next_index = len(existing_rows)
    existing_members = {row["member_name"] for row in existing_rows}
    missing_by_member = {row["member_name"]: row for row in existing_rows if not _cached_path_exists(row)}
    needed = max(0, target_count - next_index)
    candidates: list[tuple[int, str, bytes]] = []
    worst_score: int | None = None

    client = get_s3_client()
    response = client.get_object(Bucket=S3_BUCKET, Key=run["tar_key"])
    with closing(response["Body"]):
        tar = tarfile.open(fileobj=response["Body"], mode="r|*")
        for member in tar:
            if not member.isfile() or not _is_image_member(member.name):
                continue

            if member.name in missing_by_member:
                row = missing_by_member.pop(member.name)
                source = tar.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Could not extract {member.name}")
                _cache_image_bytes(
                    conn,
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

    if missing_by_member:
        missing = ", ".join(missing_by_member)
        raise RuntimeError(f"Could not find cached image members in tar: {missing}")

    for _, member_name, image_bytes in candidates:
        image_id = _insert_image_row(conn, run["run_id"], version, next_index, member_name)
        _cache_image_bytes(conn, image_bytes, run["run_id"], version, next_index, image_id)
        existing_members.add(member_name)
        next_index += 1

    if next_index < target_count:
        conn.execute("UPDATE runs SET image_target_count = ? WHERE run_id = ?", (next_index, run["run_id"]))


def _cache_existing_images(conn: sqlite3.Connection, run: sqlite3.Row, rows: list[sqlite3.Row]) -> None:
    if not rows:
        return

    wanted = {row["member_name"]: row for row in rows}
    version = int(run["selection_version"])
    client = get_s3_client()
    response = client.get_object(Bucket=S3_BUCKET, Key=run["tar_key"])
    with closing(response["Body"]):
        tar = tarfile.open(fileobj=response["Body"], mode="r|*")
        for member in tar:
            if member.name not in wanted:
                continue
            row = wanted.pop(member.name)
            _cache_member(conn, tar, member, run["run_id"], version, int(row["image_index"]), int(row["id"]))
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


def _run_lock(run_id: str) -> Lock:
    with _RUN_LOCKS_LOCK:
        lock = _RUN_LOCKS.get(run_id)
        if lock is None:
            lock = Lock()
            _RUN_LOCKS[run_id] = lock
        return lock


def _cache_member(
    conn: sqlite3.Connection,
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    run_id: str,
    version: int,
    image_index: int,
    image_id: int,
) -> None:
    source = tar.extractfile(member)
    if source is None:
        raise RuntimeError(f"Could not extract {member.name}")

    _cache_image_bytes(conn, source.read(), run_id, version, image_index, image_id)


def _cache_image_bytes(
    conn: sqlite3.Connection,
    image_bytes: bytes,
    run_id: str,
    version: int,
    image_index: int,
    image_id: int,
) -> None:
    absolute_path = CACHE_DIR / run_id / f"v{version}" / f"{image_index + 1}.jpg"
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.convert("RGB").save(
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

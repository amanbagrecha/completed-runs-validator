from __future__ import annotations

import sqlite3

from app.config import DEFAULT_IMAGE_COUNT


def submit_validations(conn: sqlite3.Connection, items: list[dict[str, int | str]]) -> int:
    saved = 0
    for item in items:
        image_id = int(item["image_id"])
        status = str(item["status"])
        notes = str(item.get("notes", ""))
        if status not in {"pass", "fail"}:
            raise ValueError(f"Invalid status: {status}")

        image = conn.execute("SELECT * FROM run_images WHERE id = ?", (image_id,)).fetchone()
        if not image:
            raise ValueError(f"Unknown image_id: {image_id}")

        conn.execute(
            """
            INSERT INTO image_validations (run_image_id, run_id, selection_version, status, notes, submitted_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(run_image_id) DO UPDATE SET
                status = excluded.status,
                notes = excluded.notes,
                submitted_at = CURRENT_TIMESTAMP
            """,
            (image_id, image["run_id"], image["selection_version"], status, notes),
        )
        saved += 1
    return saved


def complete_run_validation(conn: sqlite3.Connection, run_id: str, completed_by: str | None = None) -> None:
    run = _get_run(conn, run_id)
    version = int(run["selection_version"])
    target, selected_images, validated_images = _run_validation_progress(conn, run)
    if selected_images < target or validated_images < target:
        raise ValueError(f"Validate all {target} active images before completing this run")

    _write_run_completion(conn, run_id, version, target, completed_by)


def maybe_complete_run_validation(conn: sqlite3.Connection, run_id: str, completed_by: str | None = None) -> bool:
    run = _get_run(conn, run_id)
    target, selected_images, validated_images = _run_validation_progress(conn, run)
    if selected_images < target or validated_images < target:
        return False

    version = int(run["selection_version"])
    if (
        run["validation_completed_at"] is not None
        and run["validation_completed_selection_version"] == version
        and run["validation_completed_image_target_count"] == target
    ):
        return False

    _write_run_completion(conn, run_id, version, target, completed_by)
    return True


def _get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError(f"Unknown run_id: {run_id}")
    return run


def _run_validation_progress(conn: sqlite3.Connection, run: sqlite3.Row) -> tuple[int, int, int]:
    version = int(run["selection_version"])
    target = int(run["image_target_count"] or DEFAULT_IMAGE_COUNT)
    stats = conn.execute(
        """
        SELECT
            COUNT(DISTINCT ri.id) AS selected_images,
            COUNT(DISTINCT iv.id) AS validated_images
        FROM run_images ri
        LEFT JOIN image_validations iv ON iv.run_image_id = ri.id
        WHERE ri.run_id = ?
          AND ri.selection_version = ?
          AND ri.image_index < ?
        """,
        (run["run_id"], version, target),
    ).fetchone()
    selected_images = int(stats["selected_images"] or 0)
    validated_images = int(stats["validated_images"] or 0)
    return target, selected_images, validated_images


def _write_run_completion(
    conn: sqlite3.Connection,
    run_id: str,
    selection_version: int,
    image_target_count: int,
    completed_by: str | None,
) -> None:
    conn.execute(
        """
        UPDATE runs
        SET validation_completed_at = CURRENT_TIMESTAMP,
            validation_completed_by = ?,
            validation_completed_selection_version = ?,
            validation_completed_image_target_count = ?
        WHERE run_id = ?
        """,
        (completed_by, selection_version, image_target_count, run_id),
    )

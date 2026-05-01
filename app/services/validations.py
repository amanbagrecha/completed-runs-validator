from __future__ import annotations

import sqlite3


def submit_validations(conn: sqlite3.Connection, items: list[dict[str, int | str]]) -> int:
    saved = 0
    for item in items:
        image_id = int(item["image_id"])
        status = str(item["status"])
        if status not in {"pass", "fail"}:
            raise ValueError(f"Invalid status: {status}")

        image = conn.execute("SELECT * FROM run_images WHERE id = ?", (image_id,)).fetchone()
        if not image:
            raise ValueError(f"Unknown image_id: {image_id}")

        conn.execute(
            """
            INSERT INTO image_validations (run_image_id, run_id, selection_version, status, submitted_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(run_image_id) DO UPDATE SET
                status = excluded.status,
                submitted_at = CURRENT_TIMESTAMP
            """,
            (image_id, image["run_id"], image["selection_version"], status),
        )
        saved += 1
    return saved

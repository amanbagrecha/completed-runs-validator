from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.config import DEFAULT_IMAGE_COUNT
from app.services.s3_index import list_run_tars
from app.services.sheets import fetch_done_runs


@dataclass(frozen=True)
class SyncSummary:
    sheet_runs: int
    s3_runs: int
    indexed_runs: int
    missing_in_s3: list[str]
    extra_in_s3: list[str]


def sync_runs(conn: sqlite3.Connection) -> SyncSummary:
    sheet_runs = fetch_done_runs()
    sheet_by_id = {run.run_id: run for run in sheet_runs}
    s3_by_id = list_run_tars()

    missing = sorted(set(sheet_by_id) - set(s3_by_id))
    extra = sorted(set(s3_by_id) - set(sheet_by_id))
    common_ids = sorted(set(sheet_by_id) & set(s3_by_id))

    for run_id in common_ids:
        sheet_run = sheet_by_id[run_id]
        s3_obj = s3_by_id[run_id]
        conn.execute(
            """
            INSERT INTO runs (
                run_id, sheet_count, vehicle_type, batch_name, tar_key, source_scope,
                s3_size, s3_last_modified, image_target_count, indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(run_id) DO UPDATE SET
                sheet_count = excluded.sheet_count,
                vehicle_type = excluded.vehicle_type,
                batch_name = excluded.batch_name,
                tar_key = excluded.tar_key,
                source_scope = excluded.source_scope,
                s3_size = excluded.s3_size,
                s3_last_modified = excluded.s3_last_modified,
                indexed_at = CURRENT_TIMESTAMP
            """,
            (
                run_id,
                sheet_run.sheet_count,
                sheet_run.vehicle_type,
                s3_obj.batch_name,
                s3_obj.key,
                s3_obj.prefix,
                s3_obj.size,
                s3_obj.last_modified.isoformat() if s3_obj.last_modified else None,
                DEFAULT_IMAGE_COUNT,
            ),
        )

    conn.execute(
        """
        INSERT INTO sync_runs (sheet_runs, s3_runs, indexed_runs, missing_in_s3, extra_in_s3)
        VALUES (?, ?, ?, ?, ?)
        """,
        (len(sheet_by_id), len(s3_by_id), len(common_ids), len(missing), len(extra)),
    )

    return SyncSummary(
        sheet_runs=len(sheet_by_id),
        s3_runs=len(s3_by_id),
        indexed_runs=len(common_ids),
        missing_in_s3=missing,
        extra_in_s3=extra,
    )

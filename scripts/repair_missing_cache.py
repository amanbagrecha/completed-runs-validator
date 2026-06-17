#!/usr/bin/env python3
"""
Repair run_images rows whose cache_path points at a JPEG that does not exist on
this host's disk.

This happens after merging a validator DB (scripts/merge_remote_dbs.py): the
run_images rows are copied over but the cached image files are not, so the local
DB ends up referencing files only present on the validator machine. Every
/api/images/{id}/file request for such a row 404s, which stalls the review grid.

Nulling cache_path / cached_at marks the row as not-yet-cached, so the normal
on-demand and background machinery re-fetches the image from S3 (via member_name)
the next time the run is reviewed.

By default only *unreviewed* rows in the active review window are repaired -- those
are the ones that block reviewing. Already-validated images are left alone (they
never appear in the unreviewed flow); pass --include-reviewed to repair them too.

Usage:
    python scripts/repair_missing_cache.py --dry-run
    python scripts/repair_missing_cache.py
    python scripts/repair_missing_cache.py --include-reviewed
"""

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
LOCAL_DB = ROOT_DIR / "data" / "app.db"


def main() -> None:
    parser = argparse.ArgumentParser(description="Null out run_images cache_path values whose file is missing")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument(
        "--include-reviewed",
        action="store_true",
        help="Also repair already-validated images (default: only unreviewed rows)",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(str(LOCAL_DB))
    conn.row_factory = sqlite3.Row

    status_sql = "" if args.include_reviewed else "AND iv.id IS NULL"
    rows = conn.execute(
        f"""
        SELECT ri.id, ri.cache_path, r.batch_name
        FROM run_images ri
        JOIN runs r ON r.run_id = ri.run_id AND r.selection_version = ri.selection_version
        LEFT JOIN image_validations iv ON iv.run_image_id = ri.id
        WHERE ri.cache_path IS NOT NULL
          AND ri.image_index < r.image_target_count
          {status_sql}
        """
    ).fetchall()

    missing_ids: list[int] = []
    by_batch: dict[str, int] = defaultdict(int)
    for row in rows:
        if not (ROOT_DIR / row["cache_path"]).exists():
            missing_ids.append(int(row["id"]))
            by_batch[row["batch_name"]] += 1

    scope = "all" if args.include_reviewed else "unreviewed"
    label = "[DRY RUN] " if args.dry_run else ""
    print(f"{label}Scanned {len(rows)} active {scope} rows with a cache_path.")
    print(f"{label}{len(missing_ids)} reference a file missing on disk.")
    for batch in sorted(by_batch):
        print(f"    {batch:12s}: {by_batch[batch]}")

    if not missing_ids or args.dry_run:
        conn.close()
        return

    with conn:
        conn.executemany(
            "UPDATE run_images SET cache_path = NULL, cached_at = NULL WHERE id = ?",
            [(image_id,) for image_id in missing_ids],
        )
    conn.close()
    print(f"Cleared cache_path for {len(missing_ids)} rows; they will re-cache from S3 on next review.")


if __name__ == "__main__":
    main()

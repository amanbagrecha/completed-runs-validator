#!/usr/bin/env python3
"""
Pull remote validator SQLite databases and merge them into the local orchestrator DB.

Usage:
    python scripts/merge_remote_dbs.py
    python scripts/merge_remote_dbs.py --dry-run        # show what would change without writing
    python scripts/merge_remote_dbs.py --remotes ec2-user@host2:/path/app.db ...

Conflict resolution for `runs`:
  - If remote has compltd_completed_at and local doesn't → take remote
  - If both completed → take whichever has a later compltd_updated_at
  - If neither completed → take whichever has a later compltd_updated_at
  - Otherwise keep local

For `run_images` and `image_validations`: INSERT OR IGNORE on unique constraints.
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
LOCAL_DB = str(Path(__file__).resolve().parents[1] / "data" / "app.db")

DEFAULT_REMOTES = [
    "ec2-user@100.119.159.28:/home/ec2-user/completed-runs-validator/data/app.db",
    "ubuntu@100.101.42.18:/home/ubuntu/completed-runs-validator/data/app.db",
]


def pull_remote_db(remote_spec: str, local_path: str) -> None:
    cmd = [
        "rsync", "-az",
        "-e", f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=10",
        remote_spec,
        local_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"rsync failed for {remote_spec}:\n{result.stderr}")
    print(f"  Pulled {remote_spec}")


def col_names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def merge_runs(local: sqlite3.Connection, remote: sqlite3.Connection, dry_run: bool) -> tuple[int, int]:
    remote_cols = col_names(remote, "runs")
    local_cols = col_names(local, "runs")
    remote_col_str = ", ".join(remote_cols)

    # Only insert columns that exist in local DB
    shared_cols = [c for c in remote_cols if c in local_cols]
    shared_col_str = ", ".join(shared_cols)
    placeholders = ", ".join(["?"] * len(shared_cols))
    set_clause = ", ".join(f"{c} = ?" for c in shared_cols if c != "run_id")
    local_col_str = ", ".join(local_cols)

    updated = inserted = skipped = 0

    for remote_row in remote.execute(f"SELECT {remote_col_str} FROM runs"):
        rd = dict(zip(remote_cols, remote_row))
        run_id = rd["run_id"]

        local_row = local.execute(f"SELECT {local_col_str} FROM runs WHERE run_id = ?", (run_id,)).fetchone()

        if local_row is None:
            if not dry_run:
                vals = [rd[c] for c in shared_cols]
                local.execute(f"INSERT INTO runs ({shared_col_str}) VALUES ({placeholders})", vals)
            inserted += 1
        else:
            ld = dict(zip(local_cols, local_row))
            r_completed = rd.get("compltd_completed_at")
            l_completed = ld.get("compltd_completed_at")
            r_updated = str(rd.get("compltd_updated_at") or "")
            l_updated = str(ld.get("compltd_updated_at") or "")

            take_remote = (
                (r_completed and not l_completed)
                or (r_completed and l_completed and r_updated > l_updated)
                or (not r_completed and not l_completed and r_updated > l_updated)
            )

            if take_remote:
                if not dry_run:
                    vals = [rd[c] for c in shared_cols if c != "run_id"] + [run_id]
                    local.execute(f"UPDATE runs SET {set_clause} WHERE run_id = ?", vals)
                updated += 1
            else:
                skipped += 1

    return inserted + updated, skipped


def merge_run_images(local: sqlite3.Connection, remote: sqlite3.Connection, dry_run: bool) -> int:
    inserted = 0
    for row in remote.execute(
        "SELECT run_id, selection_version, image_index, member_name, cache_path, cached_at, created_at FROM run_images"
    ):
        if not dry_run:
            local.execute(
                """
                INSERT OR IGNORE INTO run_images
                  (run_id, selection_version, image_index, member_name, cache_path, cached_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            changes = local.execute("SELECT changes()").fetchone()[0]
        else:
            existing = local.execute(
                "SELECT 1 FROM run_images WHERE run_id=? AND selection_version=? AND image_index=?",
                (row[0], row[1], row[2]),
            ).fetchone()
            changes = 0 if existing else 1
        inserted += changes
    return inserted


def merge_image_validations(local: sqlite3.Connection, remote: sqlite3.Connection, dry_run: bool) -> int:
    inserted = 0
    rows = remote.execute(
        """
        SELECT iv.run_id, iv.selection_version, iv.status, iv.submitted_at, iv.notes,
               ri.image_index
        FROM image_validations iv
        JOIN run_images ri ON ri.id = iv.run_image_id
        """
    ).fetchall()

    for run_id, sel_ver, status, submitted_at, notes, img_idx in rows:
        local_ri = local.execute(
            "SELECT id FROM run_images WHERE run_id=? AND selection_version=? AND image_index=?",
            (run_id, sel_ver, img_idx),
        ).fetchone()

        if not local_ri:
            continue

        local_run_image_id = local_ri[0]
        if not dry_run:
            local.execute(
                """
                INSERT OR IGNORE INTO image_validations
                  (run_image_id, run_id, selection_version, status, submitted_at, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (local_run_image_id, run_id, sel_ver, status, submitted_at, notes),
            )
            changes = local.execute("SELECT changes()").fetchone()[0]
        else:
            existing = local.execute(
                "SELECT 1 FROM image_validations WHERE run_image_id=?", (local_run_image_id,)
            ).fetchone()
            changes = 0 if existing else 1
        inserted += changes

    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge remote validator DBs into local orchestrator DB")
    parser.add_argument("--remotes", nargs="+", default=DEFAULT_REMOTES, metavar="USER@HOST:/path/app.db")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    args = parser.parse_args()

    label = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{'='*60}")
    print(f"{label}DB merge started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Local DB: {LOCAL_DB}")
    print(f"{'='*60}")

    local = sqlite3.connect(LOCAL_DB)
    local.execute("PRAGMA journal_mode=WAL")

    for remote_spec in args.remotes:
        print(f"\n--- {remote_spec} ---")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = f.name
        try:
            pull_remote_db(remote_spec, tmp)
            remote = sqlite3.connect(tmp)

            if args.dry_run:
                runs_changed, runs_skipped = merge_runs(local, remote, dry_run=True)
                imgs = merge_run_images(local, remote, dry_run=True)
                vals = merge_image_validations(local, remote, dry_run=True)
            else:
                with local:
                    runs_changed, runs_skipped = merge_runs(local, remote, dry_run=False)
                    imgs = merge_run_images(local, remote, dry_run=False)
                    vals = merge_image_validations(local, remote, dry_run=False)

            print(f"  runs              : {runs_changed:4d} updated/inserted  |  {runs_skipped:4d} skipped (local newer)")
            print(f"  run_images        : {imgs:4d} inserted")
            print(f"  image_validations : {vals:4d} inserted")
            remote.close()
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
        finally:
            os.unlink(tmp)

    local.close()
    print(f"\n{label}Merge complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")


if __name__ == "__main__":
    main()

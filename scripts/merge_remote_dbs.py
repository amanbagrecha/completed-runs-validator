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
import http.cookiejar
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
LOCAL_DB = str(Path(__file__).resolve().parents[1] / "data" / "app.db")
AUTH_FILE = Path(__file__).resolve().parents[1] / "data" / "auth.txt"
LOCAL_API_URL = "http://127.0.0.1:8123"

DEFAULT_REMOTES = [
    "ec2-user@100.119.159.28:/home/ec2-user/completed-runs-validator/data/app.db",
    "ubuntu@100.101.42.18:/home/ubuntu/completed-runs-validator/data/app.db",
    "ubuntu@100.107.25.98:/home/ubuntu/completed-runs-validator/data/app.db",
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


# The local validation facts. Unlike the compltd_* summary (which is synced via the
# Google Sheet and arbitrated by compltd_updated_at), these are only ever written
# locally on the machine where a run is validated and are monotonic: once a run is
# validated it stays validated. We merge them separately so they are filled in from a
# source DB whenever the local DB lacks them, and never overwritten or wiped.
VALIDATION_FACT_COLS = [
    "validation_completed_at",
    "validation_completed_by",
    "validation_completed_selection_version",
    "validation_completed_image_target_count",
]


def merge_runs(local: sqlite3.Connection, remote: sqlite3.Connection, dry_run: bool) -> tuple[int, int]:
    remote_cols = col_names(remote, "runs")
    local_cols = col_names(local, "runs")
    remote_col_str = ", ".join(remote_cols)

    # Only insert columns that exist in local DB
    shared_cols = [c for c in remote_cols if c in local_cols]
    shared_col_str = ", ".join(shared_cols)
    placeholders = ", ".join(["?"] * len(shared_cols))
    local_col_str = ", ".join(local_cols)

    # compltd_* summary fields are merged via the timestamp comparison below; the
    # validation facts are handled on their own (fill-if-missing) so the summary
    # overwrite can never clobber a validation_completed_at the local DB already has.
    summary_cols = [c for c in shared_cols if c != "run_id" and c not in VALIDATION_FACT_COLS]
    summary_set_clause = ", ".join(f"{c} = ?" for c in summary_cols)
    validation_cols = [c for c in VALIDATION_FACT_COLS if c in shared_cols]
    validation_set_clause = ", ".join(f"{c} = ?" for c in validation_cols)

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
            continue

        ld = dict(zip(local_cols, local_row))
        r_completed = rd.get("compltd_completed_at")
        l_completed = ld.get("compltd_completed_at")
        r_updated = str(rd.get("compltd_updated_at") or "")
        l_updated = str(ld.get("compltd_updated_at") or "")

        take_summary = (
            (r_completed and not l_completed)
            or (r_completed and l_completed and r_updated > l_updated)
            or (not r_completed and not l_completed and r_updated > l_updated)
        )
        # Pull validation facts from the source whenever the local DB is missing them,
        # independent of the compltd_* timestamp (which ties when both sides share the
        # same sheet-sourced value).
        fill_validation = bool(rd.get("validation_completed_at")) and not ld.get("validation_completed_at")

        if not dry_run:
            if take_summary and summary_cols:
                vals = [rd[c] for c in summary_cols] + [run_id]
                local.execute(f"UPDATE runs SET {summary_set_clause} WHERE run_id = ?", vals)
            if fill_validation and validation_cols:
                vals = [rd[c] for c in validation_cols] + [run_id]
                local.execute(f"UPDATE runs SET {validation_set_clause} WHERE run_id = ?", vals)

        if take_summary or fill_validation:
            updated += 1
        else:
            skipped += 1

    return inserted + updated, skipped


def merge_run_images(local: sqlite3.Connection, remote: sqlite3.Connection, dry_run: bool) -> int:
    # We deliberately drop the remote cache_path / cached_at: the cached JPEGs live in
    # the remote machine's data/cache/images/ and are NOT transferred here. Copying the
    # path would leave the local DB pointing at files that don't exist on this host
    # (every /api/images/{id}/file then 404s). Inserting NULL marks the row as
    # not-yet-cached so the normal on-demand / background machinery re-fetches the image
    # from S3 via member_name when it is needed.
    inserted = 0
    for row in remote.execute(
        "SELECT run_id, selection_version, image_index, member_name, created_at FROM run_images"
    ):
        if not dry_run:
            local.execute(
                """
                INSERT OR IGNORE INTO run_images
                  (run_id, selection_version, image_index, member_name, cache_path, cached_at, created_at)
                VALUES (?, ?, ?, ?, NULL, NULL, ?)
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


def _read_auth() -> tuple[str, str]:
    config: dict[str, str] = {}
    if AUTH_FILE.exists():
        for line in AUTH_FILE.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                config[k.strip()] = v.strip()
    username = config.get("COMPLTD_ADMIN_USERNAME", "admin")
    password = config.get("COMPLTD_ADMIN_PASSWORD", "")
    return username, password


def trigger_sheet_sync() -> dict:
    username, password = _read_auth()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    login_data = urllib.parse.urlencode(
        {"username": username, "password": password, "next_path": "/review"}
    ).encode("utf-8")
    opener.open(urllib.request.Request(f"{LOCAL_API_URL}/login", data=login_data, method="POST"))

    req = urllib.request.Request(f"{LOCAL_API_URL}/api/sync", method="POST")
    with opener.open(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge remote validator DBs into local orchestrator DB")
    parser.add_argument("--remotes", nargs="+", default=DEFAULT_REMOTES, metavar="USER@HOST:/path/app.db")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--sheet-sync", action="store_true", help="After merging, flush pending completions to Google Sheets via /api/sync")
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
    print(f"\n{label}Merge complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if args.sheet_sync and not args.dry_run:
        print("\n--- Sheet sync ---")
        try:
            result = trigger_sheet_sync()
            sheet = result.get("sheet_sync", {})
            if sheet:
                for status, count in sheet.items():
                    print(f"  {status}: {count}")
            else:
                print("  nothing to sync")
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
    print()


if __name__ == "__main__":
    main()

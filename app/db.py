import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import APP_DIR, DEFAULT_IMAGE_COUNT


SQLITE_BUSY_TIMEOUT_MS = 30000


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = (APP_DIR / "schema.sql").read_text()
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(schema)
        _migrate_runs_total_image_count(conn)
        _migrate_run_completion_columns(conn)
        _migrate_run_completion_user(conn)
        _migrate_default_image_target_count(conn)
        _migrate_image_validation_notes(conn)
        _migrate_run_sheet_metadata(conn)


def _migrate_runs_total_image_count(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    if "total_image_count" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN total_image_count INTEGER")


def _migrate_run_completion_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    migrations = {
        "validation_completed_at": "ALTER TABLE runs ADD COLUMN validation_completed_at TEXT",
        "validation_completed_selection_version": (
            "ALTER TABLE runs ADD COLUMN validation_completed_selection_version INTEGER"
        ),
        "validation_completed_image_target_count": (
            "ALTER TABLE runs ADD COLUMN validation_completed_image_target_count INTEGER"
        ),
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)


def _migrate_run_completion_user(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    if "validation_completed_by" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN validation_completed_by TEXT")


def _migrate_default_image_target_count(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE runs
        SET image_target_count = ?
        WHERE image_target_count <= 0
        """,
        (DEFAULT_IMAGE_COUNT,),
    )


def _migrate_image_validation_notes(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(image_validations)").fetchall()
    }
    if "notes" not in columns:
        conn.execute("ALTER TABLE image_validations ADD COLUMN notes TEXT NOT NULL DEFAULT ''")

    conn.execute("UPDATE image_validations SET notes = '' WHERE notes IS NULL")


def _migrate_run_sheet_metadata(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    migrations = {
        "locality_name": "ALTER TABLE runs ADD COLUMN locality_name TEXT",
        "locality_category": "ALTER TABLE runs ADD COLUMN locality_category TEXT",
        "region_id": "ALTER TABLE runs ADD COLUMN region_id TEXT",
        "subtype_label": "ALTER TABLE runs ADD COLUMN subtype_label TEXT",
        "dispatch_hold": "ALTER TABLE runs ADD COLUMN dispatch_hold TEXT",
        "pipeline_status": "ALTER TABLE runs ADD COLUMN pipeline_status TEXT",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)


@contextmanager
def get_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

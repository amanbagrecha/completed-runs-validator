import logging
import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from app.config import APP_DIR, DEFAULT_IMAGE_COUNT


SQLITE_BUSY_TIMEOUT_MS = 30000

# Retry tuning for "database is locked". WAL serialises writers and busy_timeout
# makes a writer wait for a held write lock, but a read->write upgrade on a stale
# snapshot (SQLITE_BUSY_SNAPSHOT) fails *immediately* and is NOT covered by
# busy_timeout. Re-running the operation on a fresh connection takes a new
# snapshot and succeeds once the competing writer has committed.
LOCKED_RETRY_ATTEMPTS = 6
LOCKED_RETRY_BASE_DELAY = 0.05
LOCKED_RETRY_MAX_DELAY = 1.0

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


def run_with_retry(
    operation: Callable[[], T],
    *,
    attempts: int = LOCKED_RETRY_ATTEMPTS,
    base_delay: float = LOCKED_RETRY_BASE_DELAY,
) -> T:
    """Run a DB operation, retrying briefly when SQLite reports the database is locked.

    ``operation`` must be self-contained (open its own connection / transaction) so
    that each attempt runs against a fresh snapshot. It should also be idempotent,
    since a later attempt may re-run work an earlier attempt partially committed.
    Non-lock errors propagate immediately; lock errors propagate after the final
    attempt is exhausted.
    """
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc) or attempt == attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), LOCKED_RETRY_MAX_DELAY)
            delay += random.uniform(0, base_delay)
            logger.warning(
                "Database locked, retrying (attempt %d/%d) after %.3fs", attempt, attempts, delay
            )
            time.sleep(delay)
    # Unreachable: the final attempt either returns or raises above.
    raise AssertionError("run_with_retry exhausted without returning or raising")


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
        _migrate_sheet_coordination_columns(conn)


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


def _migrate_sheet_coordination_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    migrations = {
        "sheet_validation": "ALTER TABLE runs ADD COLUMN sheet_validation TEXT",
        "compltd_status": "ALTER TABLE runs ADD COLUMN compltd_status TEXT",
        "compltd_validator": "ALTER TABLE runs ADD COLUMN compltd_validator TEXT",
        "compltd_started_at": "ALTER TABLE runs ADD COLUMN compltd_started_at TEXT",
        "compltd_completed_at": "ALTER TABLE runs ADD COLUMN compltd_completed_at TEXT",
        "compltd_outcome": "ALTER TABLE runs ADD COLUMN compltd_outcome TEXT",
        "compltd_reviewed_images": "ALTER TABLE runs ADD COLUMN compltd_reviewed_images INTEGER",
        "compltd_failed_images": "ALTER TABLE runs ADD COLUMN compltd_failed_images INTEGER",
        "compltd_updated_at": "ALTER TABLE runs ADD COLUMN compltd_updated_at TEXT",
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

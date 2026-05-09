import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import APP_DIR


SQLITE_BUSY_TIMEOUT_MS = 30000


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = (APP_DIR / "schema.sql").read_text()
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(schema)


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

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import DATA_DIR, DB_PATH, APP_DIR


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    schema = (APP_DIR / "schema.sql").read_text()
    with get_conn() as conn:
        conn.executescript(schema)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

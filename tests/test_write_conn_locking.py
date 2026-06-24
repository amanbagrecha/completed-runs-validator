"""Tests for the write-lock contention fix.

These pin down the root cause of the "database is locked" 500s that broke draft
submission, and prove ``write_conn`` (BEGIN IMMEDIATE) is not vulnerable to it.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.db import get_conn, init_db, write_conn


def _seed(db_path: Path) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, batch_name, tar_key, source_scope) VALUES (?, ?, ?, ?)",
            ("run-1", "batch-1", "batch-1/run-1.tar", "wasabi"),
        )
        conn.commit()


class StaleSnapshotTests(unittest.TestCase):
    """The exact failure mode: a read->write upgrade on a stale snapshot fails
    *immediately* with SQLITE_BUSY and is NOT covered by busy_timeout."""

    def test_get_conn_read_then_write_fails_instantly_on_stale_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.db"
            _seed(db_path)

            reader = sqlite3.connect(db_path, isolation_level="DEFERRED")
            reader.row_factory = sqlite3.Row
            # A generous busy_timeout that should be irrelevant for a snapshot conflict.
            reader.execute("PRAGMA busy_timeout = 30000")
            try:
                reader.execute("BEGIN")
                reader.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()

                # A competing writer commits *after* the reader took its snapshot.
                with write_conn(db_path) as other:
                    other.execute("UPDATE runs SET vehicle_type = 'x' WHERE run_id = 'run-1'")

                # The reader now upgrades to a write on its stale snapshot.
                started = time.monotonic()
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    reader.execute("UPDATE runs SET vehicle_type = 'y' WHERE run_id = 'run-1'")
                elapsed = time.monotonic() - started
            finally:
                reader.rollback()
                reader.close()

            self.assertIn("locked", str(ctx.exception).lower())
            # Crucially: it failed fast, not after waiting out the 30s busy_timeout.
            self.assertLess(elapsed, 2.0)

    def test_write_conn_grabs_lock_up_front_so_no_writer_sneaks_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.db"
            _seed(db_path)

            blocked: list[str] = []

            def competing_writer() -> None:
                conn = sqlite3.connect(db_path, isolation_level=None)
                conn.execute("PRAGMA busy_timeout = 200")
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("UPDATE runs SET vehicle_type = 'sneak' WHERE run_id = 'run-1'")
                    conn.execute("COMMIT")
                except sqlite3.OperationalError as exc:
                    blocked.append(str(exc))
                finally:
                    conn.close()

            with write_conn(db_path) as conn:
                # We hold the write lock (BEGIN IMMEDIATE) before reading, so a
                # competing writer cannot commit in between to stale our snapshot.
                conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
                t = threading.Thread(target=competing_writer)
                t.start()
                t.join(timeout=5)
                # Our read->write does not raise: no stale snapshot is possible.
                conn.execute("UPDATE runs SET vehicle_type = 'mine' WHERE run_id = 'run-1'")

            self.assertEqual(len(blocked), 1, "competing writer should have been locked out")

            with get_conn(db_path) as conn:
                value = conn.execute(
                    "SELECT vehicle_type FROM runs WHERE run_id = 'run-1'"
                ).fetchone()[0]
            self.assertEqual(value, "mine")


class WriteConnContentionTests(unittest.TestCase):
    def test_write_conn_waits_for_a_held_lock_then_succeeds(self) -> None:
        """A held write lock makes write_conn *wait* (busy_timeout), not fail."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.db"
            _seed(db_path)

            lock_held = threading.Event()
            release = threading.Event()

            def holder() -> None:
                conn = sqlite3.connect(db_path, isolation_level=None)
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE runs SET vehicle_type = 'held' WHERE run_id = 'run-1'")
                lock_held.set()
                release.wait(timeout=5)
                conn.execute("COMMIT")
                conn.close()

            t = threading.Thread(target=holder)
            t.start()
            self.assertTrue(lock_held.wait(timeout=5))

            # Release the holder shortly; write_conn should block until then and
            # then commit without raising.
            threading.Timer(0.3, release.set).start()
            with write_conn(db_path) as conn:
                conn.execute("UPDATE runs SET subtype_label = 'after-wait' WHERE run_id = 'run-1'")
            t.join(timeout=5)

            with get_conn(db_path) as conn:
                row = conn.execute(
                    "SELECT vehicle_type, subtype_label FROM runs WHERE run_id = 'run-1'"
                ).fetchone()
            self.assertEqual(row["vehicle_type"], "held")
            self.assertEqual(row["subtype_label"], "after-wait")


if __name__ == "__main__":
    unittest.main()

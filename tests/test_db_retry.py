from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app.db as db_module
from app.db import get_conn, init_db, run_with_retry
from app.services.validations import submit_validations


class RunWithRetryUnitTests(unittest.TestCase):
    def test_retries_locked_then_succeeds(self) -> None:
        calls = {"n": 0}

        def operation():
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with patch("app.db.time.sleep") as sleep_mock:
            result = run_with_retry(operation, attempts=5, base_delay=0.01)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)
        # Slept once before each of the two retries.
        self.assertEqual(sleep_mock.call_count, 2)

    def test_non_locked_operational_error_is_not_retried(self) -> None:
        calls = {"n": 0}

        def operation():
            calls["n"] += 1
            raise sqlite3.OperationalError("no such table: runs")

        with patch("app.db.time.sleep") as sleep_mock:
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                run_with_retry(operation, attempts=5)

        self.assertIn("no such table", str(ctx.exception))
        self.assertEqual(calls["n"], 1)
        sleep_mock.assert_not_called()

    def test_gives_up_after_attempts(self) -> None:
        calls = {"n": 0}

        def operation():
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        with patch("app.db.time.sleep"):
            with self.assertRaises(sqlite3.OperationalError):
                run_with_retry(operation, attempts=4, base_delay=0.01)

        self.assertEqual(calls["n"], 4)

    def test_other_exceptions_propagate_immediately(self) -> None:
        # submit_validations raises ValueError for unknown images; the retry
        # wrapper must surface that as a 400, never swallow or retry it.
        calls = {"n": 0}

        def operation():
            calls["n"] += 1
            raise ValueError("Unknown image_id: 999")

        with self.assertRaises(ValueError):
            run_with_retry(operation, attempts=5)
        self.assertEqual(calls["n"], 1)


def _hold_write_lock(db_path: Path, hold_seconds: float, lock_held: threading.Event) -> None:
    conn = sqlite3.connect(db_path, timeout=hold_seconds + 5)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE runs SET vehicle_type = 'locked' WHERE run_id = 'run-1'")
        lock_held.set()
        time.sleep(hold_seconds)
        conn.commit()
    finally:
        conn.close()


class RunWithRetryContentionTests(unittest.TestCase):
    def _seed(self, db_path: Path) -> None:
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO runs (run_id, batch_name, tar_key, source_scope) VALUES (?, ?, ?, ?)",
                ("run-1", "batch-48", "batch-48/run-1.tar", "wasabi"),
            )
            conn.execute(
                "INSERT INTO run_images (run_id, selection_version, image_index, member_name) "
                "VALUES (?, ?, ?, ?)",
                ("run-1", 1, 0, "img-0.jpg"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_submit_survives_a_held_write_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "app.db"
            self._seed(db_path)

            # Make a blocked writer fail fast so the retry path is exercised,
            # instead of waiting out the full 30s busy_timeout.
            original_timeout = db_module.SQLITE_BUSY_TIMEOUT_MS
            db_module.SQLITE_BUSY_TIMEOUT_MS = 50
            try:
                lock_held = threading.Event()
                holder = threading.Thread(
                    target=_hold_write_lock, args=(db_path, 0.2, lock_held)
                )
                holder.start()
                self.assertTrue(lock_held.wait(timeout=5), "writer never acquired the lock")

                attempts = {"n": 0}

                def operation():
                    attempts["n"] += 1
                    with get_conn(db_path) as conn:
                        return submit_validations(
                            conn, [{"image_id": 1, "status": "pass", "notes": "ok"}]
                        )

                # The first attempt(s) hit "database is locked"; once the holder
                # commits and releases, a retry succeeds.
                saved = run_with_retry(operation)
                holder.join(timeout=5)
            finally:
                db_module.SQLITE_BUSY_TIMEOUT_MS = original_timeout

            self.assertEqual(saved, 1)
            self.assertGreaterEqual(attempts["n"], 2, "expected at least one retry under contention")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT status FROM image_validations WHERE run_image_id = 1"
                ).fetchone()
                run = conn.execute(
                    "SELECT vehicle_type FROM runs WHERE run_id = 'run-1'"
                ).fetchone()
            finally:
                conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "pass")
            # The competing writer's commit also landed.
            self.assertEqual(run["vehicle_type"], "locked")


if __name__ == "__main__":
    unittest.main()

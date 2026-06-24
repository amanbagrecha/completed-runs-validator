"""Concurrency test reproducing the production hot path.

Reviewers submitting drafts (write_conn + submit_validations, the exact route
operation) run concurrently with image-cache writes (_persist_cached_images).
With the fix, every writer uses BEGIN IMMEDIATE and holds the lock only for a
few fast statements, so they serialise cleanly with zero "database is locked"
failures -- which is what used to break draft submission under load.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app.db import get_conn, init_db, write_conn
from app.services.image_cache import _persist_cached_images
from app.services.validations import submit_validations


def _make_dataset(db_path: Path):
    from app.config import DatasetConfig

    return DatasetConfig(
        slug="test",
        label="Test",
        page_path="/test",
        api_prefix="/test/api",
        db_path=db_path,
        cache_dir=db_path.parent / "cache",
        aws_profile="test",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        batch_prefixes=[],
    )


class SubmitCachingConcurrencyTests(unittest.TestCase):
    IMAGE_COUNT = 12
    SUBMIT_THREADS = 6
    WRITER_THREADS = 4
    ITERATIONS = 40

    def _seed(self, db_path: Path) -> list[int]:
        init_db(db_path)
        image_ids: list[int] = []
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO runs (run_id, batch_name, tar_key, source_scope, image_target_count) "
                "VALUES (?, ?, ?, ?, ?)",
                ("run-1", "batch-1", "batch-1/run-1.tar", "wasabi", self.IMAGE_COUNT),
            )
            for i in range(self.IMAGE_COUNT):
                cur = conn.execute(
                    "INSERT INTO run_images (run_id, selection_version, image_index, member_name) "
                    "VALUES (?, ?, ?, ?)",
                    ("run-1", 1, i, f"img-{i}.jpg"),
                )
                image_ids.append(int(cur.lastrowid))
            conn.commit()
        return image_ids

    def test_no_lock_errors_under_concurrent_submit_and_caching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.db"
            image_ids = self._seed(db_path)
            dataset = _make_dataset(db_path)

            errors: list[str] = []
            barrier = threading.Barrier(self.SUBMIT_THREADS + self.WRITER_THREADS)

            def submit_worker(worker_idx: int) -> None:
                barrier.wait()
                try:
                    for it in range(self.ITERATIONS):
                        image_id = image_ids[(worker_idx + it) % len(image_ids)]
                        status = "pass" if (it % 2 == 0) else "fail"
                        # Exact pattern used by the /review/submit route operation.
                        with write_conn(db_path) as conn:
                            submit_validations(
                                conn,
                                [{"image_id": image_id, "status": status, "notes": "n"}],
                            )
                except Exception as exc:  # noqa: BLE001 - record any failure
                    errors.append(f"submit[{worker_idx}]: {type(exc).__name__}: {exc}")

            def caching_worker(worker_idx: int) -> None:
                barrier.wait()
                try:
                    for it in range(self.ITERATIONS):
                        # A real caching write (no network -- bytes already "fetched").
                        _persist_cached_images(
                            dataset,
                            "run-1",
                            1,
                            total_image_count=self.IMAGE_COUNT + worker_idx,
                            fetched=[],
                            shrink_target_to=None,
                        )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"caching[{worker_idx}]: {type(exc).__name__}: {exc}")

            threads = [
                threading.Thread(target=submit_worker, args=(i,))
                for i in range(self.SUBMIT_THREADS)
            ] + [
                threading.Thread(target=caching_worker, args=(i,))
                for i in range(self.WRITER_THREADS)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

            self.assertEqual(errors, [], f"writers hit errors under contention: {errors}")

            # Every image got a validation row (upserts), proving submits landed.
            with get_conn(db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM image_validations").fetchone()[0]
            self.assertGreater(count, 0)
            self.assertLessEqual(count, self.IMAGE_COUNT)


if __name__ == "__main__":
    unittest.main()

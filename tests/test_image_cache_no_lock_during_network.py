"""End-to-end test of the image-cache fix.

Drives the real selection/caching code with a fake S3 client and asserts the
durable property: while S3 network I/O is in flight, NO SQLite write lock is
held -- so a concurrent writer (e.g. a draft submission) is never blocked. This
is the regression that caused the "database is locked" 500s.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app.services.image_cache as image_cache
from app.config import ROOT_DIR
from app.db import init_db
from app.services.image_cache import ensure_run_images


def _tiny_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 16), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:  # pragma: no cover - parity with botocore stream
        pass


class _FakeExceptions:
    class NoSuchKey(Exception):
        pass


class FakeS3Client:
    """Minimal stand-in for the boto3 S3 client used by image_cache."""

    def __init__(self, manifest_files, image_bytes, on_network):
        self.exceptions = _FakeExceptions
        self._manifest_files = manifest_files
        self._image_bytes = image_bytes
        self._on_network = on_network
        self.range_calls = 0
        self.manifest_calls = 0

    def get_object(self, Bucket, Key, Range=None):  # noqa: N803 - boto3 kwarg names
        # Every call models a network round-trip; assert no write lock is held.
        self._on_network(f"{Key}|{Range}")
        if Key.endswith(".manifest.json"):
            self.manifest_calls += 1
            body = json.dumps({"files": self._manifest_files}).encode("utf-8")
            return {"Body": _Body(body)}
        self.range_calls += 1
        return {"Body": _Body(self._image_bytes)}


def _make_dataset(db_path: Path, cache_dir: Path):
    from app.config import DatasetConfig

    return DatasetConfig(
        slug="test",
        label="Test",
        page_path="/test",
        api_prefix="/test/api",
        db_path=db_path,
        cache_dir=cache_dir,
        aws_profile="test",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        batch_prefixes=[],
    )


class ImageCacheNoLockDuringNetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.db_path = tmp / "app.db"
        # cache_dir must live under ROOT_DIR so cache paths are ROOT-relative.
        self.cache_dir = ROOT_DIR / "data" / "cache" / f"test-{tmp.name}"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._rm_cache)

        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO runs (run_id, batch_name, tar_key, source_scope, image_target_count) "
                "VALUES (?, ?, ?, ?, ?)",
                ("run-1", "batch-1", "batch-1/run-1.tar", "wasabi", 3),
            )
            conn.commit()

        self.dataset = _make_dataset(self.db_path, self.cache_dir)
        self.lock_violations: list[str] = []

    def _rm_cache(self) -> None:
        import shutil

        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def _assert_db_writable(self, label: str) -> None:
        """Probe: an independent connection must be able to grab the write lock
        right now. If the caching code held a write lock across the network this
        BEGIN IMMEDIATE would block and (with a short busy_timeout) raise."""
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 400")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE runs SET pipeline_status = ? WHERE run_id = 'run-1'", (label,))
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            self.lock_violations.append(f"{label}: {exc}")
        finally:
            conn.close()

    def test_manifest_path_selects_caches_and_holds_no_write_lock(self) -> None:
        manifest_files = [
            {"name": f"img-{i}.jpg", "size": 100, "tar_offset": i * 100}
            for i in range(10)
        ]
        # Include a non-image member to confirm it is ignored in the count.
        manifest_files.append({"name": "meta.json", "size": 5, "tar_offset": 9999})
        client = FakeS3Client(manifest_files, _tiny_jpeg(), self._assert_db_writable)

        with patch.object(image_cache, "get_s3_client", return_value=client):
            rows = ensure_run_images(self.dataset, "run-1")

        # No probe was ever blocked -> no write lock held during any network call.
        self.assertEqual(self.lock_violations, [], f"write lock held during network: {self.lock_violations}")

        # Selected and cached exactly the target number of images.
        self.assertEqual(len(rows), 3)
        self.assertEqual(client.range_calls, 3)
        for row in rows:
            self.assertTrue(row["cache_path"], "row should have a cache_path")
            self.assertTrue((ROOT_DIR / row["cache_path"]).exists(), "cached JPEG should exist on disk")

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            run = conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
        # total_image_count counts only image members (10), not meta.json.
        self.assertEqual(run["total_image_count"], 10)

    def test_recache_missing_file_refetches_without_reselecting(self) -> None:
        manifest_files = [
            {"name": f"img-{i}.jpg", "size": 100, "tar_offset": i * 100}
            for i in range(10)
        ]
        client = FakeS3Client(manifest_files, _tiny_jpeg(), self._assert_db_writable)

        with patch.object(image_cache, "get_s3_client", return_value=client):
            rows = ensure_run_images(self.dataset, "run-1")
            # Delete one cached file on disk, mimicking a reclaimed JPEG.
            victim = rows[0]
            (ROOT_DIR / victim["cache_path"]).unlink()
            self.assertFalse((ROOT_DIR / victim["cache_path"]).exists())

            calls_before = client.range_calls
            rows2 = ensure_run_images(self.dataset, "run-1")

        self.assertEqual(self.lock_violations, [])
        # Exactly one image was re-fetched (the deleted one), nothing re-selected.
        self.assertEqual(client.range_calls - calls_before, 1)
        self.assertEqual({r["member_name"] for r in rows}, {r["member_name"] for r in rows2})
        self.assertTrue((ROOT_DIR / victim["cache_path"]).exists())


if __name__ == "__main__":
    unittest.main()

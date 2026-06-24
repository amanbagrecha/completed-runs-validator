"""End-to-end test of the draft-submission path through the real route handlers.

Builds the real router over a temp DB (no prod data) and invokes the actual
endpoint callables -- exercising pydantic validation, the ``write_conn`` submit
operation, run completion, and background sheet-writeback scheduling. The
network writeback is stubbed so nothing leaves the process. (Uses the handler
callables directly rather than an HTTP client to avoid an httpx dev dependency.)
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request

import app.routes as routes_module
from app.db import get_conn, init_db
from app.routes import ReviewSubmissionRequest, ValidationItem, ValidationRequest, create_router


def _make_dataset(db_path: Path):
    from app.config import DatasetConfig

    return DatasetConfig(
        slug="test",
        label="Test",
        page_path="/test",
        api_prefix="/api",
        db_path=db_path,
        cache_dir=db_path.parent / "cache",
        aws_profile="test",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        batch_prefixes=[],
    )


def _request(user: str | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/review/submit",
        "headers": [],
        "query_string": b"",
        "state": {},
    }
    req = Request(scope)
    if user is not None:
        req.state.user = user
    return req


def _run_background(tasks: BackgroundTasks) -> None:
    for task in tasks.tasks:
        task.func(*task.args, **task.kwargs)


class SubmitRouteE2ETests(unittest.TestCase):
    TARGET = 3

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "app.db"

        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO runs (run_id, batch_name, tar_key, source_scope, image_target_count) "
                "VALUES (?, ?, ?, ?, ?)",
                ("run-1", "batch-1", "batch-1/run-1.tar", "wasabi", self.TARGET),
            )
            self.image_ids = []
            for i in range(self.TARGET):
                cur = conn.execute(
                    "INSERT INTO run_images (run_id, selection_version, image_index, member_name, cache_path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("run-1", 1, i, f"img-{i}.jpg", f"data/cache/test/{i}.jpg"),
                )
                self.image_ids.append(int(cur.lastrowid))
            conn.commit()

        router = _make_router(self.db_path)
        self.submit = _endpoint(router, "/api/review/submit")
        self.save_validations = _endpoint(router, "/api/validations")

    def test_review_submit_saves_drafts_and_completes_run(self) -> None:
        payload = ReviewSubmissionRequest(
            items=[ValidationItem(image_id=iid, status="pass") for iid in self.image_ids]
        )
        bg = BackgroundTasks()
        with patch.object(routes_module, "write_run_batch", return_value={}) as wrb:
            result = self.submit(payload, _request("tester"), bg)
            _run_background(bg)

        self.assertEqual(result["saved"], self.TARGET)
        self.assertEqual(result["completed_runs"], 1)
        self.assertEqual(result["sheet_sync"]["scheduled"], 1)
        # Background sheet writeback ran (stubbed -> no network).
        self.assertTrue(wrb.called)

        with get_conn(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) FROM image_validations").fetchone()[0]
            run = conn.execute(
                "SELECT validation_completed_at, validation_completed_by FROM runs WHERE run_id = 'run-1'"
            ).fetchone()
        self.assertEqual(n, self.TARGET)
        self.assertIsNotNone(run["validation_completed_at"])
        self.assertEqual(run["validation_completed_by"], "tester")

    def test_review_submit_is_idempotent_upsert(self) -> None:
        with patch.object(routes_module, "write_run_batch", return_value={}):
            bg = BackgroundTasks()
            self.submit(
                ReviewSubmissionRequest(
                    items=[ValidationItem(image_id=self.image_ids[0], status="fail", notes="bad")]
                ),
                _request("tester"),
                bg,
            )
            bg2 = BackgroundTasks()
            self.submit(
                ReviewSubmissionRequest(
                    items=[ValidationItem(image_id=self.image_ids[0], status="pass")]
                ),
                _request("tester"),
                bg2,
            )

        with get_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status FROM image_validations WHERE run_image_id = ?",
                (self.image_ids[0],),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pass")

    def test_validations_endpoint_saves(self) -> None:
        payload = ValidationRequest(
            items=[ValidationItem(image_id=iid, status="pass") for iid in self.image_ids[:2]]
        )
        result = self.save_validations(payload)
        self.assertEqual(result["saved"], 2)

    def test_review_submit_rejects_unknown_image(self) -> None:
        payload = ReviewSubmissionRequest(items=[ValidationItem(image_id=999999, status="pass")])
        with self.assertRaises(HTTPException) as ctx:
            self.submit(payload, _request("tester"), BackgroundTasks())
        self.assertEqual(ctx.exception.status_code, 400)


def _make_router(db_path: Path):
    return create_router(_make_dataset(db_path))


def _endpoint(router, path: str):
    for route in router.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route {path} not found")


if __name__ == "__main__":
    unittest.main()

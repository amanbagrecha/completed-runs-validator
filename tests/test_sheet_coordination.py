from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.config import WASABI_DATASET
from app.db import init_db
from app.services.sheets import SheetRun, sheet_run_is_globally_completed
from app.services.sync import sync_runs


@dataclass(frozen=True)
class FakeS3Object:
    batch_name: str = "batch-11"
    key: str = "batch-11/run-1.tar"
    prefix: str = "batch-11/"
    size: int = 123
    last_modified: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SheetCoordinationTests(unittest.TestCase):
    def test_existing_validation_column_marks_run_complete_without_using_app_columns(self) -> None:
        row = SheetRun(
            run_id="run-1",
            sheet_count=10,
            vehicle_type="car",
            locality_name=None,
            locality_category=None,
            region_id=None,
            subtype_label=None,
            dispatch_hold=None,
            pipeline_status="pipeline_succeeded",
            sheet_validation="approved",
            compltd_status=None,
            compltd_validator=None,
            compltd_started_at=None,
            compltd_completed_at=None,
            compltd_outcome=None,
            compltd_reviewed_images=None,
            compltd_failed_images=None,
            compltd_updated_at=None,
        )

        self.assertTrue(sheet_run_is_globally_completed(row))

    def test_compltd_completed_column_marks_run_complete(self) -> None:
        row = SheetRun(
            run_id="run-1",
            sheet_count=10,
            vehicle_type="car",
            locality_name=None,
            locality_category=None,
            region_id=None,
            subtype_label=None,
            dispatch_hold=None,
            pipeline_status="pipeline_succeeded",
            sheet_validation=None,
            compltd_status="completed",
            compltd_validator="alice",
            compltd_started_at=None,
            compltd_completed_at="2026-01-01T00:00:00Z",
            compltd_outcome="approved",
            compltd_reviewed_images=6,
            compltd_failed_images=0,
            compltd_updated_at="2026-01-01T00:00:00Z",
        )

        self.assertTrue(sheet_run_is_globally_completed(row))

    def test_sync_persists_sheet_coordination_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "app.db"
            init_db(db_path)
            dataset = WASABI_DATASET.__class__(
                slug=WASABI_DATASET.slug,
                label=WASABI_DATASET.label,
                page_path=WASABI_DATASET.page_path,
                api_prefix=WASABI_DATASET.api_prefix,
                db_path=db_path,
                cache_dir=WASABI_DATASET.cache_dir,
                aws_profile=WASABI_DATASET.aws_profile,
                aws_region=WASABI_DATASET.aws_region,
                s3_bucket=WASABI_DATASET.s3_bucket,
                batch_prefixes=WASABI_DATASET.batch_prefixes,
            )
            sheet_row = SheetRun(
                run_id="run-1",
                sheet_count=10,
                vehicle_type="car",
                locality_name="Town",
                locality_category="town",
                region_id="R1",
                subtype_label="subtype",
                dispatch_hold=None,
                pipeline_status="pipeline_succeeded",
                sheet_validation="retry",
                compltd_status="completed",
                compltd_validator="alice",
                compltd_started_at="2026-01-01T00:00:00Z",
                compltd_completed_at="2026-01-01T00:10:00Z",
                compltd_outcome="retry",
                compltd_reviewed_images=12,
                compltd_failed_images=2,
                compltd_updated_at="2026-01-01T00:10:00Z",
            )

            with patch("app.services.sync.fetch_done_runs", return_value=[sheet_row]), patch(
                "app.services.sync.list_run_tars", return_value={"run-1": FakeS3Object()}
            ):
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                try:
                    sync_runs(conn, dataset)
                    conn.commit()
                    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", ("run-1",)).fetchone()
                finally:
                    conn.close()

        self.assertEqual(row["sheet_validation"], "retry")
        self.assertEqual(row["compltd_status"], "completed")
        self.assertEqual(row["compltd_validator"], "alice")
        self.assertEqual(row["compltd_reviewed_images"], 12)
        self.assertEqual(row["compltd_failed_images"], 2)


if __name__ == "__main__":
    unittest.main()

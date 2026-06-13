from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import init_db
from app.services.sheet_writeback import (
    DEFAULT_SHEET_TITLE,
    CompletionWriteback,
    GoogleSheetClient,
    sync_all_completions,
    write_run_completion,
)


class FakeSheetClient:
    def __init__(self) -> None:
        self.values = [
            ["folder", "validation"],
            ["run-1", "approved"],
        ]
        self.header_writes: list[tuple[str, list[list[str]]]] = []
        self.cell_writes: list[tuple[str, list[list[str]]]] = []

    def read_values(self) -> list[list[str]]:
        return self.values

    def write_values(self, cell_range: str, values: list[list[str]]) -> None:
        self.header_writes.append((cell_range, values))
        headers = values[0]
        self.values[0].extend(headers)

    def batch_write_values(self, updates: list[tuple[str, list[list[str]]]]) -> None:
        self.cell_writes.extend(updates)


class SheetWritebackTests(unittest.TestCase):
    def test_completion_writeback_appends_and_updates_only_app_owned_columns(self) -> None:
        client = FakeSheetClient()

        result = write_run_completion(
            CompletionWriteback(
                run_id="run-1",
                validator="alice",
                completed_at="2026-01-01T00:10:00Z",
                outcome="approved",
                reviewed_images=6,
                failed_images=0,
            ),
            client=client,
        )

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.row_number, 2)
        self.assertTrue(client.header_writes)
        self.assertIn("compltd_status", client.values[0])
        self.assertIn("compltd_validator", client.values[0])
        self.assertNotIn("validation", client.header_writes[0][1][0])
        written_ranges = [cell_range for cell_range, _ in client.cell_writes]
        self.assertNotIn("B2", written_ranges)
        self.assertTrue(all(not cell_range.startswith("B") for cell_range in written_ranges))

    def test_google_client_falls_back_to_configured_default_title_when_gid_differs(self) -> None:
        class MetadataOnlyClient(GoogleSheetClient):
            def _request(self, method, path, payload=None):
                return {
                    "sheets": [
                        {"properties": {"sheetId": 123, "title": "archive"}},
                        {"properties": {"sheetId": 456, "title": DEFAULT_SHEET_TITLE}},
                    ]
                }

        client = MetadataOnlyClient("spreadsheet", 999, Path("credentials.json"))

        self.assertEqual(client._sheet_title_value(), DEFAULT_SHEET_TITLE)

    def test_sync_all_completions_stores_completion_timestamp_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "app.db"
            init_db(db_path)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, batch_name, tar_key, source_scope,
                        validation_completed_at, validation_completed_selection_version,
                        validation_completed_image_target_count
                    ) VALUES ('run-1', 'batch-01', 'batch-01/run-1.tar', 'batch-01/',
                        '2026-01-01T00:00:00Z', 1, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO run_images (run_id, selection_version, image_index, member_name)
                    VALUES ('run-1', 1, 0, 'image.jpg')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO image_validations (run_image_id, run_id, selection_version, status)
                    VALUES (1, 'run-1', 1, 'pass')
                    """
                )

                with patch("app.services.sheet_writeback._utc_now", return_value="2026-01-01T00:10:00Z"):
                    summary = sync_all_completions(conn, "alice", client=FakeSheetClient())

                row = conn.execute(
                    "SELECT compltd_status, compltd_validator, compltd_completed_at FROM runs WHERE run_id = 'run-1'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(summary, {"updated": 1})
        self.assertEqual(row["compltd_status"], "completed")
        self.assertEqual(row["compltd_validator"], "alice")
        self.assertEqual(row["compltd_completed_at"], "2026-01-01T00:10:00Z")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import DatasetConfig, ROOT_DIR
from app.services.s3_index import list_run_tars


class FakePaginator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return [
            {
                "Contents": [
                    {"Key": "batch-01/run-a.tar", "Size": 10, "LastModified": None},
                    {"Key": "batch-01/run-a.manifest.json", "Size": 1, "LastModified": None},
                ],
                "CommonPrefixes": [{"Prefix": "batch-01/logs/"}],
            }
        ]


class FakeClient:
    def __init__(self, paginator: FakePaginator) -> None:
        self.paginator = paginator

    def get_paginator(self, name: str) -> FakePaginator:
        self.paginator_name = name
        return self.paginator


class S3IndexTests(unittest.TestCase):
    def test_list_run_tars_uses_non_recursive_batch_listing(self) -> None:
        paginator = FakePaginator()
        dataset = DatasetConfig(
            slug="test",
            label="Test",
            page_path="/test",
            api_prefix="/test/api",
            db_path=ROOT_DIR / "data" / "test.db",
            cache_dir=ROOT_DIR / "data" / "cache" / "test",
            aws_profile="test",
            aws_region="us-east-1",
            s3_bucket="bucket",
            batch_prefixes=[("batch-01", "batch-01/")],
        )

        with patch("app.services.s3_index.get_s3_client", return_value=FakeClient(paginator)):
            runs = list_run_tars(dataset)

        self.assertEqual(set(runs), {"run-a"})
        self.assertEqual(paginator.calls[0]["Bucket"], "bucket")
        self.assertEqual(paginator.calls[0]["Prefix"], "batch-01/")
        self.assertEqual(paginator.calls[0]["Delimiter"], "/")


if __name__ == "__main__":
    unittest.main()

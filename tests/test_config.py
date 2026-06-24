from __future__ import annotations

import unittest

from app.config import UI_DATASETS, WASABI_DATASET


class ConfigTests(unittest.TestCase):
    def test_wasabi_batches_are_contiguous_from_one(self) -> None:
        batch_names = [name for name, _ in WASABI_DATASET.batch_prefixes]

        self.assertEqual(batch_names[0], "batch-01")
        # Batches grow over time; assert the list stays a contiguous batch-01..N
        # zero-padded sequence rather than pinning an exact count.
        expected = [f"batch-{i:02d}" for i in range(1, len(batch_names) + 1)]
        self.assertEqual(batch_names, expected)

    def test_ui_datasets_hide_aws(self) -> None:
        self.assertEqual([dataset.slug for dataset in UI_DATASETS], ["wasabi"])


if __name__ == "__main__":
    unittest.main()

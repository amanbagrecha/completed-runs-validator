from __future__ import annotations

import unittest

from app.config import UI_DATASETS, WASABI_DATASET


class ConfigTests(unittest.TestCase):
    def test_wasabi_batches_cover_one_through_fifty_seven(self) -> None:
        batch_names = [name for name, _ in WASABI_DATASET.batch_prefixes]

        self.assertEqual(batch_names[0], "batch-01")
        self.assertEqual(batch_names[-1], "batch-57")
        self.assertEqual(len(batch_names), 57)

    def test_ui_datasets_hide_aws(self) -> None:
        self.assertEqual([dataset.slug for dataset in UI_DATASETS], ["wasabi"])


if __name__ == "__main__":
    unittest.main()

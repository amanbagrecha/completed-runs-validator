import argparse
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AWS_DATASET, DATASETS, WASABI_DATASET
from app.db import get_conn, init_db
from app.services.sync import sync_runs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync a dataset into its local SQLite database")
    parser.add_argument("--dataset", choices=[dataset.slug for dataset in DATASETS], default=WASABI_DATASET.slug)
    return parser.parse_args()


def get_dataset(slug: str):
    if slug == AWS_DATASET.slug:
        return AWS_DATASET
    return WASABI_DATASET


def main() -> None:
    args = parse_args()
    dataset = get_dataset(args.dataset)

    init_db(dataset.db_path)
    with get_conn(dataset.db_path) as conn:
        summary = sync_runs(conn, dataset)

    print(f"sheet_runs={summary.sheet_runs}")
    print(f"s3_runs={summary.s3_runs}")
    print(f"indexed_runs={summary.indexed_runs}")
    print(f"missing_in_s3={len(summary.missing_in_s3)}")
    print(f"extra_in_s3={len(summary.extra_in_s3)}")
    if summary.missing_in_s3:
        print("first_missing=" + ",".join(summary.missing_in_s3[:10]))
    if summary.extra_in_s3:
        print("first_extra=" + ",".join(summary.extra_in_s3[:10]))


if __name__ == "__main__":
    main()

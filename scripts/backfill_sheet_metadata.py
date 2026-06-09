from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AWS_DATASET, DATASETS, WASABI_DATASET, DatasetConfig
from app.db import init_db


DEFAULT_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1mfeHbvlI0CrT54WUC_stFTuXqg4a9a7YeVqZ5wHFbo8/"
    "gviz/tq?tqx=out:csv&gid=514978031"
)

RUN_METADATA_COLUMNS = {
    "locality_name": "TEXT",
    "locality_category": "TEXT",
    "region_id": "TEXT",
    "subtype_label": "TEXT",
    "dispatch_hold": "TEXT",
    "pipeline_status": "TEXT",
}


@dataclass(frozen=True)
class SheetMetadata:
    run_id: str
    locality_name: str | None
    locality_category: str | None
    region_id: str | None
    subtype_label: str | None
    dispatch_hold: str | None
    pipeline_status: str | None


@dataclass(frozen=True)
class BackfillResult:
    dataset: str
    db_path: Path
    total_runs: int
    matched_runs: int
    unmatched_runs: int
    changed_runs: int
    blank_locality_category: int
    missing_columns: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill run locality metadata from the completed-runs Google Sheet"
    )
    parser.add_argument(
        "--dataset",
        choices=["all", *(dataset.slug for dataset in DATASETS)],
        default="all",
        help="Dataset database to update. Defaults to both Wasabi and AWS.",
    )
    parser.add_argument("--sheet-url", default=DEFAULT_SHEET_CSV_URL)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without altering database files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sheet_rows = fetch_sheet_metadata(args.sheet_url)
    datasets = selected_datasets(args.dataset)

    print(f"sheet_rows={len(sheet_rows)}")
    print(f"datasets={','.join(dataset.slug for dataset in datasets)}")
    print(f"dry_run={args.dry_run}")

    for dataset in datasets:
        result = backfill_dataset(dataset, sheet_rows, dry_run=args.dry_run)
        print_result(result)


def selected_datasets(slug: str) -> tuple[DatasetConfig, ...]:
    if slug == "all":
        return DATASETS
    if slug == AWS_DATASET.slug:
        return (AWS_DATASET,)
    return (WASABI_DATASET,)


def fetch_sheet_metadata(url: str) -> dict[str, SheetMetadata]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        text = response.read().decode("utf-8", errors="replace")

    metadata_by_run_id: dict[str, SheetMetadata] = {}
    for row in csv.DictReader(io.StringIO(text)):
        run_id = clean_value(row.get("folder"))
        if not run_id or run_id in metadata_by_run_id:
            continue
        metadata_by_run_id[run_id] = SheetMetadata(
            run_id=run_id,
            locality_name=clean_value(row.get("locality_name")),
            locality_category=clean_value(row.get("locality_category")),
            region_id=clean_value(row.get("region_id")),
            subtype_label=clean_value(row.get("subtype_label")),
            dispatch_hold=clean_value(row.get("dispatch_hold")),
            pipeline_status=clean_value(row.get("pipeline_status")),
        )
    return metadata_by_run_id


def clean_value(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def backfill_dataset(
    dataset: DatasetConfig,
    sheet_rows: dict[str, SheetMetadata],
    *,
    dry_run: bool,
) -> BackfillResult:
    if not dry_run:
        init_db(dataset.db_path)

    conn = sqlite3.connect(dataset.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        missing_columns = missing_run_metadata_columns(conn)
        if missing_columns and not dry_run:
            add_run_metadata_columns(conn, missing_columns)

        total_runs = conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"]
        run_rows = conn.execute(
            """
            SELECT run_id, locality_name, locality_category, region_id,
                   subtype_label, dispatch_hold, pipeline_status
            FROM runs
            """
            if not missing_columns
            else "SELECT run_id FROM runs"
        ).fetchall()

        matched_runs = 0
        changed_runs = 0
        blank_locality_category = 0
        for row in run_rows:
            metadata = sheet_rows.get(row["run_id"])
            if not metadata:
                continue
            matched_runs += 1
            if not metadata.locality_category:
                blank_locality_category += 1
            if row_needs_update(row, metadata, missing_columns):
                changed_runs += 1
                if not dry_run:
                    update_run_metadata(conn, metadata)

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

        return BackfillResult(
            dataset=dataset.slug,
            db_path=dataset.db_path,
            total_runs=total_runs,
            matched_runs=matched_runs,
            unmatched_runs=total_runs - matched_runs,
            changed_runs=changed_runs,
            blank_locality_category=blank_locality_category,
            missing_columns=tuple(missing_columns),
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def missing_run_metadata_columns(conn: sqlite3.Connection) -> list[str]:
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    return [column for column in RUN_METADATA_COLUMNS if column not in existing_columns]


def add_run_metadata_columns(conn: sqlite3.Connection, missing_columns: list[str]) -> None:
    for column in missing_columns:
        conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {RUN_METADATA_COLUMNS[column]}")


def row_needs_update(row: sqlite3.Row, metadata: SheetMetadata, missing_columns: list[str]) -> bool:
    if missing_columns:
        return True
    return any(
        (row[column] or None) != getattr(metadata, column)
        for column in RUN_METADATA_COLUMNS
    )


def update_run_metadata(conn: sqlite3.Connection, metadata: SheetMetadata) -> None:
    conn.execute(
        """
        UPDATE runs
        SET locality_name = ?,
            locality_category = ?,
            region_id = ?,
            subtype_label = ?,
            dispatch_hold = ?,
            pipeline_status = ?
        WHERE run_id = ?
        """,
        (
            metadata.locality_name,
            metadata.locality_category,
            metadata.region_id,
            metadata.subtype_label,
            metadata.dispatch_hold,
            metadata.pipeline_status,
            metadata.run_id,
        ),
    )


def print_result(result: BackfillResult) -> None:
    print(f"\n[{result.dataset}]")
    print(f"db_path={result.db_path}")
    print(f"total_runs={result.total_runs}")
    print(f"matched_runs={result.matched_runs}")
    print(f"unmatched_runs={result.unmatched_runs}")
    print(f"changed_runs={result.changed_runs}")
    print(f"blank_locality_category={result.blank_locality_category}")
    if result.missing_columns:
        print("missing_columns=" + ",".join(result.missing_columns))


if __name__ == "__main__":
    main()

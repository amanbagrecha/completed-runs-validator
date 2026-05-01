from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import get_conn, init_db
from app.services.sync import sync_runs


def main() -> None:
    init_db()
    with get_conn() as conn:
        summary = sync_runs(conn)

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

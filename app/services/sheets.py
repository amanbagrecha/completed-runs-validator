import csv
import io
import urllib.request
from dataclasses import dataclass

from app.config import SHEET_CSV_URL


@dataclass(frozen=True)
class SheetRun:
    run_id: str
    sheet_count: int | None
    vehicle_type: str | None


def fetch_done_runs() -> list[SheetRun]:
    with urllib.request.urlopen(SHEET_CSV_URL, timeout=45) as response:
        text = response.read().decode("utf-8", errors="replace")

    rows = csv.DictReader(io.StringIO(text))
    runs: list[SheetRun] = []
    seen: set[str] = set()
    for row in rows:
        run_id = (row.get("run_name") or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        count_text = (row.get("count") or "").strip()
        sheet_count = int(float(count_text)) if count_text else None
        vehicle_type = (row.get("vehicle_type") or "").strip() or None
        runs.append(SheetRun(run_id, sheet_count, vehicle_type))
    return runs

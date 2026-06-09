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
    locality_name: str | None
    locality_category: str | None
    region_id: str | None
    subtype_label: str | None
    dispatch_hold: str | None
    pipeline_status: str | None


def fetch_done_runs() -> list[SheetRun]:
    request = urllib.request.Request(SHEET_CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        text = response.read().decode("utf-8", errors="replace")

    rows = csv.DictReader(io.StringIO(text))
    runs: list[SheetRun] = []
    seen: set[str] = set()
    for row in rows:
        run_id = (row.get("folder") or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        count_text = (row.get("wasabi_count") or "").strip()
        sheet_count = int(float(count_text)) if count_text else None
        vehicle_type = (row.get("vehicle") or "").strip() or None
        runs.append(
            SheetRun(
                run_id=run_id,
                sheet_count=sheet_count,
                vehicle_type=vehicle_type,
                locality_name=_clean(row.get("locality_name")),
                locality_category=_clean(row.get("locality_category")),
                region_id=_clean(row.get("region_id")),
                subtype_label=_clean(row.get("subtype_label")),
                dispatch_hold=_clean(row.get("dispatch_hold")),
                pipeline_status=_clean(row.get("pipeline_status")),
            )
        )
    return runs


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None

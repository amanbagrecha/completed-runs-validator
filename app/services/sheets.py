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
    sheet_validation: str | None
    compltd_status: str | None
    compltd_validator: str | None
    compltd_started_at: str | None
    compltd_completed_at: str | None
    compltd_outcome: str | None
    compltd_reviewed_images: int | None
    compltd_failed_images: int | None
    compltd_updated_at: str | None


# Both 'approved' and 'retry' are resolved values upstream: a retried run was already
# validated, so the app treats it as complete and does not re-serve it for review (it
# is revisited at the end of the pass, not redone now).
EXISTING_SHEET_COMPLETE_VALUES = {"approved", "retry"}
COMPLTD_COMPLETE_STATUS = "completed"


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
                sheet_validation=_clean(row.get("validation")),
                compltd_status=_clean(row.get("compltd_status")),
                compltd_validator=_clean(row.get("compltd_validator")),
                compltd_started_at=_clean(row.get("compltd_started_at")),
                compltd_completed_at=_clean(row.get("compltd_completed_at")),
                compltd_outcome=_clean(row.get("compltd_outcome")),
                compltd_reviewed_images=_clean_int(row.get("compltd_reviewed_images")),
                compltd_failed_images=_clean_int(row.get("compltd_failed_images")),
                compltd_updated_at=_clean(row.get("compltd_updated_at")),
            )
        )
    return runs


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _clean_int(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(float(value)) if value else None


def sheet_run_is_globally_completed(run: SheetRun) -> bool:
    return is_existing_sheet_validation_complete(run.sheet_validation) or is_compltd_completed(run.compltd_status)


def is_existing_sheet_validation_complete(value: str | None) -> bool:
    return _normalized(value) in EXISTING_SHEET_COMPLETE_VALUES


def is_compltd_completed(value: str | None) -> bool:
    return _normalized(value) == COMPLTD_COMPLETE_STATUS


def _normalized(value: str | None) -> str:
    return (value or "").strip().lower()

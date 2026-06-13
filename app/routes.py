from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.templating import Jinja2Templates

from app.config import DATASETS, DEFAULT_IMAGE_COUNT, DatasetConfig, ROOT_DIR
from app.auth import authenticate, clear_auth_cookie, set_auth_cookie
from app.db import get_conn
from app.services.image_cache import (
    append_run_images,
    count_prefetched_images,
    ensure_manifest_image_count,
    ensure_run_images,
    ensure_run_images_to_count,
    get_image_file_path,
    prefetch_target_for_total,
    queue_run_image_ensure,
    queue_run_image_prefetch,
    review_image_target,
)
from app.services.sheets import is_compltd_completed, is_existing_sheet_validation_complete
from app.services.sheet_writeback import CompletionWriteback, SheetWriteResult, write_run_completion
from app.services.sync import sync_runs
from app.services.validations import complete_run_validation, maybe_complete_run_validation, submit_validations


templates = Jinja2Templates(directory=str(ROOT_DIR / "app" / "templates"))
auth_router = APIRouter()

LOCALITY_CATEGORY_OPTIONS = [
    ("major_city", "Major cities"),
    ("city", "Cities"),
    ("town", "Towns"),
    ("rural", "Rural"),
    ("unknown", "Unknown"),
]


@auth_router.get("/login")
def login_page(request: Request, next: str = Query("/review")):
    if getattr(request.state, "user", None):
        return RedirectResponse(_normalize_next_path(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "next_path": _normalize_next_path(next)},
    )


@auth_router.post("/login")
async def login_submit(request: Request):
    body = parse_qs((await request.body()).decode("utf-8"))
    username = body.get("username", [""])[0].strip()
    password = body.get("password", [""])[0]
    next_path = _normalize_next_path(body.get("next_path", ["/review"])[0])
    if authenticate(username, password):
        response = RedirectResponse(next_path, status_code=303)
        set_auth_cookie(response, username)
        return response
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Invalid username or password", "next_path": next_path},
        status_code=401,
    )


@auth_router.get("/logout")
@auth_router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    clear_auth_cookie(response)
    return response


def _normalize_next_path(next_path: str) -> str:
    next_path = (next_path or "/review").strip()
    if not next_path.startswith("/") or next_path.startswith("//"):
        return "/review"
    if next_path.startswith("/login") or next_path.startswith("/logout"):
        return "/review"
    return next_path


class ValidationItem(BaseModel):
    image_id: int
    status: Literal["pass", "fail"]
    notes: str = ""


class ValidationRequest(BaseModel):
    items: list[ValidationItem]


class ReviewSubmissionRequest(BaseModel):
    items: list[ValidationItem]


def _report_page_path(dataset: DatasetConfig) -> str:
    return "/reports" if dataset.slug == "wasabi" else f"{dataset.page_path}/reports"


def _review_page_path(dataset: DatasetConfig) -> str:
    return "/review" if dataset.slug == "wasabi" else f"{dataset.page_path}/review"


def create_router(dataset: DatasetConfig) -> APIRouter:
    router = APIRouter()
    nav_pages = [{"label": item.label, "path": item.page_path} for item in DATASETS]
    review_pages = [{"label": f"{item.label} Review", "path": _review_page_path(item)} for item in DATASETS]
    report_pages = [{"label": f"{item.label} Reports", "path": _report_page_path(item)} for item in DATASETS]
    reports_page_path = _report_page_path(dataset)
    review_page_path = _review_page_path(dataset)

    @router.get(dataset.page_path)
    def index(request: Request):
        batch_names = [name for name, _ in dataset.batch_prefixes]
        with get_conn(dataset.db_path) as conn:
            latest_sync = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
            batch_counts = {
                row["batch_name"]: row["count"]
                for row in conn.execute("SELECT batch_name, COUNT(*) AS count FROM runs GROUP BY batch_name")
            }
            locality_categories = _locality_category_options(conn)
        batches = [{"name": name, "count": batch_counts.get(name, 0)} for name in batch_names]
        all_batch_count = sum(batch_counts.get(name, 0) for name in batch_names)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "batches": batches,
                "locality_categories": locality_categories,
                "all_batch_count": all_batch_count,
                "latest_sync": latest_sync,
                "image_count": DEFAULT_IMAGE_COUNT,
                "api_base": dataset.api_prefix,
                "current_page_path": dataset.page_path,
                "nav_pages": nav_pages,
                "page_label": dataset.label,
                "reports_page_path": reports_page_path,
                "current_user": getattr(request.state, "user", None),
            },
        )

    @router.get(review_page_path)
    def review_index(request: Request):
        batch_names = [name for name, _ in dataset.batch_prefixes]
        with get_conn(dataset.db_path) as conn:
            locality_categories = _locality_category_options(conn)
        return templates.TemplateResponse(
            request,
            "review.html",
            {
                "batches": batch_names,
                "locality_categories": locality_categories,
                "api_base": dataset.api_prefix,
                "current_page_path": review_page_path,
                "page_label": dataset.label,
                "review_pages": review_pages,
                "validator_page_path": dataset.page_path,
                "reports_page_path": reports_page_path,
                "current_user": getattr(request.state, "user", None),
            },
        )

    @router.get(reports_page_path)
    def reports_index(
        request: Request,
        batch: str = Query("all"),
        locality_category: str = Query("all"),
        vehicle_type: str = Query("all"),
        run_id: str = Query(""),
        limit: int = Query(25, ge=1, le=250),
        page: int = Query(1, ge=1),
    ):
        batch_names = [name for name, _ in dataset.batch_prefixes]
        offset = (page - 1) * limit
        with get_conn(dataset.db_path) as conn:
            batch_counts = {
                row["batch_name"]: row["count"]
                for row in conn.execute(
                    """
                    SELECT batch_name, COUNT(*) AS count
                    FROM runs
                    WHERE validation_completed_at IS NOT NULL
                    GROUP BY batch_name
                    """
                )
            }
            vehicle_types = [
                {"name": row["vehicle_type"], "count": row["count"]}
                for row in conn.execute(
                    """
                    SELECT vehicle_type, COUNT(*) AS count
                    FROM runs
                    WHERE validation_completed_at IS NOT NULL
                      AND vehicle_type IS NOT NULL
                      AND TRIM(vehicle_type) <> ''
                    GROUP BY vehicle_type
                    ORDER BY vehicle_type
                    """
                )
            ]
            completed_run_count = conn.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE validation_completed_at IS NOT NULL"
            ).fetchone()["count"]
            locality_categories = _locality_category_options(conn, completed_only=True)
            rows, total = _list_validation_report_rows(
                conn, dataset, batch, locality_category, vehicle_type, run_id, limit, offset
            )

        batches = [{"name": name, "count": batch_counts.get(name, 0)} for name in batch_names]
        total_pages = (total + limit - 1) // limit if total else 0
        current_page = page if total else 1
        report_rows = [_report_row_to_dict(row, dataset) for row in rows]
        return templates.TemplateResponse(
            request,
            "reports.html",
            {
                "batches": batches,
                "locality_categories": locality_categories,
                "vehicle_types": vehicle_types,
                "completed_run_count": completed_run_count,
                "validator_pages": nav_pages,
                "report_pages": report_pages,
                "validator_page_path": dataset.page_path,
                "current_report_path": reports_page_path,
                "page_label": dataset.label,
                "selected_batch": batch,
                "selected_locality_category": locality_category,
                "selected_vehicle_type": vehicle_type,
                "run_id_filter": run_id,
                "page_limit": limit,
                "current_page": current_page,
                "total_pages": total_pages,
                "report_rows": report_rows,
                "report_count_text": _report_count_text(total, current_page, total_pages, offset, len(report_rows)),
                "prev_page_url": _report_url(
                    reports_page_path,
                    batch,
                    locality_category,
                    vehicle_type,
                    run_id,
                    limit=limit,
                    page=max(1, current_page - 1),
                ) if current_page > 1 else None,
                "next_page_url": _report_url(
                    reports_page_path,
                    batch,
                    locality_category,
                    vehicle_type,
                    run_id,
                    limit=limit,
                    page=current_page + 1,
                ) if total_pages and current_page < total_pages else None,
                "download_csv_url": _report_url(
                    f"{dataset.api_prefix}/reports/validations.csv",
                    batch,
                    locality_category,
                    vehicle_type,
                    run_id,
                ),
                "current_user": getattr(request.state, "user", None),
            },
        )

    @router.post(f"{dataset.api_prefix}/sync")
    def sync_metadata():
        with get_conn(dataset.db_path) as conn:
            summary = sync_runs(conn, dataset)
        return {
            "sheet_runs": summary.sheet_runs,
            "s3_runs": summary.s3_runs,
            "indexed_runs": summary.indexed_runs,
            "missing_in_s3": len(summary.missing_in_s3),
            "extra_in_s3": len(summary.extra_in_s3),
            "missing_sample": summary.missing_in_s3[:10],
            "extra_sample": summary.extra_in_s3[:10],
        }

    @router.get(f"{dataset.api_prefix}/runs")
    def list_runs(
        batch: str = Query(...),
        run_id: str = Query(""),
        locality_category: str = Query("all"),
        status: Literal["all", "pending", "partial", "ready", "pass", "fail", "finished", "unfinished"] = Query("all"),
        limit: int = Query(10, ge=1, le=100),
        offset: int = Query(0, ge=0),
        page: int | None = Query(None, ge=1),
    ):
        if page is not None:
            offset = (page - 1) * limit

        batch_names = [name for name, _ in dataset.batch_prefixes]
        base_params: list[str | int] = []
        if batch == "all":
            placeholders = ", ".join("?" for _ in batch_names)
            where = [f"r.batch_name IN ({placeholders})"]
            base_params.extend(batch_names)
        else:
            where = ["r.batch_name = ?"]
            base_params.append(batch)
        if run_id.strip():
            where.append("r.run_id LIKE ?")
            base_params.append(f"%{run_id.strip()}%")
        _append_locality_category_filter(where, base_params, locality_category)
        _append_sheet_availability_filter(where)

        aggregate_sql = _run_status_cte(" AND ".join(where))

        status_where = ""
        status_params: list[str] = []
        if status in {"pending", "partial", "ready", "pass", "fail"}:
            status_where = "WHERE status = ?"
            status_params.append(status)
        elif status == "finished":
            status_where = "WHERE status IN ('pass', 'fail')"
        elif status == "unfinished":
            status_where = "WHERE status IN ('pending', 'partial', 'ready')"

        with get_conn(dataset.db_path) as conn:
            rows = conn.execute(
                aggregate_sql
                + f"""
                SELECT *
                FROM run_status
                {status_where}
                ORDER BY batch_name, run_id
                LIMIT ? OFFSET ?
                """,
                [*base_params, *status_params, limit, offset],
            ).fetchall()
            total = conn.execute(
                aggregate_sql
                + f"""
                SELECT COUNT(*) AS count
                FROM run_status
                {status_where}
                """,
                [*base_params, *status_params],
            ).fetchone()["count"]

        total_pages = (total + limit - 1) // limit if total else 0
        current_page = (offset // limit) + 1 if total else 1
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "page": current_page,
            "total_pages": total_pages,
            "status": status,
            "runs": [_run_to_dict(row) for row in rows],
        }

    @router.get(f"{dataset.api_prefix}/reports/validations.csv")
    def download_validation_reports_csv(
        batch: str = Query("all"),
        locality_category: str = Query("all"),
        vehicle_type: str = Query("all"),
        run_id: str = Query(""),
    ):
        with get_conn(dataset.db_path) as conn:
            rows, _ = _list_validation_report_rows(conn, dataset, batch, locality_category, vehicle_type, run_id, None, 0)
        return _validation_report_csv_response(dataset, [_report_row_to_dict(row, dataset) for row in rows])

    @router.get(f"{dataset.api_prefix}/runs/{{run_id}}/images")
    def get_run_images(run_id: str):
        try:
            with get_conn(dataset.db_path) as conn:
                _ensure_run_available(conn, run_id)
                images = ensure_run_images(conn, dataset, run_id)
                ensure_manifest_image_count(conn, dataset, run_id)
                run = _fetch_run_status(conn, run_id)
                prefetched_images = count_prefetched_images(conn, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        prefetch_status = _queue_prefetch_if_possible(dataset, run)
        return {
            "run": _run_detail_to_dict(run, len(images), prefetched_images),
            "prefetch_status": prefetch_status,
            "images": [_image_to_dict(row, dataset.api_prefix) for row in images],
        }

    @router.post(f"{dataset.api_prefix}/runs/{{run_id}}/refresh-images")
    def refresh_run_images(run_id: str):
        try:
            with get_conn(dataset.db_path) as conn:
                _ensure_run_available(conn, run_id)
                ensure_manifest_image_count(conn, dataset, run_id)
                images = append_run_images(conn, dataset, run_id, DEFAULT_IMAGE_COUNT)
                run = _fetch_run_status(conn, run_id)
                prefetched_images = count_prefetched_images(conn, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        prefetch_status = _queue_prefetch_if_possible(dataset, run)
        return {
            "run": _run_detail_to_dict(run, len(images), prefetched_images),
            "prefetch_status": prefetch_status,
            "images": [_image_to_dict(row, dataset.api_prefix) for row in images],
        }

    @router.post(f"{dataset.api_prefix}/runs/{{run_id}}/complete")
    def complete_run(run_id: str, request: Request):
        try:
            with get_conn(dataset.db_path) as conn:
                complete_run_validation(conn, run_id, getattr(request.state, "user", None))
                sheet_sync = _sync_completion_to_sheet(conn, run_id, getattr(request.state, "user", None))
                run = _fetch_run_status(conn, run_id)
        except ValueError as exc:
            status_code = 404 if str(exc).startswith("Unknown run_id") else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {"run": _run_to_dict(run), "sheet_sync": sheet_sync.status}

    @router.get(f"{dataset.api_prefix}/images/{{image_id}}/file")
    def image_file(image_id: int):
        with get_conn(dataset.db_path) as conn:
            path = get_image_file_path(conn, image_id)
        if not path:
            raise HTTPException(status_code=404, detail="Cached image not found")
        return FileResponse(path, media_type="image/jpeg")

    @router.post(f"{dataset.api_prefix}/validations")
    def save_validations(payload: ValidationRequest):
        if not payload.items:
            raise HTTPException(status_code=400, detail="No validation items submitted")
        try:
            with get_conn(dataset.db_path) as conn:
                saved = submit_validations(
                    conn,
                    [{"image_id": item.image_id, "status": item.status, "notes": item.notes} for item in payload.items],
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"saved": saved}

    @router.get(f"{dataset.api_prefix}/review/images")
    def list_review_images(
        batch: str = Query("all"),
        run_id: str = Query(""),
        locality_category: str = Query("all"),
        state: Literal["unreviewed", "submitted", "pass", "fail", "all"] = Query("unreviewed"),
    ):
        try:
            with get_conn(dataset.db_path) as conn:
                skipped_runs, pending_runs = _prepare_review_images(conn, dataset, batch, run_id, locality_category, state)
                rows = _list_review_image_rows(conn, dataset, batch, run_id, locality_category, state, skipped_runs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {
            "images": [_review_image_to_dict(row, dataset.api_prefix) for row in rows],
            "pending_runs": pending_runs,
            "skipped_runs": len(skipped_runs),
            "skipped_sample": skipped_runs[:10],
        }

    @router.get(f"{dataset.api_prefix}/review/stats")
    def review_stats(
        batch: str = Query("all"),
        run_id: str = Query(""),
        locality_category: str = Query("all"),
    ):
        with get_conn(dataset.db_path) as conn:
            return _review_stats(conn, dataset, batch, run_id, locality_category)

    @router.post(f"{dataset.api_prefix}/review/submit")
    def submit_review_image(payload: ReviewSubmissionRequest, request: Request):
        if not payload.items:
            raise HTTPException(status_code=400, detail="No review items submitted")

        try:
            with get_conn(dataset.db_path) as conn:
                run_ids = []
                seen_run_ids: set[str] = set()
                items = []
                for item in payload.items:
                    image_row = conn.execute(
                        """
                        SELECT ri.id, ri.run_id
                        FROM run_images ri
                        JOIN runs r ON r.run_id = ri.run_id AND r.selection_version = ri.selection_version
                        WHERE ri.id = ?
                        """,
                        (item.image_id,),
                    ).fetchone()
                    if not image_row:
                        raise ValueError(f"Unknown image_id: {item.image_id}")
                    if image_row["run_id"] not in seen_run_ids:
                        seen_run_ids.add(image_row["run_id"])
                        run_ids.append(image_row["run_id"])
                    items.append({"image_id": item.image_id, "status": item.status, "notes": item.notes})

                saved = submit_validations(
                    conn,
                    items,
                )
                completed_runs = 0
                sheet_sync_results: list[SheetWriteResult] = []
                for run_id in run_ids:
                    if maybe_complete_run_validation(conn, run_id, getattr(request.state, "user", None)):
                        completed_runs += 1
                        sheet_sync_results.append(
                            _sync_completion_to_sheet(conn, run_id, getattr(request.state, "user", None))
                        )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {
            "saved": saved,
            "completed_runs": completed_runs,
            "sheet_sync": _sheet_sync_summary(sheet_sync_results),
        }

    return router


def _run_to_dict(row):
    pass_images = row["pass_images"] or 0
    fail_images = row["fail_images"] or 0
    selected = row["selected_images"] or 0
    target = row["image_target_count"] or DEFAULT_IMAGE_COUNT
    validated = row["validated_images"] or 0
    unmarked = max(0, target - validated)
    status = row["status"] if "status" in row.keys() else "pending"
    return {
        "run_id": row["run_id"],
        "sheet_count": row["sheet_count"],
        "vehicle_type": row["vehicle_type"],
        "locality_name": row["locality_name"] if "locality_name" in row.keys() else None,
        "locality_category": row["locality_category"] if "locality_category" in row.keys() else None,
        "region_id": row["region_id"] if "region_id" in row.keys() else None,
        "batch_name": row["batch_name"],
        "total_image_count": row["total_image_count"],
        "selection_version": row["selection_version"],
        "image_target_count": target,
        "selected_images": selected,
        "validated_images": validated,
        "pass_images": pass_images,
        "fail_images": fail_images,
        "unmarked_images": unmarked,
        "status": status,
    }


def _image_to_dict(row, api_prefix: str):
    return {
        "id": row["id"],
        "image_index": row["image_index"],
        "member_name": row["member_name"],
        "cache_path": row["cache_path"],
        "cached_at": row["cached_at"],
        "status": row["status"],
        "notes": row["notes"] or "",
        "file_url": f"{api_prefix}/images/{row['id']}/file",
    }


def _review_image_to_dict(row, api_prefix: str):
    data = _image_to_dict(row, api_prefix)
    data["run_id"] = row["run_id"]
    data["batch_name"] = row["batch_name"]
    data["locality_name"] = row["locality_name"]
    data["locality_category"] = row["locality_category"]
    data["region_id"] = row["region_id"]
    data["submitted_at"] = row["submitted_at"] if "submitted_at" in row.keys() else None
    return data


def _queue_prefetch_if_possible(dataset: DatasetConfig, run) -> str:
    total_count = run["total_image_count"]
    if total_count is None:
        return "skipped_no_manifest"
    target = prefetch_target_for_total(int(total_count))
    if target is None or target <= int(run["image_target_count"] or DEFAULT_IMAGE_COUNT):
        return "complete"
    return queue_run_image_prefetch(dataset, run["run_id"])


def _fetch_run_status(conn, run_id: str):
    row = conn.execute(
        _run_status_cte("r.run_id = ?")
        + """
        SELECT *
        FROM run_status
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown run_id: {run_id}")
    return row


def _ensure_run_available(conn, run_id: str) -> None:
    row = conn.execute(
        """
        SELECT run_id, validation_completed_at, sheet_validation, compltd_status
        FROM runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown run_id: {run_id}")
    if row["validation_completed_at"] is not None:
        return
    if is_existing_sheet_validation_complete(row["sheet_validation"]):
        raise PermissionError("Run is already marked complete in the existing Google Sheet validation column")
    if is_compltd_completed(row["compltd_status"]):
        raise PermissionError("Run is already marked complete in the app-owned Google Sheet columns")


def _prepare_review_images(
    conn,
    dataset: DatasetConfig,
    batch: str,
    run_id: str,
    locality_category: str,
    state: str,
) -> tuple[list[str], int]:
    if state not in {"unreviewed", "all"}:
        return [], 0

    _sync_review_image_targets(conn, _list_review_run_rows(conn, dataset, batch, run_id, locality_category))
    ready_rows = _list_review_image_rows(conn, dataset, batch, run_id, locality_category, state)
    run_rows = _ordered_review_run_rows(_list_review_run_rows(conn, dataset, batch, run_id, locality_category))
    skipped_runs: list[str] = []
    if not ready_rows:
        for row in run_rows:
            run_id_value = row["run_id"]
            if not _review_run_is_pending(row):
                continue
            try:
                _bootstrap_review_run(conn, dataset, run_id_value)
            except Exception:
                skipped_runs.append(run_id_value)
                continue
            if _list_review_image_rows(conn, dataset, batch, run_id, locality_category, state, skipped_runs):
                break

    _sync_review_image_targets(conn, _list_review_run_rows(conn, dataset, batch, run_id, locality_category))
    run_rows = _ordered_review_run_rows(_list_review_run_rows(conn, dataset, batch, run_id, locality_category))
    pending_runs = 0
    for row in run_rows:
        if row["run_id"] in skipped_runs:
            continue
        if not _review_run_is_pending(row):
            continue
        queue_run_image_ensure(dataset, row["run_id"])
        pending_runs += 1

    return skipped_runs, pending_runs


def _list_review_run_rows(conn, dataset: DatasetConfig, batch: str, run_id: str, locality_category: str):
    where_sql, params = _review_run_filters(dataset, batch, run_id, locality_category)
    return conn.execute(
        f"""
        SELECT
            r.run_id,
            r.total_image_count,
            r.image_target_count,
            COUNT(DISTINCT CASE WHEN ri.image_index < r.image_target_count THEN ri.id END) AS active_images,
            COUNT(DISTINCT CASE WHEN ri.image_index < r.image_target_count AND ri.cache_path IS NOT NULL THEN ri.id END) AS cached_images
        FROM runs r
        LEFT JOIN run_images ri ON ri.run_id = r.run_id AND ri.selection_version = r.selection_version
        WHERE {where_sql}
        GROUP BY r.run_id
        ORDER BY r.batch_name, r.run_id
        """,
        params,
    ).fetchall()


def _fetch_review_run_row(conn, run_id: str):
    row = conn.execute(
        """
        SELECT
            r.run_id,
            r.total_image_count,
            r.image_target_count,
            COUNT(DISTINCT CASE WHEN ri.image_index < r.image_target_count THEN ri.id END) AS active_images,
            COUNT(DISTINCT CASE WHEN ri.image_index < r.image_target_count AND ri.cache_path IS NOT NULL THEN ri.id END) AS cached_images
        FROM runs r
        LEFT JOIN run_images ri ON ri.run_id = r.run_id AND ri.selection_version = r.selection_version
        WHERE r.run_id = ?
        GROUP BY r.run_id
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown run_id: {run_id}")
    return row


def _sync_review_image_targets(conn, run_rows) -> None:
    for row in run_rows:
        total_count = row["total_image_count"]
        if total_count is None:
            continue
        target = review_image_target(int(total_count), int(row["image_target_count"] or 1))
        current_target = int(row["image_target_count"] or 1)
        if target != current_target:
            conn.execute("UPDATE runs SET image_target_count = ? WHERE run_id = ?", (target, row["run_id"]))


def _bootstrap_review_run(conn, dataset: DatasetConfig, run_id: str) -> None:
    total_count = ensure_manifest_image_count(conn, dataset, run_id)
    row = _fetch_review_run_row(conn, run_id)
    if total_count is not None:
        target = review_image_target(int(total_count), int(row["image_target_count"] or 1))
        current_target = int(row["image_target_count"] or 1)
        if target != current_target:
            conn.execute("UPDATE runs SET image_target_count = ? WHERE run_id = ?", (target, run_id))
            row = _fetch_review_run_row(conn, run_id)

    target = int(row["image_target_count"] or 1)
    active_images = int(row["active_images"] or 0)
    cached_images = int(row["cached_images"] or 0)
    ensure_count = min(target, max(active_images, cached_images + 1, 1))
    ensure_run_images_to_count(conn, dataset, run_id, ensure_count)

    refreshed_row = _fetch_review_run_row(conn, run_id)
    refreshed_total = refreshed_row["total_image_count"]
    if refreshed_total is None:
        return

    refreshed_target = review_image_target(int(refreshed_total), int(refreshed_row["image_target_count"] or 1))
    current_target = int(refreshed_row["image_target_count"] or 1)
    if refreshed_target != current_target:
        conn.execute("UPDATE runs SET image_target_count = ? WHERE run_id = ?", (refreshed_target, run_id))


def _review_run_is_pending(row) -> bool:
    target = int(row["image_target_count"] or 1)
    active_images = int(row["active_images"] or 0)
    cached_images = int(row["cached_images"] or 0)
    return active_images < target or cached_images < target


def _ordered_review_run_rows(rows):
    return sorted(rows, key=lambda row: _review_random_key(row["run_id"]))


def _list_review_image_rows(
    conn,
    dataset: DatasetConfig,
    batch: str,
    run_id: str,
    locality_category: str,
    state: str,
    excluded_run_ids: list[str] | None = None,
):
    where_sql, params = _review_run_filters(dataset, batch, run_id, locality_category)
    status_sql = ""
    status_params: list[str] = []
    if state == "unreviewed":
        status_sql = "AND iv.id IS NULL"
    elif state == "submitted":
        status_sql = "AND iv.id IS NOT NULL"
    elif state in {"pass", "fail"}:
        status_sql = "AND iv.status = ?"
        status_params.append(state)

    excluded_sql = ""
    excluded_params: list[str] = []
    if excluded_run_ids:
        placeholders = ", ".join("?" for _ in excluded_run_ids)
        excluded_sql = f"AND ri.run_id NOT IN ({placeholders})"
        excluded_params.extend(excluded_run_ids)

    rows = conn.execute(
        f"""
        SELECT ri.id, ri.run_id, r.batch_name, r.locality_name, r.locality_category, r.region_id,
               ri.image_index, ri.member_name, ri.cache_path, ri.cached_at,
               iv.status, iv.notes, iv.submitted_at
        FROM run_images ri
        JOIN runs r ON r.run_id = ri.run_id AND r.selection_version = ri.selection_version
        LEFT JOIN image_validations iv ON iv.run_image_id = ri.id
        WHERE {where_sql}
          AND ri.image_index < r.image_target_count
          AND ri.cache_path IS NOT NULL
          {status_sql}
          {excluded_sql}
        """,
        [*params, *status_params, *excluded_params],
    ).fetchall()
    return sorted(rows, key=lambda row: _review_random_key(f"{row['run_id']}:{row['image_index']}"))


def _review_random_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _review_run_filters(dataset: DatasetConfig, batch: str, run_id: str, locality_category: str) -> tuple[str, list[str]]:
    batch_names = [name for name, _ in dataset.batch_prefixes]
    params: list[str] = []
    if batch == "all":
        placeholders = ", ".join("?" for _ in batch_names)
        where = [f"r.batch_name IN ({placeholders})"]
        params.extend(batch_names)
    else:
        where = ["r.batch_name = ?"]
        params.append(batch)

    if run_id.strip():
        where.append("r.run_id LIKE ?")
        params.append(f"%{run_id.strip()}%")
    _append_locality_category_filter(where, params, locality_category)
    _append_sheet_availability_filter(where)

    return " AND ".join(where), params


def _review_stats(conn, dataset: DatasetConfig, batch: str, run_id: str, locality_category: str) -> dict[str, object]:
    where_sql, params = _review_run_filters(dataset, batch, run_id, locality_category)
    rows = conn.execute(
        f"""
        WITH run_stats AS (
            SELECT
                r.batch_name,
                r.run_id,
                r.selection_version,
                r.image_target_count,
                r.validation_completed_at,
                r.validation_completed_selection_version,
                r.validation_completed_image_target_count,
                COUNT(DISTINCT CASE WHEN ri.image_index < r.image_target_count THEN ri.id END) AS selected_images,
                COUNT(DISTINCT iv.id) AS validated_images,
                COALESCE(SUM(CASE WHEN iv.status = 'pass' THEN 1 ELSE 0 END), 0) AS pass_images,
                COALESCE(SUM(CASE WHEN iv.status = 'fail' THEN 1 ELSE 0 END), 0) AS fail_images
            FROM runs r
            LEFT JOIN run_images ri
                ON ri.run_id = r.run_id
                AND ri.selection_version = r.selection_version
                AND ri.image_index < r.image_target_count
            LEFT JOIN image_validations iv ON iv.run_image_id = ri.id
            WHERE {where_sql}
            GROUP BY r.run_id
        ), run_progress AS (
            SELECT
                *,
                CASE
                    WHEN validation_completed_at IS NOT NULL THEN 1
                    ELSE 0
                END AS is_completed
            FROM run_stats
        )
        SELECT
            batch_name,
            COUNT(*) AS total_runs,
            COALESCE(SUM(CASE WHEN is_completed = 1 THEN 1 ELSE 0 END), 0) AS completed_runs,
            COALESCE(SUM(CASE WHEN is_completed = 0 AND validated_images > 0 THEN 1 ELSE 0 END), 0) AS in_progress_runs,
            COALESCE(SUM(CASE WHEN is_completed = 0 AND validated_images = 0 THEN 1 ELSE 0 END), 0) AS not_started_runs,
            COALESCE(SUM(CASE WHEN is_completed = 0 AND validated_images >= image_target_count AND validated_images > 0 THEN 1 ELSE 0 END), 0) AS ready_runs,
            COALESCE(SUM(image_target_count), 0) AS target_images,
            COALESCE(SUM(selected_images), 0) AS selected_images,
            COALESCE(SUM(validated_images), 0) AS validated_images,
            COALESCE(SUM(pass_images), 0) AS pass_images,
            COALESCE(SUM(fail_images), 0) AS fail_images
        FROM run_progress
        GROUP BY batch_name
        ORDER BY batch_name
        """,
        params,
    ).fetchall()

    batches = [_review_stats_row_to_dict(row) for row in rows]
    summary = {
        "total_runs": sum(item["total_runs"] for item in batches),
        "completed_runs": sum(item["completed_runs"] for item in batches),
        "in_progress_runs": sum(item["in_progress_runs"] for item in batches),
        "not_started_runs": sum(item["not_started_runs"] for item in batches),
        "ready_runs": sum(item["ready_runs"] for item in batches),
        "target_images": sum(item["target_images"] for item in batches),
        "selected_images": sum(item["selected_images"] for item in batches),
        "validated_images": sum(item["validated_images"] for item in batches),
        "pass_images": sum(item["pass_images"] for item in batches),
        "fail_images": sum(item["fail_images"] for item in batches),
    }
    summary["fail_rate"] = _rate(summary["fail_images"], summary["validated_images"])

    return {"summary": summary, "batches": batches}


def _review_stats_row_to_dict(row) -> dict[str, int | float | str]:
    validated_images = int(row["validated_images"] or 0)
    fail_images = int(row["fail_images"] or 0)
    return {
        "batch_name": row["batch_name"],
        "total_runs": int(row["total_runs"] or 0),
        "completed_runs": int(row["completed_runs"] or 0),
        "in_progress_runs": int(row["in_progress_runs"] or 0),
        "not_started_runs": int(row["not_started_runs"] or 0),
        "ready_runs": int(row["ready_runs"] or 0),
        "target_images": int(row["target_images"] or 0),
        "selected_images": int(row["selected_images"] or 0),
        "validated_images": validated_images,
        "pass_images": int(row["pass_images"] or 0),
        "fail_images": fail_images,
        "fail_rate": _rate(fail_images, validated_images),
    }


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _run_status_cte(where_sql: str) -> str:
    return f"""
        WITH run_stats AS (
            SELECT
                r.run_id,
                r.sheet_count,
                r.vehicle_type,
                r.locality_name,
                r.locality_category,
                r.region_id,
                r.batch_name,
                r.total_image_count,
                r.selection_version,
                r.image_target_count,
                r.validation_completed_at,
                r.validation_completed_selection_version,
                r.validation_completed_image_target_count,
                COUNT(DISTINCT ri.id) AS selected_images,
                COUNT(DISTINCT iv.id) AS validated_images,
                COALESCE(SUM(CASE WHEN iv.status = 'pass' THEN 1 ELSE 0 END), 0) AS pass_images,
                COALESCE(SUM(CASE WHEN iv.status = 'fail' THEN 1 ELSE 0 END), 0) AS fail_images
            FROM runs r
            LEFT JOIN run_images ri
                ON ri.run_id = r.run_id
                AND ri.selection_version = r.selection_version
                AND ri.image_index < r.image_target_count
            LEFT JOIN image_validations iv ON iv.run_image_id = ri.id
            WHERE {where_sql}
            GROUP BY r.run_id
        ), run_status AS (
            SELECT
                *,
                CASE
                    WHEN validation_completed_at IS NOT NULL
                        AND validation_completed_selection_version = selection_version
                        AND validation_completed_image_target_count = image_target_count
                        AND fail_images > 0 THEN 'fail'
                    WHEN validation_completed_at IS NOT NULL
                        AND validation_completed_selection_version = selection_version
                        AND validation_completed_image_target_count = image_target_count THEN 'pass'
                    WHEN validated_images = 0 THEN 'pending'
                    WHEN validated_images >= image_target_count THEN 'ready'
                    ELSE 'partial'
                END AS status
            FROM run_stats
        )
    """


def _run_detail_to_dict(row, selected_images: int, prefetched_images: int):
    data = _run_to_dict(row)
    data["selected_images"] = selected_images
    data["prefetched_images"] = prefetched_images
    return data


def _list_validation_report_rows(
    conn,
    dataset: DatasetConfig,
    batch: str,
    locality_category: str,
    vehicle_type: str,
    run_id: str,
    limit: int | None,
    offset: int,
):
    where_sql, params = _validation_report_filters(batch, locality_category, vehicle_type, run_id)
    base_sql = _validation_report_cte(where_sql)

    select_sql = base_sql + """
        SELECT *
        FROM validation_report
        ORDER BY validation_completed_at DESC, batch_name, run_id
    """
    select_params = list(params)
    if limit is not None:
        select_sql += " LIMIT ? OFFSET ?"
        select_params.extend([limit, offset])

    rows = conn.execute(select_sql, select_params).fetchall()
    total = conn.execute(
        base_sql
        + """
        SELECT COUNT(*) AS count
        FROM completed_runs
        """,
        params,
    ).fetchone()["count"]
    return rows, total


def _validation_report_filters(batch: str, locality_category: str, vehicle_type: str, run_id: str) -> tuple[str, list[str]]:
    where = ["r.validation_completed_at IS NOT NULL"]
    params: list[str] = []

    if batch and batch != "all":
        where.append("r.batch_name = ?")
        params.append(batch)
    _append_locality_category_filter(where, params, locality_category)
    if vehicle_type and vehicle_type != "all":
        where.append("r.vehicle_type = ?")
        params.append(vehicle_type)
    if run_id.strip():
        where.append("r.run_id LIKE ?")
        params.append(f"%{run_id.strip()}%")

    return " AND ".join(where), params


def _validation_report_cte(where_sql: str) -> str:
    return f"""
        WITH completed_runs AS (
            SELECT
                r.run_id,
                r.batch_name,
                r.source_scope,
                r.vehicle_type,
                r.locality_name,
                r.locality_category,
                r.region_id,
                r.total_image_count,
                r.validation_completed_at,
                r.validation_completed_by,
                COALESCE(r.validation_completed_selection_version, r.selection_version) AS completed_selection_version,
                COALESCE(r.validation_completed_image_target_count, r.image_target_count) AS completed_image_target_count
            FROM runs r
            WHERE {where_sql}
        ), validation_report AS (
            SELECT
                cr.run_id,
                cr.batch_name,
                cr.source_scope,
                cr.vehicle_type,
                cr.locality_name,
                cr.locality_category,
                cr.region_id,
                cr.total_image_count,
                cr.validation_completed_at,
                cr.validation_completed_by,
                COUNT(DISTINCT iv.id) AS validated_images,
                COALESCE(SUM(CASE WHEN iv.status = 'pass' THEN 1 ELSE 0 END), 0) AS pass_images,
                COALESCE(SUM(CASE WHEN iv.status = 'fail' THEN 1 ELSE 0 END), 0) AS fail_images
            FROM completed_runs cr
            LEFT JOIN run_images ri
                ON ri.run_id = cr.run_id
                AND ri.selection_version = cr.completed_selection_version
                AND ri.image_index < cr.completed_image_target_count
            LEFT JOIN image_validations iv ON iv.run_image_id = ri.id
            GROUP BY
                cr.run_id,
                cr.batch_name,
                cr.source_scope,
                cr.vehicle_type,
                cr.locality_name,
                cr.locality_category,
                cr.region_id,
                cr.total_image_count,
                cr.validation_completed_at,
                cr.validation_completed_by
        )
    """


def _report_row_to_dict(row, dataset: DatasetConfig):
    pass_images = row["pass_images"] or 0
    fail_images = row["fail_images"] or 0
    validated_images = row["validated_images"] or 0
    outcome, outcome_detail = _report_outcome(fail_images, validated_images)
    return {
        "run_name": row["run_id"],
        "batch_name": row["batch_name"],
        "download_point": _report_download_point(dataset, row["source_scope"]),
        "vehicle_type": row["vehicle_type"] or "",
        "locality_name": row["locality_name"] or "",
        "locality_category": row["locality_category"] or "",
        "region_id": row["region_id"] or "",
        "run_id": row["run_id"],
        "total_image_count": row["total_image_count"],
        "pass_images": pass_images,
        "fail_images": fail_images,
        "validated_images": validated_images,
        "outcome": outcome,
        "outcome_detail": outcome_detail,
        "validation_completed_by": row["validation_completed_by"] or "",
    }


def _report_download_point(dataset: DatasetConfig, source_scope: str | None) -> str:
    scope = (source_scope or "").lstrip("/")
    if scope and not scope.endswith("/"):
        scope += "/"
    return f"{dataset.s3_bucket}/{scope}" if scope else dataset.s3_bucket


def _report_outcome(fail_images: int, validated_images: int) -> tuple[str, str]:
    if validated_images <= 0:
        return "retry", "0/0 failed (0.0%), threshold < 10%"

    failure_rate = fail_images / validated_images
    outcome = "approved" if failure_rate < 0.10 else "retry"
    return outcome, f"{fail_images}/{validated_images} failed ({failure_rate:.1%}), threshold < 10%"


def _report_url(
    path: str,
    batch: str,
    locality_category: str,
    vehicle_type: str,
    run_id: str,
    limit: int | None = None,
    page: int | None = None,
) -> str:
    params = {
        "batch": batch or "all",
        "locality_category": locality_category or "all",
        "vehicle_type": vehicle_type or "all",
    }
    if run_id.strip():
        params["run_id"] = run_id.strip()
    if limit is not None:
        params["limit"] = str(limit)
    if page is not None:
        params["page"] = str(page)

    return f"{path}?{urlencode(params)}"


def _report_count_text(total: int, current_page: int, total_pages: int, offset: int, row_count: int) -> str:
    if not total:
        return "0 found"
    start = offset + 1
    end = min(offset + row_count, total)
    return f"{total} found - page {current_page}/{total_pages or 1} - showing {start}-{end}"


def _locality_category_options(conn, completed_only: bool = False) -> list[dict[str, str | int]]:
    where_sql = "WHERE validation_completed_at IS NOT NULL" if completed_only else ""
    rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(TRIM(locality_category), ''), 'unknown') AS category,
               COUNT(*) AS count
        FROM runs
        {where_sql}
        GROUP BY category
        """
    ).fetchall()
    counts = {row["category"]: row["count"] for row in rows}
    total = sum(counts.values())
    return [
        {"value": "all", "label": "All localities", "count": total},
        *[
            {"value": value, "label": label, "count": counts.get(value, 0)}
            for value, label in LOCALITY_CATEGORY_OPTIONS
        ],
    ]


def _append_locality_category_filter(where: list[str], params: list[str | int], locality_category: str) -> None:
    value = (locality_category or "all").strip()
    valid_values = {option_value for option_value, _ in LOCALITY_CATEGORY_OPTIONS}
    if value == "all" or value not in valid_values:
        return
    if value == "unknown":
        where.append("(r.locality_category IS NULL OR TRIM(r.locality_category) = '')")
        return
    where.append("r.locality_category = ?")
    params.append(value)


def _append_sheet_availability_filter(where: list[str]) -> None:
    where.append(
        """
        (
            r.validation_completed_at IS NOT NULL
            OR (
                LOWER(COALESCE(TRIM(r.sheet_validation), '')) NOT IN ('approved', 'retry')
                AND LOWER(COALESCE(TRIM(r.compltd_status), '')) <> 'completed'
            )
        )
        """
    )


def _sync_completion_to_sheet(conn, run_id: str, completed_by: str | None) -> SheetWriteResult:
    payload = _completion_writeback_payload(conn, run_id, completed_by)
    result = write_run_completion(payload)
    if result.status == "updated":
        conn.execute(
            """
            UPDATE runs
            SET compltd_status = 'completed',
                compltd_validator = ?,
                compltd_completed_at = ?,
                compltd_outcome = ?,
                compltd_reviewed_images = ?,
                compltd_failed_images = ?,
                compltd_updated_at = ?
            WHERE run_id = ?
            """,
            (
                payload.validator,
                payload.completed_at,
                payload.outcome,
                payload.reviewed_images,
                payload.failed_images,
                payload.completed_at,
                run_id,
            ),
        )
    return result


def _completion_writeback_payload(conn, run_id: str, completed_by: str | None) -> CompletionWriteback:
    row = conn.execute(
        """
        WITH completed AS (
            SELECT
                run_id,
                COALESCE(validation_completed_selection_version, selection_version) AS completed_selection_version,
                COALESCE(validation_completed_image_target_count, image_target_count) AS completed_image_target_count
            FROM runs
            WHERE run_id = ?
        )
        SELECT
            c.run_id,
            COUNT(DISTINCT iv.id) AS reviewed_images,
            COALESCE(SUM(CASE WHEN iv.status = 'fail' THEN 1 ELSE 0 END), 0) AS failed_images
        FROM completed c
        LEFT JOIN run_images ri
            ON ri.run_id = c.run_id
            AND ri.selection_version = c.completed_selection_version
            AND ri.image_index < c.completed_image_target_count
        LEFT JOIN image_validations iv ON iv.run_image_id = ri.id
        GROUP BY c.run_id
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown run_id: {run_id}")

    reviewed_images = int(row["reviewed_images"] or 0)
    failed_images = int(row["failed_images"] or 0)
    outcome, _ = _report_outcome(failed_images, reviewed_images)
    return CompletionWriteback(
        run_id=run_id,
        validator=completed_by or "unknown",
        completed_at=_utc_timestamp(),
        outcome=outcome,
        reviewed_images=reviewed_images,
        failed_images=failed_images,
    )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sheet_sync_summary(results: list[SheetWriteResult]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for result in results:
        summary[result.status] = summary.get(result.status, 0) + 1
    return summary


def _validation_report_csv_response(dataset: DatasetConfig, rows: list[dict[str, object]]) -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "run_name",
            "batch_id",
            "download_point",
            "vehicle_type",
            "locality_name",
            "locality_category",
            "region_id",
            "run_id",
            "total_images",
            "passed_images",
            "failed_images",
            "validation_images_done",
            "outcome",
            "completed_by",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["run_name"],
                row["batch_name"],
                row["download_point"],
                row["vehicle_type"],
                row["locality_name"],
                row["locality_category"],
                row["region_id"],
                row["run_id"],
                row["total_image_count"],
                row["pass_images"],
                row["fail_images"],
                row["validated_images"],
                row["outcome"],
                row["validation_completed_by"],
            ]
        )

    filename = f"{dataset.slug}-completed-validations.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


routers = [create_router(dataset) for dataset in DATASETS]

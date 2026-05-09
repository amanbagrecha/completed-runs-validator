from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.templating import Jinja2Templates

from app.config import DATASETS, DEFAULT_IMAGE_COUNT, DatasetConfig, ROOT_DIR
from app.db import get_conn
from app.services.image_cache import append_run_images, ensure_run_images, get_image_file_path
from app.services.sync import sync_runs
from app.services.validations import submit_validations


templates = Jinja2Templates(directory=str(ROOT_DIR / "app" / "templates"))


class ValidationItem(BaseModel):
    image_id: int
    status: Literal["pass", "fail"]


class ValidationRequest(BaseModel):
    items: list[ValidationItem]


def create_router(dataset: DatasetConfig) -> APIRouter:
    router = APIRouter()
    nav_pages = [{"label": item.label, "path": item.page_path} for item in DATASETS]

    @router.get(dataset.page_path)
    def index(request: Request):
        with get_conn(dataset.db_path) as conn:
            latest_sync = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
            batch_counts = {
                row["batch_name"]: row["count"]
                for row in conn.execute("SELECT batch_name, COUNT(*) AS count FROM runs GROUP BY batch_name")
            }
        batches = [{"name": name, "count": batch_counts.get(name, 0)} for name, _ in dataset.batch_prefixes]
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "batches": batches,
                "latest_sync": latest_sync,
                "image_count": DEFAULT_IMAGE_COUNT,
                "api_base": dataset.api_prefix,
                "current_page_path": dataset.page_path,
                "nav_pages": nav_pages,
                "page_label": dataset.label,
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
        status: Literal["all", "pending", "partial", "pass", "fail", "finished", "unfinished"] = Query("all"),
        limit: int = Query(10, ge=1, le=100),
        offset: int = Query(0, ge=0),
        page: int | None = Query(None, ge=1),
    ):
        if page is not None:
            offset = (page - 1) * limit

        base_params: list[str | int] = [batch]
        where = ["r.batch_name = ?"]
        if run_id.strip():
            where.append("r.run_id LIKE ?")
            base_params.append(f"%{run_id.strip()}%")

        aggregate_sql = f"""
            WITH run_stats AS (
                SELECT
                    r.run_id,
                    r.sheet_count,
                    r.vehicle_type,
                    r.batch_name,
                    r.selection_version,
                    r.image_target_count,
                    COUNT(DISTINCT ri.id) AS selected_images,
                    COUNT(DISTINCT iv.id) AS validated_images,
                    COALESCE(SUM(CASE WHEN iv.status = 'pass' THEN 1 ELSE 0 END), 0) AS pass_images,
                    COALESCE(SUM(CASE WHEN iv.status = 'fail' THEN 1 ELSE 0 END), 0) AS fail_images
                FROM runs r
                LEFT JOIN run_images ri ON ri.run_id = r.run_id AND ri.selection_version = r.selection_version
                LEFT JOIN image_validations iv ON iv.run_image_id = ri.id
                WHERE {' AND '.join(where)}
                GROUP BY r.run_id
            ), run_status AS (
                SELECT
                    *,
                    CASE
                        WHEN fail_images > 0 THEN 'fail'
                        WHEN validated_images >= image_target_count THEN 'pass'
                        WHEN validated_images > 0 THEN 'partial'
                        ELSE 'pending'
                    END AS status
                FROM run_stats
            )
        """

        status_where = ""
        status_params: list[str] = []
        if status in {"pending", "partial", "pass", "fail"}:
            status_where = "WHERE status = ?"
            status_params.append(status)
        elif status == "finished":
            status_where = "WHERE status IN ('pass', 'fail')"
        elif status == "unfinished":
            status_where = "WHERE status IN ('pending', 'partial')"

        with get_conn(dataset.db_path) as conn:
            rows = conn.execute(
                aggregate_sql
                + f"""
                SELECT *
                FROM run_status
                {status_where}
                ORDER BY run_id
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

    @router.get(f"{dataset.api_prefix}/runs/{{run_id}}/images")
    def get_run_images(run_id: str):
        try:
            with get_conn(dataset.db_path) as conn:
                images = ensure_run_images(conn, dataset, run_id)
                run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {
            "run": _run_detail_to_dict(run, len(images)),
            "images": [_image_to_dict(row, dataset.api_prefix) for row in images],
        }

    @router.post(f"{dataset.api_prefix}/runs/{{run_id}}/refresh-images")
    def refresh_run_images(run_id: str):
        try:
            with get_conn(dataset.db_path) as conn:
                images = append_run_images(conn, dataset, run_id, DEFAULT_IMAGE_COUNT)
                run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {
            "run": _run_detail_to_dict(run, len(images)),
            "images": [_image_to_dict(row, dataset.api_prefix) for row in images],
        }

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
                    [{"image_id": item.image_id, "status": item.status} for item in payload.items],
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"saved": saved}

    return router


def _run_to_dict(row):
    pass_images = row["pass_images"] or 0
    fail_images = row["fail_images"] or 0
    selected = row["selected_images"] or 0
    target = row["image_target_count"] or DEFAULT_IMAGE_COUNT
    validated = row["validated_images"] or 0
    status = row["status"] if "status" in row.keys() else "pending"
    return {
        "run_id": row["run_id"],
        "sheet_count": row["sheet_count"],
        "vehicle_type": row["vehicle_type"],
        "batch_name": row["batch_name"],
        "selection_version": row["selection_version"],
        "image_target_count": target,
        "selected_images": selected,
        "validated_images": validated,
        "pass_images": pass_images,
        "fail_images": fail_images,
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
        "file_url": f"{api_prefix}/images/{row['id']}/file",
    }


def _run_detail_to_dict(row, selected_images: int):
    return {
        "run_id": row["run_id"],
        "batch_name": row["batch_name"],
        "sheet_count": row["sheet_count"],
        "vehicle_type": row["vehicle_type"],
        "selection_version": row["selection_version"],
        "image_target_count": row["image_target_count"] or DEFAULT_IMAGE_COUNT,
        "selected_images": selected_images,
    }


routers = [create_router(dataset) for dataset in DATASETS]

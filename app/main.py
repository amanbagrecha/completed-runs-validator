from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.auth import auth_middleware, validate_auth_config
from app.config import DATASETS, ROOT_DIR
from app.db import init_db
from app.routes import auth_router, routers


app = FastAPI(title="Completed Runs Validator")
app.middleware("http")(auth_middleware)
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app" / "static")), name="static")
app.include_router(auth_router)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def root_redirect():
    return RedirectResponse("/review", status_code=303)


for router in routers:
    app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    validate_auth_config()
    for dataset in DATASETS:
        init_db(dataset.db_path)

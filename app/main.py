from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import DATASETS, ROOT_DIR
from app.db import init_db
from app.routes import routers


app = FastAPI(title="Completed Runs Validator")
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app" / "static")), name="static")
for router in routers:
    app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    for dataset in DATASETS:
        init_db(dataset.db_path)

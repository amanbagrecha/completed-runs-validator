from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import ROOT_DIR
from app.db import init_db
from app.routes import router


app = FastAPI(title="Completed Runs Validator")
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app" / "static")), name="static")
app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    init_db()

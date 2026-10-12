"""Milo backend: FastAPI serves the API and the dashboard (web/)."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import approvals, campaigns, chat, insights
from app.config import get_settings
from app.db.session import init_db
from app.jobs import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="Milo", version="0.1.0", lifespan=lifespan)
app.include_router(chat.router)
app.include_router(campaigns.router)
app.include_router(approvals.router)
app.include_router(insights.router)

# The UI in web/ is served from this same origin, so browsers need no CORS.
# Allowed origins only matter for a separate local dev server (e.g. a frontend on another port).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=get_settings().cors_origin_regex,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/metrics/refresh", tags=["metrics"])
async def refresh_metrics():
    return {"rows": await scheduler.pull_metrics()}


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

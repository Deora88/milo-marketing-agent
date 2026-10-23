"""Milo backend: FastAPI serves the API and the dashboard (web/)."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import approvals, campaigns, chat, faq, inbox, insights, webhooks
from app.config import get_settings
from app.db.store import init_db
from app.jobs import scheduler
from app.security import guard_requests
from app.util.redact import redact

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - start anyway; Settings shows the database error
        logging.getLogger("milo").error("MongoDB not reachable (%s). Check MONGODB_URI and Atlas Network Access.",
                                        redact(exc))
    scheduler.start()
    yield
    scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="Milo", version="0.1.0", lifespan=lifespan)
app.include_router(chat.router)
app.include_router(campaigns.router)
app.include_router(approvals.router)
app.include_router(insights.router)
app.include_router(webhooks.router)
app.include_router(inbox.router)
app.include_router(faq.router)

# The UI in web/ is served from this same origin, so browsers need no CORS.
# Allowed origins only matter for a separate local dev server (e.g. a frontend on another port).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=get_settings().cors_origin_regex,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _guard(request: Request, call_next):  # host allow-list, CSRF header, optional token
    return await guard_requests(request, call_next)


@app.post("/api/metrics/refresh", tags=["metrics"])
async def refresh_metrics():
    try:
        return {"rows": await scheduler.pull_metrics()}
    except Exception as exc:  # noqa: BLE001
        from fastapi import HTTPException

        raise HTTPException(502, f"Metrics pull failed: {redact(exc)}") from exc


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

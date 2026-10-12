"""Background job: every N minutes, pull metrics for live campaigns into MetricSnapshot."""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, delete, select

from app.config import get_settings
from app.db.models import Campaign, MetricSnapshot
from app.db.session import engine
from app.metrics import get_provider

log = logging.getLogger("milo.jobs")
scheduler = AsyncIOScheduler(timezone="UTC")


async def pull_metrics() -> int:
    provider = get_provider()
    written = 0
    with Session(engine) as session:
        live = session.exec(select(Campaign).where(Campaign.status.in_(["active", "paused"]))).all()
        for c in live:
            rows = await provider.daily(c, days=7)
            session.exec(delete(MetricSnapshot).where(MetricSnapshot.campaign_id == c.id))
            for r in rows:
                session.add(MetricSnapshot(campaign_id=c.id, date=r.date, spend=r.spend, impressions=r.impressions,
                                           reach=r.reach, clicks=r.clicks, ctr=r.ctr, cpc=r.cpc, source=provider.source))
                written += 1
        session.commit()
    log.info("pulled %d metric rows (%s)", written, provider.source)
    if get_settings().optimizer_enabled:
        from app.optimizer import run_optimizer

        with Session(engine) as session:
            result = run_optimizer(session)
        log.info("optimizer: %d recommendation(s), %d skipped", len(result["recommendations"]), len(result["skipped"]))
    return written


def start() -> None:
    scheduler.add_job(pull_metrics, "interval", minutes=get_settings().metrics_poll_minutes, id="pull_metrics",
                      replace_existing=True)
    scheduler.start()

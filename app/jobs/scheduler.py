"""Background job: every N minutes, pull metrics for campaigns on Meta into MetricSnapshot, then run the optimizer.

One failing campaign never stops the others: the error is logged to the Activity page and the pull continues.
Rows are replaced per date only when the provider returns data, so an empty or failed response never wipes
stored metrics.
"""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db import store
from app.db.models import Campaign, MetricSnapshot
from app.metrics import get_provider
from app.util.redact import redact

log = logging.getLogger("milo.jobs")
scheduler = AsyncIOScheduler(timezone="UTC")


async def pull_metrics() -> int:
    provider = get_provider()
    written, failed = 0, 0
    try:
        for c in store.find(Campaign, {"status": {"$in": ["active", "paused"]}}):
            try:
                rows = await provider.daily(c, days=7)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.warning("metrics pull failed for campaign %s: %s", c.id, redact(exc))
                store.audit("metrics_failed", campaign_id=c.id, error=redact(exc))
                continue
            if not rows:
                continue  # keep what we have
            coll = store.db()[MetricSnapshot.collection]
            coll.delete_many({"campaign_id": c.id, "date": {"$in": [r.date for r in rows]}})
            for r in rows:
                store.insert(MetricSnapshot(campaign_id=c.id, date=r.date, spend=r.spend, impressions=r.impressions,
                                            reach=r.reach, clicks=r.clicks, ctr=r.ctr, cpc=r.cpc,
                                            source=provider.source))
                written += 1
    finally:
        await provider.aclose()
    log.info("pulled %d metric rows (%s), %d campaign(s) failed", written, provider.source, failed)
    if get_settings().optimizer_enabled:
        from app.optimizer import run_optimizer

        result = run_optimizer()
        log.info("optimizer: %d recommendation(s), %d skipped", len(result["recommendations"]), len(result["skipped"]))
    return written


def recover_approvals() -> None:
    from app.approvals import recover_stuck_approvals

    ids = recover_stuck_approvals()
    if ids:
        log.warning("recovered stuck approval(s) %s: back to pending", ids)


def start() -> None:
    from app.approvals import recover_stuck_approvals

    try:  # anything still "executing" at startup was cut off when the server stopped
        ids = recover_stuck_approvals(older_than_minutes=0)
        if ids:
            log.warning("startup: recovered approval(s) %s left executing by the last run", ids)
    except Exception as exc:  # noqa: BLE001 - never block startup (e.g. database down)
        log.error("startup approval recovery failed: %s", redact(exc))
    scheduler.add_job(pull_metrics, "interval", minutes=get_settings().metrics_poll_minutes, id="pull_metrics",
                      replace_existing=True)
    scheduler.add_job(recover_approvals, "interval", minutes=5, id="recover_approvals", replace_existing=True)
    scheduler.start()

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
PULL_DAYS = 60  # enough for "last 30 days vs the 30 before" (app/analysis.py); screens still show the last 7


async def pull_metrics() -> int:
    provider = get_provider()
    written, failed = 0, 0
    try:
        for c in store.find(Campaign, {"status": {"$in": ["active", "paused"]}}):
            try:
                rows = await provider.daily(c, days=PULL_DAYS)
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
                                            link_clicks=r.link_clicks, landing_page_views=r.landing_page_views,
                                            conversations=r.conversations, leads=r.leads,
                                            post_engagement=r.post_engagement, source=provider.source))
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


def fail_stuck_dms() -> None:
    from app.dm.agent import fail_stuck

    ids = fail_stuck()
    if ids:
        log.warning("DM bot run(s) stuck for message(s) %s: marked failed for a person", ids)


def fail_stuck_posts() -> None:
    from app.social.instagram import fail_stuck_posts as recover

    ids = recover()
    if ids:
        log.warning("Instagram post(s) %s were stuck publishing: moved on (check Instagram before retrying)", ids)


async def publish_scheduled_posts() -> None:
    from app.social.instagram import publish_due

    ids = await publish_due()
    if ids:
        log.info("published scheduled Instagram post(s) %s", ids)


async def refresh_post_results() -> None:
    from app.social.insights import refresh_due

    ids = await refresh_due()
    if ids:
        log.info("refreshed Instagram results for post(s) %s", ids)


def start() -> None:
    from app.approvals import recover_stuck_approvals

    try:  # anything still "executing" at startup was cut off when the server stopped
        ids = recover_stuck_approvals(older_than_minutes=0)
        if ids:
            log.warning("startup: recovered approval(s) %s left executing by the last run", ids)
    except Exception as exc:  # noqa: BLE001 - never block startup (e.g. database down)
        log.error("startup approval recovery failed: %s", redact(exc))
    try:  # same for posts left "publishing" by the last run
        from datetime import timedelta

        from app.social.instagram import fail_stuck_posts as recover_posts

        ids = recover_posts(older_than=timedelta(0))
        if ids:
            log.warning("startup: post(s) %s were left publishing by the last run (check Instagram)", ids)
    except Exception as exc:  # noqa: BLE001
        log.error("startup post recovery failed: %s", redact(exc))
    scheduler.add_job(pull_metrics, "interval", minutes=get_settings().metrics_poll_minutes, id="pull_metrics",
                      replace_existing=True)
    scheduler.add_job(recover_approvals, "interval", minutes=5, id="recover_approvals", replace_existing=True)
    scheduler.add_job(fail_stuck_dms, "interval", minutes=2, id="fail_stuck_dms", replace_existing=True)
    scheduler.add_job(fail_stuck_posts, "interval", minutes=5, id="fail_stuck_posts", replace_existing=True)
    scheduler.add_job(publish_scheduled_posts, "interval", minutes=1, id="publish_scheduled_posts",
                      replace_existing=True)
    scheduler.add_job(refresh_post_results, "interval", minutes=15, id="refresh_post_results",
                      replace_existing=True)
    scheduler.start()

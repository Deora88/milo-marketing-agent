"""Read-only endpoints for the dashboard: app status, summary KPIs and the audit log."""
from collections import defaultdict

from fastapi import APIRouter

from app.actions import total_live_budget
from app.config import get_settings
from app.db import store
from app.db.models import Approval, AuditLog, Campaign, MetricSnapshot
from app.metrics import effective_provider

router = APIRouter(prefix="/api", tags=["insights"])


def _db_check() -> dict:
    try:
        store.count(Campaign)
        return {"ok": True, "error": ""}
    except Exception as exc:  # noqa: BLE001 - shown in Settings so a wrong MONGODB_URI is obvious
        from app.util.redact import redact

        return {"ok": False, "error": redact(exc)[:300]}


def _image_status(s, db_ok: bool) -> dict:
    from app.creative.imagegen import used_today

    return {"configured": bool(s.cloudflare_account_id and s.cloudflare_api_token), "model": s.image_model,
            "daily_limit": s.image_daily_limit, "used_today": used_today() if db_ok else 0}


def _dm_status(s, db_ok: bool) -> dict:
    from app.db import store
    from app.db.models import FaqEntry, InboundMessage

    return {"webhook_configured": bool(s.meta_app_secret and s.meta_webhook_verify_token),
            "agent_enabled": s.dm_agent_enabled, "autoreply": s.dm_autoreply, "business_name": s.dm_business_name,
            "faq_count": store.count(FaqEntry, {"enabled": True}) if db_ok else 0,
            "page_token_set": bool(s.meta_page_access_token), "human_agent_enabled": s.meta_human_agent_enabled,
            "unread": store.count(InboundMessage, {"is_echo": False, "status": "new"}) if db_ok else 0}


@router.get("/status")
def status():
    s = get_settings()
    db_state = _db_check()
    return {
        "app": "milo",
        "model": s.milo_model,
        "llm_key_set": bool(s.openrouter_api_key),
        "database": "mongodb",
        "database_ok": db_state["ok"],
        "database_error": db_state["error"],
        "meta_api_version": s.meta_api_version,
        "meta_dry_run": s.meta_dry_run,
        "meta_page_name": s.meta_page_name,
        "meta_configured": bool(s.meta_access_token and s.meta_ad_account_id and s.meta_page_id),
        "instagram_account_set": bool(s.meta_instagram_account_id),
        "spend": {
            "allowed": s.meta_allow_spend,
            "max_daily_budget_inr": s.max_daily_budget_inr,
            "max_total_daily_budget_inr": s.max_total_daily_budget_inr,
            "live_daily_budget_inr": total_live_budget() / 100 if db_state["ok"] else 0,
        },
        "api_token_required": bool(s.api_token),
        "image_generation": _image_status(s, db_state["ok"]),
        "dm": _dm_status(s, db_state["ok"]),
        "metrics_provider": effective_provider(),
        "metrics_poll_minutes": s.metrics_poll_minutes,
        "research": {
            "enabled": s.research_enabled,
            "key_set": bool(s.tavily_api_key),
            "time_range": s.research_time_range,
            "max_results": s.research_max_results,
        },
        "optimizer": {
            "enabled": s.optimizer_enabled,
            "min_impressions": s.optimizer_min_impressions,
            "pause_ctr_ratio": s.optimizer_pause_ctr_ratio,
            "scale_ctr_ratio": s.optimizer_scale_ctr_ratio,
            "trim_cpc_ratio": s.optimizer_trim_cpc_ratio,
            "budget_step_pct": s.optimizer_budget_step_pct,
            "max_daily_budget_inr": s.max_daily_budget_inr,
            "cooldown_hours": s.optimizer_cooldown_hours,
        },
    }


@router.post("/optimizer/run")
def optimizer_run():
    """Run the rule-based optimizer now. Creates pending approvals only."""
    from app.optimizer import run_optimizer

    return run_optimizer()


@router.get("/summary")
def summary():
    campaigns = store.find(Campaign)
    by_status = defaultdict(int)
    for c in campaigns:
        by_status[c.status] += 1
    from app.optimizer import window_start

    snaps = store.find(MetricSnapshot, {"date": {"$gte": window_start()}}, sort=[("date", 1)])
    daily = defaultdict(lambda: {"spend": 0.0, "clicks": 0, "impressions": 0})
    for r in snaps:
        d = daily[r.date]
        d["spend"] += r.spend
        d["clicks"] += r.clicks
        d["impressions"] += r.impressions
    spend = sum(r.spend for r in snaps)
    clicks = sum(r.clicks for r in snaps)
    impressions = sum(r.impressions for r in snaps)
    return {
        "campaigns_total": len(campaigns),
        "by_status": dict(by_status),
        "pending_approvals": store.count(Approval, {"status": "pending"}),
        "daily_budget_live_inr": total_live_budget() / 100,
        "totals_7d": {
            "spend_inr": round(spend, 2),
            "impressions": impressions,
            "clicks": clicks,
            "ctr_pct": round(clicks / impressions * 100, 2) if impressions else 0,
            "cpc_inr": round(spend / clicks, 2) if clicks else 0,
        },
        "daily": [{"date": k, **{kk: round(vv, 2) for kk, vv in v.items()}} for k, v in sorted(daily.items())],
        "metrics_source": snaps[-1].source if snaps else None,
    }


@router.get("/audit")
def audit_log(limit: int = 100):
    return store.find(AuditLog, sort=[("_id", -1)], limit=min(max(limit, 1), 500))

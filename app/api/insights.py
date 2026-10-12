"""Read-only endpoints for the dashboard: app status, summary KPIs and the audit log."""
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from app.config import get_settings
from app.db.models import Approval, AuditLog, Campaign, MetricSnapshot
from app.db.session import get_session

router = APIRouter(prefix="/api", tags=["insights"])


@router.get("/status")
def status():
    s = get_settings()
    return {
        "app": "milo",
        "model": s.milo_model,
        "llm_key_set": bool(s.openrouter_api_key),
        "meta_api_version": s.meta_api_version,
        "meta_dry_run": s.meta_dry_run,
        "meta_page_name": s.meta_page_name,
        "meta_configured": bool(s.meta_access_token and s.meta_ad_account_id and s.meta_page_id),
        "metrics_provider": s.metrics_provider,
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
            "max_daily_budget_inr": s.optimizer_max_daily_budget_inr,
            "cooldown_hours": s.optimizer_cooldown_hours,
        },
    }


@router.post("/optimizer/run")
def optimizer_run(session: Session = Depends(get_session)):
    """Run the rule-based optimizer now. Creates pending approvals only."""
    from app.optimizer import run_optimizer

    return run_optimizer(session)


@router.get("/summary")
def summary(session: Session = Depends(get_session)):
    campaigns = session.exec(select(Campaign)).all()
    by_status = defaultdict(int)
    for c in campaigns:
        by_status[c.status] += 1
    snaps = session.exec(select(MetricSnapshot)).all()
    daily = defaultdict(lambda: {"spend": 0.0, "clicks": 0, "impressions": 0})
    for r in snaps:
        d = daily[r.date]
        d["spend"] += r.spend
        d["clicks"] += r.clicks
        d["impressions"] += r.impressions
    spend = sum(r.spend for r in snaps)
    clicks = sum(r.clicks for r in snaps)
    impressions = sum(r.impressions for r in snaps)
    pending = session.exec(select(func.count(Approval.id)).where(Approval.status == "pending")).one()
    return {
        "campaigns_total": len(campaigns),
        "by_status": dict(by_status),
        "pending_approvals": pending,
        "daily_budget_live_inr": sum(c.daily_budget for c in campaigns if c.status == "active") / 100,
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
def audit_log(limit: int = 100, session: Session = Depends(get_session)):
    return session.exec(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))).all()

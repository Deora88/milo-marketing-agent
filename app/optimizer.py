"""Optimization loop: reads stored metrics, spots weak and strong campaigns, and files recommendations.

Runs after every metrics pull (APScheduler) and on demand (POST /api/optimizer/run).
Recommendations are ordinary pending Approvals with source="optimizer" and a plain-language `reason`,
so nothing changes on Meta until a human clicks Approve. The rules are deterministic and explainable:

  * pause      - CTR well below the account average (or below a floor when there is only one campaign)
  * scale up   - CTR well above average at an average-or-better cost per click -> +step% budget
  * trim       - cost per click well above average -> -step% budget
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.approvals import create_approval
from app.config import Settings, get_settings
from app.db.models import Approval, Campaign, MetricSnapshot
from app.db.session import audit


@dataclass
class CampaignStats:
    campaign_id: int
    name: str
    status: str
    daily_budget_inr: float
    spend: float
    impressions: int
    clicks: int

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions * 100 if self.impressions else 0.0

    @property
    def cpc(self) -> float:
        return self.spend / self.clicks if self.clicks else 0.0


@dataclass
class Recommendation:
    campaign_id: int
    action: str  # pause_campaign | update_budget
    reason: str
    payload: dict = field(default_factory=dict)


def _round_budget(inr: float) -> int:
    """Budgets move in Rs 50 steps, minimum Rs 100."""
    return max(100, int(round(inr / 50.0)) * 50)


def analyze(stats: list[CampaignStats], s: Settings) -> list[Recommendation]:
    """Pure rule engine: same inputs, same recommendations. No database access."""
    eligible = [x for x in stats if x.status == "active" and x.impressions >= s.optimizer_min_impressions]
    if not eligible:
        return []

    clicks = sum(x.clicks for x in eligible)
    impressions = sum(x.impressions for x in eligible)
    spend = sum(x.spend for x in eligible)
    avg_ctr = clicks / impressions * 100 if impressions else 0.0
    avg_cpc = spend / clicks if clicks else 0.0
    step = s.optimizer_budget_step_pct / 100
    recs: list[Recommendation] = []

    def facts(x: CampaignStats) -> str:
        return f"Last 7 days: {x.impressions:,} impressions, {x.clicks:,} clicks, Rs {x.spend:,.0f} spent."

    if len(eligible) == 1:
        x = eligible[0]
        if x.ctr < s.optimizer_min_ctr_pct:
            recs.append(Recommendation(x.campaign_id, "pause_campaign",
                                       f"CTR is {x.ctr:.2f}%, below the {s.optimizer_min_ctr_pct:.2f}% floor. {facts(x)}"))
        return recs

    for x in eligible:
        if x.ctr < s.optimizer_pause_ctr_ratio * avg_ctr:
            gap = (1 - x.ctr / avg_ctr) * 100 if avg_ctr else 0
            recs.append(Recommendation(
                x.campaign_id, "pause_campaign",
                f"CTR {x.ctr:.2f}% is {gap:.0f}% below the account average of {avg_ctr:.2f}%. {facts(x)}",
            ))
        elif x.ctr >= s.optimizer_scale_ctr_ratio * avg_ctr and x.cpc <= avg_cpc:
            new_budget = min(_round_budget(x.daily_budget_inr * (1 + step)), s.optimizer_max_daily_budget_inr)
            if new_budget > x.daily_budget_inr:
                recs.append(Recommendation(
                    x.campaign_id, "update_budget",
                    f"Best performer: CTR {x.ctr:.2f}% vs {avg_ctr:.2f}% average at Rs {x.cpc:.2f} per click "
                    f"(average Rs {avg_cpc:.2f}). Raise budget Rs {x.daily_budget_inr:,.0f} -> Rs {new_budget:,.0f}/day. {facts(x)}",
                    {"daily_budget": int(new_budget * 100)},
                ))
        elif avg_cpc and x.cpc > s.optimizer_trim_cpc_ratio * avg_cpc:
            new_budget = _round_budget(x.daily_budget_inr * (1 - step))
            if new_budget < x.daily_budget_inr:
                recs.append(Recommendation(
                    x.campaign_id, "update_budget",
                    f"Cost per click Rs {x.cpc:.2f} is {(x.cpc / avg_cpc - 1) * 100:.0f}% above the Rs {avg_cpc:.2f} average. "
                    f"Trim budget Rs {x.daily_budget_inr:,.0f} -> Rs {new_budget:,.0f}/day. {facts(x)}",
                    {"daily_budget": int(new_budget * 100)},
                ))
    return recs


def collect_stats(session: Session) -> list[CampaignStats]:
    campaigns = session.exec(select(Campaign).where(Campaign.status.in_(["active", "paused"]))).all()
    out = []
    for c in campaigns:
        rows = session.exec(select(MetricSnapshot).where(MetricSnapshot.campaign_id == c.id)).all()
        out.append(CampaignStats(
            campaign_id=c.id, name=c.name, status=c.status, daily_budget_inr=c.daily_budget / 100,
            spend=sum(r.spend for r in rows), impressions=sum(r.impressions for r in rows), clicks=sum(r.clicks for r in rows),
        ))
    return out


def _cooling_down(session: Session, campaign_id: int, action: str, s: Settings) -> str | None:
    now = datetime.now(timezone.utc)
    rows = session.exec(select(Approval).where(Approval.campaign_id == campaign_id)).all()
    for a in rows:
        created = a.created_at if a.created_at.tzinfo else a.created_at.replace(tzinfo=timezone.utc)
        if a.status == "pending":
            return "a request is already waiting for approval"
        if a.source == "optimizer" and now - created < timedelta(hours=s.optimizer_cooldown_hours):
            return f"recommended within the last {s.optimizer_cooldown_hours}h"
        if a.source == "optimizer" and a.status == "rejected" and a.action == action and now - created < timedelta(hours=24):
            return "you rejected the same recommendation in the last 24h"
    return None


def run_optimizer(session: Session, settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    stats = collect_stats(session)
    created, skipped = [], []
    for rec in analyze(stats, s):
        why_not = _cooling_down(session, rec.campaign_id, rec.action, s)
        if why_not:
            skipped.append({"campaign_id": rec.campaign_id, "action": rec.action, "why": why_not})
            continue
        result = create_approval(session, rec.action, rec.campaign_id, rec.payload, reason=rec.reason, source="optimizer")
        if result["status"] == "pending_approval":
            created.append({"campaign_id": rec.campaign_id, "approval_id": result["approval_id"],
                            "summary": result["summary"], "reason": rec.reason})
        else:
            skipped.append({"campaign_id": rec.campaign_id, "action": rec.action, "why": result.get("message", "")})
    audit(session, "optimizer_run", checked=len(stats), recommended=len(created), skipped=len(skipped))
    session.commit()
    return {"checked": len(stats), "recommendations": created, "skipped": skipped}

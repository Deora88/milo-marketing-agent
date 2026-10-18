"""Optimization loop: rules, caps, cooldowns, approvals-only output, simulated-data labels."""
import asyncio

from fastapi.testclient import TestClient

from app import actions
from app.config import Settings
from app.db import store
from app.db.models import Approval, Campaign
from app.jobs.scheduler import pull_metrics
from app.main import app
from app.mcp_server.server import create_campaign_draft, list_pending_approvals, request_publish
from app.optimizer import CampaignStats, analyze, run_optimizer
from tests.conftest import WEB

S = Settings(_env_file=None, max_daily_budget_inr=5000)


def stats(cid, ctr_clicks, impressions=10_000, spend=1000.0, budget=500.0, status="active", simulated=False):
    return CampaignStats(cid, f"C{cid}", status, budget, spend, impressions, ctr_clicks, simulated)


def test_rules_pause_scale_trim():
    recs = {r.campaign_id: r for r in analyze([
        stats(1, 50),                 # CTR 0.5%  -> far below average -> pause
        stats(2, 250, spend=1000),    # CTR 2.5%, cheap clicks -> scale up
        stats(3, 150, spend=3000),    # average CTR, expensive clicks -> trim
        stats(4, 150, spend=1500),    # ordinary -> nothing
    ], S)}
    assert recs[1].action == "pause_campaign" and "below the account average" in recs[1].reason
    assert recs[2].action == "update_budget" and recs[2].payload == {"daily_budget": 60000}  # 500 +20% = 600
    assert recs[3].action == "update_budget" and recs[3].payload == {"daily_budget": 40000}  # 500 -20% = 400
    assert 4 not in recs


def test_rules_ignore_thin_data_drafts_and_respect_caps():
    assert analyze([stats(1, 1, impressions=500), stats(2, 50, impressions=500)], S) == []
    assert analyze([stats(1, 10, status="paused")], S) == []
    capped = analyze([stats(1, 50), stats(2, 400, spend=500, budget=5000)], S)
    assert all(r.campaign_id != 2 for r in capped)  # already at the max budget
    tight = Settings(_env_file=None, max_daily_budget_inr=500)
    assert all(r.action != "update_budget" or r.payload["daily_budget"] <= 50000
               for r in analyze([stats(1, 50), stats(2, 250)], tight))
    single = analyze([stats(1, 40)], S)  # one campaign, CTR 0.4% < 0.6% floor
    assert single[0].action == "pause_campaign"


def test_reason_says_when_data_is_simulated():
    recs = analyze([stats(1, 50, simulated=True), stats(2, 150, simulated=True)], S)
    assert recs and "simulated data" in recs[0].reason


def _running_campaigns(n=4):
    ids = []
    for i in range(n):
        cid = create_campaign_draft(name=f"Shop {i}", daily_budget_inr=200, countries=["IN"], age_min=20, age_max=45,
                                    headline="Fresh deals", primary_text="Visit us", link_url="https://example.com",
                                    platforms=["facebook"])["campaign_id"]
        aid = request_publish(cid)["approval_id"]
        asyncio.run(actions.execute(store.get(Approval, aid)))
        a = store.get(Approval, aid)
        a.status = "approved"
        store.save(a)
        ids.append(cid)
    return ids


def test_loop_files_approvals_only_and_cools_down(monkeypatch):
    monkeypatch.setattr("app.optimizer.get_settings", lambda: Settings(_env_file=None, optimizer_min_impressions=100,
                                                                         max_daily_budget_inr=5000,
                                                                         max_total_daily_budget_inr=50000))
    monkeypatch.setattr("app.approvals.get_settings", lambda: Settings(_env_file=None, max_daily_budget_inr=5000,
                                                                         max_total_daily_budget_inr=50000))
    _running_campaigns()
    asyncio.run(pull_metrics())  # pull also runs the optimizer
    recs = store.find(Approval, {"source": "optimizer"})
    assert recs, "simulated campaigns differ in quality, so at least one recommendation is expected"
    assert all(a.status == "pending" and "simulated data" in a.reason for a in recs)
    assert all(store.get(Campaign, a.campaign_id).status == "active" for a in recs)  # nothing applied
    again = run_optimizer()
    assert again["recommendations"] == [] and again["skipped"]  # cooldown / already pending
    pending = list_pending_approvals()["pending"]
    assert any(p["source"] == "optimizer" and p["reason"] for p in pending)


def test_api_run_and_status():
    c = TestClient(app, headers=WEB)
    assert c.get("/api/status").json()["optimizer"]["enabled"] is True
    r = c.post("/api/optimizer/run").json()
    assert set(r) == {"checked", "recommendations", "skipped"}


def test_failing_campaign_does_not_stop_the_pull(monkeypatch):
    ids = _running_campaigns(2)
    from app.metrics.simulated import SimulatedInsightsProvider
    real = SimulatedInsightsProvider.daily

    async def flaky(self, campaign, days=7):
        if campaign.id == ids[0]:
            raise RuntimeError("Meta timeout")
        return await real(self, campaign, days)

    monkeypatch.setattr(SimulatedInsightsProvider, "daily", flaky)
    assert asyncio.run(pull_metrics()) >= 1  # second campaign still written
    from app.db.models import AuditLog
    assert store.find(AuditLog, {"event": "metrics_failed"})


def test_empty_response_keeps_stored_metrics(monkeypatch):
    ids = _running_campaigns(1)
    asyncio.run(pull_metrics())
    from app.db.models import MetricSnapshot
    before = store.count(MetricSnapshot, {"campaign_id": ids[0]})
    from app.metrics.simulated import SimulatedInsightsProvider

    async def empty(self, campaign, days=7):
        return []

    monkeypatch.setattr(SimulatedInsightsProvider, "daily", empty)
    asyncio.run(pull_metrics())
    assert store.count(MetricSnapshot, {"campaign_id": ids[0]}) == before > 0

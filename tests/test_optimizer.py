"""Optimization loop: rules, cooldowns, approvals-only output, and the old-database migration."""
import asyncio
import sqlite3

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import actions
from app.config import Settings
from app.db.models import Approval, Campaign
from app.db.session import engine
from app.jobs.scheduler import pull_metrics
from app.main import app
from app.mcp_server.server import create_campaign_draft, list_pending_approvals, request_publish
from app.optimizer import CampaignStats, analyze, run_optimizer

S = Settings()


def stats(cid, ctr_clicks, impressions=10_000, spend=1000.0, budget=500.0, status="active"):
    return CampaignStats(cid, f"C{cid}", status, budget, spend, impressions, ctr_clicks)


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
    single = analyze([stats(1, 40)], S)  # one campaign, CTR 0.4% < 0.6% floor
    assert single[0].action == "pause_campaign"


def _live_campaigns(n=4):
    ids = []
    with Session(engine) as s:
        for i in range(n):
            cid = create_campaign_draft(name=f"Shop {i}", daily_budget_inr=500, countries=["IN"], age_min=20, age_max=45,
                                        headline="Fresh deals", primary_text="Visit us", link_url="https://example.com")["campaign_id"]
            aid = request_publish(cid)["approval_id"]
            asyncio.run(actions.execute(s, s.get(Approval, aid)))
            a = s.get(Approval, aid); a.status = "approved"; s.add(a); s.commit()
            ids.append(cid)
    return ids


def test_loop_files_approvals_only_and_cools_down():
    _live_campaigns()
    asyncio.run(pull_metrics())  # pull also runs the optimizer
    with Session(engine) as s:
        recs = s.exec(select(Approval).where(Approval.source == "optimizer")).all()
        assert recs, "simulated campaigns differ in quality, so at least one recommendation is expected"
        assert all(a.status == "pending" and a.reason for a in recs)
        assert all(s.get(Campaign, a.campaign_id).status == "active" for a in recs)  # nothing applied
        again = run_optimizer(s)
    assert again["recommendations"] == [] and again["skipped"]  # cooldown / already pending
    pending = list_pending_approvals()["pending"]
    assert any(p["source"] == "optimizer" and p["reason"] for p in pending)


def test_api_run_and_status():
    c = TestClient(app)
    assert c.get("/api/status").json()["optimizer"]["enabled"] is True
    r = c.post("/api/optimizer/run").json()
    assert set(r) == {"checked", "recommendations", "skipped"}


def test_old_database_gets_new_columns(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE approval (id INTEGER PRIMARY KEY, action VARCHAR, campaign_id INTEGER, payload JSON, "
                "summary VARCHAR, status VARCHAR, created_at DATETIME, decided_at DATETIME)")
    con.execute("INSERT INTO approval (action, campaign_id, payload, summary, status) VALUES ('pause_campaign', 1, '{}', 'old', 'approved')")
    con.commit(); con.close()
    from sqlmodel import create_engine
    import app.db.session as sess
    original = sess.engine
    sess.engine = create_engine(f"sqlite:///{db}")
    try:
        sess.init_db()
    finally:
        sess.engine = original
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(approval)")}
    assert {"reason", "source"} <= cols
    assert sqlite3.connect(db).execute("SELECT source FROM approval").fetchone()[0] == "agent"

"""MCP tools can draft and request; only actions.execute (after Approve) changes a live ad."""
import asyncio
from types import SimpleNamespace

from sqlmodel import Session

from app import actions
from app.agent.guard import tool_guard
from app.db.models import Approval, Campaign
from app.db.session import engine
from app.mcp_server.server import (create_campaign_draft, get_campaign_metrics, request_budget_change,
                                   request_pause, request_publish)

DRAFT = dict(name="Bakery Pune", daily_budget_inr=500, countries=["in"], age_min=22, age_max=45,
             headline="Fresh bread daily", primary_text="Order warm loaves near you.", link_url="https://example.com")


def set_status(cid, status):
    with Session(engine) as s:
        c = s.get(Campaign, cid); c.status = status; s.add(c); s.commit()


def test_draft_validation():
    bad = create_campaign_draft(**{**DRAFT, "link_url": "http://x", "daily_budget_inr": 10})
    assert bad["status"] == "invalid" and len(bad["errors"]) == 2
    assert create_campaign_draft(**{**DRAFT, "special_ad_category": "HOUSING"})["status"] == "invalid"


def test_request_publish_creates_approval_but_touches_nothing():
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    r = request_publish(cid)
    assert r["status"] == "pending_approval"
    assert request_publish(cid)["approval_id"] == r["approval_id"]  # no duplicate approvals
    with Session(engine) as s:
        c = s.get(Campaign, cid)
        assert c.status == "pending_approval" and c.meta_campaign_id is None


def test_approve_executes_on_meta_dry_run():
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    aid = request_publish(cid)["approval_id"]
    with Session(engine) as s:
        asyncio.run(actions.execute(s, s.get(Approval, aid)))
        s.commit()
        c = s.get(Campaign, cid)
        assert c.status == "active" and c.meta_campaign_id.startswith("dry_") and c.meta_ad_id


def test_status_rules():
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    assert request_pause(cid)["status"] == "error"  # can't pause a draft
    set_status(cid, "active")
    assert request_publish(cid)["status"] == "error"  # already live
    assert request_budget_change(cid, 500)["status"] == "error"  # 500 == current budget
    r = request_budget_change(cid, 750)
    with Session(engine) as s:
        assert s.get(Approval, r["approval_id"]).payload == {"daily_budget": 75000}
    assert request_budget_change(cid, 50)["status"] == "invalid"


def test_guard_blocks_unknown_tools_and_audits_requests():
    assert tool_guard(SimpleNamespace(name="delete_everything"), {}, None)["status"] == "blocked"
    assert tool_guard(SimpleNamespace(name="list_campaigns"), {}, None) is None
    assert tool_guard(SimpleNamespace(name="request_publish"), {"campaign_id": 1}, None) is None


def test_simulated_metrics_only_for_live_campaigns():
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    assert asyncio.run(get_campaign_metrics(cid))["daily"] == []
    set_status(cid, "active")
    m = asyncio.run(get_campaign_metrics(cid))
    assert m["source"] == "simulated" and len(m["daily"]) == 7 and m["totals"]["spend_inr"] > 0
    assert m == asyncio.run(get_campaign_metrics(cid))  # deterministic


def test_local_targeting_draft_and_summary():
    local = {**DRAFT, "location_name": "Vaishali, Ghaziabad", "latitude": 28.6457, "longitude": 77.339, "radius_km": 4}
    cid = create_campaign_draft(**local)["campaign_id"]
    r = request_publish(cid)
    assert "4 km around Vaishali, Ghaziabad" in r["summary"]
    assert create_campaign_draft(**{**local, "radius_km": 0.2})["status"] == "invalid"
    partial = create_campaign_draft(**{**DRAFT, "latitude": 28.6})
    assert partial["status"] == "invalid" and "together" in partial["errors"][0]

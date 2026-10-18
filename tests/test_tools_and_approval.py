"""MCP tools can draft and request; only actions.execute (after Approve) changes a live ad."""
import asyncio
from types import SimpleNamespace

from app import actions
from app.agent.guard import tool_guard
from app.db import store
from app.db.models import Approval, AuditLog, Campaign
from app.mcp_server.server import (create_campaign_draft, get_campaign_metrics, request_budget_change,
                                   request_pause, request_publish)

DRAFT = dict(name="Bakery Pune", daily_budget_inr=300, countries=["in"], age_min=22, age_max=45,
             headline="Fresh bread daily", primary_text="Order warm loaves near you.", link_url="https://example.com",
             platforms=["facebook"])


def set_status(cid, status):
    c = store.get(Campaign, cid)
    c.status = status
    store.save(c)


def test_draft_validation():
    bad = create_campaign_draft(**{**DRAFT, "link_url": "http://x", "daily_budget_inr": 10})
    assert bad["status"] == "invalid" and len(bad["errors"]) == 2
    assert create_campaign_draft(**{**DRAFT, "special_ad_category": "HOUSING"})["status"] == "invalid"
    assert create_campaign_draft(**{**DRAFT, "platforms": ["tiktok"]})["status"] == "invalid"
    over = create_campaign_draft(**{**DRAFT, "daily_budget_inr": 5000})
    assert over["status"] == "invalid" and "cap" in over["errors"][0]


def test_request_publish_creates_approval_but_touches_nothing():
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    r = request_publish(cid)
    assert r["status"] == "pending_approval"
    assert "Fresh bread daily" in r["summary"] and "https://example.com" in r["summary"] and "Facebook" in r["summary"]
    assert request_publish(cid)["approval_id"] == r["approval_id"]  # no duplicate approvals
    c = store.get(Campaign, cid)
    assert c.status == "pending_approval" and c.meta_campaign_id is None


def test_instagram_needs_an_image():
    cid = create_campaign_draft(**{**DRAFT, "platforms": ["facebook", "instagram"]})["campaign_id"]
    r = request_publish(cid)
    assert r["status"] == "error" and "image" in r["message"]
    c = store.get(Campaign, cid)
    c.image_path = "data/ad_images/x.png"
    store.save(c)
    assert request_publish(cid)["status"] == "pending_approval"


def test_approve_in_dry_run_keeps_ads_paused_when_spend_is_off():
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    aid = request_publish(cid)["approval_id"]
    asyncio.run(actions.execute(store.get(Approval, aid)))
    c = store.get(Campaign, cid)
    # dry run never spends, so it is allowed to "activate"; ids are fake and marked as such
    assert c.status == "active" and c.meta_mode == "dry_run" and c.meta_campaign_id.startswith("dry_")


def test_status_rules_caps_and_budget_updates():
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    assert request_pause(cid)["status"] == "error"  # can't pause a draft
    set_status(cid, "active")
    assert request_publish(cid)["status"] == "error"  # already live
    assert request_budget_change(cid, 300)["status"] == "error"  # == current budget
    r = request_budget_change(cid, 400)
    assert store.get(Approval, r["approval_id"]).payload == {"daily_budget": 40000}
    # a new amount updates the waiting request instead of returning the old one
    r2 = request_budget_change(cid, 450)
    assert r2["approval_id"] == r["approval_id"] and store.get(Approval, r["approval_id"]).payload == {"daily_budget": 45000}
    assert request_budget_change(cid, 50)["status"] == "invalid"
    over = request_budget_change(cid, 900)
    assert over["status"] == "error" and "cap" in over["message"]


def test_total_cap_across_running_campaigns():
    a = create_campaign_draft(**{**DRAFT, "daily_budget_inr": 500})["campaign_id"]
    b = create_campaign_draft(**{**DRAFT, "daily_budget_inr": 500})["campaign_id"]
    c = create_campaign_draft(**{**DRAFT, "daily_budget_inr": 200})["campaign_id"]
    set_status(a, "active")
    set_status(b, "active")
    r = request_publish(c)
    assert r["status"] == "error" and "account cap" in r["message"]


def test_resume_is_refused_while_spend_is_off(monkeypatch):
    monkeypatch.setenv("META_DRY_RUN", "false")
    from app.config import get_settings
    get_settings.cache_clear()
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    set_status(cid, "paused")
    r = request_publish(cid)
    assert r["status"] == "error" and "META_ALLOW_SPEND" in r["message"]


def test_guard_blocks_unknown_tools_and_audits_requests():
    assert tool_guard(SimpleNamespace(name="delete_everything"), {}, None)["status"] == "blocked"
    assert tool_guard(SimpleNamespace(name="list_campaigns"), {}, None) is None
    assert tool_guard(SimpleNamespace(name="request_publish"), {"campaign_id": 1}, None) is None
    rows = store.find(AuditLog, {"event": "change_requested_by_agent"})
    assert len(rows) == 1 and rows[0].detail == {"tool": "request_publish", "args": {"campaign_id": 1}}


def test_guard_is_wired_into_the_agent():
    from app.agent.agent import build_agent

    agent = build_agent()
    assert agent.before_tool_callback is tool_guard


def test_simulated_metrics_only_for_running_campaigns():
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    assert asyncio.run(get_campaign_metrics(cid))["daily"] == []
    set_status(cid, "active")
    m = asyncio.run(get_campaign_metrics(cid))
    # created today: one day of numbers, never days before the campaign existed
    assert m["source"] == "simulated" and len(m["daily"]) == 1 and m["totals"]["spend_inr"] > 0
    assert m == asyncio.run(get_campaign_metrics(cid))  # deterministic
    set_status(cid, "paused")
    assert asyncio.run(get_campaign_metrics(cid))["daily"] == []  # paused campaigns do not spend


def test_local_targeting_draft_and_summary():
    local = {**DRAFT, "location_name": "Vaishali, Ghaziabad", "latitude": 28.6457, "longitude": 77.339, "radius_km": 4}
    cid = create_campaign_draft(**local)["campaign_id"]
    r = request_publish(cid)
    assert "4 km around Vaishali, Ghaziabad" in r["summary"]
    assert create_campaign_draft(**{**local, "radius_km": 0.2})["status"] == "invalid"
    partial = create_campaign_draft(**{**DRAFT, "latitude": 28.6})
    assert partial["status"] == "invalid" and "together" in partial["errors"][0]

"""Campaign drafts: incomplete drafts, requirements (known / assumed / missing), editing, version history."""
import asyncio

from fastapi.testclient import TestClient

from app import actions, campaign_state
from app.db import store
from app.db.models import Approval, Campaign, CampaignVersion
from app.main import app
from app.mcp_server.server import (create_campaign_draft, get_campaign_history, get_campaign_requirements,
                                   request_publish, update_campaign_draft)
from tests.conftest import WEB


def _items(cid):
    return {i["key"]: i for i in get_campaign_requirements(cid)["items"]}


def test_incomplete_draft_lists_missing_and_assumed_and_blocks_publish():
    r = create_campaign_draft(name="Doorstep Car Care", daily_budget_inr=150, platforms=["facebook"])
    assert r["status"] == "draft_saved"
    assert set(r["missing"]) == {"Headline", "Primary text", "Landing page"}
    items = _items(r["campaign_id"])
    assert items["ages"]["status"] == "assumed" and "18-65" in items["ages"]["note"]
    assert items["area"]["status"] == "assumed" and items["special_ad_category"]["status"] == "assumed"
    assert items["budget"]["status"] == "known"
    blocked = request_publish(r["campaign_id"])
    assert blocked["status"] == "error" and "Still missing" in blocked["message"]
    assert store.count(Approval) == 0


def test_agent_assumptions_edits_and_confirmations():
    r = create_campaign_draft(name="Doorstep Car Care", daily_budget_inr=150, countries=["IN"], age_min=22,
                              age_max=50, special_ad_category="NONE", platforms=["facebook"],
                              location_name="Connaught Place", latitude=28.63, longitude=77.21, radius_km=20,
                              assumed={"area": "user said Delhi NCR; 20 km around Connaught Place chosen by Milo"})
    cid = r["campaign_id"]
    assert _items(cid)["area"]["status"] == "assumed" and _items(cid)["ages"]["status"] == "known"
    u = update_campaign_draft(cid, headline="Car wash at your door", primary_text="Foam wash from Rs 499.",
                              link_url="https://deora88.github.io/doorstep-car-care/", confirmed=["area"],
                              note="user gave the copy and link")
    assert u["status"] == "draft_updated" and u["version"] == 2 and u["missing"] == [] and u["assumed"] == []
    assert any(ch.startswith("Headline: (empty) -> Car wash") for ch in u["changed"])
    budget = update_campaign_draft(cid, daily_budget_inr=200, note="user raised the budget")
    assert budget["changed"] == ["Daily budget: ₹150/day -> ₹200/day"] and budget["version"] == 3
    assert update_campaign_draft(cid, daily_budget_inr=200)["message"] == "Nothing changed."
    bad = update_campaign_draft(cid, headline="x" * 41)
    assert bad["status"] == "invalid" and "40" in bad["errors"][0]
    h = get_campaign_history(cid)
    assert h["current_version"] == 3 and [v["version"] for v in h["versions"]] == [3, 2, 1]
    assert h["versions"][0]["note"] == "user raised the budget" and h["versions"][-1]["changes"] == []
    assert request_publish(cid)["status"] == "pending_approval"
    locked = update_campaign_draft(cid, headline="New")  # publish request waiting: no silent edits
    assert locked["status"] == "invalid" and "Reject it first" in locked["errors"][0]


def test_ui_edit_confirm_and_history_routes():
    c = TestClient(app, headers=WEB)
    cid = create_campaign_draft(name="Cafe", daily_budget_inr=200, platforms=["facebook"])["campaign_id"]
    assert c.get(f"/api/campaigns/{cid}/requirements").json()["ready"] is False
    r = c.patch(f"/api/campaigns/{cid}", json={"headline": "Fresh coffee", "primary_text": "Open 7 am.",
                                               "link_url": "https://example.com", "confirmed": ["ages"]})
    assert r.status_code == 200 and r.json()["campaign"]["version"] == 2
    req = c.get(f"/api/campaigns/{cid}/requirements").json()
    assert req["ready"] is True and "Ages" not in req["assumed"]
    vers = c.get(f"/api/campaigns/{cid}/versions").json()
    assert vers["versions"][0]["source"] == "user" and len(vers["versions"][0]["changes"]) == 3
    assert c.patch(f"/api/campaigns/{cid}", json={"link_url": "http://insecure"}).status_code == 409
    assert c.patch("/api/campaigns/999", json={"name": "x"}).status_code == 404
    assert TestClient(app).patch(f"/api/campaigns/{cid}", json={"name": "x"}).status_code == 403  # CSRF


def test_image_and_approved_budget_change_are_versions():
    import io

    from PIL import Image

    c = TestClient(app, headers=WEB)
    cid = create_campaign_draft(name="Bakery", daily_budget_inr=200, headline="Fresh bread", primary_text="Daily.",
                                link_url="https://example.com")["campaign_id"]
    png = io.BytesIO()
    Image.new("RGB", (600, 600), (200, 80, 40)).save(png, "PNG")
    for _ in range(2):
        assert c.post(f"/api/campaigns/{cid}/image", files={"file": ("a.png", png.getvalue(), "image/png")}).status_code == 200
    changes = [v["changes"] for v in campaign_state.history(cid)[:2]]
    assert changes == [["Ad image: replaced"], ["Ad image: added"]]
    # a budget change approved on a live campaign is a version too
    store.update(Campaign, cid, {"status": "active", "meta_campaign_id": "c1", "meta_adset_id": "s1"})
    a = store.insert(Approval(action="update_budget", campaign_id=cid, payload={"daily_budget": 30000}))

    class Meta:
        s = type("S", (), {"meta_dry_run": True})()

        async def aclose(self):
            pass

    async def fake_update(client, camp, new):
        return None

    import app.meta.campaigns as meta
    orig = meta.update_budget
    meta.update_budget = fake_update
    try:
        asyncio.run(actions.execute(a, Meta()))
    finally:
        meta.update_budget = orig
    latest = store.find(CampaignVersion, {"campaign_id": cid}, sort=[("version", -1)], limit=1)[0]
    assert latest.source == "approval" and campaign_state.describe_changes(latest.changes) == [
        "Daily budget: ₹200/day -> ₹300/day"]


def test_new_tools_are_allow_listed():
    from app.mcp_server.server import TOOL_NAMES

    assert {"update_campaign_draft", "get_campaign_requirements", "get_campaign_history"} <= set(TOOL_NAMES)


def test_validate_wrong_types_give_errors_not_crashes():
    from app.campaign_state import validate

    base = {"name": "Diwali wash", "countries": ["IN"], "daily_budget_inr": 200}
    assert validate({**base, "age_min": None})[0] == []  # missing age = default 18
    assert validate({**base, "daily_budget_inr": "300"})[1]["daily_budget"] == 30000  # "300" is read as 300
    assert validate({**base, "countries": "IN"})[1]["countries"] == ["IN"]
    for bad in ({"daily_budget_inr": "lots"}, {"age_max": "old"}, {"age_min": 18.5}, {"daily_budget_inr": float("nan")},
                {"latitude": "x", "longitude": 77, "radius_km": 5}, {"daily_budget_inr": True}):
        errors = validate({**base, **bad})[0]
        assert any("must be a number" in e for e in errors), bad

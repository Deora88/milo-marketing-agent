"""Small hardening checks from the project review."""
import io

from fastapi.testclient import TestClient
from PIL import Image

from app.db import store
from app.db.models import Campaign
from app.main import app
from app.mcp_server import server
from tests.conftest import WEB


def _draft(**kw):
    args = dict(name="Diwali wash", daily_budget_inr=200, countries=["IN"], age_min=18, age_max=45,
                headline="Shine at your door", primary_text="Foam wash, we come to you.",
                link_url="https://example.com")
    args.update(kw)
    return server.create_campaign_draft(**args)


def test_campaign_draft_checks_name_and_countries():
    assert _draft()["status"] != "invalid"
    assert "name" in " ".join(_draft(name="  ")["errors"])
    assert "2-letter" in " ".join(_draft(countries=["India"])["errors"])
    assert "country" in " ".join(_draft(countries=[])["errors"])
    local = _draft(countries=[], latitude=28.6, longitude=77.3, radius_km=5)
    assert local["status"] != "invalid"


def test_campaign_image_must_really_be_an_image():
    c = TestClient(app, headers=WEB)
    cid = store.insert(Campaign(name="x", daily_budget=20000, countries=["IN"], age_min=18, age_max=40,
                                headline="h", primary_text="p", link_url="https://e.com")).id
    r = c.post(f"/api/campaigns/{cid}/image", files={"file": ("a.png", b"not an image", "image/png")})
    assert r.status_code == 415
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (200, 80, 40)).save(buf, "PNG")
    r = c.post(f"/api/campaigns/{cid}/image", files={"file": ("a.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200


def test_chat_rejects_empty_or_huge_messages():
    c = TestClient(app, headers=WEB)
    assert c.post("/api/chat", json={"session_id": "s", "message": ""}).status_code == 422
    assert c.post("/api/chat", json={"session_id": "s", "message": "x" * 9000}).status_code == 422


def test_status_reports_post_readiness():
    s = TestClient(app, headers=WEB).get("/api/status").json()
    assert "ready" in s["posts"]


def test_delete_conversation_and_lead():
    from app.db.models import CustomerProfile, InboundMessage, Lead

    c = TestClient(app, headers=WEB)
    for i in range(2):
        store.insert(InboundMessage(mid=f"m.del{i}", platform="messenger", page_id="P1", sender_id="U9",
                                    recipient_id="P1", text="call me 9871701615"))
    store.insert(Lead(platform="messenger", page_id="P1", customer_id="U9", phone="9871701615"))
    other = store.insert(Lead(platform="messenger", page_id="P1", customer_id="U8", phone="9871701616"))
    store.insert(CustomerProfile(platform="messenger", customer_id="U9", name="Asha"))
    r = c.delete("/api/inbox/messenger/P1/U9")
    assert r.status_code == 200 and r.json()["deleted"] == {"messages": 2, "leads": 1}
    assert c.get("/api/inbox/messenger/P1/U9").status_code == 404
    assert store.find(CustomerProfile, {"customer_id": "U9"}) == []
    assert c.delete("/api/inbox/messenger/P1/U9").status_code == 404
    assert c.delete(f"/api/leads/{other.id}").status_code == 200
    assert c.delete(f"/api/leads/{other.id}").status_code == 404
    assert c.delete("/api/leads/1", headers={"X-Milo-Client": ""}).status_code == 403  # CSRF header still needed

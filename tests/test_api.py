"""HTTP API used by the Apper frontend: CORS, status, summary, change requests, approvals, audit."""
import asyncio

from fastapi.testclient import TestClient

from app.jobs.scheduler import pull_metrics
from app.main import app
from app.mcp_server.server import create_campaign_draft

DRAFT = dict(name="Bakery Pune", daily_budget_inr=500, countries=["IN"], age_min=22, age_max=45,
             headline="Fresh bread daily", primary_text="Warm loaves near you.", link_url="https://example.com")
LOCAL_DEV = "http://localhost:5173"


def client():
    return TestClient(app)


def test_cors_allows_only_local_origins():
    r = client().options("/api/campaigns", headers={"Origin": LOCAL_DEV, "Access-Control-Request-Method": "GET"})
    assert r.headers["access-control-allow-origin"] == LOCAL_DEV
    for origin in ("https://preview--milo.apper.so", "https://evil.example"):
        bad = client().options("/api/campaigns", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in bad.headers


def test_ui_served_at_root():
    r = client().get("/")
    assert r.status_code == 200 and "Chat with Milo" in r.text


def test_full_flow_through_http():
    c = client()
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    assert c.get("/api/status").json()["meta_dry_run"] is True

    r = c.post(f"/api/campaigns/{cid}/requests", json={"action": "publish"})
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    assert c.post(f"/api/campaigns/{cid}/requests", json={"action": "pause"}).status_code == 409

    pending = c.get("/api/approvals").json()
    assert pending[0]["campaign_name"] == "Bakery Pune"
    assert c.post(f"/api/approvals/{pending[0]['id']}/approve").json()["campaign"]["status"] == "active"

    asyncio.run(pull_metrics())
    s = c.get("/api/summary").json()
    assert s["by_status"] == {"active": 1} and s["totals_7d"]["spend_inr"] > 0 and len(s["daily"]) == 7
    assert c.get("/api/campaigns", params={"status": "active", "q": "Bakery"}).json()[0]["id"] == cid
    assert len(c.get(f"/api/campaigns/{cid}/approvals").json()) == 1
    assert {e["event"] for e in c.get("/api/audit").json()} >= {"draft_created", "approval_requested", "action_executed"}
    assert c.post(f"/api/campaigns/{cid}/requests", json={"action": "budget", "daily_budget_inr": 50}).status_code == 422

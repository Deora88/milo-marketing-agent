"""HTTP API: security checks, status, summary, change requests, approvals (incl. the edge cases), audit."""
import asyncio

import pytest
import respx
from fastapi.testclient import TestClient

from app import actions
from app.db import store
from app.db.models import Approval, AuditLog, Campaign
from app.jobs.scheduler import pull_metrics
from app.main import app
from app.mcp_server.server import create_campaign_draft, request_publish
from tests.conftest import WEB

DRAFT = dict(name="Bakery Pune", daily_budget_inr=300, countries=["IN"], age_min=22, age_max=45,
             headline="Fresh bread daily", primary_text="Warm loaves near you.", link_url="https://example.com",
             platforms=["facebook"])
LOCAL_DEV = "http://localhost:5173"


def client():
    return TestClient(app, headers=WEB)


def _pending_publish():
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    return cid, request_publish(cid)["approval_id"]


# ---------------------------------------------------------------- security
def test_cors_allows_only_local_origins():
    r = client().options("/api/campaigns", headers={"Origin": LOCAL_DEV, "Access-Control-Request-Method": "GET"})
    assert r.headers["access-control-allow-origin"] == LOCAL_DEV
    for origin in ("https://preview--milo.apper.so", "https://evil.example"):
        bad = client().options("/api/campaigns", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in bad.headers


def test_cross_site_post_cannot_approve():
    """What a hidden form on another site sends: no X-Milo-Client header -> refused, nothing executed."""
    _, aid = _pending_publish()
    r = TestClient(app).post(f"/api/approvals/{aid}/approve",
                             headers={"Origin": "https://evil.example", "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403
    assert store.get(Approval, aid).status == "pending"


def test_dns_rebinding_host_is_refused():
    r = TestClient(app, base_url="http://attacker.example").get("/api/status")
    assert r.status_code == 400


def test_optional_api_token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")
    from app.config import get_settings
    get_settings.cache_clear()
    assert client().get("/api/status").status_code == 401
    assert client().get("/api/status", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_ui_served_at_root():
    r = client().get("/")
    assert r.status_code == 200 and "Chat with Milo" in r.text


# ---------------------------------------------------------------- flow
def test_full_flow_through_http():
    c = client()
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    st = c.get("/api/status").json()
    assert st["meta_dry_run"] is True and st["database"] == "mongodb" and st["spend"]["allowed"] is False

    r = c.post(f"/api/campaigns/{cid}/requests", json={"action": "publish"})
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    assert c.post(f"/api/campaigns/{cid}/requests", json={"action": "pause"}).status_code == 409

    pending = c.get("/api/approvals").json()
    assert pending[0]["campaign_name"] == "Bakery Pune"
    assert c.post(f"/api/approvals/{pending[0]['id']}/approve").json()["campaign"]["status"] == "active"

    asyncio.run(pull_metrics())
    s = c.get("/api/summary").json()
    assert s["by_status"] == {"active": 1} and s["totals_7d"]["spend_inr"] > 0 and len(s["daily"]) == 1
    assert c.get("/api/campaigns", params={"status": "active", "q": "bakery"}).json()[0]["id"] == cid
    assert len(c.get(f"/api/campaigns/{cid}/approvals").json()) == 1
    assert {e["event"] for e in c.get("/api/audit").json()} >= {"draft_created", "approval_requested", "action_executed"}
    assert c.post(f"/api/campaigns/{cid}/requests", json={"action": "budget", "daily_budget_inr": 50}).status_code == 422


# ---------------------------------------------------------------- approval gate edge cases
def test_reject_returns_campaign_to_draft():
    cid, aid = _pending_publish()
    assert client().post(f"/api/approvals/{aid}/reject").json()["approval"]["status"] == "rejected"
    assert store.get(Campaign, cid).status == "draft"
    assert store.find(AuditLog, {"event": "approval_rejected"})


def test_approve_twice_or_after_reject_or_unknown():
    c = client()
    _, aid = _pending_publish()
    assert c.post(f"/api/approvals/{aid}/approve").status_code == 200
    second = c.post(f"/api/approvals/{aid}/approve")
    assert second.status_code == 409 and "already approved" in second.json()["detail"]
    _, aid2 = _pending_publish()
    c.post(f"/api/approvals/{aid2}/reject")
    assert c.post(f"/api/approvals/{aid2}/approve").status_code == 409
    assert c.post("/api/approvals/9999/approve").status_code == 404


def test_concurrent_approve_runs_once(monkeypatch):
    """Two Approve clicks at the same time: the atomic claim lets only one execute."""
    _, aid = _pending_publish()
    calls = []
    real = actions.execute

    async def slow_execute(approval, client=None):
        calls.append(approval.id)
        await asyncio.sleep(0.05)
        return await real(approval, client)

    monkeypatch.setattr(actions, "execute", slow_execute)
    from app.api.approvals import approve

    async def both():
        return await asyncio.gather(approve(aid), approve(aid), return_exceptions=True)

    results = asyncio.run(both())
    assert len(calls) == 1
    assert sum(isinstance(r, Exception) for r in results) == 1


def test_failed_approve_is_audited_and_retryable(monkeypatch):
    c = client()
    _, aid = _pending_publish()

    async def boom(approval, client=None):
        raise RuntimeError("Meta down, token EAAB1234567890abcdefghijklmnop")

    monkeypatch.setattr(actions, "execute", boom)
    r = c.post(f"/api/approvals/{aid}/approve")
    assert r.status_code == 502 and "EAAB" not in r.json()["detail"]  # secrets are masked
    assert store.get(Approval, aid).status == "pending"  # can be retried
    fail = store.find(AuditLog, {"event": "action_failed"})
    assert fail and "***" in fail[0].detail["error"]
    monkeypatch.undo()
    assert c.post(f"/api/approvals/{aid}/approve").status_code == 200


@respx.mock(assert_all_mocked=True)
def test_mcp_tools_never_call_meta_in_live_mode(monkeypatch):
    """Live mode, no HTTP routes mocked: any Meta call would raise. Drafting and requesting must not call Meta."""
    monkeypatch.setenv("META_DRY_RUN", "false")
    from app.config import get_settings
    get_settings.cache_clear()
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    assert request_publish(cid)["status"] == "pending_approval"
    assert respx.calls.call_count == 0


def test_dry_run_ids_are_not_used_in_live_mode(monkeypatch):
    cid = create_campaign_draft(**DRAFT)["campaign_id"]
    c = store.get(Campaign, cid)
    c.status, c.meta_mode, c.meta_campaign_id = "active", "dry_run", "dry_abc"
    store.save(c)
    aid = store.insert(Approval(action="pause_campaign", campaign_id=cid)).id
    monkeypatch.setenv("META_DRY_RUN", "false")
    from app.config import get_settings
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="dry-run"):
        asyncio.run(actions.execute(store.get(Approval, aid)))

"""An approval left "executing" (process died mid-call) is put back to pending, so it can be retried or rejected."""
from datetime import timedelta

from fastapi.testclient import TestClient

from app.approvals import recover_stuck_approvals
from app.db import store
from app.db.models import Approval, AuditLog, Campaign, now
from app.main import app
from tests.conftest import WEB


def _stuck(minutes_ago: float | None) -> Approval:
    c = store.insert(Campaign(name="Cafe", daily_budget=15000, platforms=["facebook"], link_url="https://example.com"))
    since = None if minutes_ago is None else now() - timedelta(minutes=minutes_ago)
    return store.insert(Approval(action="publish_campaign", campaign_id=c.id, status="executing",
                                 executing_since=since))


def test_old_executing_approval_goes_back_to_pending():
    old, recent = _stuck(30), _stuck(1)
    assert recover_stuck_approvals() == [old.id]  # default timeout: 10 minutes
    assert store.get(Approval, old.id).status == "pending"
    assert store.get(Approval, old.id).executing_since is None
    assert store.get(Approval, recent.id).status == "executing"  # may still be running
    assert store.find(AuditLog, {"event": "approval_recovered"})


def test_startup_recovers_everything_including_legacy_rows():
    a, legacy = _stuck(1), _stuck(None)
    assert sorted(recover_stuck_approvals(older_than_minutes=0)) == sorted([a.id, legacy.id])
    assert recover_stuck_approvals(older_than_minutes=0) == []  # idempotent


def test_recovered_approval_can_be_rejected_or_approved_again():
    a, b = _stuck(30), _stuck(30)
    recover_stuck_approvals()
    client = TestClient(app)
    assert client.post(f"/api/approvals/{a.id}/reject", headers=WEB).status_code == 200
    r = client.post(f"/api/approvals/{b.id}/approve", headers=WEB)
    assert r.status_code == 200 and r.json()["approval"]["status"] == "approved"
    assert store.get(Approval, b.id).executing_since is None


def test_approve_marks_executing_since_while_running(monkeypatch):
    c = store.insert(Campaign(name="Cafe", daily_budget=15000, platforms=["facebook"], link_url="https://example.com"))
    a = store.insert(Approval(action="publish_campaign", campaign_id=c.id))
    seen = {}

    async def fake_execute(approval, client=None):
        seen["since"] = store.get(Approval, approval.id).executing_since
        return store.get(Campaign, approval.campaign_id)

    monkeypatch.setattr("app.actions.execute", fake_execute)
    r = TestClient(app).post(f"/api/approvals/{a.id}/approve", headers=WEB)
    assert r.status_code == 200 and seen["since"] is not None

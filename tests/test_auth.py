"""Sign-in and roles: sessions, first-run setup, admin-only approval, lockout, who-did-it in the records."""
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.approvals import create_approval
from app.config import get_settings
from app.db import store
from app.db.models import Approval, AuditLog, Campaign, Session, User
from app.main import app
from tests.conftest import WEB

PW = "correct horse 42"


@pytest.fixture(autouse=True)
def sign_in_on(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    auth.forget_login_attempts()
    yield
    auth.forget_login_attempts()


def _client(email=None, role="editor"):
    c = TestClient(app, headers=WEB)
    if email:
        if not store.find(User, {"email": email}):
            auth.create_user(email, PW, role)
        assert c.post("/api/auth/login", json={"email": email, "password": PW}).status_code == 200
    return c


def _pending_approval() -> int:
    c = store.insert(Campaign(name="Test", daily_budget=10000, status="active"))
    return create_approval("pause_campaign", c.id, source="user")["approval_id"]


def test_api_needs_a_session_but_health_webhooks_and_the_page_do_not():
    c = _client()
    r = c.get("/api/campaigns")
    assert r.status_code == 401 and r.json()["auth"] == "login"
    assert c.post("/api/optimizer/run").status_code == 401
    assert c.get("/api/health").status_code == 200
    assert c.get("/").status_code == 200  # the page itself is public code; its data is not
    assert c.get("/webhooks/meta").status_code == 403  # reaches the route (wrong verify token), not the sign-in wall
    assert c.get("/api/auth/state").json() == {"auth_required": True, "setup_needed": True, "user": None}


def test_first_run_setup_creates_one_admin_then_closes():
    c = _client()
    assert c.post("/api/auth/setup", json={"email": "Boss@Example.com", "password": "short"}).status_code == 409
    r = c.post("/api/auth/setup", json={"email": "Boss@Example.com", "password": PW, "name": "Boss"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin" and r.json()["user"]["email"] == "boss@example.com"
    assert c.get("/api/campaigns").status_code == 200  # signed in by the setup
    other = _client()
    assert other.post("/api/auth/setup", json={"email": "evil@example.com", "password": PW}).status_code == 409
    assert store.count(User) == 1


def test_login_cookie_is_httponly_strict_and_only_a_hash_is_stored():
    auth.create_user("a@example.com", PW, "admin")
    c = TestClient(app, headers=WEB)
    r = c.post("/api/auth/login", json={"email": "A@example.com", "password": PW})
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    sid = r.cookies[auth.COOKIE]
    [s] = store.find(Session)
    assert s.sid_hash != sid and len(s.sid_hash) == 64
    u = store.find(User)[0]
    assert PW not in (u.password_hash + u.salt) and "password" not in r.text
    assert c.get("/api/auth/state").json()["user"]["email"] == "a@example.com"


def test_wrong_password_and_unknown_email_look_the_same_and_lock_after_five():
    auth.create_user("a@example.com", PW)
    c = TestClient(app, headers=WEB)
    bad = c.post("/api/auth/login", json={"email": "a@example.com", "password": "nope nope nope"})
    unknown = c.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope nope nope"})
    assert bad.status_code == unknown.status_code == 401 and bad.json() == unknown.json()
    auth.forget_login_attempts()
    for _ in range(auth.MAX_FAILS):
        assert c.post("/api/auth/login", json={"email": "a@example.com", "password": "x" * 12}).status_code == 401
    locked = c.post("/api/auth/login", json={"email": "a@example.com", "password": PW})  # even the right one
    assert locked.status_code == 429 and "locked" in locked.json()["detail"]


def test_login_needs_the_csrf_header():
    auth.create_user("a@example.com", PW)
    assert TestClient(app).post("/api/auth/login", json={"email": "a@example.com", "password": PW}).status_code == 403


def test_editor_cannot_approve_or_reject_or_manage_users_but_admin_can():
    aid = _pending_approval()
    editor, admin = _client("ed@example.com"), _client("boss@example.com", "admin")
    for path in (f"/api/approvals/{aid}/approve", f"/api/approvals/{aid}/reject", f"/api/approvals/{aid}/approve/"):
        r = editor.post(path)
        assert r.status_code == 403 and "admin" in r.json()["detail"], path
    assert store.get(Approval, aid).status == "pending"
    assert editor.get("/api/auth/users").status_code == 403
    assert editor.post("/api/auth/users", json={"email": "x@example.com", "password": PW, "role": "admin"}).status_code == 403
    assert editor.get("/api/approvals").status_code == 200  # may look
    assert admin.post(f"/api/approvals/{aid}/reject").status_code == 200
    a = store.get(Approval, aid)
    assert a.status == "rejected" and a.decided_by == "boss@example.com"


def test_requests_and_audit_rows_record_who():
    editor = _client("ed@example.com")
    c = store.insert(Campaign(name="Test", daily_budget=10000, status="active"))
    r = editor.post(f"/api/campaigns/{c.id}/requests", json={"action": "pause"})
    assert r.status_code == 200, r.text
    assert store.get(Approval, r.json()["approval_id"]).requested_by == "ed@example.com"
    editor.post("/api/faq", json={"question": "Do you deliver?", "answer": "Yes."})
    row = [a for a in store.find(AuditLog) if a.event == "faq_changed"][-1]
    assert row.detail["by"] == "ed@example.com"
    # a failed sign-in right after, on the same client, is not stamped with the editor's name
    TestClient(app, headers=WEB).post("/api/auth/login", json={"email": "ghost@example.com", "password": "x" * 12})
    assert "by" not in [a for a in store.find(AuditLog) if a.event == "login_failed"][-1].detail


def test_logout_disable_and_role_change_end_the_session_at_once():
    admin, editor = _client("boss@example.com", "admin"), _client("ed@example.com")
    ed = store.find(User, {"email": "ed@example.com"})[0]
    assert editor.get("/api/campaigns").status_code == 200
    assert admin.patch(f"/api/auth/users/{ed.id}", json={"disabled": True}).status_code == 200
    assert editor.get("/api/campaigns").status_code == 401
    assert editor.post("/api/auth/login", json={"email": "ed@example.com", "password": PW}).status_code == 401
    assert admin.post("/api/auth/logout").status_code == 200
    assert admin.get("/api/campaigns").status_code == 401


def test_the_last_admin_cannot_be_removed_or_demoted():
    admin = _client("boss@example.com", "admin")
    me = store.find(User)[0]
    assert admin.patch(f"/api/auth/users/{me.id}", json={"role": "editor"}).status_code == 409
    assert admin.patch(f"/api/auth/users/{me.id}", json={"disabled": True}).status_code == 409
    r = admin.post("/api/auth/users", json={"email": "two@example.com", "password": PW, "role": "admin"})
    assert r.status_code == 200 and "password" not in r.text and "hash" not in r.text
    assert admin.patch(f"/api/auth/users/{me.id}", json={"role": "editor"}).status_code == 200


def test_sign_in_off_keeps_the_old_single_user_behaviour(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    get_settings.cache_clear()
    c = TestClient(app, headers=WEB)
    assert c.get("/api/campaigns").status_code == 200
    assert c.get("/api/auth/state").json()["auth_required"] is False

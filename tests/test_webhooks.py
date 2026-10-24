"""Meta DM webhook: subscribe check, signature check, storage with dedupe, and the security exception."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import store
from app.db.models import AuditLog, InboundMessage
from app.main import app

SECRET, TOKEN = "test-app-secret", "verify-me"


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", SECRET)
    monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _body(mid="m_1", text="Which areas do you cover?", obj="page"):
    return {"object": obj, "entry": [{"id": "999", "time": 1792700000000, "messaging": [{
        "sender": {"id": "psid_42"}, "recipient": {"id": "999"}, "timestamp": 1792700000000,
        "message": {"mid": mid, "text": text}}]}]}


def _post(client, body, secret=SECRET, sig=None):
    raw = json.dumps(body).encode()
    sig = sig or "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/meta", content=raw,
                       headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def test_subscribe_check_echoes_challenge_only_with_right_token():
    c = TestClient(app)
    ok = c.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": TOKEN, "hub.challenge": "123"})
    assert ok.status_code == 200 and ok.text == "123"
    bad = c.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
    assert bad.status_code == 403


def test_signed_message_is_stored():
    r = _post(TestClient(app), _body())
    assert r.status_code == 200 and r.text == "EVENT_RECEIVED"
    [m] = store.find(InboundMessage)
    assert (m.platform, m.page_id, m.sender_id, m.mid, m.text, m.status) == (
        "messenger", "999", "psid_42", "m_1", "Which areas do you cover?", "new")
    assert store.find(AuditLog, {"event": "dm_received"})


def test_wrong_or_missing_signature_is_rejected_and_nothing_stored():
    c = TestClient(app)
    assert _post(c, _body(), secret="someone-else").status_code == 401
    assert _post(c, _body(), sig="sha1=abc").status_code == 401
    raw = json.dumps(_body()).encode()
    assert c.post("/webhooks/meta", content=raw).status_code == 401
    assert store.count(InboundMessage) == 0


def test_retried_delivery_is_stored_once():
    c = TestClient(app)
    assert _post(c, _body()).status_code == 200
    assert _post(c, _body()).status_code == 200  # Meta retries: still 200, no duplicate
    assert store.count(InboundMessage) == 1


def test_instagram_and_postbacks_are_parsed():
    c = TestClient(app)
    assert _post(c, _body(mid="ig_1", obj="instagram")).status_code == 200
    pb = {"object": "page", "entry": [{"id": "999", "messaging": [{
        "sender": {"id": "psid_7"}, "recipient": {"id": "999"}, "timestamp": 1792700000001,
        "postback": {"mid": "pb_1", "title": "Get Started", "payload": "GET_STARTED"}}]}]}
    assert _post(c, pb).status_code == 200
    rows = {m.mid: m for m in store.find(InboundMessage)}
    assert rows["ig_1"].platform == "instagram"
    assert (rows["pb_1"].kind, rows["pb_1"].text) == ("postback", "Get Started")


def test_unknown_object_and_unconfigured_secret(monkeypatch):
    c = TestClient(app)
    assert _post(c, _body(obj="whatsapp_business_account")).status_code == 404
    monkeypatch.setenv("META_APP_SECRET", "")
    get_settings.cache_clear()
    assert _post(c, _body()).status_code == 503


def test_only_the_webhook_skips_the_host_allow_list():
    outside = TestClient(app, base_url="https://milo-tunnel.example.com")
    assert _post(outside, _body(mid="m_out")).status_code == 200
    assert outside.get("/api/status").status_code == 400  # everything else stays localhost-only


def test_malformed_signatures_are_rejected():
    c = TestClient(app)
    for sig in ("sha256=", "sha256=zz", "sha256=" + "0" * 64, "SHA256=abc", "sha256 =abc"):
        assert _post(c, _body(), sig=sig).status_code == 401
    assert store.count(InboundMessage) == 0


def test_oversized_body_is_refused_before_reading():
    c = TestClient(app)
    r = c.post("/webhooks/meta", content=b"x" * 10, headers={"Content-Length": str(2_000_000)})
    assert r.status_code == 413
    big = c.post("/webhooks/meta", content=b"x" * 1_000_001)
    assert big.status_code == 413


def test_retry_of_a_never_handled_message_is_requeued(monkeypatch):
    from app.api import webhooks
    monkeypatch.setenv("DM_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    queued = []

    async def fake_run(ids):
        queued.append(list(ids))
    monkeypatch.setattr(webhooks, "_run_agent", fake_run)
    c = TestClient(app)
    _post(c, _body())  # stored; pretend the process died before the agent ran
    _post(c, _body())  # Meta retries
    [m] = store.find(InboundMessage)
    assert queued == [[m.id], [m.id]]
    store.update(InboundMessage, m.id, {"agent_action": "answer"})
    _post(c, _body())  # already handled: not requeued
    assert len(queued) == 2


def test_background_failure_is_recorded(monkeypatch):
    import asyncio

    from app.api import webhooks
    from app.dm import agent

    async def boom(mid, **kw):
        raise RuntimeError("mongo down")
    monkeypatch.setattr(agent, "handle", boom)
    m = store.insert(InboundMessage(platform="messenger", page_id="999", sender_id="p", customer_id="p", mid="x"))
    asyncio.run(webhooks._run_agent([m.id]))
    row = store.get(InboundMessage, m.id)
    assert row.agent_action == "failed" and row.handed_off
    assert store.find(AuditLog, {"event": "dm_agent_failed"})

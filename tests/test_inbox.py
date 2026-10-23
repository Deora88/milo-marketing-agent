"""DM inbox: conversations, Meta's reply window and human replies through the Send API."""
import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import store
from app.db.models import AuditLog, InboundMessage, now
from app.dm import inbox
from app.main import app
from tests.conftest import WEB


def _in(mid, hours_ago=1.0, customer="psid_1", text="Hi", referral=None, platform="messenger"):
    return store.insert(InboundMessage(platform=platform, page_id="999", sender_id=customer, recipient_id="999",
                                       customer_id=customer, mid=mid, text=text, referral=referral or {},
                                       sent_at=now() - timedelta(hours=hours_ago)))


class FakeClient:
    def __init__(self, s):
        self.s, self.calls = s, []

    async def get(self, path, token=None, **params):
        self.calls.append(("GET", path, token, params))
        return {"access_token": "PAGE_TOKEN"}

    async def post(self, path, token=None, **data):
        self.calls.append(("POST", path, token, data))
        return {"recipient_id": data["recipient"]["id"], "message_id": f"m_out_{len(self.calls)}"}

    async def aclose(self):
        pass


def _live(**kw):
    return Settings(_env_file=None, meta_dry_run=False, meta_access_token="SYS", **kw)


def test_reply_window_rules():
    assert inbox.reply_window([_in("a", hours_ago=2)]).state == "open"
    assert inbox.reply_window([_in("b", hours_ago=30, customer="p2")]).state == "human_agent"
    assert inbox.reply_window([_in("c", hours_ago=24 * 8, customer="p3")]).state == "closed"
    from_ad = inbox.reply_window([_in("d", hours_ago=24 * 3, customer="p4", referral={"source": "ADS", "ad_id": "1"})])
    assert from_ad.state == "open" and from_ad.from_ad  # first message after a click-to-Messenger ad: 7 days


def test_conversations_are_grouped_with_unread_counts():
    _in("m1", hours_ago=3, customer="alice")
    _in("m2", hours_ago=2, customer="alice", text="Still there?")
    _in("m3", hours_ago=5, customer="bob")
    rows = inbox.list_conversations()
    assert [r["customer_id"] for r in rows] == ["alice", "bob"]  # newest activity first
    assert rows[0]["unread"] == 2 and rows[0]["last_text"] == "Still there?"
    inbox.open_thread("messenger", "999", "alice")
    assert inbox.list_conversations()[0]["unread"] == 0


def test_reply_in_dry_run_is_stored_and_marks_thread_answered():
    _in("m1")
    sent = asyncio.run(inbox.send_reply("messenger", "999", "psid_1", "We cover all of Delhi NCR."))
    assert sent.is_echo and sent.status == "dry_run" and sent.sent_by == "human"
    assert store.find(InboundMessage, {"mid": "m1"})[0].status == "answered"
    assert store.find(AuditLog, {"event": "dm_replied"})
    assert inbox.list_conversations()[0]["last_from"] == "page"


def test_live_reply_uses_page_token_and_send_api():
    _in("m1")
    s = _live()
    client = FakeClient(s)
    asyncio.run(inbox.send_reply("messenger", "999", "psid_1", "Hello!", client=client, settings=s))
    (_, get_path, _, get_params), (_, post_path, post_token, body) = client.calls
    assert get_path == "/999" and get_params == {"fields": "access_token"}
    assert post_path == "/999/messages" and post_token == "PAGE_TOKEN"
    assert body == {"recipient": {"id": "psid_1"}, "message": {"text": "Hello!"}, "messaging_type": "RESPONSE"}


def test_late_reply_needs_human_agent_feature_and_tag():
    _in("m1", hours_ago=30)
    with pytest.raises(inbox.ReplyError, match="HUMAN_AGENT"):
        asyncio.run(inbox.send_reply("messenger", "999", "psid_1", "Sorry for the delay"))
    s = _live(meta_human_agent_enabled=True, meta_page_access_token="PT")
    client = FakeClient(s)
    asyncio.run(inbox.send_reply("messenger", "999", "psid_1", "Sorry for the delay", client=client, settings=s))
    body = client.calls[-1][3]
    assert body["messaging_type"] == "MESSAGE_TAG" and body["tag"] == "HUMAN_AGENT"
    with pytest.raises(inbox.ReplyError):  # the bot may never use the human tag
        asyncio.run(inbox.send_reply("messenger", "999", "psid_1", "auto", sent_by="agent", client=client, settings=s))


def test_closed_window_and_bad_text_are_refused():
    _in("m1", hours_ago=24 * 8)
    with pytest.raises(inbox.ReplyError, match="closed"):
        asyncio.run(inbox.send_reply("messenger", "999", "psid_1", "Hi"))
    _in("m2", customer="p9")
    for text in ("", "x" * 2001):
        with pytest.raises(inbox.ReplyError):
            asyncio.run(inbox.send_reply("messenger", "999", "p9", text))


def test_inbox_api_list_thread_reply_and_csrf():
    _in("m1", customer="alice", text="Do you clean sofas?")
    c = TestClient(app, headers=WEB)
    assert c.get("/api/inbox").json()[0]["customer_id"] == "alice"
    t = c.get("/api/inbox/messenger/999/alice").json()
    assert t["window"]["state"] == "open" and t["messages"][0]["text"] == "Do you clean sofas?"
    assert c.get("/api/inbox/messenger/999/nobody").status_code == 404
    r = c.post("/api/inbox/messenger/999/alice/reply", json={"text": "Yes, we do."})
    assert r.status_code == 200 and r.json()["sent"]["text"] == "Yes, we do."
    assert c.post("/api/inbox/messenger/999/alice/reply", json={"text": ""}).status_code == 409
    assert TestClient(app).post("/api/inbox/messenger/999/alice/reply", json={"text": "x"}).status_code == 403

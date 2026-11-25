"""Instagram DMs through the linked Facebook Page (Messenger API for Instagram): receive, reply, check setup."""
import asyncio
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.webhooks import parse_events
from app.config import Settings
from app.db import store
from app.db.models import FaqEntry, InboundMessage, now
from app.dm import agent, inbox, instagram_setup
from app.main import app
from app.meta.client import MetaAPIError
from app.util.redact import redact
from tests.conftest import WEB

PAGE, IG, APP = "1372787802593371", "17841400000000001", "3205700776292844"


def _s(**kw):
    base = dict(_env_file=None, dm_agent_enabled=True, meta_dry_run=False, meta_page_access_token="PT",
                meta_page_id=PAGE, meta_app_id=APP, meta_app_secret="0123456789abcdef0123456789abcdef")
    return Settings(**{**base, **kw})


class FakeMeta:
    def __init__(self, s, gets=None, mid="mid.IG1"):
        self.s, self.gets, self.mid, self.calls = s, gets or {}, mid, []

    async def get(self, path, token=None, **params):
        self.calls.append(("GET", path, token, params))
        value = self.gets.get(path, {})
        if isinstance(value, Exception):
            raise value
        return value

    async def post(self, path, token=None, **data):
        self.calls.append(("POST", path, token, data))
        return {"message_id": self.mid}

    async def aclose(self):
        pass


def _ig_dm(text="Hi from Instagram", mid="ig_m1", hours_ago=1.0):
    return store.insert(InboundMessage(platform="instagram", page_id=IG, sender_id="igsid_7", recipient_id=IG,
                                       customer_id="igsid_7", mid=mid, text=text,
                                       sent_at=now() - timedelta(hours=hours_ago)))


def test_webhook_instagram_events_are_stored_with_the_instagram_account():
    body = {"object": "instagram", "entry": [{"id": IG, "time": 1, "messaging": [
        {"sender": {"id": "igsid_7"}, "recipient": {"id": IG}, "timestamp": 1,
         "message": {"mid": "ig_a", "text": "Do you clean SUVs?"}},
        {"sender": {"id": IG}, "recipient": {"id": "igsid_7"}, "timestamp": 2,
         "message": {"mid": "ig_b", "text": "Yes!", "is_echo": True}}]}]}
    dm, echo = parse_events(body)
    assert (dm.platform, dm.page_id, dm.customer_id, dm.is_echo) == ("instagram", IG, "igsid_7", False)
    assert (echo.customer_id, echo.is_echo, echo.app_id) == ("igsid_7", True, "")


def test_instagram_reply_goes_through_the_linked_page():
    _ig_dm()
    s = _s(meta_instagram_account_id=IG)
    client = FakeMeta(s)
    sent = asyncio.run(inbox.send_reply("instagram", IG, "igsid_7", "Yes, we clean SUVs.", client=client,
                                        settings=s))
    method, path, token, data = client.calls[-1]
    assert (method, path, token) == ("POST", f"/{PAGE}/messages", "PT")  # the Page, not the Instagram id
    assert data == {"recipient": {"id": "igsid_7"}, "message": {"text": "Yes, we clean SUVs."}}  # no messaging_type
    assert sent.platform == "instagram" and sent.page_id == IG and sent.status == "sent"


def test_linked_instagram_account_is_asked_from_meta_once():
    inbox._ig_linked.clear()
    _ig_dm()
    s = _s()  # META_INSTAGRAM_ACCOUNT_ID not set
    client = FakeMeta(s, gets={f"/{PAGE}": {"instagram_business_account": {"id": IG}}})
    asyncio.run(inbox.send_reply("instagram", IG, "igsid_7", "Hi", client=client, settings=s))
    asyncio.run(inbox.send_reply("instagram", IG, "igsid_7", "Hi again", client=FakeMeta(s, mid="m2"), settings=s))
    assert [c for c in client.calls if c[0] == "GET"] == [("GET", f"/{PAGE}", None,
                                                           {"fields": "instagram_business_account"})]


def test_dm_to_an_instagram_account_not_linked_to_the_page_is_refused():
    inbox._ig_linked.clear()
    store.insert(InboundMessage(platform="instagram", page_id="other_ig", sender_id="u", customer_id="u", mid="x",
                                text="hi", sent_at=now()))
    s = _s()
    client = FakeMeta(s, gets={f"/{PAGE}": {"instagram_business_account": {"id": IG}}})
    with pytest.raises(inbox.ReplyError, match="linked Instagram account"):
        asyncio.run(inbox.send_reply("instagram", "other_ig", "u", "Hi", client=client, settings=s))
    assert not [c for c in client.calls if c[0] == "POST"]


def test_instagram_text_limit_counts_bytes():
    _ig_dm()
    s = _s(meta_instagram_account_id=IG)
    for text in ("x" * 1000, "क" * 400):  # 1000 bytes / 1200 bytes
        with pytest.raises(inbox.ReplyError, match="under 1,000"):
            asyncio.run(inbox.send_reply("instagram", IG, "igsid_7", text, client=FakeMeta(s), settings=s))
    asyncio.run(inbox.send_reply("instagram", IG, "igsid_7", "x" * 999, client=FakeMeta(s), settings=s))


def test_instagram_human_agent_reply_uses_the_tag():
    _ig_dm(hours_ago=30)  # outside the 24-hour window
    s = _s(meta_instagram_account_id=IG, meta_human_agent_enabled=True)
    client = FakeMeta(s)
    asyncio.run(inbox.send_reply("instagram", IG, "igsid_7", "Sorry for the wait", client=client, settings=s))
    assert client.calls[-1][3]["messaging_type"] == "MESSAGE_TAG" and client.calls[-1][3]["tag"] == "HUMAN_AGENT"


def test_bot_auto_replies_on_instagram_within_the_limit():
    f = store.insert(FaqEntry(question="Which areas?", answer="We cover Delhi NCR."))
    long = store.insert(FaqEntry(question="Everything?", answer="Long answer. " * 70))  # ~910 bytes
    s = _s(meta_instagram_account_id=IG, dm_autoreply=True)

    def llm(ids):
        async def fake(messages):
            return json.dumps({"action": "answer", "faq_ids": ids, "reply": "x"})
        return fake
    m = _ig_dm("Which areas do you cover?")
    client = FakeMeta(s)
    done = asyncio.run(agent.handle(m.id, settings=s, llm=llm([f.id]), client=client))
    assert done.agent_action == "answer"
    assert client.calls[-1][1] == f"/{PAGE}/messages" and "We cover Delhi NCR." in client.calls[-1][3]["message"]["text"]
    m2 = _ig_dm("Tell me everything", mid="ig_m2", hours_ago=0)
    done2 = asyncio.run(agent.handle(m2.id, settings=s, llm=llm([long.id]), client=FakeMeta(s, mid="m3")))
    assert done2.agent_action == "handoff" and "too long" in done2.agent_note  # never cut, never over the limit


# --- setup check ---

def _good_gets():
    return {f"/{PAGE}": {"instagram_business_account": {"id": IG, "username": "doorstepcarcare"}},
            f"/{PAGE}/subscribed_apps": {"data": [{"id": APP, "subscribed_fields": [
                "messages", "messaging_postbacks", "message_echoes"]}]},
            f"/{APP}/subscriptions": {"data": [{"object": "instagram", "active": True,
                                                "callback_url": "https://x.ngrok.app/webhooks/meta",
                                                "fields": [{"name": "messages"}, {"name": "messaging_postbacks"}]}]}}


def test_setup_check_all_good():
    _ig_dm()
    s = _s()
    client = FakeMeta(s, gets=_good_gets())
    r = asyncio.run(instagram_setup.check(client=client, settings=s))
    assert r["ok"] is True and [c["ok"] for c in r["checks"]] == [True, True, True, True]
    assert "@doorstepcarcare" in r["checks"][0]["detail"]
    app_call = next(c for c in client.calls if c[1] == f"/{APP}/subscriptions")
    assert app_call[2] == f"{APP}|{s.meta_app_secret}"  # app access token, only sent to Meta
    assert all(c[0] == "GET" for c in client.calls)  # read-only


def test_setup_check_explains_what_is_missing():
    gets = _good_gets()
    gets[f"/{PAGE}/subscribed_apps"] = {"data": [{"id": APP, "subscribed_fields": ["messages"]}]}
    gets[f"/{APP}/subscriptions"] = {"data": [{"object": "page", "fields": [{"name": "messages"}]}]}
    gets[f"/{PAGE}"] = MetaAPIError(190, None, "Invalid OAuth access token EAAabcdefghijklmnopqrstuvwxyz123")
    r = asyncio.run(instagram_setup.check(client=FakeMeta(_s(), gets=gets), settings=_s()))
    by = {c["key"]: c for c in r["checks"]}
    assert r["ok"] is False
    assert by["linked"]["ok"] is False and "EAA" not in by["linked"]["detail"]  # token redacted
    assert "messaging_postbacks, message_echoes" in by["page_subscription"]["detail"]
    assert "no instagram object" in by["app_webhook"]["detail"]
    assert by["received"]["ok"] is False


def test_setup_check_in_dry_run_asks_meta_nothing():
    r = TestClient(app, headers=WEB).get("/api/instagram/check").json()
    assert r["dry_run"] is True and r["manual"]


def test_app_access_token_is_redacted():
    assert "0123456789abcdef" not in redact(f"bad token {APP}|0123456789abcdef0123456789abcdef")

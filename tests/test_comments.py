"""Comments on Instagram / Facebook posts: webhook parsing, dedupe, edits, person-only replies, hide, bot drafts."""
import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import store
from app.db.models import Comment, FaqEntry, InboundMessage, SocialPost
from app.main import app
from app.social import comments
from tests.conftest import WEB

SECRET, PAGE, IG = "test-app-secret", "1372787802593371", "17841454169454463"


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _post(body):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return TestClient(app).post("/webhooks/meta", content=raw,
                                headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def ig_comment(cid="1801", text="Do you come to Indirapuram?", who="17840001", username="riya.k", parent=None):
    v = {"id": cid, "text": text, "from": {"id": who, "username": username}, "media": {"id": "1789_media"}}
    if parent:
        v["parent_id"] = parent
    return {"object": "instagram", "entry": [{"id": IG, "time": 1, "changes": [{"field": "comments", "value": v}]}]}


def fb_comment(cid="111_222", verb="add", text="Price for SUV?", who="5501"):
    return {"object": "page", "entry": [{"id": PAGE, "time": 1, "changes": [{"field": "feed", "value": {
        "item": "comment", "verb": verb, "comment_id": cid, "post_id": f"{PAGE}_111", "parent_id": f"{PAGE}_111",
        "message": text, "from": {"id": who, "name": "Amit Shah"}, "created_time": 1792700000}}]}]}


def test_webhook_stores_comments_once_and_skips_own():
    assert _post(ig_comment()).status_code == 200
    assert _post(ig_comment()).status_code == 200  # Meta retry
    _post(ig_comment(cid="1802", who=IG, username="doorstep", text="Yes!", parent="1801"))  # our own reply
    _post(fb_comment())
    rows = store.find(Comment, sort=[("_id", 1)])
    assert [(c.platform, c.own, c.status) for c in rows] == [("instagram", False, "new"), ("instagram", True, "read"),
                                                            ("facebook", False, "new")]
    assert rows[2].parent_id == "" and rows[2].from_name == "Amit Shah" and rows[2].media_id == f"{PAGE}_111"
    assert store.count(InboundMessage) == 0  # comments are not DMs
    out = TestClient(app, headers=WEB).get("/api/comments?status=open").json()
    assert out["unread"] == 2 and len(out["comments"]) == 2  # own reply not listed


def test_facebook_edit_remove_and_hide_events():
    _post(fb_comment())
    _post(fb_comment(verb="edited", text="Price for a sedan?"))
    assert store.find(Comment)[0].text == "Price for a sedan?"
    _post(fb_comment(verb="hide"))
    assert store.find(Comment)[0].hidden is True
    _post(fb_comment(verb="remove"))
    c = store.find(Comment)[0]
    assert c.status == "removed"
    r = TestClient(app, headers=WEB).post(f"/api/comments/{c.id}/reply", json={"text": "Hi"})
    assert r.status_code == 409


def test_comment_linked_to_milo_post_and_mark_read():
    store.insert(SocialPost(kind="photo", caption="Diwali foam wash", status="published", meta_media_id="1789_media"))
    _post(ig_comment())
    c = TestClient(app, headers=WEB)
    row = c.get("/api/comments").json()["comments"][0]
    assert row["post"]["caption"] == "Diwali foam wash"
    assert c.post("/api/comments/read", json={}).json()["marked"] == 1
    assert c.get("/api/comments").json()["unread"] == 0


def test_dry_run_reply_and_hide_are_saved_only():
    _post(ig_comment())
    cid = store.find(Comment)[0].id
    c = TestClient(app, headers=WEB)
    r = c.post(f"/api/comments/{cid}/reply", json={"text": "Yes, we cover Indirapuram!"}).json()
    assert r["status"] == "replied" and r["replies"][0]["mode"] == "dry_run"
    assert c.post(f"/api/comments/{cid}/hide", json={"hidden": True}).json()["status"] == "hidden"
    assert c.post(f"/api/comments/{cid}/hide", json={"hidden": False}).json()["status"] == "replied"
    assert c.post(f"/api/comments/{cid}/reply", json={"text": ""}).status_code == 422
    assert c.post("/api/comments/999/reply", json={"text": "x"}).status_code == 404
    assert TestClient(app).post(f"/api/comments/{cid}/reply", json={"text": "x"}).status_code == 403  # CSRF


class FakeMeta:
    def __init__(self, s):
        self.s, self.calls = s, []

    async def get(self, path, token=None, **params):
        return {}

    async def post(self, path, token=None, **data):
        self.calls.append((path, data, token))
        return {"id": "r1"}

    async def aclose(self):
        pass


def test_live_reply_and_hide_use_the_right_endpoints():
    s = Settings(_env_file=None, meta_dry_run=False, meta_page_access_token="PT", meta_page_id=PAGE)
    comments.save_events(ig_comment(cid="1803", parent="1801"))
    comments.save_events(fb_comment())
    ig, fb = store.find(Comment, sort=[("_id", 1)])
    m = FakeMeta(s)
    asyncio.run(comments.reply(ig.id, "Yes!", client=m))
    asyncio.run(comments.reply(fb.id, "₹799 for SUVs", client=m))
    asyncio.run(comments.set_hidden(ig.id, True, client=m))
    asyncio.run(comments.set_hidden(fb.id, True, client=m))
    assert [(p, d) for p, d, _ in m.calls] == [
        ("/1801/replies", {"message": "Yes!"}),  # Instagram: answer under the top comment
        ("/111_222/comments", {"message": "₹799 for SUVs"}),
        ("/1803", {"hide": "true"}), ("/111_222", {"is_hidden": "true"})]
    assert all(t == "PT" for *_, t in m.calls)
    assert store.get(Comment, ig.id).replies[0]["meta_id"] == "r1"


def test_bot_drafts_from_faq_but_never_posts():
    store.insert(FaqEntry(question="Which areas do you cover?", answer="All of Ghaziabad and Noida."))
    comments.save_events(ig_comment())
    c = store.find(Comment)[0]

    async def llm(messages):
        return json.dumps({"action": "answer", "faq_ids": [1], "reply": "We cover all of Ghaziabad and Noida!"})

    out = asyncio.run(comments.suggest(c.id, llm=llm))
    assert out.suggested_reply.startswith("We cover") and out.replies == [] and out.status == "new"
    comments.save_events(ig_comment(cid="1804", text="call me 9871701615"))
    lead = store.find(Comment, {"comment_id": "1804"})[0]
    got = asyncio.run(comments.suggest(lead.id, llm=llm))
    assert "DM" in got.suggested_reply and "hiding" in got.agent_note


def test_own_comments_show_only_under_all_and_log_line_says_what_came():
    _post(ig_comment(cid="1810", who=IG, username="doorstep", text="Test from the business account"))
    c = TestClient(app, headers=WEB)
    assert c.get("/api/comments?status=open").json()["comments"] == []
    everything = c.get("/api/comments").json()
    assert [x["own"] for x in everything["comments"]] == [True] and everything["unread"] == 0
    assert comments.describe(ig_comment()) == "instagram: changes comments x1"
    assert comments.describe({"object": "page", "entry": [{"messaging": [{"read": {}}]}]}) == "page: read x1"
    got = comments.save_events(ig_comment(cid="1811"))
    assert got == {"new": 1, "own": 0, "duplicate": 0, "updated": 0}
    assert comments.save_events(ig_comment(cid="1811"))["duplicate"] == 1

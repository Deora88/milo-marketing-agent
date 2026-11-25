"""Fixes from the third PR #3 review: stuck posts, duplicate publish, comment double replies, tunnel guard, CORS,
huge images. (The DM send-timeout case is in test_dm_timeouts.py.)"""
import asyncio
import struct
import zlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import store
from app.db.models import Comment, SocialPost, now
from app.main import app
from app.social import comments, instagram, media, posts
from tests.conftest import WEB
from tests.test_comments import FakeMeta as CommentMeta
from tests.test_comments import ig_comment
from tests.test_instagram_posts import FakeMeta, _live, _ready, _request


def _publish(p, client):
    return asyncio.run(instagram.publish(p.id, client, sleep=lambda s: asyncio.sleep(0)))


def _media_publish_calls(client):
    return [c for c in client.calls if c[1].endswith("/media_publish")]


# --- 1. post stuck in "publishing" ---

def test_post_stuck_publishing_is_recovered_and_can_be_retried():
    p = _ready()
    _request(p)
    store.update(SocialPost, p.id, {"status": "publishing", "publishing_started_at": now() - timedelta(hours=1)})
    assert posts.get(p.id).status == "publishing"
    assert instagram.fail_stuck_posts() == [p.id]
    got = posts.get(p.id)
    assert got.status == "failed" and "may already be live" in got.error
    posts.update(p.id, caption="try again")  # editable again
    assert posts.request_publish(p.id)["status"] == "pending_approval"  # and can be approved again


def test_recent_publish_is_left_alone_but_startup_recovers_it():
    p = _ready()
    store.update(SocialPost, p.id, {"status": "publishing", "publishing_started_at": now()})
    assert instagram.fail_stuck_posts() == []  # still running
    assert instagram.fail_stuck_posts(older_than=timedelta(0)) == [p.id]  # at startup: anything left is cut off


def test_stuck_post_with_a_media_id_is_marked_published():
    p = _ready()
    store.update(SocialPost, p.id, {"status": "publishing", "meta_mode": "live", "meta_media_id": "1789_media",
                                    "publishing_started_at": now() - timedelta(hours=1)})
    instagram.fail_stuck_posts()
    assert posts.get(p.id).status == "published"


# --- 2. no second publish after a database error ---

def test_database_error_after_media_publish_never_publishes_twice(monkeypatch):
    p = _ready()
    _request(p)
    real = store.update
    state = {"failed": False}

    def flaky(cls, id_, changes):
        if cls is SocialPost and "permalink" in changes and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("database blip")
        return real(cls, id_, changes)

    monkeypatch.setattr(store, "update", flaky)
    client = FakeMeta(_live())
    done = _publish(posts.get(p.id), client)  # no exception: it is live, so the user isn't invited to retry
    assert state["failed"] and done.status == "published" and done.meta_media_id == "1789_media"
    assert "error afterwards" in done.error
    assert len(_media_publish_calls(client)) == 1

    # even if it were put back for approval, a post with a live media id is never sent to Instagram again
    real(SocialPost, p.id, {"status": "pending_approval"})
    again = FakeMeta(_live())
    _publish(posts.get(p.id), again)
    assert again.calls == [] and posts.get(p.id).status == "published"


# --- 4. comment replies: one at a time, nothing lost ---

class SlowCommentMeta(CommentMeta):
    async def post(self, path, token=None, **data):
        await asyncio.sleep(0.05)
        return await super().post(path, token=token, **data)


def test_two_quick_replies_post_once_and_keep_the_stored_reply():
    from app.config import Settings

    s = Settings(_env_file=None, meta_dry_run=False, meta_page_access_token="PT", meta_page_id="1372787802593371")
    comments.save_events(ig_comment())
    c = store.find(Comment)[0]
    m = SlowCommentMeta(s)

    async def both():
        return await asyncio.gather(comments.reply(c.id, "Yes!", client=m), comments.reply(c.id, "Yes!!", client=m),
                                    return_exceptions=True)

    results = asyncio.run(both())
    assert sum(isinstance(r, comments.CommentError) for r in results) == 1
    assert len(m.calls) == 1  # one public reply on Instagram
    got = store.get(Comment, c.id)
    assert len(got.replies) == 1 and got.replying_at is None
    asyncio.run(comments.reply(c.id, "Also: we open at 7 am", client=m))  # lock released: a later reply works
    assert len(store.get(Comment, c.id).replies) == 2  # appended, not overwritten


def test_stale_reply_lock_expires():
    comments.save_events(ig_comment(cid="1900"))
    c = store.find(Comment)[0]
    store.update(Comment, c.id, {"replying_at": now() - timedelta(minutes=5)})  # a crashed earlier attempt
    assert asyncio.run(comments.reply(c.id, "Hi")).status == "replied"  # dry run


# --- 5. tunnel guard ---

def test_dashboard_and_api_are_blocked_through_a_tunnel_without_a_token(monkeypatch):
    c = TestClient(app, headers=WEB)
    assert c.get("/api/status").status_code == 200  # local browser: fine
    tunnel = {"X-Forwarded-For": "203.0.113.7", "Host": "localhost"}  # even with the Host header rewritten
    r = c.get("/api/status", headers=tunnel)
    assert r.status_code == 403 and "localhost:8000" in r.json()["detail"]
    assert c.get("/", headers=tunnel).status_code == 403
    assert c.post("/api/approvals/1/approve", headers=tunnel).status_code == 403
    assert c.get("/media/" + "x" * 32 + "/pub-00.jpg", headers=tunnel).status_code == 404  # still reachable
    assert c.get("/webhooks/meta", headers=tunnel).status_code == 403  # reaches the route (bad verify token)
    assert "Verification" in c.get("/webhooks/meta", headers=tunnel).text

    monkeypatch.setenv("API_TOKEN", "s3cret-token")
    get_settings.cache_clear()
    assert c.get("/api/status", headers=tunnel).status_code == 401  # token set: remote use needs the token
    assert c.get("/api/status", headers={**tunnel, "Authorization": "Bearer s3cret-token"}).status_code == 200


# --- 6. CORS only allows the headers and methods the UI uses ---

def test_cors_preflight_refuses_other_headers():
    c = TestClient(app)
    ok = c.options("/api/status", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                                           "Access-Control-Request-Headers": "x-milo-client,content-type"})
    assert ok.status_code == 200
    bad = c.options("/api/status", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                                            "Access-Control-Request-Headers": "x-evil"})
    assert bad.status_code == 400


# --- 8. huge images are refused before decoding ---

def _png_header(w, h):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IEND", b"")


def test_decompression_bomb_is_refused():
    with pytest.raises(media.MediaError, match="too large"):
        media._open(_png_header(20000, 20000))  # 400 megapixels, a few bytes on disk
    from app.db.models import Campaign

    cid = store.insert(Campaign(name="x", daily_budget=20000, countries=["IN"], age_min=18, age_max=40,
                                headline="h", primary_text="p", link_url="https://e.com")).id
    r = TestClient(app, headers=WEB).post(f"/api/campaigns/{cid}/image",
                                          files={"file": ("a.png", _png_header(20000, 20000), "image/png")})
    assert r.status_code == 415


# --- review of 16 Nov: public-prefix traversal, clickjacking headers, redaction of configured secrets ---

def test_dot_segments_do_not_get_the_public_prefix_exemption(monkeypatch):
    """/webhooks/../index.html went past the tunnel guard (prefix match) and returned the dashboard's HTML."""
    monkeypatch.setenv("API_TOKEN", "")
    get_settings.cache_clear()
    c = TestClient(app)
    tunnel = {"X-Forwarded-For": "203.0.113.7", "Host": "localhost"}
    for path in ("/webhooks/../index.html", "/media/../index.html", "/webhooks/./../index.html",
                 "/media/x/../../index.html"):
        req = c.build_request("GET", "http://localhost" + path, headers=tunnel)
        req.url = req.url.copy_with(raw_path=path.encode())  # keep the dots: httpx would normalise them away
        assert c.send(req).status_code == 403, path
    assert c.get("/media/" + "x" * 32 + "/pub-00.jpg", headers=tunnel).status_code == 404  # real prefix still open


def test_every_response_forbids_framing():
    c = TestClient(app)
    for r in (c.get("/"), c.get("/api/health"), c.get("/api/nope"), c.post("/api/optimizer/run")):
        assert r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"


def test_redact_masks_configured_secrets_and_bearer_values(monkeypatch):
    from app.util.redact import redact

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cfTokenValue_1234567890")
    get_settings.cache_clear()
    out = redact("401 for token cfTokenValue_1234567890; sent Authorization: Bearer abcDEF123456789xyz")
    assert "cfTokenValue" not in out and "abcDEF123456789xyz" not in out
    get_settings.cache_clear()

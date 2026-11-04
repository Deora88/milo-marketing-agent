"""Organic Instagram posts (photo, carousel, Reel, Story): drafts, approval gate, publishing, public media, stories."""
import asyncio
import io
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api.webhooks import parse_events
from app.config import Settings, get_settings
from app.db import store
from app.db.models import Approval, InboundMessage, SocialPost, now
from app.dm import agent
from app.main import app
from app.social import instagram, posts
from tests.conftest import WEB

PAGE, IG = "1372787802593371", "17841454169454463"


def png(w=1200, h=900, color=(30, 90, 160)):
    b = io.BytesIO()
    Image.new("RGBA", (w, h), color + (255,)).save(b, "PNG")
    return b.getvalue()


def _live(**kw):
    base = dict(_env_file=None, meta_dry_run=False, meta_page_access_token="PT", meta_page_id=PAGE,
                meta_instagram_account_id=IG, public_base_url="https://milo.example.dev")
    return Settings(**{**base, **kw})


class FakeMeta:
    def __init__(self, s, statuses=None):
        self.s, self.calls, self.n = s, [], 0
        self.statuses = list(statuses or [])  # status_code answers for container checks, in order

    async def get(self, path, token=None, **params):
        self.calls.append(("GET", path, params))
        if params.get("fields") == "status_code,status":
            return {"status_code": self.statuses.pop(0) if self.statuses else "FINISHED"}
        if params.get("fields") == "permalink":
            return {"permalink": "https://www.instagram.com/p/ABC123/"}
        return {}

    async def post(self, path, token=None, **data):
        self.calls.append(("POST", path, data))
        assert token == "PT"  # the Page token, never the user token
        self.n += 1
        return {"id": f"c{self.n}" if path.endswith("/media") else "1789_media"}

    async def aclose(self):
        pass


def _ready(kind="photo", n=1, caption="Fresh foam wash at your door 🚗 #cardetailing", when=None):
    p = posts.create(kind, caption, when)
    for i in range(n):
        posts.add_image(p.id, png(color=(30 + i * 40, 90, 160)))
    return posts.get(p.id)


def _request(p):
    r = posts.request_publish(p.id)
    assert r["status"] == "pending_approval"
    return r["approval_id"]


# --- drafts and checks ---

def test_caption_and_schedule_rules():
    with pytest.raises(posts.PostError, match="2200"):
        posts.create("photo", "x" * 2201)
    with pytest.raises(posts.PostError, match="30 hashtags"):
        posts.create("photo", " ".join(f"#t{i}" for i in range(31)))
    with pytest.raises(posts.PostError, match="timezone"):
        posts.create("photo", "hi", "2030-01-01T10:00")
    with pytest.raises(posts.PostError, match="5 minutes"):
        posts.create("photo", "hi", (now() + timedelta(minutes=1)).isoformat())
    with pytest.raises(posts.PostError, match="60 days"):
        posts.create("photo", "hi", (now() + timedelta(days=90)).isoformat())
    with pytest.raises(posts.PostError, match="Post type"):
        posts.create("tiktok", "hi")
    assert posts.create("story", "ignored caption").caption == ""  # stories have no caption
    when = (now() + timedelta(hours=3)).isoformat()
    assert posts.create("reel", "ok", when).scheduled_at is not None


def test_images_are_stored_as_jpeg_with_per_type_limits():
    p = posts.create("photo", "hi")
    p = posts.add_image(p.id, png(1000, 2000))  # too tall for the feed -> cropped to 4:5
    img = Image.open(p.images[0])
    assert img.format == "JPEG" and img.size == (1080, 1350)
    with pytest.raises(posts.PostError, match="at most 1"):
        posts.add_image(p.id, png())
    with pytest.raises(posts.media.MediaError, match="not an image"):
        posts.add_image(posts.create("carousel", "x").id, b"not an image at all")
    with pytest.raises(posts.PostError, match="remove some"):
        posts.update(_ready("carousel", 3).id, kind="photo")


def test_request_needs_images_and_a_public_address(monkeypatch):
    p = posts.create("carousel", "two please")
    posts.add_image(p.id, png())
    with pytest.raises(posts.PostError, match="at least 2 images"):
        posts.request_publish(p.id)
    monkeypatch.setenv("META_DRY_RUN", "false")
    get_settings.cache_clear()
    with pytest.raises(posts.PostError, match="PUBLIC_BASE_URL"):
        posts.request_publish(_ready().id)


def test_request_prepares_exact_media_and_one_approval():
    p = _ready("reel", 3)
    aid = _request(p)
    p = posts.get(p.id)
    files = [f.name for f in posts.publish_files(p)]
    assert files == ["pub-00.jpg", "pub-01.jpg", "pub-02.jpg", "reel.mp4"]
    assert Image.open(posts.publish_files(p)[0]).size == (1080, 1920)  # 9:16 frames
    assert p.status == "pending_approval" and len(p.media_token) >= 32
    a = store.get(Approval, aid)
    assert a.action == "publish_post" and a.post_id == p.id and "Reel (3 image(s), 9 s video)" in a.summary
    assert posts.request_publish(p.id)["approval_id"] == aid  # no duplicate approval
    with pytest.raises(posts.PostError, match="can't be changed"):
        posts.update(p.id, caption="changed while waiting")


# --- approval gate (dry run: nothing reaches Instagram) ---

def test_approve_publishes_and_reject_returns_to_draft():
    c = TestClient(app, headers=WEB)
    p = _ready()
    aid = _request(p)
    rows = c.get("/api/approvals").json()
    assert rows[0]["post_id"] == p.id and rows[0]["post_kind"] == "photo"
    r = c.post(f"/api/approvals/{aid}/approve").json()
    assert r["post"]["status"] == "published" and r["post"]["meta_mode"] == "dry_run"
    p2 = _ready("story")
    aid2 = _request(p2)
    c.post(f"/api/approvals/{aid2}/reject")
    assert posts.get(p2.id).status == "draft"


def test_scheduled_post_waits_for_its_time():
    c = TestClient(app, headers=WEB)
    p = _ready(when=(now() + timedelta(hours=2)).isoformat())
    aid = _request(p)
    r = c.post(f"/api/approvals/{aid}/approve").json()
    assert r["post"]["status"] == "scheduled"
    assert asyncio.run(instagram.publish_due()) == []  # not yet
    store.update(SocialPost, p.id, {"scheduled_at": now() - timedelta(minutes=1)})
    assert asyncio.run(instagram.publish_due()) == [p.id]
    assert posts.get(p.id).status == "published"


def test_long_missed_schedule_is_not_posted_by_surprise():
    c = TestClient(app, headers=WEB)
    p = _ready(when=(now() + timedelta(hours=2)).isoformat())
    c.post(f"/api/approvals/{_request(p)}/approve")
    store.update(SocialPost, p.id, {"scheduled_at": now() - timedelta(hours=7)})  # Milo was off
    assert asyncio.run(instagram.publish_due()) == []
    got = posts.get(p.id)
    assert got.status == "failed" and "Missed" in got.error
    posts.update(p.id, caption="again")  # editable again, can be re-requested


# --- live publishing calls (fake Meta) ---

def _publish(p, client):
    return asyncio.run(instagram.publish(p.id, client, sleep=lambda s: asyncio.sleep(0)))


def test_live_photo_publish_calls():
    p = _ready()
    _request(p)
    client = FakeMeta(_live())
    done = _publish(posts.get(p.id), client)
    p = posts.get(p.id)
    base = f"https://milo.example.dev/media/{p.media_token}"
    posts_ = [c for c in client.calls if c[0] == "POST"]
    assert posts_[0] == ("POST", f"/{IG}/media", {"image_url": f"{base}/pub-00.jpg", "caption": p.caption})
    assert posts_[1] == ("POST", f"/{IG}/media_publish", {"creation_id": "c1"})
    assert done.status == "published" and done.permalink == "https://www.instagram.com/p/ABC123/"


def test_live_carousel_reel_and_story_calls():
    car = _ready("carousel", 3)
    _request(car)
    client = FakeMeta(_live())
    _publish(posts.get(car.id), client)
    sent = [c[2] for c in client.calls if c[0] == "POST" and c[1].endswith("/media")]
    assert all(s.get("is_carousel_item") == "true" for s in sent[:3])
    assert sent[3]["media_type"] == "CAROUSEL" and sent[3]["children"] == "c1,c2,c3"

    reel = _ready("reel", 2)
    _request(reel)
    client = FakeMeta(_live(), statuses=["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    _publish(posts.get(reel.id), client)
    body = next(c[2] for c in client.calls if c[0] == "POST" and c[1].endswith("/media"))
    assert body["media_type"] == "REELS" and body["video_url"].endswith("/reel.mp4") and body["share_to_feed"] == "true"
    assert sum(1 for c in client.calls if c[2].get("fields") == "status_code,status") == 3  # waited for processing

    story = _ready("story")
    _request(story)
    client = FakeMeta(_live())
    _publish(posts.get(story.id), client)
    body = next(c[2] for c in client.calls if c[0] == "POST" and c[1].endswith("/media"))
    assert body["media_type"] == "STORIES" and "caption" not in body


def test_failed_processing_is_recorded_and_retry_is_possible(monkeypatch):
    from app import actions

    p = _ready("reel", 1)
    aid = _request(p)
    with pytest.raises(instagram.PublishError, match="ERROR"):
        asyncio.run(actions.execute(store.get(Approval, aid), FakeMeta(_live(), statuses=["ERROR"])))
    p = posts.get(p.id)
    assert p.status == "pending_approval" and "ERROR" in p.error  # approve again to retry


# --- public media address Meta downloads from ---

def test_public_media_serves_only_waiting_post_files_from_any_host():
    p = _ready()
    _request(p)
    p = posts.get(p.id)
    outside = TestClient(app, base_url="https://milo.example.dev")  # not in ALLOWED_HOSTS
    r = outside.get(f"/media/{p.media_token}/pub-00.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert outside.get(f"/media/{p.media_token}/../../.env").status_code in (400, 404)  # never the file
    assert outside.get(f"/media/{'x' * 32}/pub-00.jpg").status_code == 404
    assert outside.get(f"/media/{p.media_token}/{p.images[0].split('/')[-1]}").status_code == 404  # originals: no
    assert outside.get("/api/posts").status_code == 400  # the rest of Milo stays localhost-only
    assert outside.post(f"/media/{p.media_token}/pub-00.jpg").status_code != 200
    store.update(SocialPost, p.id, {"status": "published"})
    assert outside.get(f"/media/{p.media_token}/pub-00.jpg").status_code == 404  # gone once posted


def test_posts_api_round_trip():
    c = TestClient(app, headers=WEB)
    p = c.post("/api/posts", json={"kind": "carousel", "caption": "Before and after #carcare"}).json()
    for color in ((10, 10, 10), (200, 200, 200)):
        r = c.post(f"/api/posts/{p['id']}/images", files={"file": ("a.png", png(color=color), "image/png")})
        assert r.status_code == 200
    assert c.get(f"/api/posts/{p['id']}/images/1").headers["content-type"] == "image/jpeg"
    assert c.patch(f"/api/posts/{p['id']}", json={"caption": "Updated"}).json()["caption"] == "Updated"
    r = c.post(f"/api/posts/{p['id']}/request").json()
    assert r["status"] == "pending_approval"
    assert c.get("/api/posts").json()[0]["approval_id"] == r["approval_id"]
    assert c.delete(f"/api/posts/{p['id']}").status_code == 409  # waiting for approval
    assert TestClient(app).post("/api/posts", json={"kind": "photo"}).status_code == 403  # CSRF header


def test_agent_tool_drafts_a_post_with_ai_images(monkeypatch):
    from app.creative import imagegen
    from app.mcp_server import server

    async def fake_flux(prompt, *a, **k):
        return png()
    monkeypatch.setattr(imagegen, "generate_image", fake_flux)
    r = asyncio.run(server.create_instagram_post("reel", "Ceramic coating day ✨ #ceramiccoating",
                                                 ["glossy black car, studio light", "water beading on paint"]))
    assert r["status"] == "draft_saved" and r["images"] == 2
    p = posts.get(r["post_id"])
    assert p.source == "agent" and len(p.images) == 2
    assert server.request_post_publish(p.id)["status"] == "pending_approval"
    assert "request_post_publish" in server.TOOL_NAMES and "create_instagram_post" in server.TOOL_NAMES


# --- story replies and mentions in the Inbox ---

def test_story_replies_and_mentions_are_recognised():
    body = {"object": "instagram", "entry": [{"id": IG, "messaging": [
        {"sender": {"id": "u1"}, "recipient": {"id": IG}, "timestamp": 1,
         "message": {"mid": "s1", "text": "Love this!", "reply_to": {"story": {"url": "https://cdn.x/s.jpg", "id": "9"}}}},
        {"sender": {"id": "u2"}, "recipient": {"id": IG}, "timestamp": 2,
         "message": {"mid": "s2", "attachments": [{"type": "story_mention", "payload": {"url": "https://cdn.x/m.jpg"}}]}}]}]}
    reply, mention = parse_events(body)
    assert reply.story == {"kind": "reply", "url": "https://cdn.x/s.jpg", "id": "9"} and reply.text == "Love this!"
    assert mention.story["kind"] == "mention" and mention.text == ""
    store.insert(mention)
    d = asyncio.run(agent.decide(store.find(InboundMessage, {"mid": "s2"})[0], Settings(_env_file=None)))
    assert d["action"] == "handoff" and "story" in d["note"]


# --- text on Story / Reel images ---

def test_story_text_is_drawn_into_the_posted_image():
    from app.social import media

    p = posts.create("story", overlay={"headline": "Car wash at your doorstep", "details": "Delhi NCR · 8 am to 8 pm",
                                       "cta": "DM us to book"})
    posts.add_image(p.id, png())
    _request(posts.get(p.id))
    p = posts.get(p.id)
    posted = Image.open(posts.publish_files(p)[0])
    plain = Image.open(io.BytesIO(media.vertical_frame(Path(p.images[0]).read_bytes())))
    assert posted.size == (1080, 1920) and posted.tobytes() != plain.tobytes()
    top = posted.crop((90, 150, 990, 260)).convert("L")
    assert top.getextrema()[1] > 230  # white headline pixels in the top band
    a = store.get(Approval, posts.request_publish(p.id)["approval_id"])
    assert 'Text on the image: "Car wash at your doorstep · Delhi NCR · 8 am to 8 pm · DM us to book"' in a.summary


def test_story_text_limits_and_emoji():
    from app.social import media

    with pytest.raises(posts.PostError, match="headline can be at most 60"):
        posts.create("story", overlay={"headline": "x" * 61})
    assert posts.create("reel", "c", overlay={"headline": "  Shine   on ", "cta": ""}).overlay == {"headline": "Shine on"}
    assert media.clean_text("Fresh wash 🚗✨ today ₹399") == "Fresh wash today ₹399"


def test_preview_frame_endpoint_and_patch():
    c = TestClient(app, headers=WEB)
    p = c.post("/api/posts", json={"kind": "story"}).json()
    c.post(f"/api/posts/{p['id']}/images", files={"file": ("a.png", png(), "image/png")})
    r = c.patch(f"/api/posts/{p['id']}", json={"overlay": {"headline": "Weekend slots open", "cta": "Book now"}})
    assert r.json()["overlay"] == {"headline": "Weekend slots open", "cta": "Book now"}
    img = c.get(f"/api/posts/{p['id']}/preview/0")
    assert img.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(img.content)).size == (1080, 1920)
    assert c.get(f"/api/posts/{p['id']}/preview/5").status_code == 404


def test_agent_can_add_story_text(monkeypatch):
    from app.creative import imagegen
    from app.mcp_server import server

    async def fake_flux(prompt, *a, **k):
        return png()
    monkeypatch.setattr(imagegen, "generate_image", fake_flux)
    r = asyncio.run(server.create_instagram_post("story", "", ["foam on a silver SUV"], headline="At your doorstep",
                                                 cta="DM us to book"))
    assert posts.get(r["post_id"]).overlay == {"headline": "At your doorstep", "cta": "DM us to book"}

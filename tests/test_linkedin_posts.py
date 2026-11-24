"""Organic LinkedIn Page posts (text, photo, multi-image): drafts, approval gate, little text, publishing over the
Posts + Images API (HTTP mocked), never-twice, scheduler, agent tool."""
import asyncio
import io
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings, get_settings
from app.db import store
from app.db.models import SocialPost, now
from app.linkedin.client import LinkedInAPIError, LinkedInClient
from app.main import app
from app.social import instagram, linkedin, posts
from tests.conftest import WEB

ORG = "5515715"


def png(w=1600, h=900, color=(10, 102, 194)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


def _draft(kind="text", n=0, text="Our plant-care workshop is this Saturday (free) #plantcare #smallbusiness",
           when=None):
    p = posts.create(kind, text, when, platform="linkedin", alt_text="Monstera in a white pot")
    for i in range(n):
        posts.add_image(p.id, png(color=(10 + i * 30, 102, 194)))
    return posts.get(p.id)


def _approved(p):
    r = posts.request_publish(p.id)
    assert r["status"] == "pending_approval"
    return posts.get(p.id)


def _live(**kw):
    return Settings(**{**dict(_env_file=None, linkedin_dry_run=False, linkedin_access_token="LI-TOKEN-123456",
                              linkedin_organization_id=ORG, linkedin_api_version="202610"), **kw})


class FakeLinkedIn:
    """httpx transport that plays LinkedIn's Images + Posts API and records every request."""

    def __init__(self, post_status=201, image_statuses=None, upload_host="www.linkedin.com"):
        self.calls, self.n = [], 0
        self.post_status, self.upload_host = post_status, upload_host
        self.image_statuses = list(image_statuses or [])

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = req.url.path
        if path == "/rest/images" and req.url.params.get("action") == "initializeUpload":
            self.n += 1
            return httpx.Response(200, json={"value": {
                "uploadUrl": f"https://{self.upload_host}/dms-uploads/img{self.n}/uploaded-image/0",
                "image": f"urn:li:image:IMG{self.n}", "uploadUrlExpiresAt": 0}})
        if req.method == "PUT":
            return httpx.Response(201)
        if path.startswith("/rest/images/"):
            return httpx.Response(200, json={"status": self.image_statuses.pop(0) if self.image_statuses
                                             else "AVAILABLE"})
        if path == "/rest/posts" and req.method == "POST":
            if self.post_status >= 400:
                return httpx.Response(self.post_status, json={"message": "boom", "status": self.post_status})
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:7000000000000000001"})
        return httpx.Response(404, json={"message": "unexpected " + path})

    def client(self, **kw):
        async def no_sleep(_):
            return None
        return LinkedInClient(_live(**kw), transport=httpx.MockTransport(self), sleep=no_sleep)

    def posts_bodies(self):
        return [json.loads(r.content) for r in self.calls if r.url.path == "/rest/posts" and r.method == "POST"]


# --- little text ---

def test_text_is_escaped_and_hashtags_kept():
    out = linkedin.to_little_text("Q3 results (up 40%) & more_stuff @team [link] #growth #plant_care")
    assert out == ("Q3 results \\(up 40%\\) & more\\_stuff \\@team \\[link\\] "
                   "{hashtag|\\#|growth} {hashtag|\\#|plant\\_care}")
    assert linkedin.to_little_text("price#1 and ##double") == "price\\#1 and \\#\\#double"  # not hashtags


# --- drafts and checks ---

def test_linkedin_types_and_limits():
    with pytest.raises(posts.PostError, match="3000"):
        posts.create("text", "x" * 3001, platform="linkedin")
    with pytest.raises(posts.PostError, match="LinkedIn post type"):
        posts.create("reel", "hi", platform="linkedin")
    with pytest.raises(posts.PostError, match="Platform"):
        posts.create("photo", "hi", platform="tiktok")
    p = _draft("text")
    with pytest.raises(posts.PostError, match="no images"):
        posts.add_image(p.id, png())
    m = _draft("multi_image", n=1)
    with pytest.raises(posts.PostError, match="at least 2"):
        posts.request_publish(m.id)
    posts.add_image(m.id, png())
    assert posts.request_publish(m.id)["status"] == "pending_approval"
    empty = posts.create("text", "", platform="linkedin")
    with pytest.raises(posts.PostError, match="post text"):
        posts.request_publish(empty.id)


def test_images_are_kept_uncropped():
    p = _draft("photo", n=1)
    w, h = Image.open(p.images[0]).size
    assert (w, h) == (1600, 900)  # Instagram would crop 16:9 to 1.91:1 and resize to 1080 wide


def test_switching_platform_keeps_the_closest_type():
    p = posts.create("carousel", "Two looks #plants")
    posts.add_image(p.id, png())
    posts.add_image(p.id, png())
    li = posts.update(p.id, platform="linkedin")
    assert (li.platform, li.kind) == ("linkedin", "multi_image")
    back = posts.update(p.id, platform="instagram")
    assert (back.platform, back.kind) == ("instagram", "carousel")
    t = posts.create("photo", "No picture yet")
    assert posts.update(t.id, platform="linkedin").kind == "text"


def test_live_request_needs_token_but_no_public_url(monkeypatch):
    monkeypatch.setenv("LINKEDIN_DRY_RUN", "false")
    monkeypatch.setenv("META_DRY_RUN", "false")  # Instagram would need PUBLIC_BASE_URL; LinkedIn doesn't
    get_settings.cache_clear()
    p = _draft("photo", n=1)
    with pytest.raises(posts.PostError, match="LINKEDIN_ACCESS_TOKEN"):
        posts.request_publish(p.id)
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "LI-TOKEN-123456")
    monkeypatch.setenv("LINKEDIN_ORGANIZATION_ID", ORG)
    get_settings.cache_clear()
    r = posts.request_publish(p.id)
    assert r["status"] == "pending_approval" and "LinkedIn Page" in r["summary"]


# --- approval gate (dry run) ---

def test_approve_in_dry_run_marks_published_without_linkedin():
    c = TestClient(app, headers=WEB)
    p = _draft("photo", n=1)
    aid = posts.request_publish(p.id)["approval_id"]
    row = c.get("/api/approvals").json()[0]
    assert row["post_platform"] == "linkedin" and row["post_kind"] == "photo"
    r = c.post(f"/api/approvals/{aid}/approve").json()
    assert r["post"]["status"] == "published" and r["post"]["meta_mode"] == "dry_run"
    got = posts.get(p.id)
    assert got.linkedin_post_urn.startswith("urn:li:share:dry_") and got.permalink == ""


def test_api_creates_and_lists_linkedin_drafts():
    c = TestClient(app, headers=WEB)
    d = c.post("/api/posts", json={"platform": "linkedin", "kind": "text", "caption": "Hello LinkedIn"}).json()
    assert d["platform"] == "linkedin" and d["kind_label"] == "Text" and d["max_images"] == 0
    d = c.patch(f"/api/posts/{d['id']}", json={"kind": "multi_image", "alt_text": "Two plants"}).json()
    assert d["kind"] == "multi_image" and d["max_images"] == 20 and d["alt_text"] == "Two plants"
    assert c.get("/api/status").json()["linkedin"]["dry_run"] is True


# --- publishing (HTTP mocked) ---

def test_live_multi_image_publish_uploads_waits_and_posts():
    fake = FakeLinkedIn(image_statuses=["PROCESSING", "AVAILABLE", "AVAILABLE"])
    p = _approved(_draft("multi_image", n=2))
    got = asyncio.run(linkedin.publish(p.id, fake.client()))
    assert got.status == "published" and got.meta_mode == "live"
    assert got.linkedin_post_urn == "urn:li:share:7000000000000000001"
    assert got.permalink == "https://www.linkedin.com/feed/update/urn:li:share:7000000000000000001/"
    api = [r for r in fake.calls if r.url.host == "api.linkedin.com"]
    assert all(r.headers["LinkedIn-Version"] == "202610" and r.headers["X-Restli-Protocol-Version"] == "2.0.0"
               for r in api)
    init = [json.loads(r.content) for r in fake.calls if r.url.params.get("action") == "initializeUpload"]
    assert init == [{"initializeUploadRequest": {"owner": f"urn:li:organization:{ORG}"}}] * 2
    puts = [r for r in fake.calls if r.method == "PUT"]
    assert len(puts) == 2 and puts[0].content[:2] == b"\xff\xd8"  # JPEG bytes
    (body,) = fake.posts_bodies()
    assert body["author"] == f"urn:li:organization:{ORG}" and body["visibility"] == "PUBLIC"
    assert body["lifecycleState"] == "PUBLISHED" and body["distribution"]["feedDistribution"] == "MAIN_FEED"
    assert body["content"] == {"multiImage": {"images": [
        {"id": "urn:li:image:IMG1", "altText": "Monstera in a white pot"},
        {"id": "urn:li:image:IMG2", "altText": "Monstera in a white pot"}]}}
    assert body["commentary"].startswith("Our plant-care workshop is this Saturday \\(free\\)")
    assert "{hashtag|\\#|plantcare}" in body["commentary"]


def test_text_post_has_no_content_and_single_photo_uses_media():
    fake = FakeLinkedIn()
    asyncio.run(linkedin.publish(_approved(_draft("text")).id, fake.client()))
    asyncio.run(linkedin.publish(_approved(_draft("photo", n=1)).id, fake.client()))
    text_body, photo_body = fake.posts_bodies()
    assert "content" not in text_body
    assert photo_body["content"] == {"media": {"id": "urn:li:image:IMG1", "altText": "Monstera in a white pot"}}


def test_server_error_on_create_is_not_retried_and_marks_failed():
    fake = FakeLinkedIn(post_status=500)
    p = _approved(_draft("text"))
    with pytest.raises(LinkedInAPIError):
        asyncio.run(linkedin.publish(p.id, fake.client()))
    assert len(fake.posts_bodies()) == 1  # a 5xx may have created it: never send it twice
    got = posts.get(p.id)
    assert got.status == "failed" and "500" in got.error and "LI-TOKEN" not in got.error


def test_rate_limit_on_create_is_retried():
    fake = FakeLinkedIn(post_status=429)
    p = _approved(_draft("text"))
    with pytest.raises(LinkedInAPIError):
        asyncio.run(linkedin.publish(p.id, fake.client()))
    assert len(fake.posts_bodies()) == 4  # 1 + 3 retries; a 429 means nothing was created


def test_upload_url_off_linkedin_is_refused():
    fake = FakeLinkedIn(upload_host="evil.example.com")
    p = _approved(_draft("photo", n=1))
    with pytest.raises(LinkedInAPIError, match="upload host"):
        asyncio.run(linkedin.publish(p.id, fake.client()))
    assert not [r for r in fake.calls if r.method == "PUT"]  # the token never left LinkedIn


def test_failed_image_processing_stops_before_the_post():
    fake = FakeLinkedIn(image_statuses=["PROCESSING_FAILED"])
    p = _approved(_draft("photo", n=1))
    with pytest.raises(LinkedInAPIError):
        asyncio.run(linkedin.publish(p.id, fake.client()))
    assert fake.posts_bodies() == [] and posts.get(p.id).status == "failed"


def test_never_posted_twice():
    fake = FakeLinkedIn()
    p = _approved(_draft("text"))
    store.update(SocialPost, p.id, {"meta_mode": "live", "linkedin_post_urn": "urn:li:share:1"})
    got = asyncio.run(linkedin.publish(p.id, fake.client()))
    assert got.status == "published" and fake.calls == []


def test_interrupted_publish_is_recovered_with_a_linkedin_note():
    p = _approved(_draft("text"))
    store.update(SocialPost, p.id, {"status": "publishing", "publishing_started_at": now() - timedelta(hours=1)})
    assert instagram.fail_stuck_posts() == [p.id]
    got = posts.get(p.id)
    assert got.status == "failed" and "Check LinkedIn" in got.error


def test_scheduler_publishes_linkedin_posts_at_their_time():
    c = TestClient(app, headers=WEB)
    p = _draft("text", when=(now() + timedelta(hours=2)).isoformat())
    aid = posts.request_publish(p.id)["approval_id"]
    assert c.post(f"/api/approvals/{aid}/approve").json()["post"]["status"] == "scheduled"
    store.update(SocialPost, p.id, {"scheduled_at": now() - timedelta(minutes=1)})
    assert asyncio.run(instagram.publish_due()) == [p.id]
    assert posts.get(p.id).linkedin_post_urn.startswith("urn:li:share:dry_")


class FakeResults:
    """LinkedIn's results APIs: socialMetadata and memberCreatorPostAnalytics, each allowed or refused (403)."""

    def __init__(self, metadata_ok=True, analytics_ok=True):
        self.metadata_ok, self.analytics_ok, self.calls = metadata_ok, analytics_ok, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if req.url.path.startswith("/rest/socialMetadata/"):
            if not self.metadata_ok:
                return httpx.Response(403, json={"message": "Not enough permissions", "status": 403})
            return httpx.Response(200, json={"reactionSummaries": {"LIKE": {"count": 5}, "PRAISE": {"count": 2}},
                                             "commentSummary": {"count": 3, "topLevelCount": 2}})
        if req.url.path == "/rest/memberCreatorPostAnalytics":
            if not self.analytics_ok:
                return httpx.Response(403, json={"message": "Not enough permissions", "status": 403})
            n = {"IMPRESSION": 140, "MEMBERS_REACHED": 96, "RESHARE": 1}[req.url.params["queryType"]]
            return httpx.Response(200, json={"elements": [{"count": n, "metricType": {}}]})
        return httpx.Response(404, json={"message": "unexpected"})

    def client(self, **kw):
        return LinkedInClient(_live(**{"linkedin_author": "me", "linkedin_person_id": "abc", **kw}),
                              transport=httpx.MockTransport(self))


def _published(urn="urn:li:share:7000000000000000001"):
    p = _draft("text")
    return store.update(SocialPost, p.id, {"status": "published", "meta_mode": "live", "linkedin_post_urn": urn,
                                           "published_at": now()})


def test_results_read_reactions_comments_and_impressions():
    fake = FakeResults()
    got = asyncio.run(linkedin.fetch_results(_published(), fake.client()))
    assert got.insights == {"reactions": 7, "comments": 3, "impressions": 140, "members_reached": 96, "reshares": 1}
    assert got.insights_note == "" and got.insights_at is not None
    meta = next(r for r in fake.calls if r.url.path.startswith("/rest/socialMetadata/"))
    assert meta.url.raw_path.endswith(b"/socialMetadata/urn%3Ali%3Ashare%3A7000000000000000001")
    a = next(r for r in fake.calls if r.url.path == "/rest/memberCreatorPostAnalytics")
    assert b"entity=(share:urn%3Ali%3Ashare%3A7000000000000000001)" in a.url.raw_path
    assert a.url.params["q"] == "entity" and a.url.params["aggregation"] == "TOTAL"


def test_results_without_permission_explain_and_keep_what_works():
    fake = FakeResults(analytics_ok=False)
    got = asyncio.run(linkedin.fetch_results(_published(), fake.client()))
    assert got.insights == {"reactions": 7, "comments": 3} and got.insights_blocked is False  # partly allowed
    assert "r_member_postAnalytics" in got.insights_note
    assert len([r for r in fake.calls if r.url.path == "/rest/memberCreatorPostAnalytics"]) == 1  # stops at first 403
    fake = FakeResults(metadata_ok=False, analytics_ok=False)
    got = asyncio.run(linkedin.fetch_results(_published(), fake.client()))
    assert got.insights == {} and "reactions and comments" in got.insights_note
    assert got.insights_blocked is True


def test_refused_results_are_checked_daily_not_hourly():
    from datetime import timedelta as td

    from app.social import insights

    fake = FakeResults(metadata_ok=False, analytics_ok=False)
    got = asyncio.run(linkedin.fetch_results(_published(), fake.client()))
    assert got.insights_blocked and not insights.due(got)  # just checked
    later = store.update(SocialPost, got.id, {"insights_at": now() - td(hours=2)})
    assert not insights.due(later)  # an hour or two later: still no
    assert insights.due(store.update(SocialPost, got.id, {"insights_at": now() - td(hours=25)}))  # next day: yes
    # once the app gets the permission, a refresh clears the flag and hourly checks come back
    fixed = asyncio.run(linkedin.fetch_results(posts.get(got.id), FakeResults().client()))
    assert not fixed.insights_blocked and fixed.insights["impressions"] == 140


def test_results_for_ugc_posts_and_page_posts():
    fake = FakeResults()
    asyncio.run(linkedin.fetch_results(_published("urn:li:ugcPost:55"), fake.client()))
    a = next(r for r in fake.calls if r.url.path == "/rest/memberCreatorPostAnalytics")
    assert b"entity=(ugc:urn%3Ali%3AugcPost%3A55)" in a.url.raw_path
    fake = FakeResults()
    got = asyncio.run(linkedin.fetch_results(_published(), fake.client(linkedin_author="page")))
    assert got.insights == {"reactions": 7, "comments": 3} and "Page posts" in got.insights_note
    assert not [r for r in fake.calls if r.url.path == "/rest/memberCreatorPostAnalytics"]


def test_results_in_dry_run_never_call_linkedin():
    from app.social import insights

    p = _published()
    assert asyncio.run(insights.refresh_due()) == [p.id]  # LinkedIn posts are refreshed too
    assert "LINKEDIN_DRY_RUN" in posts.get(p.id).insights_note and posts.get(p.id).insights == {}
    d = _draft("text")
    store.update(SocialPost, d.id, {"status": "published", "meta_mode": "dry_run", "published_at": now()})
    assert "Dry run" in asyncio.run(insights.fetch(d.id)).insights_note


# --- agent tool ---

def test_agent_drafts_linkedin_posts(monkeypatch):
    from app.creative import imagegen
    from app.mcp_server import server

    async def fake_flux(prompt, *a, **k):
        return png()
    monkeypatch.setattr(imagegen, "generate_image", fake_flux)
    r = asyncio.run(server.create_linkedin_post("We're hiring a plant doctor 🌿 #hiring"))
    assert r["status"] == "draft_saved" and r["kind"] == "text" and r["platform"] == "linkedin"
    r2 = asyncio.run(server.create_linkedin_post("Before and after", ["wilted fern", "healthy fern"]))
    p = posts.get(r2["post_id"])
    assert (p.platform, p.kind, len(p.images), p.source) == ("linkedin", "multi_image", 2, "agent")
    assert server.request_post_publish(p.id)["status"] == "pending_approval"
    assert "create_linkedin_post" in server.TOOL_NAMES


# --- posting as your own profile (LINKEDIN_AUTHOR=me, self-serve w_member_social) ---

def test_post_as_my_profile_uses_person_urn_for_post_and_images():
    fake = FakeLinkedIn()
    p = _approved(_draft("multi_image", n=2))
    got = asyncio.run(linkedin.publish(p.id, fake.client(linkedin_author="me", linkedin_person_id="abc123XYZ",
                                                         linkedin_organization_id="")))
    assert got.status == "published" and got.meta_mode == "live"
    init = [json.loads(r.content) for r in fake.calls if r.url.params.get("action") == "initializeUpload"]
    assert init == [{"initializeUploadRequest": {"owner": "urn:li:person:abc123XYZ"}}] * 2
    (body,) = fake.posts_bodies()
    assert body["author"] == "urn:li:person:abc123XYZ" and len(body["content"]["multiImage"]["images"]) == 2


def test_profile_mode_needs_person_id_not_page(monkeypatch):
    monkeypatch.setenv("LINKEDIN_DRY_RUN", "false")
    monkeypatch.setenv("LINKEDIN_AUTHOR", "me")
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "LI-TOKEN-123456")
    get_settings.cache_clear()
    p = _draft("text")
    with pytest.raises(posts.PostError, match="LINKEDIN_PERSON_ID"):
        posts.request_publish(p.id)
    monkeypatch.setenv("LINKEDIN_PERSON_ID", "abc123XYZ")
    get_settings.cache_clear()
    r = posts.request_publish(p.id)
    assert r["status"] == "pending_approval" and "your own LinkedIn profile" in r["summary"]
    c = TestClient(app, headers=WEB)
    st = c.get("/api/status").json()["linkedin"]
    assert st["author"] == "me" and st["configured"] is True
    assert c.get(f"/api/posts/{p.id}").json()["linkedin_author"] == "me"

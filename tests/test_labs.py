"""Trends page (prototype; code name "labs"): own-account patterns, upcoming moments, competitor watch, Ad Library link, on/off switch."""
import asyncio
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import labs
from app.config import Settings, get_settings
from app.db import store
from app.db.models import Competitor, SocialPost, now
from app.labs import competitors, moments, own
from app.main import app
from app.mcp_server.server import TOOL_NAMES, get_trends_and_ideas
from tests.conftest import WEB

IST = timezone(timedelta(hours=5, minutes=30))


def _post(kind, reach, when_ist, caption="Foam wash ₹499 at your door"):
    store.insert(SocialPost(kind=kind, caption=caption, status="published", meta_mode="live",
                            meta_media_id=f"m{reach}", published_at=when_ist.astimezone(timezone.utc),
                            insights={"reach": reach, "like_count": 10}))


def test_own_account_needs_enough_posts_then_finds_patterns():
    assert own.summary()["ready"] is False
    base = datetime.now(IST).replace(minute=0, second=0, microsecond=0) - timedelta(days=3)
    sunday = base - timedelta(days=(base.weekday() - 6) % 7)  # most recent Sunday
    _post("reel", 1200, sunday.replace(hour=19))
    _post("reel", 1000, sunday.replace(hour=20) - timedelta(days=7))
    _post("photo", 400, (sunday - timedelta(days=4)).replace(hour=10), caption="A long caption " * 12)
    _post("photo", 500, (sunday - timedelta(days=3)).replace(hour=11), caption="Spotless car, no price here")
    r = own.summary()
    assert r["ready"] and r["metric"] == "reach" and r["with_results"] == 4
    assert r["best_format"] == {"name": "Reels", "posts": 2, "avg": 1100}
    assert r["best_format_ratio"] == round(1100 / 450, 1)
    assert (r["best_time"]["day"], r["best_time"]["slot"]) == ("Weekend", "Evening")
    assert r["caption_price"][0]["name"] == "Shows a price"
    assert "reel" in r["idea_prompt"].lower() and "facts and prices i have given" in r["idea_prompt"].lower()


def test_dry_run_and_old_posts_are_ignored():
    store.insert(SocialPost(kind="reel", status="published", meta_mode="dry_run", published_at=now(),
                            insights={"reach": 999}))
    store.insert(SocialPost(kind="reel", status="published", meta_mode="live", published_at=now() - timedelta(days=60),
                            insights={"reach": 999}))
    assert own.summary()["with_results"] == 0


def test_upcoming_moments_window_and_prompts():
    ms = moments.upcoming(today=date(2026, 11, 6), business="Doorstep Car Care")
    names = [m["name"] for m in ms]
    assert "Diwali" in names and "Bhai Dooj" in names and "Christmas" in names
    assert "Monsoon" not in names and "Valentine's Day" not in names  # outside the 60 days
    diwali = next(m for m in ms if m["name"] == "Diwali")
    assert diwali["start"] == "2026-11-08" and diwali["days_away"] == 2
    assert "Doorstep Car Care" in diwali["post_prompt"] and "ask me for the offer" in diwali["post_prompt"]
    assert diwali["ad_prompt"].startswith("Make a Facebook and Instagram ad")
    season = next(m for m in ms if m["name"].startswith("Winter dust"))
    assert season["ongoing"] and season["end"] == "2027-01-31"
    assert ms[0]["ongoing"] and not any(m["ongoing"] for m in ms[sum(x["ongoing"] for x in ms):])  # ongoing first


def test_ad_library_link():
    url = labs.ad_library_url("car wash")
    assert url.startswith("https://www.facebook.com/ads/library/?") and "q=car+wash" in url and "country=IN" in url


class FakeMeta:
    def __init__(self, s, payload=None, error=None):
        self.s, self.payload, self.error, self.calls = s, payload, error, []

    async def get(self, path, token=None, **params):
        self.calls.append((path, params, token))
        if self.error:
            raise self.error
        return self.payload

    async def aclose(self):
        pass


def _iso(days_ago):
    return (now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


def test_competitor_add_validate_refresh_and_rank():
    live = Settings(_env_file=None, meta_dry_run=False, meta_page_access_token="PT", meta_page_id="P1",
                    meta_instagram_account_id="IG1")
    assert competitors.add("@Sample_AutoSpa").username == "sample_autospa"
    for bad in ("", "has space", "x" * 31, "bad/char"):
        try:
            competitors.add(bad)
            raise AssertionError(bad)
        except competitors.LabsError:
            pass
    try:
        competitors.add("sample_autospa")
        raise AssertionError("duplicate")
    except competitors.LabsError as exc:
        assert "already" in str(exc)
    payload = {"business_discovery": {"username": "sample_autospa", "name": "Sample Auto Spa", "followers_count": 5400,
                                      "media_count": 120, "media": {"data": [
        {"caption": "Before/after SUV", "like_count": 2100, "comments_count": 210, "media_type": "VIDEO",
         "media_product_type": "REELS", "permalink": "https://instagram.com/p/a", "timestamp": _iso(3)},
        {"caption": "5 signs", "like_count": 900, "comments_count": 30, "media_type": "CAROUSEL_ALBUM",
         "media_product_type": "FEED", "permalink": "https://instagram.com/p/b", "timestamp": _iso(5)},
        {"caption": "Hidden likes", "comments_count": 40, "media_type": "IMAGE", "media_product_type": "FEED",
         "permalink": "https://instagram.com/p/c", "timestamp": _iso(8)},
        {"caption": "Too old", "like_count": 9999, "comments_count": 0, "media_type": "IMAGE",
         "media_product_type": "FEED", "timestamp": _iso(60)}]}}}
    m = FakeMeta(live, payload)
    c = store.find(Competitor)[0]
    asyncio.run(competitors.refresh(c, client=m, force=True))
    path, params, token = m.calls[0]
    assert path == "/IG1" and token == "PT" and "business_discovery.username(sample_autospa)" in params["fields"]
    o = competitors.overview()
    assert [p["engagement"] for p in o["top_posts"]] == [2310, 930, 40]  # the 60-day-old post is left out
    assert o["accounts"][0]["followers"] == 5400 and o["accounts"][0]["posts_in_period"] == 3
    assert o["findings"][0].startswith("Reels get") and "hide their likes" in o["note"]
    assert asyncio.run(competitors.refresh(store.find(Competitor)[0], client=FakeMeta(live, payload))) is not None
    assert len(m.calls) == 1  # refreshed less than 6 hours ago: no second call


def test_competitor_errors_dry_run_and_limit():
    from app.meta.client import MetaAPIError

    c = competitors.add("someone")
    dry = asyncio.run(competitors.refresh(c, client=FakeMeta(Settings(_env_file=None, meta_dry_run=True)),
                                          force=True))
    assert "Dry run" in dry.error
    live = Settings(_env_file=None, meta_dry_run=False, meta_page_access_token="PT", meta_page_id="P1",
                    meta_instagram_account_id="IG1")
    err = MetaAPIError(110, None, "Invalid user id: cannot be found")
    got = asyncio.run(competitors.refresh(c, client=FakeMeta(live, error=err), force=True))
    assert "business or creator account" in got.error
    for i in range(competitors.MAX_ACCOUNTS - 1):
        competitors.add(f"acct{i}")
    try:
        competitors.add("one_too_many")
        raise AssertionError("limit")
    except competitors.LabsError as exc:
        assert "up to 10" in str(exc)


def test_api_routes_and_switch(monkeypatch):
    c = TestClient(app, headers=WEB)
    d = c.get("/api/labs?keyword=car wash").json()
    assert d["version"] == labs.VERSION and "q=car+wash" in d["ad_library"]["url"] and d["moments"]
    r = c.post("/api/labs/competitors", json={"username": "@sample_carcare"})
    assert r.status_code == 200 and r.json()["accounts"][0]["username"] == "sample_carcare"
    assert "Dry run" in r.json()["accounts"][0]["error"]  # tests run in dry run: no Instagram call
    assert c.post("/api/labs/competitors", json={"username": "bad name"}).status_code == 409
    assert c.delete("/api/labs/competitors/sample_carcare").status_code == 200
    assert c.delete("/api/labs/competitors/sample_carcare").status_code == 404
    assert TestClient(app).post("/api/labs/competitors", json={"username": "x"}).status_code == 403  # CSRF
    assert c.get("/api/status").json()["labs_enabled"] is True
    assert "get_trends_and_ideas" in TOOL_NAMES and get_trends_and_ideas()["prototype"] is True

    monkeypatch.setenv("LABS_ENABLED", "false")
    get_settings.cache_clear()
    assert c.get("/api/labs").status_code == 404
    assert c.get("/api/status").json()["labs_enabled"] is False
    assert get_trends_and_ideas()["status"] == "unavailable"


def test_hashtags_show_labelled_samples_until_meta_approves():
    from app.labs import hashtags

    h = asyncio.run(hashtags.lookup("#CarWash"))
    assert h["tag"] == "carwash" and h["sample"] and not h["live_enabled"]
    assert all(p["sample"] for p in h["top"] + h["recent"]) and "Sample posts" in h["note"]
    assert "verified Meta business" in h["requirement"]
    assert h["top"][0]["engagement"] >= h["top"][-1]["engagement"] and h["findings"][0].startswith("Reels get")
    for bad in ("", "two words", "🔥", "#" * 3):
        try:
            hashtags.clean(bad)
            raise AssertionError(bad)
        except competitors.LabsError:
            pass
    assert hashtags.clean("दिवाली") == "दिवाली"  # Hindi hashtags are fine


class FakeTags(FakeMeta):
    async def get(self, path, token=None, **params):
        self.calls.append((path, params, token))
        if self.error:
            raise self.error
        if path == "/ig_hashtag_search":
            return {"data": [{"id": "H1"}]}
        likes = 50 if "recent" in path else 900
        return {"data": [{"caption": "Shine #carwash #suv", "like_count": likes, "comments_count": 5,
                          "media_type": "VIDEO", "media_product_type": "REELS", "permalink": "https://instagram.com/p/x",
                          "timestamp": _iso(0)},
                         {"caption": "Hidden likes #carwash", "comments_count": 7, "media_type": "IMAGE",
                          "permalink": "https://instagram.com/p/y", "timestamp": _iso(0)}]}


def test_hashtags_live_search_cache_weekly_limit_and_approval_error(monkeypatch):
    from app.db.models import HashtagSearch
    from app.labs import hashtags
    from app.meta.client import MetaAPIError

    monkeypatch.setenv("LABS_HASHTAGS_LIVE", "true")
    monkeypatch.setenv("META_DRY_RUN", "false")
    monkeypatch.setenv("META_INSTAGRAM_ACCOUNT_ID", "IG1")
    get_settings.cache_clear()
    s = get_settings()
    m = FakeTags(s)
    h = asyncio.run(hashtags.lookup("carwash", client=m))
    assert not h["sample"] and h["top"][0]["engagement"] == 905 and h["top"][1]["likes"] is None
    assert m.calls[0][0] == "/ig_hashtag_search" and m.calls[0][1] == {"user_id": "IG1", "q": "carwash"}
    assert {c[0] for c in m.calls[1:]} == {"/H1/top_media", "/H1/recent_media"} and h["used"] == 1
    asyncio.run(hashtags.lookup("carwash", client=m))
    assert len(m.calls) == 3  # cached for 6 hours
    asyncio.run(hashtags.lookup("carwash", client=m, force=True))
    assert len(m.calls) == 5 and "/ig_hashtag_search" not in [c[0] for c in m.calls[3:]]  # id reused, not counted
    assert hashtags.used_this_week() == 1

    err = MetaAPIError(10, None, "To use 'Instagram Public Content Access', your use of this endpoint must be reviewed")
    got = asyncio.run(hashtags.lookup("cardetailing", client=FakeTags(s, error=err)))
    assert got["sample"] and "hasn't approved" in got["reason"]

    for i in range(hashtags.WEEKLY_LIMIT - 1):
        store.insert(HashtagSearch(tag=f"t{i}", first_queried_at=now()))
    try:
        asyncio.run(hashtags.lookup("onemore", client=FakeTags(s)))
        raise AssertionError("limit")
    except competitors.LabsError as exc:
        assert "30 different hashtags" in str(exc)
    asyncio.run(hashtags.lookup("carwash", client=FakeTags(s), force=True))  # already counted: still allowed
    get_settings.cache_clear()


def test_hashtag_route():
    c = TestClient(app, headers=WEB)
    assert c.get("/api/labs").json()["hashtags"]["live_enabled"] is False
    r = c.post("/api/labs/hashtags", json={"tag": "cardetailing"}).json()
    assert r["sample"] and r["tag"] == "cardetailing"
    assert c.post("/api/labs/hashtags", json={"tag": "bad tag"}).status_code == 409
    assert TestClient(app).post("/api/labs/hashtags", json={"tag": "x"}).status_code == 403  # CSRF header needed
    assert c.get("/api/labs/hashtags?tag=x").status_code in (404, 405)  # never a GET
    assert get_trends_and_ideas()["hashtags"]["live"] is False

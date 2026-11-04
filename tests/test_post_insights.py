"""Results of published Instagram posts: likes/comments fields, insights metrics, Story rules, scheduler."""
import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import store
from app.db.models import SocialPost, now
from app.main import app
from app.meta.client import MetaAPIError
from app.social import insights
from tests.conftest import WEB


def _s():
    return Settings(_env_file=None, meta_dry_run=False, meta_page_access_token="PT", meta_page_id="555")


class FakeMeta:
    def __init__(self, fields=None, metrics=None, fail=None, fail_metric=None):
        self.s, self.calls = _s(), []
        self.fields, self.metrics, self.fail, self.fail_metric = fields or {}, metrics or {}, fail, fail_metric

    async def get(self, path, token=None, **params):
        self.calls.append((path, params))
        assert token == "PT"
        if path.endswith("/insights"):
            if self.fail:
                raise self.fail
            names = params["metric"].split(",")
            if self.fail_metric and self.fail_metric in names:
                raise MetaAPIError(100, None, f"metric {self.fail_metric} not supported")
            return {"data": [{"name": n, "values": [{"value": self.metrics[n]}]} for n in names if n in self.metrics]}
        return self.fields

    async def aclose(self):
        pass


def _published(kind="photo", hours_ago=1.0, mode="live"):
    return store.insert(SocialPost(kind=kind, status="published", meta_mode=mode, meta_media_id="1789",
                                   published_at=now() - timedelta(hours=hours_ago)))


def test_post_results_combine_fields_and_insights():
    p = _published("reel")
    client = FakeMeta(fields={"like_count": 42, "comments_count": 5},
                      metrics={"reach": 310, "views": 520, "saved": 7, "shares": 3, "total_interactions": 57,
                               "ig_reels_avg_watch_time": 4200})
    done = asyncio.run(insights.fetch(p.id, client))
    assert done.insights == {"like_count": 42, "comments_count": 5, "reach": 310, "views": 520, "saved": 7,
                             "shares": 3, "total_interactions": 57, "ig_reels_avg_watch_time": 4200}
    assert done.insights_at and done.insights_note == ""


def test_story_results_have_no_likes_and_expire():
    story = _published("story", hours_ago=2)
    done = asyncio.run(insights.fetch(story.id, FakeMeta(metrics={"reach": 12, "views": 20, "replies": 2})))
    assert done.insights == {"reach": 12, "views": 20, "replies": 2} and "Story likes" in done.insights_note
    old = _published("story", hours_ago=30)
    client = FakeMeta(metrics={"reach": 1})
    done = asyncio.run(insights.fetch(old.id, client))
    assert "expired" in done.insights_note and not any(c[0].endswith("/insights") for c in client.calls)


def test_missing_permission_and_small_audience_are_explained():
    p = _published()
    done = asyncio.run(insights.fetch(p.id, FakeMeta(fields={"like_count": 3},
                                                     fail=MetaAPIError(10, None, "Application does not have "
                                                                                 "permission for this action"))))
    assert done.insights == {"like_count": 3} and "instagram_manage_insights" in done.insights_note
    done = asyncio.run(insights.fetch(p.id, FakeMeta(fail=MetaAPIError(10, None, "Not enough viewers for the "
                                                                                 "media to show insights"))))
    assert "Fewer than 5" in done.insights_note


def test_one_unsupported_metric_does_not_lose_the_others():
    p = _published("carousel")
    client = FakeMeta(metrics={"reach": 50, "views": 80, "saved": 1}, fail_metric="shares")
    done = asyncio.run(insights.fetch(p.id, client))
    assert done.insights == {"reach": 50, "views": 80, "saved": 1}


def test_dry_run_posts_have_no_results_and_drafts_are_refused():
    c = TestClient(app, headers=WEB)
    dry = _published(mode="dry_run")
    r = c.post(f"/api/posts/{dry.id}/insights/refresh").json()
    assert "Dry run" in r["insights_note"]
    draft = store.insert(SocialPost(kind="photo"))
    assert c.post(f"/api/posts/{draft.id}/insights/refresh").status_code == 409


def test_scheduler_refresh_rules():
    story = _published("story", hours_ago=1)
    old_story = _published("story", hours_ago=30)
    post = _published("photo", hours_ago=48)
    old_post = _published("photo", hours_ago=24 * 10)
    assert insights.due(story) and not insights.due(old_story) and insights.due(post) and not insights.due(old_post)
    fresh = store.update(SocialPost, post.id, {"insights_at": now() - timedelta(minutes=10)})
    assert not insights.due(fresh)  # posts: at most hourly

"""Results of published Instagram posts (read-only): likes, comments, saves, shares, views, reach...

Instagram Graph API, Facebook Login path, Page token:
- like_count and comments_count are fields on the media (GET /{media-id}?fields=like_count,comments_count) and
  need no extra permission;
- everything else is GET /{media-id}/insights?metric=... and needs instagram_manage_insights.
Instagram doesn't share Story likes at all. Story numbers exist only while the Story is live (24 hours), and
values under 5 are hidden for privacy (error code 10).
"""
import logging
from datetime import timedelta

from app.db import store
from app.db.models import SocialPost, now
from app.dm import inbox
from app.meta.client import MetaAPIError, MetaClient
from app.util.redact import redact

METRICS = {
    "photo": ["reach", "views", "saved", "shares", "total_interactions"],
    "carousel": ["reach", "views", "saved", "shares", "total_interactions"],
    "reel": ["reach", "views", "saved", "shares", "total_interactions", "ig_reels_avg_watch_time"],
    "story": ["reach", "views", "replies", "shares", "total_interactions", "follows", "profile_visits"],
}
STORY_HOURS = 24
FEED_DAYS = 7  # the scheduler keeps refreshing posts and Reels for a week
log = logging.getLogger("milo.posts")


def _value(row: dict):
    if "total_value" in row:
        return (row.get("total_value") or {}).get("value")
    values = row.get("values") or [{}]
    return values[-1].get("value")


async def fetch(post_id: int, client: MetaClient | None = None) -> SocialPost:
    """Ask Instagram for this post's latest numbers and store them. Never raises for Meta errors: the reason
    is kept in insights_note so the page can explain it."""
    p = store.get(SocialPost, post_id)
    if p is None or p.status != "published":
        raise ValueError("Results exist only for published posts.")
    if p.meta_mode != "live" or not p.meta_media_id:
        return store.update(SocialPost, p.id, {"insights_note": "Dry run: this post isn't on Instagram, so there "
                                                                "are no results."})
    own = client is None
    client = client or MetaClient()
    found: dict = dict(p.insights)
    notes: list[str] = []
    try:
        token = await inbox._page_token(client, client.s.meta_page_id)
        if p.kind != "story":  # likes and comments: plain fields, no insights permission needed
            try:
                f = await client.get(f"/{p.meta_media_id}", token=token, fields="like_count,comments_count")
                found.update({k: f[k] for k in ("like_count", "comments_count") if k in f})
            except MetaAPIError as exc:
                notes.append(f"Likes/comments: {redact(exc)[:120]}")
        else:
            notes.append("Instagram doesn't share Story likes (only replies, views and reach).")
        expired = p.kind == "story" and p.published_at and now() - p.published_at > timedelta(hours=STORY_HOURS)
        if expired:
            notes.append("The Story has expired; Instagram keeps Story numbers for 24 hours only.")
        else:
            metrics = METRICS.get(p.kind, [])
            try:
                rows = (await client.get(f"/{p.meta_media_id}/insights", token=token,
                                         metric=",".join(metrics))).get("data") or []
                found.update({r["name"]: _value(r) for r in rows if r.get("name")})
            except MetaAPIError as exc:
                text = str(exc).lower()
                if "viewer" in text or "not enough" in text:  # (#10) Not enough viewers for the media...
                    notes.append("Fewer than 5 people have seen it yet, so Instagram hides views and reach.")
                elif exc.code in (10, 200) or "permission" in text:
                    notes.append("Views, reach and saves need the instagram_manage_insights permission on the "
                                 "token. Likes and comments still work without it.")
                else:  # one metric Instagram doesn't support for this post: ask one by one, keep what works
                    for m in metrics:
                        try:
                            rows = (await client.get(f"/{p.meta_media_id}/insights", token=token,
                                                     metric=m)).get("data") or []
                            found.update({r["name"]: _value(r) for r in rows if r.get("name")})
                        except MetaAPIError:
                            continue
    finally:
        if own:
            await client.aclose()
    found = {k: v for k, v in found.items() if isinstance(v, (int, float))}
    return store.update(SocialPost, p.id, {"insights": found, "insights_at": now(), "insights_note": " ".join(notes)})


def due(p: SocialPost) -> bool:
    """Should the scheduler refresh this post now? Stories every 15 min while live, posts hourly for a week."""
    if p.status != "published" or p.meta_mode != "live" or not p.published_at:
        return False
    age = now() - p.published_at
    since = now() - p.insights_at if p.insights_at else timedelta(days=999)
    if p.kind == "story":
        return age <= timedelta(hours=STORY_HOURS) and since >= timedelta(minutes=15)
    return age <= timedelta(days=FEED_DAYS) and since >= timedelta(hours=1)


async def refresh_due() -> list[int]:
    done = []
    for p in store.find(SocialPost, {"status": "published", "meta_mode": "live"}):
        if due(p):
            try:
                await fetch(p.id)
                done.append(p.id)
            except Exception:  # noqa: BLE001 - one post failing never stops the others
                log.exception("results for post %s could not be refreshed", p.id)
    return done

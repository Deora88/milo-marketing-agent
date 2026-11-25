"""Publishing an approved post to Instagram (Content Publishing API, Instagram API with Facebook Login).

Runs only for an approved "publish_post" approval: from app/actions.py when the user approves, or from the
scheduler at the approved time. Flow (graph.facebook.com, Page access token, instagram_content_publish):
1. create a media container per item: POST /{ig-user-id}/media (image_url / video_url / media_type);
   carousel: one container per image (is_carousel_item=true), then a CAROUSEL container with the children;
2. wait until each container's status_code is FINISHED (videos are processed asynchronously);
3. POST /{ig-user-id}/media_publish (creation_id) and read the permalink.
Meta downloads the media from PUBLIC_BASE_URL/media/<token>/<file>, served by app/api/posts.py.
"""
import asyncio
import logging
import uuid
from datetime import timedelta

from app.db import store
from app.db.models import SocialPost, now
from app.dm import inbox
from app.meta import campaigns as meta
from app.meta.client import MetaAPIError, MetaClient
from app.social import posts
from app.util.redact import redact

POLL_SECONDS = 5
POLL_LIMIT = 60  # 5 minutes for a video to be processed
STUCK_AFTER = timedelta(minutes=30)  # longer than any real publish (video processing waits up to 5 min)
MISSED_AFTER = timedelta(hours=6)  # Milo was off at the approved time: don't post a stale offer by surprise
log = logging.getLogger("milo.posts")


class PublishError(RuntimeError):
    pass


async def _wait_ready(client: MetaClient, container: str, token: str, sleep=asyncio.sleep) -> None:
    for _ in range(POLL_LIMIT):
        status = (await client.get(f"/{container}", token=token, fields="status_code,status")) or {}
        code = status.get("status_code", "FINISHED")
        if code in ("FINISHED", "PUBLISHED"):
            return
        if code in ("ERROR", "EXPIRED"):
            raise PublishError(f"Instagram couldn't process the media ({code}): {status.get('status', '')}"[:300])
        await sleep(POLL_SECONDS)
    raise PublishError("Instagram took longer than 5 minutes to process the video. Try again later.")


async def publish(post_id: int, client: MetaClient | None = None, sleep=asyncio.sleep) -> SocialPost:
    """Publish now. The post must already be approved (status scheduled or pending_approval -> publishing).
    LinkedIn posts are handed to app/social/linkedin.py (same approval gate, same statuses)."""
    current = store.get(SocialPost, post_id)
    if current is not None and current.platform == "linkedin":
        from app.social import linkedin

        return await linkedin.publish(post_id, sleep=sleep)
    p = store.claim(SocialPost, post_id, {"status": {"$in": ["pending_approval", "scheduled"]}},
                    {"status": "publishing", "error": "", "publishing_started_at": now()})
    if p is None:
        current = store.get(SocialPost, post_id)
        raise PublishError(f"Post is {current.status if current else 'missing'}; it can't be published now.")
    if p.meta_mode == "live" and p.meta_media_id:  # already on Instagram (an earlier run published it): never twice
        store.audit("post_publish_skipped", post_id=p.id, reason="already on Instagram", media_id=p.meta_media_id)
        return store.update(SocialPost, p.id, {"status": "published", "published_at": p.published_at or now(),
                                               "error": ""})
    own = client is None
    client = client or MetaClient()
    s = client.s
    try:
        if s.meta_dry_run:
            store.audit("post_published", post_id=p.id, kind=p.kind, mode="dry_run")
            return store.update(SocialPost, p.id, {"status": "published", "meta_mode": "dry_run",
                                                   "meta_media_id": f"dry_{uuid.uuid4().hex[:10]}",
                                                   "published_at": now()})
        ig = await meta.instagram_user_id(client)
        token = await inbox._page_token(client, s.meta_page_id)
        base = f"{s.public_base_url.rstrip('/')}/media/{p.media_token}"
        files = posts.publish_files(p)
        images = [f"{base}/{f.name}" for f in files if f.suffix == ".jpg"]

        async def container(**params) -> str:
            resp = await client.post(f"/{ig}/media", token=token, **params)
            if not resp.get("id"):
                raise PublishError("Instagram didn't return a media container id.")
            return str(resp["id"])

        if p.kind == "photo":
            creation = await container(image_url=images[0], caption=p.caption)
        elif p.kind == "story":
            creation = await container(image_url=images[0], media_type="STORIES")
        elif p.kind == "reel":
            creation = await container(video_url=f"{base}/reel.mp4", media_type="REELS", caption=p.caption,
                                       share_to_feed="true", cover_url=images[0])
        else:  # carousel
            children = [await container(image_url=u, is_carousel_item="true") for u in images]
            for child in children:
                await _wait_ready(client, child, token, sleep)
            creation = await container(media_type="CAROUSEL", children=",".join(children), caption=p.caption)
        await _wait_ready(client, creation, token, sleep)
        published = await client.post(f"/{ig}/media_publish", token=token, creation_id=creation)
        media_id = str(published.get("id") or "")
        if media_id:  # it is live now: save the id first, so no failure below can lead to a second publish
            store.update(SocialPost, p.id, {"meta_media_id": media_id, "meta_mode": "live"})
        link = ""
        if media_id:
            try:
                link = (await client.get(f"/{media_id}", token=token, fields="permalink")).get("permalink", "")
            except MetaAPIError:
                link = ""  # Stories may have no permalink; the post is published anyway
        store.audit("post_published", post_id=p.id, kind=p.kind, mode="live", media_id=media_id)
        return store.update(SocialPost, p.id, {"status": "published", "meta_mode": "live", "meta_media_id": media_id,
                                               "permalink": link, "published_at": now()})
    except Exception as exc:
        note = f"{type(exc).__name__}: {redact(exc)}"[:300]
        try:
            current = store.get(SocialPost, p.id)
            if current and current.meta_mode == "live" and current.meta_media_id:  # live on Instagram already
                store.update(SocialPost, p.id, {"status": "published", "published_at": current.published_at or now(),
                                                "error": f"Posted, but Milo hit an error afterwards: {note}"[:300]})
                store.audit("post_published", post_id=p.id, kind=p.kind, mode="live", media_id=current.meta_media_id,
                            note="saved after an error")
                return store.get(SocialPost, p.id)  # it is live: report success, never invite a retry
            else:
                store.update(SocialPost, p.id, {"status": "failed", "error": note})
                store.audit("post_failed", post_id=p.id, error=note)
        except Exception:  # noqa: BLE001 - e.g. the database is down; fail_stuck_posts cleans up later
            log.exception("could not record the result of post %s", p.id)
        raise
    finally:
        if own:
            await client.aclose()


def fail_stuck_posts(older_than: timedelta = STUCK_AFTER) -> list[int]:
    """Posts left in "publishing" (Milo stopped or crashed mid-publish) can't be retried, edited or deleted.
    Move them on: "published" if Instagram returned a media id, otherwise "failed" with a note to check Instagram
    first, since the post may already be live. Runs at startup (older_than=0) and every few minutes."""
    done = []
    cutoff = now() - older_than
    for p in store.find(SocialPost, {"status": "publishing"}):
        started = p.publishing_started_at
        if started is not None and started > cutoff:
            continue  # still running
        where = "LinkedIn" if p.platform == "linkedin" else "Instagram"
        if p.meta_mode == "live" and (p.meta_media_id or p.linkedin_post_urn):
            changes = {"status": "published", "published_at": p.published_at or now(), "error": ""}
        else:
            changes = {"status": "failed", "error": f"Publishing was interrupted (Milo stopped or crashed). Check "
                                                     f"{where} before retrying: the post may already be live."}
        if store.claim(SocialPost, p.id, {"status": "publishing"}, changes):
            store.audit("post_recovered", post_id=p.id, status=changes["status"])
            done.append(p.id)
    return done


async def publish_due(sleep=asyncio.sleep) -> list[int]:
    """Scheduler: publish approved posts whose time has come. One failure never stops the others."""
    done = []
    for p in store.find(SocialPost, {"status": "scheduled"}):
        if not p.scheduled_at or p.scheduled_at > now():
            continue
        if now() - p.scheduled_at > MISSED_AFTER:
            note = (f"Missed: Milo wasn't running at the approved time ({p.scheduled_at:%Y-%m-%d %H:%M} UTC). "
                    "Pick a new time and request approval again.")
            if store.claim(SocialPost, p.id, {"status": "scheduled"}, {"status": "failed", "error": note}):
                store.audit("post_failed", post_id=p.id, error=note)
            continue
        try:
            await publish(p.id, sleep=sleep)
            done.append(p.id)
        except Exception:  # noqa: BLE001 - recorded on the post (status failed) and in Activity
            log.exception("scheduled post %s could not be published", p.id)
    return done

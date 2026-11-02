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
import uuid

from app.db import store
from app.db.models import SocialPost, now
from app.dm import inbox
from app.meta import campaigns as meta
from app.meta.client import MetaAPIError, MetaClient
from app.social import posts
from app.util.redact import redact

POLL_SECONDS = 5
POLL_LIMIT = 60  # 5 minutes for a video to be processed


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
    """Publish now. The post must already be approved (status scheduled or pending_approval -> publishing)."""
    p = store.claim(SocialPost, post_id, {"status": {"$in": ["pending_approval", "scheduled"]}},
                    {"status": "publishing", "error": ""})
    if p is None:
        current = store.get(SocialPost, post_id)
        raise PublishError(f"Post is {current.status if current else 'missing'}; it can't be published now.")
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
        store.update(SocialPost, p.id, {"status": "failed", "error": note})
        store.audit("post_failed", post_id=p.id, error=note)
        raise
    finally:
        if own:
            await client.aclose()


async def publish_due(sleep=asyncio.sleep) -> list[int]:
    """Scheduler: publish approved posts whose time has come. One failure never stops the others."""
    done = []
    for p in store.find(SocialPost, {"status": "scheduled"}):
        if p.scheduled_at and p.scheduled_at <= now():
            try:
                await publish(p.id, sleep=sleep)
                done.append(p.id)
            except Exception:  # noqa: BLE001 - recorded on the post (status failed) and in Activity
                pass
    return done

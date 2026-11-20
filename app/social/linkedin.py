"""Publishing an approved post to the LinkedIn Page (Posts API, Community Management API).

Runs only for an approved "publish_post" approval: from app/actions.py when the user approves, or from the
scheduler at the approved time (both go through app/social/instagram.publish, which hands LinkedIn posts here).
Flow (api.linkedin.com/rest, member token of a Page admin, w_organization_social):
1. per image: POST /images?action=initializeUpload (owner = the Page), PUT the JPEG to uploadUrl, then wait until
   GET /images/{urn} is AVAILABLE (a post whose image is not processed may never show to members);
2. POST /posts with the escaped text (little text format), PUBLIC visibility, MAIN_FEED, PUBLISHED;
   content.media for one image, content.multiImage for 2-20;
3. the new post's URN comes back in the x-restli-id header and is saved at once, so it is never posted twice.
"""
import asyncio
import logging
import re
from pathlib import Path

from app.db import store
from app.db.models import SocialPost, now
from app.linkedin.client import LinkedInClient, post_url
from app.util.redact import redact

log = logging.getLogger("milo.posts")

# little text format: these characters are markup and must be escaped to show as text, or the post can be cut
# off (or refused) at that point.
_RESERVED = re.compile(r"[\\|{}@\[\]()<>#*_~]")
_HASHTAG = re.compile(r"(?<![\w#\\])#(\w+)")


class PublishError(RuntimeError):
    pass


def escape_text(text: str) -> str:
    return _RESERVED.sub(lambda m: "\\" + m.group(0), text)


def to_little_text(text: str) -> str:
    """Post text as LinkedIn's little text: everything escaped, and each #word kept as a real hashtag."""
    out, last = [], 0
    for m in _HASHTAG.finditer(text):
        out.append(escape_text(text[last:m.start()]))
        out.append("{hashtag|\\#|" + escape_text(m.group(1)) + "}")
        last = m.end()
    out.append(escape_text(text[last:]))
    return "".join(out)


def build_body(p: SocialPost, author: str, image_urns: list[str]) -> dict:
    body = {
        "author": author,
        "commentary": to_little_text(p.caption),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    alt = p.alt_text or ""
    if len(image_urns) == 1:
        body["content"] = {"media": {"id": image_urns[0], **({"altText": alt} if alt else {})}}
    elif len(image_urns) > 1:
        body["content"] = {"multiImage": {"images": [{"id": u, **({"altText": alt} if alt else {})}
                                                     for u in image_urns]}}
    return body


async def publish(post_id: int, client: LinkedInClient | None = None, sleep=asyncio.sleep) -> SocialPost:
    """Publish now. The post must already be approved (status scheduled or pending_approval -> publishing)."""
    p = store.claim(SocialPost, post_id, {"status": {"$in": ["pending_approval", "scheduled"]}},
                    {"status": "publishing", "error": "", "publishing_started_at": now()})
    if p is None:
        current = store.get(SocialPost, post_id)
        raise PublishError(f"Post is {current.status if current else 'missing'}; it can't be published now.")
    if p.meta_mode == "live" and p.linkedin_post_urn:  # an earlier run already posted it: never twice
        store.audit("post_publish_skipped", post_id=p.id, reason="already on LinkedIn", urn=p.linkedin_post_urn)
        return store.update(SocialPost, p.id, {"status": "published", "published_at": p.published_at or now(),
                                               "error": ""})
    own = client is None
    client = client or LinkedInClient(sleep=sleep)
    mode = "dry_run" if client.dry_run else "live"
    try:
        author = client.organization_urn
        urns = []
        for path in p.images:
            urn = await client.upload_image(Path(path).read_bytes(), owner=author)
            await client.wait_image_available(urn)
            urns.append(urn)
        urn = await client.create_post(build_body(p, author, urns))
        # It is live now: save the URN first, so no failure below can lead to a second post.
        store.update(SocialPost, p.id, {"linkedin_post_urn": urn, "meta_mode": mode})
        store.audit("post_published", post_id=p.id, platform="linkedin", kind=p.kind, mode=mode, urn=urn)
        return store.update(SocialPost, p.id, {"status": "published", "meta_mode": mode, "linkedin_post_urn": urn,
                                               "permalink": post_url(urn), "published_at": now()})
    except Exception as exc:
        note = f"{type(exc).__name__}: {redact(exc)}"[:300]
        try:
            current = store.get(SocialPost, p.id)
            if current and current.linkedin_post_urn and current.meta_mode == "live":
                store.update(SocialPost, p.id, {"status": "published", "published_at": current.published_at or now(),
                                                "error": f"Posted, but Milo hit an error afterwards: {note}"[:300]})
                return store.get(SocialPost, p.id)
            store.update(SocialPost, p.id, {"status": "failed", "error": note})
            store.audit("post_failed", post_id=p.id, platform="linkedin", error=note)
        except Exception:
            log.exception("could not record the result of LinkedIn post %s", p.id)
        raise
    finally:
        if own:
            await client.aclose()

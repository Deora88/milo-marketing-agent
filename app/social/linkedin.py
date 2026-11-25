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
from app.linkedin.client import (
    LinkedInAPIError,
    LinkedInClient,
    PostMayBeLiveError,
    post_url,
)
from app.util.redact import redact

log = logging.getLogger("milo.posts")

# little text format: these characters are markup and must be escaped to show as text, or the post can be cut
# off (or refused) at that point.
_RESERVED = re.compile(r"[\\|{}@\[\]()<>#*_~]")
_HASHTAG = re.compile(r"(?<![\w#\\])#(\w+)")


class PublishError(RuntimeError):
    pass


MAYBE_LIVE = "Check LinkedIn before retrying: the post may already be live."


ANALYTICS = {"impressions": "IMPRESSION", "members_reached": "MEMBERS_REACHED", "reshares": "RESHARE"}
NEEDS_CMA = "it needs LinkedIn's Community Management API on the app"


async def fetch_results(p: SocialPost, client: LinkedInClient | None = None) -> SocialPost:
    """Read a published LinkedIn post's numbers and store them in p.insights. Never raises for LinkedIn errors:
    what LinkedIn refused is explained in insights_note.
    - reactions, comments: Social Metadata API;
    - impressions, members reached, reposts: Member Post Analytics API (posts on your own profile only;
      permission r_member_postAnalytics)."""
    if p.meta_mode != "live" or not p.linkedin_post_urn:
        return store.update(SocialPost, p.id, {"insights_note": "Dry run: this post isn't on LinkedIn, so there "
                                                                "are no results."})
    own = client is None
    client = client or LinkedInClient()
    if client.dry_run:
        if own:
            await client.aclose()
        return store.update(SocialPost, p.id, {"insights_at": now(),
                                               "insights_note": "LINKEDIN_DRY_RUN is on, so Milo doesn't call "
                                                                "LinkedIn for results."})
    found, notes, refused, asked = dict(p.insights), [], 0, 0
    try:
        try:
            asked += 1
            found.update(await client.social_metadata(p.linkedin_post_urn))
        except LinkedInAPIError as exc:
            if exc.status == 403:
                refused += 1
                notes.append(f"LinkedIn doesn't let this app read reactions and comments yet ({NEEDS_CMA}).")
            else:
                notes.append(f"Reactions/comments: {redact(exc)[:120]}")
        if client.posts_as_member:
            asked += 1
            for key, metric in ANALYTICS.items():
                try:
                    found[key] = await client.member_post_metric(p.linkedin_post_urn, metric)
                except LinkedInAPIError as exc:
                    if exc.status == 403:
                        refused += 1
                        notes.append("Impressions, reach and reposts need the r_member_postAnalytics permission "
                                     f"({NEEDS_CMA}). They show on the post on LinkedIn.")
                    else:
                        notes.append(f"Impressions: {redact(exc)[:120]}")
                    break  # same permission for all three: one note is enough
        else:
            notes.append("Milo doesn't read impressions for Page posts yet. They show on the post on LinkedIn.")
    finally:
        if own:
            await client.aclose()
    found = {k: v for k, v in found.items() if isinstance(v, (int, float))}
    blocked = refused == asked  # LinkedIn said no to everything: the app lacks the permission, not a glitch
    return store.update(SocialPost, p.id, {"insights": found, "insights_at": now(), "insights_note": " ".join(notes),
                                           "insights_blocked": blocked})


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
    creating = False  # True once POST /posts may have reached LinkedIn
    try:
        author = client.author_urn
        urns = []
        for path in p.images:
            urn = await client.upload_image(Path(path).read_bytes(), owner=author)
            await client.wait_image_available(urn)
            urns.append(urn)
        creating = True
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
            # A 4xx on create means LinkedIn refused it; anything else at or after create may have posted it.
            refused = isinstance(exc, LinkedInAPIError) and not isinstance(exc, PostMayBeLiveError) \
                and 400 <= exc.status < 500
            maybe_live = mode == "live" and creating and not refused
            error = f"{MAYBE_LIVE} {note}"[:300] if maybe_live else note
            store.update(SocialPost, p.id, {"status": "failed", "error": error})
            store.audit("post_failed", post_id=p.id, platform="linkedin", error=note, may_be_live=maybe_live)
        except Exception:
            log.exception("could not record the result of LinkedIn post %s", p.id)
        raise
    finally:
        if own:
            await client.aclose()

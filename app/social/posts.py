"""Organic posts for Instagram and the LinkedIn Page: drafts, images, checks, and the approval request.
Nothing here calls Meta or LinkedIn.

A post is published only after a person approves it (approval action "publish_post", executed by
app/actions.py, or by the scheduler at the approved time). Same gate as ads.
"""
import re
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings
from app.actor import current_actor
from app.db import store
from app.db.models import Approval, SocialPost, now
from app.social import media

PLATFORMS = {"instagram": "Instagram", "linkedin": "LinkedIn"}
KINDS = {"photo": "Photo", "carousel": "Carousel", "reel": "Reel", "story": "Story"}  # Instagram
MAX_IMAGES = {"photo": 1, "carousel": 10, "reel": 10, "story": 1}
MIN_IMAGES = {"photo": 1, "carousel": 2, "reel": 1, "story": 1}
MAX_CAPTION, MAX_HASHTAGS, MAX_MENTIONS = 2200, 30, 20
# LinkedIn Page posts (Posts API): text only, one image, or a MultiImage post (2-20 images, organic only)
LINKEDIN_KINDS = {"text": "Text", "photo": "Photo", "multi_image": "Multi-image"}
LINKEDIN_MAX_IMAGES = {"text": 0, "photo": 1, "multi_image": 20}
LINKEDIN_MIN_IMAGES = {"text": 0, "photo": 1, "multi_image": 2}
LINKEDIN_MAX_TEXT, MAX_ALT_TEXT = 3000, 300
# Switching platform on a draft keeps the closest post type
TO_LINKEDIN = {"photo": "photo", "carousel": "multi_image", "reel": "multi_image", "story": "photo"}
TO_INSTAGRAM = {"text": "photo", "photo": "photo", "multi_image": "carousel"}
EDITABLE = {"draft", "failed"}


def kinds_for(platform: str) -> dict:
    return LINKEDIN_KINDS if platform == "linkedin" else KINDS


def max_images(platform: str, kind: str) -> int:
    return (LINKEDIN_MAX_IMAGES if platform == "linkedin" else MAX_IMAGES).get(kind, 1)


def min_images(platform: str, kind: str) -> int:
    return (LINKEDIN_MIN_IMAGES if platform == "linkedin" else MIN_IMAGES).get(kind, 1)


def kind_label(p: SocialPost) -> str:
    return kinds_for(p.platform).get(p.kind, p.kind)


def _check_platform_kind(platform: str, kind: str) -> None:
    if platform not in PLATFORMS:
        raise PostError(f"Platform must be one of: {', '.join(PLATFORMS)}.")
    if kind not in kinds_for(platform):
        prefix = "LinkedIn post type" if platform == "linkedin" else "Post type"
        raise PostError(f"{prefix} must be one of: {', '.join(kinds_for(platform))}.")


class PostError(ValueError):
    """A request Milo refuses; the message is shown to the user (or the agent)."""


def _dir(post_id: int) -> Path:
    return media.POST_DIR / str(post_id)


def get(post_id: int) -> SocialPost:
    p = store.get(SocialPost, post_id)
    if p is None:
        raise PostError("Post not found.")
    return p


def _editable(p: SocialPost) -> None:
    if p.status not in EDITABLE:
        raise PostError(f"This post is {p.status.replace('_', ' ')}; it can't be changed now.")


def caption_problems(kind: str, caption: str, platform: str = "instagram") -> list[str]:
    out = []
    if platform == "linkedin":
        if len(caption) > LINKEDIN_MAX_TEXT:
            out.append(f"Text is {len(caption)} characters; LinkedIn allows {LINKEDIN_MAX_TEXT}.")
        return out
    if len(caption) > MAX_CAPTION:
        out.append(f"Caption is {len(caption)} characters; Instagram allows {MAX_CAPTION}.")
    if len(re.findall(r"#\w+", caption)) > MAX_HASHTAGS:
        out.append(f"More than {MAX_HASHTAGS} hashtags.")
    if len(re.findall(r"@[\w.]+", caption)) > MAX_MENTIONS:
        out.append(f"More than {MAX_MENTIONS} @mentions.")
    return out


def _parse_when(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        when = value
    else:
        try:
            when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise PostError("Schedule time must look like 2026-11-05T18:30 (with a timezone, e.g. +05:30).") from exc
    if when.tzinfo is None:
        raise PostError("Schedule time needs a timezone, e.g. 2026-11-05T18:30+05:30.")
    when = when.astimezone(timezone.utc)
    if when < now() + timedelta(minutes=5):
        raise PostError("Schedule time must be at least 5 minutes from now (or leave it empty to post on approval).")
    if when > now() + timedelta(days=60):
        raise PostError("Schedule time can be at most 60 days ahead.")
    return when


def clean_overlay(overlay: dict | None) -> dict:
    """Story/Reel text: headline (60), details (90), cta (30); empty fields dropped."""
    out = {}
    for key, limit in media.OVERLAY_LIMITS.items():
        text = " ".join(str((overlay or {}).get(key) or "").split())
        if len(text) > limit:
            raise PostError(f"Text on image: {key} can be at most {limit} characters.")
        if text:
            out[key] = text
    return out


def _clean_alt(alt_text: str | None) -> str:
    alt = " ".join(str(alt_text or "").split())
    if len(alt) > MAX_ALT_TEXT:
        raise PostError(f"Image description can be at most {MAX_ALT_TEXT} characters.")
    return alt


def create(kind: str, caption: str = "", scheduled_at=None, source: str = "user",
           overlay: dict | None = None, platform: str = "instagram", alt_text: str = "") -> SocialPost:
    _check_platform_kind(platform, kind)
    caption = (caption or "").strip()
    problems = caption_problems(kind, caption, platform)
    if problems:
        raise PostError(" ".join(problems))
    linkedin = platform == "linkedin"
    p = store.insert(SocialPost(platform=platform, kind=kind, caption="" if kind == "story" else caption,
                                scheduled_at=_parse_when(scheduled_at), source=source,
                                overlay={} if linkedin else clean_overlay(overlay),
                                alt_text=_clean_alt(alt_text) if linkedin else ""))
    store.audit("post_drafted", post_id=p.id, kind=kind, platform=platform, source=source)
    return p


def update(post_id: int, kind: str | None = None, caption: str | None = None, scheduled_at="__keep__",
           overlay: dict | None = None, platform: str | None = None, alt_text: str | None = None) -> SocialPost:
    p = get(post_id)
    _editable(p)
    changes: dict = {}
    plat = platform or p.platform
    if plat not in PLATFORMS:
        raise PostError(f"Platform must be one of: {', '.join(PLATFORMS)}.")
    if plat != p.platform:
        changes["platform"] = plat
        if kind is None:  # keep the closest type on the other platform
            kind = (TO_LINKEDIN if plat == "linkedin" else TO_INSTAGRAM).get(p.kind, "photo")
            if plat == "linkedin" and kind == "photo" and not p.images:
                kind = "text"
            if plat == "linkedin" and kind == "multi_image" and len(p.images) < 2:
                kind = "photo" if p.images else "text"
        if plat == "linkedin":
            changes["overlay"] = {}
    if kind is not None:
        _check_platform_kind(plat, kind)
        if len(p.images) > max_images(plat, kind):
            raise PostError(f"A {PLATFORMS[plat]} {kinds_for(plat)[kind]} post can have at most "
                            f"{max_images(plat, kind)} image(s); remove some first.")
        changes["kind"] = kind
    k = changes.get("kind", p.kind)
    if caption is not None:
        caption = caption.strip()
        changes["caption"] = caption
    problems = caption_problems(k, changes.get("caption", p.caption), plat)
    if problems:
        raise PostError(" ".join(problems))
    if k == "story":
        changes["caption"] = ""
    if alt_text is not None and plat == "linkedin":
        changes["alt_text"] = _clean_alt(alt_text)
    if scheduled_at != "__keep__":
        changes["scheduled_at"] = _parse_when(scheduled_at)
    if overlay is not None and plat == "instagram":
        changes["overlay"] = clean_overlay(overlay)
    changes.update(status="draft", error="")
    return store.update(SocialPost, p.id, changes)


def add_image(post_id: int, data: bytes) -> SocialPost:
    """Store an image (uploaded or AI-made) as a normalised JPEG. Formatting per post type happens at publish."""
    p = get(post_id)
    _editable(p)
    limit = max_images(p.platform, p.kind)
    if len(p.images) >= limit:
        if p.platform == "linkedin" and p.kind == "text":
            raise PostError("A LinkedIn text post has no images. Switch the type to Photo or Multi-image first.")
        raise PostError(f"A {kind_label(p)} can have at most {limit} image(s).")
    if p.platform == "linkedin":  # LinkedIn shows any aspect ratio: keep the picture uncropped
        img = media.normalised_jpeg(data, max_side=1920)
    else:
        img = media.feed_jpeg(data) if p.kind in ("photo", "carousel") else media.normalised_jpeg(data)
    d = _dir(p.id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"img-{secrets.token_hex(4)}.jpg"
    path.write_bytes(img)
    return store.update(SocialPost, p.id, {"images": [*p.images, str(path)], "status": "draft", "error": ""})


def remove_image(post_id: int, index: int) -> SocialPost:
    p = get(post_id)
    _editable(p)
    if not 0 <= index < len(p.images):
        raise PostError("No such image.")
    Path(p.images[index]).unlink(missing_ok=True)
    return store.update(SocialPost, p.id, {"images": [x for i, x in enumerate(p.images) if i != index]})


def delete(post_id: int) -> None:
    p = get(post_id)
    if p.status in ("pending_approval", "scheduled", "publishing"):
        raise PostError("Reject or wait for the pending approval first.")
    shutil.rmtree(_dir(p.id), ignore_errors=True)
    store.db()[SocialPost.collection].delete_one({"_id": p.id})
    store.audit("post_deleted", post_id=post_id)


def publish_files(p: SocialPost) -> list[Path]:
    """The files Meta will download, in order: JPEGs per image, plus reel.mp4 for Reels."""
    d = _dir(p.id)
    return sorted(d.glob("pub-*.jpg")) + ([d / "reel.mp4"] if p.kind == "reel" else [])


def prepare_media(p: SocialPost) -> SocialPost:
    """Make the exact files that will be published (Story/Reel frames in 9:16, the Reel video)."""
    d = _dir(p.id)
    for old in d.glob("pub-*.jpg"):
        old.unlink()
    (d / "reel.mp4").unlink(missing_ok=True)
    frames = []
    for i, src in enumerate(p.images):
        data = Path(src).read_bytes()
        out = media.feed_jpeg(data) if p.kind in ("photo", "carousel") else media.vertical_frame(data, p.overlay)
        (d / f"pub-{i:02d}.jpg").write_bytes(out)
        frames.append(out)
    video = ""
    if p.kind == "reel":
        video = str(media.build_reel(frames, d / "reel.mp4"))
    return store.update(SocialPost, p.id, {"video_path": video, "media_token": secrets.token_urlsafe(24)})


def describe(p: SocialPost) -> str:
    """The approval card text: exactly what will be posted, where and when."""
    n = len(p.images)
    if p.platform == "linkedin":
        what = {"text": "a text post", "photo": "a post with 1 image",
                "multi_image": f"a post with {n} images"}[p.kind]
        when = (f"at {p.scheduled_at.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC" if p.scheduled_at
                else "right after approval")
        text = f' Text: "{p.caption[:180]}{"…" if len(p.caption) > 180 else ""}"' if p.caption else ""
        where = ("your own LinkedIn profile" if get_settings().linkedin_author.strip().lower() == "me"
                 else "your LinkedIn Page")
        return f"Post {what} to {where} (public) {when}.{text}"
    what = {"photo": "a photo", "carousel": f"a carousel of {n} images",
            "reel": f"a Reel ({n} image(s), {max(3, n * media.SECONDS_PER_IMAGE)} s video)",
            "story": "a Story (disappears after 24 hours)"}[p.kind]
    when = (f"at {p.scheduled_at.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC" if p.scheduled_at
            else "right after approval")
    cap = f' Caption: "{p.caption[:180]}{"…" if len(p.caption) > 180 else ""}"' if p.caption else ""
    shown = " · ".join(p.overlay.get(k, "") for k in ("headline", "details", "cta") if p.overlay.get(k))
    text = f' Text on the image: "{shown}".' if shown and p.kind in ("story", "reel") else ""
    return f"Post {what} to your Instagram account {when}.{cap}{text}"


def request_publish(post_id: int, source: str = "user") -> dict:
    """Checks the post and creates a pending approval. Never publishes."""
    p = get(post_id)
    existing = store.find(Approval, {"post_id": p.id, "status": {"$in": ["pending", "executing"]}})
    if existing:
        return {"status": "pending_approval", "approval_id": existing[0].id, "summary": existing[0].summary,
                "message": "An approval for this post is already waiting for the user."}
    _editable(p)
    s = get_settings()
    need = min_images(p.platform, p.kind)
    if len(p.images) < need:
        raise PostError(f"A {kind_label(p)} needs at least {need} image{'s' if need > 1 else ''}. "
                        "Upload one or generate it with AI.")
    if len(p.images) > max_images(p.platform, p.kind):
        raise PostError(f"A {kind_label(p)} can have at most {max_images(p.platform, p.kind)} image(s).")
    problems = caption_problems(p.kind, p.caption, p.platform)
    if problems:
        raise PostError(" ".join(problems))
    if p.platform == "linkedin" and not p.caption:
        raise PostError("Write the post text first: LinkedIn posts need text.")
    if p.scheduled_at and p.scheduled_at < now() + timedelta(minutes=2):
        raise PostError("The schedule time has passed. Pick a new time or clear it.")
    warning = ""
    if p.platform == "linkedin":
        as_me = s.linkedin_author.strip().lower() == "me"
        who, who_id = (("LINKEDIN_PERSON_ID", s.linkedin_person_id) if as_me
                       else ("LINKEDIN_ORGANIZATION_ID", s.linkedin_organization_id))
        if not s.linkedin_dry_run and not (s.linkedin_access_token and who_id):
            raise PostError(f"Set LINKEDIN_ACCESS_TOKEN and {who} in .env (scripts/linkedin_token.py prints both), "
                            "or keep LINKEDIN_DRY_RUN=true to try it without posting.")
        if not s.linkedin_dry_run:
            warning = _linkedin_token_check(s.linkedin_token_expires_at, p.scheduled_at)
    elif not s.meta_dry_run and not s.public_base_url.startswith("https://"):
        raise PostError("Set PUBLIC_BASE_URL in .env to your public https address (your ngrok domain): Meta "
                        "downloads the post's images and video from there.")
    if p.platform == "instagram":  # LinkedIn gets the stored JPEGs uploaded directly at publish time
        try:
            p = prepare_media(p)
        except media.MediaError as exc:
            raise PostError(str(exc)) from exc
    a = store.insert(Approval(action="publish_post", post_id=p.id, summary=describe(p), source=source,
                              requested_by=current_actor()))
    store.update(SocialPost, p.id, {"status": "pending_approval", "error": ""})
    store.audit("approval_requested", approval_id=a.id, action="publish_post", post_id=p.id, source=source)
    out = {"status": "pending_approval", "approval_id": a.id, "summary": a.summary,
           "message": "Not posted. The user must click Approve in the Milo dashboard."}
    if warning:
        out["warning"] = warning
        out["message"] += " " + warning
    return out


TOKEN_WARN_DAYS = 7


def _linkedin_token_check(expires_at: str, scheduled_at: datetime | None) -> str:
    """Refuse a LinkedIn post whose token has expired (or will have expired by the scheduled time), so it fails
    here instead of with a 401 after approval. Returns a warning when the token has under TOKEN_WARN_DAYS left.
    LINKEDIN_TOKEN_EXPIRES_AT is optional: without it there is nothing to check."""
    if not expires_at:
        return ""
    try:
        expires = datetime.fromisoformat(expires_at[:10]).replace(tzinfo=timezone.utc)  # as the status page
    except ValueError:
        return ""
    fix = "Make a new one with scripts/linkedin_token.py and update .env."
    if expires <= now():
        raise PostError(f"The LinkedIn token expired on {expires_at[:10]}. {fix}")
    if scheduled_at and scheduled_at >= expires:
        raise PostError(f"The LinkedIn token expires on {expires_at[:10]}, before the scheduled time. {fix}")
    days = (expires - now()).days
    if days < TOKEN_WARN_DAYS:
        return f"The LinkedIn token expires in {days} day{'s' if days != 1 else ''} ({expires_at[:10]}). {fix}"
    return ""


def media_file(token: str, name: str) -> Path | None:
    """The file behind a public media URL, only for posts waiting to be (or being) published."""
    if not token or len(token) < 20:
        return None
    rows = store.find(SocialPost, {"media_token": token}, limit=1)
    if not rows or rows[0].status not in ("pending_approval", "scheduled", "publishing", "failed"):
        return None
    for f in publish_files(rows[0]):
        if f.name == name and f.is_file():
            return f
    return None

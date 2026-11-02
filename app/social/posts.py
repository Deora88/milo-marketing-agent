"""Organic Instagram posts: drafts, images, checks, and the approval request. Nothing here calls Meta.

A post is published only after a person approves it (approval action "publish_post", executed by
app/actions.py, or by the scheduler at the approved time). Same gate as ads.
"""
import re
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings
from app.db import store
from app.db.models import Approval, SocialPost, now
from app.social import media

KINDS = {"photo": "Photo", "carousel": "Carousel", "reel": "Reel", "story": "Story"}
MAX_IMAGES = {"photo": 1, "carousel": 10, "reel": 10, "story": 1}
MIN_IMAGES = {"photo": 1, "carousel": 2, "reel": 1, "story": 1}
MAX_CAPTION, MAX_HASHTAGS, MAX_MENTIONS = 2200, 30, 20
EDITABLE = {"draft", "failed"}


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


def caption_problems(kind: str, caption: str) -> list[str]:
    out = []
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


def create(kind: str, caption: str = "", scheduled_at=None, source: str = "user",
           overlay: dict | None = None) -> SocialPost:
    if kind not in KINDS:
        raise PostError(f"Post type must be one of: {', '.join(KINDS)}.")
    caption = (caption or "").strip()
    problems = caption_problems(kind, caption)
    if problems:
        raise PostError(" ".join(problems))
    p = store.insert(SocialPost(kind=kind, caption="" if kind == "story" else caption,
                                scheduled_at=_parse_when(scheduled_at), source=source,
                                overlay=clean_overlay(overlay)))
    store.audit("post_drafted", post_id=p.id, kind=kind, source=source)
    return p


def update(post_id: int, kind: str | None = None, caption: str | None = None, scheduled_at="__keep__",
           overlay: dict | None = None) -> SocialPost:
    p = get(post_id)
    _editable(p)
    changes: dict = {}
    if kind is not None:
        if kind not in KINDS:
            raise PostError(f"Post type must be one of: {', '.join(KINDS)}.")
        if len(p.images) > MAX_IMAGES[kind]:
            raise PostError(f"A {KINDS[kind]} can have at most {MAX_IMAGES[kind]} image(s); remove some first.")
        changes["kind"] = kind
    k = changes.get("kind", p.kind)
    if caption is not None:
        caption = caption.strip()
        problems = caption_problems(k, caption)
        if problems:
            raise PostError(" ".join(problems))
        changes["caption"] = caption
    if k == "story":
        changes["caption"] = ""
    if scheduled_at != "__keep__":
        changes["scheduled_at"] = _parse_when(scheduled_at)
    if overlay is not None:
        changes["overlay"] = clean_overlay(overlay)
    changes.update(status="draft", error="")
    return store.update(SocialPost, p.id, changes)


def add_image(post_id: int, data: bytes) -> SocialPost:
    """Store an image (uploaded or AI-made) as a normalised JPEG. Formatting per post type happens at publish."""
    p = get(post_id)
    _editable(p)
    if len(p.images) >= MAX_IMAGES[p.kind]:
        raise PostError(f"A {KINDS[p.kind]} can have at most {MAX_IMAGES[p.kind]} image(s).")
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
    if len(p.images) < MIN_IMAGES[p.kind]:
        need = MIN_IMAGES[p.kind]
        raise PostError(f"A {KINDS[p.kind]} needs at least {need} image{'s' if need > 1 else ''}. "
                        "Upload one or generate it with AI.")
    problems = caption_problems(p.kind, p.caption)
    if problems:
        raise PostError(" ".join(problems))
    if p.scheduled_at and p.scheduled_at < now() + timedelta(minutes=2):
        raise PostError("The schedule time has passed. Pick a new time or clear it.")
    if not s.meta_dry_run and not s.public_base_url.startswith("https://"):
        raise PostError("Set PUBLIC_BASE_URL in .env to your public https address (your ngrok domain): Meta "
                        "downloads the post's images and video from there.")
    try:
        p = prepare_media(p)
    except media.MediaError as exc:
        raise PostError(str(exc)) from exc
    a = store.insert(Approval(action="publish_post", post_id=p.id, summary=describe(p), source=source))
    store.update(SocialPost, p.id, {"status": "pending_approval", "error": ""})
    store.audit("approval_requested", approval_id=a.id, action="publish_post", post_id=p.id, source=source)
    return {"status": "pending_approval", "approval_id": a.id, "summary": a.summary,
            "message": "Not posted. The user must click Approve in the Milo dashboard."}


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

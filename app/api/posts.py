"""Instagram posts (organic): drafts, images, approval requests (localhost only, like every /api route),
plus the one public route Meta downloads post media from (/media/<token>/<file>)."""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.db import store
from app.db.models import Approval, SocialPost
from app.social import posts
from app.util.redact import redact

router = APIRouter(prefix="/api/posts", tags=["instagram posts"])
public = APIRouter(tags=["instagram posts"])


def _out(p: SocialPost) -> dict:
    pending = store.find(Approval, {"post_id": p.id, "status": {"$in": ["pending", "executing"]}}, limit=1)
    d = p.model_dump(exclude={"images", "video_path", "media_token"})
    d.update(image_count=len(p.images), has_video=bool(p.video_path),
             approval_id=pending[0].id if pending else None, kind_label=posts.KINDS.get(p.kind, p.kind))
    return d


def _call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except posts.PostError as exc:
        raise HTTPException(409 if "not found" not in str(exc) else 404, str(exc)) from exc


class StoryText(BaseModel):
    """Text drawn onto Story/Reel images (Instagram's API can't add text or stickers)."""
    headline: str = Field("", max_length=200)
    details: str = Field("", max_length=300)
    cta: str = Field("", max_length=100)


class PostIn(BaseModel):
    kind: str = "photo"
    caption: str = Field("", max_length=5000)
    scheduled_at: Optional[str] = None  # ISO time with timezone, e.g. 2026-11-05T18:30+05:30
    overlay: Optional[StoryText] = None


class PostPatch(BaseModel):
    kind: Optional[str] = None
    caption: Optional[str] = Field(None, max_length=5000)
    scheduled_at: Optional[str] = None  # send null to post right after approval
    overlay: Optional[StoryText] = None


@router.get("")
def list_posts():
    return [_out(p) for p in store.find(SocialPost, sort=[("_id", -1)])]


@router.post("")
def create_post(body: PostIn):
    return _out(_call(posts.create, body.kind, body.caption, body.scheduled_at,
                      overlay=body.overlay.model_dump() if body.overlay else None))


@router.get("/{post_id}")
def get_post(post_id: int):
    return _out(_call(posts.get, post_id))


@router.patch("/{post_id}")
def update_post(post_id: int, body: PostPatch):
    sched = body.scheduled_at if "scheduled_at" in body.model_fields_set else "__keep__"
    return _out(_call(posts.update, post_id, body.kind, body.caption, sched,
                      body.overlay.model_dump() if body.overlay else None))


@router.delete("/{post_id}")
def delete_post(post_id: int):
    _call(posts.delete, post_id)
    return {"deleted": post_id}


@router.post("/{post_id}/images")
async def upload_image(post_id: int, file: UploadFile):
    """JPG or PNG, max 8 MB. Stored as JPEG; sized for the post type when publish is requested."""
    data = await file.read(posts.media.MAX_UPLOAD + 1)
    try:
        return _out(_call(posts.add_image, post_id, data))
    except posts.media.MediaError as exc:
        raise HTTPException(415, str(exc)) from exc


class GenerateIn(BaseModel):
    prompt: str = Field(min_length=3, max_length=600)


@router.post("/{post_id}/generate-image")
async def generate_image(post_id: int, body: GenerateIn):
    """AI image (FLUX on Cloudflare Workers AI), added to the post. Nothing is sent to Instagram."""
    return _out(await generate_for_post(post_id, body.prompt, source="user"))


async def generate_for_post(post_id: int, prompt: str, source: str):
    from app.creative.imagegen import ImageGenUnavailable, build_prompt
    from app.creative.imagegen import generate_image as flux

    p = _call(posts.get, post_id)
    _call(posts._editable, p)
    try:
        data = await flux(build_prompt(prompt))
    except ImageGenUnavailable as exc:
        raise HTTPException(503, redact(exc)) from exc
    p = _call(posts.add_image, post_id, data)
    store.audit("image_generated", post_id=post_id, source=source, prompt=prompt[:300], bytes=len(data))
    return p


@router.delete("/{post_id}/images/{index}")
def remove_image(post_id: int, index: int):
    return _out(_call(posts.remove_image, post_id, index))


@router.get("/{post_id}/images/{index}")
def get_image(post_id: int, index: int):
    p = _call(posts.get, post_id)
    if not 0 <= index < len(p.images):
        raise HTTPException(404, "no such image")
    return FileResponse(p.images[index], media_type="image/jpeg")


@router.get("/{post_id}/preview/{index}")
def preview_frame(post_id: int, index: int):
    """Story/Reel: the exact 9:16 frame that will be posted (with the text on it). Nothing is saved."""
    from fastapi.responses import Response

    p = _call(posts.get, post_id)
    if not 0 <= index < len(p.images):
        raise HTTPException(404, "no such image")
    data = posts.media.vertical_frame(Path(p.images[index]).read_bytes(), p.overlay)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/{post_id}/video")
def get_video(post_id: int):
    p = _call(posts.get, post_id)
    if not p.video_path:
        raise HTTPException(404, "The Reel video is made when you request publishing.")
    return FileResponse(p.video_path, media_type="video/mp4")


@router.post("/{post_id}/request")
def request_publish(post_id: int):
    """Like the agent's request tools: only creates a pending approval. Nothing is posted until Approve."""
    return _call(posts.request_publish, post_id, source="user")


@public.api_route("/media/{token}/{name}", methods=["GET", "HEAD"], include_in_schema=False)
def public_media(token: str, name: str):
    """Meta downloads post media from here (through PUBLIC_BASE_URL). Only files of a post that is waiting to be
    published, behind a random token; everything else is 404. Reachable from the internet like /webhooks/."""
    f = posts.media_file(token, name)
    if f is None:
        raise HTTPException(404, "not found")
    return FileResponse(f, media_type="video/mp4" if f.suffix == ".mp4" else "image/jpeg",
                        headers={"Cache-Control": "no-store"})

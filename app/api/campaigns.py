import re
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.approvals import create_approval, to_minor
from app.db import store
from app.db.models import Approval, Campaign, MetricSnapshot
from app.meta import campaigns as meta
from app.meta.client import MetaAPIError, MetaClient
from app.util.redact import redact

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])
IMAGE_DIR = Path("data/ad_images")
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _get(campaign_id: int) -> Campaign:
    c = store.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    return c


@router.get("")
def list_campaigns(status: Optional[str] = None, q: Optional[str] = None):
    filt: dict = {}
    if status and status != "all":
        filt["status"] = status
    if q:
        filt["name"] = {"$regex": re.escape(q), "$options": "i"}
    return store.find(Campaign, filt, sort=[("_id", -1)])


@router.get("/{campaign_id}")
def get_campaign(campaign_id: int):
    return _get(campaign_id)


@router.get("/{campaign_id}/metrics")
def campaign_metrics(campaign_id: int):
    from app.optimizer import window_start

    rows = store.find(MetricSnapshot, {"campaign_id": campaign_id, "date": {"$gte": window_start()}}, sort=[("date", 1)])
    return {"source": rows[-1].source if rows else None, "daily": rows}


@router.post("/{campaign_id}/image")
async def upload_image(campaign_id: int, file: UploadFile):
    """Ad image (JPG/PNG, max 8 MB). Required for Instagram. Sent to Meta when the campaign is published."""
    c = _get(campaign_id)
    if c.meta_creative_id:
        raise HTTPException(409, "The ad is already created on Meta; make a new campaign to change the image.")
    ext = IMAGE_TYPES.get(file.content_type or "")
    if not ext:
        raise HTTPException(415, "Use a JPG or PNG image.")
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image is larger than 8 MB.")
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGE_DIR / f"campaign-{c.id}{ext}"
    path.write_bytes(data)
    c.image_path, c.meta_image_hash = str(path), None
    store.save(c)
    store.audit("image_uploaded", campaign_id=c.id, bytes=len(data))
    return c


class GenerateImage(BaseModel):
    prompt: str = ""


@router.post("/{campaign_id}/generate-image")
async def generate_image(campaign_id: int, body: GenerateImage):
    """AI ad image (FLUX on Cloudflare Workers AI). Draft-only: nothing is sent to Meta until publish is approved."""
    from app.creative.imagegen import ImageGenUnavailable, generate_for_campaign

    _get(campaign_id)
    try:
        return await generate_for_campaign(campaign_id, body.prompt, source="user")
    except ImageGenUnavailable as exc:
        raise HTTPException(503, redact(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{campaign_id}/image")
def get_image(campaign_id: int):
    c = _get(campaign_id)
    if not c.image_path or not Path(c.image_path).is_file():
        raise HTTPException(404, "no image")
    return FileResponse(c.image_path)


@router.get("/{campaign_id}/preview")
async def campaign_preview(campaign_id: int, format: str = "MOBILE_FEED_STANDARD"):
    """How Meta renders this campaign's ad. Read-only, so it needs no approval."""
    c = _get(campaign_id)
    if format not in meta.PREVIEW_FORMATS:
        raise HTTPException(400, f"format must be one of {sorted(meta.PREVIEW_FORMATS)}")
    result = {"format": format, "formats": meta.PREVIEW_FORMATS, "available": False, "src": "", "reason": "",
              "source": ""}
    client = MetaClient()
    try:
        if client.s.meta_dry_run:
            result["reason"] = "Dry run is on, so Milo does not contact Meta. Set META_DRY_RUN=false for a real preview."
        elif c.meta_ad_id and c.meta_mode == "live":
            src = await meta.get_preview_src(client, c.meta_ad_id, format)
            result.update(available=bool(src), src=src, source="ad",
                          reason="" if src else "Meta returned no preview for this format.")
        else:
            # Not on Meta yet: Meta renders the draft without creating anything.
            src = await meta.generate_preview_src(client, c, format)
            result.update(available=bool(src), src=src, source="draft",
                          reason="" if src else "Meta returned no preview for this format.")
    except (MetaAPIError, ValueError) as exc:
        result["reason"] = f"Meta could not render a preview: {redact(exc)}"
    finally:
        await client.aclose()
    return result


@router.get("/{campaign_id}/approvals")
def campaign_approvals(campaign_id: int):
    return store.find(Approval, {"campaign_id": campaign_id}, sort=[("_id", -1)])


class ChangeRequest(BaseModel):
    action: Literal["publish", "pause", "budget"]
    daily_budget_inr: Optional[float] = None


ACTION_MAP = {"publish": "publish_campaign", "pause": "pause_campaign", "budget": "update_budget"}


@router.post("/{campaign_id}/requests")
def request_change(campaign_id: int, body: ChangeRequest):
    """Buttons in the UI use this. Like the agent's request_* tools, it only creates a pending approval."""
    payload = {}
    if body.action == "budget":
        if body.daily_budget_inr is None or body.daily_budget_inr < 100:
            raise HTTPException(422, "daily_budget_inr must be at least 100")
        payload["daily_budget"] = to_minor(body.daily_budget_inr)
    result = create_approval(ACTION_MAP[body.action], campaign_id, payload, source="user")
    if result["status"] == "error":
        raise HTTPException(409, result["message"])
    return result

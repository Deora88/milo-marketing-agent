"""Ad image generation with FLUX.1 [schnell] on Cloudflare Workers AI (free daily allowance, Apache-2.0 model).

The model is asked for a clean photo with NO text: image models misspell words, and Meta's ad text, headline and
button already carry the message. The picture is saved as the campaign's ad image (draft only); it reaches Meta
only when a human approves publishing.
"""
import base64
from datetime import datetime, time, timezone
from pathlib import Path

import httpx

from app.config import Settings, get_settings
from app.db import store
from app.db.models import AuditLog, Campaign

IMAGE_DIR = Path("data/ad_images")
STYLE = ("professional advertising photograph, high quality, natural light, inviting, "
         "square composition, no text, no letters, no words, no logos, no watermark")
MAX_PROMPT = 600


class ImageGenUnavailable(Exception):
    """Not configured, daily limit reached, or Cloudflare refused the request."""


def default_prompt(c: Campaign) -> str:
    place = f" in {c.location_name}" if c.location_name else ""
    return f"Ad photo for: {c.headline}. {c.primary_text}{place}"


def build_prompt(subject: str) -> str:
    subject = " ".join((subject or "").split())[:MAX_PROMPT]
    return f"{subject}. {STYLE}"


def used_today() -> int:
    start = datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)
    return store.count(AuditLog, {"event": "image_generated", "created_at": {"$gte": start}})


def _check(s: Settings) -> None:
    if not (s.cloudflare_account_id and s.cloudflare_api_token):
        raise ImageGenUnavailable("Image generation is not set up: add CLOUDFLARE_ACCOUNT_ID and "
                                  "CLOUDFLARE_API_TOKEN to .env (free Cloudflare Workers AI account).")
    if used_today() >= s.image_daily_limit:
        raise ImageGenUnavailable(f"Daily image limit reached ({s.image_daily_limit}, IMAGE_DAILY_LIMIT). "
                                  "Try again tomorrow or upload an image.")


async def generate_image(prompt: str, settings: Settings | None = None,
                         transport: httpx.AsyncBaseTransport | None = None) -> bytes:
    s = settings or get_settings()
    _check(s)
    url = (f"https://api.cloudflare.com/client/v4/accounts/{s.cloudflare_account_id}"
           f"/ai/run/{s.image_model}")
    body = {"prompt": prompt, "steps": max(1, min(int(s.image_steps), 8))}
    try:
        async with httpx.AsyncClient(timeout=60, transport=transport) as http:
            resp = await http.post(url, json=body, headers={"Authorization": f"Bearer {s.cloudflare_api_token}"})
    except httpx.HTTPError as exc:
        raise ImageGenUnavailable(f"Could not reach Cloudflare: {exc.__class__.__name__}") from exc
    try:
        data = resp.json()
    except ValueError:
        raise ImageGenUnavailable(f"Cloudflare returned an unexpected response (HTTP {resp.status_code}).") from None
    if resp.status_code in (401, 403):
        raise ImageGenUnavailable("Cloudflare rejected the API token (check CLOUDFLARE_API_TOKEN and its Workers AI permission).")
    if resp.status_code == 429:
        raise ImageGenUnavailable("Cloudflare's free daily allowance is used up. Try again tomorrow or upload an image.")
    image_b64 = ((data or {}).get("result") or {}).get("image")
    if not data.get("success", True) or not image_b64:
        errs = "; ".join(e.get("message", "") for e in (data.get("errors") or [])) or f"HTTP {resp.status_code}"
        raise ImageGenUnavailable(f"Cloudflare could not generate the image: {errs[:200]}")
    return base64.b64decode(image_b64)


async def generate_for_campaign(campaign_id: int, subject: str = "", source: str = "user",
                                settings: Settings | None = None,
                                transport: httpx.AsyncBaseTransport | None = None) -> Campaign:
    """Generate an ad image and attach it to a campaign that is not on Meta yet. Draft-only: nothing is sent to Meta."""
    c = store.get(Campaign, campaign_id)
    if c is None:
        raise ValueError("campaign not found")
    if c.meta_creative_id:
        raise ValueError("The ad is already created on Meta; make a new campaign to change the image.")
    prompt = build_prompt(subject or default_prompt(c))
    data = await generate_image(prompt, settings, transport)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGE_DIR / f"campaign-{c.id}.jpg"
    path.write_bytes(data)
    c.image_path, c.meta_image_hash = str(path), None
    store.save(c)
    store.audit("image_generated", campaign_id=c.id, source=source, prompt=prompt[:300], bytes=len(data))
    return c

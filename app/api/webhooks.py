"""Meta webhook for DMs: Messenger and Instagram messages sent to the Page.

Step 1 of DM handling: verify, store, answer fast. Nothing here replies to anyone and nothing can change an ad;
the DM agent that answers stored messages comes next.

- GET  /webhooks/meta: Meta's subscribe check (hub.mode, hub.verify_token, hub.challenge).
- POST /webhooks/meta: one or more events. The X-Hub-Signature-256 header must be the HMAC-SHA256 of the raw
  body with the app secret, checked before the body is parsed. Meta wants a 200 within 5 seconds and retries
  failed deliveries, so events are stored with their message id (mid) as a unique key and repeats are dropped.

This is the only route that accepts requests from outside localhost (see app/security.py); the signature check
replaces the Host allow-list and CSRF header for it.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.db import store
from app.db.models import InboundMessage

log = logging.getLogger("milo.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY = 1_000_000  # Meta batches are small; refuse anything bigger before hashing it
PLATFORM = {"page": "messenger", "instagram": "instagram"}


def signature_ok(raw: bytes, header: str, secret: str) -> bool:
    """X-Hub-Signature-256 = "sha256=" + hex HMAC-SHA256(raw body, app secret). Constant-time compare."""
    if not header.startswith("sha256=") or not secret:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256=").strip().lower(), expected)


@router.get("/meta")
def verify(request: Request):
    """Meta calls this once when you save the webhook in the App Dashboard: echo the challenge if the token matches."""
    q = request.query_params
    token = get_settings().meta_webhook_verify_token
    if (q.get("hub.mode") == "subscribe" and token
            and hmac.compare_digest(q.get("hub.verify_token", ""), token)):
        return PlainTextResponse(q.get("hub.challenge", ""))
    return PlainTextResponse("Verification failed", status_code=403)


def _ts(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_events(body: dict) -> list[InboundMessage]:
    """Messages and postbacks from a webhook body. Other event types (reads, deliveries) are ignored for now."""
    platform = PLATFORM.get(body.get("object", ""))
    out = []
    for entry in body.get("entry") or []:
        for ev in entry.get("messaging") or []:
            sender = str((ev.get("sender") or {}).get("id", ""))
            recipient = str((ev.get("recipient") or {}).get("id", ""))
            if "message" in ev:
                m = ev["message"] or {}
                if not m.get("mid"):
                    continue
                out.append(InboundMessage(
                    platform=platform, page_id=str(entry.get("id", "")), sender_id=sender, recipient_id=recipient,
                    mid=m["mid"], text=(m.get("text") or "")[:2000], is_echo=bool(m.get("is_echo")),
                    referral=ev.get("referral") or m.get("referral") or {}, sent_at=_ts(ev.get("timestamp"))))
            elif "postback" in ev:
                p = ev["postback"] or {}
                mid = p.get("mid") or f"postback:{sender}:{ev.get('timestamp', '')}"
                out.append(InboundMessage(
                    platform=platform, page_id=str(entry.get("id", "")), sender_id=sender, recipient_id=recipient,
                    mid=mid, kind="postback", text=(p.get("title") or p.get("payload") or "")[:2000],
                    referral=p.get("referral") or {}, sent_at=_ts(ev.get("timestamp"))))
    return out


@router.post("/meta")
async def receive(request: Request):
    secret = get_settings().meta_app_secret
    if not secret:
        return JSONResponse({"detail": "Webhook not configured: set META_APP_SECRET."}, status_code=503)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return JSONResponse({"detail": "Payload too large."}, status_code=413)
    if not signature_ok(raw, request.headers.get("x-hub-signature-256", ""), secret):
        log.warning("webhook: rejected a request with a missing or wrong signature")
        return JSONResponse({"detail": "Invalid signature."}, status_code=401)
    try:
        body = json.loads(raw)
    except ValueError:
        return JSONResponse({"detail": "Body is not JSON."}, status_code=400)
    if body.get("object") not in PLATFORM:
        return JSONResponse({"detail": "Unsupported object."}, status_code=404)

    stored = duplicates = 0
    for msg in parse_events(body):
        try:
            store.insert(msg)
            stored += 1
        except DuplicateKeyError:
            duplicates += 1  # Meta retried a delivery we already have
    if stored:
        store.audit("dm_received", count=stored, platform=PLATFORM[body["object"]])
    log.info("webhook: %d new event(s), %d duplicate(s)", stored, duplicates)
    return PlainTextResponse("EVENT_RECEIVED")

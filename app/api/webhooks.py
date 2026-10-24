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

from fastapi import APIRouter, BackgroundTasks, Request
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
                echo = bool(m.get("is_echo"))
                out.append(InboundMessage(
                    platform=platform, page_id=str(entry.get("id", "")), sender_id=sender, recipient_id=recipient,
                    customer_id=recipient if echo else sender,
                    mid=m["mid"], text=(m.get("text") or "")[:2000], is_echo=echo,
                    app_id=str(m.get("app_id") or "") if echo else "",
                    referral=ev.get("referral") or m.get("referral") or {}, sent_at=_ts(ev.get("timestamp"))))
            elif "postback" in ev:
                p = ev["postback"] or {}
                mid = p.get("mid") or f"postback:{sender}:{ev.get('timestamp', '')}"
                out.append(InboundMessage(
                    platform=platform, page_id=str(entry.get("id", "")), sender_id=sender, recipient_id=recipient,
                    customer_id=sender, mid=mid, kind="postback", text=(p.get("title") or p.get("payload") or "")[:2000],
                    referral=p.get("referral") or {}, sent_at=_ts(ev.get("timestamp"))))
    return out


async def _run_agent(ids: list[int]) -> None:
    """After the 200 is sent: let the DM agent look at each new message (drafts or sends a reply)."""
    from app.dm import agent

    for mid in ids:
        try:
            await agent.handle(mid)
        except Exception as exc:  # noqa: BLE001 - never let one message break the others
            log.exception("dm agent failed for message %s", mid)
            try:
                agent.mark_failed(mid, "background", exc)
            except Exception:  # noqa: BLE001 - e.g. the database itself is down; the traceback above stays
                log.exception("could not record the failure for message %s", mid)


async def _read_capped(request: Request) -> bytes | None:
    """The raw body, or None if it is bigger than MAX_BODY (checked before and while reading, never after)."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY:
        return None
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            return None
    return bytes(raw)


@router.post("/meta")
async def receive(request: Request, background: BackgroundTasks):
    secret = get_settings().meta_app_secret
    if not secret:
        log.error("webhook: META_APP_SECRET is not set, refusing a Meta delivery (Meta will retry, then may disable "
                  "the webhook)")
        return JSONResponse({"detail": "Webhook not configured: set META_APP_SECRET."}, status_code=503)
    raw = await _read_capped(request)
    if raw is None:
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
    new_inbound: list[int] = []
    for msg in parse_events(body):
        try:
            store.insert(msg)
            stored += 1
            if not msg.is_echo:
                new_inbound.append(msg.id)
        except DuplicateKeyError:
            duplicates += 1  # Meta retried a delivery we already have
            old = (store.find(InboundMessage, {"mid": msg.mid}) or [None])[0]
            if old and not old.is_echo and not old.agent_action:
                new_inbound.append(old.id)  # stored earlier but never handled (e.g. restart after the 200)
    if stored:
        store.audit("dm_received", count=stored, platform=PLATFORM[body["object"]])
    log.info("webhook: %d new event(s), %d duplicate(s)", stored, duplicates)
    if new_inbound and get_settings().dm_agent_enabled:
        background.add_task(_run_agent, new_inbound)
    return PlainTextResponse("EVENT_RECEIVED")

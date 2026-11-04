"""Request checks that protect the approval gate.

1. Host allow-list (default localhost, 127.0.0.1): blocks DNS-rebinding, where a malicious site points its own
   domain at 127.0.0.1 to reach Milo from the browser.
2. Every request that changes something (POST/PUT/PATCH/DELETE under /api) must send the X-Milo-Client header.
   A cross-site page cannot add a custom header without a CORS preflight, and CORS only allows localhost origins,
   so a hidden form or fetch on another site can no longer press Approve (CSRF).
3. Optional API token (API_TOKEN in .env) for running Milo beyond localhost: every /api request must then send
   "Authorization: Bearer <token>".

The exceptions are /webhooks/ and /media/: Meta calls them from the internet (through a tunnel or a public host),
so they skip the Host allow-list. /webhooks/ is protected by the X-Hub-Signature-256 check in app/api/webhooks.py
and can only store incoming messages. /media/ is read-only and serves only the files of an approved-or-pending
Instagram post behind a random 32-character token (app/api/posts.py); everything else there is 404.
"""
import hmac

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import get_settings

CLIENT_HEADER = "x-milo-client"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


WEBHOOK_PREFIX = "/webhooks/"
MEDIA_PREFIX = "/media/"


async def guard_requests(request: Request, call_next):
    s = get_settings()
    if request.url.path.startswith(WEBHOOK_PREFIX):
        return await call_next(request)  # signature-checked in the route itself
    if request.url.path.startswith(MEDIA_PREFIX) and request.method in ("GET", "HEAD"):
        return await call_next(request)  # random-token, read-only post media for Meta's downloader
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    allowed = {h.strip().lower() for h in s.allowed_hosts.split(",") if h.strip()}
    if host not in allowed:
        return JSONResponse({"detail": f"Host '{host}' is not allowed (ALLOWED_HOSTS)."}, status_code=400)

    path = request.url.path
    if path.startswith("/api/") and request.method != "OPTIONS":
        if s.api_token:
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given, s.api_token):
                return JSONResponse({"detail": "Missing or wrong API token."}, status_code=401)
        if request.method in UNSAFE and not request.headers.get(CLIENT_HEADER):
            return JSONResponse({"detail": "Missing X-Milo-Client header."}, status_code=403)
    return await call_next(request)

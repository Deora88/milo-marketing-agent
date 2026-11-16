"""Request checks that protect the approval gate.

1. Host allow-list (default localhost, 127.0.0.1): blocks DNS-rebinding, where a malicious site points its own
   domain at 127.0.0.1 to reach Milo from the browser.
2. Every request that changes something (POST/PUT/PATCH/DELETE under /api) must send the X-Milo-Client header.
   A cross-site page cannot add a custom header without a CORS preflight, and CORS only allows localhost origins,
   so a hidden form or fetch on another site can no longer press Approve (CSRF).
3. Optional API token (API_TOKEN in .env) for running Milo beyond localhost: every /api request must then send
   "Authorization: Bearer <token>".
4. Tunnel guard: a request that came through a tunnel or reverse proxy (ngrok, cloudflared, nginx add
   X-Forwarded-* / Forwarded headers) may only reach /webhooks/ and /media/ unless API_TOKEN is set. This holds
   even if the tunnel rewrites the Host header to localhost, so the dashboard and /api are never public by accident.

The exceptions are /webhooks/ and /media/: Meta calls them from the internet (through a tunnel or a public host),
so they skip the Host allow-list. /webhooks/ is protected by the X-Hub-Signature-256 check in app/api/webhooks.py
and can only store incoming messages. /media/ is read-only and serves only the files of an approved-or-pending
Instagram post behind a random 32-character token (app/api/posts.py); everything else there is 404.
A path with "." / ".." segments or a doubled slash is never treated as one of these two public prefixes
(/webhooks/../index.html used to slip past the tunnel guard and return the dashboard's HTML).

5. Every response carries X-Frame-Options: DENY and CSP frame-ancestors 'none', so another website cannot load
   the dashboard in a hidden frame and trick a click on Approve (clickjacking; the X-Milo-Client header does not
   stop that, because the framed dashboard sends its own requests), plus nosniff and a strict referrer policy.
"""
import hmac

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import get_settings

CLIENT_HEADER = "x-milo-client"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip")
WEBHOOK_PREFIX = "/webhooks/"
MEDIA_PREFIX = "/media/"


SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def _plain(path: str) -> bool:
    """No ".", ".." or empty segments (and no backslash): the path means what its prefix says."""
    return "\\" not in path and all(seg not in ("", ".", "..") for seg in path.split("/")[1:])


async def guard_requests(request: Request, call_next):
    response = await _guard(request, call_next)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


async def _guard(request: Request, call_next):
    s = get_settings()
    plain = _plain(request.url.path)
    if plain and request.url.path.startswith(WEBHOOK_PREFIX):
        return await call_next(request)  # signature-checked in the route itself
    if plain and request.url.path.startswith(MEDIA_PREFIX) and request.method in ("GET", "HEAD"):
        return await call_next(request)  # random-token, read-only post media for Meta's downloader
    if not s.api_token and any(h in request.headers for h in PROXY_HEADERS):
        return JSONResponse({"detail": "Milo's dashboard and API are not available through the public tunnel. Open "
                                       "http://localhost:8000 on this PC, or set API_TOKEN to allow remote use."},
                            status_code=403)
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

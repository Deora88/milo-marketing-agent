"""LinkedIn REST client for organic Page posts (Community Management API: Posts API + Images API).

- Every call carries the monthly "LinkedIn-Version" header (YYYYMM) and "X-Restli-Protocol-Version: 2.0.0".
- Retries 429 and 5xx with exponential backoff (honours Retry-After).
- LINKEDIN_DRY_RUN=true (default) returns fake URNs and never touches LinkedIn.

Needs a 3-legged member access token with w_organization_social (r_organization_social to read posts back),
from a member who is an ADMINISTRATOR / CONTENT_ADMINISTRATOR of the Page. Tokens last 60 days.
"""
import asyncio
import logging
import uuid
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config import Settings, get_settings

log = logging.getLogger("milo.linkedin")
BASE = "https://api.linkedin.com/rest"
RETRY_STATUS = {429, 500, 502, 503, 504}


class LinkedInAPIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"LinkedIn error {status} {code}: {message}".strip())
        self.status, self.code, self.message = status, code, message


def _error(resp: httpx.Response) -> LinkedInAPIError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    code = str(body.get("code") or body.get("serviceErrorCode") or "")
    msg = body.get("message") or resp.text[:200] or "unknown error"
    if resp.status_code == 401:
        msg += " (the LinkedIn token is missing, wrong or expired: tokens last 60 days; make a new one)"
    elif resp.status_code == 403:
        msg += (" (the token's member must be an admin of the Page, and the app needs the Community Management "
                "API with w_organization_social)")
    return LinkedInAPIError(resp.status_code, code, msg)


class LinkedInClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 sleep=asyncio.sleep):
        self.s = settings or get_settings()
        self._http = httpx.AsyncClient(timeout=60, transport=transport)
        self._sleep = sleep

    @property
    def dry_run(self) -> bool:
        return self.s.linkedin_dry_run

    @property
    def organization_urn(self) -> str:
        raw = self.s.linkedin_organization_id.strip()
        return raw if raw.startswith("urn:li:organization:") else f"urn:li:organization:{raw}"

    def _headers(self, json_body: bool = True) -> dict:
        h = {"Authorization": f"Bearer {self.s.linkedin_access_token}",
             "LinkedIn-Version": self.s.linkedin_api_version,
             "X-Restli-Protocol-Version": "2.0.0"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    async def request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                      retries: int = 3, retry_on: set[int] = RETRY_STATUS) -> httpx.Response:
        url = path if path.startswith("https://") else f"{BASE}{path}"
        delay = 2.0
        for attempt in range(retries + 1):
            resp = await self._http.request(method, url, json=json, params=params, headers=self._headers())
            if resp.status_code < 400:
                return resp
            if resp.status_code in retry_on and attempt < retries:
                wait = float(resp.headers.get("retry-after") or delay)
                log.warning("LinkedIn %s on %s %s, retrying in %.0fs", resp.status_code, method, path, wait)
                await self._sleep(wait)
                delay *= 2
                continue
            raise _error(resp)
        raise LinkedInAPIError(0, "", "retries exhausted")

    # --- images ---

    async def upload_image(self, data: bytes, owner: str | None = None) -> str:
        """initializeUpload -> PUT the bytes to uploadUrl -> image URN (urn:li:image:...)."""
        owner = owner or self.organization_urn
        if self.dry_run:
            log.info("[dry-run] upload image (%d bytes) for %s", len(data), owner)
            return f"urn:li:image:dry_{uuid.uuid4().hex[:10]}"
        resp = await self.request("POST", "/images?action=initializeUpload",
                                  json={"initializeUploadRequest": {"owner": owner}})
        value = (resp.json() or {}).get("value") or {}
        upload_url, urn = value.get("uploadUrl"), value.get("image")
        if not upload_url or not urn:
            raise LinkedInAPIError(resp.status_code, "", "initializeUpload returned no uploadUrl / image URN")
        host = urlparse(upload_url).hostname or ""
        if not (host == "linkedin.com" or host.endswith(".linkedin.com")):  # never send the token elsewhere
            raise LinkedInAPIError(0, "", f"unexpected upload host {host!r}")
        put = await self._http.put(upload_url, content=data,
                                   headers={"Authorization": f"Bearer {self.s.linkedin_access_token}",
                                            "Content-Type": "application/octet-stream"})
        if put.status_code >= 400:
            raise _error(put)
        return urn

    async def image_status(self, urn: str) -> str:
        if self.dry_run:
            return "AVAILABLE"
        resp = await self.request("GET", f"/images/{quote(urn, safe='')}")
        return str((resp.json() or {}).get("status") or "")

    async def wait_image_available(self, urn: str, tries: int = 30, every: float = 2.0) -> None:
        """A post made before its image is processed may never show to members, so wait for AVAILABLE."""
        for _ in range(tries):
            status = await self.image_status(urn)
            if status == "AVAILABLE":
                return
            if status in ("PROCESSING_FAILED", "DELETED"):
                raise LinkedInAPIError(0, status, f"LinkedIn could not process image {urn}")
            await self._sleep(every)
        raise LinkedInAPIError(0, "TIMEOUT", "LinkedIn took too long to process the image. Try again later.")

    # --- posts ---

    async def create_post(self, body: dict) -> str:
        """POST /rest/posts -> post URN from the x-restli-id header (urn:li:share:... or urn:li:ugcPost:...)."""
        if self.dry_run:
            log.info("[dry-run] create LinkedIn post %s", {k: body[k] for k in ("author", "content") if k in body})
            return f"urn:li:share:dry_{uuid.uuid4().hex[:10]}"
        # Retry only 429 (nothing was created). A 5xx may have created the post, so never retry it: no duplicates.
        resp = await self.request("POST", "/posts", json=body, retry_on={429})
        urn = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id") or ""
        if not urn:
            raise LinkedInAPIError(resp.status_code, "", "LinkedIn created the post but returned no id")
        return urn

    async def delete_post(self, urn: str) -> None:
        if self.dry_run:
            return
        await self.request("DELETE", f"/posts/{quote(urn, safe='')}")

    async def get_post(self, urn: str) -> dict[str, Any]:
        resp = await self.request("GET", f"/posts/{quote(urn, safe='')}")
        return resp.json() or {}

    async def aclose(self) -> None:
        await self._http.aclose()


def post_url(urn: str) -> str:
    """Public link to a post, e.g. https://www.linkedin.com/feed/update/urn:li:share:123/."""
    return f"https://www.linkedin.com/feed/update/{urn}/" if urn and "dry_" not in urn else ""

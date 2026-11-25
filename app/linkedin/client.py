"""LinkedIn REST client for organic Page posts (Community Management API: Posts API + Images API).

- Every call carries the monthly "LinkedIn-Version" header (YYYYMM) and "X-Restli-Protocol-Version: 2.0.0".
- Retries 429 and 5xx with exponential backoff (honours Retry-After).
- LINKEDIN_DRY_RUN=true (default) returns fake URNs and never touches LinkedIn.

Two ways to post (LINKEDIN_AUTHOR):
- page: as the company Page. Token with w_organization_social (Community Management API, reviewed) from a Page admin.
- me:   as the signed-in member's own profile. Token with w_member_social (self-serve "Share on LinkedIn" product).
Tokens last 60 days.
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


class PostMayBeLiveError(LinkedInAPIError):
    """Creating the post failed in a way where LinkedIn may still have published it (5xx, timeout, network error,
    or a success with no post id). Never retry it automatically; a person must check LinkedIn first."""


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
        msg += (" (posting as the Page: the token's member must be a Page admin and the app needs the Community "
                "Management API; posting as yourself: the app needs 'Share on LinkedIn' and the token w_member_social)")
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
    def posts_as_member(self) -> bool:
        return self.s.linkedin_author.strip().lower() == "me"

    @property
    def author_urn(self) -> str:
        """Who the post is from: urn:li:person:<id> (LINKEDIN_AUTHOR=me) or urn:li:organization:<id> (page)."""
        if self.posts_as_member:
            raw = self.s.linkedin_person_id.strip()
            return raw if raw.startswith("urn:li:person:") else f"urn:li:person:{raw}"
        return self.organization_urn

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
        owner = owner or self.author_urn
        if self.dry_run:
            log.info("[dry-run] upload image (%d bytes) for %s", len(data), owner)
            return f"urn:li:image:dry_{uuid.uuid4().hex[:10]}"
        resp = await self.request("POST", "/images?action=initializeUpload",
                                  json={"initializeUploadRequest": {"owner": owner}})
        value = (resp.json() or {}).get("value") or {}
        upload_url, urn = value.get("uploadUrl"), value.get("image")
        if not upload_url or not urn:
            raise LinkedInAPIError(resp.status_code, "", "initializeUpload returned no uploadUrl / image URN")
        parts = urlparse(upload_url)
        host = parts.hostname or ""
        # never send the token anywhere but LinkedIn, and never in clear text
        if parts.scheme != "https" or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
            raise LinkedInAPIError(0, "", f"unexpected upload URL {parts.scheme}://{host}")
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
        # Retry only 429 (nothing was created). A 5xx or a timeout may have created the post, so never retry it,
        # and say so: the caller must not treat it as a plain failure that is safe to approve again.
        try:
            resp = await self.request("POST", "/posts", json=body, retry_on={429})
        except httpx.TransportError as exc:  # timeout or dropped connection after the request was sent
            raise PostMayBeLiveError(0, type(exc).__name__,
                                     "no answer from LinkedIn while creating the post") from exc
        except LinkedInAPIError as exc:
            if exc.status >= 500:
                raise PostMayBeLiveError(exc.status, exc.code, exc.message) from exc
            raise  # 4xx: LinkedIn refused it, nothing was posted
        urn = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id") or _id_from_body(resp)
        if not urn:
            raise PostMayBeLiveError(resp.status_code, "", "LinkedIn accepted the post but returned no post id")
        return urn

    async def delete_post(self, urn: str) -> None:
        if self.dry_run:
            return
        await self.request("DELETE", f"/posts/{quote(urn, safe='')}")

    async def get_post(self, urn: str) -> dict[str, Any]:
        resp = await self.request("GET", f"/posts/{quote(urn, safe='')}")
        return resp.json() or {}

    # --- results (read-only) ---

    async def social_metadata(self, urn: str) -> dict[str, int]:
        """Reactions and comments on a post: GET /rest/socialMetadata/{urn} (Social Metadata API)."""
        resp = await self.request("GET", f"/socialMetadata/{quote(urn, safe='')}", retries=1)
        body = resp.json() or {}
        reactions = sum(int((r or {}).get("count") or 0) for r in (body.get("reactionSummaries") or {}).values())
        comments = int(((body.get("commentSummary") or {}).get("count")) or 0)
        return {"reactions": reactions, "comments": comments}

    async def member_post_metric(self, urn: str, metric: str) -> int:
        """One lifetime number for a post on the member's own profile (Member Post Analytics API, needs
        r_member_postAnalytics): metric = IMPRESSION | MEMBERS_REACHED | RESHARE | REACTION | COMMENT."""
        kind = "ugc" if ":ugcPost:" in urn else "share"
        # Rest.li finder: the URN inside (share:...) is percent-encoded, the brackets and colon are not.
        path = (f"/memberCreatorPostAnalytics?q=entity&entity=({kind}:{quote(urn, safe='')})"
                f"&queryType={metric}&aggregation=TOTAL")
        resp = await self.request("GET", path, retries=1)
        return sum(int((e or {}).get("count") or 0) for e in (resp.json() or {}).get("elements") or [])

    async def aclose(self) -> None:
        await self._http.aclose()


def _id_from_body(resp: httpx.Response) -> str:
    """Fallback when the x-restli-id header is missing: some responses carry the URN in the body instead."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    urn = str((body or {}).get("id") or "") if isinstance(body, dict) else ""
    return urn if urn.startswith("urn:li:") else ""


def post_url(urn: str) -> str:
    """Public link to a post, e.g. https://www.linkedin.com/feed/update/urn:li:share:123/."""
    return f"https://www.linkedin.com/feed/update/{urn}/" if urn and "dry_" not in urn else ""

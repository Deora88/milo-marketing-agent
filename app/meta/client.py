"""Throttled Meta Graph API client.

- Reads Meta's usage headers on every response and slows down above 75% utilisation.
- Retries rate-limit errors (codes 4, 17, 613, 80000-80014) with exponential backoff.
- META_DRY_RUN=true returns fake IDs and never touches Meta (safe for development).
"""
import asyncio
import json
import logging
import uuid
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger("milo.meta")

RATE_LIMIT_CODES = {4, 17, 613} | set(range(80000, 80015))
SLOWDOWN_PCT = 75


class MetaAPIError(Exception):
    def __init__(self, code: int, subcode: int | None, message: str):
        super().__init__(f"Meta error {code}/{subcode}: {message}")
        self.code, self.subcode, self.message = code, subcode, message


def max_usage_pct(headers: httpx.Headers) -> float:
    """Highest utilisation % reported across Meta's usage headers."""
    pct = 0.0
    for name in ("x-business-use-case-usage", "x-ad-account-usage", "x-fb-ads-insights-throttle", "x-app-usage"):
        raw = headers.get(name)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        # x-business-use-case-usage = {"<account_id>": [{"call_count": .., "total_time": .., ...}]}
        entries = [e for v in data.values() if isinstance(v, list) for e in v] if name == "x-business-use-case-usage" else [data]
        for e in entries:
            for key in ("call_count", "total_time", "total_cputime", "acc_id_util_pct", "app_id_util_pct"):
                if isinstance(e.get(key), (int, float)):
                    pct = max(pct, float(e[key]))
    return pct


class MetaClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings or get_settings()
        self.base = f"https://graph.facebook.com/{self.s.meta_api_version}"
        self._http = httpx.AsyncClient(base_url=self.base, timeout=30, transport=transport)
        self.last_usage_pct = 0.0

    @property
    def account(self) -> str:
        # Accept the id with or without the "act_" prefix in .env.
        raw = self.s.meta_ad_account_id.strip().removeprefix("act_")
        return f"act_{raw}"

    async def request(self, method: str, path: str, *, params: dict | None = None, data: dict | None = None, retries: int = 4) -> dict[str, Any]:
        if self.s.meta_dry_run:
            log.info("[dry-run] %s %s %s", method, path, data or params)
            return {"id": f"dry_{uuid.uuid4().hex[:10]}", "success": True, "data": []}

        headers = {"Authorization": f"Bearer {self.s.meta_access_token}"}
        enc = lambda d: {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in (d or {}).items()}  # noqa: E731
        body, params = enc(data), enc(params) or None
        delay = 2.0
        for attempt in range(retries + 1):
            if self.last_usage_pct >= SLOWDOWN_PCT:
                await asyncio.sleep(1.0)
            resp = await self._http.request(method, path, params=params, data=body or None, headers=headers)
            self.last_usage_pct = max_usage_pct(resp.headers)
            payload = resp.json()
            err = payload.get("error")
            if not err:
                return payload
            code, sub = err.get("code", 0), err.get("error_subcode")
            if code in RATE_LIMIT_CODES and attempt < retries:
                log.warning("Rate limited (%s/%s), retrying in %.0fs", code, sub, delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            # error_user_title / error_user_msg carry Meta's plain-language explanation ("Invalid parameter" alone
            # is not actionable), so include them when present.
            detail = " - ".join(x for x in (err.get("error_user_title"), err.get("error_user_msg")) if x)
            msg = err.get("message", "unknown error") + (f" ({detail})" if detail else "")
            raise MetaAPIError(code, sub, msg)
        raise MetaAPIError(0, None, "retries exhausted")

    async def get(self, path: str, **params) -> dict:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, **data) -> dict:
        return await self.request("POST", path, data=data)

    async def aclose(self) -> None:
        await self._http.aclose()

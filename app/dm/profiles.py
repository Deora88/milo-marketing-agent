"""Who sent a DM: the name Meta shares for the person, looked up once and cached in MongoDB.

- Messenger: User Profile API, GET /{PSID}?fields=first_name,last_name (Page access token).
- Instagram: GET /{IGSID}?fields=name,username (Page access token of the linked Page).
Meta may refuse (privacy settings, missing permission, person deleted the conversation): the Inbox then shows
"Unknown sender" with the id, and Milo asks again after a day. In dry-run mode Meta is never called.
"""
import logging
from datetime import timedelta

import httpx
from pymongo.errors import DuplicateKeyError

from app.config import Settings, get_settings
from app.db import store
from app.db.models import CustomerProfile, now
from app.dm import inbox
from app.meta.client import MetaAPIError, MetaClient
from app.util.redact import redact

log = logging.getLogger("milo.profiles")
RETRY_AFTER = timedelta(hours=24)
FIELDS = {"messenger": "first_name,last_name", "instagram": "name,username"}


def get(platform: str, customer_id: str) -> CustomerProfile | None:
    rows = store.find(CustomerProfile, {"platform": platform, "customer_id": customer_id}, limit=1)
    return rows[0] if rows else None


def all_by_key() -> dict[tuple[str, str], CustomerProfile]:
    return {(p.platform, p.customer_id): p for p in store.find(CustomerProfile)}


def _save(platform: str, customer_id: str, changes: dict) -> CustomerProfile:
    old = get(platform, customer_id)
    if old is None:
        try:
            return store.insert(CustomerProfile(platform=platform, customer_id=customer_id, **changes))
        except DuplicateKeyError:  # another lookup stored it first
            old = get(platform, customer_id)
    return store.update(CustomerProfile, old.id, changes)


def _due(p: CustomerProfile | None) -> bool:
    if p is None or p.fetched_at is None:
        return True
    if p.name or p.username:
        return False  # names are kept; they rarely change
    return now().replace(tzinfo=None) - p.fetched_at.replace(tzinfo=None) >= RETRY_AFTER


async def ensure(platform: str, page_id: str, customer_id: str, client: MetaClient | None = None,
                 settings: Settings | None = None) -> CustomerProfile | None:
    """The cached profile, looked up from Meta first if it's missing (or failed more than a day ago)."""
    s = settings or get_settings()
    cached = get(platform, customer_id)
    if not customer_id or platform not in FIELDS or s.meta_dry_run or not _due(cached):
        return cached
    own = client is None
    client = client or MetaClient(s)
    try:
        token = await inbox._page_token(client, await inbox.sending_page(client, platform, page_id))
        data = await client.get(f"/{customer_id}", token=token, fields=FIELDS[platform])
        if platform == "messenger":
            name = " ".join(x for x in (data.get("first_name"), data.get("last_name")) if x)
            changes = {"name": name[:120], "username": ""}
        else:
            changes = {"name": str(data.get("name") or "")[:120], "username": str(data.get("username") or "")[:60]}
        changes.update(fetched_at=now(), error="" if (changes["name"] or changes["username"]) else "no name shared")
    except (MetaAPIError, inbox.ReplyError, httpx.HTTPError) as exc:
        log.warning("profile lookup failed for %s %s: %s", platform, customer_id[-4:], redact(exc)[:200])
        changes = {"fetched_at": now(), "error": f"{type(exc).__name__}: {redact(exc)}"[:200]}
    finally:
        if own:
            await client.aclose()
    return _save(platform, customer_id, changes)


async def ensure_many(keys: list[tuple[str, str, str]]) -> None:
    """Background: look up the senders of newly stored DMs. Never raises (the Inbox just shows the id)."""
    for platform, page_id, customer_id in dict.fromkeys(keys):
        try:
            await ensure(platform, page_id, customer_id)
        except Exception:  # noqa: BLE001 - e.g. database down; the name is a nicety, never a blocker
            log.exception("profile lookup crashed for %s", customer_id[-4:])


def display(platform: str, customer_id: str, cache: dict | None = None) -> dict:
    """What the Inbox shows for a sender."""
    p = (cache or {}).get((platform, customer_id)) if cache is not None else get(platform, customer_id)
    return {"id": customer_id, "name": p.name if p else "", "username": p.username if p else "",
            "lookup_error": p.error if p else ""}

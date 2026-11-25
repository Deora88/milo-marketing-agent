"""Competitor watch: public Instagram business/creator accounts the user adds, read with the Business Discovery API.

GET /{our-ig-user-id}?fields=business_discovery.username(<name>){...} with the Page token. It works only for
business and creator accounts (not personal ones) and returns public numbers: followers, and per post the like and
comment counts (an owner can hide likes; then only comments count). Milo stores those numbers and a short caption
excerpt, to rank posts and find patterns; it never copies or re-posts anyone's content. Read-only, rate-friendly:
an account is refreshed at most every REFRESH_HOURS unless the user clicks Refresh.
"""
import re
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from app.db import store
from app.db.models import Competitor, SocialPost, now
from app.dm import inbox
from app.meta import campaigns as meta
from app.meta.client import MetaAPIError, MetaClient
from app.util.redact import redact

MAX_ACCOUNTS = 10
DAYS = 30
REFRESH_HOURS = 6
USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
FIELDS = ("business_discovery.username({u}){{username,name,followers_count,media_count,"
          "media.limit(30){{caption,like_count,comments_count,media_type,media_product_type,permalink,timestamp}}}}")


class LabsError(ValueError):
    """A request Milo refuses; the message is shown to the user."""


def clean(username: str) -> str:
    u = (username or "").strip().lstrip("@").strip()
    if not USERNAME.match(u):
        raise LabsError("Use an Instagram username: letters, numbers, dots and underscores, up to 30 characters.")
    return u.lower()


def add(username: str) -> Competitor:
    u = clean(username)
    if store.count(Competitor) >= MAX_ACCOUNTS:
        raise LabsError(f"Trends follows up to {MAX_ACCOUNTS} accounts. Remove one first.")
    try:
        c = store.insert(Competitor(username=u))
    except DuplicateKeyError as exc:
        raise LabsError(f"@{u} is already on the list.") from exc
    store.audit("labs_competitor_added", username=u)
    return c


def remove(username: str) -> None:
    u = clean(username)
    if not store.db()[Competitor.collection].delete_one({"username": u}).deleted_count:
        raise LabsError(f"@{u} is not on the list.")
    store.audit("labs_competitor_removed", username=u)


def _type(m: dict) -> str:
    if m.get("media_product_type") == "REELS":
        return "Reel"
    return {"CAROUSEL_ALBUM": "Carousel", "VIDEO": "Video", "IMAGE": "Photo"}.get(m.get("media_type", ""), "Post")


async def refresh(c: Competitor, client: MetaClient | None = None, force: bool = False) -> Competitor:
    if not force and c.checked_at and now() - c.checked_at < timedelta(hours=REFRESH_HOURS) and not c.error:
        return c
    own = client is None
    client = client or MetaClient()
    try:
        if client.s.meta_dry_run:
            return store.update(Competitor, c.id, {"error": "Dry run: Milo doesn't call Instagram. Set "
                                                           "META_DRY_RUN=false to load this account's posts.",
                                                  "checked_at": now()})
        ig = await meta.instagram_user_id(client)
        token = await inbox._page_token(client, client.s.meta_page_id)
        data = (await client.get(f"/{ig}", token=token, fields=FIELDS.format(u=c.username))).get(
            "business_discovery") or {}
        media = [{"caption": (m.get("caption") or "")[:200], "likes": m.get("like_count"),
                  "comments": m.get("comments_count") or 0, "type": _type(m), "permalink": m.get("permalink", ""),
                  "timestamp": m.get("timestamp", "")} for m in ((data.get("media") or {}).get("data") or [])]
        return store.update(Competitor, c.id, {"name": data.get("name", ""), "followers": data.get("followers_count") or 0,
                                               "media_count": data.get("media_count") or 0, "media": media,
                                               "checked_at": now(), "error": ""})
    except MetaAPIError as exc:
        text = str(exc).lower()
        why = ("Instagram couldn't find a business or creator account with this username (personal accounts "
               "can't be read)." if "cannot be found" in text or exc.code == 110 or "business" in text
               else f"Meta refused: {redact(exc)}")
        return store.update(Competitor, c.id, {"error": why[:300], "checked_at": now()})
    finally:
        if own:
            await client.aclose()


async def refresh_all(force: bool = False) -> list[Competitor]:
    return [await refresh(c, force=force) for c in store.find(Competitor, sort=[("_id", 1)])]


def _recent(c: Competitor, days: int) -> list[dict]:
    cutoff = (now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    return [m for m in c.media if (m.get("timestamp") or "")[:19] >= cutoff]


def _eng(m: dict) -> int:
    return (m.get("likes") or 0) + (m.get("comments") or 0)


def overview(days: int = DAYS) -> dict:
    accounts = store.find(Competitor, sort=[("_id", 1)])
    rows, by_type, per_week = [], {}, []
    for c in accounts:
        recent = _recent(c, days)
        for m in recent:
            rows.append({**m, "username": c.username, "engagement": _eng(m)})
            by_type.setdefault(m["type"], []).append(_eng(m))
        if c.checked_at and not c.error:
            per_week.append(len(recent) / (days / 7))
    top = sorted(rows, key=lambda r: r["engagement"], reverse=True)[:10]
    findings = []
    avg = {t: round(sum(v) / len(v)) for t, v in by_type.items() if v}
    if len(avg) >= 2:
        best = max(avg, key=avg.get)
        other = min(avg, key=avg.get)
        if avg[other]:
            findings.append(f"{best}s get {avg[best] / avg[other]:.1f}x the likes + comments of {other.lower()}s "
                            f"for these accounts (averages {avg[best]:,} vs {avg[other]:,}).")
    if per_week:
        mine = len([p for p in store.find(SocialPost, {"status": "published", "meta_mode": "live",
                                                        "platform": {"$ne": "linkedin"}})
                    if p.published_at and p.published_at >= now() - timedelta(days=days)]) / (days / 7)
        findings.append(f"They post about {sum(per_week) / len(per_week):.1f} times a week on average; "
                        f"you post {mine:.1f}.")
    hidden = sum(1 for r in rows if r.get("likes") is None)
    return {"days": days, "max_accounts": MAX_ACCOUNTS,
            "accounts": [{"username": c.username, "name": c.name, "followers": c.followers,
                          "posts_in_period": len(_recent(c, days)), "checked_at": c.checked_at, "error": c.error}
                         for c in accounts],
            "top_posts": top, "avg_by_type": avg, "findings": findings,
            "note": ("Public numbers from Instagram. " + (f"{hidden} post(s) hide their likes, so only comments "
                                                          "count for them. " if hidden else "") +
                     "Findings are calculated from these numbers; they are hints, not guarantees.")}

"""Hashtag trends: the top and most recent public Instagram posts for a hashtag (Instagram Hashtag Search API).

GET /ig_hashtag_search?user_id={ig}&q={tag} gives the hashtag id, then /{hashtag-id}/top_media and /recent_media
(recent = last 24 hours) give public posts with like and comment counts. Meta allows this only with the
"Instagram Public Content Access" feature, which needs a verified Meta business, Tech Provider status and App
Review. Until then (LABS_HASHTAGS_LIVE=false, or dry run) the card shows clearly labelled SAMPLE posts.

Limits Meta sets: 30 unique hashtags per Instagram account per rolling 7 days (a repeat search of the same tag
doesn't count again), no emoji hashtags, no Stories. Milo stores the numbers and a short caption excerpt only, to
rank posts; it never copies or re-posts anyone's content. A tag is refreshed at most every REFRESH_HOURS.
"""
import re
import unicodedata
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.db import store
from app.db.models import HashtagSearch, now
from app.labs.competitors import LabsError, _eng, _type
from app.meta import campaigns as meta
from app.meta.client import MetaAPIError, MetaClient
from app.util.redact import redact

WEEKLY_LIMIT = 30
REFRESH_HOURS = 6
TAG = re.compile(r"^\w{1,100}$")  # letters (any script), digits, underscore; no emoji or spaces
MEDIA_FIELDS = "caption,like_count,comments_count,media_type,permalink,timestamp"
NEEDS = ("Needs a verified Meta business. Real hashtag posts come from Meta's \"Instagram Public Content Access\", "
         "which Meta gives only after Business Verification, Tech Provider status and App Review. Until then these "
         "are sample posts, made up to show how the card works.")
APPROVAL_ERROR = ("Meta hasn't approved Instagram Public Content Access for this app yet (it needs a verified Meta "
                  "business and App Review). Showing sample posts.")


def clean(tag: str) -> str:
    t = (tag or "").strip().lstrip("#").strip()
    # Indian scripts write vowels as combining marks (e.g. the "ि" in दिवाली), which \w alone doesn't match.
    base = "".join(ch for ch in t if not unicodedata.category(ch).startswith("M"))
    if not base or not TAG.match(base) or len(t) > 100:
        raise LabsError("Use one hashtag without the # sign: letters, numbers or underscores (no spaces or emoji).")
    return t.lower()


def used_this_week() -> int:
    return store.count(HashtagSearch, {"first_queried_at": {"$gte": now() - timedelta(days=7)}})


def is_live() -> bool:
    s = get_settings()
    return s.labs_hashtags_live and not s.meta_dry_run


def _media(m: dict) -> dict:
    caption = m.get("caption") or ""
    return {"caption": caption[:200], "likes": m.get("like_count"), "comments": m.get("comments_count") or 0,
            "type": _type(m), "permalink": m.get("permalink", ""), "timestamp": m.get("timestamp", ""),
            "hashtags": caption.count("#")}


def sample(tag: str) -> dict:
    """Made-up example posts so the card can be shown (and recorded for App Review) before Meta approves."""
    t = f"#{tag}"
    rows = [("Reel", 2140, 186, f"Before/after: muddy SUV to showroom shine in 40 minutes {t} #detailing #foamwash"),
            ("Reel", 1630, 122, f"POV: we come to your door at 7 am {t} #doorstep #morningroutine"),
            ("Carousel", 980, 64, f"5 signs your car paint needs a wax this winter (save this) {t} #carcare"),
            ("Reel", 870, 41, f"Pressure wash ASMR, sound on {t}"),
            ("Photo", 420, 18, f"Diwali offer: interior + exterior, book this week {t} #diwali #offer #pune #car"),
            ("Photo", 260, 9, f"Clean car, happy owner {t}")]
    posts = [{"caption": c, "likes": lk, "comments": cm, "type": ty, "permalink": "", "timestamp": "",
              "hashtags": c.count("#"), "sample": True} for ty, lk, cm, c in rows]
    return {"top": posts, "recent": posts[3:] + posts[:1]}


def findings(posts: list[dict]) -> list[str]:
    out, by_type = [], {}
    for p in posts:
        by_type.setdefault(p["type"], []).append(_eng(p))
    avg = {t: round(sum(v) / len(v)) for t, v in by_type.items()}
    if len(avg) >= 2:
        best, other = max(avg, key=avg.get), min(avg, key=avg.get)
        if avg[other]:
            out.append(f"{best}s get {avg[best] / avg[other]:.1f}x the likes + comments of {other.lower()}s "
                       f"(averages {avg[best]:,} vs {avg[other]:,}).")
    if posts:
        share = max(by_type, key=lambda t: len(by_type[t]))
        out.append(f"{len(by_type[share])} of the {len(posts)} top posts are {share.lower()}s.")
        top = sorted(posts, key=_eng, reverse=True)[: max(1, len(posts) // 2)]
        out.append(f"The best half use about {sum(p['hashtags'] for p in top) / len(top):.0f} hashtags each.")
    return out


def _result(tag: str, top: list[dict], recent: list[dict], *, is_sample: bool, reason: str = "",
            checked_at=None) -> dict:
    top = sorted(top, key=_eng, reverse=True)[:10]
    return {"tag": tag, "sample": is_sample, "reason": reason, "live_enabled": is_live(),
            "top": [{**p, "engagement": _eng(p)} for p in top],
            "recent": [{**p, "engagement": _eng(p)} for p in recent[:6]],
            "findings": findings(top), "used": used_this_week(), "limit": WEEKLY_LIMIT, "checked_at": checked_at,
            "requirement": NEEDS,
            "idea_prompt": (f"Using what works for the top #{tag} posts on the Trends page as inspiration only (never "
                            "copy anyone's content), make an Instagram post for us. Ask me for our offer first."),
            "note": ("Sample posts, not real Instagram data. " if is_sample else "Public numbers from Instagram. ") +
                    "Findings are calculated from these numbers; they are hints, not guarantees."}


async def lookup(tag: str, client: MetaClient | None = None, force: bool = False) -> dict:
    """The card's data for one hashtag: real posts when Meta has approved the feature, otherwise sample posts."""
    t = clean(tag)
    if not is_live():
        why = ("Dry run: Milo doesn't call Instagram." if get_settings().meta_dry_run and
               get_settings().labs_hashtags_live else "")
        return _result(t, **sample(t), is_sample=True, reason=why)
    h = (store.find(HashtagSearch, {"tag": t}) or [None])[0]
    fresh = h and h.checked_at and not h.error and now() - h.checked_at < timedelta(hours=REFRESH_HOURS)
    if fresh and not force:
        return _result(t, h.top, h.recent, is_sample=False, checked_at=h.checked_at)
    counted = h and h.first_queried_at and now() - h.first_queried_at < timedelta(days=7)
    if not counted and used_this_week() >= WEEKLY_LIMIT:
        raise LabsError(f"Instagram allows {WEEKLY_LIMIT} different hashtags a week and this account has used them "
                        "all. Search a hashtag you've already used this week, or wait a few days.")
    own = client is None
    client = client or MetaClient()
    try:
        ig = await meta.instagram_user_id(client)
        hid = h.hashtag_id if counted and h.hashtag_id else ""
        if not hid:
            found = (await client.get("/ig_hashtag_search", user_id=ig, q=t)).get("data") or []
            if not found:
                raise LabsError(f"Instagram has no hashtag #{t}.")
            hid = found[0]["id"]
        top = (await client.get(f"/{hid}/top_media", user_id=ig, fields=MEDIA_FIELDS, limit=25)).get("data") or []
        recent = (await client.get(f"/{hid}/recent_media", user_id=ig, fields=MEDIA_FIELDS, limit=25)).get(
            "data") or []
        changes = {"hashtag_id": hid, "top": [_media(m) for m in top], "recent": [_media(m) for m in recent],
                   "checked_at": now(), "error": ""}
        if not counted:
            changes["first_queried_at"] = now()
        h = _save(t, h, changes)
        store.audit("labs_hashtag_searched", tag=t)
        return _result(t, h.top, h.recent, is_sample=False, checked_at=h.checked_at)
    except MetaAPIError as exc:
        text = str(exc).lower()
        if exc.code == 10 or "public content access" in text:
            return _result(t, **sample(t), is_sample=True, reason=APPROVAL_ERROR)
        if "limit" in text and "hashtag" in text:
            raise LabsError(f"Instagram's limit of {WEEKLY_LIMIT} hashtags a week is reached. Try again in a few "
                            "days.") from exc
        raise LabsError(f"Meta refused: {redact(exc)}"[:300]) from exc
    finally:
        if own:
            await client.aclose()


def _save(tag: str, h: HashtagSearch | None, changes: dict) -> HashtagSearch:
    if h:
        return store.update(HashtagSearch, h.id, changes)
    try:
        return store.insert(HashtagSearch(tag=tag, **changes))
    except DuplicateKeyError:  # a second request saved it first
        h = store.find(HashtagSearch, {"tag": tag})[0]
        return store.update(HashtagSearch, h.id, changes)

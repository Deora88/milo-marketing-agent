"""What works on the business's own Instagram account, from stored post results (app/social/insights.py).

Score per post: reach if Instagram shared it, else views, else likes + comments. Grouped by format, by day type
and time of day (India time), and by caption style. Shown only with at least MIN_POSTS published posts, and every
group says how many posts it is based on, so a single lucky post isn't presented as a pattern.
"""
import re
from datetime import timedelta, timezone

from app.db import store
from app.db.models import SocialPost, now

IST = timezone(timedelta(hours=5, minutes=30))  # India has no daylight saving time
DAYS = 30
MIN_POSTS = 3
SLOTS = [("Morning", 5, 12), ("Afternoon", 12, 17), ("Evening", 17, 21), ("Night", 21, 29)]
FORMATS = {"reel": "Reels", "carousel": "Carousels", "photo": "Photos", "story": "Stories"}
PRICE = re.compile(r"(₹|\brs\.?\s?\d|\binr\b|\d+\s?/-)", re.I)


def score(p: SocialPost) -> tuple[float | None, str]:
    i = p.insights or {}
    if i.get("reach") is not None:
        return float(i["reach"]), "reach"
    if i.get("views") is not None:
        return float(i["views"]), "views"
    if i.get("like_count") is not None or i.get("comments_count") is not None:
        return float((i.get("like_count") or 0) + (i.get("comments_count") or 0)), "likes + comments"
    return None, ""


def _slot(hour: int) -> str:
    for name, start, end in SLOTS:
        if start <= hour < end or start <= hour + 24 < end:
            return name
    return "Night"


def _avg(xs: list[float]) -> float:
    return round(sum(xs) / len(xs)) if xs else 0


def _group(rows: list[tuple[str, float]]) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for key, val in rows:
        groups.setdefault(key, []).append(val)
    return sorted(({"name": k, "posts": len(v), "avg": _avg(v)} for k, v in groups.items()),
                  key=lambda g: g["avg"], reverse=True)


def summary(days: int = DAYS) -> dict:
    since = now() - timedelta(days=days)
    posts = [p for p in store.find(SocialPost, {"status": "published", "meta_mode": "live"})
             if p.published_at and p.published_at >= since]
    scored = [(p, *score(p)) for p in posts]
    scored = [(p, s, m) for p, s, m in scored if s is not None]
    base = {"days": days, "posts": len(posts), "with_results": len(scored), "min_posts": MIN_POSTS}
    if len(scored) < MIN_POSTS:
        return {**base, "ready": False, "message": (
            f"Needs at least {MIN_POSTS} published Instagram posts with results from the last {days} days "
            f"(found {len(scored)}). Results arrive within an hour of posting; dry-run posts have none.")}
    metric = max({m for _, _, m in scored}, key=[m for _, _, m in scored].count)
    formats = _group([(FORMATS.get(p.kind, p.kind), s) for p, s, _ in scored])
    grid: dict[str, dict[str, list[float]]] = {"Weekday": {}, "Weekend": {}}
    for p, s, _ in scored:
        local = p.published_at.astimezone(IST)
        day = "Weekend" if local.weekday() >= 5 else "Weekday"
        grid[day].setdefault(_slot(local.hour), []).append(s)
    heat = {d: {name: {"avg": _avg(grid[d].get(name, [])), "posts": len(grid[d].get(name, []))}
                for name, _, _ in SLOTS} for d in grid}
    best_time = max(((d, n, c) for d, row in heat.items() for n, c in row.items() if c["posts"]),
                    key=lambda x: x[2]["avg"])
    styles = _group([("Short (under 120 characters)" if len(p.caption) < 120 else "Long (120+ characters)", s)
                     for p, s, _ in scored if p.kind != "story"])
    price = _group([("Shows a price" if PRICE.search(p.caption or "") else "No price", s)
                    for p, s, _ in scored if p.kind != "story"])
    top = sorted(scored, key=lambda x: x[1], reverse=True)[:3]
    best_format = formats[0]
    ratio = round(best_format["avg"] / formats[-1]["avg"], 1) if len(formats) > 1 and formats[-1]["avg"] else None
    caption_tip = " and ".join(g["name"].split(" (")[0].lower() for g in (styles[:1] + price[:1]) if g)
    idea = (f"Make an Instagram {best_format['name'].rstrip('s').lower()} for us to post on a "
            f"{best_time[0].lower()} {best_time[1].lower()}" + (f", with a {caption_tip} caption" if caption_tip else "")
            + ". Use only facts and prices I have given you.")
    return {**base, "ready": True, "metric": metric, "formats": formats, "heat": heat, "slots": [s[0] for s in SLOTS],
            "best_format": best_format, "best_format_ratio": ratio,
            "best_time": {"day": best_time[0], "slot": best_time[1], **best_time[2]},
            "caption_length": styles, "caption_price": price,
            "top_posts": [{"id": p.id, "kind": p.kind, "caption": p.caption[:120], "score": s, "metric": m,
                           "permalink": p.permalink} for p, s, m in top],
            "idea_prompt": idea,
            "note": f"Based on {len(scored)} posts. Averages of {metric}; a group with 1-2 posts is a hint, not a rule."}

"""Upcoming moments: Indian festivals and seasons with a ready post or ad idea.

Festival dates that follow the lunar calendar change every year, so they are listed per year and must be checked
each year. The 2026 dates were checked against a published 2026 festival calendar (hindutone.com,
"November 2026 festivals"). Fixed dates (Christmas, New Year, Republic Day...) repeat yearly.
Ideas never contain prices or offers: the prompt asks Milo to use only what the user gives it.
"""
from datetime import date, timedelta

WINDOW_DAYS = 60

# (start, end or None, name, idea for a post, idea for an ad)
DATED = [
    ("2026-11-06", None, "Dhanteras", "a Story about getting the car ready for the festive days", ""),
    ("2026-11-08", None, "Diwali", "a festive Story wishing customers Happy Diwali, with our offer if I give one",
     "a Diwali ad for people near us who want their car shining for the festival"),
    ("2026-11-11", None, "Bhai Dooj", "a carousel: gift a car wash to your brother or sister", ""),
    ("2026-11-15", None, "Chhath Puja", "a post about a quick interior clean before the trip home", ""),
    ("2026-11-24", None, "Guru Nanak Jayanti", "a respectful greeting Story (no selling)", ""),
]
YEARLY = [  # month-day, name, post idea, ad idea
    ("12-25", "Christmas", "a festive Story with a year-end thank-you to customers", ""),
    ("12-31", "New Year's Eve", "a Reel looking back at the year's best before/after cleans",
     "a New Year ad: start the year with a clean car"),
    ("01-26", "Republic Day", "a greeting Story (no selling)", ""),
    ("02-14", "Valentine's Day", "a playful post: a car date-night ready", ""),
]
SEASONS = [  # (start month-day, end month-day, name, post idea, ad idea)
    ("11-01", "01-31", "Winter dust and fog season (Delhi NCR)",
     "a carousel: 3 reasons to wash more often in dusty winter months",
     "a weekly wash plan ad for the dusty winter months"),
    ("11-01", "12-15", "Wedding season", "a post: car decorated and spotless for the wedding day",
     "an ad for a pre-wedding car clean"),
    ("06-15", "09-30", "Monsoon", "a Reel explaining an underbody wash after rain",
     "a monsoon underbody and interior anti-smell clean ad"),
    ("03-15", "06-15", "Summer", "a post about sun protection for the car's paint", ""),
]


def _prompt(kind: str, idea: str, name: str, business: str) -> str:
    what = "Instagram post" if kind == "post" else "Facebook and Instagram ad"
    return (f"Make a {what} for {business}: {idea} for {name}. Use only facts, prices and offers I give you; "
            "ask me for the offer first.")


def upcoming(today: date | None = None, business: str = "our business", days: int = WINDOW_DAYS) -> list[dict]:
    today = today or date.today()
    end = today + timedelta(days=days)
    out = []

    def add(start: date, finish: date | None, name: str, post: str, ad: str, kind: str):
        last = finish or start
        if last < today or start > end:
            return
        item = {"name": name, "start": start.isoformat(), "end": finish.isoformat() if finish else None,
                "kind": kind, "days_away": max(0, (start - today).days), "ongoing": start <= today <= last,
                "post_idea": post, "post_prompt": _prompt("post", post, name, business) if post else "",
                "ad_idea": ad, "ad_prompt": _prompt("ad", ad, name, business) if ad else ""}
        out.append(item)

    for s, e, name, post, ad in DATED:
        add(date.fromisoformat(s), date.fromisoformat(e) if e else None, name, post, ad, "festival")
    for year in (today.year, today.year + 1):
        for md, name, post, ad in YEARLY:
            add(date.fromisoformat(f"{year}-{md}"), None, name, post, ad, "date")
        for s, e, name, post, ad in SEASONS:
            start = date.fromisoformat(f"{year}-{s}")
            finish = date.fromisoformat(f"{year if e >= s else year + 1}-{e}")
            add(start, finish, name, post, ad, "season")
    seen, unique = set(), []
    for m in sorted(out, key=lambda m: (not m["ongoing"], m["start"], m["name"])):
        if (m["name"], m["start"]) not in seen:
            seen.add((m["name"], m["start"]))
            unique.append(m)
    return unique

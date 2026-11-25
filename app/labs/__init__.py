"""Trends page (prototype; code name "labs"): trends and ideas. Read-only: nothing here posts, publishes or spends.

- own.py: what works on the business's own Instagram account, from the post results Milo already stores.
- moments.py: upcoming Indian festivals and seasons, each with a ready idea for a post or an ad.
- competitors.py: top recent posts of public business accounts the user adds (Instagram Business Discovery API).
- ad_library_url(): a link to Meta's public Ad Library, already searched (Meta has no API for commercial ads).

Switched off with LABS_ENABLED=false. Every idea is only a chat prompt: Milo drafts it and the user approves.
"""
from urllib.parse import urlencode

from app.config import get_settings

VERSION = "0.1"


def ad_library_url(keyword: str, country: str | None = None) -> str:
    country = (country or get_settings().labs_ad_library_country or "IN").upper()
    q = {"active_status": "active", "ad_type": "all", "country": country, "q": keyword.strip()[:100],
         "search_type": "keyword_unordered", "media_type": "all"}
    return "https://www.facebook.com/ads/library/?" + urlencode(q)

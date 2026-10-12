"""Web research for ad copy, via the Tavily API (read-only).

Used by the MCP tools `research_topic` and `read_landing_page`. Results are *recent context* - pages
published in the chosen time window - not search-volume trend data.

Safety: everything returned from the web is untrusted text. It is trimmed, stripped of markup-like
noise, and handed to the model labelled as information to cite, never as instructions.
"""
import re

import httpx

from app.config import Settings, get_settings

SEARCH_URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"
SNIPPET_CHARS = 350
PAGE_CHARS = 2500


class ResearchUnavailable(Exception):
    """No API key, research switched off, or Tavily could not be reached."""


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"<[^>]{0,200}>", " ", text or "")  # drop stray HTML tags
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _check(s: Settings) -> None:
    if not s.research_enabled:
        raise ResearchUnavailable("Web research is switched off (RESEARCH_ENABLED=false).")
    if not s.tavily_api_key:
        raise ResearchUnavailable("No TAVILY_API_KEY in .env, so web research is not available.")


async def _post(url: str, body: dict, s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    headers = {"Authorization": f"Bearer {s.tavily_api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=25, transport=transport) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise ResearchUnavailable(f"Could not reach Tavily: {exc.__class__.__name__}") from exc
    if resp.status_code in (401, 403):
        raise ResearchUnavailable("Tavily rejected the API key.")
    if resp.status_code == 429 or resp.status_code == 432:
        raise ResearchUnavailable("Tavily credit or rate limit reached.")
    if resp.status_code >= 400:
        raise ResearchUnavailable(f"Tavily error {resp.status_code}.")
    return resp.json()


async def search_recent(topic: str, location: str = "", settings: Settings | None = None,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Recent pages about a topic (optionally a place). Returns sources with dates and short snippets."""
    s = settings or get_settings()
    _check(s)
    query = " ".join(x for x in (topic.strip(), location.strip()) if x)[:300]
    if not query:
        raise ResearchUnavailable("Give a topic to research.")
    data = await _post(SEARCH_URL, {
        "query": query,
        "topic": "general",
        "search_depth": "basic",  # 1 credit
        "max_results": s.research_max_results,
        "time_range": s.research_time_range,  # day | week | month | year
        "include_published_date": True,
        "include_answer": False,
        "include_raw_content": False,
    }, s, transport)
    sources = [
        {
            "title": _clean(r.get("title", ""), 140),
            "url": r.get("url", ""),
            "published_date": r.get("published_date"),
            "snippet": _clean(r.get("content", ""), SNIPPET_CHARS),
        }
        for r in data.get("results", [])
        if str(r.get("url", "")).startswith(("https://", "http://"))
    ]
    return {"query": query, "time_range": s.research_time_range, "sources": sources}


async def extract_page(url: str, settings: Settings | None = None,
                       transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Main text of one public https page (e.g. the advertiser's landing page)."""
    s = settings or get_settings()
    _check(s)
    if not url.startswith("https://"):
        raise ResearchUnavailable("Only https:// pages can be read.")
    data = await _post(EXTRACT_URL, {"urls": [url], "extract_depth": "basic"}, s, transport)
    results = data.get("results") or []
    if not results:
        raise ResearchUnavailable("Could not read that page.")
    return {"url": url, "text": _clean(results[0].get("raw_content", ""), PAGE_CHARS)}

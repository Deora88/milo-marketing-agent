"""Web research tools: Tavily calls are mocked with respx; nothing leaves the machine."""
import asyncio
import json

import httpx
import respx

from app.config import Settings
from app.mcp_server import server
from app.research import tavily

KEY = Settings(tavily_api_key="tvly-test", research_time_range="week", research_max_results=3)
SEARCH = {"results": [
    {"title": "Yoga studios in <b>Bengaluru</b> offer monsoon trials", "url": "https://news.example/yoga",
     "content": "Several studios in Indiranagar are running free first-week trials. " * 20, "published_date": "2026-10-01"},
    {"title": "bad", "url": "javascript:alert(1)", "content": "x"},
]}


@respx.mock
def test_search_recent_sends_window_and_trims_results():
    route = respx.post(tavily.SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH))
    out = asyncio.run(tavily.search_recent("yoga classes", "Bengaluru", settings=KEY))
    body = json.loads(route.calls[0].request.content)
    assert body["query"] == "yoga classes Bengaluru" and body["time_range"] == "week" and body["max_results"] == 3
    assert route.calls[0].request.headers["authorization"] == "Bearer tvly-test"
    assert len(out["sources"]) == 1  # non-http URL dropped
    s = out["sources"][0]
    assert "<b>" not in s["title"] and len(s["snippet"]) <= tavily.SNIPPET_CHARS + 1 and s["published_date"] == "2026-10-01"


def test_unavailable_without_key_or_when_disabled():
    for st in (Settings(tavily_api_key=""), Settings(tavily_api_key="k", research_enabled=False)):
        try:
            asyncio.run(tavily.search_recent("yoga", settings=st))
            raise AssertionError("expected ResearchUnavailable")
        except tavily.ResearchUnavailable:
            pass


@respx.mock
def test_errors_become_unavailable():
    respx.post(tavily.SEARCH_URL).mock(return_value=httpx.Response(432, json={}))
    try:
        asyncio.run(tavily.search_recent("yoga", settings=KEY))
        raise AssertionError("expected ResearchUnavailable")
    except tavily.ResearchUnavailable as exc:
        assert "limit" in str(exc)


@respx.mock
def test_landing_page_https_only_and_trimmed():
    respx.post(tavily.EXTRACT_URL).mock(return_value=httpx.Response(200, json={"results": [{"raw_content": "Fresh bread. " * 500}]}))
    out = asyncio.run(tavily.extract_page("https://example.com", settings=KEY))
    assert len(out["text"]) <= tavily.PAGE_CHARS + 1
    try:
        asyncio.run(tavily.extract_page("http://example.com", settings=KEY))
        raise AssertionError("expected ResearchUnavailable")
    except tavily.ResearchUnavailable:
        pass


@respx.mock
def test_mcp_tools_label_web_content_untrusted_and_degrade(monkeypatch):
    monkeypatch.setattr(tavily, "get_settings", lambda: KEY)
    respx.post(tavily.SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH))
    ok = asyncio.run(server.research_topic("yoga classes", "Bengaluru"))
    assert ok["status"] == "ok" and "untrusted" in ok["note"] and ok["sources"]
    monkeypatch.setattr(tavily, "get_settings", lambda: Settings(tavily_api_key=""))
    off = asyncio.run(server.research_topic("yoga classes"))
    assert off["status"] == "unavailable" and "without research" in off["message"]

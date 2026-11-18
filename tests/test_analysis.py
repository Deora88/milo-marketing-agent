"""Analytics: conversions/engagement from Meta Insights, this period vs the previous one, creatives compared,
and facts / calculations / assumptions kept apart."""
import asyncio
from datetime import date, timedelta

import httpx
import respx
from fastapi.testclient import TestClient

from app import analysis
from app.config import Settings
from app.db import store
from app.db.models import Campaign, MetricSnapshot
from app.main import app
from app.mcp_server.server import TOOL_NAMES, analyze_performance
from app.meta.client import MetaClient
from app.metrics.meta_insights import MetaInsightsProvider
from app.metrics.simulated import SimulatedInsightsProvider
from tests.conftest import WEB


def _camp(name, headline, destination="website", image=False):
    return store.insert(Campaign(name=name, daily_budget=20000, headline=headline, primary_text="p",
                                 link_url="https://e.com", destination=destination, status="active",
                                 image_path="x.jpg" if image else ""))


def _day(cid, d, spend, imp, clicks, lpv=0, conv=0, src="meta"):
    store.insert(MetricSnapshot(campaign_id=cid, date=d.isoformat(), spend=spend, impressions=imp, clicks=clicks,
                                link_clicks=int(clicks * 0.8), landing_page_views=lpv, conversations=conv,
                                source=src))


def _two_weeks():
    end = date.today() - timedelta(days=1)
    a = _camp("Wash weekdays", "Car wash at your doorstep", image=True)
    b = _camp("Wash chat", "Chat to book a wash", destination="messenger")
    for i in range(14):
        d = end - timedelta(days=i)
        this_week = i < 7
        _day(a.id, d, 200, 2000, 40 if this_week else 30, lpv=20 if this_week else 18)
        _day(b.id, d, 150, 1500, 15, conv=5 if this_week else 4)
    return a, b, end


def test_this_week_vs_last_week_with_facts_calculations_assumptions():
    a, b, end = _two_weeks()
    r = analysis.analyze(days=7)
    assert r["period"]["current"] == [(end - timedelta(days=6)).isoformat(), end.isoformat()]
    acc = r["account"]
    assert acc["current"]["clicks"] == 7 * 55 and acc["previous"]["clicks"] == 7 * 45
    assert acc["change_pct"]["clicks"] == round((55 - 45) / 45 * 100, 1)
    assert acc["metrics"]["results"] == 7 * 20 + 7 * 5  # landing page views + conversations
    ctr = next(c for c in r["calculations"] if c["name"] == "Account: CTR")
    assert ctr["formula"] == "clicks / impressions x 100" and ctr["inputs"] == "385 / 24,500"
    assert ctr["value"] == "1.57%"
    facts = {f["what"]: f["value"] for f in r["facts"]}
    assert facts["Data source"] == "Meta Insights" and facts["Landing page views"] == "140 / 126"
    assert any("attribution" in x for x in r["assumptions"])
    per = {c["name"]: c for c in r["campaigns"]}
    assert per["Wash chat"]["result"] == "conversation" and per["Wash chat"]["metrics"]["results"] == 35
    assert [c["headline"] for c in r["creatives"]] == ["Car wash at your doorstep", "Chat to book a wash"]
    assert all(c["enough_data"] for c in r["creatives"])
    assert any(f.startswith("Best creative by CTR") for f in r["findings"])


def test_new_campaign_without_previous_period_and_small_samples_are_flagged():
    c = _camp("New", "Brand new")
    _day(c.id, date.today(), 50, 300, 4, lpv=2, src="simulated")
    r = analysis.analyze(c.id, days=7)
    text = " ".join(r["assumptions"])
    assert "simulated" in text and "previous period" in text and "Fewer than" in text
    assert r["account"]["change_pct"]["ctr"] is None
    assert any(x["value"] == "n/a (no previous data)" for x in r["calculations"])
    assert r["creatives"][0]["enough_data"] is False


def test_no_data_and_unknown_campaign():
    assert analysis.analyze()["has_data"] is False
    assert analyze_performance(999)["status"] == "error"
    assert "analyze_performance" in TOOL_NAMES


def test_api_route():
    _two_weeks()
    c = TestClient(app, headers=WEB)
    r = c.get("/api/analysis?days=7").json()
    assert r["has_data"] and len(r["creatives"]) == 2
    assert c.get("/api/analysis?campaign_id=999").status_code == 404


@respx.mock
def test_meta_insights_reads_link_clicks_and_actions():
    s = Settings(_env_file=None, meta_dry_run=False, meta_access_token="T", meta_api_version="v26.0")
    respx.get(url__regex=r".*/c1/insights.*").mock(return_value=httpx.Response(200, json={"data": [{
        "date_start": "2026-11-01", "spend": "200.5", "impressions": "3000", "reach": "2100", "clicks": "60",
        "inline_link_clicks": "45", "actions": [
            {"action_type": "landing_page_view", "value": "30"},
            {"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "4"},
            {"action_type": "post_engagement", "value": "90"},
            {"action_type": "lead", "value": "2"}, {"action_type": "onsite_conversion.lead_grouped", "value": "2"},
            {"action_type": "video_view", "value": "500"}]}]}))
    c = Campaign(id=1, name="x", daily_budget=20000, meta_campaign_id="c1", meta_mode="live")
    p = MetaInsightsProvider(MetaClient(s))
    rows = asyncio.run(p.daily(c, days=14))
    r = rows[0]
    assert (r.link_clicks, r.landing_page_views, r.conversations, r.post_engagement, r.leads) == (45, 30, 4, 90, 2)


def test_simulated_metrics_include_results_per_destination():
    web = Campaign(id=7, name="w", daily_budget=20000, status="active", destination="website")
    chat = Campaign(id=8, name="m", daily_budget=20000, status="active", destination="messenger")
    w = asyncio.run(SimulatedInsightsProvider().daily(web, days=1))[0]
    m = asyncio.run(SimulatedInsightsProvider().daily(chat, days=1))[0]
    assert w.landing_page_views > 0 and w.conversations == 0 and w.link_clicks <= w.clicks
    assert m.conversations > 0 and m.landing_page_views == 0

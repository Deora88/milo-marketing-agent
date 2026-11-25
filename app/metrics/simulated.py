"""Simulated Insights for dry-run / demo mode (no Meta calls, no spend).

Deterministic per campaign: same campaign + date = same numbers, so the dashboard is stable.
Numbers scale with daily budget and drift slightly day to day. Clearly labelled "simulated" in the UI and in
optimizer recommendations. Only running (active) campaigns accrue numbers, and never before they were created.
"""
import random
from datetime import date, timedelta, timezone

from app.db.models import Campaign
from app.metrics.base import DailyMetrics


class SimulatedInsightsProvider:
    source = "simulated"

    def __init__(self, cpm: float = 120.0, base_ctr: float = 1.2):
        self.cpm = cpm  # cost per 1,000 impressions, account currency (major units)
        self.base_ctr = base_ctr  # %

    async def aclose(self) -> None:
        return None

    async def daily(self, campaign: Campaign, days: int = 7) -> list[DailyMetrics]:
        if campaign.status != "active":  # paused/draft campaigns do not deliver, so they do not spend
            return []
        # created_at is stored in UTC (MongoDB may return it without tzinfo); the days below use the local
        # calendar (date.today()), so compare on the local date. Otherwise, between local midnight and the UTC
        # offset, a new campaign looks one day older and gets numbers for the day before it existed.
        created_utc = campaign.created_at
        if created_utc.tzinfo is None:
            created_utc = created_utc.replace(tzinfo=timezone.utc)
        created = created_utc.astimezone().date()
        budget = campaign.daily_budget / 100  # minor -> major units
        # Each campaign gets a stable "quality" so some do better than others (gives the optimizer something to find).
        quality = random.Random(f"quality-{campaign.id}").uniform(0.45, 1.6)
        out = []
        for i in range(days - 1, -1, -1):  # includes today, so a new demo campaign shows numbers at once
            d = date.today() - timedelta(days=i)
            if d < created:
                continue
            rng = random.Random(f"{campaign.id}-{d.isoformat()}")
            spend = round(budget * rng.uniform(0.85, 1.0), 2)
            impressions = int(spend / self.cpm * 1000 * rng.uniform(0.9, 1.1))
            ctr = self.base_ctr * quality * rng.uniform(0.85, 1.15) / 100
            clicks = int(impressions * ctr)
            link_clicks = int(clicks * rng.uniform(0.7, 0.85))
            messenger = campaign.destination == "messenger"
            out.append(
                DailyMetrics(
                    date=d.isoformat(),
                    spend=spend,
                    impressions=impressions,
                    reach=int(impressions * rng.uniform(0.6, 0.8)),
                    clicks=clicks,
                    link_clicks=link_clicks,
                    landing_page_views=0 if messenger else int(link_clicks * rng.uniform(0.55, 0.8)),
                    conversations=int(link_clicks * rng.uniform(0.2, 0.35)) if messenger else 0,
                    post_engagement=int(clicks * rng.uniform(1.2, 1.8)),
                )
            )
        return out

"""Simulated Insights for sandbox accounts (Meta returns no Insights there).

Deterministic per campaign: same campaign + date = same numbers, so the dashboard is stable.
Numbers scale with daily budget and drift slightly day to day. Clearly labelled "simulated" in the UI.
"""
import random
from datetime import date, timedelta

from app.db.models import Campaign
from app.metrics.base import DailyMetrics


class SimulatedInsightsProvider:
    source = "simulated"

    def __init__(self, cpm: float = 120.0, base_ctr: float = 1.2):
        self.cpm = cpm  # cost per 1,000 impressions, account currency (major units)
        self.base_ctr = base_ctr  # %

    async def daily(self, campaign: Campaign, days: int = 7) -> list[DailyMetrics]:
        if campaign.status not in ("active", "paused"):
            return []
        budget = campaign.daily_budget / 100  # minor -> major units
        # Each campaign gets a stable "quality" so some do better than others (gives the optimizer something to find).
        quality = random.Random(f"quality-{campaign.id}").uniform(0.45, 1.6)
        out = []
        for i in range(days, 0, -1):
            d = date.today() - timedelta(days=i)
            rng = random.Random(f"{campaign.id}-{d.isoformat()}")
            spend = round(budget * rng.uniform(0.85, 1.0), 2)
            impressions = int(spend / self.cpm * 1000 * rng.uniform(0.9, 1.1))
            ctr = self.base_ctr * quality * rng.uniform(0.85, 1.15) / 100
            out.append(
                DailyMetrics(
                    date=d.isoformat(),
                    spend=spend,
                    impressions=impressions,
                    reach=int(impressions * rng.uniform(0.6, 0.8)),
                    clicks=int(impressions * ctr),
                )
            )
        return out

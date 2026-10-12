"""Real Meta Insights: GET /{campaign_id}/insights, one row per day."""
from app.db.models import Campaign
from app.meta.client import MetaClient
from app.metrics.base import DailyMetrics


class MetaInsightsProvider:
    source = "meta"

    def __init__(self, client: MetaClient):
        self.client = client

    async def daily(self, campaign: Campaign, days: int = 7) -> list[DailyMetrics]:
        if not campaign.meta_campaign_id:
            return []
        preset = {7: "last_7d", 14: "last_14d", 30: "last_30d"}.get(days, "last_7d")
        resp = await self.client.get(
            f"/{campaign.meta_campaign_id}/insights",
            fields="spend,impressions,reach,clicks",
            date_preset=preset,
            time_increment=1,
            level="campaign",
        )
        return [
            DailyMetrics(
                date=row["date_start"],
                spend=float(row.get("spend", 0)),
                impressions=int(row.get("impressions", 0)),
                reach=int(row.get("reach", 0)),
                clicks=int(row.get("clicks", 0)),
            )
            for row in resp.get("data", [])
        ]

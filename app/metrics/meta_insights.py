"""Real Meta Insights: GET /{campaign_id}/insights, one row per day, exact date range, all pages."""
from datetime import date, timedelta

from app.db.models import Campaign
from app.meta.client import MetaClient
from app.metrics.base import DailyMetrics

MAX_PAGES = 20


class MetaInsightsProvider:
    source = "meta"

    def __init__(self, client: MetaClient):
        self.client = client

    async def aclose(self) -> None:
        await self.client.aclose()

    async def daily(self, campaign: Campaign, days: int = 7) -> list[DailyMetrics]:
        if not campaign.meta_campaign_id or campaign.meta_mode != "live":
            return []
        days = max(1, min(int(days), 90))
        until = date.today() - timedelta(days=1)  # complete days only
        since = until - timedelta(days=days - 1)
        params = {
            "fields": "spend,impressions,reach,clicks",
            "time_range": {"since": since.isoformat(), "until": until.isoformat()},
            "time_increment": 1,
            "level": "campaign",
            "limit": 100,
        }
        rows: list[dict] = []
        resp = await self.client.get(f"/{campaign.meta_campaign_id}/insights", **params)
        for _ in range(MAX_PAGES):
            rows.extend(resp.get("data", []))
            after = ((resp.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not (resp.get("paging") or {}).get("next"):
                break
            resp = await self.client.get(f"/{campaign.meta_campaign_id}/insights", **params, after=after)
        return [
            DailyMetrics(
                date=row["date_start"],
                spend=float(row.get("spend", 0)),
                impressions=int(row.get("impressions", 0)),
                reach=int(row.get("reach", 0)),
                clicks=int(row.get("clicks", 0)),
            )
            for row in rows
        ]

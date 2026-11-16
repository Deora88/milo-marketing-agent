"""One interface for metrics, two implementations (Meta Insights / simulated)."""
from dataclasses import dataclass
from typing import Protocol

from app.db.models import Campaign


@dataclass
class DailyMetrics:
    date: str  # YYYY-MM-DD
    spend: float
    impressions: int
    reach: int
    clicks: int  # all clicks (Meta "clicks": link, profile, reactions...)
    # Conversions and engagement (Meta "inline_link_clicks" and "actions"; 0 when Meta reports none)
    link_clicks: int = 0  # clicks on the ad's link or button
    landing_page_views: int = 0  # the landing page actually loaded (website ads)
    conversations: int = 0  # Messenger conversations started (click-to-Messenger ads)
    leads: int = 0
    post_engagement: int = 0  # reactions, comments, shares, saves, clicks on the ad

    @property
    def ctr(self) -> float:
        return round(self.clicks / self.impressions * 100, 2) if self.impressions else 0.0

    @property
    def cpc(self) -> float:
        return round(self.spend / self.clicks, 2) if self.clicks else 0.0


class MetricsProvider(Protocol):
    source: str

    async def daily(self, campaign: Campaign, days: int = 7) -> list[DailyMetrics]: ...

    async def aclose(self) -> None: ...

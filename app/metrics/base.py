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
    clicks: int

    @property
    def ctr(self) -> float:
        return round(self.clicks / self.impressions * 100, 2) if self.impressions else 0.0

    @property
    def cpc(self) -> float:
        return round(self.spend / self.clicks, 2) if self.clicks else 0.0


class MetricsProvider(Protocol):
    source: str

    async def daily(self, campaign: Campaign, days: int = 7) -> list[DailyMetrics]: ...

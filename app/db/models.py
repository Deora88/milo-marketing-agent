"""Database tables: campaigns, approvals, metric snapshots, audit log."""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import JSON, Column, Field, SQLModel


def now() -> datetime:
    return datetime.now(timezone.utc)


class Campaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    objective: str = "OUTCOME_TRAFFIC"
    special_ad_category: str = "NONE"
    daily_budget: int  # minor units (paise / cents)
    countries: list[str] = Field(default_factory=lambda: ["IN"], sa_column=Column(JSON))
    age_min: int = 18
    age_max: int = 65
    # Optional local targeting: people within radius_km of a point (replaces country targeting when set).
    location_name: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_km: Optional[float] = None
    headline: str = ""
    primary_text: str = ""
    link_url: str = ""
    call_to_action: str = "LEARN_MORE"
    # draft -> pending_approval -> active / paused / rejected
    status: str = "draft"
    meta_campaign_id: Optional[str] = None
    meta_adset_id: Optional[str] = None
    meta_creative_id: Optional[str] = None
    meta_ad_id: Optional[str] = None
    created_at: datetime = Field(default_factory=now)


class Approval(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    action: str  # publish_campaign | pause_campaign | update_budget
    campaign_id: int = Field(foreign_key="campaign.id")
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    summary: str = ""
    reason: str = ""  # why it was requested (filled by the optimizer)
    source: str = "agent"  # agent | optimizer | user
    status: str = "pending"  # pending | approved | rejected
    created_at: datetime = Field(default_factory=now)
    decided_at: Optional[datetime] = None


class MetricSnapshot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    date: str  # YYYY-MM-DD
    spend: float = 0
    impressions: int = 0
    reach: int = 0
    clicks: int = 0
    ctr: float = 0
    cpc: float = 0
    source: str = "simulated"  # simulated | meta
    pulled_at: datetime = Field(default_factory=now)


class AuditLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event: str
    detail: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now)

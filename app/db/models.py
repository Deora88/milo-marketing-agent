"""Documents stored in MongoDB: campaigns, approvals, metric snapshots, audit log.

Plain Pydantic models. Ids are small sequential integers (from a `counters` collection) so URLs and approval
cards stay readable (#campaign/3, approval #12). Money is stored in minor units (paise).
"""
from datetime import datetime, timezone
from typing import ClassVar, Optional

from pydantic import BaseModel, Field, field_validator


def now() -> datetime:
    return datetime.now(timezone.utc)


class Doc(BaseModel):
    collection: ClassVar[str] = ""
    id: Optional[int] = None

    @field_validator("*", mode="after")
    @classmethod
    def _utc(cls, v):
        # MongoDB returns naive UTC datetimes; make them timezone-aware again.
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class Campaign(Doc):
    collection: ClassVar[str] = "campaigns"
    name: str
    objective: str = "OUTCOME_TRAFFIC"
    special_ad_category: str = "NONE"
    daily_budget: int  # minor units (paise)
    countries: list[str] = Field(default_factory=lambda: ["IN"])
    age_min: int = 18
    age_max: int = 65
    # Optional local targeting: people within radius_km of a point (replaces country targeting when set).
    location_name: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_km: Optional[float] = None
    # Where the ad runs: one ad set, Meta picks the best placements on these platforms.
    platforms: list[str] = Field(default_factory=lambda: ["facebook", "instagram"])
    headline: str = ""
    primary_text: str = ""
    link_url: str = ""
    call_to_action: str = "LEARN_MORE"
    # Where a click goes: "website" (link_url) or "messenger" (opens a chat with the Page, click-to-Messenger).
    destination: str = "website"
    welcome_message: str = ""  # Messenger only: first message people see in the chat (max 300 chars)
    image_path: str = ""  # local file uploaded in the UI; sent to Meta as an ad image on publish
    # draft -> pending_approval -> paused / active ; rejected publish -> draft
    status: str = "draft"
    meta_mode: str = ""  # "live" or "dry_run": how the meta_* ids below were created
    meta_campaign_id: Optional[str] = None
    meta_adset_id: Optional[str] = None
    meta_image_hash: Optional[str] = None
    meta_creative_id: Optional[str] = None
    meta_ad_id: Optional[str] = None
    created_at: datetime = Field(default_factory=now)


class Approval(Doc):
    collection: ClassVar[str] = "approvals"
    action: str  # publish_campaign | pause_campaign | update_budget
    campaign_id: int
    payload: dict = Field(default_factory=dict)
    summary: str = ""
    reason: str = ""  # why it was requested (filled by the optimizer)
    source: str = "agent"  # agent | optimizer | user
    status: str = "pending"  # pending | executing | approved | rejected
    created_at: datetime = Field(default_factory=now)
    decided_at: Optional[datetime] = None
    executing_since: Optional[datetime] = None  # set while Approve runs; lets a crashed run be recovered


class MetricSnapshot(Doc):
    collection: ClassVar[str] = "metric_snapshots"
    campaign_id: int
    date: str  # YYYY-MM-DD
    spend: float = 0
    impressions: int = 0
    reach: int = 0
    clicks: int = 0
    ctr: float = 0
    cpc: float = 0
    source: str = "meta"  # meta | simulated
    pulled_at: datetime = Field(default_factory=now)


class AuditLog(Doc):
    collection: ClassVar[str] = "audit_log"
    event: str
    detail: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)

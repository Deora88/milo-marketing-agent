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
    action: str  # publish_campaign | pause_campaign | update_budget | publish_post
    campaign_id: int = 0  # 0 for an Instagram post approval
    post_id: Optional[int] = None  # publish_post: the SocialPost to publish
    payload: dict = Field(default_factory=dict)
    summary: str = ""
    reason: str = ""  # why it was requested (filled by the optimizer)
    source: str = "agent"  # agent | optimizer | user
    status: str = "pending"  # pending | executing | approved | rejected
    created_at: datetime = Field(default_factory=now)
    decided_at: Optional[datetime] = None
    executing_since: Optional[datetime] = None  # set while Approve runs; lets a crashed run be recovered


class InboundMessage(Doc):
    """A DM (or postback) delivered by the Meta webhook. Stored first, answered later (DM agent, next step)."""
    collection: ClassVar[str] = "inbound_messages"
    platform: str  # "messenger" | "instagram"
    page_id: str = ""  # the Page / Instagram account that received it (entry.id)
    sender_id: str = ""  # page-scoped id of the person (PSID / IGSID)
    recipient_id: str = ""
    customer_id: str = ""  # the person in the conversation (sender of inbound, recipient of outbound)
    mid: str  # Meta's message id: unique, used to drop retries
    kind: str = "message"  # message | postback
    text: str = ""
    is_echo: bool = False  # a message the Page itself sent (outbound)
    referral: dict = Field(default_factory=dict)  # ad referral (ad_id, source) when the chat came from an ad
    sent_at: Optional[datetime] = None  # Meta's timestamp
    received_at: datetime = Field(default_factory=now)
    status: str = "new"  # inbound: new | read | answered | handed_off ; outbound: sent | dry_run
    # Instagram story replies / mentions: {"kind": "reply" | "mention", "url": "<story media URL>", "id": "..."}
    story: dict = Field(default_factory=dict)
    sent_by: str = ""  # outbound: "human" (Inbox) or "agent" (DM agent); "" = an echo Milo did not send itself
    app_id: str = ""  # echoes: the Meta app that sent the message (Milo's own app id, or e.g. Business Suite)
    # DM agent result for an inbound message: processing | answer | lead | handoff | skipped | failed,
    # a draft reply and why
    agent_action: str = ""
    suggested_reply: str = ""
    agent_note: str = ""
    handed_off: bool = False  # the agent passed this message to a person (keeps the bot out for DM_HANDOFF_HOURS)
    # a person clicked "Hand back to bot": hand-offs and Page replies before this time no longer keep the bot out
    bot_resumed_at: Optional[datetime] = None
    agent_started_at: Optional[datetime] = None  # when the agent claimed it ("processing")


class FaqEntry(Doc):
    """One question and answer the DM agent may use. It answers only from these."""
    collection: ClassVar[str] = "faq_entries"
    question: str
    answer: str
    enabled: bool = True
    sample: bool = False  # loaded from Milo's sample FAQ (not the business's own words yet)
    created_at: datetime = Field(default_factory=now)


class Lead(Doc):
    """Contact details a person shared in a DM, for a callback."""
    collection: ClassVar[str] = "leads"
    platform: str
    page_id: str
    customer_id: str
    name: str = ""
    phone: str = ""
    note: str = ""  # the message it came from
    created_at: datetime = Field(default_factory=now)


class SocialPost(Doc):
    """An organic Instagram post (not an ad): photo, carousel, Reel or Story. Published only after approval."""
    collection: ClassVar[str] = "social_posts"
    kind: str = "photo"  # photo | carousel | reel | story
    caption: str = ""  # stories have no caption on Instagram
    # Stories/Reels: text drawn onto the image (the API can't add text or stickers): headline, details, cta
    overlay: dict = Field(default_factory=dict)
    images: list[str] = Field(default_factory=list)  # local JPEG files, in order (data/posts/...)
    video_path: str = ""  # reels: MP4 built from the images when publish is requested
    media_token: str = ""  # random part of the public media URL Meta downloads from (PUBLIC_BASE_URL/media/...)
    scheduled_at: Optional[datetime] = None  # publish at this time (UTC); None = right after approval
    # draft -> pending_approval -> scheduled -> publishing -> published | failed ; rejected -> draft
    status: str = "draft"
    meta_mode: str = ""  # live | dry_run
    meta_media_id: str = ""
    permalink: str = ""
    error: str = ""
    source: str = "user"  # user | agent
    created_at: datetime = Field(default_factory=now)
    published_at: Optional[datetime] = None


class KnowledgeDoc(Doc):
    """A business PDF (price list, brochure, policies) uploaded in the Inbox. The DM bot may draft answers from it."""
    collection: ClassVar[str] = "knowledge_docs"
    filename: str
    size: int = 0  # bytes of the uploaded PDF (the PDF itself is not kept, only its text)
    sha256: str = ""  # catches the same file uploaded twice
    pages: list[str] = Field(default_factory=list)  # extracted text, one entry per page
    chars: int = 0
    enabled: bool = True
    created_at: datetime = Field(default_factory=now)


class CustomerProfile(Doc):
    """The name Meta shares for a person who messaged the Page (Messenger profile / Instagram user), cached."""
    collection: ClassVar[str] = "customer_profiles"
    platform: str
    customer_id: str  # PSID (Messenger) or IGSID (Instagram)
    name: str = ""
    username: str = ""  # Instagram only
    fetched_at: Optional[datetime] = None
    error: str = ""  # why the last lookup failed (redacted); retried after a day


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

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
    # Requirements: details Milo filled in without the user saying so -> why (app/campaign_state.py)
    assumptions: dict = Field(default_factory=dict)
    version: int = 1  # latest CampaignVersion number
    created_at: datetime = Field(default_factory=now)


class CampaignVersion(Doc):
    """One saved state of a campaign's ad, targeting and budget, and what changed from the version before."""
    collection: ClassVar[str] = "campaign_versions"
    campaign_id: int
    version: int
    snapshot: dict = Field(default_factory=dict)
    changes: dict = Field(default_factory=dict)  # field -> {"from": old, "to": new}; empty for version 1
    source: str = "agent"  # agent | user | approval | system
    note: str = ""
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
    requested_by: str = ""  # email of the signed-in person who asked ("" = the agent, the optimizer, or sign-in off)
    decided_by: str = ""  # email of the admin who approved or rejected


class User(Doc):
    """Someone who may sign in (app/auth.py). The password is stored only as a salted scrypt hash."""
    collection: ClassVar[str] = "users"
    email: str
    name: str = ""
    role: str = "editor"  # editor | admin (only an admin may approve, reject and manage users)
    salt: str = ""
    password_hash: str = ""
    disabled: bool = False
    created_at: datetime = Field(default_factory=now)
    last_login_at: Optional[datetime] = None


class Session(Doc):
    """A signed-in browser. Only the SHA-256 of the cookie value is kept."""
    collection: ClassVar[str] = "sessions"
    sid_hash: str
    user_id: int
    expires_at: datetime
    created_at: datetime = Field(default_factory=now)


class InboundMessage(Doc):
    """A DM (or postback) delivered by the Meta webhook. Stored first, then handled by the DM bot (app/dm/agent.py)."""
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
    send_started_at: Optional[datetime] = None  # the bot began sending its reply (a timeout after this may be sent)
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
    """An organic post (not an ad) for Instagram (photo, carousel, Reel, Story) or the LinkedIn Page (text, photo,
    multi_image). Published only after approval."""
    collection: ClassVar[str] = "social_posts"
    platform: str = "instagram"  # instagram | linkedin
    kind: str = "photo"  # instagram: photo | carousel | reel | story ; linkedin: text | photo | multi_image
    caption: str = ""  # stories have no caption on Instagram
    # Stories/Reels: text drawn onto the image (the API can't add text or stickers): headline, details, cta
    overlay: dict = Field(default_factory=dict)
    images: list[str] = Field(default_factory=list)  # local JPEG files, in order (data/posts/...)
    video_path: str = ""  # reels: MP4 built from the images when publish is requested
    media_token: str = ""  # random part of the public media URL Meta downloads from (PUBLIC_BASE_URL/media/...)
    scheduled_at: Optional[datetime] = None  # publish at this time (UTC); None = right after approval
    # draft -> pending_approval -> scheduled -> publishing -> published | failed ; rejected -> draft
    status: str = "draft"
    meta_mode: str = ""  # live | dry_run (for LinkedIn posts too: how it was published)
    meta_media_id: str = ""
    linkedin_post_urn: str = ""  # urn:li:share:... once on LinkedIn (saved first, so it is never posted twice)
    alt_text: str = ""  # LinkedIn: image description for screen readers (all images)
    permalink: str = ""
    error: str = ""
    source: str = "user"  # user | agent
    created_at: datetime = Field(default_factory=now)
    published_at: Optional[datetime] = None
    publishing_started_at: Optional[datetime] = None  # set when publishing starts; finds runs cut off by a crash
    # Results from Instagram (likes, views, reach...): {metric: number}; refreshed by the scheduler
    insights: dict = Field(default_factory=dict)
    insights_at: Optional[datetime] = None
    insights_note: str = ""  # why some numbers are missing (no permission yet, under 5 people, story expired)
    insights_blocked: bool = False  # the platform refused every number (no permission): checked daily, not hourly


class Comment(Doc):
    """A public comment on the business's Instagram post or Facebook Page post, delivered by the webhook.
    Replies are written by a person in Milo (the bot only drafts), unless COMMENT_AUTOREPLY=true lets the bot post
    pre-written words (FAQ answer / exact PDF sentences)."""
    collection: ClassVar[str] = "comments"
    platform: str  # "instagram" | "facebook"
    account_id: str  # Instagram account id or Page id the comment was made on (webhook entry.id)
    comment_id: str  # Meta's comment id: unique, used to drop retries
    media_id: str = ""  # Instagram media id, or the Facebook post id
    parent_id: str = ""  # set when this comment is itself a reply
    from_id: str = ""
    from_name: str = ""  # Facebook name or Instagram username
    text: str = ""
    own: bool = False  # written by the business itself (its replies come back through the webhook too)
    status: str = "new"  # new | read | replied | hidden | removed
    hidden: bool = False
    replies: list[dict] = Field(default_factory=list)  # {text, at, mode: live|dry_run, meta_id, by: person|bot}
    replying_at: Optional[datetime] = None  # a reply is being posted (blocks a second click; expires after 2 min)
    suggested_reply: str = ""
    agent_note: str = ""
    commented_at: Optional[datetime] = None
    received_at: datetime = Field(default_factory=now)


class Competitor(Doc):
    """Trends page: a public Instagram business/creator account the user follows up on (Business Discovery API).
    Only public numbers and captions are stored, to rank their posts; nothing is copied or re-posted."""
    collection: ClassVar[str] = "labs_competitors"
    username: str
    name: str = ""
    followers: int = 0
    media_count: int = 0
    media: list[dict] = Field(default_factory=list)  # {caption, likes, comments, type, permalink, timestamp}
    checked_at: Optional[datetime] = None
    error: str = ""
    added_at: datetime = Field(default_factory=now)


class HashtagSearch(Doc):
    """Trends page: one Instagram hashtag looked up with the Hashtag Search API (only after Meta approval).
    Meta allows 30 unique hashtags per rolling 7 days; first_queried_at is what counts against that limit."""
    collection: ClassVar[str] = "labs_hashtags"
    tag: str
    hashtag_id: str = ""
    first_queried_at: Optional[datetime] = None
    top: list[dict] = Field(default_factory=list)  # {caption, likes, comments, type, permalink, timestamp, hashtags}
    recent: list[dict] = Field(default_factory=list)
    checked_at: Optional[datetime] = None
    error: str = ""


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
    link_clicks: int = 0
    landing_page_views: int = 0
    conversations: int = 0
    leads: int = 0
    post_engagement: int = 0
    source: str = "meta"  # meta | simulated
    pulled_at: datetime = Field(default_factory=now)


class AuditLog(Doc):
    collection: ClassVar[str] = "audit_log"
    event: str
    detail: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)

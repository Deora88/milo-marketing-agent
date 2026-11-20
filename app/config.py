"""App settings, loaded from .env. Secrets never leave the backend."""
import logging
import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Which env file to read. Tests set MILO_ENV_FILE="" so a developer's personal .env never affects them
# (child processes such as the MCP server inherit it).
ENV_FILE = os.environ.get("MILO_ENV_FILE", ".env") or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    # LLM
    openrouter_api_key: str = ""
    milo_model: str = "openrouter/openrouter/free"

    # Meta
    meta_api_version: str = "v26.0"
    meta_access_token: str = ""
    meta_ad_account_id: str = ""
    meta_page_id: str = ""
    meta_page_name: str = ""  # shown on Milo's own ad mock-up
    # Instagram account linked to the Page (Instagram user id). Found automatically from the Page when empty.
    meta_instagram_account_id: str = ""
    # Optional: build ads from an existing Page post instead of a new one. Post id, "pageid_postid" or post URL.
    meta_page_post_id: str = ""
    meta_dry_run: bool = True  # true = never call Meta (log only)
    # Webhook for DMs (Messenger / Instagram). The app secret signs every event (X-Hub-Signature-256);
    # the verify token is the value you type in the App Dashboard when subscribing.
    meta_app_secret: str = ""
    # The app's own id (App Dashboard). Echo webhooks carry the id of the app that sent each Page message, so
    # Milo can tell its own replies from a person answering in Business Suite or the Instagram app.
    meta_app_id: str = ""
    meta_webhook_verify_token: str = ""
    # Replies from the Inbox use a Page access token (pages_messaging). Fetched from the Page with
    # META_ACCESS_TOKEN when empty. HUMAN_AGENT replies (up to 7 days) need Meta's Human Agent feature approved.
    meta_page_access_token: str = ""
    meta_human_agent_enabled: bool = False
    # DM agent: answers new DMs only from the FAQ, saves leads, hands everything else to a person.
    # dm_autoreply=false (default): it drafts a reply in the Inbox and a person clicks Send.
    dm_agent_enabled: bool = True
    dm_autoreply: bool = False
    # With DM_AUTOREPLY=true: also send answers from business PDFs automatically, but only as the PDF's own
    # sentences, checked word for word in code ("From our price list: ..."). false = PDF answers stay drafts.
    dm_pdf_autoreply: bool = False
    # Public comments: answer automatically from the FAQ or exact PDF sentences; a comment with a phone number gets
    # "please DM us" and is hidden; anything else waits for a person. Off by default (replies are public).
    comment_autoreply: bool = False
    # A bot hand-off (question not in the FAQ) keeps the bot out of that conversation for this many hours, then
    # it may answer FAQ questions again. A person replying as the Page keeps it out until "Hand back to bot".
    dm_handoff_hours: int = 24
    dm_business_name: str = "Milo Demo Home Care"

    # Spend safety (live ad account). With allow_spend=false Milo creates and edits campaigns on Meta but keeps
    # every ad PAUSED, so nothing delivers and nothing is charged. Resuming is refused until this is true.
    meta_allow_spend: bool = False
    max_daily_budget_inr: float = 500  # hard cap per campaign, for every request (agent, UI, optimizer)
    max_total_daily_budget_inr: float = 1000  # hard cap across all running campaigns

    # Metrics
    metrics_provider: str = "meta"  # meta (real Insights) | simulated (demo data, labelled in the UI)
    metrics_poll_minutes: int = 15
    approval_timeout_minutes: int = 10  # an approval "executing" longer than this goes back to pending

    # Optimizer: after each metrics pull, file recommendations as approvals (never applied automatically)
    optimizer_enabled: bool = True
    optimizer_min_impressions: int = 1000  # ignore campaigns with less data than this (7 days)
    optimizer_pause_ctr_ratio: float = 0.6  # pause if CTR < 60% of the account average
    optimizer_scale_ctr_ratio: float = 1.2  # scale up if CTR >= 120% of average and CPC <= average
    optimizer_trim_cpc_ratio: float = 1.3  # trim budget if CPC > 130% of the account average
    optimizer_budget_step_pct: int = 20  # size of a budget change
    optimizer_min_ctr_pct: float = 0.6  # with a single campaign, pause below this CTR
    optimizer_cooldown_hours: int = 6  # one recommendation per campaign per window (Meta: 4 budget edits/hour)

    # Trends page (prototype; code name "labs"). false hides the page, its API and the agent tool.
    labs_enabled: bool = True
    labs_ad_library_country: str = "IN"  # country for the Ad Library link
    # Hashtag trends need Meta's "Instagram Public Content Access" (verified Meta business + Tech Provider + App
    # Review). Leave false until Meta approves; the card then shows clearly labelled sample posts.
    labs_hashtags_live: bool = False
    labs_default_hashtag: str = "plantcare"

    # Web research for ad copy (Tavily). Read-only; "recent context", not search-volume trends.
    tavily_api_key: str = ""
    research_enabled: bool = True
    research_time_range: str = "month"  # day | week | month | year
    research_max_results: int = 5

    # Instagram posts (organic, not ads): Meta downloads the images/video from a public HTTPS address, so set
    # this to the tunnel or server address that reaches Milo, e.g. https://name.ngrok-free.dev (no trailing /).
    public_base_url: str = ""

    # LinkedIn Page posts (organic, Community Management API). The token is a 3-legged member token with
    # w_organization_social from a Page admin; it lasts 60 days. LINKEDIN_DRY_RUN=true = never call LinkedIn.
    linkedin_access_token: str = ""
    linkedin_organization_id: str = ""  # the Page's numeric id (or urn:li:organization:<id>)
    linkedin_api_version: str = "202610"  # LinkedIn-Version header, YYYYMM; each version is supported >= 1 year
    linkedin_dry_run: bool = True
    linkedin_token_expires_at: str = ""  # optional, e.g. 2027-01-18: the status page warns 7 days before

    # Ad image generation (Cloudflare Workers AI, FLUX.1 [schnell]; free daily allowance)
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    image_model: str = "@cf/black-forest-labs/flux-1-schnell"
    image_steps: int = 4  # 1-8; more = slower, slightly better
    image_daily_limit: int = 40  # keeps well inside the free allowance (~150 images/day)
    # PDF search for the DM / comment bot: meaning-based search with Cloudflare's bge-m3 embeddings (same account
    # as images), merged with keyword search. false, or no Cloudflare settings = keyword search (BM25) only.
    knowledge_embeddings: bool = True
    knowledge_min_similarity: float = 0.45  # passages less alike than this are not shown to the bot

    # Database: MongoDB (Atlas or local). "mongomock://" = in-memory, for tests only.
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "milo"

    # API protection. Every request that changes something must carry the X-Milo-Client header (blocks CSRF),
    # and the Host must be in allowed_hosts (blocks DNS rebinding). Set api_token if Milo runs beyond localhost.
    api_token: str = ""
    allowed_hosts: str = "localhost,127.0.0.1"
    # Sign-in (app/auth.py). true: everyone signs in with email + password; only an admin may approve, reject and
    # manage users. On first run the sign-in page lets you create the first admin (from this computer only).
    # false: no sign-in, as before (one person on their own computer).
    auth_required: bool = True
    session_hours: int = 12  # a sign-in lasts this long

    # App
    # Browser origins allowed to call the API besides the app's own origin (local dev servers only).
    cors_origin_regex: str = r"http://(localhost|127\.0\.0\.1)(:\d+)?"

    @model_validator(mode="after")
    def _warn_unsafe_dm_settings(self):
        """Log (not refuse) setting combinations that silently don't do what they say."""
        log = logging.getLogger("milo.config")
        if self.dm_autoreply and not self.dm_agent_enabled:
            log.warning("DM_AUTOREPLY=true has no effect while DM_AGENT_ENABLED=false.")
        if self.dm_autoreply and not self.meta_app_secret:
            log.warning("DM_AUTOREPLY=true but META_APP_SECRET is empty: the webhook refuses every DM.")
        if self.dm_autoreply and not self.meta_dry_run and not (self.meta_page_access_token or self.meta_access_token):
            log.warning("DM_AUTOREPLY=true but there is no Page token (META_PAGE_ACCESS_TOKEN / META_ACCESS_TOKEN).")
        if self.dm_autoreply and not self.meta_app_id:
            log.warning("DM_AUTOREPLY=true without META_APP_ID: every echo Milo can't match counts as a person "
                        "replying, so the bot may step back more often than needed.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

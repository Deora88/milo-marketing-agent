"""App settings, loaded from .env. Secrets never leave the backend."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    openrouter_api_key: str = ""
    milo_model: str = "openrouter/openrouter/free"

    # Meta
    meta_api_version: str = "v26.0"
    meta_access_token: str = ""
    meta_ad_account_id: str = ""
    meta_page_id: str = ""
    meta_page_name: str = ""  # shown on Milo's own ad mock-up
    # Optional: build ads from an existing Page post instead of a new one. Needed while the Meta app is in
    # Development mode, which blocks app-created ad posts (error 1885183). Post id, "pageid_postid" or post URL.
    meta_page_post_id: str = ""
    meta_dry_run: bool = True

    # Metrics
    metrics_provider: str = "simulated"  # simulated | meta
    metrics_poll_minutes: int = 15

    # Optimizer: after each metrics pull, file recommendations as approvals (never applied automatically)
    optimizer_enabled: bool = True
    optimizer_min_impressions: int = 1000  # ignore campaigns with less data than this (7 days)
    optimizer_pause_ctr_ratio: float = 0.6  # pause if CTR < 60% of the account average
    optimizer_scale_ctr_ratio: float = 1.2  # scale up if CTR >= 120% of average and CPC <= average
    optimizer_trim_cpc_ratio: float = 1.3  # trim budget if CPC > 130% of the account average
    optimizer_budget_step_pct: int = 20  # size of a budget change
    optimizer_max_daily_budget_inr: float = 5000  # never recommend above this
    optimizer_min_ctr_pct: float = 0.6  # with a single campaign, pause below this CTR
    optimizer_cooldown_hours: int = 6  # one recommendation per campaign per window (Meta: 4 budget edits/hour)

    # Web research for ad copy (Tavily). Read-only; "recent context", not search-volume trends.
    tavily_api_key: str = ""
    research_enabled: bool = True
    research_time_range: str = "month"  # day | week | month | year
    research_max_results: int = 5

    # App
    database_url: str = "sqlite:///./milo.db"
    # Browser origins allowed to call the API besides the app's own origin (local dev servers only).
    cors_origin_regex: str = r"http://(localhost|127\.0\.0\.1)(:\d+)?"


@lru_cache
def get_settings() -> Settings:
    return Settings()

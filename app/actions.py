"""Write actions that change live ads. Only ever run AFTER a human approves them.

The agent (through the MCP server's request_* tools) can only create an Approval row. This module executes it
when the user clicks Approve. Nothing else in the codebase calls Meta write endpoints.

Spend safety: with META_ALLOW_SPEND=false (default) a publish creates everything on Meta but leaves it PAUSED,
so nothing delivers and nothing is charged.
"""
from app.config import get_settings
from app.db import store
from app.db.models import Approval, Campaign
from app.meta import campaigns as meta
from app.meta.client import MetaClient

PLATFORM_LABEL = {"facebook": "Facebook", "instagram": "Instagram"}


def audience_label(c: Campaign) -> str:
    if c.radius_km and c.latitude is not None:
        return f"{c.radius_km:g} km around {c.location_name or f'{c.latitude:.4f},{c.longitude:.4f}'}"
    return ", ".join(c.countries)


def platforms_label(c: Campaign) -> str:
    if c.destination == "messenger":
        return "Facebook + Messenger"
    return " + ".join(PLATFORM_LABEL.get(p, p) for p in c.platforms)


def spend_blocked_reason() -> str | None:
    s = get_settings()
    if s.meta_dry_run or s.meta_allow_spend:
        return None
    return ("Spending is turned off (META_ALLOW_SPEND=false), so Milo keeps ads paused on Meta. "
            "Set META_ALLOW_SPEND=true only when the account owner agrees to spend.")


def total_live_budget(exclude_id: int | None = None) -> int:
    return sum(c.daily_budget for c in store.find(Campaign, {"status": "active"}) if c.id != exclude_id)


def describe(action: str, c: Campaign, payload: dict) -> str:
    """The approval card text: everything that will go live, so the user approves exactly that."""
    s = get_settings()
    if action == "publish_campaign":
        creative = (f"the existing Page post {s.meta_page_post_id} (its text and image replace Milo's copy)"
                    if s.meta_page_post_id else f'"{c.headline}" - "{c.primary_text}" -> '
                    + ("opens a Messenger chat with your Page" if c.destination == "messenger" else c.link_url))
        verb = "Resume" if c.status == "paused" else "Publish"
        note = "" if spend_blocked_reason() is None or c.status == "paused" else " Created PAUSED: spending is off."
        return (f"{verb} '{c.name}' on {platforms_label(c)} at Rs {c.daily_budget / 100:,.0f}/day to "
                f"{audience_label(c)}, ages {c.age_min}-{c.age_max}. Ad: {creative}.{note}")
    if action == "pause_campaign":
        return f"Pause '{c.name}'"
    if action == "update_budget":
        return f"Change '{c.name}' daily budget Rs {c.daily_budget / 100:,.0f} -> Rs {payload['daily_budget'] / 100:,.0f}"
    return f"{action} on '{c.name}'"


def _forget_dry_run_ids(c: Campaign, live: bool) -> None:
    """Ids made in dry-run mode are fake. In live mode treat the campaign as not created on Meta yet."""
    if live and c.meta_mode == "dry_run":
        c.meta_campaign_id = c.meta_adset_id = c.meta_image_hash = c.meta_creative_id = c.meta_ad_id = None
        c.meta_mode = ""


async def execute(approval: Approval, client: MetaClient | None = None) -> Campaign:
    client = client or MetaClient()
    s = client.s
    live = not s.meta_dry_run
    c = store.get(Campaign, approval.campaign_id)
    if c is None:
        raise ValueError("campaign not found")
    try:
        _forget_dry_run_ids(c, live)
        if approval.action == "publish_campaign":
            if not c.meta_ad_id:  # also resumes a publish that failed half-way
                c.meta_mode = "live" if live else "dry_run"
                try:
                    await meta.create_on_meta(client, c)
                finally:
                    store.save(c)  # keep the ids of objects that were created, so a retry resumes
            if spend_blocked_reason() is None:
                await meta.set_status(client, c, "ACTIVE")
                c.status = "active"
            else:
                c.status = "paused"  # on Meta, but not delivering: no spend
        elif approval.action in ("pause_campaign", "update_budget") and not c.meta_campaign_id:
            raise ValueError("This campaign is not on Meta yet (it was only published in dry-run mode). Publish it first.")
        elif approval.action == "pause_campaign":
            await meta.set_status(client, c, "PAUSED")
            c.status = "paused"
        elif approval.action == "update_budget":
            new = approval.payload["daily_budget"]
            from app.approvals import budget_cap_error  # re-check caps at execution time

            err = budget_cap_error(c, new, approval.action)
            if err:
                raise ValueError(err)
            await meta.update_budget(client, c, new)
            c.daily_budget = new
        else:
            raise ValueError(f"unknown action {approval.action}")
    finally:
        await client.aclose()
    store.save(c)
    store.audit("action_executed", approval_id=approval.id, action=approval.action, campaign_id=c.id,
                mode="live" if live else "dry_run", campaign_status=c.status)
    return c

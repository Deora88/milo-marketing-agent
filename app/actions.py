"""Write actions that change live ads. Only ever run AFTER a human approves them.

The agent (through the MCP server's request_* tools) can only create an Approval row. This module executes it
when the user clicks Approve. Nothing else in the codebase calls Meta write endpoints.
"""
from sqlmodel import Session

from app.db.models import Approval, Campaign
from app.db.session import audit
from app.meta import campaigns as meta
from app.meta.client import MetaClient

def audience_label(c: Campaign) -> str:
    if c.radius_km and c.latitude is not None:
        return f"{c.radius_km:g} km around {c.location_name or f'{c.latitude:.4f},{c.longitude:.4f}'}"
    return ", ".join(c.countries)


def describe(action: str, c: Campaign, payload: dict) -> str:
    if action == "publish_campaign":
        return f"Publish '{c.name}' at Rs {c.daily_budget / 100:,.0f}/day to {audience_label(c)}, ages {c.age_min}-{c.age_max}"
    if action == "pause_campaign":
        return f"Pause '{c.name}'"
    if action == "update_budget":
        return f"Change '{c.name}' daily budget Rs {c.daily_budget / 100:,.0f} -> Rs {payload['daily_budget'] / 100:,.0f}"
    return f"{action} on '{c.name}'"


async def execute(session: Session, approval: Approval, client: MetaClient | None = None) -> Campaign:
    client = client or MetaClient()
    c = session.get(Campaign, approval.campaign_id)
    if c is None:
        raise ValueError("campaign not found")
    try:
        if approval.action == "publish_campaign":
            if not c.meta_ad_id:  # also resumes a publish that failed half-way
                await meta.create_on_meta(client, c)
            await meta.set_status(client, c, "ACTIVE")
            c.status = "active"
        elif approval.action == "pause_campaign":
            await meta.set_status(client, c, "PAUSED")
            c.status = "paused"
        elif approval.action == "update_budget":
            await meta.update_budget(client, c, approval.payload["daily_budget"])
            c.daily_budget = approval.payload["daily_budget"]
        else:
            raise ValueError(f"unknown action {approval.action}")
    finally:
        await client.aclose()
    session.add(c)
    audit(session, "action_executed", approval_id=approval.id, action=approval.action, campaign_id=c.id)
    return c

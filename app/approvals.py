"""Creating approval requests. Shared by the MCP server (request_* tools), the UI buttons and the optimizer.

A request never changes a live ad. It stores a pending Approval; app/actions.py executes it only after a human
clicks Approve in the dashboard (POST /api/approvals/{id}/approve).
"""
from app.actions import describe, spend_blocked_reason, total_live_budget
from app.config import get_settings
from app.db import store
from datetime import timedelta

from app.db.models import Approval, Campaign, now

# action -> campaign statuses it may start from
ALLOWED_FROM = {
    "publish_campaign": {"draft", "paused"},
    "pause_campaign": {"active"},
    "update_budget": {"active", "paused"},
}


def to_minor(rupees: float) -> int:
    return int(round(rupees * 100))


def budget_cap_error(c: Campaign, new_budget: int, action: str) -> str | None:
    """Hard caps for every budget that could spend money, whoever asked (agent, UI or optimizer)."""
    s = get_settings()
    if new_budget > to_minor(s.max_daily_budget_inr):
        return f"Rs {new_budget / 100:,.0f}/day is above the Rs {s.max_daily_budget_inr:,.0f}/day cap (MAX_DAILY_BUDGET_INR)."
    will_run = action == "publish_campaign" or c.status == "active"
    if will_run:
        total = total_live_budget(exclude_id=c.id) + new_budget
        if total > to_minor(s.max_total_daily_budget_inr):
            return (f"Running campaigns would total Rs {total / 100:,.0f}/day, above the "
                    f"Rs {s.max_total_daily_budget_inr:,.0f}/day account cap (MAX_TOTAL_DAILY_BUDGET_INR).")
    return None


def create_approval(action: str, campaign_id: int, payload: dict | None = None, reason: str = "",
                    source: str = "agent") -> dict:
    payload = payload or {}
    if action not in ALLOWED_FROM:
        return {"status": "error", "message": f"unknown action {action}"}
    c = store.get(Campaign, campaign_id)
    if c is None:
        return {"status": "error", "message": f"Campaign {campaign_id} not found."}

    existing = store.find(Approval, {"campaign_id": c.id, "action": action, "status": {"$in": ["pending", "executing"]}})
    if existing:
        a = existing[0]
        if a.status == "pending" and payload and a.payload != payload:
            # Same kind of change, new details (e.g. a different budget): update the waiting request.
            err = budget_cap_error(c, payload.get("daily_budget", c.daily_budget), action)
            if err:
                return {"status": "error", "message": err}
            a.payload, a.summary, a.reason, a.source = payload, describe(action, c, payload), reason, source
            store.save(a)
            store.audit("approval_updated", approval_id=a.id, action=action, campaign_id=c.id, source=source)
            return {"status": "pending_approval", "approval_id": a.id, "summary": a.summary,
                    "message": "Updated the request that was already waiting. Not executed until the user approves."}
        return {"status": "pending_approval", "approval_id": a.id, "summary": a.summary,
                "message": "An approval for this is already waiting for the user."}

    if c.status not in ALLOWED_FROM[action]:
        return {"status": "error", "message": f"Cannot {action} a campaign that is '{c.status}'."}
    if action == "update_budget":
        if payload.get("daily_budget") == c.daily_budget:
            return {"status": "error", "message": "New budget equals the current budget."}
        err = budget_cap_error(c, payload["daily_budget"], action)
    elif action == "publish_campaign":
        err = budget_cap_error(c, c.daily_budget, action)
        if (not err and "instagram" in c.platforms and not c.image_path
                and not get_settings().meta_page_post_id and not c.meta_creative_id):
            err = ("Instagram ads need an image. Upload one on the campaign page, "
                   "or run the campaign on Facebook only.")
        if not err and c.status == "paused":
            err = spend_blocked_reason()  # resuming starts delivery = spends money
    else:
        err = None
    if err:
        return {"status": "error", "message": err}

    approval = store.insert(Approval(action=action, campaign_id=c.id, payload=payload,
                                     summary=describe(action, c, payload), reason=reason, source=source))
    if action == "publish_campaign" and c.status == "draft":
        c.status = "pending_approval"
        store.save(c)
    store.audit("approval_requested", approval_id=approval.id, action=action, campaign_id=c.id, source=source)
    return {
        "status": "pending_approval",
        "approval_id": approval.id,
        "summary": approval.summary,
        "message": "Not executed. The user must click Approve in the Milo dashboard.",
    }


def recover_stuck_approvals(older_than_minutes: int | None = None) -> list[int]:
    """Put approvals left in "executing" back to "pending" so they can be approved again or rejected.

    An approval is "executing" only while Approve runs. If the process dies mid-call it would stay there forever
    (approve and reject both answer 409). On startup every executing approval is stale (older_than_minutes=0);
    the scheduler also recovers ones older than APPROVAL_TIMEOUT_MINUTES. A retry is safe: publishing resumes from
    the Meta ids already saved and finds timed-out creates by their unique name, so nothing is duplicated."""
    minutes = get_settings().approval_timeout_minutes if older_than_minutes is None else older_than_minutes
    cutoff = now() - timedelta(minutes=minutes)
    recovered = []
    for a in store.find(Approval, {"status": "executing"}):
        if a.executing_since and a.executing_since > cutoff:
            continue  # still running
        expect = {"status": "executing", "executing_since": a.executing_since}
        if store.claim(Approval, a.id, expect, {"status": "pending", "executing_since": None}):
            store.audit("approval_recovered", approval_id=a.id, campaign_id=a.campaign_id,
                        stuck_since=a.executing_since.isoformat() if a.executing_since else None)
            recovered.append(a.id)
    return recovered

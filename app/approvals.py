"""Creating approval requests. Shared by the MCP server (request_* tools).

A request never changes a live ad. It stores a pending Approval; app/actions.py executes it only after a human
clicks Approve in the dashboard (POST /api/approvals/{id}/approve).
"""
from sqlmodel import Session, select

from app.actions import describe
from app.db.models import Approval, Campaign
from app.db.session import audit

# action -> campaign statuses it may start from
ALLOWED_FROM = {
    "publish_campaign": {"draft", "paused"},
    "pause_campaign": {"active"},
    "update_budget": {"active", "paused"},
}


def to_minor(rupees: float) -> int:
    return int(round(rupees * 100))


def create_approval(
    session: Session,
    action: str,
    campaign_id: int,
    payload: dict | None = None,
    reason: str = "",
    source: str = "agent",
) -> dict:
    payload = payload or {}
    if action not in ALLOWED_FROM:
        return {"status": "error", "message": f"unknown action {action}"}
    c = session.get(Campaign, campaign_id)
    if c is None:
        return {"status": "error", "message": f"Campaign {campaign_id} not found."}
    existing = session.exec(
        select(Approval).where(Approval.campaign_id == c.id, Approval.action == action, Approval.status == "pending")
    ).first()
    if existing:
        return {"status": "pending_approval", "approval_id": existing.id, "summary": existing.summary,
                "message": "An approval for this is already waiting for the user."}

    if c.status not in ALLOWED_FROM[action]:
        return {"status": "error", "message": f"Cannot {action} a campaign that is '{c.status}'."}
    if action == "update_budget" and payload.get("daily_budget") == c.daily_budget:
        return {"status": "error", "message": "New budget equals the current budget."}

    approval = Approval(
        action=action,
        campaign_id=c.id,
        payload=payload,
        summary=describe(action, c, payload),
        reason=reason,
        source=source,
    )
    session.add(approval)
    if action == "publish_campaign" and c.status == "draft":
        c.status = "pending_approval"
        session.add(c)
    session.commit()
    session.refresh(approval)
    audit(session, "approval_requested", approval_id=approval.id, action=action, campaign_id=c.id, source=source)
    session.commit()
    return {
        "status": "pending_approval",
        "approval_id": approval.id,
        "summary": approval.summary,
        "message": "Not executed. The user must click Approve in the Milo dashboard.",
    }

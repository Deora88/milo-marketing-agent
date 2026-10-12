from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import actions
from app.db.models import Approval, Campaign
from app.db.session import audit, get_session

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.get("")
def list_approvals(status: str = "pending", session: Session = Depends(get_session)):
    """status = pending | approved | rejected | all. Each row includes the campaign name."""
    query = select(Approval, Campaign.name).join(Campaign, Campaign.id == Approval.campaign_id)
    if status != "all":
        query = query.where(Approval.status == status)
    rows = session.exec(query.order_by(Approval.id.desc())).all()
    return [a.model_dump() | {"campaign_name": name} for a, name in rows]


def _pending(session: Session, approval_id: int) -> Approval:
    a = session.get(Approval, approval_id)
    if not a:
        raise HTTPException(404, "approval not found")
    if a.status != "pending":
        raise HTTPException(409, f"approval already {a.status}")
    return a


@router.post("/{approval_id}/approve")
async def approve(approval_id: int, session: Session = Depends(get_session)):
    a = _pending(session, approval_id)
    try:
        campaign = await actions.execute(session, a)
    except Exception as exc:
        audit(session, "action_failed", approval_id=a.id, error=str(exc))
        session.commit()
        raise HTTPException(502, f"Meta call failed: {exc}") from exc
    a.status, a.decided_at = "approved", datetime.now(timezone.utc)
    session.add(a)
    session.commit()
    session.refresh(a)
    session.refresh(campaign)
    return {"approval": a, "campaign": campaign}


@router.post("/{approval_id}/reject")
def reject(approval_id: int, session: Session = Depends(get_session)):
    a = _pending(session, approval_id)
    a.status, a.decided_at = "rejected", datetime.now(timezone.utc)
    c = session.get(Campaign, a.campaign_id)
    if a.action == "publish_campaign" and c and c.status == "pending_approval":
        c.status = "draft"
        session.add(c)
    session.add(a)
    audit(session, "approval_rejected", approval_id=a.id)
    session.commit()
    session.refresh(a)
    return {"approval": a}

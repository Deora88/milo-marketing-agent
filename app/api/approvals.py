from fastapi import APIRouter, HTTPException

from app import actions
from app.db import store
from app.db.models import Approval, Campaign, now
from app.util.redact import redact

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.get("")
def list_approvals(status: str = "pending"):
    """status = pending | approved | rejected | executing | all. Each row includes the campaign name."""
    rows = store.find(Approval, {} if status == "all" else {"status": status}, sort=[("_id", -1)])
    names = {c.id: c.name for c in store.find(Campaign, {"_id": {"$in": list({a.campaign_id for a in rows})}})}
    return [a.model_dump() | {"campaign_name": names.get(a.campaign_id, "")} for a in rows]


def _claim(approval_id: int, new_status: str) -> Approval:
    """Atomically move pending -> new_status. A second click, a retry or a concurrent reject gets 409."""
    a = store.claim(Approval, approval_id, {"status": "pending"}, {"status": new_status})
    if a is None:
        current = store.get(Approval, approval_id)
        if current is None:
            raise HTTPException(404, "approval not found")
        raise HTTPException(409, f"approval already {current.status}")
    return a


@router.post("/{approval_id}/approve")
async def approve(approval_id: int):
    a = _claim(approval_id, "executing")
    try:
        campaign = await actions.execute(a)
    except Exception as exc:  # noqa: BLE001 - any failure: record it and let the user retry
        store.claim(Approval, a.id, {"status": "executing"}, {"status": "pending"})
        store.audit("action_failed", approval_id=a.id, error=redact(exc))
        raise HTTPException(502, f"Meta call failed: {redact(exc)}") from exc
    a = store.claim(Approval, a.id, {"status": "executing"}, {"status": "approved", "decided_at": now()}) or a
    return {"approval": a, "campaign": campaign}


@router.post("/{approval_id}/reject")
def reject(approval_id: int):
    a = _claim(approval_id, "rejected")
    a.decided_at = now()
    store.save(a)
    c = store.get(Campaign, a.campaign_id)
    if a.action == "publish_campaign" and c and c.status == "pending_approval":
        c.status = "draft"
        store.save(c)
    store.audit("approval_rejected", approval_id=a.id)
    return {"approval": a}

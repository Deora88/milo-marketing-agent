from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, col, select

from app.approvals import create_approval, to_minor
from app.db.models import Approval, Campaign, MetricSnapshot
from app.db.session import get_session
from app.meta import campaigns as meta
from app.meta.client import MetaAPIError, MetaClient

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


@router.get("")
def list_campaigns(status: Optional[str] = None, q: Optional[str] = None, session: Session = Depends(get_session)):
    query = select(Campaign).order_by(Campaign.id.desc())
    if status and status != "all":
        query = query.where(Campaign.status == status)
    if q:
        query = query.where(col(Campaign.name).contains(q))
    return session.exec(query).all()


@router.get("/{campaign_id}")
def get_campaign(campaign_id: int, session: Session = Depends(get_session)):
    c = session.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    return c


@router.get("/{campaign_id}/metrics")
def campaign_metrics(campaign_id: int, session: Session = Depends(get_session)):
    rows = session.exec(
        select(MetricSnapshot).where(MetricSnapshot.campaign_id == campaign_id).order_by(MetricSnapshot.date)
    ).all()
    return {"source": rows[-1].source if rows else None, "daily": rows}


@router.get("/{campaign_id}/preview")
async def campaign_preview(campaign_id: int, format: str = "MOBILE_FEED_STANDARD",
                           session: Session = Depends(get_session)):
    """How Meta renders this campaign's ad. Read-only, so it needs no approval."""
    c = session.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    if format not in meta.PREVIEW_FORMATS:
        raise HTTPException(400, f"format must be one of {sorted(meta.PREVIEW_FORMATS)}")
    result = {"format": format, "formats": meta.PREVIEW_FORMATS, "available": False, "src": "", "reason": "",
              "source": ""}
    client = MetaClient()
    try:
        if client.s.meta_dry_run:
            result["reason"] = "Dry run is on, so Milo does not contact Meta. Set META_DRY_RUN=false for a real preview."
        elif c.meta_ad_id:
            src = await meta.get_preview_src(client, c.meta_ad_id, format)
            result.update(available=bool(src), src=src, source="ad",
                          reason="" if src else "Meta returned no preview for this format.")
        else:
            # No ad on Meta yet (draft, or Meta blocked the creative): Meta renders the draft without creating it.
            src = await meta.generate_preview_src(client, c, format)
            result.update(available=bool(src), src=src, source="draft",
                          reason="" if src else "Meta returned no preview for this format.")
    except MetaAPIError as exc:
        result["reason"] = f"Meta could not render a preview: {exc}"
    finally:
        await client.aclose()
    return result


@router.get("/{campaign_id}/approvals")
def campaign_approvals(campaign_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(Approval).where(Approval.campaign_id == campaign_id).order_by(Approval.id.desc())
    ).all()


class ChangeRequest(BaseModel):
    action: Literal["publish", "pause", "budget"]
    daily_budget_inr: Optional[float] = None


ACTION_MAP = {"publish": "publish_campaign", "pause": "pause_campaign", "budget": "update_budget"}


@router.post("/{campaign_id}/requests")
def request_change(campaign_id: int, body: ChangeRequest, session: Session = Depends(get_session)):
    """Buttons in the UI use this. Like the agent's request_* tools, it only creates a pending approval."""
    payload = {}
    if body.action == "budget":
        if body.daily_budget_inr is None or body.daily_budget_inr < 100:
            raise HTTPException(422, "daily_budget_inr must be at least 100")
        payload["daily_budget"] = to_minor(body.daily_budget_inr)
    result = create_approval(session, ACTION_MAP[body.action], campaign_id, payload, source="user")
    if result["status"] == "error":
        raise HTTPException(409, result["message"])
    return result

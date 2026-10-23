"""FAQ for the DM agent, and the leads it captured (localhost only, like every /api route)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import store
from app.db.models import FaqEntry, Lead
from app.dm import agent

router = APIRouter(prefix="/api", tags=["dm agent"])


class FaqIn(BaseModel):
    question: str = Field(min_length=3, max_length=300)
    answer: str = Field(min_length=2, max_length=1000)
    enabled: bool = True


@router.get("/faq")
def list_faq():
    return [f.model_dump() for f in store.find(FaqEntry, sort=[("_id", 1)])]


@router.post("/faq")
def add_faq(body: FaqIn):
    f = store.insert(FaqEntry(question=body.question.strip(), answer=body.answer.strip(), enabled=body.enabled))
    store.audit("faq_changed", change="added", faq_id=f.id)
    return f.model_dump()


@router.put("/faq/{faq_id}")
def edit_faq(faq_id: int, body: FaqIn):
    f = store.get(FaqEntry, faq_id)
    if f is None:
        raise HTTPException(404, "FAQ entry not found")
    f.question, f.answer, f.enabled, f.sample = body.question.strip(), body.answer.strip(), body.enabled, False
    store.save(f)
    store.audit("faq_changed", change="edited", faq_id=f.id)
    return f.model_dump()


@router.delete("/faq/{faq_id}")
def delete_faq(faq_id: int):
    if store.db()[FaqEntry.collection].delete_one({"_id": faq_id}).deleted_count == 0:
        raise HTTPException(404, "FAQ entry not found")
    store.audit("faq_changed", change="deleted", faq_id=faq_id)
    return {"deleted": faq_id}


@router.post("/faq/sample")
def sample_faq():
    """Add Milo's sample home-cleaning FAQ (entries already present are skipped)."""
    added = agent.load_sample_faq()
    if added:
        store.audit("faq_changed", change="sample loaded", count=added)
    return {"added": added}


@router.get("/leads")
def leads():
    return [x.model_dump() for x in store.find(Lead, sort=[("_id", -1)])]

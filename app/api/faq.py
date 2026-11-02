"""FAQ for the DM agent, and the leads it captured (localhost only, like every /api route)."""
import csv
import io
import re

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.db import store
from app.db.models import FaqEntry, Lead, now
from app.dm import agent, profiles

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


def _lead_rows() -> list[dict]:
    """Leads, newest first, with the sender's name and id from the Meta profile lookup."""
    cache = profiles.all_by_key()
    rows = []
    for x in store.find(Lead, sort=[("_id", -1)]):
        d = x.model_dump()
        d["sender"] = profiles.display(x.platform, x.customer_id, cache)
        rows.append(d)
    return rows


@router.get("/leads")
def leads():
    return _lead_rows()


def _cell(value) -> str:
    """One CSV cell, safe to open in Excel: a value starting with = + - @ is shown as text, not run as a
    formula (CSV injection). Phone numbers like +919871701615 stay as they are."""
    text = "" if value is None else str(value)
    if re.fullmatch(r"\+?\d[\d\s-]*", text):
        return text
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/leads.csv")
def leads_csv():
    """All leads as a CSV file for Excel (UTF-8 with BOM, so Hindi names show correctly)."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Captured at (UTC)", "Name", "Phone", "Platform", "Sender ID", "Message"])
    for r in _lead_rows():
        name = r["name"] or r["sender"]["name"] or (f"@{r['sender']['username']}" if r["sender"]["username"] else "")
        when = r["created_at"].strftime("%Y-%m-%d %H:%M") if r.get("created_at") else ""
        w.writerow([_cell(v) for v in (when, name, r["phone"], r["platform"], r["customer_id"], r["note"])])
    filename = f"milo-leads-{now().strftime('%Y-%m-%d')}.csv"
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})

"""Business PDFs for the DM bot (localhost only, like every /api route): upload, read, switch on/off, delete."""
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.db import store
from app.db.models import KnowledgeDoc
from app.dm import knowledge

log = logging.getLogger("milo.knowledge")
router = APIRouter(prefix="/api/knowledge", tags=["dm agent"])


class DocPatch(BaseModel):
    enabled: bool


@router.get("")
def list_docs():
    """Uploaded PDFs, newest first (without their full text)."""
    return [knowledge.summary(d) for d in store.find(KnowledgeDoc, sort=[("_id", -1)])]


@router.post("")
async def upload(file: UploadFile, background: BackgroundTasks):
    """Upload one PDF. Milo keeps only its text; the DM bot may draft answers from it."""
    data = await file.read(knowledge.MAX_BYTES + 1)
    if len(data) > knowledge.MAX_BYTES:
        raise HTTPException(413, f"PDF is larger than {knowledge.MAX_BYTES // (1024 * 1024)} MB.")
    try:
        # pypdf + database work in a worker thread: a slow PDF never stalls webhooks (Meta wants a reply in 5 s)
        doc = await run_in_threadpool(knowledge.add_document, file.filename or "document.pdf", data)
    except knowledge.DocError as exc:
        raise HTTPException(400, str(exc)) from exc
    background.add_task(_warm_embeddings, doc.id)  # so the first DM about it doesn't wait for embeddings
    return knowledge.summary(doc)


async def _warm_embeddings(doc_id: int) -> None:
    from app.dm import embeddings

    doc = store.get(KnowledgeDoc, doc_id)
    if doc is None or not embeddings.enabled():
        return
    try:
        await embeddings.vectors_for([p["text"] for p in knowledge.passages(doc)])
    except embeddings.EmbeddingError as exc:  # search still works with keywords; vectors are made on first use
        log.warning("knowledge: could not embed document %s now (%s)", doc_id, exc)


def _get(doc_id: int) -> KnowledgeDoc:
    doc = store.get(KnowledgeDoc, doc_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


@router.get("/{doc_id}")
def read_doc(doc_id: int):
    """The extracted text, page by page: exactly what the DM bot can see."""
    doc = _get(doc_id)
    return {**knowledge.summary(doc), "pages": doc.pages}


@router.patch("/{doc_id}")
def toggle(doc_id: int, body: DocPatch):
    _get(doc_id)
    doc = store.update(KnowledgeDoc, doc_id, {"enabled": body.enabled})
    store.audit("knowledge_doc_changed", doc_id=doc_id, enabled=body.enabled)
    return knowledge.summary(doc)


@router.delete("/{doc_id}")
def delete(doc_id: int):
    if store.db()[KnowledgeDoc.collection].delete_one({"_id": doc_id}).deleted_count == 0:
        raise HTTPException(404, "Document not found")
    from app.dm import embeddings

    embeddings.forget_unused([p["text"] for d in store.find(KnowledgeDoc) for p in knowledge.passages(d)])
    store.audit("knowledge_doc_changed", doc_id=doc_id, deleted=True)
    return {"deleted": doc_id}

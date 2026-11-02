"""Business PDFs for the DM bot: upload checks, text extraction, and finding the passages that match a DM.

- Only the extracted text is stored (one entry per page), never the PDF file itself.
- Scanned PDFs (images, no text layer) are refused: OCR is not part of Milo.
- Each page is split into short passages with ids like "D3-2" (document 3, passage 2). The DM bot sees only the
  few passages that share the most words with the customer's message, and must cite their ids (checked in code).
"""
import hashlib
import io
import logging
import re

from app.db import store
from app.db.models import KnowledgeDoc

log = logging.getLogger("milo.knowledge")

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 100
MAX_CHARS = 200_000  # extracted text kept per document
PASSAGE_CHARS = 700
MAX_PASSAGES = 4  # passages shown to the model per DM
MIN_TEXT = 20  # less text than this in the whole PDF = scanned / image-only

_WORD = re.compile(r"[a-z0-9ऀ-ॿ]+")
_STOP = set("""a an and are as at be by can do does for from have how i in is it its me my of on or our the their
there this to us we what when where which who will with you your yes no please hi hello hey thanks ok kya hai
""".split())


class DocError(ValueError):
    """Upload refused; the message is shown to the user."""


def extract_pages(data: bytes) -> list[str]:
    """Text of each page. Raises DocError for anything that isn't a readable, text-based PDF within the limits."""
    if len(data) > MAX_BYTES:
        raise DocError(f"PDF is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    if not data.lstrip()[:5] == b"%PDF-":
        raise DocError("That file is not a PDF.")
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocError("The PDF is password-protected. Upload a copy without a password.")
        if len(reader.pages) > MAX_PAGES:
            raise DocError(f"The PDF has {len(reader.pages)} pages; the limit is {MAX_PAGES}.")
        pages = [_clean(p.extract_text() or "") for p in reader.pages]
    except DocError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("pdf: could not read upload: %s", type(exc).__name__)
        raise DocError("Milo couldn't read this PDF (it may be damaged). Try saving it again as PDF.") from exc
    if sum(len(p) for p in pages) < MIN_TEXT:
        raise DocError("No text found in this PDF. It looks scanned (pictures of pages); Milo can only read PDFs "
                       "with real text. Export it again from Word/Docs, or type the answers into the FAQ.")
    out, total = [], 0
    for p in pages:  # cap the stored text, whole pages first
        if total >= MAX_CHARS:
            break
        out.append(p[:MAX_CHARS - total])
        total += len(out[-1])
    return out


def _clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def add_document(filename: str, data: bytes) -> KnowledgeDoc:
    digest = hashlib.sha256(data).hexdigest()
    same = store.find(KnowledgeDoc, {"sha256": digest})
    if same:
        raise DocError(f"This PDF is already uploaded as \"{same[0].filename}\".")
    pages = extract_pages(data)
    name = re.sub(r"[^\w .()\-]+", "_", (filename or "document.pdf").rsplit("/", 1)[-1].rsplit("\\", 1)[-1])[:120]
    doc = store.insert(KnowledgeDoc(filename=name or "document.pdf", size=len(data), sha256=digest, pages=pages,
                                    chars=sum(len(p) for p in pages)))
    store.audit("knowledge_doc_added", doc_id=doc.id, pages=len(pages), chars=doc.chars)
    return doc


def _split(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    """Short passages on paragraph / sentence boundaries where possible."""
    parts, cur = [], ""
    for piece in re.split(r"(?<=[.!?])\s+|\n\n", text):
        piece = piece.strip()
        if not piece:
            continue
        while len(piece) > size:  # one very long sentence (tables, lists)
            cut = piece.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if cur and len(cur) + 1 + len(piece) > size:
            parts.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}".strip()
    if cur:
        parts.append(cur)
    return parts


def passages(doc: KnowledgeDoc) -> list[dict]:
    out, n = [], 0
    for page_no, text in enumerate(doc.pages, start=1):
        for chunk in _split(text):
            n += 1
            out.append({"ref": f"D{doc.id}-{n}", "doc_id": doc.id, "filename": doc.filename, "page": page_no,
                        "text": chunk})
    return out


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in _STOP}


def search(question: str, limit: int = MAX_PASSAGES) -> list[dict]:
    """The passages from enabled documents that share the most words with the question (none if no overlap)."""
    q = _words(question)
    if not q:
        return []
    scored = []
    for doc in store.find(KnowledgeDoc, {"enabled": True}, sort=[("_id", 1)]):
        for i, p in enumerate(passages(doc)):
            hits = len(q & _words(p["text"]))
            if hits:
                scored.append((-hits, doc.id, i, p))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored[:limit]]


def find_passages(refs: list[str]) -> dict[str, dict]:
    """ref -> passage, for refs that exist in an enabled document."""
    want = {r for r in refs if isinstance(r, str)}
    ids = {int(m.group(1)) for r in want if (m := re.fullmatch(r"D(\d+)-\d+", r))}
    out = {}
    for doc_id in ids:
        doc = store.get(KnowledgeDoc, doc_id)
        if doc and doc.enabled:
            out.update({p["ref"]: p for p in passages(doc) if p["ref"] in want})
    return out


def summary(doc: KnowledgeDoc) -> dict:
    """For the document list: everything except the full text."""
    d = doc.model_dump(exclude={"pages"})
    d["page_count"] = len(doc.pages)
    d["preview"] = next((p for p in doc.pages if p), "")[:200]
    return d

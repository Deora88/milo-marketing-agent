"""Business PDFs for the DM bot: upload checks, text extraction, and finding the passages that match a DM.

- Only the extracted text is stored (one entry per page), never the PDF file itself.
- Scanned PDFs (images, no text layer) are refused: OCR is not part of Milo.
- Each page is split into short passages with ids like "D3-2" (document 3, passage 2). Neighbouring passages
  overlap by a sentence or two, so a service name and its price (often on the next line) land in the same passage.
- search(): BM25 ranking (the standard keyword formula: rare words count more, long passages don't win by size)
  with light stemming ("Sundays" = "Sunday") and a few common synonyms (GPay = UPI). hybrid_search() adds meaning-
  based search with embeddings (app/dm/embeddings.py) when Cloudflare is configured, and falls back to BM25 alone.
- The DM bot sees the top passages and must cite their ids (checked in code).
"""
import hashlib
import io
import logging
import math
import re

from app.db import store
from app.db.models import KnowledgeDoc

log = logging.getLogger("milo.knowledge")

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 100
MAX_CHARS = 200_000  # extracted text kept per document
PASSAGE_CHARS = 700
OVERLAP_CHARS = 200  # up to this much of the previous passage's end starts the next one
MAX_PASSAGES = 4  # passages shown to the model per DM
RRF_K = 60  # rank fusion constant (the usual value): 1 / (60 + rank) from each ranking
MIN_TEXT = 20  # less text than this in the whole PDF = scanned / image-only

_WORD = re.compile(r"[a-z0-9ऀ-ॿ]+")
_STOP = set("""a an and are as at be by can do does for from have how i in is it its me my of on or our the their
there this to us we what when where which who will with you your yes no please hi hello hey thanks ok kya hai
""".split())
# Words customers use that business documents usually don't (searched as well as the customer's own word)
SYNONYMS = {"gpay": "upi", "phonepe": "upi", "paytm": "upi", "subscription": "plan", "membership": "plan",
            "discount": "off", "fur": "hair", "rains": "rain", "raining": "rain", "refund": "refund money",
            "timing": "timings hours", "open": "timings", "charges": "price", "cost": "price", "rate": "price"}


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


def _pieces(text: str, size: int) -> list[str]:
    """Sentences / paragraphs, with any piece longer than `size` cut at a space."""
    out = []
    for piece in re.split(r"(?<=[.!?])\s+|\n\n", text):
        piece = piece.strip()
        while len(piece) > size:  # one very long sentence (tables, lists)
            cut = piece.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            out.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            out.append(piece)
    return out


def _split(text: str, size: int = PASSAGE_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Passages of at most `size` characters on sentence boundaries. Each passage after the first starts with the
    last sentence(s) of the one before (up to `overlap` characters), so a fact cut at a boundary is whole in one."""
    parts: list[str] = []
    cur: list[str] = []
    for piece in _pieces(text, size):
        if cur and len(" ".join(cur + [piece])) > size:
            parts.append(" ".join(cur))
            tail: list[str] = []
            for prev in reversed(cur):  # carry the end of this passage into the next
                if len(" ".join([prev] + tail)) > overlap or len(" ".join([prev] + tail + [piece])) > size:
                    break
                tail.insert(0, prev)
            cur = tail
        cur.append(piece)
    if cur:
        parts.append(" ".join(cur))
    return parts


def passages(doc: KnowledgeDoc) -> list[dict]:
    out, n = [], 0
    for page_no, text in enumerate(doc.pages, start=1):
        for chunk in _split(text):
            n += 1
            out.append({"ref": f"D{doc.id}-{n}", "doc_id": doc.id, "filename": doc.filename, "page": page_no,
                        "text": chunk})
    return out


def _stem(w: str) -> str:
    for suf in ("ing", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _words(text: str) -> set[str]:
    return set(_tokens(text))


def _tokens(text: str, synonyms: bool = False) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower()):
        if len(w) <= 2 or w in _STOP:
            continue
        out.append(_stem(w))
        if synonyms and w in SYNONYMS:
            out += [_stem(x) for x in SYNONYMS[w].split()]
    return out


def all_passages() -> list[dict]:
    """Every passage of the enabled documents, in a stable order."""
    return [p for doc in store.find(KnowledgeDoc, {"enabled": True}, sort=[("_id", 1)]) for p in passages(doc)]


def bm25_rank(question: str, ps: list[dict], k1: float = 1.5, b: float = 0.75) -> list[tuple[float, dict]]:
    """(score, passage) for passages that share at least one word with the question, best first."""
    q = list(dict.fromkeys(_tokens(question, synonyms=True)))
    if not q or not ps:
        return []
    docs = [_tokens(p["text"]) for p in ps]
    n, avg = len(docs), (sum(map(len, docs)) / len(docs)) or 1
    df: dict[str, int] = {}
    for d in docs:
        for w in set(d):
            df[w] = df.get(w, 0) + 1
    scored = []
    for p, d in zip(ps, docs, strict=True):
        score = 0.0
        for w in q:
            f = d.count(w)
            if f:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(d) / avg))
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda t: -t[0])  # stable: ties keep document order
    return scored


def search(question: str, limit: int = MAX_PASSAGES) -> list[dict]:
    """Keyword search (BM25): the passages that best match the question's words (none if no word matches)."""
    return [p for _, p in bm25_rank(question, all_passages())[:limit]]


async def hybrid_search(question: str, limit: int = MAX_PASSAGES, settings=None, transport=None) -> list[dict]:
    """BM25 and embeddings, merged by reciprocal rank fusion. Embeddings find reworded and Hinglish questions
    ("gaadi ki dhulai" = car wash); BM25 keeps exact names and prices on top. Without Cloudflare, or if it fails,
    this is BM25 alone, so the bot keeps working."""
    from app.dm import embeddings

    ps = all_passages()
    keyword = [p for _, p in bm25_rank(question, ps)]
    try:
        meaning = await embeddings.rank(question, ps, settings=settings, transport=transport)
    except embeddings.EmbeddingError as exc:
        log.warning("knowledge: embeddings unavailable, keyword search only (%s)", exc)
        meaning = []
    if not meaning:
        return keyword[:limit]
    score: dict[str, float] = {}
    by_ref = {p["ref"]: p for p in ps}
    for ranking in (keyword, meaning):
        for rank, p in enumerate(ranking):
            score[p["ref"]] = score.get(p["ref"], 0) + 1 / (RRF_K + rank + 1)
    return [by_ref[r] for r in sorted(score, key=lambda r: -score[r])[:limit]]


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

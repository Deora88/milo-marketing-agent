"""Meaning-based search for business PDFs: Cloudflare Workers AI embeddings (@cf/baai/bge-m3, multilingual).

An embedding is a list of numbers that places a text by its meaning, so "gaadi ki dhulai" lands near "car wash"
and "subscription" near "monthly plan" even with no shared words. Each passage is embedded once and cached in
MongoDB (collection knowledge_vectors, keyed by model + a hash of the text), so a re-upload or a restart costs
nothing; only the customer's question is embedded per DM, and it is not stored.

Cost: bge-m3 uses about 1,075 Neurons per million tokens and Cloudflare gives 10,000 Neurons a day free, so a
small business stays far inside the free allowance (it shares the account with ad image generation).
Uses CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN (the same as image generation). KNOWLEDGE_EMBEDDINGS=false or
no Cloudflare settings = keyword search only. Any failure raises EmbeddingError and the caller falls back.
"""
import hashlib
import logging
import math

import httpx

from app.config import Settings, get_settings
from app.db import store
from app.util.redact import redact

log = logging.getLogger("milo.embeddings")
MODEL = "@cf/baai/bge-m3"
COLLECTION = "knowledge_vectors"
BATCH = 50  # texts per request
TIMEOUT = 8.0  # seconds; a DM never waits long for search


class EmbeddingError(RuntimeError):
    """Embeddings could not be used now (not configured, network, Cloudflare error)."""


def enabled(s: Settings | None = None) -> bool:
    s = s or get_settings()
    return bool(s.knowledge_embeddings and s.cloudflare_account_id and s.cloudflare_api_token)


def _key(text: str) -> str:
    return hashlib.sha256(f"{MODEL}\n{text}".encode()).hexdigest()


async def embed(texts: list[str], settings: Settings | None = None,
                transport: httpx.AsyncBaseTransport | None = None) -> list[list[float]]:
    """One vector per text, in order."""
    s = settings or get_settings()
    if not enabled(s):
        raise EmbeddingError("not configured")
    url = f"https://api.cloudflare.com/client/v4/accounts/{s.cloudflare_account_id}/ai/run/{MODEL}"
    out: list[list[float]] = []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as http:
            for i in range(0, len(texts), BATCH):
                resp = await http.post(url, json={"text": texts[i:i + BATCH]},
                                       headers={"Authorization": f"Bearer {s.cloudflare_api_token}"})
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                data = ((body or {}).get("result") or {}).get("data")
                if resp.status_code != 200 or not isinstance(data, list) or len(data) != len(texts[i:i + BATCH]):
                    errs = "; ".join(str(e.get("message", "")) for e in (body or {}).get("errors") or [])[:200]
                    raise EmbeddingError(f"Cloudflare answered {resp.status_code}{': ' + errs if errs else ''}")
                out += data
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"{type(exc).__name__}: {redact(exc)}"[:200]) from exc
    return out


async def vectors_for(texts: list[str], settings: Settings | None = None, transport=None) -> list[list[float]]:
    """Cached vectors for these texts; only the missing ones are embedded (and then stored)."""
    col = store.db()[COLLECTION]
    keys = [_key(t) for t in texts]
    have = {d["_id"]: d["vector"] for d in col.find({"_id": {"$in": list(set(keys))}})}
    missing = list(dict.fromkeys(t for t, k in zip(texts, keys, strict=True) if k not in have))
    if missing:
        for t, v in zip(missing, await embed(missing, settings, transport), strict=True):
            have[_key(t)] = v
            col.update_one({"_id": _key(t)}, {"$set": {"vector": v, "model": MODEL}}, upsert=True)
    return [have[k] for k in keys]


def _cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb) if na and nb else 0.0


async def rank(question: str, passages: list[dict], settings: Settings | None = None, transport=None) -> list[dict]:
    """Passages by meaning, best first; only those at least KNOWLEDGE_MIN_SIMILARITY alike. [] when off."""
    s = settings or get_settings()
    if not enabled(s) or not passages or not question.strip():
        return []
    vecs = await vectors_for([p["text"] for p in passages], s, transport)
    q = (await embed([question[:1000]], s, transport))[0]  # the customer's words are never stored
    scored = sorted(((_cos(q, v), i) for i, v in enumerate(vecs)), key=lambda t: (-t[0], t[1]))
    return [passages[i] for sim, i in scored if sim >= s.knowledge_min_similarity]


def forget_unused(keep_texts: list[str]) -> int:
    """Delete cached vectors of passages that no longer exist (after a PDF is deleted)."""
    keep = [_key(t) for t in keep_texts]
    return store.db()[COLLECTION].delete_many({"_id": {"$nin": keep}}).deleted_count

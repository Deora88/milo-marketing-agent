"""Public comments on the business's Instagram posts and Facebook Page posts.

Arrive through the same signed webhook as DMs (/webhooks/meta):
- Instagram: object "instagram", entry[].changes[] with field "comments"
  (value: id, text, from {id, username}, media {id}, parent_id); needs instagram_manage_comments.
- Facebook: object "page", entry[].changes[] with field "feed" and item "comment"
  (value: comment_id, post_id, parent_id, message, from {id, name}, verb add/edited/remove/hide/unhide);
  needs pages_read_user_content (read) and pages_manage_engagement (reply, hide).

A comment is public, so the bot never posts by itself: it only drafts a reply (on request) from the FAQ, and a
person clicks Reply. Replies and hiding go out with the Page token; dry run only saves them in Milo.
"""
import logging
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from app.config import Settings, get_settings
from app.db import store
from app.db.models import Comment, InboundMessage, SocialPost, now
from app.dm import inbox
from app.meta.client import MetaAPIError, MetaClient
from app.util.redact import redact

log = logging.getLogger("milo.comments")
MAX_REPLY = 2000  # Instagram allows 2,200 characters per comment


class CommentError(ValueError):
    """A request Milo refuses; the message is shown to the user."""


def _when(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_changes(body: dict) -> list[dict]:
    """Comment events from a webhook body: {"new": Comment} or {"update": comment_id, "changes": {...}}."""
    obj = body.get("object")
    out: list[dict] = []
    for entry in body.get("entry") or []:
        account = str(entry.get("id", ""))
        for ch in entry.get("changes") or []:
            v = ch.get("value") or {}
            if obj == "instagram" and ch.get("field") == "comments" and v.get("id"):
                who = v.get("from") or {}
                out.append({"new": Comment(
                    platform="instagram", account_id=account, comment_id=str(v["id"]),
                    media_id=str((v.get("media") or {}).get("id") or ""), parent_id=str(v.get("parent_id") or ""),
                    from_id=str(who.get("id") or ""), from_name=str(who.get("username") or "")[:100],
                    text=str(v.get("text") or "")[:2200], own=str(who.get("id") or "") == account,
                    commented_at=now())})
            elif obj == "page" and ch.get("field") == "feed" and v.get("item") == "comment" and v.get("comment_id"):
                cid, verb = str(v["comment_id"]), v.get("verb") or "add"
                who = v.get("from") or {}
                if verb == "add":
                    parent = str(v.get("parent_id") or "")
                    post = str(v.get("post_id") or "")
                    out.append({"new": Comment(
                        platform="facebook", account_id=account, comment_id=cid, media_id=post,
                        parent_id="" if parent == post else parent, from_id=str(who.get("id") or ""),
                        from_name=str(who.get("name") or "")[:100], text=str(v.get("message") or "")[:8000],
                        own=str(who.get("id") or "") == account, commented_at=_when(v.get("created_time")))})
                elif verb == "edited":
                    out.append({"update": cid, "changes": {"text": str(v.get("message") or "")[:8000]}})
                elif verb == "remove":
                    out.append({"update": cid, "changes": {"status": "removed"}})
                elif verb in ("hide", "unhide"):
                    out.append({"update": cid, "changes": {"hidden": verb == "hide"}})
    return out


def describe(body: dict) -> str:
    """What a webhook delivery holds, for the server log: object, then the counts per kind, e.g.
    "instagram: 1 message, changes comments x1" or "page: 1 read". Never message or comment text."""
    kinds: dict[str, int] = {}
    for entry in body.get("entry") or []:
        for ev in entry.get("messaging") or []:
            k = next((x for x in ("message", "postback", "read", "delivery", "reaction", "referral") if x in ev),
                     "other")
            kinds[k] = kinds.get(k, 0) + 1
        for ch in entry.get("changes") or []:
            v = ch.get("value") or {}
            k = f"changes {ch.get('field', '?')}" + (f"/{v.get('item')}" if v.get("item") else "")
            kinds[k] = kinds.get(k, 0) + 1
    return f"{body.get('object', '?')}: " + (", ".join(f"{k} x{n}" for k, n in kinds.items()) or "nothing")


def save_events(body: dict) -> dict:
    """Store new comments (retries dropped by the unique comment_id) and apply edits/removals.
    Returns {"new": n, "own": n, "duplicate": n, "updated": n}."""
    out = {"new": 0, "own": 0, "duplicate": 0, "updated": 0}
    added = 0
    for ev in parse_changes(body):
        if "new" in ev:
            c: Comment = ev["new"]
            if c.own:
                c.status = "read"  # the business's own reply coming back: not something to answer
            try:
                store.insert(c)
                added += 1
                out["own" if c.own else "new"] += 1
            except DuplicateKeyError:
                out["duplicate"] += 1  # Meta retried a delivery we already have
        else:
            store.db()[Comment.collection].update_one({"comment_id": ev["update"]}, {"$set": ev["changes"]})
            out["updated"] += 1
    if added:
        store.audit("comment_received", count=added, platform="instagram" if body.get("object") == "instagram"
                    else "facebook")
    return out


def get(comment_id: int) -> Comment:
    c = store.get(Comment, comment_id)
    if c is None:
        raise CommentError("Comment not found.")
    return c


def list_comments(status: str | None = None, limit: int = 200) -> list[dict]:
    """Newest first, with the Milo post they belong to (if Milo published it). Comments written by the business's
    own account are listed only under "All" (status empty), marked own, so a test comment is never invisible."""
    q: dict = {} if not status else {"own": False}
    if status == "open":
        q["status"] = {"$in": ["new", "read"]}
    elif status:
        q["status"] = status
    rows = store.find(Comment, q, sort=[("_id", -1)], limit=limit)
    media = {c.media_id for c in rows if c.media_id}
    posts = {p.meta_media_id: p for p in store.find(SocialPost, {"meta_media_id": {"$in": list(media)}})} if media \
        else {}
    out = []
    for c in rows:
        d = c.model_dump()
        p = posts.get(c.media_id)
        d["post"] = {"id": p.id, "kind": p.kind, "caption": p.caption[:80], "permalink": p.permalink} if p else None
        out.append(d)
    return out


def unread() -> int:
    return store.count(Comment, {"own": False, "status": "new"})


def mark_read(ids: list[int] | None = None) -> int:
    q: dict = {"own": False, "status": "new"}
    if ids is not None:
        q["_id"] = {"$in": ids}
    return store.db()[Comment.collection].update_many(q, {"$set": {"status": "read"}}).modified_count


async def _token(client: MetaClient) -> str:
    return await inbox._page_token(client, client.s.meta_page_id)


async def reply(comment_id: int, text: str, client: MetaClient | None = None) -> Comment:
    """A person's public reply under the comment, posted as the business."""
    c = get(comment_id)
    text = (text or "").strip()
    if not text:
        raise CommentError("Write a reply first.")
    if len(text) > MAX_REPLY:
        raise CommentError(f"Replies can be at most {MAX_REPLY} characters.")
    if c.own:
        raise CommentError("This comment was written by your own account.")
    if c.status == "removed":
        raise CommentError("This comment was deleted on Meta, so it can't get a reply.")
    own = client is None
    client = client or MetaClient()
    try:
        if client.s.meta_dry_run:
            meta_id, mode = "", "dry_run"
        else:
            token = await _token(client)
            if c.platform == "instagram":  # Instagram threads are one level deep: answer the top comment
                resp = await client.post(f"/{c.parent_id or c.comment_id}/replies", token=token, message=text)
            else:
                resp = await client.post(f"/{c.comment_id}/comments", token=token, message=text)
            meta_id, mode = str(resp.get("id") or ""), "live"
    except MetaAPIError as exc:
        raise CommentError(f"Meta refused the reply: {redact(exc)}") from exc
    finally:
        if own:
            await client.aclose()
    entry = {"text": text, "at": now(), "mode": mode, "meta_id": meta_id}
    store.audit("comment_replied", comment_id=c.id, platform=c.platform, mode=mode)
    return store.update(Comment, c.id, {"replies": [*c.replies, entry], "status": "replied",
                                        "suggested_reply": ""})


async def set_hidden(comment_id: int, hidden: bool, client: MetaClient | None = None) -> Comment:
    """Hide (or show again) a comment: hidden comments stay visible only to the writer and their friends."""
    c = get(comment_id)
    if c.status == "removed":
        raise CommentError("This comment was deleted on Meta.")
    own = client is None
    client = client or MetaClient()
    try:
        if not client.s.meta_dry_run:
            token = await _token(client)
            flag = "true" if hidden else "false"
            if c.platform == "instagram":
                await client.post(f"/{c.comment_id}", token=token, hide=flag)
            else:
                await client.post(f"/{c.comment_id}", token=token, is_hidden=flag)
    except MetaAPIError as exc:
        raise CommentError(f"Meta refused: {redact(exc)}") from exc
    finally:
        if own:
            await client.aclose()
    store.audit("comment_hidden" if hidden else "comment_unhidden", comment_id=c.id, platform=c.platform,
                mode="dry_run" if client.s.meta_dry_run else "live")
    status = "hidden" if hidden else ("replied" if c.replies else "read")
    return store.update(Comment, c.id, {"hidden": hidden, "status": status})


async def suggest(comment_id: int, settings: Settings | None = None, llm=None) -> Comment:
    """Ask the DM bot for a draft public reply from the FAQ. Saves the draft only; never posts."""
    from app.dm import agent

    c = get(comment_id)
    s = settings or get_settings()
    probe = InboundMessage(platform="instagram" if c.platform == "instagram" else "messenger",
                           page_id=c.account_id, sender_id=c.from_id, customer_id=c.from_id,
                           mid=f"comment:{c.comment_id}", text=c.text)
    d = await agent.decide(probe, settings=s, llm=llm)
    if d["action"] == "answer" and d.get("reply"):
        draft = d["reply"]
        note = d.get("note", "") + ("; from a PDF: check it before replying" if d.get("draft_only") else "")
    elif d["action"] == "lead":
        draft = f"Thanks for reaching out! Please send us a DM and the {s.dm_business_name} team will call you back."
        note = "the comment has a phone number or asks for a callback: consider hiding it (it's public)"
    else:
        draft, note = "", f"no draft: {d.get('note', 'not covered by the FAQ')}"
    return store.update(Comment, c.id, {"suggested_reply": draft[:MAX_REPLY], "agent_note": note[:300]})

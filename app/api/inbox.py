"""Inbox API (localhost only, like every /api route): read DM conversations and reply by hand."""
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.dm import inbox, profiles
from app.util.redact import redact

log = logging.getLogger("milo.inbox")
router = APIRouter(prefix="/api/inbox", tags=["inbox"])


class ReplyIn(BaseModel):
    text: str


@router.get("")
def conversations():
    """Conversations, newest first, with unread count, reply-window state and the sender's name and id."""
    rows = inbox.list_conversations()
    cache = profiles.all_by_key()
    for r in rows:
        r["sender"] = profiles.display(r["platform"], r["customer_id"], cache)
    return rows


@router.get("/{platform}/{page_id}/{customer_id}")
async def thread(platform: str, page_id: str, customer_id: str):
    """All messages with one person (marks their new messages as read), with the sender's name and id."""
    data = inbox.open_thread(platform, page_id, customer_id)
    if not data["messages"]:
        raise HTTPException(404, "conversation not found")
    try:  # cached; asks Meta only if the name is missing (at most once a day after a failure)
        await profiles.ensure(platform, page_id, customer_id)
    except Exception:  # noqa: BLE001 - the name is a nicety: show the id rather than fail the Inbox
        log.exception("sender lookup failed")
    data["sender"] = profiles.display(platform, customer_id)
    return data


@router.delete("/{platform}/{page_id}/{customer_id}")
def delete_conversation(platform: str, page_id: str, customer_id: str):
    """Delete Milo's copy of this conversation, the person's leads and cached name (data deletion requests).
    Nothing is deleted on Facebook / Instagram."""
    if not inbox.thread_messages(platform, page_id, customer_id):
        raise HTTPException(404, "conversation not found")
    return {"deleted": inbox.delete_conversation(platform, page_id, customer_id)}


@router.post("/{platform}/{page_id}/{customer_id}/suggest")
async def suggest(platform: str, page_id: str, customer_id: str):
    """Ask the DM agent for a draft reply to the person's latest message (never sends)."""
    from app.config import get_settings
    from app.db import store
    from app.db.models import InboundMessage
    from app.dm import agent

    latest = [m for m in inbox.thread_messages(platform, page_id, customer_id) if not m.is_echo]
    if not latest:
        raise HTTPException(404, "no customer message to answer")
    msg = latest[-1]
    reset = store.claim(InboundMessage, msg.id, {"agent_action": {"$ne": "processing"}},
                        {"agent_action": "", "suggested_reply": "", "agent_note": "", "handed_off": False})
    if reset is None:
        raise HTTPException(409, "The bot is still working on this message. Try again in a few seconds.")
    s = get_settings().model_copy(update={"dm_autoreply": False, "dm_agent_enabled": True})
    done = await agent.handle(msg.id, settings=s, force=True)  # a person asked: draft even if they own it
    return {"message": done.model_dump() if done else None}


@router.post("/{platform}/{page_id}/{customer_id}/handback")
async def handback(platform: str, page_id: str, customer_id: str, background: BackgroundTasks):
    """Let the DM bot answer this conversation again (clears earlier hand-offs and Page replies). If the
    customer's latest message is still unanswered, the bot answers it now (auto-sent only with DM_AUTOREPLY=true)."""
    from app.db import store
    from app.db.models import InboundMessage
    from app.dm import agent

    if not inbox.hand_back(platform, page_id, customer_id):
        raise HTTPException(404, "conversation not found")
    msgs = inbox.thread_messages(platform, page_id, customer_id)
    last = msgs[-1] if msgs else None
    rerun = bool(last and not last.is_echo and last.agent_action in ("skipped", "handoff", "failed")
                 and store.claim(InboundMessage, last.id, {"agent_action": last.agent_action},
                                 {"agent_action": "", "agent_note": "", "suggested_reply": "", "handed_off": False}))
    if rerun:
        background.add_task(agent.handle, last.id)
    return {"ok": True, "answering_latest": rerun}


@router.post("/{platform}/{page_id}/{customer_id}/reply")
async def reply(platform: str, page_id: str, customer_id: str, body: ReplyIn):
    """Send a reply as the Page (a person in the Inbox). Refused outside Meta's reply window."""
    try:
        sent = await inbox.send_reply(platform, page_id, customer_id, body.text, sent_by="human")
    except inbox.ReplyError as exc:
        raise HTTPException(409, redact(exc)) from exc
    return {"sent": sent.model_dump()}

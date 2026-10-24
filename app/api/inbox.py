"""Inbox API (localhost only, like every /api route): read DM conversations and reply by hand."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.dm import inbox
from app.util.redact import redact

router = APIRouter(prefix="/api/inbox", tags=["inbox"])


class ReplyIn(BaseModel):
    text: str


@router.get("")
def conversations():
    """Conversations, newest first, with unread count and reply-window state."""
    return inbox.list_conversations()


@router.get("/{platform}/{page_id}/{customer_id}")
def thread(platform: str, page_id: str, customer_id: str):
    """All messages with one person (marks their new messages as read)."""
    data = inbox.open_thread(platform, page_id, customer_id)
    if not data["messages"]:
        raise HTTPException(404, "conversation not found")
    return data


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


@router.post("/{platform}/{page_id}/{customer_id}/reply")
async def reply(platform: str, page_id: str, customer_id: str, body: ReplyIn):
    """Send a reply as the Page (a person in the Inbox). Refused outside Meta's reply window."""
    try:
        sent = await inbox.send_reply(platform, page_id, customer_id, body.text, sent_by="human")
    except inbox.ReplyError as exc:
        raise HTTPException(409, redact(exc)) from exc
    return {"sent": sent.model_dump()}

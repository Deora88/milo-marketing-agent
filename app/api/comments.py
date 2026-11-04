"""Comments on the business's Instagram and Facebook posts (localhost only, like every /api route).
A person replies or hides; the bot only drafts a reply when asked."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.social import comments

router = APIRouter(prefix="/api/comments", tags=["comments"])


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=comments.MAX_REPLY)


class HideIn(BaseModel):
    hidden: bool = True


class ReadIn(BaseModel):
    ids: Optional[list[int]] = None  # None = all new comments


def _err(exc: comments.CommentError) -> HTTPException:
    return HTTPException(404 if "not found" in str(exc) else 409, str(exc))


@router.get("")
def list_comments(status: Optional[str] = None):
    """Newest first. status: open (new + read), new, read, replied, hidden, removed; empty = all."""
    return {"comments": comments.list_comments(status), "unread": comments.unread()}


@router.post("/read")
def mark_read(body: ReadIn):
    return {"marked": comments.mark_read(body.ids)}


@router.post("/{comment_id}/reply")
async def reply(comment_id: int, body: ReplyIn):
    """Posts the reply publicly as the business (dry run: saved in Milo only)."""
    try:
        return (await comments.reply(comment_id, body.text)).model_dump()
    except comments.CommentError as exc:
        raise _err(exc) from exc


@router.post("/{comment_id}/hide")
async def hide(comment_id: int, body: HideIn):
    try:
        return (await comments.set_hidden(comment_id, body.hidden)).model_dump()
    except comments.CommentError as exc:
        raise _err(exc) from exc


@router.post("/{comment_id}/suggest")
async def suggest(comment_id: int):
    """Draft a reply from the FAQ (never posts)."""
    try:
        return (await comments.suggest(comment_id)).model_dump()
    except comments.CommentError as exc:
        raise _err(exc) from exc

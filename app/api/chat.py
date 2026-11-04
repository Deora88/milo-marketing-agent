import json

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.agent.runner import chat_turn
from app.util.redact import redact

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=8000)


@router.post("")
async def chat(body: ChatIn):
    async def stream():
        try:
            async for event in chat_turn(body.session_id, body.message):
                yield {"event": event["type"], "data": json.dumps(event)}
        except Exception as exc:  # surface LLM/rate-limit errors to the UI instead of a silent hang
            yield {"event": "error", "data": json.dumps({"type": "error", "message": redact(exc)})}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(stream())

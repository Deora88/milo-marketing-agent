import json

from fastapi import APIRouter
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.agent.runner import chat_turn

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatIn(BaseModel):
    session_id: str
    message: str


@router.post("")
async def chat(body: ChatIn):
    async def stream():
        try:
            async for event in chat_turn(body.session_id, body.message):
                yield {"event": event["type"], "data": json.dumps(event)}
        except Exception as exc:  # surface LLM/rate-limit errors to the UI instead of a silent hang
            yield {"event": "error", "data": json.dumps({"type": "error", "message": str(exc)})}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(stream())

"""Runs Milo for one chat turn and turns ADK events into simple dicts for the SSE stream.

- Streams text as the model writes it (ADK StreamingMode.SSE -> "delta" events).
- Hides the model's internal reasoning (parts with thought=True) from the user.
- Logs how long each turn took, so slow free models are easy to spot.
"""
import logging
import time
from collections.abc import AsyncIterator

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from app.agent.agent import build_agent
from app.db import store
from app.db.models import Approval

log = logging.getLogger("milo.chat")
APP_NAME = "milo"
RUN_CONFIG = RunConfig(streaming_mode=StreamingMode.SSE)
USER_ID = "local-user"

_session_service = InMemorySessionService()
_runner: Runner | None = None


def get_runner() -> Runner:
    global _runner
    if _runner is None:
        _runner = Runner(app_name=APP_NAME, agent=build_agent(), session_service=_session_service)
    return _runner


async def ensure_session(session_id: str) -> None:
    existing = await _session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    if existing is None:
        await _session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)


def _max_approval_id() -> int:
    last = store.find(Approval, sort=[("_id", -1)], limit=1)
    return last[0].id if last else 0


def _new_pending(after_id: int, seen: set[int], ids: set[int] | None = None) -> list[Approval]:
    """Approvals this chat turn created: new since the turn started and named in this turn's tool results
    (so optimizer or UI requests filed at the same moment are not shown as if Milo made them)."""
    rows = store.find(Approval, {"_id": {"$gt": after_id}, "status": "pending"})
    return [a for a in rows if a.id not in seen and (ids is None or a.id in ids)]


def _approval_ids(result) -> set[int]:
    """approval_id values mentioned in an MCP tool result (dict, or wrapped as text content)."""
    import json
    import re

    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return {int(x) for x in re.findall(r'approval_id\\?"?\s*[:=]\s*\\?"?(\d+)', text)}


async def chat_turn(session_id: str, text: str) -> AsyncIterator[dict]:
    await ensure_session(session_id)
    start_id, seen = _max_approval_id(), set()
    t0, first = time.perf_counter(), None
    message = types.Content(role="user", parts=[types.Part(text=text)])
    async for event in get_runner().run_async(
        user_id=USER_ID, session_id=session_id, new_message=message, run_config=RUN_CONFIG
    ):
        if not event.content or not event.content.parts:
            continue
        if first is None:
            first = time.perf_counter() - t0
        for part in event.content.parts:
            if part.thought:  # model's private reasoning - never shown
                continue
            if part.function_call:
                yield {"type": "tool_call", "name": part.function_call.name}
            elif part.function_response:
                yield {"type": "tool_result", "name": part.function_response.name,
                       "result": part.function_response.response or {}}
                # Approvals are created inside the MCP server process; pick up the ones this turn created.
                for a in _new_pending(start_id, seen, _approval_ids(part.function_response.response or {})):
                    seen.add(a.id)
                    yield {"type": "approval", "approval_id": a.id, "summary": a.summary, "action": a.action,
                           "source": a.source, "reason": a.reason}
            elif part.text:
                if event.partial:
                    yield {"type": "delta", "text": part.text}
                elif event.is_final_response():
                    yield {"type": "message", "text": part.text}
    log.info("chat turn: first response %.1fs, total %.1fs", first or 0, time.perf_counter() - t0)

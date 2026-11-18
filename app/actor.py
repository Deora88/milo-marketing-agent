"""Who is making the current request (set by app/security.py once the session is checked).

A context variable, so the data layer can stamp audit rows and approvals with the person's email without every
function passing a user around. Empty outside a signed-in request (background jobs, the DM bot, the MCP server).
"""
from contextvars import ContextVar

_actor: ContextVar[str] = ContextVar("milo_actor", default="")


def set_actor(email: str) -> None:
    _actor.set(email or "")


def current_actor() -> str:
    return _actor.get()

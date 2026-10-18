"""Tool guard: an ADK before_tool_callback that runs before every MCP tool call.

Defence in depth on top of the MCP server's request-only design:
- only allow-listed tools may run (a tool added to the server later is blocked until reviewed here);
- every change request is written to the audit log with the arguments the model chose.
Returning a dict skips the tool and hands that dict back to the model.
"""
from typing import Any, Optional

from app.db import store
from app.mcp_server.server import TOOL_NAMES

REQUEST_TOOLS = {"request_publish", "request_pause", "request_budget_change"}


def tool_guard(tool: Any, args: dict[str, Any], tool_context: Any) -> Optional[dict]:
    if tool.name not in TOOL_NAMES:
        return {"status": "blocked", "message": f"Tool '{tool.name}' is not allowed for Milo."}
    if tool.name in REQUEST_TOOLS:
        store.audit("change_requested_by_agent", tool=tool.name, args=args)
    return None

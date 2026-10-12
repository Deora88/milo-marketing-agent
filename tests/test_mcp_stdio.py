"""End-to-end over the real MCP protocol: start the server as a subprocess, list tools, call them."""
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.mcp_server.server import TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]


def _json(result):
    return json.loads(result.content[0].text)


async def test_mcp_server_over_stdio():
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server.server"],
                                   cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert names == set(TOOL_NAMES)

            draft = _json(await session.call_tool("create_campaign_draft", {
                "name": "MCP test", "daily_budget_inr": 300, "countries": ["IN"], "age_min": 18, "age_max": 35,
                "headline": "Hello", "primary_text": "World", "link_url": "https://example.com"}))
            assert draft["status"] == "draft_saved"

            req = _json(await session.call_tool("request_publish", {"campaign_id": draft["campaign_id"]}))
            assert req["status"] == "pending_approval"

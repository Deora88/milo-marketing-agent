"""Milo's root agent: one LlmAgent on OpenRouter (via LiteLLM).

Tools come from the milo-meta MCP server, launched as a subprocess over stdio (McpToolset).
"""
import os
import sys
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from app.agent.guard import tool_guard
from app.agent.prompts import MILO_INSTRUCTION
from app.config import get_settings
from app.mcp_server.server import TOOL_NAMES

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_meta_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,  # same Python/venv as the backend
                args=["-m", "app.mcp_server.server"],
                cwd=str(PROJECT_ROOT),
                env=dict(os.environ),  # shares DATABASE_URL, META_* settings
            ),
            timeout=30,
        ),
        tool_filter=TOOL_NAMES,
    )


def build_agent() -> LlmAgent:
    s = get_settings()
    if s.openrouter_api_key:
        os.environ.setdefault("OPENROUTER_API_KEY", s.openrouter_api_key)
    return LlmAgent(
        name="milo",
        model=LiteLlm(model=s.milo_model),
        description="Creates, publishes (with approval) and monitors Meta ad campaigns.",
        instruction=MILO_INSTRUCTION,
        tools=[build_meta_toolset()],
        before_tool_callback=tool_guard,
    )

"""The chat path's model call must not crash on an import error between litellm and the mcp SDK.

litellm 1.105+ imports names that exist only in mcp 2.x; Milo uses mcp 1.x (FastMCP), so pyproject pins litellm
below 1.105. This calls litellm exactly like ADK does (OpenRouter, with a tool), against a closed local port: the
only acceptable failure is "can't connect", never "cannot import".
"""
import asyncio

import litellm


def test_openrouter_call_has_no_import_error():
    async def call():
        try:
            await litellm.acompletion(
                model="openrouter/openrouter/free", api_key="sk-or-test", api_base="http://127.0.0.1:9/api/v1",
                messages=[{"role": "user", "content": "hi"}], num_retries=0, timeout=5,
                tools=[{"type": "function", "function": {"name": "list_campaigns",
                                                         "parameters": {"type": "object", "properties": {}}}}])
        except Exception as exc:  # noqa: BLE001 - a connection error is expected here
            return str(exc)
        return ""

    error = asyncio.run(call())
    assert "cannot import" not in error and "ImportError" not in error, error

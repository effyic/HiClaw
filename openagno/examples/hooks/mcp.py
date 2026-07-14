"""Backward-compatible MCP extension hooks (connection + tool filter + headers)."""
from __future__ import annotations

import os
from typing import Any

from agno_worker.hooks.protocols import MCPServerConfig

WEKNORA_API_KEY = os.environ.get("WEKNORA_API_KEY", "")


def mcp_headers_hook(
    run_context: Any,
    server_config: MCPServerConfig,
    headers: dict[str, str],
) -> dict[str, str] | None:
    """Optional secondary pass over MCP HTTP headers after default forward.

    Default pipeline already injects:
      - user-id / tenant-id / session-id / role-code
      - all inbound headers starting with ``x-``

    Return ``None`` to keep ``headers`` unchanged; return a dict to replace them.
    """
    del run_context, server_config, headers
    return None


def mcp_connection_hook(server_config: MCPServerConfig) -> None:
    if server_config.name == "weknora" and WEKNORA_API_KEY:
        headers = dict(server_config.headers or {})
        headers.setdefault("X-API-Key", WEKNORA_API_KEY)
        server_config.headers = headers


def mcp_tool_filter_hook(run_context: Any, available_tools: list[Any]) -> list[Any]:
    del run_context
    return available_tools

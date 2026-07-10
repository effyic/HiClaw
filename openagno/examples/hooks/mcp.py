"""Backward-compatible MCP extension hooks (connection + tool filter)."""
from __future__ import annotations

import os
from typing import Any

from agno_worker.hooks.protocols import MCPServerConfig

WEKNORA_API_KEY = os.environ.get("WEKNORA_API_KEY", "")


def mcp_connection_hook(server_config: MCPServerConfig) -> None:
    if server_config.name == "weknora" and not server_config.headers and WEKNORA_API_KEY:
        server_config.headers = {"X-API-Key": WEKNORA_API_KEY}


def mcp_tool_filter_hook(run_context: Any, available_tools: list[Any]) -> list[Any]:
    del run_context
    return available_tools

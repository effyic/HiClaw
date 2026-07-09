"""MCP hooks — WeKnora knowledge base via streamable HTTP."""
from __future__ import annotations

import os
from typing import Any

from agno_worker.hooks.protocols import MCPServerConfig

WEKNORA_MCP_URL = os.environ.get(
    "WEKNORA_MCP_URL", "http://172.16.1.203:18082/mcp"
)
WEKNORA_API_KEY = os.environ.get("WEKNORA_API_KEY", "")


def _weknora_headers() -> dict[str, str]:
    if not WEKNORA_API_KEY:
        return {}
    return {"X-API-Key": WEKNORA_API_KEY}


def get_mcp_servers_hook(
    run_context: Any, business_scenario: str
) -> list[MCPServerConfig]:
    """Return WeKnora knowledge-base MCP for every run."""
    del business_scenario
    if not WEKNORA_MCP_URL:
        return []
    return [
        MCPServerConfig(
            name="weknora",
            url=WEKNORA_MCP_URL,
            transport="streamable-http",
            headers=_weknora_headers(),
        )
    ]


def mcp_connection_hook(server_config: MCPServerConfig) -> None:
    """Inject API key at connection time if not already set."""
    if server_config.name != "weknora":
        return
    if not server_config.headers and WEKNORA_API_KEY:
        server_config.headers = _weknora_headers()


def mcp_tool_filter_hook(run_context: Any, available_tools: list[Any]) -> list[Any]:
    """Pass through all tools; customize per tenant here if needed."""
    del run_context
    return available_tools

"""Standard MCP server configuration from agno_agent table."""
from __future__ import annotations

import os
from typing import Any

from agno_worker.hooks.protocols import MCPServerConfig
from agno_worker.tenant.context import TenantContextResolver

WEKNORA_MCP_URL = os.environ.get("WEKNORA_MCP_URL", "http://172.16.1.203:18082/mcp")
WEKNORA_API_KEY = os.environ.get("WEKNORA_API_KEY", "")


def _weknora_headers() -> dict[str, str]:
    if not WEKNORA_API_KEY:
        return {}
    return {"X-API-Key": WEKNORA_API_KEY}


class TenantMCPBuilder:
    """Resolve MCP servers from tenant agent configuration."""

    def __init__(self, resolver: TenantContextResolver | None = None) -> None:
        self._resolver = resolver or TenantContextResolver()

    def build_servers(
        self,
        run_context: Any,
        business_scenario: str = "",
    ) -> list[MCPServerConfig]:
        del business_scenario
        ctx = self._resolver.resolve(run_context)
        cfg = ctx.agent_config
        if not cfg.get("mcp_enabled"):
            return []

        mcp_config = cfg.get("mcp_config") or {}
        servers_cfg = mcp_config.get("servers") or []
        if not servers_cfg:
            servers_cfg = [
                {"name": "weknora", "url": WEKNORA_MCP_URL, "transport": "streamable-http"}
            ]

        out: list[MCPServerConfig] = []
        for server in servers_cfg:
            headers = dict(server.get("headers") or {})
            if not headers and server.get("name", "weknora") == "weknora":
                headers = _weknora_headers()
            out.append(
                MCPServerConfig(
                    name=server.get("name", "weknora"),
                    url=server.get("url", WEKNORA_MCP_URL),
                    transport=server.get("transport", "streamable-http"),
                    headers=headers,
                )
            )
        return out

    @staticmethod
    def apply_connection_defaults(server_config: MCPServerConfig) -> None:
        if server_config.name == "weknora" and not server_config.headers and WEKNORA_API_KEY:
            server_config.headers = _weknora_headers()

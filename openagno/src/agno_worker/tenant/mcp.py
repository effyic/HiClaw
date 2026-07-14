"""Standard MCP server configuration from agno_agent table."""
from __future__ import annotations

from typing import Any

from agno_worker.hooks.protocols import MCPServerConfig
from agno_worker.mcp.headers import apply_forwarded_mcp_headers
from agno_worker.tenant.context import TenantContextResolver


class TenantMCPBuilder:
    """Resolve MCP servers from tenant agent configuration (mcp_config in agno_agent)."""

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
            return []

        out: list[MCPServerConfig] = []
        for server in servers_cfg:
            url = str(server.get("url") or "").strip()
            command = str(server.get("command") or "").strip()
            if not url and not command:
                continue
            out.append(
                MCPServerConfig(
                    name=str(server.get("name") or "mcp"),
                    url=url,
                    command=command,
                    transport=str(server.get("transport") or "streamable-http"),
                    headers=dict(server.get("headers") or {}),
                    env=dict(server.get("env") or {}),
                    include_tools=list(server.get("include_tools") or []),
                    exclude_tools=list(server.get("exclude_tools") or []),
                )
            )
        return out

    @staticmethod
    def apply_forwarded_headers(
        run_context: Any,
        servers: list[MCPServerConfig],
    ) -> list[MCPServerConfig]:
        """Inject identity + ``x-*`` headers onto HTTP MCP servers."""
        return apply_forwarded_mcp_headers(run_context, servers)

    @staticmethod
    def apply_connection_defaults(server_config: MCPServerConfig) -> None:
        """No-op in standard flow; use mcp_connection_hook for vendor-specific defaults."""
        del server_config

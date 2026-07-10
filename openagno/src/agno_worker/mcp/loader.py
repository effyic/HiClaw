"""Build Agno MCPTools instances from tenant MCP server configs."""
from __future__ import annotations

import logging
from typing import Any, Protocol

from agno_worker.hooks.protocols import MCPServerConfig

logger = logging.getLogger(__name__)


class MCPConnectionHandler(Protocol):
    def on_mcp_connection(self, server_config: MCPServerConfig) -> None: ...


def build_mcp_tools(
    servers: list[MCPServerConfig],
    connection_handler: MCPConnectionHandler | Any,
) -> list[Any]:
    if not servers:
        return []

    try:
        from agno.tools.mcp import MCPTools, StreamableHTTPClientParams
    except ImportError as exc:
        logger.warning("MCPTools unavailable (%s); skipping MCP servers", exc)
        return []

    tools: list[Any] = []
    for server in servers:
        try:
            if hasattr(connection_handler, "on_mcp_connection"):
                connection_handler.on_mcp_connection(server)
            elif hasattr(connection_handler, "call"):
                connection_handler.call("mcp_connection_hook", server)
            kwargs: dict[str, Any] = {}
            if server.url:
                kwargs["transport"] = server.transport
                if server.headers:
                    kwargs["server_params"] = StreamableHTTPClientParams(
                        url=server.url,
                        headers=server.headers,
                    )
                else:
                    kwargs["url"] = server.url
            elif server.command:
                kwargs["command"] = server.command
            else:
                logger.warning("Skipping MCP server without url/command: %s", server.name)
                continue
            if server.name:
                kwargs["name"] = server.name
            if server.env:
                kwargs["env"] = server.env
            if server.include_tools:
                kwargs["include_tools"] = server.include_tools
            if server.exclude_tools:
                kwargs["exclude_tools"] = server.exclude_tools
            kwargs["refresh_connection"] = True
            tools.append(MCPTools(**kwargs))
        except Exception as exc:
            logger.warning("Failed to build MCPTools for %s: %s", server.name, exc)
    return tools

"""Build Agno MCPTools instances from tenant MCP server configs."""
from __future__ import annotations

import logging
from typing import Any, Protocol

from agno_worker.hooks.protocols import MCPServerConfig
from agno_worker.mcp.headers import bind_mcp_tool_headers, make_mcp_header_provider
from agno_worker.mcp.pool import get_default_pool

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

    pool = get_default_pool()
    tools: list[Any] = []
    for server in servers:
        try:
            _notify_connection(connection_handler, server)
            if not server.url and not server.command:
                logger.warning(
                    "Skipping MCP server without url/command: %s", server.name
                )
                continue

            # Bind loop vars explicitly for the factory closure.
            tool = pool.get_or_create(
                server,
                lambda s=server: _new_mcp_tools(
                    s, MCPTools, StreamableHTTPClientParams
                ),
            )
            # Pool key is name+url only — always rebind headers from *this*
            # agent's mcp_config (+ finalize forward merge) before the run.
            bind_mcp_tool_headers(tool, server)
            tools.append(tool)
        except Exception as exc:
            logger.warning("Failed to build MCPTools for %s: %s", server.name, exc)
    return tools


def _notify_connection(
    connection_handler: MCPConnectionHandler | Any,
    server: MCPServerConfig,
) -> None:
    if hasattr(connection_handler, "on_mcp_connection"):
        connection_handler.on_mcp_connection(server)
    elif hasattr(connection_handler, "call"):
        connection_handler.call("mcp_connection_hook", server)


def _new_mcp_tools(
    server: MCPServerConfig,
    mcp_tools_cls: Any,
    http_params_cls: Any,
) -> Any:
    """Construct one MCPTools; pool decides refresh_connection afterward.

    HTTP headers are not frozen on ``server_params``: Agno merges
    ``header_provider`` output per run, and ``build_mcp_tools`` rebinds the
    provider on every pool borrow (DB headers + request identity / ``x-*``).
    """
    kwargs: dict[str, Any] = {}
    if server.url:
        kwargs["transport"] = server.transport
        # Empty params headers — outbound map comes from header_provider only,
        # so a pooled instance cannot leak another agent's campus-id / API key.
        kwargs["server_params"] = http_params_cls(
            url=server.url,
            headers={},
        )
        kwargs["header_provider"] = make_mcp_header_provider(server.headers)
    elif server.command:
        kwargs["command"] = server.command
    else:
        raise ValueError(f"MCP server {server.name!r} missing url/command")

    if server.name:
        kwargs["name"] = server.name
    if server.env:
        kwargs["env"] = server.env
    if server.include_tools:
        kwargs["include_tools"] = server.include_tools
    if server.exclude_tools:
        kwargs["exclude_tools"] = server.exclude_tools

    # Start False; MCPToolsPool.tune_refresh_connection flips to True while cold
    # so Agno's callable-tools path still connects on first use.
    kwargs["refresh_connection"] = False
    return mcp_tools_cls(**kwargs)

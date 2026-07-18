"""Build Agno MCPTools instances from tenant MCP server configs."""
from __future__ import annotations

import logging
from typing import Any, Protocol

from agno_worker.hooks.protocols import MCPServerConfig
from agno_worker.mcp.headers import (
    make_mcp_header_provider,
    split_static_and_per_run_headers,
)
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
            tools.append(
                pool.get_or_create(
                    server,
                    lambda s=server: _new_mcp_tools(
                        s, MCPTools, StreamableHTTPClientParams
                    ),
                )
            )
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

    Identity headers (``session-id`` / ``user-id`` / ``role-code`` / ``x-*``) go
    through Agno ``header_provider`` so pooled long-lived connections still open
    a per-run MCP session with the correct conversation identity.
    """
    static_headers, per_run_headers = split_static_and_per_run_headers(server.headers)

    kwargs: dict[str, Any] = {}
    if server.url:
        kwargs["transport"] = server.transport
        if static_headers:
            kwargs["server_params"] = http_params_cls(
                url=server.url,
                headers=static_headers,
            )
        else:
            kwargs["url"] = server.url
            # Still pass empty params when only per-run headers exist so Agno
            # can merge header_provider output onto a params object.
            if per_run_headers:
                kwargs["server_params"] = http_params_cls(
                    url=server.url,
                    headers={},
                )
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

    if server.url and (per_run_headers or static_headers):
        # Always attach provider for HTTP MCP so later runs under a pooled
        # instance still receive current session/user identity from run_context.
        kwargs["header_provider"] = make_mcp_header_provider(per_run_headers)

    # Start False; MCPToolsPool.tune_refresh_connection flips to True while cold
    # so Agno's callable-tools path still connects on first use.
    kwargs["refresh_connection"] = False
    return mcp_tools_cls(**kwargs)

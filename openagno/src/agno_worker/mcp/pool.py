"""Process-wide MCPTools pool — avoid per-run Streamable-HTTP reconnect.

Why this exists
---------------
``Agent.tools`` is a callable resolved on every run. Each call used to construct
fresh ``MCPTools`` with ``refresh_connection=True``, so Agno performed a full
MCP handshake before ``ModelRequestStarted`` (often twice when ``tools()`` ran
more than once). That produced a 5s+ SSE silence after PreHook.

Design
------
* Pool by **stable** server identity (URL/command + non-turn headers).
  Per-turn headers (user/session/role, debug flags) are ignored in the key so
  sessions share one connection.
* Soft-close: Agno may call ``close()`` on run teardown for list-mounted tools;
  pooled instances must survive.
* Stable ``is_alive``: many servers lack ping; a failed ping used to force
  ``connect(force=True)`` and double the handshake.
* ``refresh_connection``: True only until the instance is initialized (Agno's
  callable-tools path connects only when this flag is True); then False so
  warm runs skip handshake/list_tools.

Env
---
* ``AGNO_MCP_POOL`` — ``true``/``false`` (default ``true``)
* ``AGNO_MCP_REFRESH_CONNECTION`` — optional force ``true``/``false`` override
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from agno_worker.hooks.protocols import MCPServerConfig

logger = logging.getLogger(__name__)

# Per-turn forward headers — must not fragment the pool.
_TURN_HEADER_NAMES = frozenset(
    {
        "user-id",
        "session-id",
        "role-code",
        "user_id",
        "session_id",
        "role_code",
    }
)
_TURN_HEADER_PREFIXES = ("x-debug", "x-ignore", "x-enable-thinking")

_MARK = "_effyic_mcp_pooled"
_ORIG_CLOSE = "_effyic_mcp_orig_close"
_ORIG_IS_ALIVE = "_effyic_mcp_orig_is_alive"


def pool_enabled() -> bool:
    return os.environ.get("AGNO_MCP_POOL", "true").lower() in ("1", "true", "yes")


def pool_key(server: MCPServerConfig) -> tuple[Any, ...]:
    """Stable cache key for a server config (excludes per-turn headers)."""
    return (
        (server.name or "").strip(),
        (server.url or "").strip(),
        (server.command or "").strip(),
        (server.transport or "streamable-http").strip(),
        _stable_headers(server.headers),
        tuple(sorted(str(x) for x in (server.include_tools or []))),
        tuple(sorted(str(x) for x in (server.exclude_tools or []))),
    )


def _stable_headers(headers: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for raw_key, raw_value in (headers or {}).items():
        key = str(raw_key).lower().strip()
        if key in _TURN_HEADER_NAMES:
            continue
        if any(key.startswith(p) for p in _TURN_HEADER_PREFIXES):
            continue
        value = str(raw_value).strip()
        if value:
            items.append((key, value))
    items.sort(key=lambda kv: kv[0])
    return tuple(items)


def _env_refresh_override() -> bool | None:
    raw = os.environ.get("AGNO_MCP_REFRESH_CONNECTION", "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    return None


def tune_refresh_connection(tool: Any) -> None:
    """Set ``refresh_connection`` for Agno's callable-tools connect path."""
    override = _env_refresh_override()
    if override is not None:
        tool.refresh_connection = override
        return
    ready = bool(getattr(tool, "_initialized", False)) and (
        getattr(tool, "session", None) is not None
    )
    # Cold: True so aget_tools connects; warm: False to skip handshake.
    tool.refresh_connection = not ready


def prepare_pooled_tool(tool: Any) -> Any:
    """Idempotently wrap close/is_alive and tune refresh for pool reuse."""
    if not getattr(tool, _MARK, False):
        setattr(tool, _MARK, True)
        _install_soft_close(tool)
        _install_stable_is_alive(tool)
    tune_refresh_connection(tool)
    return tool


def _install_soft_close(tool: Any) -> None:
    if not hasattr(tool, "close") or hasattr(tool, _ORIG_CLOSE):
        return
    original = tool.close
    setattr(tool, _ORIG_CLOSE, original)

    async def soft_close(*_a: Any, **_kw: Any) -> None:
        logger.debug(
            "MCP pool: ignoring close for %s (connection kept)",
            getattr(tool, "name", "?"),
        )

    tool.close = soft_close  # type: ignore[method-assign]


def _install_stable_is_alive(tool: Any) -> None:
    if not hasattr(tool, "is_alive") or hasattr(tool, _ORIG_IS_ALIVE):
        return
    original = tool.is_alive
    setattr(tool, _ORIG_IS_ALIVE, original)

    async def stable_is_alive(*_a: Any, **_kw: Any) -> bool:
        if getattr(tool, "session", None) is None:
            return False
        if getattr(tool, "_initialized", False):
            return True
        try:
            return bool(await original())
        except Exception:
            return False

    tool.is_alive = stable_is_alive  # type: ignore[method-assign]


class MCPToolsPool:
    """Thread-safe in-process cache of MCPTools instances."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tools: dict[tuple[Any, ...], Any] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def get_or_create(
        self,
        server: MCPServerConfig,
        factory: Callable[[], Any],
    ) -> Any:
        """Return a pooled tool, creating via ``factory`` on miss.

        When pooling is disabled (``AGNO_MCP_POOL=false``), always calls
        ``factory`` and only tunes refresh — no cache, no soft-close.
        """
        if not pool_enabled():
            tool = factory()
            tune_refresh_connection(tool)
            return tool

        key = pool_key(server)
        with self._lock:
            tool = self._tools.get(key)
            if tool is None:
                tool = factory()
                prepare_pooled_tool(tool)
                self._tools[key] = tool
                logger.info(
                    "MCP pool: create name=%s endpoint=%s",
                    server.name or "",
                    server.url or server.command or "",
                )
            else:
                prepare_pooled_tool(tool)
                logger.debug(
                    "MCP pool: reuse name=%s ready=%s refresh=%s",
                    server.name or "",
                    bool(getattr(tool, "_initialized", False)),
                    getattr(tool, "refresh_connection", None),
                )
            return tool

    def clear(self) -> None:
        """Drop all entries and best-effort close underlying sessions."""
        with self._lock:
            tools = list(self._tools.values())
            self._tools.clear()

        for tool in tools:
            self._hard_close(tool)

        if tools:
            logger.info("MCP pool: cleared %s connection(s)", len(tools))

    @staticmethod
    def _hard_close(tool: Any) -> None:
        original = getattr(tool, _ORIG_CLOSE, None)
        if original is None:
            return
        try:
            tool.close = original  # type: ignore[method-assign]
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(original())
            else:
                loop.create_task(original())
        except Exception as exc:
            logger.debug("MCP pool: close on clear failed: %s", exc)


_default_pool = MCPToolsPool()


def get_default_pool() -> MCPToolsPool:
    return _default_pool


def clear_mcp_tools_pool() -> None:
    """Clear the process-default pool (call on AgentSpec/hooks reload)."""
    _default_pool.clear()

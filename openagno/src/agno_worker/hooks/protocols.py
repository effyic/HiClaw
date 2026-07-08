"""Hook interface definitions and shared types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


@dataclass
class MCPServerConfig:
    name: str = ""
    url: str = ""
    command: str = ""
    transport: str = "streamable-http"
    env: dict[str, str] = field(default_factory=dict)
    include_tools: list[str] = field(default_factory=list)
    exclude_tools: list[str] = field(default_factory=list)


@dataclass
class DBConnection:
    url: str = ""
    driver: str = "mysql"
    options: dict[str, Any] = field(default_factory=dict)


HookFn = Callable[..., Any]


@runtime_checkable
class PromptHooks(Protocol):
    def get_system_prompt_hook(self, run_context: Any, session_state: dict[str, Any]) -> str: ...

    def get_instructions_hook(self, run_context: Any, user_profile: dict[str, Any]) -> str: ...

    def get_context_filter_hook(self, run_context: Any) -> dict[str, Any]: ...


@runtime_checkable
class MCPHooks(Protocol):
    def get_mcp_servers_hook(
        self, run_context: Any, business_scenario: str
    ) -> list[MCPServerConfig]: ...

    def mcp_tool_filter_hook(self, run_context: Any, available_tools: list[Any]) -> list[Any]: ...

    def mcp_connection_hook(self, server_config: MCPServerConfig) -> None: ...


@runtime_checkable
class DataHooks(Protocol):
    def get_db_connection_hook(self, run_context: Any) -> DBConnection: ...

    def data_query_hook(self, query: str, run_context: Any) -> Any: ...

    def result_processing_hook(self, results: Any, run_context: Any) -> dict[str, Any]: ...


@runtime_checkable
class SessionHooks(Protocol):
    def session_init_hook(self, session_id: str, user_context: dict[str, Any]) -> None: ...

    def session_update_hook(
        self, session_state: dict[str, Any], run_context: Any
    ) -> dict[str, Any]: ...

    def session_cleanup_hook(self, session_id: str) -> None: ...


# All hook names resolved by HookRegistry (Skills intentionally omitted).
HOOK_NAMES: tuple[str, ...] = (
    "get_system_prompt_hook",
    "get_instructions_hook",
    "get_context_filter_hook",
    "get_mcp_servers_hook",
    "mcp_tool_filter_hook",
    "mcp_connection_hook",
    "get_db_connection_hook",
    "data_query_hook",
    "result_processing_hook",
    "session_init_hook",
    "session_update_hook",
    "session_cleanup_hook",
)


@dataclass
class HookSet:
    """Resolved hook callables keyed by name."""

    hooks: dict[str, HookFn] = field(default_factory=dict)

    def get(self, name: str) -> HookFn | None:
        return self.hooks.get(name)

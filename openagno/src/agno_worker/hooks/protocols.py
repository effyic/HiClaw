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
    headers: dict[str, str] = field(default_factory=dict)
    include_tools: list[str] = field(default_factory=list)
    exclude_tools: list[str] = field(default_factory=list)


@dataclass
class SkillRef:
    """Lightweight skill metadata; full instructions load on demand."""

    name: str
    description: str = ""
    source_path: str = ""
    scripts: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DBConnection:
    url: str = ""
    driver: str = "mysql"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class UserContext:
    """Caller identity resolved from HTTP headers and request body."""

    user_id: str = ""
    tenant_id: str = ""
    session_id: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


HookFn = Callable[..., Any]

# Optional extension hooks loaded from PVC-mounted directory.
# Tenant prompt/MCP/skills/workflow are built by standard pipeline first;
# these hooks may transform the standard output.
EXTENSION_HOOK_NAMES: tuple[str, ...] = (
    "enrich_business_context_hook",
    "transform_prompt_hook",
    "transform_mcp_servers_hook",
    "transform_skills_hook",
    "transform_workflow_hook",
    "transform_session_state_hook",
    "mcp_tool_filter_hook",
    "mcp_connection_hook",
    "result_processing_hook",
    "request_pre_filter_hook",
    "request_post_filter_hook",
)


@runtime_checkable
class BusinessContextHooks(Protocol):
    def enrich_business_context_hook(
        self, run_context: Any, base_context: dict[str, Any]
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class TransformHooks(Protocol):
    def transform_prompt_hook(
        self, run_context: Any, prompt_bundle: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def transform_mcp_servers_hook(
        self, run_context: Any, servers: list[MCPServerConfig]
    ) -> list[MCPServerConfig] | None: ...

    def transform_skills_hook(
        self, run_context: Any, catalog: list[Any]
    ) -> list[Any] | None: ...

    def transform_workflow_hook(
        self, run_context: Any, payload: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def transform_session_state_hook(
        self, run_context: Any, session_state: dict[str, Any]
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class RequestFilterHooks(Protocol):
    def request_pre_filter_hook(
        self, user_context: UserContext, metadata: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def request_post_filter_hook(
        self,
        user_context: UserContext,
        run_output: dict[str, Any],
        run_context: Any = None,
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class MCPHooks(Protocol):
    def mcp_tool_filter_hook(self, run_context: Any, available_tools: list[Any]) -> list[Any]: ...

    def mcp_connection_hook(self, server_config: MCPServerConfig) -> None: ...


@dataclass
class HookSet:
    """Resolved optional extension hook callables keyed by name."""

    hooks: dict[str, HookFn] = field(default_factory=dict)

    def get(self, name: str) -> HookFn | None:
        return self.hooks.get(name)

    def has(self, name: str) -> bool:
        return name in self.hooks

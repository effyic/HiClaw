"""Optional transform hooks for tenant pipeline outputs."""
from __future__ import annotations

from typing import Any

from agno_worker.hooks.protocols import MCPServerConfig


def transform_prompt_hook(
    run_context: Any,
    prompt_bundle: dict[str, Any],
) -> dict[str, Any] | None:
    """Transform standard prompt bundle after DB resolution."""
    del run_context
    return None


def transform_mcp_servers_hook(
    run_context: Any,
    servers: list[MCPServerConfig],
) -> list[MCPServerConfig] | None:
    del run_context, servers
    return None


def transform_skills_hook(run_context: Any, catalog: list[Any]) -> list[Any] | None:
    del run_context, catalog
    return None


def transform_workflow_hook(
    run_context: Any,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    del run_context, payload
    return None


def transform_session_state_hook(
    run_context: Any,
    session_state: dict[str, Any],
) -> dict[str, Any] | None:
    del run_context, session_state
    return None

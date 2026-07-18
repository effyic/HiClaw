"""Resolve tenant / role from run context and request metadata."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agno_worker.tenant.cache import ensure_run_cache
from agno_worker.tenant.store import AgentStore


def _factory_input(run_context: Any) -> dict[str, Any]:
    factory = getattr(run_context, "factory_input", None)
    if isinstance(factory, str):
        try:
            factory = json.loads(factory)
        except json.JSONDecodeError:
            return {}
    return dict(factory) if isinstance(factory, dict) else {}


def _session_state(run_context: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    persisted = getattr(run_context, "session_state", None)
    if isinstance(persisted, dict):
        merged.update(persisted)
    factory = _factory_input(run_context)
    factory_state = factory.get("session_state")
    if isinstance(factory_state, dict):
        merged.update(factory_state)
    return merged


def _metadata(run_context: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    persisted = getattr(run_context, "metadata", None)
    if isinstance(persisted, dict):
        merged.update(persisted)
    factory = _factory_input(run_context)
    factory_meta = factory.get("metadata")
    if isinstance(factory_meta, dict):
        merged.update(factory_meta)
    if factory.get("tenant_id") and not merged.get("tenant_id"):
        merged["tenant_id"] = factory["tenant_id"]
    return merged


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


@dataclass
class TenantContext:
    tenant_id: str
    role_code: str
    agent_id: int = 0
    agent_config: dict[str, Any] = field(default_factory=dict)


class TenantContextResolver:
    """Resolve tenant identity and load matching agno_agent configuration."""

    def __init__(self, store: AgentStore | None = None) -> None:
        self._store = store or AgentStore()

    def resolve(self, run_context: Any) -> TenantContext:
        cache = ensure_run_cache(run_context)
        cached = cache.get("tenant_context")
        if isinstance(cached, TenantContext):
            return cached

        tenant_id = self.resolve_tenant_id(run_context)
        role_code = self.resolve_role_code(run_context)
        agent_config = self._store.load_agent_resolved(tenant_id, role_code=role_code)
        ctx = TenantContext(
            tenant_id=tenant_id,
            role_code=role_code,
            agent_id=(
                int(agent_config.get("id") or 0)
                if str(agent_config.get("tenant_id") or "") == tenant_id
                else 0
            ),
            agent_config=agent_config,
        )
        cache["tenant_context"] = ctx
        return ctx

    def resolve_tenant_id(self, run_context: Any) -> str:
        metadata = _metadata(run_context)
        session_state = _session_state(run_context)
        factory = _factory_input(run_context)
        tenant_id = (
            metadata.get("tenant_id")
            or session_state.get("tenant_id")
            or factory.get("tenant_id")
            or getattr(run_context, "tenant_id", None)
            or "default"
        )
        return str(tenant_id)

    def resolve_role_code(self, run_context: Any) -> str:
        """Prefer request metadata (HTTP header), then session state."""
        metadata = _metadata(run_context)
        session_state = _session_state(run_context)
        role_code = _first_non_empty(
            metadata.get("role_code"),
            session_state.get("role_code"),
            session_state.get("active_role"),
            metadata.get("active_role"),
            session_state.get("role"),
            metadata.get("role"),
            session_state.get("agent"),
            metadata.get("agent"),
        )
        return role_code or "default"


_default_resolver = TenantContextResolver()


def resolve_tenant_context(run_context: Any) -> TenantContext:
    return _default_resolver.resolve(run_context)

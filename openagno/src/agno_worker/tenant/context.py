"""Resolve tenant / role / workflow from run context and request metadata."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agno_worker.tenant.cache import ensure_run_cache
from agno_worker.tenant.store import AgentStore, parse_workflow, workflow_kind, workflow_route_key


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


@dataclass
class TenantContext:
    tenant_id: str
    role_code: str
    workflow_kind: str
    route_key: str | None
    agent_config: dict[str, Any] = field(default_factory=dict)

    @property
    def workflow(self) -> dict[str, Any]:
        return dict(self.agent_config.get("workflow") or {})


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
        kind = self.resolve_workflow_kind(run_context)
        route_key = self.resolve_route_key(run_context)
        agent_config = self._store.load_agent_resolved(
            tenant_id,
            role_code=role_code,
            kind=kind,
            route_key=route_key,
        )
        ctx = TenantContext(
            tenant_id=tenant_id,
            role_code=role_code,
            workflow_kind=kind,
            route_key=route_key,
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

    def resolve_workflow(self, run_context: Any) -> dict[str, Any]:
        session_state = _session_state(run_context)
        metadata = _metadata(run_context)
        for source in (session_state, metadata):
            raw = source.get("workflow")
            if isinstance(raw, dict):
                return parse_workflow(raw)
        return parse_workflow({})

    def resolve_route_key(self, run_context: Any) -> str | None:
        workflow = self.resolve_workflow(run_context)
        route_key = workflow_route_key(workflow)
        if route_key:
            return route_key
        session_state = _session_state(run_context)
        metadata = _metadata(run_context)
        for source in (session_state, metadata):
            for key in ("active_role", "role_code", "role"):
                value = source.get(key)
                if value and str(value).startswith("expert_"):
                    return str(value)[len("expert_") :]
        return None

    def resolve_workflow_kind(self, run_context: Any) -> str:
        workflow = self.resolve_workflow(run_context)
        kind = workflow_kind(workflow)
        if kind != "default" or workflow.get("kind"):
            return kind
        session_state = _session_state(run_context)
        metadata = _metadata(run_context)
        for source in (session_state, metadata):
            phase = source.get("phase")
            if phase == "triage":
                return "triage"
            if phase in ("consultation", "expert"):
                return "expert"
        for source in (session_state, metadata):
            for key in ("active_role", "role_code", "role"):
                value = source.get(key)
                if value == "triage":
                    return "triage"
                if value and str(value).startswith("expert"):
                    return "expert"
        return "default"

    def resolve_role_code(self, run_context: Any) -> str:
        session_state = _session_state(run_context)
        metadata = _metadata(run_context)
        for source in (session_state, metadata):
            for key in ("active_role", "role_code", "role", "agent"):
                candidate = source.get(key)
                if candidate and str(candidate) not in ("default", ""):
                    return str(candidate)
                if candidate == "triage":
                    return "triage"
        kind = self.resolve_workflow_kind(run_context)
        route_key = self.resolve_route_key(run_context)
        if kind == "triage":
            return "triage"
        if kind == "expert" and route_key:
            return f"expert_{route_key}"
        for source in (session_state, metadata):
            for key in ("active_role", "role_code", "role", "agent"):
                candidate = source.get(key)
                if candidate:
                    return str(candidate)
        return "default"


_default_resolver = TenantContextResolver()


def resolve_tenant_context(run_context: Any) -> TenantContext:
    return _default_resolver.resolve(run_context)

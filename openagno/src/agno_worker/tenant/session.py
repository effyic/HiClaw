"""Standard session state management for tenant workflows."""
from __future__ import annotations

import json
from typing import Any

from agno_worker.tenant.context import TenantContextResolver
from agno_worker.tenant.store import workflow_kind, workflow_phase


class TenantSessionManager:
    """Initialize and update session state with tenant / role / workflow."""

    def __init__(self, resolver: TenantContextResolver | None = None) -> None:
        self._resolver = resolver or TenantContextResolver()
        self._active_sessions: set[str] = set()

    def init_session(self, session_id: str, user_context: dict[str, Any]) -> None:
        if session_id:
            self._active_sessions.add(session_id)

    def cleanup_session(self, session_id: str) -> None:
        self._active_sessions.discard(session_id)

    def build_session_updates(
        self,
        session_state: dict[str, Any],
        run_context: Any,
    ) -> dict[str, Any]:
        incoming = dict(session_state or {})
        factory = getattr(run_context, "factory_input", None)
        if isinstance(factory, str):
            try:
                factory = json.loads(factory)
            except json.JSONDecodeError:
                factory = {}
        if isinstance(factory, dict):
            factory_state = factory.get("session_state")
            if isinstance(factory_state, dict):
                incoming = {**incoming, **factory_state}

        ctx = self._resolver.resolve(run_context)
        cfg = ctx.agent_config
        workflow = dict(cfg.get("workflow") or self._resolver.resolve_workflow(run_context))
        kind = workflow_kind(workflow)
        tenant_id = ctx.tenant_id
        role_code = ctx.role_code

        merged = {**incoming, "tenant_id": tenant_id}
        merged["active_role"] = role_code
        merged["role_code"] = role_code
        merged["workflow"] = workflow

        phase = workflow_phase(workflow) or incoming.get("phase")
        if not phase:
            if kind == "expert":
                phase = "consultation"
            elif kind == "triage":
                phase = "triage"
        if phase:
            merged["phase"] = phase

        return merged

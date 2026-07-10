"""Hook vs AgentSpec merge rules: hooks override spec when they return data."""
from __future__ import annotations

import os
from typing import Any

from agno_worker.agentspec.schema import AgentDef, AgentSpec

DEFAULT_KNOWLEDGE_PROVIDER = os.environ.get("AGNO_KNOWLEDGE_PROVIDER", "").strip()


def has_hook_data(value: Any) -> bool:
    """Return True when a hook result should override AgentSpec."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list)):
        return bool(value)
    return True


def pick_hook_or_spec(hook_value: Any, spec_value: Any) -> Any:
    """Prefer hook output; fall back to AgentSpec when hook is empty/None."""
    if has_hook_data(hook_value):
        return hook_value
    return spec_value


def resolve_active_role(session_state: dict[str, Any], spec: AgentSpec) -> str:
    """Pick the active role name from session state, validated against spec.agents."""
    for key in ("active_role", "role", "phase", "agent"):
        candidate = session_state.get(key)
        if candidate and candidate in spec.agents:
            return str(candidate)
    if spec.agents:
        return next(iter(spec.agents))
    return "default"


def role_def(spec: AgentSpec, role_name: str) -> AgentDef | None:
    return spec.agents.get(role_name)


def spec_system_prompt(defn: AgentDef | None) -> str:
    if defn is None:
        return ""
    parts: list[str] = []
    if defn.role:
        parts.append(defn.role.strip())
    return "\n".join(parts)


def spec_instructions(defn: AgentDef | None) -> str:
    if defn is None:
        return ""
    if defn.instructions:
        return defn.instructions.strip()
    return ""


def spec_context_filters(active_role: str, spec: AgentSpec) -> dict[str, Any]:
    """AgentSpec fallback filters for knowledge-aware MCP tools."""
    defn = role_def(spec, active_role)
    filters: dict[str, Any] = {
        "role": active_role,
        "knowledge_ids": [],
    }
    provider = DEFAULT_KNOWLEDGE_PROVIDER
    if defn and defn.knowledge:
        provider = defn.knowledge.provider or provider
        filters["knowledge_ids"] = list(defn.knowledge.knowledge_ids)
    if provider:
        filters["provider"] = provider
    return filters


def normalize_knowledge_filters(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize knowledge filter payload for MCP consumption."""
    if not raw:
        out: dict[str, Any] = {"knowledge_ids": []}
        if DEFAULT_KNOWLEDGE_PROVIDER:
            out["provider"] = DEFAULT_KNOWLEDGE_PROVIDER
        return out

    out = dict(raw)
    ids: list[str] = []
    knowledge_ids = out.get("knowledge_ids")
    if isinstance(knowledge_ids, list):
        ids = [str(item).strip() for item in knowledge_ids if str(item).strip()]

    out["knowledge_ids"] = ids
    out.pop("knowledge_id", None)
    if provider := str(out.get("provider") or DEFAULT_KNOWLEDGE_PROVIDER).strip():
        out["provider"] = provider
    elif "provider" in out:
        out.pop("provider")
    return out


def build_run_dependencies(
    run_context: Any,
    knowledge_filters: dict[str, Any],
) -> dict[str, Any]:
    """Populate per-run dependencies for tenant-aware hooks and prompt assembly."""
    metadata = getattr(run_context, "metadata", None) or {}
    user_id = getattr(run_context, "user_id", None) or metadata.get("user_id") or ""
    tenant_id = metadata.get("tenant_id") or knowledge_filters.get("tenant_id") or ""

    tenant: dict[str, Any] = {
        "tenant_id": str(tenant_id) if tenant_id else "",
        "knowledge_ids": list(knowledge_filters.get("knowledge_ids") or []),
    }
    if provider := knowledge_filters.get("provider"):
        tenant["provider"] = provider

    user_profile: dict[str, Any] = {
        "user_id": str(user_id) if user_id else "",
        "tenant_id": tenant["tenant_id"],
    }
    extra_profile = metadata.get("user_profile")
    if isinstance(extra_profile, dict):
        user_profile.update(extra_profile)

    return {"tenant": tenant, "user_profile": user_profile}

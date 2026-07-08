"""Hook vs AgentSpec merge rules: hooks override spec when they return data."""
from __future__ import annotations

from typing import Any

from agno_worker.agentspec.schema import AgentDef, AgentSpec

# WeKnora MCP tools should read ``run_context.knowledge_filters`` using this shape:
# {"tenant_id": "...", "provider": "weknora", "knowledge_ids": ["kb-1", "kb-2"], ...}


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
    """AgentSpec fallback filters; knowledge retrieval is via MCP, not prompt text."""
    defn = role_def(spec, active_role)
    filters: dict[str, Any] = {
        "role": active_role,
        "provider": "weknora",
        "knowledge_ids": [],
    }
    if defn and defn.knowledge:
        filters["provider"] = defn.knowledge.provider or "weknora"
        filters["knowledge_ids"] = list(defn.knowledge.knowledge_ids)
    return filters


def normalize_knowledge_filters(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize hook/spec filters for WeKnora MCP consumption."""
    if not raw:
        return {"provider": "weknora", "knowledge_ids": []}

    out = dict(raw)
    ids: list[str] = []
    knowledge_ids = out.get("knowledge_ids")
    if isinstance(knowledge_ids, list):
        ids = [str(item).strip() for item in knowledge_ids if str(item).strip()]

    out["knowledge_ids"] = ids
    out.pop("knowledge_id", None)
    out.setdefault("provider", "weknora")
    return out


def build_run_dependencies(
    run_context: Any,
    knowledge_filters: dict[str, Any],
) -> dict[str, Any]:
    """Populate per-run dependencies for tenant-aware hooks and prompt assembly."""
    metadata = getattr(run_context, "metadata", None) or {}
    user_id = getattr(run_context, "user_id", None) or metadata.get("user_id") or ""
    tenant_id = metadata.get("tenant_id") or knowledge_filters.get("tenant_id") or ""

    tenant = {
        "tenant_id": str(tenant_id) if tenant_id else "",
        "knowledge_ids": list(knowledge_filters.get("knowledge_ids") or []),
        "provider": knowledge_filters.get("provider", "weknora"),
    }

    user_profile: dict[str, Any] = {
        "user_id": str(user_id) if user_id else "",
        "tenant_id": tenant["tenant_id"],
    }
    extra_profile = metadata.get("user_profile")
    if isinstance(extra_profile, dict):
        user_profile.update(extra_profile)

    return {"tenant": tenant, "user_profile": user_profile}

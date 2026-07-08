"""Hook vs AgentSpec merge rules: hooks override spec when they return data."""
from __future__ import annotations

from typing import Any

from agno_worker.agentspec.schema import AgentDef, AgentSpec


def has_hook_data(value: Any) -> bool:
    """Return True when a hook result should override AgentSpec."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return True
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
    parts: list[str] = []
    if defn.instructions:
        parts.append(defn.instructions.strip())
    if defn.knowledge and defn.knowledge.knowledge_id:
        parts.append(
            f"Use knowledge base provider={defn.knowledge.provider} "
            f"knowledge_id={defn.knowledge.knowledge_id} for retrieval."
        )
    return "\n\n".join(parts)

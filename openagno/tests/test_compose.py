"""Tests for hook vs AgentSpec override composition."""
from __future__ import annotations

from agno_worker.agentspec.schema import AgentDef, AgentSpec, KnowledgeRef
from agno_worker.hooks.compose import (
    has_hook_data,
    pick_hook_or_spec,
    resolve_active_role,
    spec_instructions,
    spec_system_prompt,
)


def _sample_spec() -> AgentSpec:
    return AgentSpec(
        name="medical-orchestrator",
        agents={
            "triage": AgentDef(
                name="triage",
                role="分诊护士",
                instructions="AgentSpec 分诊 instructions。",
            ),
            "consultation": AgentDef(
                name="consultation",
                role="专科医生",
                instructions="AgentSpec 问诊 instructions。",
                knowledge=KnowledgeRef(knowledge_id="kb-cardiology"),
            ),
        },
    )


def test_has_hook_data() -> None:
    assert not has_hook_data(None)
    assert not has_hook_data("")
    assert not has_hook_data("   ")
    assert not has_hook_data({})
    assert has_hook_data("hello")
    assert has_hook_data({"k": "v"})
    assert has_hook_data([])


def test_pick_hook_or_spec() -> None:
    assert pick_hook_or_spec("hook", "spec") == "hook"
    assert pick_hook_or_spec("", "spec") == "spec"
    assert pick_hook_or_spec(None, "spec") == "spec"


def test_resolve_active_role_from_phase() -> None:
    spec = _sample_spec()
    role = resolve_active_role({"phase": "consultation"}, spec)
    assert role == "consultation"


def test_spec_fallback_when_hook_empty() -> None:
    spec = _sample_spec()
    defn = spec.agents["triage"]
    assert spec_system_prompt(defn) == "分诊护士"
    assert "AgentSpec 分诊" in spec_instructions(defn)

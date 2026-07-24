"""Unit tests for collection probe + hard-gated write tools."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agno_worker.tenant.collection import (  # noqa: E402
    PHASE_COLLECTING,
    PHASE_CONFIRMED,
    PHASE_PROBING,
    append_probe_note,
    empty_collection_state,
    filter_collection_gated_tools,
    finish_probe,
    hard_gated_tool_names,
    pending_required_action_tools,
    resolve_probe_config,
    update_collected_fields,
)
from agno_worker.tenant.collection.kinds.dialogue.core import (  # noqa: E402
    current_required_action,
)


def _triage_config(*, mode: str = "concurrent"):
    return {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "required_actions_mode": mode,
        "probe": {
            "enabled": True,
            "max_rounds": 2,
            "allow_skip": True,
            "hints": [],
        },
        "schema": {
            "source": "inline",
            "fields": [
                {"name": "主诉", "required": True},
                {"name": "持续时间", "required": True},
                {"name": "推荐科室", "required": True, "after_probe": True},
            ],
        },
        "required_actions": [
            {"type": "reply", "field": "推荐科室", "when": "missing_empty"},
            {"type": "mcp", "tool": "mec_create_emr_case", "when": "missing_empty"},
        ],
    }


def test_probe_defaults_hints_empty():
    cfg = resolve_probe_config({"probe": {"enabled": True, "max_rounds": 3}})
    assert cfg is not None
    assert cfg["hints"] == []
    assert cfg["min_rounds"] == 0
    assert cfg["goal"] == ""
    assert "early_finish_keywords" not in cfg
    assert "gate_fields" not in cfg


def test_probe_min_rounds_blocks_early_finish_until_min():
    config = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "probe": {
            "enabled": True,
            "min_rounds": 2,
            "max_rounds": 4,
            "allow_skip": True,
            "hints": [],
        },
        "schema": {
            "source": "inline",
            "fields": [{"name": "主诉", "required": True}],
        },
    }
    state = update_collected_fields(empty_collection_state(), {"主诉": "头痛"}, config)
    assert state["phase"] == PHASE_PROBING
    state = append_probe_note(state, config, "伴恶心")
    try:
        finish_probe(state, config, reason="差不多了")
        raise AssertionError("expected min_rounds block")
    except ValueError as exc:
        assert "min_rounds" in str(exc)
    state = append_probe_note(state, config, "无急症")
    state = finish_probe(state, config, reason="信息足够可结案")
    assert state["probe_done"] is True


def test_triage_probe_before_recommended_dept():
    config = _triage_config()
    state = update_collected_fields(
        empty_collection_state(), {"主诉": "头痛", "持续时间": "三天"}, config
    )
    assert state["phase"] == PHASE_PROBING
    assert "推荐科室" in state["missing"]
    assert pending_required_action_tools(state, config) == []

    # Hard-gate: after_probe slot cannot be filled while probe is active.
    try:
        update_collected_fields(state, {"推荐科室": "神经内科[sjnk]"}, config)
        raise AssertionError("expected after_probe hard-gate during probing")
    except ValueError as exc:
        assert "after_probe" in str(exc) or "deferred" in str(exc)

    state = append_probe_note(state, config, "伴恶心")
    assert state["probe_rounds"] == 1
    state = finish_probe(state, config, reason="enough")
    assert state["phase"] == PHASE_COLLECTING
    # Concurrent: MCP is already pending/visible alongside reply.
    assert pending_required_action_tools(state, config) == ["mec_create_emr_case"]
    assert hard_gated_tool_names(state, config) == set()
    current = current_required_action(state, config)
    assert current is not None
    assert current["type"] == "reply"
    assert current["field"] == "推荐科室"

    state = update_collected_fields(state, {"推荐科室": "神经内科[sjnk]"}, config)
    assert state["phase"] == PHASE_CONFIRMED
    assert "reply:推荐科室" in (state.get("actions_done") or {})
    assert pending_required_action_tools(state, config) == ["mec_create_emr_case"]
    current = current_required_action(state, config)
    assert current is not None
    assert current["type"] == "mcp"
    assert current["tool"] == "mec_create_emr_case"


def test_concurrent_mode_exposes_mcp_with_reply_same_turn():
    config = _triage_config(mode="concurrent")
    state = update_collected_fields(
        empty_collection_state(), {"主诉": "头痛", "持续时间": "三天"}, config
    )
    assert "mec_create_emr_case" in hard_gated_tool_names(state, config)
    state = append_probe_note(state, config, "伴恶心")
    state = finish_probe(state, config, reason="enough")
    assert hard_gated_tool_names(state, config) == set()
    assert pending_required_action_tools(state, config) == ["mec_create_emr_case"]


def test_serial_reply_then_mcp_hard_gates_write_until_reply_done():
    config = _triage_config(mode="serial")
    state = update_collected_fields(
        empty_collection_state(), {"主诉": "头痛", "持续时间": "三天"}, config
    )
    state = append_probe_note(state, config, "伴恶心")
    state = finish_probe(state, config, reason="enough")
    # Serial: reply current → write MCP stays hard-gated.
    assert "mec_create_emr_case" in hard_gated_tool_names(state, config)
    assert pending_required_action_tools(state, config) == []

    state = update_collected_fields(state, {"推荐科室": "神经内科[sjnk]"}, config)
    assert hard_gated_tool_names(state, config) == set()
    assert pending_required_action_tools(state, config) == ["mec_create_emr_case"]


def test_after_probe_defers_decision_slot():
    config = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "required_actions_mode": "concurrent",
        "probe": {
            "enabled": True,
            "min_rounds": 1,
            "max_rounds": 2,
            "allow_skip": True,
        },
        "schema": {
            "source": "inline",
            "fields": [
                {"name": "主诉", "required": True},
                {"name": "推荐科室", "required": True, "after_probe": True},
            ],
        },
        "required_actions": [
            {"type": "reply", "field": "推荐科室", "when": "missing_empty"},
            {"type": "mcp", "tool": "mec_create_emr_case", "when": "missing_empty"},
        ],
    }
    state = update_collected_fields(empty_collection_state(), {"主诉": "头晕"}, config)
    assert state["phase"] == PHASE_PROBING
    assert "推荐科室" in state["missing"]
    state = append_probe_note(state, config, "姿势相关")
    state = finish_probe(state, config, reason="enough")
    assert state["phase"] == PHASE_COLLECTING
    state = update_collected_fields(state, {"推荐科室": "神经内科[sjnk]"}, config)
    assert state["phase"] == PHASE_CONFIRMED


def test_field_probe_before_global_probe():
    config = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "probe": {
            "enabled": True,
            "min_rounds": 1,
            "max_rounds": 2,
            "allow_skip": True,
        },
        "schema": {
            "source": "inline",
            "fields": [
                {
                    "name": "主诉",
                    "required": True,
                    "probe": {"enabled": True, "min_rounds": 1, "max_rounds": 2},
                },
                {"name": "推荐科室", "required": True, "after_probe": True},
            ],
        },
    }
    state = update_collected_fields(empty_collection_state(), {"主诉": "头痛"}, config)
    assert state["phase"] == PHASE_COLLECTING
    assert state["field_probe_active"] == "主诉"
    state = append_probe_note(state, config, "胀痛", field="主诉")
    assert state["field_probes"]["主诉"]["rounds"] == 1
    state = finish_probe(state, config, field="主诉", reason="主诉已够细")
    assert not state.get("field_probe_active")
    assert state["phase"] == PHASE_PROBING
    state = append_probe_note(state, config, "全局鉴别")
    state = finish_probe(state, config, reason="可推荐")
    assert state["phase"] == PHASE_COLLECTING
    assert "推荐科室" in state["missing"]


def test_hard_gate_hides_write_tool_until_probe_and_missing_done():
    # Serial: MCP stays gated through probe and until reply field is filled.
    config = _triage_config(mode="serial")
    state = update_collected_fields(
        empty_collection_state(), {"主诉": "头痛", "持续时间": "三天"}, config
    )
    assert state["phase"] == PHASE_PROBING
    assert "mec_create_emr_case" in hard_gated_tool_names(state, config)

    state = append_probe_note(state, config, "伴恶心")
    state = finish_probe(state, config, reason="enough")
    assert "mec_create_emr_case" in hard_gated_tool_names(state, config)

    state = update_collected_fields(state, {"推荐科室": "神经内科[sjnk]"}, config)
    assert state["phase"] == PHASE_CONFIRMED
    assert hard_gated_tool_names(state, config) == set()
    assert pending_required_action_tools(state, config) == ["mec_create_emr_case"]

    config2 = {
        **config,
        "required_actions": [
            {"type": "reply", "field": "推荐科室", "when": "missing_empty"},
            {
                "type": "mcp",
                "tool": "mec_create_emr_case",
                "when": "missing_empty",
                "hard_gate": False,
            },
        ],
    }
    early = update_collected_fields(
        empty_collection_state(), {"主诉": "头痛", "持续时间": "三天"}, config2
    )
    assert hard_gated_tool_names(early, config2) == set()


def test_filter_collection_gated_tools_drops_write_mcp():
    config = _triage_config(mode="concurrent")
    state = update_collected_fields(
        empty_collection_state(), {"主诉": "头痛", "持续时间": "三天"}, config
    )
    run_context = SimpleNamespace(
        session_state={"collection": state, "workflow": config},
        dependencies={"agent_config": {"workflow": config}},
        metadata={},
    )
    tools = [
        SimpleNamespace(name="md_get_dept_list"),
        SimpleNamespace(name="mec_create_emr_case"),
        SimpleNamespace(name="collection_probe_note"),
    ]
    kept = filter_collection_gated_tools(run_context, tools)
    names = [t.name for t in kept]
    assert "mec_create_emr_case" not in names
    assert "md_get_dept_list" in names
    assert "collection_probe_note" in names


if __name__ == "__main__":
    test_probe_defaults_hints_empty()
    test_probe_min_rounds_blocks_early_finish_until_min()
    test_triage_probe_before_recommended_dept()
    test_concurrent_mode_exposes_mcp_with_reply_same_turn()
    test_serial_reply_then_mcp_hard_gates_write_until_reply_done()
    test_after_probe_defers_decision_slot()
    test_field_probe_before_global_probe()
    test_hard_gate_hides_write_tool_until_probe_and_missing_done()
    test_filter_collection_gated_tools_drops_write_mcp()
    print("ok")

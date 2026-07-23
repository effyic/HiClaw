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
    advance_collection_phase,
    append_probe_note,
    empty_collection_state,
    filter_collection_gated_tools,
    finish_probe,
    hard_gated_tool_names,
    pending_required_action_tools,
    resolve_probe_config,
    update_collected_fields,
)


def _triage_config():
    return {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "probe": {
            "enabled": True,
            "max_rounds": 2,
            "allow_skip": True,
            "hints": [],
            "gate_fields": ["主诉", "持续时间"],
        },
        "schema": {
            "source": "inline",
            "fields": [
                {"name": "主诉", "required": True},
                {"name": "持续时间", "required": True},
                {"name": "推荐科室", "required": True},
            ],
        },
        "required_actions": [
            {"type": "mcp", "tool": "mec_create_emr_case", "when": "missing_empty"},
        ],
    }


def test_probe_defaults_hints_empty():
    cfg = resolve_probe_config({"probe": {"enabled": True, "max_rounds": 3}})
    assert cfg is not None
    assert cfg["hints"] == []
    assert cfg["gate_fields"] == []
    assert cfg["min_rounds"] == 0
    assert cfg["goal"] == ""
    assert "拒绝" in cfg["early_finish_keywords"]
    assert "急症" not in cfg["early_finish_keywords"]


def test_probe_min_rounds_blocks_early_finish():
    config = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "probe": {
            "enabled": True,
            "min_rounds": 2,
            "max_rounds": 4,
            "allow_skip": True,
            "hints": [],
            "early_finish_keywords": ["拒绝", "急症"],
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
    state = finish_probe(state, config, reason="患者拒绝继续")
    assert state["probe_done"] is True


def test_triage_probe_before_recommended_dept():
    config = _triage_config()
    state = update_collected_fields(
        empty_collection_state(), {"主诉": "头痛", "持续时间": "三天"}, config
    )
    assert state["phase"] == PHASE_PROBING
    assert "推荐科室" in state["missing"]
    assert pending_required_action_tools(state, config) == []

    state = append_probe_note(state, config, "伴恶心")
    assert state["probe_rounds"] == 1
    state = finish_probe(state, config, reason="enough")
    assert state["phase"] == PHASE_COLLECTING
    assert pending_required_action_tools(state, config) == []

    state = update_collected_fields(state, {"推荐科室": "神经内科[sjnk]"}, config)
    assert state["phase"] == PHASE_CONFIRMED
    assert pending_required_action_tools(state, config) == ["mec_create_emr_case"]


def test_hard_gate_hides_write_tool_until_probe_and_missing_done():
    config = _triage_config()
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
            {
                "type": "mcp",
                "tool": "mec_create_emr_case",
                "when": "missing_empty",
                "hard_gate": False,
            }
        ],
    }
    early = update_collected_fields(
        empty_collection_state(), {"主诉": "头痛", "持续时间": "三天"}, config2
    )
    assert hard_gated_tool_names(early, config2) == set()


def test_filter_collection_gated_tools_drops_write_mcp():
    config = _triage_config()
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
    test_probe_min_rounds_blocks_early_finish()
    test_triage_probe_before_recommended_dept()
    test_hard_gate_hides_write_tool_until_probe_and_missing_done()
    test_filter_collection_gated_tools_drops_write_mcp()
    print("ok")

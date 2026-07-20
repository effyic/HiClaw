"""Unit tests for collection probe (enrichment loop)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agno_worker.tenant.collection import (  # noqa: E402
    PHASE_COLLECTING,
    PHASE_PROBING,
    PHASE_CONFIRMED,
    advance_collection_phase,
    append_probe_note,
    empty_collection_state,
    finish_probe,
    pending_required_action_tools,
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
            "gate_fields": ["主诉", "持续时间"],
            "hints": ["伴随症状"],
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


def test_probe_starts_after_gate_fields_before_recommended_dept():
    config = _triage_config()
    state = empty_collection_state()
    state = update_collected_fields(
        state, {"主诉": "头痛", "持续时间": "三天"}, config
    )
    assert state["phase"] == PHASE_PROBING
    assert "推荐科室" in state["missing"]
    assert pending_required_action_tools(state, config) == []


def test_probe_note_then_collect_recommended_then_required_action():
    config = _triage_config()
    state = empty_collection_state()
    state = update_collected_fields(
        state, {"主诉": "头痛", "持续时间": "三天"}, config
    )
    state = append_probe_note(state, config, "伴恶心，无发热")
    assert state["probe_rounds"] == 1
    assert state["phase"] == PHASE_PROBING

    state = finish_probe(state, config, reason="enough")
    assert state["probe_done"] is True
    assert state["phase"] == PHASE_COLLECTING
    assert pending_required_action_tools(state, config) == []

    state = update_collected_fields(
        state, {"推荐科室": "神经内科[sjnk]"}, config
    )
    assert state["phase"] == PHASE_CONFIRMED
    assert pending_required_action_tools(state, config) == ["mec_create_emr_case"]


def test_inquiry_probe_after_all_required():
    config = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "probe": {"enabled": True, "max_rounds": 1, "allow_skip": True},
        "schema": {
            "source": "inline",
            "fields": [
                {"name": "主诉症状", "required": True},
                {"name": "过敏史", "required": False},
            ],
        },
        "required_actions": [
            {"type": "mcp", "tool": "mec_create_emr_case", "when": "missing_empty"},
        ],
    }
    state = update_collected_fields(
        empty_collection_state(), {"主诉症状": "咳嗽"}, config
    )
    assert state["phase"] == PHASE_PROBING
    assert pending_required_action_tools(state, config) == []
    state = append_probe_note(state, config, "夜间加重")
    assert state["probe_done"] is True
    state = advance_collection_phase(state, config)
    assert state["phase"] == PHASE_CONFIRMED
    assert pending_required_action_tools(state, config) == ["mec_create_emr_case"]


if __name__ == "__main__":
    test_probe_starts_after_gate_fields_before_recommended_dept()
    test_probe_note_then_collect_recommended_then_required_action()
    test_inquiry_probe_after_all_required()
    print("ok")

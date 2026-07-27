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


def _triage_config(*, mode: str = "serial"):
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
            ],
        },
        "required_actions": [
            {
                "type": "reply",
                "field": "推荐科室",
                "when": "missing_empty",
                "goal": "最终推荐科室，格式 名称[code]",
                "pattern": "^.+\\[[A-Za-z0-9_-]+\\]$",
            },
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

    # Hard-gate: reply result slot cannot be filled while probe is active.
    try:
        update_collected_fields(state, {"推荐科室": "神经内科[sjnk]"}, config)
        raise AssertionError("expected deferred reply-slot hard-gate during probing")
    except ValueError as exc:
        assert "deferred" in str(exc) or "reply" in str(exc)

    state = append_probe_note(state, config, "伴恶心")
    assert state["probe_rounds"] == 1
    state = finish_probe(state, config, reason="enough")
    assert state["phase"] == PHASE_COLLECTING
    # Serial: reply current → MCP still hard-gated / not yet actionable.
    assert pending_required_action_tools(state, config) == []
    assert "mec_create_emr_case" in hard_gated_tool_names(state, config)
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


def test_serial_exposes_mcp_only_after_reply_field():
    """Serial: MCP stays gated until reply field is filled (same-turn unlock OK)."""
    config = _triage_config(mode="serial")
    state = update_collected_fields(
        empty_collection_state(), {"主诉": "头痛", "持续时间": "三天"}, config
    )
    assert "mec_create_emr_case" in hard_gated_tool_names(state, config)
    state = append_probe_note(state, config, "伴恶心")
    state = finish_probe(state, config, reason="enough")
    # Reply step current → write MCP still hard-gated.
    assert "mec_create_emr_case" in hard_gated_tool_names(state, config)
    assert pending_required_action_tools(state, config) == []
    # Full remaining chain is visible for same-turn prompts.
    from agno_worker.tenant.collection import pending_required_actions

    pending = pending_required_actions(state, config)
    assert any(a.get("type") == "reply" for a in pending)
    assert any(a.get("tool") == "mec_create_emr_case" for a in pending)

    state = update_collected_fields(state, {"推荐科室": "神经内科[sjnk]"}, config)
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


def test_legacy_concurrent_mode_coerced_to_serial():
    from agno_worker.tenant.collection.kinds.dialogue.config import required_actions_mode

    assert required_actions_mode({"required_actions_mode": "concurrent"}) == "serial"
    assert required_actions_mode({"required_actions_mode": "serial"}) == "serial"
    assert required_actions_mode({}) == "serial"


def test_reply_result_slot_defers_decision():
    config = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "required_actions_mode": "serial",
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
            ],
        },
        "required_actions": [
            {"type": "reply", "field": "推荐科室", "when": "missing_empty"},
        ],
    }
    state = update_collected_fields(empty_collection_state(), {"主诉": "头痛"}, config)
    assert state["phase"] == PHASE_COLLECTING
    assert state["current_field"] == "主诉"
    assert state["field_stage"] == "probe"
    assert state["field_probe_active"] == "主诉"
    state = append_probe_note(state, config, "胀痛", field="主诉")
    assert state["field_probes"]["主诉"]["rounds"] == 1
    state = finish_probe(state, config, field="主诉", reason="主诉已够细")
    assert not state.get("field_probe_active")
    assert not state.get("current_field")
    assert state["phase"] == PHASE_PROBING
    state = append_probe_note(state, config, "全局鉴别")
    state = finish_probe(state, config, reason="可推荐")
    assert state["phase"] == PHASE_COLLECTING
    assert "推荐科室" in state["missing"]


def test_cursor_stashes_ahead_and_blocks_while_min_probe():
    """Cursor: stash multi-facts; hard-block later writes until field probe min met."""
    config = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "probe": {
            "enabled": True,
            "min_rounds": 0,
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
                {
                    "name": "持续时间",
                    "required": True,
                },
            ],
        },
    }
    state = update_collected_fields(
        empty_collection_state(),
        {"主诉": "头晕", "持续时间": "两天"},
        config,
    )
    assert state["collected"] == {"主诉": "头晕"}
    assert state["pending_collected"] == {"持续时间": "两天"}
    assert state["current_field"] == "主诉"
    assert state["field_stage"] == "probe"

    try:
        update_collected_fields(state, {"持续时间": "两天"}, config)
        raise AssertionError("expected block of next slot during field probe min")
    except ValueError as exc:
        assert "cursor locked" in str(exc)
        assert "主诉" in str(exc)

    state = append_probe_note(state, config, "体位相关", field="主诉")
    state = finish_probe(state, config, field="主诉", reason="主诉够细")
    # Pending auto-applies when cursor advances.
    assert state["collected"]["持续时间"] == "两天"
    assert "持续时间" not in (state.get("pending_collected") or {})
    assert state["current_field"] == ""
    assert state["phase"] == PHASE_PROBING


def test_cursor_auto_finish_probe_when_writing_next_and_min_met():
    """min_rounds met: writing the next field auto-finishes current field probe."""
    config = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "probe": {
            "enabled": True,
            "min_rounds": 0,
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
                {"name": "持续时间", "required": True},
            ],
        },
    }
    state = update_collected_fields(empty_collection_state(), {"主诉": "头晕"}, config)
    assert state["field_stage"] == "probe"
    state = append_probe_note(state, config, "体位相关", field="主诉")
    state = update_collected_fields(state, {"持续时间": "两天"}, config)
    assert state["collected"]["主诉"] == "头晕"
    assert state["collected"]["持续时间"] == "两天"
    assert state["field_probes"]["主诉"]["done"] is True
    assert state["phase"] == PHASE_PROBING
    assert not state.get("current_field")




def test_cursor_cascade_pending_without_field_probe():
    """Without field probes, stashed facts cascade onto the advancing cursor."""
    config = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "probe": {
            "enabled": True,
            "min_rounds": 0,
            "max_rounds": 1,
            "allow_skip": True,
        },
        "schema": {
            "source": "inline",
            "fields": [
                {"name": "主诉", "required": True},
                {"name": "持续时间", "required": True},
                {"name": "既往病史", "required": True},
            ],
        },
    }
    state = update_collected_fields(
        empty_collection_state(),
        {"主诉": "头晕", "持续时间": "两天", "既往病史": "高血压"},
        config,
    )
    assert state["collected"] == {
        "主诉": "头晕",
        "持续时间": "两天",
        "既往病史": "高血压",
    }
    assert not (state.get("pending_collected") or {})
    assert not state.get("current_field")
    assert state["phase"] == PHASE_PROBING


def test_cursor_pending_applies_when_field_probe_hits_max_rounds():
    """max_rounds auto-done must cascade pending like finish_probe."""
    config = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "probe": {
            "enabled": True,
            "min_rounds": 0,
            "max_rounds": 2,
            "allow_skip": True,
        },
        "schema": {
            "source": "inline",
            "fields": [
                {
                    "name": "主诉",
                    "required": True,
                    "probe": {"enabled": True, "min_rounds": 1, "max_rounds": 1},
                },
                {"name": "持续时间", "required": True},
            ],
        },
    }
    state = update_collected_fields(
        empty_collection_state(),
        {"主诉": "头晕", "持续时间": "两天"},
        config,
    )
    assert state["pending_collected"] == {"持续时间": "两天"}
    state = append_probe_note(state, config, "体位相关", field="主诉")
    assert state["field_probes"]["主诉"]["done"] is True
    assert state["collected"]["持续时间"] == "两天"
    assert not (state.get("pending_collected") or {})
    assert state["phase"] == PHASE_PROBING


def test_enrichment_skipped_when_only_reply_result_slots():
    """Schemas with no pre-probe slots must not empty-run enrichment."""
    from agno_worker.tenant.collection import (
        is_probe_finished,
        is_probe_ready,
        load_schema_into_state,
    )

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
            ],
        },
        "required_actions": [
            {"type": "reply", "field": "决策项", "when": "missing_empty"},
        ],
    }
    state = load_schema_into_state(
        empty_collection_state(),
        config["schema"]["fields"],
        config,
    )
    assert state["phase"] == PHASE_COLLECTING
    assert "决策项" in state["missing"]
    assert not state.get("current_field")
    assert is_probe_ready(state, config) is False
    assert is_probe_finished(state, config) is True
    state = update_collected_fields(state, {"决策项": "选项A"}, config)
    assert state["collected"]["决策项"] == "选项A"
    assert state["phase"] == PHASE_CONFIRMED


def test_ask_batch_size_hard_cursor_always_one():
    from agno_worker.tenant.collection import ask_batch_size

    assert ask_batch_size({"ask_batch_size": 5}) == 1
    assert ask_batch_size({"ask_batch_size": 1}) == 1
    assert ask_batch_size(None) == 1


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
    config = _triage_config(mode="serial")
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
    test_serial_exposes_mcp_only_after_reply_field()
    test_serial_reply_then_mcp_hard_gates_write_until_reply_done()
    test_legacy_concurrent_mode_coerced_to_serial()
    test_reply_result_slot_defers_decision()
    test_field_probe_before_global_probe()
    test_cursor_stashes_ahead_and_blocks_while_min_probe()
    test_cursor_auto_finish_probe_when_writing_next_and_min_met()
    test_cursor_cascade_pending_without_field_probe()
    test_cursor_pending_applies_when_field_probe_hits_max_rounds()
    test_enrichment_skipped_when_only_reply_result_slots()
    test_ask_batch_size_hard_cursor_always_one()
    test_hard_gate_hides_write_tool_until_probe_and_missing_done()
    test_filter_collection_gated_tools_drops_write_mcp()
    print("ok")

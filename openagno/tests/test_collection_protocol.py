"""Unit tests for config-driven collection dialogue protocol."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agno_worker.runtime.storage import SLIM_SESSION_STATE_KEYS, slim_session_state
from agno_worker.tenant.collection import (
    COLLECTION_STATE_KEY,
    PHASE_COLLECTING,
    PHASE_CONFIRMED,
    PHASE_DONE,
    PHASE_READY,
    advance_collection_phase,
    confirm_collection,
    ensure_collection_state,
    load_schema_into_state,
    mark_collection_done,
    resolve_collection_config,
    store_collection_draft,
    sync_collection_into_session_state,
    update_collected_fields,
)


INQUIRY_WORKFLOW = {
    "kind": "medical",
    "phase": "inquiry",
    "collection": {
        "kind": "collection_dialogue",
        "confirm_required": True,
        "ask_batch_size": 2,
        "schema": {
            "source": "inline",
            "fields": [
                {"name": "主诉", "required": True, "description": "主要症状"},
                {"name": "持续时间", "required": True},
                {"name": "过敏史", "required": False},
            ],
        },
        "complete_action": {"type": "mcp", "tool": "mec_create_emr_case"},
    },
}


def test_resolve_nested_and_top_level_config():
    assert resolve_collection_config(INQUIRY_WORKFLOW) is not None
    top = {"kind": "collection_dialogue", "schema": {"source": "inline", "fields": []}}
    assert resolve_collection_config(top) is top
    assert resolve_collection_config({"kind": "medical"}) is None


def test_slim_persists_collection_for_cluster():
    assert "collection" in SLIM_SESSION_STATE_KEYS
    state = {
        "tenant_id": "1",
        "phase": PHASE_COLLECTING,
        "collection": {"phase": PHASE_COLLECTING, "collected": {"主诉": "头痛"}},
        "ephemeral": "drop-me",
        "_debug_request": True,
    }
    slim = slim_session_state(state)
    assert "collection" in slim
    assert slim["collection"]["collected"]["主诉"] == "头痛"
    assert "ephemeral" not in slim
    assert "_debug_request" not in slim


def test_unknown_field_names_rejected():
    config = resolve_collection_config(INQUIRY_WORKFLOW)
    state = load_schema_into_state({}, None, config)
    with pytest.raises(ValueError, match="unknown field names"):
        update_collected_fields(state, {"主诉": "头痛", "不存在的字段": "x"}, config)


def test_update_requires_schema():
    config = resolve_collection_config(
        {
            "kind": "collection_dialogue",
            "schema": {"source": "mcp", "tool": "mec_get_emr_field_list"},
            "confirm_required": False,
        }
    )
    state = ensure_collection_state({}, config)
    with pytest.raises(ValueError, match="schema is empty"):
        update_collected_fields(state, {"主诉": "头痛"}, config)


def test_early_write_and_continue_asking():
    """Allow mark_done with missing fields; FSM returns to collecting."""
    config = resolve_collection_config(INQUIRY_WORKFLOW)
    state = load_schema_into_state({}, None, config)
    state = update_collected_fields(state, {"主诉": "咳嗽"}, config)
    assert state["phase"] == PHASE_COLLECTING
    assert "持续时间" in state["missing"]

    state = mark_collection_done(state, config, result="partial-ok")
    assert state["completed"] is True
    assert state["phase"] == PHASE_COLLECTING
    assert "持续时间" in state["missing"]


def test_complete_done_then_supplement_and_rewrite():
    config = resolve_collection_config(INQUIRY_WORKFLOW)
    state = load_schema_into_state({}, None, config)
    state = update_collected_fields(
        state, {"主诉": "咳嗽", "持续时间": "1周"}, config
    )
    assert state["phase"] == PHASE_READY
    state = confirm_collection(state, config)
    assert state["phase"] == PHASE_CONFIRMED
    state = store_collection_draft(state, config, "# 病历\n咳嗽一周")
    assert state["draft_payload"].startswith("# 病历")
    state = mark_collection_done(state, config, result="ok")
    assert state["phase"] == PHASE_DONE
    assert state["completed"] is True

    # User later adds optional field — stay writable
    state = update_collected_fields(state, {"过敏史": "青霉素"}, config)
    assert state["completed"] is True
    assert state["phase"] == PHASE_DONE
    state = mark_collection_done(state, config, result="updated")
    assert state["complete_result"] == "updated"


def test_sync_does_not_use_workflow_scenario_phase():
    ctx = SimpleNamespace(metadata={"confirm": True}, dependencies={})
    prior = {
        "phase": "inquiry",
        COLLECTION_STATE_KEY: {
            "phase": PHASE_READY,
            "schema": [
                {"name": "主诉", "required": True},
                {"name": "持续时间", "required": True},
            ],
            "collected": {"主诉": "发热", "持续时间": "2天"},
            "missing": [],
            "user_confirmed": False,
        },
        "workflow": INQUIRY_WORKFLOW,
    }
    merged = sync_collection_into_session_state(prior, ctx, INQUIRY_WORKFLOW)
    assert merged["phase"] == PHASE_CONFIRMED
    assert merged[COLLECTION_STATE_KEY]["user_confirmed"] is True
    advanced = advance_collection_phase(
        merged[COLLECTION_STATE_KEY],
        resolve_collection_config(INQUIRY_WORKFLOW),
    )
    assert advanced["phase"] == PHASE_CONFIRMED

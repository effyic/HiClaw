"""Unit tests for config-driven collection dialogue protocol."""
from __future__ import annotations

from types import SimpleNamespace

from agno_worker.hooks.protocols import MCPServerConfig
from agno_worker.runtime.storage import SLIM_SESSION_STATE_KEYS, slim_session_state
from agno_worker.tenant.collection import (
    COLLECTION_STATE_KEY,
    PHASE_COLLECTING,
    PHASE_CONFIRMED,
    PHASE_DONE,
    PHASE_READY,
    advance_collection_phase,
    apply_collection_mcp_excludes,
    authorize_complete,
    confirm_collection,
    ensure_collection_state,
    is_write_authorized,
    load_schema_into_state,
    mark_collection_done,
    resolve_collection_config,
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
        "gated_mcp_tools": ["mec_create_emr_case"],
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


def test_phase_advances_and_gates_write_tools():
    config = resolve_collection_config(INQUIRY_WORKFLOW)
    state = load_schema_into_state({}, None, config)
    assert state["phase"] == PHASE_COLLECTING
    assert set(state["missing"]) == {"主诉", "持续时间"}

    state = update_collected_fields(state, {"主诉": "头痛", "持续时间": "3天"}, config)
    assert state["missing"] == []
    assert state["phase"] == PHASE_READY
    assert not is_write_authorized(state, config)

    state = confirm_collection(state, config)
    assert state["phase"] == PHASE_CONFIRMED
    assert is_write_authorized(state, config)

    servers = [
        MCPServerConfig(
            name="medical",
            url="http://example/mcp",
            include_tools=["mec_get_emr_field_list", "mec_create_emr_case"],
        )
    ]
    ctx = SimpleNamespace(
        session_state={
            "workflow": INQUIRY_WORKFLOW,
            COLLECTION_STATE_KEY: ensure_collection_state(
                {"phase": PHASE_COLLECTING, "schema": state["schema"], "collected": {}},
                config,
            ),
        },
        metadata={},
        dependencies={},
    )
    excluded = apply_collection_mcp_excludes(ctx, servers)
    assert "mec_create_emr_case" in excluded[0].exclude_tools

    ctx.session_state[COLLECTION_STATE_KEY] = state
    opened = apply_collection_mcp_excludes(ctx, servers)
    assert "mec_create_emr_case" not in (opened[0].exclude_tools or [])


def test_complete_and_done():
    config = resolve_collection_config(INQUIRY_WORKFLOW)
    state = load_schema_into_state({}, None, config)
    state = update_collected_fields(
        state, {"主诉": "咳嗽", "持续时间": "1周"}, config
    )
    state = confirm_collection(state, config)
    state = authorize_complete(state, config, "# 病历\n咳嗽一周")
    assert state["completion_authorized"] is True
    assert state["draft_payload"].startswith("# 病历")
    state = mark_collection_done(state, config, result="ok")
    assert state["phase"] == PHASE_DONE
    assert state["completed"] is True
    assert not is_write_authorized(state, config)


def test_sync_does_not_use_workflow_scenario_phase():
    ctx = SimpleNamespace(metadata={"confirm": True}, dependencies={})
    # Pretend slots already filled from a previous replica turn.
    prior = {
        "phase": "inquiry",  # stale scenario label from old builds
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
    advanced = advance_collection_phase(merged[COLLECTION_STATE_KEY], resolve_collection_config(INQUIRY_WORKFLOW))
    assert advanced["phase"] == PHASE_CONFIRMED

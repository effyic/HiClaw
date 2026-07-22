"""Unit tests for workflow.scripts opening/closing phase hooks."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agno_worker.tenant.collection import (  # noqa: E402
    PHASE_COLLECTING,
    PHASE_READY,
    apply_scripts_progress_from_run,
    collection_instructions_appendix,
    empty_collection_state,
    resolve_scripts_config,
    update_collected_fields,
)


def _config_with_scripts(**extra):
    cfg = {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "scripts": {
            "opening": "您好！我是测试助手。请描述不适。",
            "closing": "根据您的情况，建议如下。温馨提示：带好证件。",
            "opening_policy": "first_turn_required",
        },
        "schema": {
            "source": "inline",
            "fields": [
                {"name": "主诉", "required": True},
                {"name": "推荐科室", "required": True},
            ],
        },
        "required_actions": [
            {"type": "mcp", "tool": "mec_create_emr_case", "when": "missing_empty"},
        ],
    }
    cfg.update(extra)
    return cfg


def test_resolve_scripts_config():
    assert resolve_scripts_config({}) is None
    scripts = resolve_scripts_config(_config_with_scripts())
    assert scripts["opening"].startswith("您好")
    assert scripts["opening_policy"] == "first_turn_required"


def test_appendix_requires_opening_on_first_collecting_turn():
    from agno_worker.tenant.collection import ensure_collection_state
    config = _config_with_scripts()
    state = ensure_collection_state(empty_collection_state(), config)
    state["phase"] = PHASE_COLLECTING
    text = collection_instructions_appendix(state, config)
    assert "## dialogue_scripts" in text
    assert "OPENING REQUIRED" in text
    assert "scripts.opening" in text
    assert "您好！我是测试助手" in text


def test_appendix_skips_opening_after_delivered():
    config = _config_with_scripts()
    state = empty_collection_state()
    state["phase"] = PHASE_COLLECTING
    state["opening_delivered"] = True
    state = update_collected_fields(state, {"主诉": "头晕"}, config)
    text = collection_instructions_appendix(state, config)
    assert "OPENING REQUIRED" not in text


def test_apply_scripts_marks_opening_after_reply():
    config = _config_with_scripts()
    state = empty_collection_state()
    state["phase"] = PHASE_COLLECTING
    out = apply_scripts_progress_from_run(state, config, had_patient_reply=True)
    assert out["opening_delivered"] is True


def test_appendix_requires_closing_when_ready():
    config = _config_with_scripts()
    state = empty_collection_state()
    state = update_collected_fields(
        state, {"主诉": "头晕", "推荐科室": "神经内科[sjnk]"}, config
    )
    state["phase"] = PHASE_READY
    state["probe_done"] = True
    state["opening_delivered"] = True
    text = collection_instructions_appendix(state, config)
    assert "CLOSING REQUIRED" in text
    assert "温馨提示：带好证件" in text

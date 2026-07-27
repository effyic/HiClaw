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


def _two_reply_config():
    return _config_with_scripts(
        required_actions=[
            {
                "type": "reply",
                "field": "阶段小结",
                "user_visible": True,
                "when": "missing_empty",
            },
            {
                "type": "reply",
                "field": "推荐科室",
                "user_visible": True,
                "when": "missing_empty",
            },
            {
                "type": "mcp",
                "tool": "mec_create_emr_case",
                "when": "missing_empty",
                "user_visible": False,
            },
        ]
    )


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
    config = _config_with_scripts(
        required_actions=[
            {
                "type": "reply",
                "field": "推荐科室",
                "user_visible": True,
                "when": "missing_empty",
            },
            {
                "type": "mcp",
                "tool": "mec_create_emr_case",
                "when": "missing_empty",
                "user_visible": False,
            },
        ]
    )
    state = empty_collection_state()
    state = update_collected_fields(state, {"主诉": "头晕"}, config)
    state["collected"]["推荐科室"] = "神经内科[sjnk]"
    state["phase"] = PHASE_READY
    state["probe_done"] = True
    state["opening_delivered"] = True
    text = collection_instructions_appendix(state, config)
    assert "REQUIRED ACTIONS (silent chain)" in text


def test_progress_after_first_reply_no_closing():
    from agno_worker.tenant.collection.kinds.dialogue.scripts import (
        compose_patient_reply_progress,
    )

    config = _two_reply_config()
    state = empty_collection_state()
    state["collected"] = {
        "阶段小结": "目前信息已汇总如下。",
    }
    state["actions_done"] = {"reply:阶段小结": {"ok": True}}
    text = compose_patient_reply_progress(state, config)
    assert text == "目前信息已汇总如下。"
    assert "温馨提示" not in text


def test_progress_after_last_reply_includes_closing_before_mcp():
    from agno_worker.tenant.collection.kinds.dialogue.scripts import (
        compose_patient_reply_progress,
    )

    config = _two_reply_config()
    state = empty_collection_state()
    state["collected"] = {
        "阶段小结": "目前信息已汇总如下。",
        "推荐科室": "神经内科[sjnk]",
    }
    state["actions_done"] = {
        "reply:阶段小结": {"ok": True},
        "reply:推荐科室": {"ok": True},
    }
    text = compose_patient_reply_progress(state, config)
    assert "目前信息已汇总如下。" in text
    assert "建议您挂：神经内科[sjnk]" in text
    assert text.index("目前信息已汇总如下。") < text.index("建议您挂")
    assert "温馨提示：带好证件" in text


def test_compose_single_reply_with_reason_and_closing():
    from agno_worker.tenant.collection.kinds.dialogue.scripts import (
        compose_patient_reply_progress,
    )

    config = _config_with_scripts(
        required_actions=[
            {
                "type": "reply",
                "field": "推荐科室",
                "user_visible": True,
                "reason_field": "分科理由",
                "when": "missing_empty",
            },
            {
                "type": "mcp",
                "tool": "mec_create_emr_case",
                "when": "missing_empty",
                "user_visible": False,
            },
        ]
    )
    state = empty_collection_state()
    state["collected"] = {
        "分科理由": "头晕与体位相关，优先考虑神经系统问题。",
        "推荐科室": "神经内科[sjnk]",
    }
    state["actions_done"] = {"reply:推荐科室": {"ok": True}}
    text = compose_patient_reply_progress(state, config)
    assert text is not None
    assert text.count("建议您挂") == 1
    assert text.count("温馨提示") == 1
    assert "体位相关" in text
    assert text.index("体位相关") < text.index("建议您挂")

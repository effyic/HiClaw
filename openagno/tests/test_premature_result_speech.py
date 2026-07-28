"""Tests for protocol-driven premature result speech stripping."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agno_worker.tenant.collection import empty_collection_state  # noqa: E402
from agno_worker.tenant.collection.kinds.dialogue.scripts import (  # noqa: E402
    SPEECH_COMPOSE,
    SPEECH_SILENT,
    SPEECH_STREAM,
    PatientTurnSpeech,
    apply_scripts_progress_from_run,
    compose_or_keep_patient_reply,
    compose_patient_reply_progress,
    looks_like_result_speech_chunk,
    patient_sse_replace_content,
    resolve_patient_turn_speech,
    silent_reply_chain_active,
    strip_premature_result_speech,
)


def _triage_config():
    return {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "scripts": {
            "opening": "您好！我是智能分诊助手。",
            "closing": "温馨提示：\n- 请携带您的身份证和医保卡",
            "opening_policy": "first_turn_required",
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
                "reason_field": "分科理由",
                "user_visible": True,
                "when": "missing_empty",
                "pattern": r"^.+\[[A-Za-z0-9_-]+\]$",
                "patient_template": "{reason}\n\n建议您挂：{value}",
            },
            {
                "type": "mcp",
                "tool": "mec_create_emr_case",
                "when": "missing_empty",
                "user_visible": False,
            },
        ],
    }


def test_strip_via_silent_chain_when_pipeline_ready():
    config = _triage_config()
    state = empty_collection_state()
    state["collected"] = {"主诉": "头疼", "持续时间": "三天"}
    state["missing"] = ["推荐科室"]
    state["schema"] = [
        {"name": "主诉", "required": True},
        {"name": "持续时间", "required": True},
    ]
    assert silent_reply_chain_active(state, config)
    raw = (
        "根据您的描述，建议您前往 **神经内科[sjnk]** 就诊。\n\n"
        "温馨提示：\n- 请携带您的身份证和医保卡"
    )
    assert strip_premature_result_speech(raw, state, config) == ""


def test_strip_keeps_normal_question_while_collecting():
    config = _triage_config()
    state = empty_collection_state()
    state["collected"] = {"主诉": "头疼"}
    state["missing"] = ["持续时间", "推荐科室"]
    state["schema"] = [
        {"name": "主诉", "required": True},
        {"name": "持续时间", "required": True},
    ]
    raw = "这种胀痛的感觉持续多久了？"
    assert not silent_reply_chain_active(state, config)
    assert strip_premature_result_speech(raw, state, config) == raw


def test_strip_template_literal_and_pattern_while_collecting():
    config = _triage_config()
    state = empty_collection_state()
    state["collected"] = {"主诉": "头疼"}
    state["missing"] = ["持续时间", "推荐科室"]
    state["schema"] = [
        {"name": "主诉", "required": True},
        {"name": "持续时间", "required": True},
    ]
    raw = "建议您挂：神经内科[sjnk]\n\n这种胀痛的感觉持续多久了？"
    cleaned = strip_premature_result_speech(raw, state, config)
    assert "建议您挂" not in cleaned
    assert "持续多久" in cleaned
    assert looks_like_result_speech_chunk(
        "建议您挂：神经内科[sjnk]", state, config
    )


def test_patient_template_compose():
    config = _triage_config()
    state = empty_collection_state()
    state["collected"] = {
        "主诉": "头疼",
        "持续时间": "三天",
        "分科理由": "偏侧胀痛伴恶心",
        "推荐科室": "神经内科[sjnk]",
    }
    state["missing"] = []
    state["actions_done"] = {"reply:推荐科室": {"ok": True}}
    text = compose_patient_reply_progress(state, config)
    assert text is not None
    assert "建议您挂：神经内科[sjnk]" in text
    assert "偏侧胀痛" in text
    assert "温馨提示" in text


def test_compose_delivers_once_then_silences_mcp_followup():
    config = _triage_config()
    state = empty_collection_state()
    state["collected"] = {
        "主诉": "头疼",
        "持续时间": "三天",
        "推荐科室": "神经内科[sjnk]",
    }
    state["missing"] = []
    state["actions_done"] = {"reply:推荐科室": {"ok": True}}
    state["phase"] = "confirmed"
    first = compose_or_keep_patient_reply("模型又说了一遍", state, config)
    assert first is not None
    assert "建议您挂：神经内科[sjnk]" in first
    state = apply_scripts_progress_from_run(
        state, config, had_patient_reply=True
    )
    assert state.get("result_reply_delivered") is True
    second = compose_or_keep_patient_reply("再推一次科室", state, config)
    assert second == ""


def test_protocol_compose_never_wiped_by_empty_sse():
    """Empty wipe is not part of the SSE contract."""
    composed = "偏侧胀痛\n\n建议您挂：神经内科[sjnk]\n\n温馨提示"
    speech = resolve_patient_turn_speech(
        {
            **empty_collection_state(),
            "collected": {
                "主诉": "头疼",
                "持续时间": "三天",
                "分科理由": "偏侧胀痛",
                "推荐科室": "神经内科[sjnk]",
            },
            "missing": [],
            "actions_done": {"reply:推荐科室": {"ok": True}},
        },
        _triage_config(),
        reply_delivered_at_start=False,
    )
    assert speech.mode == SPEECH_COMPOSE
    # Already pushed via tool progress — final must skip, not clear.
    assert (
        patient_sse_replace_content(speech=speech, streamed_visible=speech.text)
        is None
    )
    # Silent follow-up must also skip (never content:"").
    assert (
        patient_sse_replace_content(
            speech=PatientTurnSpeech(SPEECH_SILENT, ""),
            streamed_visible=composed,
        )
        is None
    )


def test_compose_or_keep_keeps_model_after_full_delivery():
    config = _triage_config()
    state = empty_collection_state()
    state["collected"] = {
        "主诉": "头疼",
        "持续时间": "三天",
        "推荐科室": "神经内科[sjnk]",
    }
    state["missing"] = []
    state["actions_done"] = {
        "reply:推荐科室": {"ok": True},
        "mec_create_emr_case": {"ok": True},
    }
    state["phase"] = "done"
    state["completed"] = True
    state["result_reply_delivered"] = True
    text = compose_or_keep_patient_reply("好的，我知道了", state, config)
    assert text == "好的，我知道了"
    assert "建议您挂" not in (text or "")

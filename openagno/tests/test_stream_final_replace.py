"""Patient SSE replace contract: non-empty authoritative text only, never empty wipe."""
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
    patient_sse_replace_content,
    resolve_patient_turn_speech,
)


def _config():
    return {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "scripts": {
            "opening": "您好！",
            "closing": "温馨提示：请携带证件",
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


def test_identical_stream_skips_replace():
    opening = "您好！请描述一下主要不适。"
    assert (
        patient_sse_replace_content(
            speech=PatientTurnSpeech(SPEECH_STREAM, opening),
            streamed_visible=opening,
        )
        is None
    )


def test_compose_emits_when_not_yet_streamed():
    composed = "偏侧胀痛\n\n建议您挂：神经内科[sjnk]\n\n温馨提示：请携带证件"
    assert (
        patient_sse_replace_content(
            speech=PatientTurnSpeech(SPEECH_COMPOSE, composed),
            streamed_visible="",
        )
        == composed
    )


def test_compose_skips_when_progress_already_pushed():
    composed = "建议您挂：神经内科[sjnk]"
    assert (
        patient_sse_replace_content(
            speech=PatientTurnSpeech(SPEECH_COMPOSE, composed),
            streamed_visible=composed,
        )
        is None
    )


def test_silent_never_emits_even_if_buffer_nonempty():
    assert (
        patient_sse_replace_content(
            speech=PatientTurnSpeech(SPEECH_SILENT, ""),
            streamed_visible="泄漏的结果话术",
        )
        is None
    )


def test_empty_text_never_emits_wipe():
    assert (
        patient_sse_replace_content(
            speech=PatientTurnSpeech(SPEECH_STREAM, ""),
            streamed_visible="任意已显示文案",
        )
        is None
    )


def test_resolve_first_delivery_is_compose():
    config = _config()
    state = empty_collection_state()
    state["collected"] = {
        "主诉": "头疼",
        "持续时间": "三天",
        "分科理由": "偏侧胀痛",
        "推荐科室": "神经内科[sjnk]",
    }
    state["missing"] = []
    state["actions_done"] = {"reply:推荐科室": {"ok": True}}
    speech = resolve_patient_turn_speech(
        state, config, model_text="模型乱说", reply_delivered_at_start=False
    )
    assert speech.mode == SPEECH_COMPOSE
    assert "建议您挂：神经内科[sjnk]" in speech.text
    assert "温馨提示" in speech.text


def test_resolve_mcp_followup_is_silent():
    config = _config()
    state = empty_collection_state()
    state["collected"] = {
        "主诉": "头疼",
        "持续时间": "三天",
        "推荐科室": "神经内科[sjnk]",
    }
    state["missing"] = []
    state["actions_done"] = {"reply:推荐科室": {"ok": True}}
    state["result_reply_delivered"] = True
    speech = resolve_patient_turn_speech(
        state, config, model_text="又说一遍", reply_delivered_at_start=True
    )
    assert speech.mode == SPEECH_SILENT
    assert (
        patient_sse_replace_content(speech=speech, streamed_visible="已推送小结")
        is None
    )


def test_resolve_post_close_chitchat_streams_model():
    config = _config()
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
    state["result_reply_delivered"] = True
    state["completed"] = True
    speech = resolve_patient_turn_speech(
        state, config, model_text="好的，我知道了", reply_delivered_at_start=True
    )
    assert speech.mode == SPEECH_STREAM
    assert speech.text == "好的，我知道了"


def test_stale_silent_still_emits_posthook_body():
    """Event state may still look like silent-chain while run content already has compose."""
    from agno_worker.runtime.engine import AgnoRuntime

    config = _config()
    # Stale: pipeline ready, reply field not yet visible on this snapshot.
    stale = empty_collection_state()
    stale["collected"] = {"主诉": "头疼", "持续时间": "三天"}
    stale["missing"] = ["推荐科室"]
    stale["schema"] = [
        {"name": "主诉", "required": True},
        {"name": "持续时间", "required": True},
    ]
    body = (
        "偏侧胀痛伴恶心\n\n建议您挂：神经内科[sjnk]\n\n温馨提示：请携带证件"
    )
    # Simulate session_state wrapper used by engine._collection_ctx
    session_state = {"workflow": config, "collection": stale}
    text = AgnoRuntime._resolve_patient_final_text(
        session_state,
        model_text=body,
        reply_delivered_at_start=False,
        suppress_result_stream=True,
    )
    assert "建议您挂：神经内科[sjnk]" in text
    sse = AgnoRuntime._patient_final_sse_content(
        session_state,
        final_text=text,
        streamed_visible="",
        reply_delivered_at_start=False,
    )
    assert sse == text

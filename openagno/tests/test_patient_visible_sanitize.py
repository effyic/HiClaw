"""Tests for patient-visible reply sanitize + probe salvage."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agno_worker.tenant.collection import (  # noqa: E402
    PHASE_PROBING,
    apply_probe_salvage_and_nudge,
    empty_collection_state,
    sanitize_patient_visible_reply,
)


def test_sanitize_strips_system_tip_and_textual_tool_call():
    raw = (
        "这种轻微的头晕，是感觉周围东西在转，还是觉得自己站不稳？\n\n"
        'collection_probe_note(note="患者描述模糊，需继续澄清")\n\n'
        "[系统提示] 当前为扩采阶段（probing）：请根据用户本轮回答调用 collection_probe_note"
    )
    clean, salvaged = sanitize_patient_visible_reply(raw)
    assert "[系统提示]" not in clean
    assert "collection_probe_note" not in clean
    assert "周围东西在转" in clean
    assert len(salvaged) == 1
    assert salvaged[0]["tool"] == "collection_probe_note"
    assert "患者描述模糊" in salvaged[0]["note"]


def _probe_config():
    return {
        "kind": "collection_dialogue",
        "confirm_required": False,
        "probe": {"enabled": True, "max_rounds": 5},
        "schema": {
            "source": "inline",
            "fields": [{"name": "主诉", "required": True}],
        },
    }


def test_apply_probe_salvage_sets_nudge_when_missed():
    state = empty_collection_state()
    state["phase"] = PHASE_PROBING
    state["collected"] = {"主诉": "头晕"}
    state["schema"] = [{"name": "主诉", "required": True}]
    state["missing"] = []
    out = apply_probe_salvage_and_nudge(
        state, _probe_config(), tools_ok=set(), salvaged=[],
    )
    assert out["phase"] == PHASE_PROBING
    assert out["probe_nudge_due"] is True


def test_apply_probe_salvage_clears_nudge_when_salvaged():
    state = empty_collection_state()
    state["phase"] = PHASE_PROBING
    state["collected"] = {"主诉": "头晕"}
    state["schema"] = [{"name": "主诉", "required": True}]
    state["missing"] = []
    out = apply_probe_salvage_and_nudge(
        state,
        _probe_config(),
        tools_ok=set(),
        salvaged=[{"tool": "collection_probe_note", "note": "加班诱发头晕"}],
    )
    assert out["probe_nudge_due"] is False
    assert out["probe_rounds"] >= 1


if __name__ == "__main__":
    test_sanitize_strips_system_tip_and_textual_tool_call()
    test_apply_probe_salvage_sets_nudge_when_missed()
    test_apply_probe_salvage_clears_nudge_when_salvaged()
    print("ok")

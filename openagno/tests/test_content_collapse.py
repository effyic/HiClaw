"""Tests for domain-agnostic tool-turn content segment joining."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agno_worker.runtime.structured_output import (  # noqa: E402
    join_content_segments,
    prefer_last_assistant_after_tools,
)


def test_join_without_tools_keeps_all_deltas():
    # Token deltas within one reply must concatenate.
    assert join_content_segments(["你", "好", "世界"], tools_intervened=False) == "你好世界"


def test_join_with_tools_keeps_last_segment_only():
    pre = "正在查询，请稍候…"
    post = "结果如下：已完成。"
    out = join_content_segments([pre, post], tools_intervened=True)
    assert out == post


def test_join_single_segment_after_tools():
    assert join_content_segments(["仅一段"], tools_intervened=True) == "仅一段"


def test_prefer_last_assistant_after_tools():
    run = SimpleNamespace(
        tools=[{"tool_name": "lookup"}],
        messages=[
            SimpleNamespace(role="assistant", content="先说一遍"),
            SimpleNamespace(role="tool", content="{}"),
            SimpleNamespace(role="assistant", content="最终答复"),
        ],
    )
    assert prefer_last_assistant_after_tools(run) == "最终答复"


def test_prefer_last_skips_when_no_tools():
    run = SimpleNamespace(
        tools=[],
        messages=[SimpleNamespace(role="assistant", content="普通回复")],
    )
    assert prefer_last_assistant_after_tools(run) is None

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


def test_content_to_reply_preserves_status_marker():
    """Sync chat must return post-hook content (marker), not raw last message."""
    from agno_worker.runtime.structured_output import content_to_reply_text

    marked = (
        "建议您挂：神经内科[sjnk]\n\n"
        "<!--COLLECTION_STATUS {\"phase\":\"confirmed\"}-->"
    )
    run = SimpleNamespace(
        tools=[{"tool_name": "mec_create_emr_case"}],
        messages=[
            SimpleNamespace(role="assistant", content="建议您挂：神经内科[sjnk]"),
        ],
        content=marked,
    )
    # Engine should prefer run.content over prefer_last(...).
    preferred = prefer_last_assistant_after_tools(run)
    assert preferred == "建议您挂：神经内科[sjnk]"
    assert "COLLECTION_STATUS" in content_to_reply_text(run.content)


def test_collapse_exact_duplicate_tip_paragraphs():
    from agno_worker.runtime.structured_output import collapse_duplicate_paragraphs

    tip = "温馨提示：\n- 请携带您的身份证和医保卡"
    text = f"建议您挂：骨科[gk]\n\n{tip}\n\n建议您挂：骨科[gk]\n\n{tip}"
    out = collapse_duplicate_paragraphs(text)
    assert out.count("温馨提示") == 1
    assert out.count("建议您挂：骨科[gk]") == 1


def test_collapse_tool_turn_echo_dedupes_when_tools_intervened():
    from agno_worker.runtime.structured_output import collapse_tool_turn_echo

    # Historical pattern: pre-tool closing + post-tool restatement in one blob.
    text = (
        "根据您描述的症状，头晕与低头动作密切相关。\n\n"
        "建议您挂：**骨科[gk]**\n\n"
        "温馨提示：\n- 请携带您的身份证和医保卡\n- 如有之前的检查报告请一并携带\n\n"
        "根据您描述的症状，为您推荐的就诊科室如下。\n\n"
        "**推荐理由：**\n您的头晕与颈部姿势密切相关。\n\n"
        "**建议您挂：骨科[gk]**\n\n"
        "温馨提示：\n- 请携带您的身份证和医保卡\n- 如有之前的检查报告请一并携带"
    )
    out = collapse_tool_turn_echo(text, tools_intervened=True)
    assert out.count("温馨提示") == 1
    assert out.count("建议您挂") == 1
    assert "推荐理由" in out or "建议您挂" in out
    # Without tools, leave text untouched (no silent rewrite).
    soft = collapse_tool_turn_echo(text, tools_intervened=False)
    assert soft == text


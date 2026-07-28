"""Patient-visible reply sanitize and ask-quality tracking."""
from __future__ import annotations

import re
from typing import Any

from agno_worker.tenant.collection.kinds.dialogue.constants import PHASE_PROBING
from agno_worker.tenant.collection.kinds.dialogue.core import (
    append_probe_note,
    ensure_collection_state,
    is_field_probe_active,
)

_SYSTEM_TIP_RE = re.compile(r"\n*\[系统提示\][^\n]*", re.MULTILINE)
# Model sometimes prints tool calls as chat text instead of invoking tools.
_TEXTUAL_TOOL_CALL_RE = re.compile(
    r"(?:^|\n)\s*(collection_[a-z_]+)\s*\(\s*(?:note\s*=\s*)?"
    r"(?P<q>['\"])(?P<body>.*?)(?P=q)\s*\)\s*(?=\n|$)",
    re.DOTALL | re.IGNORECASE,
)
_TEXTUAL_TOOL_CALL_LOOSE_RE = re.compile(
    r"(?:^|\n)\s*collection_[a-z_]+\s*\([^()\n]*\)\s*(?=\n|$)",
    re.IGNORECASE,
)
# Never show write-back / EMR job status to the patient.
_WRITE_STATUS_LINE_RE = re.compile(
    r"(?m)^[^\n]*(?:电子病历|写库|病历生成|任务已提交|任务已受理|正在后台处理|"
    r"系统正在处理|请稍后重试|写入成功|写入失败)[^\n]*\n?"
)


def sanitize_patient_visible_reply(text: str | None) -> tuple[str, list[dict[str, str]]]:
    """Strip operator/system leaks from user-visible text; salvage fake tool calls.

    Returns ``(clean_text, salvaged)`` where salvaged items look like
    ``{"tool": "collection_probe_note", "note": "..."}``.
    """
    if not text:
        return "", []
    salvaged: list[dict[str, str]] = []
    cleaned = str(text)

    def _consume(match: re.Match[str]) -> str:
        tool = str(match.group(1) or "").strip()
        body = str(match.group("body") or "").strip()
        if tool and body:
            salvaged.append({"tool": tool, "note": body})
        return "\n"

    cleaned = _TEXTUAL_TOOL_CALL_RE.sub(_consume, cleaned)
    cleaned = _TEXTUAL_TOOL_CALL_LOOSE_RE.sub("\n", cleaned)
    cleaned = _SYSTEM_TIP_RE.sub("", cleaned)
    cleaned = _WRITE_STATUS_LINE_RE.sub("", cleaned)
    # Collapse excessive blank lines left by stripping.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, salvaged


_OR_CHOICE_RE = re.compile(r"(还是|或者)")
_ASK_NORMALIZE_RE = re.compile(r"[\s？?\s，,。.!！、；;：:（）()【】\[\]\"'“”‘’]+")


def _normalize_ask_text(text: str) -> str:
    return _ASK_NORMALIZE_RE.sub("", str(text or "")).strip().lower()


def _ask_stem(text: str) -> str:
    """Core ask stem before A-or-B tails (还是/或者) for near-repeat matching."""
    raw = str(text or "").strip()
    for sep in ("还是", "或者"):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
    return _normalize_ask_text(raw)


def patient_ask_quality_issues(text: str | None) -> list[str]:
    """Detect multi-question / A-or-B patterns in user-visible reply."""
    raw = str(text or "").strip()
    if not raw:
        return []
    issues: list[str] = []
    qmarks = raw.count("？") + raw.count("?")
    if qmarks > 1:
        issues.append("multi_qmark")
    # 「A还是B？」is compound even with one qmark. Plain 「恶心、呕吐或者耳鸣」lists OK.
    if qmarks >= 1 and _OR_CHOICE_RE.search(raw):
        issues.append("or_choice")
    return issues


def apply_ask_quality_tracking(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    reply_text: str | None,
) -> dict[str, Any]:
    """Record last ask; set next-turn nudge on quality misses / near-repeat."""
    out = ensure_collection_state(state, config)
    cleaned = str(reply_text or "").strip()
    issues = patient_ask_quality_issues(cleaned)
    prev = str(out.get("last_ask_text") or "").strip()
    prev_norm = _normalize_ask_text(prev)
    cur_norm = _normalize_ask_text(cleaned)
    prev_stem = _ask_stem(prev)
    cur_stem = _ask_stem(cleaned)
    if (
        prev_norm
        and cur_norm
        and len(cur_norm) >= 8
        and (
            cur_norm == prev_norm
            or cur_norm in prev_norm
            or prev_norm in cur_norm
            or (
                len(cur_stem) >= 6
                and len(prev_stem) >= 6
                and (cur_stem == prev_stem or cur_stem in prev_stem or prev_stem in cur_stem)
            )
        )
    ):
        issues.append("repeat_ask")
    out["ask_quality_nudge_due"] = bool(issues)
    if cleaned:
        out["last_ask_text"] = cleaned
    return out


def apply_probe_salvage_and_nudge(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    tools_ok: set[str],
    salvaged: list[dict[str, str]],
) -> dict[str, Any]:
    """Apply text-salvaged probe notes; set/clear next-turn internal nudge."""
    out = ensure_collection_state(state, config)
    probing = str(out.get("phase") or "") == PHASE_PROBING or is_field_probe_active(out)
    if not probing:
        out["probe_nudge_due"] = False
        return out

    salvaged_ok = False
    for item in salvaged:
        if item.get("tool") == "collection_probe_note" and item.get("note"):
            try:
                out = append_probe_note(out, config, item["note"])
                salvaged_ok = True
            except ValueError:
                # FSM not ready — keep textual salvage out of patient reply.
                pass

    called = bool(tools_ok & {"collection_probe_note", "collection_probe_finish"})
    if called or salvaged_ok:
        out["probe_nudge_due"] = False
    else:
        out["probe_nudge_due"] = True
    return out

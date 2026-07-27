"""workflow.scripts delivery flags, prompt rules, and patient reply composition."""
from __future__ import annotations

import json
import re
from typing import Any

from agno_worker.tenant.collection.kinds.dialogue.config import (
    last_reply_required_action,
    resolve_required_actions,
    resolve_scripts_config,
)
from agno_worker.tenant.collection.kinds.dialogue.constants import (
    OPENING_POLICY_FIRST_TURN,
    OPENING_POLICY_OPTIONAL,
    PHASE_COLLECTING,
    PHASE_CONFIRMED,
    PHASE_DONE,
    PHASE_INIT,
    PHASE_PROBING,
    PHASE_READY,
)
from agno_worker.tenant.collection.kinds.dialogue.core import (
    ensure_collection_state,
    incomplete_required_actions,
    is_probe_finished,
    is_required_action_completed,
    pending_required_action_tools,
    sync_reply_actions_done,
)


def _append_dialogue_scripts_rules(
    lines: list[str],
    current: dict[str, Any],
    config: dict[str, Any] | None,
    rule_n: int,
) -> None:
    """Inject workflow.scripts opening/closing constraints into the protocol appendix."""
    scripts = resolve_scripts_config(config)
    if not scripts:
        return
    phase = str(current.get("phase") or "")
    opening = scripts.get("opening") or ""
    closing = scripts.get("closing") or ""
    policy = scripts.get("opening_policy") or OPENING_POLICY_FIRST_TURN

    lines.extend(["", "## dialogue_scripts"])
    if opening:
        lines.append(f"scripts.opening: {json.dumps(opening, ensure_ascii=False)}")
    guide = scripts.get("guide") or ""
    if guide:
        lines.append(f"scripts.guide: {json.dumps(guide, ensure_ascii=False)}")
    if closing:
        lines.append(f"scripts.closing: {json.dumps(closing, ensure_ascii=False)}")
    lines.append(f"opening_delivered: {bool(current.get('opening_delivered'))}")
    lines.append(f"closing_delivered: {bool(current.get('closing_delivered'))}")
    lines.append(f"opening_policy: {policy}")

    if guide:
        lines.append(
            f"{rule_n}. GUIDE (workflow.scripts): when asking the user, follow "
            "scripts.guide (tone/pace); still at most one atomic question per turn."
        )
        rule_n += 1

    if (
        opening
        and policy == OPENING_POLICY_FIRST_TURN
        and not current.get("opening_delivered")
        and phase in {PHASE_INIT, PHASE_COLLECTING}
    ):
        lines.append(
            f"{rule_n}. OPENING REQUIRED (workflow.scripts): user-visible reply MUST "
            "begin with scripts.opening (light paraphrase OK; keep identity/welcome). "
            "Even if the user already stated the primary concern, do not skip the opening. "
            "After the opening, ask at most ONE next missing question "
            "(do not re-ask facts already given). "
            "FORBIDDEN: first reply that is only a follow-up question with no opening."
        )
        rule_n += 1
    elif (
        opening
        and policy == OPENING_POLICY_OPTIONAL
        and not current.get("opening_delivered")
        and phase in {PHASE_INIT, PHASE_COLLECTING}
    ):
        lines.append(
            f"{rule_n}. OPENING OPTIONAL: prefer scripts.opening on the first user-visible "
            "reply when natural; still at most one question after it."
        )
        rule_n += 1

    pipeline_ready = (
        not (current.get("missing") or [])
        and is_probe_finished(current, config)
        and phase in {PHASE_READY, PHASE_CONFIRMED, PHASE_DONE}
    )
    if pipeline_ready and incomplete_required_actions(current, config):
        lines.append(
            f"{rule_n}. REQUIRED ACTIONS (silent chain): execute pending steps with "
            "tools / collection_update_fields ONLY — FORBIDDEN patient-facing text "
            "during the chain. Protocol renders patient-visible text after EACH "
            "completed user_visible type=reply (cumulative, in definition order); "
            "scripts.closing is appended only when the LAST type=reply step completes. "
            "user_visible=false steps (e.g. mcp) stay silent."
        )
    elif (
        closing
        and current.get("closing_delivered")
        and pending_required_action_tools(current, config)
        and phase != PHASE_DONE
    ):
        lines.append(
            f"{rule_n}. CLOSING ALREADY DELIVERED: patient reply was already rendered. "
            "Call pending write tools only — FORBIDDEN to restate closing/recommendation."
        )


def apply_scripts_progress_from_run(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    had_patient_reply: bool,
) -> dict[str, Any]:
    """Advance opening/closing delivery flags after a turn (cluster-safe)."""
    out = ensure_collection_state(state, config)
    scripts = resolve_scripts_config(config)
    if not scripts:
        return out
    if (
        scripts.get("opening")
        and had_patient_reply
        and not out.get("opening_delivered")
        and str(out.get("phase") or "") in {PHASE_INIT, PHASE_COLLECTING, PHASE_PROBING}
    ):
        out["opening_delivered"] = True
    if scripts.get("closing") and not out.get("closing_delivered"):
        phase = str(out.get("phase") or "")
        if out.get("completed") or phase in {PHASE_CONFIRMED, PHASE_DONE}:
            out["closing_delivered"] = True
        elif (
            had_patient_reply
            and not (out.get("missing") or [])
            and is_probe_finished(out, config)
            and phase in {PHASE_READY, PHASE_CONFIRMED}
        ):
            last_reply = last_reply_required_action(config)
            if last_reply and is_required_action_completed(out, last_reply):
                out["closing_delivered"] = True
    return out


def _field_value_filled(collected: dict[str, Any], name: str) -> bool:
    value = collected.get(name)
    if value is None:
        return False
    return bool(str(value).strip())


def _clean_slot_text(value: str) -> str:
    return re.sub(r"\*\*", "", str(value or "").strip())


def _format_reply_field_for_patient(
    action: dict[str, Any],
    value: str,
    collected: dict[str, Any],
) -> str:
    """Map one user_visible reply slot to patient-facing text."""
    field = str(action.get("field") or "").strip()
    clean = _clean_slot_text(value)
    if field == "推荐科室":
        reason = ""
        for key in ("分科理由", "推荐理由", "reason"):
            candidate = str(collected.get(key) or "").strip()
            if candidate:
                reason = _clean_slot_text(candidate)
                break
        reason_field = str(action.get("reason_field") or "").strip()
        if reason_field:
            reason = _clean_slot_text(str(collected.get(reason_field) or "")) or reason
        dept_line = f"建议您挂：{clean}"
        return f"{reason}\n\n{dept_line}".strip() if reason else dept_line
    if field == "问诊摘要":
        # Always label so patients don't mistake the block for scripts.closing only.
        if clean.startswith("问诊摘要") or clean.startswith("根据您"):
            return clean
        return f"根据您刚才的描述，问诊小结如下：\n\n{clean}"
    return clean


def _iter_user_visible_reply_actions(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for action in resolve_required_actions(config):
        if str(action.get("type") or "") != "reply":
            continue
        if action.get("user_visible", True):
            out.append(action)
    return out


def compose_patient_reply_progress(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> str | None:
    """Cumulative patient text for completed user_visible reply steps (serial order).

    - After each ``type=reply`` with ``user_visible=true`` completes, include that
      step's rendered substance (stop at the first incomplete reply).
    - Append ``scripts.closing`` only when the **last** ``type=reply`` in the
      chain is complete (even if later mcp steps are still pending).
    """
    if not config:
        return None
    current = sync_reply_actions_done(ensure_collection_state(state, config), config)
    collected = current.get("collected") if isinstance(current.get("collected"), dict) else {}
    scripts = resolve_scripts_config(config) or {}
    closing = str(scripts.get("closing") or "").strip()
    last_reply = last_reply_required_action(config)
    parts: list[str] = []
    for action in _iter_user_visible_reply_actions(config):
        field = str(action.get("field") or "").strip()
        if not field:
            continue
        if not is_required_action_completed(current, action):
            break
        value = str(collected.get(field) or "").strip()
        if not value:
            break
        parts.append(_format_reply_field_for_patient(action, value, collected))
    if not parts:
        return None
    if (
        closing
        and last_reply
        and last_reply.get("user_visible", True)
        and is_required_action_completed(current, last_reply)
    ):
        body = "\n\n".join(parts)
        if closing not in body:
            parts.append(closing)
    return "\n\n".join(p for p in parts if p and str(p).strip()).strip() or None


def compose_or_keep_patient_reply(
    model_text: str | None,
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> str | None:
    """Prefer protocol-composed progress; never keep premature scripts.closing alone."""
    composed = compose_patient_reply_progress(state, config)
    if composed:
        return composed
    text = strip_premature_scripts_closing(str(model_text or "").strip(), state, config)
    return text or None


def strip_premature_scripts_closing(
    text: str,
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> str:
    """Remove scripts.closing from model speech until the last type=reply is done.

    Models often dump ``scripts.closing`` before writing 问诊摘要 / 推荐科室; that
    must not reach the patient as the sole bubble.
    """
    raw = str(text or "").strip()
    if not raw or not config:
        return raw
    scripts = resolve_scripts_config(config) or {}
    closing = str(scripts.get("closing") or "").strip()
    if not closing:
        return raw
    current = sync_reply_actions_done(ensure_collection_state(state, config), config)
    last_reply = last_reply_required_action(config)
    if last_reply and is_required_action_completed(current, last_reply):
        return raw
    # Reply chain not done — strip closing (and closing-only bubbles).
    if raw == closing or raw.replace("\r\n", "\n") == closing.replace("\r\n", "\n"):
        return ""
    if closing in raw:
        cleaned = raw.replace(closing, "").strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned
    return raw


def collection_state_from_session(session_state: Any) -> dict[str, Any] | None:
    """Extract collection sub-state from an agno session_state blob."""
    if not isinstance(session_state, dict):
        return None
    coll = session_state.get("collection")
    return coll if isinstance(coll, dict) else None


def session_state_from_run_event(event: Any) -> dict[str, Any] | None:
    """Best-effort session_state extraction from a streaming run event."""
    if event is None:
        return None
    direct = getattr(event, "session_state", None)
    if isinstance(direct, dict):
        return direct
    for attr in ("run_response", "run_output", "response"):
        nested = getattr(event, attr, None)
        if nested is None:
            continue
        ss = getattr(nested, "session_state", None)
        if isinstance(ss, dict):
            return ss
    return None

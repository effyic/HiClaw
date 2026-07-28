"""workflow.scripts delivery flags, prompt rules, and patient reply composition."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
    if (
        had_patient_reply
        and reply_action_complete(out, config)
        and not out.get("result_reply_delivered")
    ):
        out["result_reply_delivered"] = True
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
    """Map one user_visible reply slot to patient-facing text via protocol config.

    Uses ``patient_template`` with ``{value}`` / ``{reason}`` placeholders when set;
    otherwise returns the slot value (optionally prefixed by ``reason_field``).
    """
    clean = _clean_slot_text(value)
    reason = ""
    reason_field = str(action.get("reason_field") or "").strip()
    if reason_field:
        reason = _clean_slot_text(str(collected.get(reason_field) or ""))
    template = str(action.get("patient_template") or "").strip()
    if template:
        rendered = (
            template.replace("{value}", clean).replace("{reason}", reason).strip()
        )
        if not reason:
            rendered = re.sub(r"^\n+", "", rendered)
            rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
        return rendered
    if reason:
        return f"{reason}\n\n{clean}".strip()
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


# Patient SSE / post_hook speech modes (protocol-owned, scene-agnostic).
SPEECH_STREAM = "stream"
SPEECH_COMPOSE = "compose"
SPEECH_SILENT = "silent"


@dataclass(frozen=True)
class PatientTurnSpeech:
    """Authoritative patient-visible text decision for one turn.

    - ``stream``: model Q&A (collecting / probing / post-close chitchat)
    - ``compose``: protocol renders ``type=reply`` + optional ``scripts.closing``
    - ``silent``: no patient bubble (write-MCP follow-up or reply chain waiting)
    """

    mode: str
    text: str = ""


def resolve_patient_turn_speech(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    model_text: str | None = None,
    reply_delivered_at_start: bool = False,
) -> PatientTurnSpeech:
    """Decide patient-visible speech from collection protocol state only."""
    current = sync_reply_actions_done(ensure_collection_state(state, config), config)
    composed = compose_patient_reply_progress(current, config) or ""

    # Write-MCP-only follow-up *after a prior turn* already showed the reply.
    # Do not use result_reply_delivered alone — post_hook may flip it this turn
    # before stream finalization, which would wrongly silence first delivery.
    if pending_mcp_only(current, config) and reply_delivered_at_start:
        return PatientTurnSpeech(SPEECH_SILENT, "")

    # Waiting for reply slot fill: tools only; protocol will compose after.
    if silent_reply_chain_active(current, config) and not composed:
        return PatientTurnSpeech(SPEECH_SILENT, "")

    # Protocol compose owns the bubble on first delivery. After a prior turn
    # already delivered, fall through so post-close chitchat can stream.
    if composed and not reply_delivered_at_start:
        return PatientTurnSpeech(SPEECH_COMPOSE, composed)

    cleaned = strip_premature_result_speech(
        str(model_text or "").strip(), current, config
    )
    if cleaned:
        from agno_worker.runtime.structured_output import collapse_duplicate_paragraphs

        cleaned = collapse_duplicate_paragraphs(cleaned)
    return PatientTurnSpeech(SPEECH_STREAM, cleaned or "")


def patient_sse_replace_content(
    *,
    speech: PatientTurnSpeech,
    streamed_visible: str = "",
) -> str | None:
    """Content for a patient ``RunContent`` with ``replace=true``, or None to skip.

    Contract (no historical empty-wipe):
    - ``silent`` → no event
    - empty text → no event (never clear the bubble with ``content: ""``)
    - identical to what SSE already showed → no event
    - otherwise → non-empty authoritative text
    """
    if speech.mode == SPEECH_SILENT:
        return None
    text = str(speech.text or "").strip()
    if not text:
        return None
    if text == str(streamed_visible or "").strip():
        return None
    return text


def should_suppress_patient_deltas(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> bool:
    """True when model token deltas must not reach patient SSE."""
    current = sync_reply_actions_done(ensure_collection_state(state, config), config)
    if silent_reply_chain_active(current, config):
        return True
    # Reply substance is protocol-owned while write MCP remains.
    if pending_mcp_only(current, config):
        return True
    return False


def compose_or_keep_patient_reply(
    model_text: str | None,
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> str | None:
    """Map protocol speech resolution onto post_hook assistant content."""
    speech = resolve_patient_turn_speech(
        state,
        config,
        model_text=model_text,
        reply_delivered_at_start=bool(state.get("result_reply_delivered")),
    )
    if speech.mode == SPEECH_SILENT:
        return ""
    return speech.text or None


def reply_action_complete(state: dict[str, Any], config: dict[str, Any] | None) -> bool:
    current = sync_reply_actions_done(ensure_collection_state(state, config), config)
    last_reply = last_reply_required_action(config)
    if not last_reply:
        return True
    return is_required_action_completed(current, last_reply)


def pending_mcp_only(state: dict[str, Any], config: dict[str, Any] | None) -> bool:
    """True when schema slots are done, reply rendered, only write MCP remains."""
    current = sync_reply_actions_done(ensure_collection_state(state, config), config)
    if current.get("missing"):
        return False
    if not reply_action_complete(current, config):
        return False
    return bool(pending_required_action_tools(current, config))


def silent_reply_chain_active(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> bool:
    """True when required_actions pipeline is ready but a type=reply step is pending.

    Protocol: patient-facing text is forbidden during the silent chain — only
    tools / ``collection_update_fields``; compose renders after reply completes.
    """
    from agno_worker.tenant.collection.kinds.dialogue.core import (
        incomplete_required_actions,
        required_action_pipeline_ready,
    )

    current = sync_reply_actions_done(ensure_collection_state(state, config), config)
    if not required_action_pipeline_ready(current, config):
        return False
    for action in incomplete_required_actions(current, config):
        if str(action.get("type") or "") == "reply":
            return True
    return False


def _template_literals(template: str) -> list[str]:
    """Static fragments from ``patient_template`` (outside ``{value}`` / ``{reason}``)."""
    parts = re.split(r"\{(?:value|reason)\}", str(template or ""))
    out: list[str] = []
    for part in parts:
        lit = str(part or "").strip()
        if len(lit) >= 4:
            out.append(lit)
    return out


def _incomplete_reply_actions(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    current = sync_reply_actions_done(ensure_collection_state(state, config), config)
    out: list[dict[str, Any]] = []
    for action in resolve_required_actions(config):
        if str(action.get("type") or "") != "reply":
            continue
        if is_required_action_completed(current, action):
            continue
        out.append(action)
    return out


def _protocol_fragment_match(chunk: str, fragment: str, *, min_len: int = 4) -> bool:
    """True when chunk is the fragment, contains it, or is a streaming prefix of it."""
    c = str(chunk or "").strip()
    f = str(fragment or "").strip()
    if not c or not f:
        return False
    if c == f or f in c:
        return True
    if len(c) < min_len:
        return False
    # Token deltas of scripts.closing / patient_template ("感谢您的" …).
    if f.startswith(c) or c in f:
        return True
    return False


def looks_like_result_speech_chunk(
    text: str,
    state: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> bool:
    """Heuristic from protocol config only (closing / patterns / templates)."""
    raw = str(text or "").strip()
    if not raw:
        return False
    if not config:
        return False
    if silent_reply_chain_active(state or {}, config):
        return True
    # Until reply compose is done, suppress closing / template fragments — including
    # progressive SSE token prefixes (full-string match alone lets closing leak).
    if not reply_action_complete(state or {}, config):
        scripts = resolve_scripts_config(config) or {}
        closing = str(scripts.get("closing") or "").strip()
        if closing and _protocol_fragment_match(raw, closing):
            return True
        for action in _incomplete_reply_actions(state or {}, config):
            pattern = str(action.get("pattern") or "").strip()
            if pattern:
                try:
                    if re.search(pattern, raw):
                        return True
                except re.error:
                    pass
            for lit in _template_literals(str(action.get("patient_template") or "")):
                if _protocol_fragment_match(raw, lit):
                    return True
        return False
    scripts = resolve_scripts_config(config) or {}
    closing = str(scripts.get("closing") or "").strip()
    if closing and (raw == closing or closing in raw):
        return True
    return False


def strip_premature_scripts_closing(
    text: str,
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> str:
    """Backward-compatible alias for :func:`strip_premature_result_speech`."""
    return strip_premature_result_speech(text, state, config)


def strip_premature_result_speech(
    text: str,
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> str:
    """Remove premature result speech using protocol hooks only.

    - ``scripts.closing`` until last type=reply completes
    - Silent chain (pipeline ready + pending reply): drop all patient text
    - Lines matching incomplete reply ``pattern``
    - Paragraphs containing incomplete reply ``patient_template`` literals
    - Write-MCP-only follow-ups after reply delivered: empty
    """
    raw = str(text or "").strip()
    if not raw or not config:
        return raw
    scripts = resolve_scripts_config(config) or {}
    closing = str(scripts.get("closing") or "").strip()
    reply_done = reply_action_complete(state, config)
    mcp_only = pending_mcp_only(state, config)

    if reply_done and not mcp_only:
        return raw
    # MCP-only follow-up: silence *model* text. Protocol compose is never
    # passed through this function on the SSE path (engine uses speech modes).
    if mcp_only:
        return ""
    if silent_reply_chain_active(state, config):
        return ""

    if closing:
        if raw == closing or raw.replace("\r\n", "\n") == closing.replace("\r\n", "\n"):
            return ""
        if closing in raw:
            raw = raw.replace(closing, "").strip()
        elif _protocol_fragment_match(raw, closing):
            # Entire buffer is a progressive closing dump — drop it.
            return ""

    incomplete = _incomplete_reply_actions(state, config)
    pattern_res: list[re.Pattern[str]] = []
    literals: list[str] = []
    for action in incomplete:
        pattern = str(action.get("pattern") or "").strip()
        if pattern:
            try:
                pattern_res.append(re.compile(pattern))
            except re.error:
                pass
        literals.extend(_template_literals(str(action.get("patient_template") or "")))

    paras = [p.strip() for p in re.split(r"\n\s*\n", raw) if p and str(p).strip()]
    kept: list[str] = []
    for para in paras:
        if any(lit in para for lit in literals):
            continue
        cleaned_lines: list[str] = []
        for line in para.splitlines():
            if any(rx.search(line) for rx in pattern_res):
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines).strip()
        if cleaned:
            kept.append(cleaned)
    cleaned = "\n\n".join(kept).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


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

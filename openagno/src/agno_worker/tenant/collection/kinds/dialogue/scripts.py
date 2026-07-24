"""workflow.scripts delivery flags and prompt rules."""
from __future__ import annotations

import json
from typing import Any

from agno_worker.tenant.collection.kinds.dialogue.config import resolve_scripts_config
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
    is_probe_finished,
    pending_required_action_tools,
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
            f"{rule_n}. GUIDE (workflow.scripts): when asking patients, follow "
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
            f"{rule_n}. OPENING REQUIRED (workflow.scripts): patient-visible reply MUST "
            "begin with scripts.opening (light paraphrase OK; keep identity/welcome). "
            "Even if the user already stated a chief complaint, do not skip the opening. "
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
            f"{rule_n}. OPENING OPTIONAL: prefer scripts.opening on the first patient-visible "
            "reply when natural; still at most one question after it."
        )
        rule_n += 1

    ready_to_close = (
        bool(closing)
        and not current.get("closing_delivered")
        and not (current.get("missing") or [])
        and is_probe_finished(current, config)
        and phase in {PHASE_READY, PHASE_CONFIRMED}
    )
    if ready_to_close:
        lines.append(
            f"{rule_n}. CLOSING REQUIRED (workflow.scripts): for this patient-visible "
            "closing turn, base the reply on scripts.closing (light paraphrase OK). "
            "Emit the full closing at most ONCE in the session; then call pending "
            "write/required_actions tools. Do not invent a second closing block after tools. "
            "Do NOT name specific disease diagnoses in the patient-visible closing; "
            "use cautious direction/mechanism wording only."
        )
    elif (
        closing
        and not current.get("closing_delivered")
        and not (current.get("missing") or [])
        and is_probe_finished(current, config)
        and pending_required_action_tools(current, config)
        and phase != PHASE_DONE
    ):
        lines.append(
            f"{rule_n}. CLOSING REQUIRED (workflow.scripts): missing is empty and write "
            "tools are pending — patient-visible closing MUST follow scripts.closing "
            "once (with recommendation/summary as configured), then call pending tools. "
            "Do NOT name specific disease diagnoses in the patient-visible closing."
        )
    elif (
        closing
        and current.get("closing_delivered")
        and pending_required_action_tools(current, config)
        and phase != PHASE_DONE
    ):
        lines.append(
            f"{rule_n}. CLOSING ALREADY DELIVERED: scripts.closing / recommendation "
            "was already shown. Call pending write tools only; patient-visible reply "
            "must be ONE short status line — FORBIDDEN to restate closing/recommendation."
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
            out["closing_delivered"] = True
    return out


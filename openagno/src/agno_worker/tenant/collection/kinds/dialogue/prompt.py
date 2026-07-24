"""Per-turn collection_protocol instructions appendix."""
from __future__ import annotations

import json
from typing import Any

from agno_worker.tenant.collection.kinds.dialogue.config import (
    REQUIRED_ACTIONS_MODE_CONCURRENT,
    ask_batch_size,
    confirm_required,
    required_actions_mode,
    resolve_probe_config,
    suggested_write_tool,
)
from agno_worker.tenant.collection.kinds.dialogue.constants import (
    PHASE_CONFIRMED,
    PHASE_DONE,
    PHASE_PROBING,
)
from agno_worker.tenant.collection.kinds.dialogue.core import (
    current_required_action,
    ensure_collection_state,
    hard_gated_tool_names,
    is_field_probe_active,
    pending_required_action_tools,
    pending_required_actions,
    pre_probe_field_names,
)
from agno_worker.tenant.collection.kinds.dialogue.schema import schema_source
from agno_worker.tenant.collection.kinds.dialogue.scripts import (
    _append_dialogue_scripts_rules,
)

def collection_instructions_appendix(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> str:
    current = ensure_collection_state(state, config)
    batch = ask_batch_size(config)
    missing = current.get("missing") or []
    focus = missing[:batch]
    source = schema_source(config)
    write_tool = suggested_write_tool(config)
    probe = resolve_probe_config(config)
    gated = sorted(hard_gated_tool_names(current, config))
    active_action = current_required_action(current, config)
    pending_actions = pending_required_actions(current, config)
    actions_mode = required_actions_mode(config)
    if write_tool and write_tool in gated:
        # Do not prompt the model to call a tool that is hard-hidden.
        write_tool = None
    lines = [
        "## collection_protocol",
        f"phase: {current.get('phase')}",
        f"schema_source: {source}",
        f"ask_batch_size: {batch}",
        f"confirm_required: {confirm_required(config)}",
        f"user_confirmed: {current.get('user_confirmed')}",
        f"completed_once: {current.get('completed')}",
        f"actions_done: {json.dumps(sorted((current.get('actions_done') or {}).keys()), ensure_ascii=False)}",
        f"required_actions_mode: {actions_mode}",
        f"required_actions_pending: {json.dumps(pending_actions, ensure_ascii=False)}",
        f"required_action_current: {json.dumps(active_action, ensure_ascii=False)}",
        f"write_tools_hard_gated: {json.dumps(gated, ensure_ascii=False)}",
        f"missing: {json.dumps(missing, ensure_ascii=False)}",
        f"collected: {json.dumps(current.get('collected') or {}, ensure_ascii=False)}",
        f"field_probe_active: {json.dumps(current.get('field_probe_active') or '', ensure_ascii=False)}",
        f"field_probes: {json.dumps(current.get('field_probes') or {}, ensure_ascii=False)}",
        f"pre_probe_fields: {json.dumps(pre_probe_field_names(current.get('schema') or [], config), ensure_ascii=False)}",
    ]
    if probe:
        lines.extend(
            [
                f"probe_enabled: true",
                f"probe_rounds: {current.get('probe_rounds') or 0}/{probe.get('max_rounds')}",
                f"probe_min_rounds: {probe.get('min_rounds') or 0}",
                f"probe_done: {bool(current.get('probe_done'))}",
                f"probe_notes: {json.dumps(current.get('probe_notes') or [], ensure_ascii=False)}",
                f"probe_goal: {json.dumps(probe.get('goal') or '', ensure_ascii=False)}",
                f"probe_hints: {json.dumps(probe.get('hints') or [], ensure_ascii=False)}",
                f"probe_allow_skip: {bool(probe.get('allow_skip'))}",
            ]
        )
    lines.extend(["", "Rules:"])
    if source == "mcp" or not current.get("schema"):
        lines.append(
            "1. If schema is empty, call collection_load_schema "
            "(MCP schema: fetch fields via MCP then pass fields_json)."
        )
    else:
        lines.append(
            "1. Inline schema is already loaded; do not reload unless fields changed."
        )
    lines.extend(
        [
            f"2. Each turn ask at most {batch} items from missing; then call "
            "collection_update_fields with ONLY schema field names.",
            "3. While missing is non-empty OR write_tools_hard_gated is non-empty, "
            "do not attempt write/required_actions MCP — those tools are removed "
            "from the available tool list until conditions are met.",
            "4. Do not invent completion; call collection_status to inspect progress.",
            "4b. HIGH-QUALITY QUESTIONING (collecting + probing): "
            "patient-visible reply may contain at most ONE question (one '?' / '？'). "
            "Do not bundle two topics into one turn. "
            "FORBIDDEN even with a single '?': A-or-B choice forms "
            "(Chinese '还是' / '或者' between two symptom options) — ask one yes/no side only. "
            "Pick the single next ask with maximal information gain for the goal; "
            "ground it in the user's last answer + collected (do not ignore what they "
            "just said). "
            "NO-REPEAT (hard): never re-ask a fact already stated in this session "
            "(including details given in the user's first message); never repeat the "
            "same or near-identical question after the user already answered — "
            "paraphrase counts as repeat; advance to a NEW information gap. "
            "If the prior answer was vague, one short clarification only — do not "
            "restart the same topic. "
            "Prefer short colloquial phrasing over checklist / form language. "
            "Optional brief empathy (<=1 short clause) then the question — no preamble lists. "
            "Patient-visible text must NEVER include tool names, function-call syntax, "
            "or bracket tags like [系统提示].",
        ]
    )
    if current.get("ask_quality_nudge_due"):
        lines.append(
            "4c. PREVIOUS TURN ask-quality miss (multi-question, A-or-B, or repeated ask). "
            "This turn: exactly ONE atomic question; no '还是/或者' choice; "
            "explicitly build on the patient's latest answer; FORBIDDEN to repeat "
            "or paraphrase the prior question — ask a different next gap."
        )
    active_field = str(current.get("field_probe_active") or "").strip()
    if active_field and is_field_probe_active(current):
        field_cfg = None
        for item in current.get("schema") or []:
            if isinstance(item, dict) and str(item.get("name") or "").strip() == active_field:
                field_cfg = item
                break
        fp = (field_cfg or {}).get("probe") if isinstance(field_cfg, dict) else None
        fp = fp if isinstance(fp, dict) else {}
        entry = (current.get("field_probes") or {}).get(active_field) or {}
        goal = str(fp.get("goal") or "").strip()
        goal_clause = f" Follow field probe_goal: {goal}." if goal else ""
        lines.extend(
            [
                f"5. FIELD PROBE (collecting, field={active_field}): slot value is collected; "
                f"enrich this field only (rounds "
                f"{int(entry.get('rounds') or 0)}/{int(fp.get('max_rounds') or 0)}, "
                f"min={int(fp.get('min_rounds') or 0)})."
                + goal_clause
                + " Ask exactly 1 atomic clarifying question about this field. "
                "After the user answers, call collection_probe_note(note=..., field="
                f"{active_field!r}) via the TOOL INTERFACE in the SAME turn. "
                "After min_rounds, you MAY call collection_probe_finish(field=...) if "
                "enough detail is present; otherwise continue until max_rounds. "
                "Do not fill other missing slots or run global probe until this field probe ends. "
                "FORBIDDEN: printing tool call syntax to the patient.",
            ]
        )
        if current.get("probe_nudge_due"):
            lines.append(
                "5b. PREVIOUS TURN missed collection_probe_note for the active field. "
                "This turn: first call collection_probe_note for that field, then ask next "
                "or finish if min_rounds met."
            )
        rule_base = 6
        write_tool = None
    elif probe and current.get("phase") == PHASE_PROBING:
        goal = str(probe.get("goal") or "").strip()
        goal_clause = (
            f"Follow probe_goal: {goal}. "
            if goal
            else "Follow agent instructions for enrichment purpose. "
        )
        lines.extend(
            [
                "5. GLOBAL PROBE LOOP (phase=probing): pre-probe slots (and their field "
                "probes) are done. "
                + goal_clause
                + "Choose the next question from dialogue + collected to close the "
                "largest remaining information gap for that goal — adaptive, not a "
                "fixed questionnaire. "
                "If probe_hints is non-empty, treat it as an optional dimension checklist: "
                "prefer unanswered dimensions; skip what is already clear; "
                "do not recite hints verbatim. "
                "If probe_hints is empty, rely on probe_goal + dialogue + collected. "
                "Ask exactly 1 atomic question per turn (see rule 4b). "
                "FORBIDDEN: A-or-B ('还是/或者') compound asks. "
                "After each user answer, the next ask MUST advance using that answer "
                "(do not ignore new information and repeat an unrelated prior ask). "
                "Prefer questions that best discriminate among remaining plausible "
                "paths for the goal, rather than generic completeness fishing. "
                "CRITICAL: after the user answers, you MUST call "
                "collection_probe_note via the TOOL INTERFACE in the SAME turn "
                "(note=concise enrichment note: why this ask + "
                "key positives / pertinent negatives) "
                "before ending — otherwise probe_rounds will not advance. "
                "FORBIDDEN: writing tool names or call syntax such as "
                "collection_probe_note(...) into the patient-visible reply. "
                "After probe_min_rounds notes, you MAY call collection_probe_finish "
                "when your own judgment says enough information is present for the goal; "
                "otherwise continue toward max_rounds. "
                "collection_probe_finish is blocked until probe_min_rounds. "
                "Write/required_actions MCP tools are HARD-REMOVED while probing "
                "(see write_tools_hard_gated); do not invent a write call. "
                "Also forbidden: filling remaining after_probe / decision slots that should "
                "wait until after probe. "
                "Prefer probe.goal / probe.hints and schema field guidance; "
                "do not invent off-config topics beyond those.",
            ]
        )
        if current.get("probe_nudge_due"):
            lines.append(
                "5b. PREVIOUS TURN missed collection_probe_note. This turn: "
                "first call collection_probe_note for the latest patient answer "
                "(tool interface only), then ask the next atomic clinical question. "
                "Do not print tool syntax to the patient."
            )
        rule_base = 6
        # Do not nudge write tools while still probing.
        write_tool = None
    else:
        lines.append(
            "5. When missing is empty"
            + (
                " and probe is finished,"
                if probe
                else ","
            )
            + " produce a brief structured summary for the operator"
            + (
                " and ask for confirmation (collection_confirm or wait for confirm)."
                if confirm_required(config)
                else "."
            )
            + (
                f" Then call write MCP ({write_tool}) if configured."
                if write_tool
                else " Then call any pending required_actions write tools."
            )
        )
        rule_base = 6
    # required_actions: serial (one step) or concurrent (same-turn bundle).
    if actions_mode == REQUIRED_ACTIONS_MODE_CONCURRENT and pending_actions:
        reply_fields = [
            str(a.get("field") or "").strip()
            for a in pending_actions
            if str(a.get("type") or "") == "reply" and str(a.get("field") or "").strip()
        ]
        mcp_tools = pending_required_action_tools(current, config)
        lines.append(
            f"{rule_base}. REQUIRED ACTIONS (concurrent, same turn): complete ALL "
            "pending required_actions in this turn when possible. "
            + (
                "Fill reply field(s) via collection_update_fields "
                + json.dumps(reply_fields, ensure_ascii=False)
                + " and output the patient-facing recommendation/summary once; "
                if reply_fields
                else ""
            )
            + (
                "call MCP tool(s) "
                + json.dumps(mcp_tools, ensure_ascii=False)
                + " successfully in the same turn after the reply field(s) are written. "
                if mcp_tools
                else ""
            )
            + "Preferred order follows required_actions definition; do not leave "
            "pending steps for a later turn unless a tool fails."
        )
        rule_n = rule_base + 1
        if mcp_tools and not (current.get("missing") or []):
            lines.append(
                f"{rule_n}. Anti-repeat: after recommendation text is set, patient-visible "
                "closing must appear at most once this turn; do not emit a second full "
                "recommendation block after MCP tools."
            )
            rule_n += 1
        write_tool = None
    elif active_action and str(active_action.get("type") or "") == "reply":
        field = str(active_action.get("field") or "").strip()
        lines.append(
            f"{rule_base}. REQUIRED ACTION (serial, type=reply): current step is "
            f"patient-facing reply for schema field {field!r}. "
            "Fill that field via collection_update_fields (after any needed lookup tools), "
            "and output the recommendation/summary to the patient in this turn. "
            "Later MCP required_actions stay hard-gated until this reply step completes."
        )
        rule_n = rule_base + 1
        write_tool = None
    elif write_tool or pending_required_action_tools(current, config):
        lines.append(
            f"{rule_base}. After a successful write/update MCP call, "
            "collection_mark_done(result=...). User may later add details — "
            "update fields / probe notes and write again."
        )
        rule_n = rule_base + 1
    else:
        rule_n = rule_base
    pending = pending_required_action_tools(current, config)
    if actions_mode != REQUIRED_ACTIONS_MODE_CONCURRENT and pending:
        lines.append(
            f"{rule_n}. REQUIRED ACTION (serial, type=mcp): call these tools "
            "successfully first (do not only reply with text): "
            + json.dumps(pending, ensure_ascii=False)
            + ". Earlier reply steps must already be done; collection_mark_done is "
            "blocked until the full required_actions chain succeeds."
        )
        rule_n += 1
    # Cross-turn anti-repeat (serial + concurrent): once decision slots are filled
    # and write tools remain pending, do not re-emit the recommendation/closing.
    if pending and not (current.get("missing") or []):
        lines.append(
            f"{rule_n}. Anti-repeat: missing is empty and write tools are still "
            "pending ("
            + json.dumps(pending, ensure_ascii=False)
            + "). Call the pending tools first. Patient-visible reply must be "
            "ONE short status line only — do NOT restate the previous recommendation, "
            "closing tips, or summary verbatim."
        )
        rule_n += 1
    elif (
        str(current.get("phase") or "") in {PHASE_CONFIRMED, PHASE_DONE}
        and (current.get("completed") or not (current.get("missing") or []))
    ):
        lines.append(
            f"{rule_n}. Collection finished (completed or all slots filled): "
            "patient-visible reply MUST be brief (<=2 short sentences). "
            "FORBIDDEN: restate recommendation / decision reasons, closing tips, "
            "or the previous summary — even if the user asks again for the result. "
            "If they re-ask, answer with only the already collected decision value "
            "in one line, nothing else."
        )
        rule_n += 1
    elif focus:
        lines.append(f"{rule_n}. This turn focus fields: {json.dumps(focus, ensure_ascii=False)}")
        rule_n += 1
    if write_tool:
        label = "MUST call" if pending and write_tool in pending else "Suggested write/update MCP tool"
        lines.append(f"{rule_n}. {label}: {write_tool}")
        rule_n += 1
    patterned = [
        {
            "name": f.get("name"),
            "pattern": f.get("pattern"),
            "pattern_message": f.get("pattern_message") or "",
        }
        for f in (current.get("schema") or [])
        if isinstance(f, dict) and str(f.get("pattern") or "").strip()
    ]
    if patterned:
        lines.append(
            f"{rule_n}. Fields with pattern MUST match when calling collection_update_fields: "
            + json.dumps(patterned, ensure_ascii=False)
        )
        rule_n += 1
    _append_dialogue_scripts_rules(lines, current, config, rule_n)
    return "\n".join(lines)


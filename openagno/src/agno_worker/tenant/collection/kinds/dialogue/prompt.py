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
    FIELD_STAGE_COLLECT,
    FIELD_STAGE_PROBE,
    current_required_action,
    ensure_collection_state,
    field_stage,
    hard_gated_tool_names,
    later_collectable_fields,
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
    cursor = str(current.get("current_field") or "").strip()
    stage = field_stage(current)
    later = later_collectable_fields(current, config)
    # Ask focus is ONLY the cursor — never the full missing list.
    focus = [cursor] if cursor else []
    source = schema_source(config)
    write_tool = suggested_write_tool(config)
    probe = resolve_probe_config(config)
    gated = sorted(hard_gated_tool_names(current, config))
    active_action = current_required_action(current, config)
    pending_actions = pending_required_actions(current, config)
    actions_mode = required_actions_mode(config)
    if write_tool and write_tool in gated:
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
        f"current_field: {json.dumps(cursor, ensure_ascii=False)}",
        f"field_stage: {json.dumps(stage, ensure_ascii=False)}",
        f"later_fields: {json.dumps(later, ensure_ascii=False)}",
        f"pending_collected: {json.dumps(current.get('pending_collected') or {}, ensure_ascii=False)}",
        f"missing: {json.dumps(missing, ensure_ascii=False)}",
        f"collected: {json.dumps(current.get('collected') or {}, ensure_ascii=False)}",
        f"field_probes: {json.dumps(current.get('field_probes') or {}, ensure_ascii=False)}",
        f"pre_probe_fields: {json.dumps(pre_probe_field_names(current.get('schema') or [], config), ensure_ascii=False)}",
    ]
    if probe:
        # Enrichment probe min/max apply ONLY after required cursor is idle
        # (phase=probing). Do not present them as a global dialogue budget
        # while still on current_field collect/field-probe.
        enrichment_active = str(current.get("phase") or "") == PHASE_PROBING
        lines.append("enrichment_probe_enabled: true")
        lines.append(
            "enrichment_probe_scope: ONLY after all required pre-probe fields "
            "(+ field probes) are done; NOT a global min/max for the whole dialogue; "
            "NOT a substitute for schema.fields[].probe min/max"
        )
        if enrichment_active or not cursor:
            lines.extend(
                [
                    f"enrichment_probe_rounds: {current.get('probe_rounds') or 0}/{probe.get('max_rounds')}",
                    f"enrichment_probe_min_rounds: {probe.get('min_rounds') or 0}",
                    f"enrichment_probe_done: {bool(current.get('probe_done'))}",
                    f"enrichment_probe_notes: {json.dumps(current.get('probe_notes') or [], ensure_ascii=False)}",
                    f"enrichment_probe_goal: {json.dumps(probe.get('goal') or '', ensure_ascii=False)}",
                    f"enrichment_probe_hints: {json.dumps(probe.get('hints') or [], ensure_ascii=False)}",
                    f"enrichment_probe_allow_skip: {bool(probe.get('allow_skip'))}",
                ]
            )
        else:
            lines.append(
                "enrichment_probe_status: deferred_until_required_cursor_idle "
                f"(probe_done={bool(current.get('probe_done'))})"
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
            "2. HARD CURSOR: ask and write ONLY current_field. "
            "later_fields / other missing slots are FORBIDDEN to ask. "
            "If the user stated several facts at once, call collection_update_fields "
            "with all of them — protocol keeps current_field and stashes the rest "
            "into pending_collected (auto-applied when the cursor arrives). "
            "Do NOT re-ask facts already in collected or pending_collected.",
            "3. While current_field is non-empty OR write_tools_hard_gated is non-empty, "
            "do not attempt write/required_actions MCP — those tools are removed "
            "from the available tool list until conditions are met.",
            "4. Do not invent completion; call collection_status to inspect progress.",
            "4b. HIGH-QUALITY QUESTIONING (collecting + probing): "
            "user-visible reply may contain at most ONE question (one '?' / '？'). "
            "Do not bundle two topics into one turn. "
            "FORBIDDEN even with a single '?': A-or-B choice forms "
            "(Chinese '还是' / '或者' between two options) — ask one yes/no side only. "
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
            "User-visible text must NEVER include tool names, function-call syntax, "
            "or bracket tags like [系统提示].",
        ]
    )
    if current.get("ask_quality_nudge_due"):
        lines.append(
            "4c. PREVIOUS TURN ask-quality miss (multi-question, A-or-B, "
            "repeated ask, or re-ask of a fact already in collected). "
            "This turn: exactly ONE atomic question; no '还是/或者' choice; "
            "explicitly build on the user's latest answer; FORBIDDEN to repeat "
            "or paraphrase the prior question OR any fact already in collected "
            "(esp. position aggravation already described in 主诉) — ask a "
            "different next gap, or finish probe if enrichment_min is met."
        )
    if cursor and stage == FIELD_STAGE_PROBE:
        field_cfg = None
        for item in current.get("schema") or []:
            if isinstance(item, dict) and str(item.get("name") or "").strip() == cursor:
                field_cfg = item
                break
        fp = (field_cfg or {}).get("probe") if isinstance(field_cfg, dict) else None
        fp = fp if isinstance(fp, dict) else {}
        entry = (current.get("field_probes") or {}).get(cursor) or {}
        goal = str(fp.get("goal") or "").strip()
        goal_clause = f" Follow field probe_goal: {goal}." if goal else ""
        rounds_now = int(entry.get("rounds") or 0)
        min_now = int(fp.get("min_rounds") or 0)
        max_now = int(fp.get("max_rounds") or 0)
        must_more = rounds_now < min_now
        lines.extend(
            [
                f"5. FIELD CURSOR (stage=probe, field={cursor}): value is collected; "
                f"enrich THIS field only (rounds {rounds_now}/{max_now}, min={min_now})."
                + goal_clause
                + f" Patient-visible question MUST be about {cursor!r} only. "
                "FORBIDDEN to ask later_fields "
                + json.dumps(later, ensure_ascii=False)
                + ". "
                + (
                    f"rounds<{min_now}: MUST ask one clarifying question about "
                    f"{cursor!r} and call collection_probe_note "
                    f"(finish is blocked until min_rounds). "
                    if must_more
                    else (
                        f"rounds>={min_now}: DEFAULT is collection_probe_finish(field="
                        f"{cursor!r}) in this turn — especially when collected["
                        f"{cursor!r}] already has enough detail for the field goal. "
                        "Only ask ONE new clarifying question about THIS field if a "
                        "clear information gap remains that is NOT already stated in "
                        f"collected[{cursor!r}], pending_collected, or prior dialogue. "
                        "FORBIDDEN to re-ask / paraphrase facts already inside "
                        f"collected[{cursor!r}] (e.g. position aggravation already "
                        "described). Writing the next field also auto-finishes when "
                        "min_rounds is met. "
                    )
                )
                + "If you ask, ask exactly 1 atomic clarifying question about THIS field. "
                "After the user answers, call collection_probe_note(note=..., field="
                f"{cursor!r}) via the TOOL INTERFACE in the SAME turn. "
                "FORBIDDEN: printing tool call syntax to the user.",
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
    elif cursor and stage == FIELD_STAGE_COLLECT:
        lines.extend(
            [
                f"5. FIELD CURSOR (stage=collect, field={cursor}): ask ONE question to "
                f"obtain {cursor!r}, then collection_update_fields. "
                "FORBIDDEN to ask later_fields "
                + json.dumps(later, ensure_ascii=False)
                + ". If pending_collected already has "
                f"{cursor!r}, do not re-ask — the protocol will auto-apply it after "
                "the previous field probe ends.",
            ]
        )
        rule_base = 6
    elif probe and current.get("phase") == PHASE_PROBING:
        goal = str(probe.get("goal") or "").strip()
        goal_clause = (
            f"Follow enrichment_probe_goal: {goal}. "
            if goal
            else "Follow agent instructions for enrichment purpose. "
        )
        lines.extend(
            [
                "5. ENRICHMENT PROBE (phase=probing): required pre-probe cursor is idle. "
                "workflow.probe.min_rounds/max_rounds bound THIS enrichment loop only "
                "(collection_probe_note/finish without field=). "
                "They are NOT a global dialogue round budget and do NOT control "
                "fields[].probe or current_field collect. "
                + goal_clause
                + "Choose the next question from dialogue + collected to close the "
                "largest remaining information gap for that goal — adaptive, not a "
                "fixed questionnaire. "
                "If enrichment_probe_hints is non-empty, treat it as an optional "
                "dimension checklist: prefer unanswered dimensions; skip what is "
                "already clear; do not recite hints verbatim. "
                "If enrichment_probe_hints is empty, rely on enrichment_probe_goal + "
                "dialogue + collected. "
                "Ask exactly 1 atomic question per turn (see rule 4b). "
                "FORBIDDEN: A-or-B ('还是/或者') compound asks. "
                "NO-REPEAT vs collected (hard): before asking, scan collected "
                "values (especially chief complaint / 主诉*) and prior dialogue; "
                "if the fact is already stated (e.g. position/turning aggravation, "
                "duration, nausea), do NOT re-ask or paraphrase it — pick a "
                "different uncovered gap or call collection_probe_finish when "
                "enrichment_probe_min_rounds is met. "
                "After each user answer, the next ask MUST advance using that answer "
                "(do not ignore new information and repeat an unrelated prior ask). "
                "Prefer questions that best discriminate among remaining plausible "
                "paths for the goal, rather than generic completeness fishing. "
                "CRITICAL: after the user answers, you MUST call "
                "collection_probe_note via the TOOL INTERFACE in the SAME turn "
                "(note=concise enrichment note: why this ask + "
                "key positives / relevant negatives) "
                "before ending — otherwise enrichment rounds will not advance. "
                "FORBIDDEN: writing tool names or call syntax such as "
                "collection_probe_note(...) into the user-visible reply. "
                "After enrichment_probe_min_rounds notes, you MAY call "
                "collection_probe_finish when your own judgment says enough "
                "information is present for the goal; otherwise continue toward "
                "enrichment max_rounds. "
                "collection_probe_finish is blocked until enrichment_probe_min_rounds. "
                "Write/required_actions MCP tools are HARD-REMOVED while probing "
                "(see write_tools_hard_gated); do not invent a write call. "
                "Also forbidden: filling remaining after_probe / decision slots that "
                "should wait until after enrichment probe. "
                "Prefer enrichment_probe_goal / hints and schema field guidance; "
                "do not invent off-config topics beyond those.",
            ]
        )
        if current.get("probe_nudge_due"):
            lines.append(
                "5b. PREVIOUS TURN missed collection_probe_note. This turn: "
                "first call collection_probe_note for the latest user answer "
                "(tool interface only), then ask the next atomic question. "
                "Do not print tool syntax to the user."
            )
        rule_base = 6
        write_tool = None
    else:
        lines.append(
            "5. When missing is empty"
            + (
                " and enrichment probe is finished,"
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
                + " and output the user-facing recommendation/summary once; "
                "patient-visible text MUST include the substance of those reply "
                "field value(s) (brief OK) — do NOT only emit scripts.closing "
                "while hiding the summary/recommendation solely inside the field. "
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
                f"{rule_n}. Anti-repeat: after recommendation text is set, user-visible "
                "closing must appear at most once this turn; do not emit a second full "
                "recommendation block after MCP tools."
            )
            rule_n += 1
        write_tool = None
    elif active_action and str(active_action.get("type") or "") == "reply":
        field = str(active_action.get("field") or "").strip()
        lines.append(
            f"{rule_base}. REQUIRED ACTION (serial, type=reply): current step is "
            f"user-facing reply for schema field {field!r}. "
            "Fill that field via collection_update_fields (after any needed lookup tools), "
            "and output the recommendation/summary to the user in this turn. "
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
    if pending and not (current.get("missing") or []):
        lines.append(
            f"{rule_n}. Anti-repeat: missing is empty and write tools are still "
            "pending ("
            + json.dumps(pending, ensure_ascii=False)
            + "). Call the pending tools first. Patient-visible reply must be "
            "ONE short status line only — do NOT restate the previous recommendation, "
            "closing tips, or summary verbatim. "
            "FORBIDDEN: tell the patient that write/EMR/case creation failed, "
            "succeeded, or needs retry — never expose write-tool outcomes."
        )
        rule_n += 1
    elif (
        str(current.get("phase") or "") in {PHASE_CONFIRMED, PHASE_DONE}
        and (current.get("completed") or not (current.get("missing") or []))
    ):
        lines.append(
            f"{rule_n}. Collection finished (completed or all slots filled): "
            "user-visible reply MUST be brief (<=2 short sentences). "
            "FORBIDDEN: restate recommendation / decision reasons, closing tips, "
            "or the previous summary — even if the user asks again for the result. "
            "If they re-ask, answer with only the already collected decision value "
            "in one line, nothing else."
        )
        rule_n += 1
    elif focus:
        lines.append(
            f"{rule_n}. This turn ask_focus (ONLY): "
            + json.dumps(focus, ensure_ascii=False)
            + f" (field_stage={stage})."
        )
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

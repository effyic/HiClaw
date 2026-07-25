"""Agno Function tools for collection_dialogue."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

from agno_worker.tenant.collection.kinds.dialogue.config import suggested_write_tool
from agno_worker.tenant.collection.kinds.dialogue.core import (
    apply_required_actions_from_run,
    append_probe_note,
    confirm_collection,
    finish_probe,
    get_collection_state,
    load_schema_into_state,
    mark_collection_done,
    pending_required_action_tools,
    set_collection_state,
    store_collection_draft,
    update_collected_fields,
    workflow_from_run_context,
)
from agno_worker.tenant.collection.registry import resolve_collection_config

logger = logging.getLogger(__name__)

def _parse_fields_json(fields_json: str | None) -> list[dict[str, Any]] | dict[str, Any] | None:
    if fields_json is None or not str(fields_json).strip():
        return None
    raw = str(fields_json).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    return parsed


def build_collection_tools() -> list[Any]:
    """Agno Function tools for the collection protocol."""
    try:
        from agno.tools import tool
    except ImportError:
        logger.warning("agno.tools unavailable; collection tools disabled")
        return []

    @tool(
        name="collection_load_schema",
        description=(
            "Load/replace the collection field schema into session state. "
            "For workflow.schema.source=inline, call with no args. "
            "For source=mcp, pass fields_json from the schema MCP tool response."
        ),
    )
    def collection_load_schema(
        fields_json: str = "",
        run_context: Any = None,
    ) -> str:
        ctx = run_context
        if ctx is None:
            return json.dumps({"ok": False, "error": "run_context missing"}, ensure_ascii=False)
        config = resolve_collection_config(workflow_from_run_context(ctx))
        if not config:
            return json.dumps(
                {"ok": False, "error": "collection protocol not enabled for this agent"},
                ensure_ascii=False,
            )
        try:
            parsed = _parse_fields_json(fields_json) if fields_json else None
            fields: list[dict[str, Any]] | None
            if parsed is None:
                fields = None
            elif isinstance(parsed, list):
                fields = parsed
            elif isinstance(parsed, dict) and isinstance(parsed.get("fields"), list):
                fields = parsed["fields"]
            elif isinstance(parsed, dict):
                # single field object or name->desc map
                if "name" in parsed or "label" in parsed:
                    fields = [parsed]
                else:
                    fields = [
                        {"name": str(k), "description": str(v), "required": True}
                        for k, v in parsed.items()
                    ]
            else:
                raise ValueError("fields_json must be a list or object")
            state = load_schema_into_state(get_collection_state(ctx), fields, config)
            set_collection_state(ctx, state)
            return json.dumps(
                {
                    "ok": True,
                    "phase": state["phase"],
                    "schema": state["schema"],
                    "missing": state["missing"],
                    "collected": state["collected"],
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool(
        name="collection_update_fields",
        description=(
            "Merge extracted field values into collection state. "
            "Pass fields_json as object {fieldName: value}."
        ),
    )
    def collection_update_fields(
        fields_json: str,
        run_context: Any = None,
    ) -> str:
        ctx = run_context
        if ctx is None:
            return json.dumps({"ok": False, "error": "run_context missing"}, ensure_ascii=False)
        config = resolve_collection_config(workflow_from_run_context(ctx))
        if not config:
            return json.dumps(
                {"ok": False, "error": "collection protocol not enabled"},
                ensure_ascii=False,
            )
        try:
            parsed = _parse_fields_json(fields_json)
            if not isinstance(parsed, dict):
                raise ValueError("fields_json must be a JSON object")
            state = update_collected_fields(get_collection_state(ctx), parsed, config)
            stashed = list(state.pop("_cursor_stashed", []) or [])
            set_collection_state(ctx, state)
            cursor = state.get("current_field") or ""
            stage = state.get("field_stage") or ""
            payload: dict[str, Any] = {
                "ok": True,
                "phase": state["phase"],
                "missing": state["missing"],
                "collected": state["collected"],
                "current_field": cursor,
                "field_stage": stage,
                "pending_collected": state.get("pending_collected") or {},
                "field_probe_active": state.get("field_probe_active") or "",
                "ask_focus": [cursor] if cursor else [],
            }
            if stashed:
                payload["stashed_fields"] = stashed
                payload["hint"] = (
                    f"Cursor kept current_field={cursor!r} (stage={stage}). "
                    f"Ahead-of-cursor facts stashed in pending_collected: {stashed}. "
                    "Ask ONLY about current_field; do not re-ask stashed facts."
                )
            elif stage == "probe" and cursor:
                payload["hint"] = (
                    f"current_field={cursor!r} stage=probe: ask ONLY about this field "
                    "for min..max probe rounds; later_fields are blocked."
                )
            elif stage == "collect" and cursor:
                payload["hint"] = (
                    f"current_field={cursor!r} stage=collect: ask/write ONLY this field."
                )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool(
        name="collection_status",
        description="Return current collection phase, missing fields, and collected values.",
    )
    def collection_status(run_context: Any = None) -> str:
        ctx = run_context
        if ctx is None:
            return json.dumps({"ok": False, "error": "run_context missing"}, ensure_ascii=False)
        config = resolve_collection_config(workflow_from_run_context(ctx))
        state = get_collection_state(ctx)
        pending = pending_required_action_tools(state, config)
        cursor = state.get("current_field") or ""
        return json.dumps(
            {
                "ok": True,
                "enabled": config is not None,
                "phase": state["phase"],
                "missing": state["missing"],
                "collected": state["collected"],
                "current_field": cursor,
                "field_stage": state.get("field_stage") or "",
                "pending_collected": state.get("pending_collected") or {},
                "ask_focus": [cursor] if cursor else [],
                "schema": state["schema"],
                "user_confirmed": state["user_confirmed"],
                "completed": state["completed"],
                "actions_done": sorted((state.get("actions_done") or {}).keys()),
                "required_actions_pending": pending,
                "probe_rounds": state.get("probe_rounds") or 0,
                "probe_done": bool(state.get("probe_done")),
                "probe_notes": list(state.get("probe_notes") or []),
                "field_probe_active": state.get("field_probe_active") or "",
            },
            ensure_ascii=False,
        )

    @tool(
        name="collection_probe_note",
        description=(
            "Record one enrichment note after the user answers a probe question. "
            "For per-field probe (current_field stage=probe): pass field=<slot name>; "
            "rounds follow schema.fields[].probe min/max only. "
            "For post-required enrichment (phase=probing): omit field; rounds follow "
            "workflow.probe min/max only — not a global dialogue budget."
        ),
    )
    def collection_probe_note(
        note: str,
        field: str = "",
        run_context: Any = None,
    ) -> str:
        ctx = run_context
        if ctx is None:
            return json.dumps({"ok": False, "error": "run_context missing"}, ensure_ascii=False)
        config = resolve_collection_config(workflow_from_run_context(ctx))
        if not config:
            return json.dumps(
                {"ok": False, "error": "collection protocol not enabled"},
                ensure_ascii=False,
            )
        try:
            state = append_probe_note(
                get_collection_state(ctx),
                config,
                note,
                field=field or None,
            )
            set_collection_state(ctx, state)
            cursor = state.get("current_field") or ""
            return json.dumps(
                {
                    "ok": True,
                    "phase": state["phase"],
                    "current_field": cursor,
                    "field_stage": state.get("field_stage") or "",
                    "pending_collected": state.get("pending_collected") or {},
                    "field_probe_active": state.get("field_probe_active") or "",
                    "field_probes": state.get("field_probes") or {},
                    "probe_rounds": state.get("probe_rounds"),
                    "probe_done": bool(state.get("probe_done")),
                    "probe_notes": state.get("probe_notes") or [],
                    "required_actions_pending": pending_required_action_tools(state, config),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool(
        name="collection_probe_finish",
        description=(
            "End a field or enrichment probe early after its own min_rounds and before "
            "max_rounds, when the model judges enough information is present. "
            "Pass field=<slot name> for per-field probe (schema.fields[].probe). "
            "Omit field for post-required enrichment (workflow.probe) — those min/max "
            "do NOT control field probes or the whole dialogue. "
            "Blocked until that loop's min_rounds. Blocked when allow_skip=false until max. "
            "reason is optional free text for the note log (no keyword whitelist)."
        ),
    )
    def collection_probe_finish(
        reason: str = "",
        field: str = "",
        run_context: Any = None,
    ) -> str:
        ctx = run_context
        if ctx is None:
            return json.dumps({"ok": False, "error": "run_context missing"}, ensure_ascii=False)
        config = resolve_collection_config(workflow_from_run_context(ctx))
        if not config:
            return json.dumps(
                {"ok": False, "error": "collection protocol not enabled"},
                ensure_ascii=False,
            )
        try:
            state = finish_probe(
                get_collection_state(ctx),
                config,
                reason=reason or None,
                field=field or None,
            )
            set_collection_state(ctx, state)
            cursor = state.get("current_field") or ""
            return json.dumps(
                {
                    "ok": True,
                    "phase": state["phase"],
                    "current_field": cursor,
                    "field_stage": state.get("field_stage") or "",
                    "pending_collected": state.get("pending_collected") or {},
                    "field_probe_active": state.get("field_probe_active") or "",
                    "field_probes": state.get("field_probes") or {},
                    "probe_done": bool(state.get("probe_done")),
                    "probe_rounds": state.get("probe_rounds"),
                    "required_actions_pending": pending_required_action_tools(state, config),
                    "hint": (
                        "Probe finished; produce operator summary then call write MCP "
                        "if required_actions pending."
                        if not state.get("missing") and not cursor
                        else (
                            f"Continue cursor: ask/write ONLY current_field={cursor!r} "
                            f"(stage={state.get('field_stage') or ''})."
                            if cursor
                            else "Probe segment finished; continue global probe / actions."
                        )
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool(
        name="collection_confirm",
        description="Mark user confirmation after required fields are complete.",
    )
    def collection_confirm(run_context: Any = None) -> str:
        ctx = run_context
        if ctx is None:
            return json.dumps({"ok": False, "error": "run_context missing"}, ensure_ascii=False)
        config = resolve_collection_config(workflow_from_run_context(ctx))
        if not config:
            return json.dumps(
                {"ok": False, "error": "collection protocol not enabled"},
                ensure_ascii=False,
            )
        try:
            state = confirm_collection(get_collection_state(ctx), config)
            set_collection_state(ctx, state)
            return json.dumps(
                {"ok": True, "phase": state["phase"], "user_confirmed": True},
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool(
        name="collection_complete",
        description=(
            "Optional: store a draft_payload snapshot (e.g. markdown case text) "
            "before or after calling the write/update MCP tool."
        ),
    )
    def collection_complete(
        draft_payload: str = "",
        run_context: Any = None,
    ) -> str:
        ctx = run_context
        if ctx is None:
            return json.dumps({"ok": False, "error": "run_context missing"}, ensure_ascii=False)
        config = resolve_collection_config(workflow_from_run_context(ctx))
        if not config:
            return json.dumps(
                {"ok": False, "error": "collection protocol not enabled"},
                ensure_ascii=False,
            )
        try:
            state = store_collection_draft(
                get_collection_state(ctx),
                config,
                draft_payload or None,
            )
            set_collection_state(ctx, state)
            next_tool = suggested_write_tool(config)
            return json.dumps(
                {
                    "ok": True,
                    "phase": state["phase"],
                    "missing": state["missing"],
                    "draft_payload": state.get("draft_payload"),
                    "suggested_mcp_tool": next_tool,
                    "hint": (
                        f"Call MCP tool {next_tool} with the draft (or current collected "
                        "fields), then collection_mark_done. You may write again later "
                        "after the user adds more information."
                        if next_tool
                        else "Draft stored; call collection_mark_done after write succeeds."
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool(
        name="collection_mark_done",
        description=(
            "Record that a write/update MCP call succeeded. Blocked while "
            "required_actions tools are still pending. "
            "Safe after early or partial saves; user may continue adding fields "
            "and write again."
        ),
    )
    def collection_mark_done(
        result: str = "",
        run_context: Any = None,
    ) -> str:
        ctx = run_context
        if ctx is None:
            return json.dumps({"ok": False, "error": "run_context missing"}, ensure_ascii=False)
        config = resolve_collection_config(workflow_from_run_context(ctx))
        if not config:
            return json.dumps(
                {"ok": False, "error": "collection protocol not enabled"},
                ensure_ascii=False,
            )
        try:
            # Same-turn MCP successes: prefer run_context.tools, fall back to messages.
            state = apply_required_actions_from_run(
                get_collection_state(ctx),
                config,
                SimpleNamespace(
                    tools=getattr(ctx, "tools", None) or [],
                    messages=getattr(ctx, "messages", None) or [],
                ),
            )
            state = mark_collection_done(state, config, result or None)
            set_collection_state(ctx, state)
            return json.dumps(
                {
                    "ok": True,
                    "phase": state["phase"],
                    "completed": True,
                    "missing": state["missing"],
                    "actions_done": sorted((state.get("actions_done") or {}).keys()),
                    "hint": (
                        "Keep asking for missing fields."
                        if state.get("missing")
                        else "Required fields complete; further updates still allowed."
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    return [
        collection_load_schema,
        collection_update_fields,
        collection_status,
        collection_probe_note,
        collection_probe_finish,
        collection_confirm,
        collection_complete,
        collection_mark_done,
    ]


def snapshot_collection_for_log(state: dict[str, Any]) -> dict[str, Any]:
    """Compact view for logs (no large drafts)."""
    return {
        "phase": state.get("phase"),
        "missing": list(state.get("missing") or []),
        "collected_keys": sorted((state.get("collected") or {}).keys()),
        "current_field": state.get("current_field") or "",
        "field_stage": state.get("field_stage") or "",
        "pending_collected_keys": sorted((state.get("pending_collected") or {}).keys()),
        "user_confirmed": bool(state.get("user_confirmed")),
        "completed": bool(state.get("completed")),
        "actions_done": sorted((state.get("actions_done") or {}).keys()),
        "probe_rounds": state.get("probe_rounds") or 0,
        "probe_done": bool(state.get("probe_done")),
        "field_probe_active": state.get("field_probe_active") or "",
    }


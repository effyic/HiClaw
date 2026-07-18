"""Config-driven collection dialogue protocol (slot-filling FSM).

State lives only in ``session_state["collection"]``, which is persisted with the
Agno session row (``AGNO_DB_URL``). That makes multi-turn progress safe under
clustered agno_worker replicas — no in-process memory is required.

Write/update MCP tools from agent config stay visible. Missing required fields
drive follow-up questions via prompt + ``collection_*`` tools; the model may
still save or update a case early or repeatedly.

Enable by publishing ``agno_agent.workflow`` as either::

    {"kind": "collection_dialogue", "schema": {...}, "complete_action": {...}}

or nested under an existing medical workflow::

    {"kind": "medical", "phase": "inquiry", "collection": {...}}
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

COLLECTION_STATE_KEY = "collection"
STATUS_MARKER_PREFIX = "<!--COLLECTION_STATUS"
STATUS_MARKER_SUFFIX = "-->"

PHASE_INIT = "init"
PHASE_COLLECTING = "collecting"
PHASE_READY = "ready"
PHASE_CONFIRMED = "confirmed"
PHASE_DONE = "done"

_VALID_PHASES = frozenset(
    {PHASE_INIT, PHASE_COLLECTING, PHASE_READY, PHASE_CONFIRMED, PHASE_DONE}
)


def resolve_collection_config(workflow: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return collection protocol config from agent workflow JSON, or None."""
    if not isinstance(workflow, dict) or not workflow:
        return None
    if str(workflow.get("kind") or "").strip() == "collection_dialogue":
        return workflow
    nested = workflow.get("collection")
    if not isinstance(nested, dict) or not nested:
        return None
    kind = str(nested.get("kind") or "").strip()
    if kind == "collection_dialogue" or nested.get("schema") or nested.get("complete_action"):
        return nested
    return None


def is_collection_enabled(workflow: dict[str, Any] | None) -> bool:
    return resolve_collection_config(workflow) is not None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def confirm_required(config: dict[str, Any] | None) -> bool:
    if not config:
        return True
    return _as_bool(config.get("confirm_required"), True)


def ask_batch_size(config: dict[str, Any] | None) -> int:
    raw = (config or {}).get("ask_batch_size", 2)
    try:
        size = int(raw)
    except (TypeError, ValueError):
        size = 2
    return max(1, min(size, 5))


def suggested_write_tool(config: dict[str, Any] | None) -> str | None:
    """Return complete_action MCP tool name for prompt hints."""
    if not config:
        return None
    action = config.get("complete_action")
    if isinstance(action, dict) and str(action.get("type") or "").strip() == "mcp":
        tool = str(action.get("tool") or "").strip()
        return tool or None
    return None


def normalize_schema_fields(raw_fields: Any) -> list[dict[str, Any]]:
    """Normalize schema fields; preserve generic constraints (pattern / exports).

    Domain-specific slots (e.g. medical triage dept code) belong in published
    ``agno_agent.workflow`` or ``transform_workflow_hook``, not in worker code.
    """
    if not isinstance(raw_fields, list):
        return []
    fields: list[dict[str, Any]] = []
    for item in raw_fields:
        if not isinstance(item, dict):
            continue
        name = str(
            item.get("name")
            or item.get("fieldKey")
            or item.get("label")
            or ""
        ).strip()
        if not name:
            continue
        if item.get("enabled") is False:
            continue
        field: dict[str, Any] = {
            "name": name,
            "description": str(item.get("description") or "").strip(),
            "required": _as_bool(item.get("required"), True),
        }
        pattern = str(item.get("pattern") or "").strip()
        if pattern:
            field["pattern"] = pattern
        pattern_message = str(item.get("pattern_message") or "").strip()
        if pattern_message:
            field["pattern_message"] = pattern_message
        exports = item.get("exports")
        if isinstance(exports, dict) and exports:
            field["exports"] = {
                str(k): v for k, v in exports.items() if str(k).strip()
            }
        fields.append(field)
    return fields


def resolve_inline_schema_fields(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Resolve inline schema.fields from workflow config only (no domain defaults)."""
    if not isinstance(config, dict):
        return []
    schema_cfg = config.get("schema") if isinstance(config.get("schema"), dict) else {}
    return normalize_schema_fields(schema_cfg.get("fields"))


def _schema_field_by_name(
    schema: list[dict[str, Any]],
    name: str,
) -> dict[str, Any] | None:
    for field in schema:
        if str(field.get("name") or "").strip() == name:
            return field
    return None


def _validate_field_value(
    schema: list[dict[str, Any]],
    name: str,
    text: str,
) -> str:
    """Enforce optional per-field ``pattern`` from published schema."""
    field = _schema_field_by_name(schema, name)
    if not field:
        return text
    pattern = str(field.get("pattern") or "").strip()
    if not pattern:
        return text
    try:
        matched = re.fullmatch(pattern, text)
    except re.error as exc:
        logger.warning("invalid schema pattern for field %s: %s", name, exc)
        return text
    if not matched:
        message = str(field.get("pattern_message") or "").strip() or (
            f"field {name!r} must match pattern {pattern!r}"
        )
        raise ValueError(message)
    return text


def _export_from_text(source: str, spec: Any) -> str:
    """Extract one export value; ``spec`` is a regex str or {regex, group}."""
    text = str(source or "")
    if not text or spec is None:
        return ""
    if isinstance(spec, str):
        regex, group = spec, 1
    elif isinstance(spec, dict):
        regex = str(spec.get("regex") or "").strip()
        group = spec.get("group", 1)
    else:
        return ""
    if not regex:
        return ""
    try:
        matched = re.search(regex, text)
    except re.error as exc:
        logger.warning("invalid export regex %r: %s", regex, exc)
        return ""
    if not matched:
        return ""
    try:
        if isinstance(group, str):
            return str(matched.group(group) or "").strip()
        return str(matched.group(int(group)) or "").strip()
    except (IndexError, KeyError, ValueError):
        return ""


def apply_schema_exports(
    schema: list[dict[str, Any]] | None,
    collected: dict[str, Any] | None,
    reply_text: str | None = None,
) -> dict[str, str]:
    """Project schema field ``exports`` into flat keys for H5 / metadata."""
    out: dict[str, str] = {}
    if not isinstance(schema, list):
        return out
    collected_map = collected if isinstance(collected, dict) else {}
    for field in schema:
        if not isinstance(field, dict):
            continue
        exports = field.get("exports")
        if not isinstance(exports, dict) or not exports:
            continue
        name = str(field.get("name") or "").strip()
        source = str(collected_map.get(name) or "").strip()
        if not source and reply_text:
            source = str(reply_text)
        for export_key, spec in exports.items():
            key = str(export_key).strip()
            if not key or key in out:
                continue
            value = _export_from_text(source, spec)
            if value:
                out[key] = value
    return out


def compute_missing(
    schema: list[dict[str, Any]],
    collected: dict[str, Any],
) -> list[str]:
    missing: list[str] = []
    for field in schema:
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        if not _as_bool(field.get("required"), True):
            continue
        value = collected.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


def empty_collection_state() -> dict[str, Any]:
    return {
        "phase": PHASE_INIT,
        "schema": [],
        "collected": {},
        "missing": [],
        "user_confirmed": False,
        "completed": False,
        "draft_payload": None,
        "complete_result": None,
    }


def ensure_collection_state(
    existing: Any,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = empty_collection_state()
    if isinstance(existing, dict):
        state.update({k: v for k, v in existing.items() if k in state or k == "scenario"})
        if isinstance(existing.get("collected"), dict):
            state["collected"] = {
                str(k): v for k, v in existing["collected"].items() if str(k).strip()
            }
        if isinstance(existing.get("schema"), list):
            state["schema"] = normalize_schema_fields(existing["schema"])
        if isinstance(existing.get("missing"), list):
            state["missing"] = [str(x) for x in existing["missing"]]
        phase = str(existing.get("phase") or PHASE_INIT).strip()
        state["phase"] = phase if phase in _VALID_PHASES else PHASE_INIT
        state["user_confirmed"] = _as_bool(existing.get("user_confirmed"), False)
        state["completed"] = _as_bool(existing.get("completed"), False)

    # Inline schema is always owned by published agno_agent.workflow (SaaS-editable).
    # Reconcile every call so admin field edits take effect without writing agent rows
    # and without freezing the first-turn snapshot in session_state.
    if config and schema_source(config) == "inline":
        state = reconcile_inline_schema_from_config(state, config)
    elif state["schema"]:
        state["missing"] = compute_missing(state["schema"], state["collected"])
    return state


def reconcile_inline_schema_from_config(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Adopt latest inline schema.fields; keep collected values only for still-valid keys.

    Never persists back to ``agno_agent`` — only mutates in-memory / session_state
    collection progress for the current conversation.
    """
    out = dict(state or empty_collection_state())
    latest = resolve_inline_schema_fields(config)
    allowed = {
        str(field.get("name") or "").strip()
        for field in latest
        if str(field.get("name") or "").strip()
    }
    collected_in = out.get("collected") if isinstance(out.get("collected"), dict) else {}
    out["collected"] = {
        str(k): v
        for k, v in collected_in.items()
        if str(k).strip() and str(k).strip() in allowed
    }
    out["schema"] = latest
    out["missing"] = compute_missing(out["schema"], out["collected"])
    if out["schema"] and out.get("phase") == PHASE_INIT:
        out["phase"] = PHASE_COLLECTING
    elif not out["schema"]:
        # Published template has empty fields — do not invent domain slots.
        out["phase"] = PHASE_INIT
    return out


def advance_collection_phase(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Deterministically advance phase from collected/missing/confirm flags.

    ``completed`` means \"wrote at least once\" and does not freeze the FSM:
    if the user later supplies more fields (or required ones are still missing),
    phase returns to collecting so the model keeps asking.
    """
    out = ensure_collection_state(state, config)
    if not out["schema"]:
        out["phase"] = PHASE_INIT
        return out

    out["missing"] = compute_missing(out["schema"], out["collected"])
    if out["missing"]:
        out["phase"] = PHASE_COLLECTING
        return out

    # schema loaded and required fields filled
    if confirm_required(config) and not out["user_confirmed"]:
        out["phase"] = PHASE_READY
        return out

    if out["completed"]:
        out["phase"] = PHASE_DONE
        return out

    out["phase"] = PHASE_CONFIRMED
    return out


def _request_confirm_flag(run_context: Any) -> bool:
    metadata = getattr(run_context, "metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    for key in ("confirm", "collection_confirm", "user_confirmed"):
        if key in metadata and _as_bool(metadata.get(key), False):
            return True

    headers = metadata.get("request_headers")
    if not isinstance(headers, dict):
        return False
    lower = {str(k).lower(): v for k, v in headers.items()}
    for key in ("x-collection-confirm", "x-confirm", "collection-confirm"):
        if key in headers and _as_bool(headers.get(key), False):
            return True
        if key.lower() in lower and _as_bool(lower[key.lower()], False):
            return True
    return False


def apply_metadata_flags(
    state: dict[str, Any],
    run_context: Any,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Honor client confirm flags from metadata / headers (cluster-safe)."""
    out = ensure_collection_state(state, config)
    if _request_confirm_flag(run_context) and not out["missing"] and out["schema"]:
        out["user_confirmed"] = True
    return advance_collection_phase(out, config)


def load_schema_into_state(
    state: dict[str, Any],
    fields: list[dict[str, Any]] | None,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    out = ensure_collection_state(state, config)
    schema_fields: list[dict[str, Any]] = []
    if fields:
        schema_fields = normalize_schema_fields(fields)
    elif config:
        schema_cfg = config.get("schema") if isinstance(config.get("schema"), dict) else {}
        source = str(schema_cfg.get("source") or "inline").strip()
        if source == "inline":
            schema_fields = resolve_inline_schema_fields(config)
        else:
            raise ValueError(
                f"schema.source={source!r} requires fields_json from MCP "
                f"(call the schema tool then collection_load_schema with the result)"
            )
    if not schema_fields:
        raise ValueError("no schema fields available to load")

    out["schema"] = schema_fields
    out["missing"] = compute_missing(out["schema"], out["collected"])
    if out["phase"] == PHASE_INIT:
        out["phase"] = PHASE_COLLECTING
    return advance_collection_phase(out, config)


def schema_field_names(schema: list[dict[str, Any]]) -> set[str]:
    return {
        str(field.get("name") or "").strip()
        for field in schema
        if str(field.get("name") or "").strip()
    }


def update_collected_fields(
    state: dict[str, Any],
    updates: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    out = ensure_collection_state(state, config)
    if not isinstance(updates, dict) or not updates:
        raise ValueError("fields must be a non-empty object of {name: value}")
    if not out["schema"]:
        raise ValueError(
            "schema is empty; call collection_load_schema before collection_update_fields"
        )

    allowed = schema_field_names(out["schema"])
    unknown = [str(k).strip() for k in updates if str(k).strip() and str(k).strip() not in allowed]
    if unknown:
        raise ValueError(
            "unknown field names (must match schema): "
            + ", ".join(unknown)
            + "; allowed="
            + ", ".join(sorted(allowed))
        )

    collected = dict(out["collected"])
    for key, value in updates.items():
        name = str(key).strip()
        if not name:
            continue
        if value is None:
            collected.pop(name, None)
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = text.strip()
        if not text:
            collected.pop(name, None)
        else:
            collected[name] = _validate_field_value(out["schema"], name, text)
    out["collected"] = collected
    return advance_collection_phase(out, config)


def confirm_collection(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    out = ensure_collection_state(state, config)
    if out["missing"]:
        raise ValueError(
            "cannot confirm while required fields are missing: "
            + ", ".join(out["missing"])
        )
    out["user_confirmed"] = True
    return advance_collection_phase(out, config)


def store_collection_draft(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    draft_payload: str | None = None,
) -> dict[str, Any]:
    """Store an optional draft_payload snapshot (e.g. markdown case text)."""
    out = ensure_collection_state(state, config)
    if draft_payload is not None and str(draft_payload).strip():
        out["draft_payload"] = str(draft_payload).strip()
    if not out["missing"] and out["schema"]:
        out["user_confirmed"] = True
    return advance_collection_phase(out, config)


def mark_collection_done(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    result: Any = None,
) -> dict[str, Any]:
    """Record that a write / update MCP succeeded. Allows early and repeated writes."""
    out = ensure_collection_state(state, config)
    out["completed"] = True
    if result is not None:
        out["complete_result"] = (
            result if isinstance(result, (str, dict, list)) else str(result)
        )
    return advance_collection_phase(out, config)


def workflow_from_run_context(run_context: Any) -> dict[str, Any]:
    session_state = getattr(run_context, "session_state", None) or {}
    if isinstance(session_state, dict):
        workflow = session_state.get("workflow")
        if isinstance(workflow, dict) and workflow:
            return workflow
    deps = getattr(run_context, "dependencies", None) or {}
    agent_config = deps.get("agent_config") if isinstance(deps, dict) else None
    if isinstance(agent_config, dict):
        workflow = agent_config.get("workflow")
        if isinstance(workflow, dict):
            return workflow
    business = deps.get("business_context") if isinstance(deps, dict) else None
    if isinstance(business, dict):
        agent_config = business.get("agent_config")
        if isinstance(agent_config, dict):
            workflow = agent_config.get("workflow")
            if isinstance(workflow, dict):
                return workflow
    return {}


def get_collection_state(run_context: Any) -> dict[str, Any]:
    session_state = getattr(run_context, "session_state", None)
    if not isinstance(session_state, dict):
        return empty_collection_state()
    config = resolve_collection_config(workflow_from_run_context(run_context))
    return ensure_collection_state(session_state.get(COLLECTION_STATE_KEY), config)


def set_collection_state(run_context: Any, state: dict[str, Any]) -> dict[str, Any]:
    if getattr(run_context, "session_state", None) is None:
        run_context.session_state = {}
    config = resolve_collection_config(workflow_from_run_context(run_context))
    advanced = advance_collection_phase(state, config)
    run_context.session_state[COLLECTION_STATE_KEY] = advanced
    run_context.session_state["phase"] = advanced["phase"]
    return advanced


def sync_collection_into_session_state(
    session_state: dict[str, Any],
    run_context: Any,
    workflow: dict[str, Any],
) -> dict[str, Any]:
    """Merge collection FSM into session_state for DB persistence (cluster-safe)."""
    config = resolve_collection_config(workflow)
    if not config:
        return session_state

    merged = dict(session_state or {})
    state = ensure_collection_state(merged.get(COLLECTION_STATE_KEY), config)
    state = apply_metadata_flags(state, run_context, config)
    state = advance_collection_phase(state, config)
    merged[COLLECTION_STATE_KEY] = state
    merged["phase"] = state["phase"]
    return merged


def schema_source(config: dict[str, Any] | None) -> str:
    if not config:
        return "inline"
    schema_cfg = config.get("schema")
    if not isinstance(schema_cfg, dict):
        return "inline"
    return str(schema_cfg.get("source") or "inline").strip() or "inline"


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
    lines = [
        "## collection_protocol",
        f"phase: {current.get('phase')}",
        f"schema_source: {source}",
        f"ask_batch_size: {batch}",
        f"confirm_required: {confirm_required(config)}",
        f"user_confirmed: {current.get('user_confirmed')}",
        f"completed_once: {current.get('completed')}",
        f"missing: {json.dumps(missing, ensure_ascii=False)}",
        f"collected: {json.dumps(current.get('collected') or {}, ensure_ascii=False)}",
        "",
        "Rules:",
    ]
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
            "3. While missing is non-empty, keep asking the patient for those fields. "
            "You may still call write/update MCP if the user asks to save a partial case.",
            "4. Do not invent completion; call collection_status to inspect progress.",
            "5. When missing is empty, summarize for the user"
            + (
                " and ask for confirmation (collection_confirm or wait for confirm)."
                if confirm_required(config)
                else "."
            ),
            "6. Write/update MCP tools are always available. After a successful write, "
            "call collection_mark_done(result=...). User may later add symptoms — "
            "update fields and write again.",
        ]
    )
    if focus:
        lines.append(f"7. This turn focus fields: {json.dumps(focus, ensure_ascii=False)}")
    if write_tool:
        lines.append(f"8. Suggested write/update MCP tool: {write_tool}")
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
            "9. Fields with pattern MUST match when calling collection_update_fields: "
            + json.dumps(patterned, ensure_ascii=False)
        )
    return "\n".join(lines)


def collection_status_payload(
    state: dict[str, Any],
    reply_text: str | None = None,
) -> dict[str, Any]:
    """Structured progress for H5 / SSE metadata (prefer over parsing reply text).

    Generic payload includes ``collected`` plus optional keys projected from each
    schema field's ``exports`` (configured at publish time / via workflow hook).
    """
    collected = state.get("collected") if isinstance(state.get("collected"), dict) else {}
    schema = state.get("schema") if isinstance(state.get("schema"), list) else []
    payload: dict[str, Any] = {
        "phase": state.get("phase"),
        "missing": list(state.get("missing") or []),
        "ready": not bool(state.get("missing")) and bool(schema),
        "user_confirmed": bool(state.get("user_confirmed")),
        "completed": bool(state.get("completed")),
        "collected_keys": sorted(collected.keys()),
        "collected": dict(collected),
    }
    exported = apply_schema_exports(schema, collected, reply_text=reply_text)
    payload.update(exported)
    return payload


def format_status_marker(
    state: dict[str, Any],
    reply_text: str | None = None,
) -> str:
    body = json.dumps(
        collection_status_payload(state, reply_text=reply_text),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{STATUS_MARKER_PREFIX} {body}{STATUS_MARKER_SUFFIX}"


def append_status_marker(content: str | None, state: dict[str, Any]) -> str:
    text = str(content or "")
    marker = format_status_marker(state, reply_text=text)
    if STATUS_MARKER_PREFIX in text:
        # Refresh marker so late-parsed dept_code from the reply is visible to H5.
        prefix, _, _tail = text.partition(STATUS_MARKER_PREFIX)
        return f"{prefix.rstrip()}\n\n{marker}" if prefix.strip() else marker
    if text.strip():
        return f"{text.rstrip()}\n\n{marker}"
    return marker


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
            set_collection_state(ctx, state)
            return json.dumps(
                {
                    "ok": True,
                    "phase": state["phase"],
                    "missing": state["missing"],
                    "collected": state["collected"],
                },
                ensure_ascii=False,
            )
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
        return json.dumps(
            {
                "ok": True,
                "enabled": config is not None,
                "phase": state["phase"],
                "missing": state["missing"],
                "collected": state["collected"],
                "schema": state["schema"],
                "user_confirmed": state["user_confirmed"],
                "completed": state["completed"],
            },
            ensure_ascii=False,
        )

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
            "Record that a write/update MCP call succeeded. Safe after early or "
            "partial saves; user may continue adding fields and write again."
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
            state = mark_collection_done(
                get_collection_state(ctx),
                config,
                result or None,
            )
            set_collection_state(ctx, state)
            return json.dumps(
                {
                    "ok": True,
                    "phase": state["phase"],
                    "completed": True,
                    "missing": state["missing"],
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
        "user_confirmed": bool(state.get("user_confirmed")),
        "completed": bool(state.get("completed")),
    }

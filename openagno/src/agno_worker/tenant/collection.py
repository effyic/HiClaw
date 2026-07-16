"""Config-driven collection dialogue protocol (slot-filling FSM).

State lives only in ``session_state["collection"]``, which is persisted with the
Agno session row (``AGNO_DB_URL``). That makes multi-turn progress safe under
clustered agno_worker replicas — no in-process memory is required.

Enable by publishing ``agno_agent.workflow`` as either::

    {"kind": "collection_dialogue", "schema": {...}, "complete_action": {...}}

or nested under an existing medical workflow::

    {"kind": "medical", "phase": "inquiry", "collection": {...}}
"""
from __future__ import annotations

import json
import logging
from typing import Any

from agno_worker.hooks.protocols import MCPServerConfig

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


def gated_mcp_tools(config: dict[str, Any] | None) -> list[str]:
    if not config:
        return []
    names: list[str] = []
    raw = config.get("gated_mcp_tools")
    if isinstance(raw, list):
        names.extend(str(item).strip() for item in raw if str(item).strip())
    action = config.get("complete_action")
    if isinstance(action, dict) and str(action.get("type") or "").strip() == "mcp":
        tool = str(action.get("tool") or "").strip()
        if tool and tool not in names:
            names.append(tool)
    return names


def normalize_schema_fields(raw_fields: Any) -> list[dict[str, Any]]:
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
        fields.append(
            {
                "name": name,
                "description": str(item.get("description") or "").strip(),
                "required": _as_bool(item.get("required"), True),
            }
        )
    return fields


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
        "completion_authorized": False,
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
        state["completion_authorized"] = _as_bool(
            existing.get("completion_authorized"), False
        )
        state["completed"] = _as_bool(existing.get("completed"), False)

    if config and not state["schema"]:
        schema_cfg = config.get("schema")
        if isinstance(schema_cfg, dict) and str(schema_cfg.get("source") or "") == "inline":
            state["schema"] = normalize_schema_fields(schema_cfg.get("fields"))
            state["missing"] = compute_missing(state["schema"], state["collected"])
            if state["schema"] and state["phase"] == PHASE_INIT:
                state["phase"] = PHASE_COLLECTING

    if state["schema"]:
        state["missing"] = compute_missing(state["schema"], state["collected"])
    return state


def advance_collection_phase(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Deterministically advance phase from collected/missing/confirm flags."""
    out = ensure_collection_state(state, config)
    if out["completed"]:
        out["phase"] = PHASE_DONE
        out["missing"] = []
        return out

    if not out["schema"]:
        out["phase"] = PHASE_INIT
        return out

    out["missing"] = compute_missing(out["schema"], out["collected"])
    if out["missing"]:
        out["phase"] = PHASE_COLLECTING
        out["completion_authorized"] = False
        return out

    # schema loaded and required fields filled
    if confirm_required(config) and not out["user_confirmed"]:
        out["phase"] = PHASE_READY
        out["completion_authorized"] = False
        return out

    out["phase"] = PHASE_CONFIRMED
    return out


def is_write_authorized(state: dict[str, Any], config: dict[str, Any] | None) -> bool:
    """Gated MCP write tools are visible when required slots are filled and confirmed."""
    current = ensure_collection_state(state, config)
    if current["completed"]:
        return False
    if not current["schema"]:
        return False
    if current["missing"]:
        return False
    if confirm_required(config) and not current["user_confirmed"]:
        return False
    return True


def apply_metadata_flags(
    state: dict[str, Any],
    run_context: Any,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Honor client confirm flags from metadata / headers (cluster-safe)."""
    out = ensure_collection_state(state, config)
    metadata = getattr(run_context, "metadata", None) or {}
    headers = metadata.get("request_headers") if isinstance(metadata, dict) else None
    if not isinstance(headers, dict):
        headers = {}

    confirm = False
    for key in ("confirm", "collection_confirm", "user_confirmed"):
        if key in metadata and _as_bool(metadata.get(key), False):
            confirm = True
    for key in ("x-collection-confirm", "x-confirm", "collection-confirm"):
        if key in headers and _as_bool(headers.get(key), False):
            confirm = True
        lower = {str(k).lower(): v for k, v in headers.items()}
        if key.lower() in lower and _as_bool(lower[key.lower()], False):
            confirm = True

    if confirm and not out["missing"] and out["schema"]:
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
            schema_fields = normalize_schema_fields(schema_cfg.get("fields"))
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


def update_collected_fields(
    state: dict[str, Any],
    updates: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    out = ensure_collection_state(state, config)
    if not isinstance(updates, dict) or not updates:
        raise ValueError("fields must be a non-empty object of {name: value}")
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
            collected[name] = text
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


def authorize_complete(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    draft_payload: str | None = None,
) -> dict[str, Any]:
    out = ensure_collection_state(state, config)
    if out["missing"]:
        raise ValueError(
            "cannot complete while required fields are missing: "
            + ", ".join(out["missing"])
        )
    if confirm_required(config) and not out["user_confirmed"]:
        raise ValueError("user confirmation required before complete")
    out["user_confirmed"] = True
    out["completion_authorized"] = True
    if draft_payload is not None and str(draft_payload).strip():
        out["draft_payload"] = str(draft_payload).strip()
    out = advance_collection_phase(out, config)
    out["phase"] = PHASE_CONFIRMED
    return out


def mark_collection_done(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    result: Any = None,
) -> dict[str, Any]:
    out = ensure_collection_state(state, config)
    if out["missing"]:
        raise ValueError(
            "cannot mark done while required fields are missing: "
            + ", ".join(out["missing"])
        )
    if confirm_required(config) and not out["user_confirmed"]:
        raise ValueError("user confirmation required before mark_done")
    out["completed"] = True
    out["completion_authorized"] = False
    out["phase"] = PHASE_DONE
    if result is not None:
        out["complete_result"] = (
            result if isinstance(result, (str, dict, list)) else str(result)
        )
    return out


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


def apply_collection_mcp_excludes(
    run_context: Any,
    servers: list[MCPServerConfig],
) -> list[MCPServerConfig]:
    """Hide gated write MCP tools until collection_complete authorizes them."""
    workflow = workflow_from_run_context(run_context)
    config = resolve_collection_config(workflow)
    if not config or not servers:
        return servers

    gated = gated_mcp_tools(config)
    if not gated:
        return servers

    state = get_collection_state(run_context)
    if is_write_authorized(state, config):
        return servers

    gated_set = set(gated)
    updated: list[MCPServerConfig] = []
    for server in servers:
        exclude = list(server.exclude_tools or [])
        changed = False
        for name in gated_set:
            if name not in exclude:
                exclude.append(name)
                changed = True
        if not changed:
            updated.append(server)
            continue
        updated.append(
            MCPServerConfig(
                name=server.name,
                url=server.url,
                command=server.command,
                transport=server.transport,
                env=dict(server.env or {}),
                headers=dict(server.headers or {}),
                include_tools=list(server.include_tools or []),
                exclude_tools=exclude,
            )
        )
    return updated


def collection_instructions_appendix(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> str:
    current = ensure_collection_state(state, config)
    batch = ask_batch_size(config)
    missing = current.get("missing") or []
    focus = missing[:batch]
    lines = [
        "## collection_protocol",
        f"phase: {current.get('phase')}",
        f"ask_batch_size: {batch}",
        f"confirm_required: {confirm_required(config)}",
        f"user_confirmed: {current.get('user_confirmed')}",
        f"completed: {current.get('completed')}",
        f"missing: {json.dumps(missing, ensure_ascii=False)}",
        f"collected: {json.dumps(current.get('collected') or {}, ensure_ascii=False)}",
        "",
        "Rules:",
        "1. If schema is empty, call collection_load_schema first "
        "(inline schema needs no args; MCP schema: fetch fields then pass fields_json).",
        f"2. Each turn ask at most {batch} items from missing; then call collection_update_fields.",
        "3. Do not invent completion; call collection_status to inspect progress.",
        "4. When missing is empty, summarize for the user"
        + (" and ask for confirmation, then collection_confirm." if confirm_required(config) else "."),
        "5. After confirmation call collection_complete with the final payload, "
        "then call the configured MCP write tool, then collection_mark_done.",
        "6. Never call gated write MCP tools before required fields are complete"
        + (" and the user has confirmed." if confirm_required(config) else "."),
    ]
    if focus:
        lines.append(f"7. This turn focus fields: {json.dumps(focus, ensure_ascii=False)}")
    action = (config or {}).get("complete_action")
    if isinstance(action, dict) and action.get("tool"):
        lines.append(
            f"8. complete_action MCP tool when authorized: {action.get('tool')}"
        )
    return "\n".join(lines)


def format_status_marker(state: dict[str, Any]) -> str:
    payload = {
        "phase": state.get("phase"),
        "missing": state.get("missing") or [],
        "ready": not bool(state.get("missing")) and bool(state.get("schema")),
        "user_confirmed": bool(state.get("user_confirmed")),
        "completed": bool(state.get("completed")),
        "collected_keys": sorted((state.get("collected") or {}).keys()),
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"{STATUS_MARKER_PREFIX} {body}{STATUS_MARKER_SUFFIX}"


def append_status_marker(content: str | None, state: dict[str, Any]) -> str:
    text = str(content or "")
    marker = format_status_marker(state)
    if STATUS_MARKER_PREFIX in text:
        return text
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
                "completion_authorized": state["completion_authorized"],
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
            "Authorize completion after fields are filled (and confirmed if required). "
            "Pass draft_payload (e.g. markdown case). Unlocks gated MCP write tools."
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
            state = authorize_complete(
                get_collection_state(ctx),
                config,
                draft_payload or None,
            )
            set_collection_state(ctx, state)
            action = config.get("complete_action") if isinstance(config, dict) else None
            next_tool = None
            if isinstance(action, dict) and str(action.get("type") or "") == "mcp":
                next_tool = str(action.get("tool") or "").strip() or None
            return json.dumps(
                {
                    "ok": True,
                    "phase": state["phase"],
                    "completion_authorized": True,
                    "draft_payload": state.get("draft_payload"),
                    "call_mcp_tool": next_tool,
                    "hint": (
                        f"Now call MCP tool {next_tool} with the draft payload, "
                        "then collection_mark_done."
                        if next_tool
                        else "Completion authorized; call collection_mark_done when finished."
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool(
        name="collection_mark_done",
        description="Mark collection completed after gated write MCP (or side effect) succeeded.",
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
                {"ok": True, "phase": state["phase"], "completed": True},
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
        "completion_authorized": bool(state.get("completion_authorized")),
        "completed": bool(state.get("completed")),
    }

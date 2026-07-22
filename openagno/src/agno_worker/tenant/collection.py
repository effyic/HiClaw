"""Config-driven collection dialogue protocol (slot-filling FSM).

State lives only in ``session_state["collection"]``, which is persisted with the
Agno session row (``AGNO_DB_URL``). That makes multi-turn progress safe under
clustered agno_worker replicas — no in-process memory is required.

Write/update MCP tools from agent config stay visible. Missing required fields
drive follow-up questions via prompt + ``collection_*`` tools; the model may
still save or update a case early or repeatedly.

``required_actions`` enforces post-collection MCP (or other) tools: successes
are recorded into ``session_state.collection.actions_done`` from the same-turn
``run_output.tools`` / messages (cluster-safe). ``collection_mark_done`` is
rejected while required actions are still pending.

Enable by publishing ``agno_agent.workflow`` as either::

    {
      "kind": "collection_dialogue",
      "schema": {...},
      "required_actions": [
        {"type": "mcp", "tool": "your_write_tool", "when": "missing_empty"}
      ]
    }

Domain policy (tone, triage rules, EMR, etc.) belongs in the published agent
``system_prompt`` / ``instructions_append`` / ``probe.goal`` — not in this module.

Single and multiple end-actions use the same list field (length 1 or N).
"""
from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

COLLECTION_STATE_KEY = "collection"
STATUS_MARKER_PREFIX = "<!--COLLECTION_STATUS"
STATUS_MARKER_SUFFIX = "-->"

PHASE_INIT = "init"
PHASE_COLLECTING = "collecting"
PHASE_PROBING = "probing"
PHASE_READY = "ready"
PHASE_CONFIRMED = "confirmed"
PHASE_DONE = "done"

_VALID_PHASES = frozenset(
    {
        PHASE_INIT,
        PHASE_COLLECTING,
        PHASE_PROBING,
        PHASE_READY,
        PHASE_CONFIRMED,
        PHASE_DONE,
    }
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
    if kind == "collection_dialogue" or nested.get("schema") or nested.get("required_actions"):
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


# Generic early-finish allowlist (SaaS-safe). Domain keywords (e.g. 急症/120)
# belong in ``workflow.probe.early_finish_keywords``.
_DEFAULT_PROBE_EARLY_FINISH_KEYWORDS = (
    "拒绝",
    "不想",
    "不答",
    "refuse",
    "skip",
    "emergency",
)


def resolve_probe_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Optional enrichment loop after gate / required slots are filled.

    Published under ``workflow.probe``::

        {
          "enabled": true,
          "max_rounds": 3,
          "min_rounds": 2,
          "goal": "optional domain purpose string from the tenant agent",
          "hints": [],
          "allow_skip": true,
          "gate_fields": ["slot_a", "slot_b"],
          "early_finish_keywords": ["拒绝", "refuse", "emergency"]
        }

    Platform owns only the FSM (rounds / notes / gates). What to ask and why
    comes from ``goal`` / ``hints`` / agent instructions — never hardcoded here.

    - ``goal``: free-text enrichment purpose injected into the protocol.
    - ``hints``: optional dimension checklist. Empty → model follows ``goal`` +
      dialogue + collected (recommended for adaptive questioning).
      Non-empty → prefer unanswered dimensions; not a fixed script.
    - ``min_rounds``: early ``collection_probe_finish`` blocked until this many
      ``collection_probe_note`` calls (unless reason matches early_finish_keywords).
    - ``gate_fields`` (optional): enter probing once these names are filled, even if
      other required slots are still missing. After probe finishes, FSM returns to
      collecting for the remaining required fields.
      If omitted/empty: probing starts only when **all** required fields are filled.
    - ``early_finish_keywords``: substrings in finish reason that bypass min_rounds.
    - While ``phase=probing``, required MCP write tools stay gated.
    """
    if not isinstance(config, dict):
        return None
    raw = config.get("probe")
    if not isinstance(raw, dict) or not raw:
        return None
    if not _as_bool(raw.get("enabled"), False):
        return None
    try:
        max_rounds = int(raw.get("max_rounds", 3))
    except (TypeError, ValueError):
        max_rounds = 3
    max_rounds = max(0, min(max_rounds, 8))
    try:
        min_rounds = int(raw.get("min_rounds", 0))
    except (TypeError, ValueError):
        min_rounds = 0
    min_rounds = max(0, min(min_rounds, max_rounds if max_rounds > 0 else 0))
    hints_raw = raw.get("hints")
    hints: list[str] = []
    if isinstance(hints_raw, list):
        hints = [str(x).strip() for x in hints_raw if str(x).strip()]
    gate_raw = raw.get("gate_fields")
    gate_fields: list[str] = []
    if isinstance(gate_raw, list):
        gate_fields = [str(x).strip() for x in gate_raw if str(x).strip()]
    goal = str(raw.get("goal") or "").strip()
    kw_raw = raw.get("early_finish_keywords")
    if isinstance(kw_raw, list) and kw_raw:
        early_finish_keywords = [str(x).strip() for x in kw_raw if str(x).strip()]
    else:
        early_finish_keywords = list(_DEFAULT_PROBE_EARLY_FINISH_KEYWORDS)
    return {
        "enabled": True,
        "max_rounds": max_rounds,
        "min_rounds": min_rounds,
        "goal": goal,
        "hints": hints,
        "allow_skip": _as_bool(raw.get("allow_skip"), True),
        "gate_fields": gate_fields,
        "early_finish_keywords": early_finish_keywords,
    }


def probe_max_rounds(config: dict[str, Any] | None) -> int:
    probe = resolve_probe_config(config)
    if not probe:
        return 0
    return int(probe.get("max_rounds") or 0)


def _gate_fields_filled(state: dict[str, Any], gate_fields: list[str]) -> bool:
    collected = state.get("collected") if isinstance(state.get("collected"), dict) else {}
    for name in gate_fields:
        value = collected.get(name)
        if value is None or (isinstance(value, str) and not str(value).strip()):
            return False
    return True


def is_probe_ready(state: dict[str, Any], config: dict[str, Any] | None) -> bool:
    """Whether enrichment may start (gate_fields filled, or all required filled)."""
    probe = resolve_probe_config(config)
    if not probe:
        return False
    gate_fields = probe.get("gate_fields") or []
    if gate_fields:
        return _gate_fields_filled(state, gate_fields)
    return not bool(state.get("missing"))


def is_probe_finished(state: dict[str, Any], config: dict[str, Any] | None) -> bool:
    """True when probe is disabled, skipped/finished, or max rounds reached."""
    probe = resolve_probe_config(config)
    if not probe:
        return True
    if _as_bool(state.get("probe_done"), False):
        return True
    try:
        rounds = int(state.get("probe_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    return rounds >= int(probe.get("max_rounds") or 0)


def is_probe_active(state: dict[str, Any], config: dict[str, Any] | None) -> bool:
    """True while enrichment loop should run (ready and not finished)."""
    if not resolve_probe_config(config):
        return False
    if is_probe_finished(state, config):
        return False
    return is_probe_ready(state, config)


def resolve_required_actions(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize ``required_actions`` (unified list for one or many end-actions)."""
    if not config:
        return []
    raw = config.get("required_actions")
    if not isinstance(raw, list):
        return []
    actions: list[dict[str, Any]] = []
    for item in raw:
        normalized = _normalize_required_action(item)
        if normalized:
            actions.append(normalized)
    return actions


def suggested_write_tool(config: dict[str, Any] | None) -> str | None:
    """Return the last MCP tool in ``required_actions`` for prompt hints."""
    actions = resolve_required_actions(config)
    if not actions:
        return None
    tool = str(actions[-1].get("tool") or "").strip()
    return tool or None


def _normalize_required_action(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    action_type = str(item.get("type") or "mcp").strip() or "mcp"
    tool = str(item.get("tool") or "").strip()
    if action_type != "mcp" or not tool:
        return None
    when = str(item.get("when") or "missing_empty").strip() or "missing_empty"
    if when not in {"missing_empty", "before_mark_done"}:
        when = "missing_empty"
    # Hard-hide tool from the model until when-condition is met (prevents
    # prompt-only bypass). Default on for missing_empty; off for before_mark_done.
    if "hard_gate" in item:
        hard_gate = _as_bool(item.get("hard_gate"), True)
    else:
        hard_gate = when == "missing_empty"
    return {
        "type": action_type,
        "tool": tool,
        "when": when,
        "hard_gate": hard_gate,
    }


def required_action_when_met(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    action: dict[str, Any],
    *,
    for_mark_done: bool = False,
) -> bool:
    """Whether a required_action's ``when`` condition is currently satisfied."""
    current = ensure_collection_state(state, config)
    when = str(action.get("when") or "missing_empty")
    if when == "before_mark_done":
        return bool(for_mark_done)
    # missing_empty: required slots filled and probe finished (if enabled)
    if current.get("missing"):
        return False
    if not current.get("schema"):
        return False
    if not is_probe_finished(current, config):
        return False
    return True


def hard_gated_tool_names(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> set[str]:
    """MCP tool names that must be hidden until their when-condition is met.

    Soft prompt rules are not enough — models occasionally call write tools
    as soon as gate/required slots look complete. Hiding the tool is the hard gate.
    """
    if not config:
        return set()
    blocked: set[str] = set()
    for action in resolve_required_actions(config):
        if not action.get("hard_gate"):
            continue
        tool = str(action.get("tool") or "").strip()
        if not tool:
            continue
        # before_mark_done tools stay visible; only mark_done is gated.
        if str(action.get("when") or "") == "before_mark_done":
            continue
        if not required_action_when_met(state, config, action):
            blocked.add(tool)
    return blocked


def _tool_callable_name(tool: Any) -> str:
    for attr in ("name", "tool_name"):
        value = getattr(tool, attr, None)
        if value is not None and str(value).strip():
            return str(value).strip()
    fn = getattr(tool, "entrypoint", None) or getattr(tool, "function", None)
    if fn is not None:
        nested = getattr(fn, "__name__", None) or getattr(fn, "name", None)
        if nested is not None and str(nested).strip():
            return str(nested).strip()
    return ""


def filter_collection_gated_tools(
    run_context: Any,
    tools: list[Any],
) -> list[Any]:
    """Drop hard-gated required_actions tools from the live tool list."""
    if not tools:
        return tools
    config = resolve_collection_config(workflow_from_run_context(run_context))
    if not config:
        return tools
    state = get_collection_state(run_context)
    blocked = hard_gated_tool_names(state, config)
    if not blocked:
        return tools
    kept: list[Any] = []
    for tool in tools:
        name = _tool_callable_name(tool)
        if name and name in blocked:
            logger.info(
                "collection hard-gate: hiding tool %s (phase=%s probe_done=%s missing=%s)",
                name,
                state.get("phase"),
                state.get("probe_done"),
                state.get("missing"),
            )
            continue
        kept.append(tool)
    return kept



def required_action_auto_mark_done(config: dict[str, Any] | None) -> bool:
    """Whether satisfying required MCP tools should auto ``completed=true``.

    Default true when any ``required_actions`` is configured; override with
    top-level ``auto_mark_done``.
    """
    if not config or not resolve_required_actions(config):
        return False
    if "auto_mark_done" in config:
        return _as_bool(config.get("auto_mark_done"), True)
    return True


def pending_required_action_tools(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    for_mark_done: bool = False,
) -> list[str]:
    """Return required MCP tool names not yet recorded in ``actions_done``.

    ``when=missing_empty`` applies once required fields are filled and probe
    finished. ``when=before_mark_done`` only gates mark_done.
    """
    current = ensure_collection_state(state, config)
    actions = resolve_required_actions(config)
    if not actions:
        return []
    done = current.get("actions_done") if isinstance(current.get("actions_done"), dict) else {}
    pending: list[str] = []
    for action in actions:
        tool = str(action.get("tool") or "").strip()
        if not tool or tool in done:
            continue
        if not required_action_when_met(
            current, config, action, for_mark_done=for_mark_done
        ):
            continue
        pending.append(tool)
    return pending


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
        "actions_done": {},
        # enrichment loop (config ``probe``)
        "probe_rounds": 0,
        "probe_notes": [],
        "probe_done": False,
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
        if isinstance(existing.get("actions_done"), dict):
            state["actions_done"] = {
                str(k): v
                for k, v in existing["actions_done"].items()
                if str(k).strip()
            }
        else:
            state["actions_done"] = {}
        phase = str(existing.get("phase") or PHASE_INIT).strip()
        state["phase"] = phase if phase in _VALID_PHASES else PHASE_INIT
        state["user_confirmed"] = _as_bool(existing.get("user_confirmed"), False)
        state["completed"] = _as_bool(existing.get("completed"), False)
        try:
            state["probe_rounds"] = max(0, int(existing.get("probe_rounds") or 0))
        except (TypeError, ValueError):
            state["probe_rounds"] = 0
        notes = existing.get("probe_notes")
        if isinstance(notes, list):
            state["probe_notes"] = [str(x).strip() for x in notes if str(x).strip()]
        else:
            state["probe_notes"] = []
        state["probe_done"] = _as_bool(existing.get("probe_done"), False)

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

    # Probe may start before all required are filled when gate_fields is set
    # (e.g. remaining required slots after gate_fields). Check probe before missing→collecting.
    if is_probe_active(out, config):
        out["phase"] = PHASE_PROBING
        return out

    if out["missing"]:
        out["phase"] = PHASE_COLLECTING
        return out

    # required filled, probe finished
    if confirm_required(config) and not out["user_confirmed"]:
        out["phase"] = PHASE_READY
        return out

    if out["completed"]:
        out["phase"] = PHASE_DONE
        return out

    out["phase"] = PHASE_CONFIRMED
    return out


def append_probe_note(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    note: str,
) -> dict[str, Any]:
    """Record one enrichment note and increment ``probe_rounds``."""
    out = ensure_collection_state(state, config)
    probe = resolve_probe_config(config)
    if not probe:
        raise ValueError("probe loop is not enabled in workflow")
    if not is_probe_ready(out, config):
        raise ValueError(
            "cannot probe until gate_fields (or all required fields) are filled"
        )
    text = str(note or "").strip()
    if not text:
        raise ValueError("probe note must be non-empty")
    notes = list(out.get("probe_notes") or [])
    notes.append(text)
    out["probe_notes"] = notes
    try:
        rounds = int(out.get("probe_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    out["probe_rounds"] = rounds + 1
    if out["probe_rounds"] >= int(probe.get("max_rounds") or 0):
        out["probe_done"] = True
    return advance_collection_phase(out, config)


def _probe_early_finish_allowed(
    reason: str | None,
    keywords: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """True when finish reason matches configured early_finish_keywords."""
    text = str(reason or "").strip().lower()
    if not text:
        return False
    keys = keywords if keywords is not None else _DEFAULT_PROBE_EARLY_FINISH_KEYWORDS
    return any(str(k).strip().lower() in text for k in keys if str(k).strip())


def finish_probe(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """End the enrichment loop early (skip remaining rounds)."""
    out = ensure_collection_state(state, config)
    probe = resolve_probe_config(config)
    if not probe:
        raise ValueError("probe loop is not enabled in workflow")
    if not is_probe_ready(out, config):
        raise ValueError(
            "cannot finish probe until gate_fields (or all required fields) are filled"
        )
    if not _as_bool(probe.get("allow_skip"), True) and not is_probe_finished(out, config):
        raise ValueError(
            "probe.allow_skip=false; continue until max_rounds via collection_probe_note"
        )
    try:
        rounds = int(out.get("probe_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    min_rounds = int(probe.get("min_rounds") or 0)
    if rounds < min_rounds and not _probe_early_finish_allowed(
        reason, probe.get("early_finish_keywords")
    ):
        raise ValueError(
            f"probe.min_rounds={min_rounds}; need more collection_probe_note "
            "or a reason matching probe.early_finish_keywords"
        )
    out["probe_done"] = True
    if reason and str(reason).strip():
        notes = list(out.get("probe_notes") or [])
        notes.append(f"[skip] {str(reason).strip()}")
        out["probe_notes"] = notes
    return advance_collection_phase(out, config)


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
    """Record that a write / update MCP succeeded. Allows early and repeated writes.

    Rejects when configured ``required_actions`` tools have not been recorded
    in ``actions_done`` yet.
    """
    out = ensure_collection_state(state, config)
    pending = pending_required_action_tools(out, config, for_mark_done=True)
    if pending:
        raise ValueError(
            "required actions not done before collection_mark_done: "
            + ", ".join(pending)
            + ". Call these tools successfully first (successes are tracked "
            "automatically from this turn's tool results)."
        )
    out["completed"] = True
    if result is not None:
        out["complete_result"] = (
            result if isinstance(result, (str, dict, list)) else str(result)
        )
    return advance_collection_phase(out, config)


def record_action_done(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    tool_name: str,
    *,
    detail: Any = None,
) -> dict[str, Any]:
    """Persist that ``tool_name`` succeeded (cluster-safe under collection state)."""
    out = ensure_collection_state(state, config)
    name = str(tool_name or "").strip()
    if not name:
        return out
    done = dict(out.get("actions_done") or {})
    entry: dict[str, Any] = {"ok": True}
    if detail is not None:
        entry["detail"] = (
            detail if isinstance(detail, (str, dict, list)) else str(detail)
        )
    done[name] = entry
    out["actions_done"] = done
    return advance_collection_phase(out, config)


def _tool_entry_name(entry: Any) -> str | None:
    if isinstance(entry, dict):
        name = entry.get("tool_name") or entry.get("name") or entry.get("tool")
    else:
        name = (
            getattr(entry, "tool_name", None)
            or getattr(entry, "name", None)
            or getattr(entry, "tool", None)
        )
    text = str(name or "").strip()
    return text or None


def _tool_entry_succeeded(entry: Any) -> bool:
    if isinstance(entry, dict):
        if entry.get("tool_call_error"):
            return False
        result = entry.get("result")
    else:
        if getattr(entry, "tool_call_error", None):
            return False
        result = getattr(entry, "result", None)
    if result is None:
        return True
    text = str(result).strip()
    if not text:
        return True
    # Agno/MCP often returns transport errors as plain text with tool_call_error=false.
    lower = text.lower()
    if lower.startswith("error from mcp tool"):
        return False
    if "serviceexception" in lower or "exception(" in lower:
        return False
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and data.get("ok") is False:
                return False
        except json.JSONDecodeError:
            pass
    return True


def extract_successful_tool_names(run_output: Any) -> list[str]:
    """Collect successful tool names from Agno tools list and/or messages.

    Mid-turn ``collection_mark_done`` often sees prior MCP results on
    ``run_context.messages`` before they appear on ``run_context.tools``.
    """
    if run_output is None:
        return []
    tools = getattr(run_output, "tools", None)
    messages = getattr(run_output, "messages", None)
    if isinstance(run_output, dict):
        if tools is None:
            tools = run_output.get("tools")
        if messages is None:
            messages = run_output.get("messages")
    names: list[str] = []
    seen: set[str] = set()

    def _add(entry: Any) -> None:
        if not _tool_entry_succeeded(entry):
            return
        name = _tool_entry_name(entry)
        if not name or name in seen:
            return
        seen.add(name)
        names.append(name)

    for entry in tools or []:
        _add(entry)
    for msg in messages or []:
        if isinstance(msg, dict):
            role = str(msg.get("role") or "").lower()
            tool_name = msg.get("tool_name") or msg.get("name")
            content = msg.get("content")
        else:
            role = str(getattr(msg, "role", "") or "").lower()
            tool_name = getattr(msg, "tool_name", None) or getattr(msg, "name", None)
            content = getattr(msg, "content", None)
        if role not in {"tool", "function"} and not tool_name:
            continue
        _add({"tool_name": tool_name, "result": content, "tool_call_error": False})
    return names


def apply_required_actions_from_run(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    run_output: Any,
) -> dict[str, Any]:
    """Record required MCP successes from this turn; optionally auto mark_done.

    Call from post_hook before session scrub so ``run_output.tools`` is still present.
    """
    out = ensure_collection_state(state, config)
    required = resolve_required_actions(config)
    if not required:
        return out
    required_names = {
        str(a.get("tool") or "").strip()
        for a in required
        if str(a.get("tool") or "").strip()
    }
    for name in extract_successful_tool_names(run_output):
        if name in required_names:
            out = record_action_done(out, config, name)
    pending = pending_required_action_tools(out, config)
    if (
        not pending
        and required_action_auto_mark_done(config)
        and not out.get("missing")
        and out.get("schema")
        and is_probe_finished(out, config)
        and not out.get("completed")
    ):
        out["completed"] = True
        out = advance_collection_phase(out, config)
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
    probe = resolve_probe_config(config)
    gated = sorted(hard_gated_tool_names(current, config))
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
        f"required_actions_pending: {json.dumps(pending_required_action_tools(current, config), ensure_ascii=False)}",
        f"write_tools_hard_gated: {json.dumps(gated, ensure_ascii=False)}",
        f"missing: {json.dumps(missing, ensure_ascii=False)}",
        f"collected: {json.dumps(current.get('collected') or {}, ensure_ascii=False)}",
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
            "Pick the single next ask with maximal information gain for the goal; "
            "ground it in the user's last answer + collected (do not ignore what they "
            "just said). Never re-ask facts already stated. Prefer short colloquial "
            "phrasing over checklist / form language. "
            "Optional brief empathy (<=1 short clause) then the question — no preamble lists.",
        ]
    )
    if probe and current.get("phase") == PHASE_PROBING:
        goal = str(probe.get("goal") or "").strip()
        goal_clause = (
            f"Follow probe_goal: {goal}. "
            if goal
            else "Follow agent instructions for enrichment purpose. "
        )
        lines.extend(
            [
                "5. PROBE LOOP (phase=probing): gate/required slots for probing are filled. "
                + goal_clause
                + "Choose the next question from dialogue + collected to close the "
                "largest remaining information gap for that goal — adaptive, not a "
                "fixed questionnaire. "
                "If probe_hints is non-empty, treat it as an optional dimension checklist: "
                "prefer unanswered dimensions; skip what is already clear; "
                "do not recite hints verbatim. "
                "If probe_hints is empty, rely on probe_goal + dialogue + collected. "
                "Ask exactly 1 atomic question per turn (see rule 4b). "
                "Prefer questions that best discriminate among remaining plausible "
                "paths for the goal, rather than generic completeness fishing. "
                "CRITICAL: after the user answers, you MUST call "
                "collection_probe_note(note=concise enrichment note: why this ask + "
                "key positives / pertinent negatives) in the SAME turn "
                "before ending — otherwise probe_rounds will not advance. "
                "Default: continue until probe_rounds reaches max_rounds. "
                "collection_probe_finish is blocked until probe_min_rounds notes "
                "unless reason matches probe.early_finish_keywords. "
                "Do not end early just because information 'seems enough'. "
                "Write/required_actions MCP tools are HARD-REMOVED while probing "
                "(see write_tools_hard_gated); do not invent a write call. "
                "Also forbidden: filling remaining decision/result slots that should "
                "wait until after probe, closing summary, collection_mark_done.",
            ]
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
    if write_tool or pending_required_action_tools(current, config):
        lines.append(
            f"{rule_base}. After a successful write/update MCP call, "
            "collection_mark_done(result=...). User may later add details — "
            "update fields / probe notes and write again."
        )
        rule_n = rule_base + 1
    else:
        rule_n = rule_base
    pending = pending_required_action_tools(current, config)
    if pending:
        lines.append(
            f"{rule_n}. REQUIRED before closing / ending this turn: call these tools "
            "successfully first (do not only reply with text): "
            + json.dumps(pending, ensure_ascii=False)
            + ". collection_mark_done is blocked until they succeed."
        )
        rule_n += 1
        # Cross-turn anti-repeat: once decision/result slots are filled, do not
        # re-emit the same patient-facing closing / recommendation block.
        collected = current.get("collected") or {}
        if isinstance(collected, dict) and collected and not (current.get("missing") or []):
            lines.append(
                f"{rule_n}. Anti-repeat: missing is empty and write tools are still "
                "pending. Call the pending tools first. Patient-visible reply must be "
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
    return "\n".join(lines)


def collection_status_payload(
    state: dict[str, Any],
    reply_text: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Structured progress for H5 / SSE metadata (prefer over parsing reply text).

    Generic payload includes ``collected`` plus optional keys projected from each
    schema field's ``exports`` (configured at publish time / via workflow hook).
    """
    collected = state.get("collected") if isinstance(state.get("collected"), dict) else {}
    schema = state.get("schema") if isinstance(state.get("schema"), list) else []
    actions_done = (
        state.get("actions_done") if isinstance(state.get("actions_done"), dict) else {}
    )
    pending = pending_required_action_tools(state, config) if config else []
    payload: dict[str, Any] = {
        "phase": state.get("phase"),
        "missing": list(state.get("missing") or []),
        "ready": not bool(state.get("missing"))
        and bool(schema)
        and is_probe_finished(state, config),
        "user_confirmed": bool(state.get("user_confirmed")),
        "completed": bool(state.get("completed")),
        "collected_keys": sorted(collected.keys()),
        "collected": dict(collected),
        "actions_done": sorted(actions_done.keys()),
        "required_actions_pending": pending,
        "probe_rounds": int(state.get("probe_rounds") or 0),
        "probe_done": bool(state.get("probe_done")),
        "probe_notes": list(state.get("probe_notes") or []),
    }
    exported = apply_schema_exports(schema, collected, reply_text=reply_text)
    payload.update(exported)
    return payload


def format_status_marker(
    state: dict[str, Any],
    reply_text: str | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    body = json.dumps(
        collection_status_payload(state, reply_text=reply_text, config=config),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{STATUS_MARKER_PREFIX} {body}{STATUS_MARKER_SUFFIX}"


def append_status_marker(
    content: str | None,
    state: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> str:
    text = str(content or "")
    marker = format_status_marker(state, reply_text=text, config=config)
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
        pending = pending_required_action_tools(state, config)
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
                "actions_done": sorted((state.get("actions_done") or {}).keys()),
                "required_actions_pending": pending,
                "probe_rounds": state.get("probe_rounds") or 0,
                "probe_done": bool(state.get("probe_done")),
                "probe_notes": list(state.get("probe_notes") or []),
            },
            ensure_ascii=False,
        )

    @tool(
        name="collection_probe_note",
        description=(
            "During phase=probing: record one enrichment note from the user's "
            "latest answer, then advance probe_rounds. Call once per answered "
            "probe question. When max_rounds is reached, probe ends."
        ),
    )
    def collection_probe_note(
        note: str,
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
            state = append_probe_note(get_collection_state(ctx), config, note)
            set_collection_state(ctx, state)
            return json.dumps(
                {
                    "ok": True,
                    "phase": state["phase"],
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
            "End the probing loop early (skip remaining rounds). Use only when "
            "reason matches workflow.probe.early_finish_keywords (e.g. user refuses). "
            "Blocked until probe_min_rounds notes unless reason matches those keywords. "
            "Also blocked when probe.allow_skip=false and max_rounds not yet reached. "
            "Do NOT use merely because information 'seems enough'."
        ),
    )
    def collection_probe_finish(
        reason: str = "",
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
            state = finish_probe(get_collection_state(ctx), config, reason=reason or None)
            set_collection_state(ctx, state)
            return json.dumps(
                {
                    "ok": True,
                    "phase": state["phase"],
                    "probe_done": True,
                    "probe_rounds": state.get("probe_rounds"),
                    "required_actions_pending": pending_required_action_tools(state, config),
                    "hint": (
                        "Probe finished; produce operator summary then call write MCP "
                        "if required_actions pending."
                        if not state.get("missing")
                        else "Probe finished but required fields still missing."
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
        "user_confirmed": bool(state.get("user_confirmed")),
        "completed": bool(state.get("completed")),
        "actions_done": sorted((state.get("actions_done") or {}).keys()),
        "probe_rounds": state.get("probe_rounds") or 0,
        "probe_done": bool(state.get("probe_done")),
    }

"""FSM core for collection_dialogue: state, probe loop, required actions."""
from __future__ import annotations

import json
import logging
from typing import Any

from agno_worker.tenant.collection.kinds.dialogue.config import (
    REQUIRED_ACTIONS_MODE_CONCURRENT,
    confirm_required,
    reply_action_fields,
    required_action_auto_mark_done,
    required_action_key,
    required_actions_mode,
    resolve_probe_config,
    resolve_required_actions,
)
from agno_worker.tenant.collection.kinds.dialogue.constants import (
    COLLECTION_STATE_KEY,
    PHASE_COLLECTING,
    PHASE_CONFIRMED,
    PHASE_DONE,
    PHASE_INIT,
    PHASE_PROBING,
    PHASE_READY,
    _VALID_PHASES,
)
from agno_worker.tenant.collection.kinds.dialogue.schema import (
    compute_missing,
    field_is_after_probe,
    normalize_schema_fields,
    resolve_field_probe,
    resolve_inline_schema_fields,
    schema_field_names,
    schema_source,
    _schema_field_by_name,
    _validate_field_value,
)
from agno_worker.tenant.collection.kinds.dialogue.util import as_bool
from agno_worker.tenant.collection.registry import resolve_collection_config

logger = logging.getLogger(__name__)

def _value_filled(collected: dict[str, Any], name: str) -> bool:
    value = collected.get(name)
    if value is None:
        return False
    if isinstance(value, str) and not str(value).strip():
        return False
    return True


def empty_field_probe_entry() -> dict[str, Any]:
    return {"rounds": 0, "notes": [], "done": False}


def pre_probe_field_names(
    schema: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> list[str]:
    """Required slot names that must be filled (+ field-probed) before global probe.

    Deferred slots: ``after_probe=true`` or referenced by ``required_actions`` type=reply.
    """
    deferred = reply_action_fields(config)
    names: list[str] = []
    for field in schema or []:
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        if not as_bool(field.get("required"), True):
            continue
        if field_is_after_probe(field) or name in deferred:
            continue
        names.append(name)
    return names


def deferred_required_fields(
    schema: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> set[str]:
    """Fields that may stay missing until their reply required_action is current."""
    deferred = set(reply_action_fields(config))
    for field in schema or []:
        name = str(field.get("name") or "").strip()
        if name and field_is_after_probe(field):
            deferred.add(name)
    return deferred


def _normalize_field_probe_entry(raw: Any) -> dict[str, Any]:
    entry = empty_field_probe_entry()
    if not isinstance(raw, dict):
        return entry
    try:
        entry["rounds"] = max(0, int(raw.get("rounds") or 0))
    except (TypeError, ValueError):
        entry["rounds"] = 0
    notes = raw.get("notes")
    if isinstance(notes, list):
        entry["notes"] = [str(x).strip() for x in notes if str(x).strip()]
    entry["done"] = as_bool(raw.get("done"), False)
    return entry


def ensure_field_probe_maps(state: dict[str, Any]) -> dict[str, Any]:
    """Normalize ``field_probes`` / ``field_probe_active`` on state."""
    out = dict(state)
    raw_map = out.get("field_probes")
    cleaned: dict[str, Any] = {}
    if isinstance(raw_map, dict):
        for key, value in raw_map.items():
            name = str(key).strip()
            if name:
                cleaned[name] = _normalize_field_probe_entry(value)
    out["field_probes"] = cleaned
    out["field_probe_active"] = str(out.get("field_probe_active") or "").strip()
    return out


def is_field_probe_finished(
    entry: dict[str, Any] | None,
    field_probe: dict[str, Any] | None,
) -> bool:
    if not field_probe:
        return True
    current = _normalize_field_probe_entry(entry)
    if current.get("done"):
        return True
    return int(current.get("rounds") or 0) >= int(field_probe.get("max_rounds") or 0)


def pending_field_probe_name(state: dict[str, Any]) -> str | None:
    """First filled schema field whose per-field probe is still open."""
    collected = state.get("collected") if isinstance(state.get("collected"), dict) else {}
    probes = state.get("field_probes") if isinstance(state.get("field_probes"), dict) else {}
    for field in state.get("schema") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if not name or not _value_filled(collected, name):
            continue
        field_probe = resolve_field_probe(field)
        if not field_probe:
            continue
        entry = probes.get(name)
        if not is_field_probe_finished(entry, field_probe):
            return name
    return None


def sync_field_probe_active(state: dict[str, Any]) -> dict[str, Any]:
    """Clear finished active field probe; activate next pending field probe."""
    out = ensure_field_probe_maps(state)
    active = out.get("field_probe_active") or ""
    if active:
        field = _schema_field_by_name(out.get("schema") or [], active)
        field_probe = resolve_field_probe(field)
        entry = (out.get("field_probes") or {}).get(active)
        if not field_probe or is_field_probe_finished(entry, field_probe):
            out["field_probe_active"] = ""
            active = ""
    if not active:
        pending = pending_field_probe_name(out)
        if pending:
            out["field_probe_active"] = pending
            probes = dict(out.get("field_probes") or {})
            probes.setdefault(pending, empty_field_probe_entry())
            out["field_probes"] = probes
    return out


def is_field_probe_active(state: dict[str, Any]) -> bool:
    return bool(str(state.get("field_probe_active") or "").strip())


def _pre_probe_slots_filled(state: dict[str, Any], field_names: list[str]) -> bool:
    collected = state.get("collected") if isinstance(state.get("collected"), dict) else {}
    for name in field_names:
        if not _value_filled(collected, name):
            return False
    return True


def _pre_probe_field_probes_done(state: dict[str, Any], field_names: list[str]) -> bool:
    schema = state.get("schema") or []
    probes = state.get("field_probes") if isinstance(state.get("field_probes"), dict) else {}
    for name in field_names:
        field = _schema_field_by_name(schema, name)
        field_probe = resolve_field_probe(field)
        if not field_probe:
            continue
        if not is_field_probe_finished(probes.get(name), field_probe):
            return False
    return True


def is_probe_ready(state: dict[str, Any], config: dict[str, Any] | None) -> bool:
    """Whether global enrichment may start (pre-probe slots + field probes done)."""
    probe = resolve_probe_config(config)
    if not probe:
        return False
    if is_field_probe_active(state):
        return False
    pre_names = pre_probe_field_names(state.get("schema") or [], config)
    if pre_names:
        if not _pre_probe_slots_filled(state, pre_names):
            return False
        return _pre_probe_field_probes_done(state, pre_names)
    # No pre-probe required slots: wait until non-deferred required slots are filled.
    deferred = deferred_required_fields(state.get("schema") or [], config)
    missing = [m for m in (state.get("missing") or []) if str(m) not in deferred]
    return not bool(missing)


def is_probe_finished(state: dict[str, Any], config: dict[str, Any] | None) -> bool:
    """True when global probe is disabled, skipped/finished, or max rounds reached."""
    probe = resolve_probe_config(config)
    if not probe:
        return True
    if as_bool(state.get("probe_done"), False):
        return True
    try:
        rounds = int(state.get("probe_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    return rounds >= int(probe.get("max_rounds") or 0)


def is_probe_active(state: dict[str, Any], config: dict[str, Any] | None) -> bool:
    """True while the global enrichment loop should run."""
    if not resolve_probe_config(config):
        return False
    if is_probe_finished(state, config):
        return False
    return is_probe_ready(state, config)


def required_action_pipeline_ready(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    for_mark_done: bool = False,
) -> bool:
    """Whether the required_actions serial pipeline may start."""
    current = ensure_collection_state(state, config)
    if not current.get("schema"):
        return False
    if is_field_probe_active(current):
        return False
    if not is_probe_finished(current, config):
        return False
    deferred = deferred_required_fields(current.get("schema") or [], config)
    blocking_missing = [
        m for m in (current.get("missing") or []) if str(m) not in deferred
    ]
    if blocking_missing and not for_mark_done:
        return False
    return True


def is_required_action_completed(
    state: dict[str, Any],
    action: dict[str, Any],
) -> bool:
    """Whether one required_actions step is satisfied."""
    key = required_action_key(action)
    if not key:
        return False
    done = state.get("actions_done") if isinstance(state.get("actions_done"), dict) else {}
    if key in done:
        return True
    if str(action.get("type") or "") == "reply":
        field = str(action.get("field") or "").strip()
        collected = state.get("collected") if isinstance(state.get("collected"), dict) else {}
        return bool(field) and _value_filled(collected, field)
    return False


def sync_reply_actions_done(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Persist reply-step completion into ``actions_done`` when fields are filled."""
    out = dict(state or {})
    done = dict(out.get("actions_done") or {}) if isinstance(out.get("actions_done"), dict) else {}
    changed = False
    for action in resolve_required_actions(config):
        if str(action.get("type") or "") != "reply":
            continue
        key = required_action_key(action)
        if not key or key in done:
            continue
        if is_required_action_completed(out, action):
            done[key] = {"ok": True, "type": "reply", "field": action.get("field")}
            changed = True
    if changed:
        out["actions_done"] = done
    return out


def current_required_action(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    for_mark_done: bool = False,
) -> dict[str, Any] | None:
    """First incomplete required_actions step in definition order (serial)."""
    if not required_action_pipeline_ready(state, config, for_mark_done=for_mark_done):
        return None
    current = ensure_collection_state(state, config)
    for action in resolve_required_actions(config):
        when = str(action.get("when") or "missing_empty")
        if when == "before_mark_done" and not for_mark_done:
            # before_mark_done steps only gate mark_done; skip for normal turns
            if is_required_action_completed(current, action):
                continue
            return None
        if is_required_action_completed(current, action):
            continue
        return action
    return None


def required_action_when_met(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    action: dict[str, Any],
    *,
    for_mark_done: bool = False,
) -> bool:
    """Whether a required_action may run / be recorded now."""
    when = str(action.get("when") or "missing_empty")
    if when == "before_mark_done":
        return bool(for_mark_done)
    if not required_action_pipeline_ready(state, config, for_mark_done=for_mark_done):
        return False
    if is_required_action_completed(state, action):
        return False
    mode = required_actions_mode(config)
    if mode == REQUIRED_ACTIONS_MODE_CONCURRENT:
        return True
    current = current_required_action(state, config, for_mark_done=for_mark_done)
    if not current:
        return False
    return required_action_key(current) == required_action_key(action)


def hard_gated_tool_names(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> set[str]:
    """MCP tool names that must be hidden until their turn / pipeline ready."""
    if not config:
        return set()
    blocked: set[str] = set()
    mode = required_actions_mode(config)
    pipeline_ready = required_action_pipeline_ready(state, config)
    current = current_required_action(state, config)
    current_key = required_action_key(current) if current else ""
    for action in resolve_required_actions(config):
        if str(action.get("type") or "") != "mcp":
            continue
        if not action.get("hard_gate"):
            continue
        tool = str(action.get("tool") or "").strip()
        if not tool:
            continue
        if str(action.get("when") or "") == "before_mark_done":
            continue
        if is_required_action_completed(state, action):
            continue
        if mode == REQUIRED_ACTIONS_MODE_CONCURRENT:
            # Concurrent: hide only until the whole pipeline may start; then all
            # incomplete MCP required_actions are visible in the same turn.
            if not pipeline_ready:
                blocked.add(tool)
            continue
        # Serial: only the current MCP step is visible; others stay hidden.
        if required_action_key(action) != current_key:
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


def pending_required_actions(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    for_mark_done: bool = False,
) -> list[dict[str, Any]]:
    """Return incomplete required_actions that are actionable now."""
    current = ensure_collection_state(state, config)
    actions = resolve_required_actions(config)
    if not actions:
        return []
    if not required_action_pipeline_ready(current, config, for_mark_done=for_mark_done):
        return []
    mode = required_actions_mode(config)
    if mode == REQUIRED_ACTIONS_MODE_CONCURRENT:
        pending: list[dict[str, Any]] = []
        for action in actions:
            when = str(action.get("when") or "missing_empty")
            if when == "before_mark_done" and not for_mark_done:
                continue
            if is_required_action_completed(current, action):
                continue
            pending.append(action)
        return pending

    pending = []
    seen_current = False
    active = current_required_action(current, config, for_mark_done=for_mark_done)
    active_key = required_action_key(active) if active else ""
    for action in actions:
        key = required_action_key(action)
        if is_required_action_completed(current, action):
            continue
        if not seen_current:
            if key != active_key:
                continue
            seen_current = True
        pending.append(action)
        # Serial: only expose the current step in prompts/status pending list.
        break
    return pending


def pending_required_action_tools(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    for_mark_done: bool = False,
) -> list[str]:
    """Return pending MCP tool names among actionable required_actions."""
    pending: list[str] = []
    for action in pending_required_actions(state, config, for_mark_done=for_mark_done):
        if str(action.get("type") or "") != "mcp":
            continue
        tool = str(action.get("tool") or "").strip()
        if tool:
            pending.append(tool)
    return pending


def append_probe_note(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    note: str,
    *,
    field: str | None = None,
) -> dict[str, Any]:
    """Record one enrichment note (per-field or global) and advance rounds."""
    out = ensure_collection_state(state, config)
    out = sync_field_probe_active(out)
    text = str(note or "").strip()
    if not text:
        raise ValueError("probe note must be non-empty")

    target = str(field or "").strip() or str(out.get("field_probe_active") or "").strip()
    if target:
        schema_field = _schema_field_by_name(out.get("schema") or [], target)
        field_probe = resolve_field_probe(schema_field)
        if not field_probe:
            raise ValueError(f"field probe is not enabled for {target!r}")
        collected = out.get("collected") if isinstance(out.get("collected"), dict) else {}
        if not _value_filled(collected, target):
            raise ValueError(f"cannot field-probe {target!r} before its value is collected")
        probes = dict(out.get("field_probes") or {})
        entry = _normalize_field_probe_entry(probes.get(target))
        if is_field_probe_finished(entry, field_probe):
            raise ValueError(f"field probe already finished for {target!r}")
        notes = list(entry.get("notes") or [])
        notes.append(text)
        entry["notes"] = notes
        entry["rounds"] = int(entry.get("rounds") or 0) + 1
        if entry["rounds"] >= int(field_probe.get("max_rounds") or 0):
            entry["done"] = True
        probes[target] = entry
        out["field_probes"] = probes
        out["field_probe_active"] = target
        out["probe_nudge_due"] = False
        return advance_collection_phase(out, config)

    probe = resolve_probe_config(config)
    if not probe:
        raise ValueError("probe loop is not enabled in workflow")
    if not is_probe_ready(out, config):
        raise ValueError(
            "cannot global-probe until pre-probe fields (and their field probes) are done"
        )
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
    out["probe_nudge_due"] = False
    return advance_collection_phase(out, config)


def finish_probe(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    reason: str | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    """End a field or global enrichment loop early (after min_rounds).

    Between min_rounds and max_rounds the model decides whether to finish.
    ``early_finish_keywords`` are no longer used.
    """
    out = ensure_collection_state(state, config)
    out = sync_field_probe_active(out)
    target = str(field or "").strip() or str(out.get("field_probe_active") or "").strip()

    if target:
        schema_field = _schema_field_by_name(out.get("schema") or [], target)
        field_probe = resolve_field_probe(schema_field)
        if not field_probe:
            raise ValueError(f"field probe is not enabled for {target!r}")
        probes = dict(out.get("field_probes") or {})
        entry = _normalize_field_probe_entry(probes.get(target))
        if is_field_probe_finished(entry, field_probe):
            out["field_probe_active"] = ""
            return advance_collection_phase(out, config)
        if not as_bool(field_probe.get("allow_skip"), True):
            raise ValueError(
                f"field probe allow_skip=false for {target!r}; continue until max_rounds"
            )
        rounds = int(entry.get("rounds") or 0)
        min_rounds = int(field_probe.get("min_rounds") or 0)
        if rounds < min_rounds:
            raise ValueError(
                f"field probe min_rounds={min_rounds} for {target!r}; "
                "need more collection_probe_note before finish"
            )
        entry["done"] = True
        if reason and str(reason).strip():
            notes = list(entry.get("notes") or [])
            notes.append(f"[skip] {str(reason).strip()}")
            entry["notes"] = notes
        probes[target] = entry
        out["field_probes"] = probes
        out["field_probe_active"] = ""
        out["probe_nudge_due"] = False
        return advance_collection_phase(out, config)

    probe = resolve_probe_config(config)
    if not probe:
        raise ValueError("probe loop is not enabled in workflow")
    if not is_probe_ready(out, config):
        raise ValueError(
            "cannot finish global probe until pre-probe fields (and their field probes) are done"
        )
    if not as_bool(probe.get("allow_skip"), True) and not is_probe_finished(out, config):
        raise ValueError(
            "probe.allow_skip=false; continue until max_rounds via collection_probe_note"
        )
    try:
        rounds = int(out.get("probe_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    min_rounds = int(probe.get("min_rounds") or 0)
    if rounds < min_rounds:
        raise ValueError(
            f"probe.min_rounds={min_rounds}; need more collection_probe_note "
            "before finish (model may end early only after min_rounds)"
        )
    out["probe_done"] = True
    if reason and str(reason).strip():
        notes = list(out.get("probe_notes") or [])
        notes.append(f"[skip] {str(reason).strip()}")
        out["probe_notes"] = notes
    out["probe_nudge_due"] = False
    return advance_collection_phase(out, config)


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
        # global enrichment loop (config ``probe``)
        "probe_rounds": 0,
        "probe_notes": [],
        "probe_done": False,
        # per-field enrichment (schema.fields[].probe)
        "field_probes": {},
        "field_probe_active": "",
        # workflow.scripts delivery flags (cluster-safe in session_state)
        "opening_delivered": False,
        "closing_delivered": False,
        # Internal-only: remind model next turn to call collection_probe_note
        "probe_nudge_due": False,
        # Internal-only: remind next turn after multi-ask / A-or-B / repeat-ask
        "ask_quality_nudge_due": False,
        "last_ask_text": "",
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
        state["user_confirmed"] = as_bool(existing.get("user_confirmed"), False)
        state["completed"] = as_bool(existing.get("completed"), False)
        try:
            state["probe_rounds"] = max(0, int(existing.get("probe_rounds") or 0))
        except (TypeError, ValueError):
            state["probe_rounds"] = 0
        notes = existing.get("probe_notes")
        if isinstance(notes, list):
            state["probe_notes"] = [str(x).strip() for x in notes if str(x).strip()]
        else:
            state["probe_notes"] = []
        state["probe_done"] = as_bool(existing.get("probe_done"), False)
        state = ensure_field_probe_maps(
            {
                **state,
                "field_probes": existing.get("field_probes"),
                "field_probe_active": existing.get("field_probe_active"),
            }
        )
        state["opening_delivered"] = as_bool(existing.get("opening_delivered"), False)
        state["closing_delivered"] = as_bool(existing.get("closing_delivered"), False)
        state["probe_nudge_due"] = as_bool(existing.get("probe_nudge_due"), False)
        state["ask_quality_nudge_due"] = as_bool(
            existing.get("ask_quality_nudge_due"), False
        )
        state["last_ask_text"] = str(existing.get("last_ask_text") or "").strip()
    else:
        state = ensure_field_probe_maps(state)

    # Inline schema is always owned by published agno_agent.workflow (SaaS-editable).
    if config and schema_source(config) == "inline":
        state = reconcile_inline_schema_from_config(state, config)
    elif state["schema"]:
        state["missing"] = compute_missing(state["schema"], state["collected"])
    return ensure_field_probe_maps(state)


def reconcile_inline_schema_from_config(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Adopt latest inline schema.fields; keep collected values only for still-valid keys."""
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
        out["phase"] = PHASE_INIT
    return out


def advance_collection_phase(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Deterministically advance phase from collected/missing/probe/confirm flags."""
    out = ensure_collection_state(state, config)
    if not out["schema"]:
        out["phase"] = PHASE_INIT
        return out

    out["missing"] = compute_missing(out["schema"], out["collected"])
    out = sync_reply_actions_done(out, config)
    out = sync_field_probe_active(out)

    # Per-field probe runs inside collecting (active field set on state).
    if is_field_probe_active(out):
        out["phase"] = PHASE_COLLECTING
        return out

    # Global probe may start before after_probe / reply-action slots are filled.
    if is_probe_active(out, config):
        out["phase"] = PHASE_PROBING
        return out

    if out["missing"]:
        out["phase"] = PHASE_COLLECTING
        return out

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
        if key in metadata and as_bool(metadata.get(key), False):
            return True

    headers = metadata.get("request_headers")
    if not isinstance(headers, dict):
        return False
    lower = {str(k).lower(): v for k, v in headers.items()}
    for key in ("x-collection-confirm", "x-confirm", "collection-confirm"):
        if key in headers and as_bool(headers.get(key), False):
            return True
        if key.lower() in lower and as_bool(lower[key.lower()], False):
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

    # Hard-gate deferred / after_probe decision slots until global probe finishes
    # (and until any active field probe ends). Soft prompt alone is insufficient.
    deferred = deferred_required_fields(out["schema"], config)
    probe_cfg = resolve_probe_config(config)
    probe_blocked = bool(probe_cfg) and (
        is_field_probe_active(out) or not is_probe_finished(out, config)
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
            continue
        if probe_blocked and name in deferred:
            raise ValueError(
                f"field {name!r} is after_probe / deferred; "
                "finish probe (probe_done) before collection_update_fields"
            )
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


def incomplete_required_actions(
    state: dict[str, Any],
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """All required_actions steps that are not yet satisfied (definition order)."""
    current = ensure_collection_state(state, config)
    current = sync_reply_actions_done(current, config)
    return [
        action
        for action in resolve_required_actions(config)
        if not is_required_action_completed(current, action)
    ]


def mark_collection_done(
    state: dict[str, Any],
    config: dict[str, Any] | None,
    result: Any = None,
) -> dict[str, Any]:
    """Record that a write / update MCP succeeded. Allows early and repeated writes.

    Rejects while any required_actions step (reply or mcp) is still incomplete.
    """
    out = ensure_collection_state(state, config)
    out = sync_reply_actions_done(out, config)
    pending = incomplete_required_actions(out, config)
    if pending:
        labels = []
        for action in pending:
            key = required_action_key(action) or str(action.get("type"))
            labels.append(key)
        raise ValueError(
            "required actions not done before collection_mark_done: "
            + ", ".join(labels)
            + ". Complete reply/MCP steps in order first."
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
    Reply steps are auto-marked when their schema field is filled.
    """
    out = ensure_collection_state(state, config)
    out = sync_reply_actions_done(out, config)
    required = resolve_required_actions(config)
    if not required:
        return out
    tool_to_action = {
        str(a.get("tool") or "").strip(): a
        for a in required
        if str(a.get("type") or "") == "mcp" and str(a.get("tool") or "").strip()
    }
    for name in extract_successful_tool_names(run_output):
        action = tool_to_action.get(name)
        if not action:
            continue
        # Serial gate: ignore out-of-order MCP successes until their turn.
        if not required_action_when_met(out, config, action):
            logger.info(
                "collection required_actions: ignore out-of-order mcp success %s "
                "(current=%s)",
                name,
                required_action_key(current_required_action(out, config)),
            )
            continue
        out = record_action_done(out, config, name)
        out = sync_reply_actions_done(out, config)
    pending = incomplete_required_actions(out, config)
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


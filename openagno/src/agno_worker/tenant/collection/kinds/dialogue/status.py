"""Collection status payload and HTML marker helpers."""
from __future__ import annotations

import json
from typing import Any

from agno_worker.tenant.collection.kinds.dialogue.constants import (
    STATUS_MARKER_PREFIX,
    STATUS_MARKER_SUFFIX,
)
from agno_worker.tenant.collection.kinds.dialogue.config import required_actions_mode
from agno_worker.tenant.collection.kinds.dialogue.core import (
    FIELD_STAGE_PROBE,
    current_required_action,
    field_stage,
    is_probe_finished,
    later_collectable_fields,
    pending_required_actions,
    pending_required_action_tools,
)
from agno_worker.tenant.collection.kinds.dialogue.schema import apply_schema_exports

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
    pending_actions = pending_required_actions(state, config) if config else []
    active = current_required_action(state, config) if config else None
    cursor = str(state.get("current_field") or "").strip()
    stage = field_stage(state)
    payload: dict[str, Any] = {
        "phase": state.get("phase"),
        "missing": list(state.get("missing") or []),
        "current_field": cursor,
        "field_stage": stage,
        "ask_focus": [cursor] if cursor else [],
        "later_fields": later_collectable_fields(state, config) if config else [],
        "pending_collected": dict(state.get("pending_collected") or {})
        if isinstance(state.get("pending_collected"), dict)
        else {},
        "ready": not bool(state.get("missing"))
        and bool(schema)
        and is_probe_finished(state, config)
        and stage != FIELD_STAGE_PROBE
        and not cursor,
        "user_confirmed": bool(state.get("user_confirmed")),
        "completed": bool(state.get("completed")),
        "collected_keys": sorted(collected.keys()),
        "collected": dict(collected),
        "actions_done": sorted(actions_done.keys()),
        "required_actions_mode": required_actions_mode(config) if config else "serial",
        "required_actions_pending": pending_actions,
        "required_action_current": active,
        "required_actions_pending_tools": pending,
        "probe_rounds": int(state.get("probe_rounds") or 0),
        "probe_done": bool(state.get("probe_done")),
        "probe_notes": list(state.get("probe_notes") or []),
        # Alias: enrichment probe is post-required only (not a global round budget).
        "enrichment_probe_rounds": int(state.get("probe_rounds") or 0),
        "enrichment_probe_done": bool(state.get("probe_done")),
        "field_probe_active": str(state.get("field_probe_active") or ""),
        "field_probes": dict(state.get("field_probes") or {})
        if isinstance(state.get("field_probes"), dict)
        else {},
        # Alias of field_stage == "probe" (derived mirror for older clients).
        "field_probe_busy": stage == FIELD_STAGE_PROBE,
    }
    exported = apply_schema_exports(
        schema, collected, reply_text=reply_text, config=config
    )
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
        # Refresh marker so late-parsed schema exports from the reply are visible to clients.
        prefix, _, _tail = text.partition(STATUS_MARKER_PREFIX)
        return f"{prefix.rstrip()}\n\n{marker}" if prefix.strip() else marker
    if text.strip():
        return f"{text.rstrip()}\n\n{marker}"
    return marker


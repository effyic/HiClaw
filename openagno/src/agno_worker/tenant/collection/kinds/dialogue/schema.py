"""Schema normalize / validate / export helpers."""
from __future__ import annotations

import logging
import re
from typing import Any

from agno_worker.tenant.collection.kinds.dialogue.util import as_bool

logger = logging.getLogger(__name__)

# Shared constraint keys for schema.fields[] and required_actions type=reply.
SLOT_CONSTRAINT_KEYS = ("goal", "pattern", "pattern_message", "exports")


def extract_slot_constraints(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Pull goal / pattern / pattern_message / exports from a field or reply action.

    Used by both ``schema.fields[]`` and ``required_actions`` type=reply so
    validation / prompt / H5 export share one shape.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    goal = str(raw.get("goal") or "").strip()
    if goal:
        out["goal"] = goal
    pattern = str(raw.get("pattern") or "").strip()
    if pattern:
        out["pattern"] = pattern
    pattern_message = str(raw.get("pattern_message") or "").strip()
    if pattern_message:
        out["pattern_message"] = pattern_message
    exports = raw.get("exports")
    if isinstance(exports, dict) and exports:
        cleaned = {str(k): v for k, v in exports.items() if str(k).strip()}
        if cleaned:
            out["exports"] = cleaned
    return out


def merge_slot_constraints(*sources: dict[str, Any] | None) -> dict[str, Any]:
    """Merge constraint dicts; later non-empty values win (exports merge by key)."""
    out: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict) or not source:
            continue
        for key in ("goal", "pattern", "pattern_message"):
            value = str(source.get(key) or "").strip()
            if value:
                out[key] = value
        exports = source.get("exports")
        if isinstance(exports, dict) and exports:
            merged_exports = dict(out.get("exports") or {})
            for export_key, spec in exports.items():
                name = str(export_key).strip()
                if name:
                    merged_exports[name] = spec
            if merged_exports:
                out["exports"] = merged_exports
    return out


def apply_slot_constraints(target: dict[str, Any], raw: dict[str, Any] | None) -> dict[str, Any]:
    """Copy extracted constraints onto ``target`` (mutates and returns it)."""
    target.update(extract_slot_constraints(raw))
    return target


def normalize_schema_fields(raw_fields: Any) -> list[dict[str, Any]]:
    """Normalize schema fields; preserve generic constraints (pattern / exports).

    Domain-specific slots and exports belong in published
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
            "required": as_bool(item.get("required"), True),
        }
        apply_slot_constraints(field, item)
        field_probe = normalize_field_probe(item.get("probe"))
        if field_probe:
            field["probe"] = field_probe
        fields.append(field)
    return fields


def normalize_field_probe(raw: Any) -> dict[str, Any] | None:
    """Optional per-field enrichment after the slot value is collected.

    Published under ``schema.fields[].probe``::

        {
          "enabled": true,
          "min_rounds": 0,
          "max_rounds": 2,
          "goal": "optional field-specific purpose",
          "allow_skip": true
        }

    These min/max bound **this field only** (while ``field_stage=probe``).
    They are independent of ``workflow.probe.min_rounds/max_rounds``, which
    only govern the post-required enrichment loop (``phase=probing``).

    After ``min_rounds`` and before ``max_rounds``, the model may call
    ``collection_probe_finish(field=...)`` to end early. At ``max_rounds`` the
    field probe ends automatically via ``collection_probe_note``.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    if not as_bool(raw.get("enabled"), False):
        return None
    try:
        max_rounds = int(raw.get("max_rounds", 2))
    except (TypeError, ValueError):
        max_rounds = 2
    max_rounds = max(0, min(max_rounds, 8))
    try:
        min_rounds = int(raw.get("min_rounds", 0))
    except (TypeError, ValueError):
        min_rounds = 0
    min_rounds = max(0, min(min_rounds, max_rounds if max_rounds > 0 else 0))
    goal = str(raw.get("goal") or "").strip()
    return {
        "enabled": True,
        "max_rounds": max_rounds,
        "min_rounds": min_rounds,
        "goal": goal,
        "allow_skip": as_bool(raw.get("allow_skip"), True),
    }


def resolve_field_probe(field: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(field, dict):
        return None
    probe = field.get("probe")
    if isinstance(probe, dict) and probe.get("enabled"):
        return probe if "max_rounds" in probe else normalize_field_probe(probe)
    return normalize_field_probe(probe)


def field_ask_goal(field: dict[str, Any] | None) -> str:
    """Business ask guidance for a field def (probe.goal, else field.goal)."""
    if not isinstance(field, dict):
        return ""
    probe = field.get("probe") if isinstance(field.get("probe"), dict) else {}
    goal = str((probe or {}).get("goal") or "").strip()
    if goal:
        return goal
    return str(field.get("goal") or "").strip()


def resolve_field_def(
    schema: list[dict[str, Any]] | None,
    name: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Look up a field def with schema + reply constraints merged.

    Precedence for overlapping ``goal`` / ``pattern`` / ``exports``:
    schema fills first, then ``required_actions`` type=reply overlays
    (reply wins on conflict). Probe / required flags stay on the schema side.
    """
    schema_field = _schema_field_by_name(schema or [], name)
    reply_slot = _reply_slot_by_name(config, name)
    if not schema_field and not reply_slot:
        return None
    if schema_field and not reply_slot:
        return schema_field
    if reply_slot and not schema_field:
        return dict(reply_slot)
    merged = dict(schema_field)
    merged.update(
        merge_slot_constraints(
            extract_slot_constraints(schema_field),
            extract_slot_constraints(reply_slot),
        )
    )
    return merged


def iter_resolved_field_defs(
    schema: list[dict[str, Any]] | None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Ordered field defs for validate / export / missing / prompt.

    Schema collect fields first (merged with reply meta when same name), then
    reply-only result slots that are not already in schema.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field in schema or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        resolved = resolve_field_def([field], name, config)
        out.append(resolved if resolved is not None else field)
    if config:
        from agno_worker.tenant.collection.kinds.dialogue.config import reply_result_slots

        for slot in reply_result_slots(config):
            name = str(slot.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(dict(slot))
    return out


def writable_field_names(
    schema: list[dict[str, Any]] | None,
    config: dict[str, Any] | None = None,
) -> set[str]:
    """Names allowed in collection_update_fields (schema + reply result slots)."""
    names = set(schema_field_names(schema or []))
    if config:
        from agno_worker.tenant.collection.kinds.dialogue.config import reply_action_fields

        names |= reply_action_fields(config)
    return names


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


def _reply_slot_by_name(
    config: dict[str, Any] | None,
    name: str,
) -> dict[str, Any] | None:
    if not config or not name:
        return None
    from agno_worker.tenant.collection.kinds.dialogue.config import reply_result_slots

    for slot in reply_result_slots(config):
        if str(slot.get("name") or "").strip() == name:
            return slot
    return None


def _validate_field_value(
    schema: list[dict[str, Any]],
    name: str,
    text: str,
    config: dict[str, Any] | None = None,
) -> str:
    """Enforce optional per-field ``pattern`` from schema and/or reply action."""
    field = resolve_field_def(schema, name, config)
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
    config: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Project merged field ``exports`` into flat keys for H5 / metadata."""
    out: dict[str, str] = {}
    collected_map = collected if isinstance(collected, dict) else {}
    for field in iter_resolved_field_defs(schema, config):
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
    config: dict[str, Any] | None = None,
) -> list[str]:
    """Required schema fields + unfilled reply result slots (merged defs)."""
    missing: list[str] = []
    for field in iter_resolved_field_defs(schema, config):
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        if not as_bool(field.get("required"), True):
            continue
        value = collected.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


def schema_source(config: dict[str, Any] | None) -> str:
    if not config:
        return "inline"
    schema_cfg = config.get("schema")
    if not isinstance(schema_cfg, dict):
        return "inline"
    return str(schema_cfg.get("source") or "inline").strip() or "inline"


def schema_field_names(schema: list[dict[str, Any]]) -> set[str]:
    return {
        str(field.get("name") or "").strip()
        for field in schema
        if str(field.get("name") or "").strip()
    }

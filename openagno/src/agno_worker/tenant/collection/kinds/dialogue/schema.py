"""Schema normalize / validate / export helpers."""
from __future__ import annotations

import logging
import re
from typing import Any

from agno_worker.tenant.collection.kinds.dialogue.util import as_bool

logger = logging.getLogger(__name__)

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
            "required": as_bool(item.get("required"), True),
        }
        if as_bool(item.get("after_probe"), False):
            field["after_probe"] = True
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
        field_probe = normalize_field_probe(item.get("probe"))
        if field_probe:
            field["probe"] = field_probe
        fields.append(field)
    return fields


def normalize_field_probe(raw: Any) -> dict[str, Any] | None:
    """Optional per-field enrichment loop after the slot value is collected.

    Published under ``schema.fields[].probe``::

        {
          "enabled": true,
          "min_rounds": 0,
          "max_rounds": 2,
          "goal": "optional field-specific purpose",
          "allow_skip": true
        }

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


def field_is_after_probe(field: dict[str, Any] | None) -> bool:
    """True when the field should be collected only after the global probe."""
    if not isinstance(field, dict):
        return False
    return as_bool(field.get("after_probe"), False)


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

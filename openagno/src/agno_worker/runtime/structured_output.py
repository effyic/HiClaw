"""Helpers for per-run structured output (dynamic JSON Schema → Agno)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[^0-9A-Za-z_]+")


def normalize_output_schema(raw: Any) -> type[BaseModel] | dict[str, Any] | None:
    """Normalize request body ``output_schema`` for ``Agent.arun``.

    Accepts:
    - Plain JSON Schema object (``type=object`` + ``properties``) → dynamic Pydantic
    - Provider envelope (``type=json_schema``) → pass-through dict
    - Already a Pydantic model type → as-is
    """
    if raw is None:
        return None
    if isinstance(raw, type) and issubclass(raw, BaseModel):
        return raw
    if not isinstance(raw, dict) or not raw:
        return None

    # Provider-specific envelope: pass through unchanged.
    if str(raw.get("type") or "") == "json_schema" or "json_schema" in raw:
        return raw

    # Plain JSON Schema → Pydantic (best for native structured outputs).
    if "properties" in raw or str(raw.get("type") or "") == "object":
        try:
            return json_object_schema_to_model(raw)
        except Exception as exc:
            logger.warning(
                "Failed to convert JSON schema to Pydantic, using dict: %s",
                exc,
            )
            return raw
    return raw


def json_object_schema_to_model(schema: dict[str, Any]) -> type[BaseModel]:
    """Build a Pydantic model from a JSON Schema object (string fields preferred)."""
    title = str(schema.get("title") or "DynamicStructuredOutput")
    model_name = _SAFE_NAME.sub("_", title).strip("_") or "DynamicStructuredOutput"
    if model_name[0].isdigit():
        model_name = f"M_{model_name}"

    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    required = {
        str(item)
        for item in (schema.get("required") or [])
        if str(item).strip()
    }

    field_definitions: dict[str, Any] = {}
    for raw_name, prop in properties.items():
        name = str(raw_name).strip()
        if not name:
            continue
        prop_dict = prop if isinstance(prop, dict) else {}
        description = str(prop_dict.get("description") or "").strip()
        json_type = str(prop_dict.get("type") or "string").lower()
        py_type: type[Any]
        if json_type in {"integer", "int"}:
            py_type = int
        elif json_type in {"number", "float"}:
            py_type = float
        elif json_type in {"boolean", "bool"}:
            py_type = bool
        elif json_type == "array":
            py_type = list
        elif json_type == "object":
            py_type = dict
        else:
            py_type = str

        if name in required:
            field_definitions[name] = (
                py_type,
                Field(..., description=description or None),
            )
        else:
            default: Any = "" if py_type is str else None
            field_definitions[name] = (
                py_type,
                Field(default=default, description=description or None),
            )

    if not field_definitions:
        return create_model(model_name)  # type: ignore[call-overload]
    return create_model(model_name, **field_definitions)  # type: ignore[call-overload]


def content_to_reply_text(content: Any) -> str:
    """Serialize run content to a stable text reply (JSON for structured)."""
    if content is None:
        return ""
    if isinstance(content, BaseModel):
        return content.model_dump_json(ensure_ascii=False)
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, (int, float, bool)):
        return json.dumps(content, ensure_ascii=False)
    return str(content)

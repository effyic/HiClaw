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
        default: Any = ... if name in required else ""
        if py_type is list:
            default = ... if name in required else []
        elif py_type is dict:
            default = ... if name in required else {}
        elif py_type is bool:
            default = ... if name in required else False
        elif py_type in {int, float}:
            default = ... if name in required else 0
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


def join_content_segments(
    segments: list[str],
    *,
    tools_intervened: bool = False,
) -> str:
    """Join assistant content segments for one agent run (domain-agnostic).

    Agno may emit text both before and after tool calls. When tools intervened
    and there is more than one non-empty segment, keep the **last** segment —
    that is the post-tool user-facing reply. No domain keywords are consulted.

    Token/delta chunks within one segment must already be joined by the caller
    before being passed as a segment.
    """
    cleaned = [str(s).strip() for s in segments if s is not None and str(s).strip()]
    if not cleaned:
        return ""
    if tools_intervened:
        return collapse_tool_turn_echo(cleaned[-1], tools_intervened=True)
    return "".join(cleaned)


def collapse_duplicate_paragraphs(text: str) -> str:
    """Keep the last occurrence of each paragraph (exact or near-duplicate).

    Near-duplicates (SequenceMatcher ratio >= 0.85) are treated as the same
    paragraph so pre/post-tool closings with slight rephrasing collapse.
    """
    from difflib import SequenceMatcher

    paras = [p.strip() for p in re.split(r"\n\s*\n", str(text or "")) if p and str(p).strip()]
    if not paras:
        return str(text or "").strip()

    def is_same(a: str, b: str) -> bool:
        if a == b:
            return True
        threshold = 0.85 if min(len(a), len(b)) < 40 else 0.82
        return SequenceMatcher(None, a, b).ratio() >= threshold

    kept: list[str] = []
    for para in reversed(paras):
        if any(is_same(para, prev) for prev in kept):
            continue
        kept.append(para)
    kept.reverse()
    return "\n\n".join(kept)


def collapse_tool_turn_echo(text: str, *, tools_intervened: bool = False) -> str:
    """Collapse near-duplicate paragraphs when a tool turn echoed its closing.

    Agno may concatenate pre-tool and post-tool speech into one assistant
    ``content`` blob. When tools intervened, drop earlier near-duplicate
    paragraphs and keep the later ones. Structural only — no domain keywords.
    """
    raw = str(text or "").strip()
    if not raw:
        return raw
    if not tools_intervened:
        return raw
    return collapse_duplicate_paragraphs(raw)


def prefer_last_assistant_after_tools(run_output: Any) -> str | None:
    """If this run called tools, return the last assistant text message (if any).

    Used on sync paths where ``content`` may already concatenate pre/post tool
    speech. Structural only — no domain string matching.
    """
    if run_output is None:
        return None
    tools = getattr(run_output, "tools", None)
    has_tools = False
    if isinstance(tools, list) and tools:
        has_tools = True
    messages = getattr(run_output, "messages", None)
    if not isinstance(messages, list) or not messages:
        return None
    if not has_tools:
        # Still detect tool-role messages in the transcript.
        for msg in messages:
            role = str(getattr(msg, "role", None) or "").lower()
            if role == "tool":
                has_tools = True
                break
            if getattr(msg, "tool_calls", None) or getattr(msg, "tool_args", None):
                has_tools = True
                break
    if not has_tools:
        return None
    last_text = None
    for msg in messages:
        role = str(getattr(msg, "role", None) or "").lower()
        if role not in {"assistant", "model"}:
            continue
        content = getattr(msg, "content", None)
        if content is None:
            continue
        text = content_to_reply_text(content).strip()
        if text:
            last_text = text
    if last_text is None:
        return None
    return collapse_tool_turn_echo(last_text, tools_intervened=True)

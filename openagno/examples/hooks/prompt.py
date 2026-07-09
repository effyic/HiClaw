"""Prompt hooks — system prompt, instructions, and knowledge filters."""
from __future__ import annotations

from typing import Any


def get_system_prompt_hook(run_context: Any, session_state: dict[str, Any]) -> str:
    del run_context, session_state
    return ""


def get_instructions_hook(run_context: Any, user_profile: dict[str, Any]) -> str:
    del run_context, user_profile
    return ""


def get_context_filter_hook(run_context: Any) -> dict[str, Any]:
    metadata = getattr(run_context, "metadata", None) or {}
    tenant_id = str(metadata.get("tenant_id") or "")
    return {
        "tenant_id": tenant_id,
        "provider": "weknora",
        "knowledge_ids": [],
    }

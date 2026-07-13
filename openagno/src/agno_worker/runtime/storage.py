"""Per-request session storage scrubbing controlled by x-debug-request header."""
from __future__ import annotations

from typing import Any

from agno_worker.api.identity import default_debug_request

# Persisted on session_state for scrub lookup; stripped before save in slim mode.
DEBUG_REQUEST_STATE_KEY = "_debug_request"

# Business session_state keys kept in slim storage mode.
SLIM_SESSION_STATE_KEYS = frozenset(
    {
        "tenant_id",
        "active_role",
        "role_code",
        "phase",
        "workflow",
        "session_id",
    }
)

# Message fields kept in slim storage mode.
SLIM_MESSAGE_FIELDS = frozenset(
    {
        "id",
        "role",
        "content",
        "reasoning_content",
        "redacted_reasoning_content",
        "created_at",
        "name",
    }
)

_MESSAGE_FIELDS_TO_CLEAR = (
    "tool_calls",
    "tool_call_id",
    "tool_name",
    "tool_args",
    "tool_call_error",
    "metrics",
    "audio",
    "images",
    "videos",
    "files",
    "audio_output",
    "image_output",
    "video_output",
    "file_output",
    "provider_data",
    "citations",
    "references",
    "compressed_content",
    "from_history",
    "temporary",
    "stop_after_tool_call",
)


def slim_session_state(session_state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(session_state, dict):
        return {}
    return {
        key: value
        for key, value in session_state.items()
        if key in SLIM_SESSION_STATE_KEYS and not str(key).startswith("_")
    }


def is_debug_storage(run_response: Any) -> bool:
    """Read per-run debug flag from session_state or metadata."""
    session_state = getattr(run_response, "session_state", None) or {}
    if isinstance(session_state, dict) and DEBUG_REQUEST_STATE_KEY in session_state:
        return bool(session_state[DEBUG_REQUEST_STATE_KEY])

    metadata = getattr(run_response, "metadata", None) or {}
    if isinstance(metadata, dict) and "debug_request" in metadata:
        return bool(metadata["debug_request"])

    return default_debug_request()


def apply_slim_storage_scrub(agent: Any, run_response: Any) -> None:
    """Scrub run output before persistence when debug storage is disabled."""
    from agno.utils.agent import (
        scrub_history_messages_from_run_output,
        scrub_media_from_run_output,
        scrub_tool_results_from_run_output,
    )

    scrub_media_from_run_output(run_response)
    scrub_tool_results_from_run_output(run_response)
    scrub_history_messages_from_run_output(run_response)

    _scrub_run_fields(run_response)
    _scrub_messages(run_response)
    _scrub_session_state(run_response)


def _scrub_run_fields(run_response: Any) -> None:
    for attr in (
        "metrics",
        "events",
        "tools",
        "images",
        "videos",
        "audio",
        "files",
        "response_audio",
        "additional_input",
        "reasoning_messages",
        "model_provider_data",
        "citations",
        "references",
        "followups",
        "requirements",
    ):
        if hasattr(run_response, attr):
            setattr(run_response, attr, None)


def _scrub_messages(run_response: Any) -> None:
    messages = getattr(run_response, "messages", None)
    if not messages:
        return

    kept = []
    for message in messages:
        role = getattr(message, "role", None)
        if role not in {"user", "assistant"}:
            continue
        kept.append(_slim_message(message))
    run_response.messages = kept or None


def _slim_message(message: Any) -> Any:
    model_construct = getattr(type(message), "model_construct", None)
    if callable(model_construct):
        data = {
            field: getattr(message, field)
            for field in SLIM_MESSAGE_FIELDS
            if getattr(message, field, None) is not None
        }
        return model_construct(**data)

    for field in _MESSAGE_FIELDS_TO_CLEAR:
        if hasattr(message, field):
            setattr(message, field, None)
    return message


def _scrub_session_state(run_response: Any) -> None:
    session_state = getattr(run_response, "session_state", None)
    if not isinstance(session_state, dict):
        return

    run_response.session_state = slim_session_state(session_state)


def sync_debug_request_to_session_state(run_context: Any, debug_request: bool) -> None:
    """Propagate debug flag into run_context for storage scrub lookup."""
    if run_context.session_state is None:
        run_context.session_state = {}
    run_context.session_state[DEBUG_REQUEST_STATE_KEY] = debug_request

    metadata = getattr(run_context, "metadata", None)
    if metadata is None:
        run_context.metadata = {"debug_request": debug_request}
    elif isinstance(metadata, dict):
        metadata["debug_request"] = debug_request

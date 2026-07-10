"""Optional request pre/post filter extension hooks."""
from __future__ import annotations

from typing import Any

from agno_worker.hooks.protocols import UserContext


def request_pre_filter_hook(
    user_context: UserContext,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """Return modified metadata, or {"allowed": False, "reason": "..."} to reject."""
    del user_context
    return None


def request_post_filter_hook(
    user_context: UserContext,
    run_output: dict[str, Any],
    run_context: Any = None,
) -> dict[str, Any] | None:
    """Return modified run_output (reply, session_id)."""
    del user_context, run_output, run_context
    return None

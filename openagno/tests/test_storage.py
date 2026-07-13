from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from agno_worker.api.identity import default_debug_request, resolve_debug_request
from agno_worker.runtime.storage import (
    apply_slim_storage_scrub,
    is_debug_storage,
    sync_debug_request_to_session_state,
)


def test_resolve_debug_request_defaults_to_slim_storage() -> None:
    assert resolve_debug_request({}) is False
    assert resolve_debug_request(None) is False
    assert default_debug_request() is False


def test_resolve_debug_request_env_default_true() -> None:
    import os

    prev = os.environ.get("AGNO_DEBUG_REQUEST_DEFAULT")
    os.environ["AGNO_DEBUG_REQUEST_DEFAULT"] = "true"
    try:
        assert resolve_debug_request({}) is True
        assert default_debug_request() is True
    finally:
        if prev is None:
            os.environ.pop("AGNO_DEBUG_REQUEST_DEFAULT", None)
        else:
            os.environ["AGNO_DEBUG_REQUEST_DEFAULT"] = prev


def test_resolve_debug_request_false_enables_slim_storage() -> None:
    assert resolve_debug_request({"x-debug-request": "false"}) is False
    assert resolve_debug_request({"X-Debug-Request": "0"}) is False
    assert resolve_debug_request({"x-debug-requet": "false"}) is False


def test_resolve_debug_request_true_keeps_full_storage() -> None:
    assert resolve_debug_request({"x-debug-request": "true"}) is True
    assert resolve_debug_request({"x-debug-request": "1"}) is True


def test_is_debug_storage_reads_session_state() -> None:
    run = SimpleNamespace(
        session_state={"_debug_request": False},
        metadata={},
    )
    assert is_debug_storage(run) is False


def test_sync_debug_request_to_session_state() -> None:
    ctx = SimpleNamespace(session_state={}, metadata={})
    sync_debug_request_to_session_state(ctx, False)
    assert ctx.session_state["_debug_request"] is False
    assert ctx.metadata["debug_request"] is False


def test_apply_slim_storage_scrub_keeps_user_assistant_and_reasoning() -> None:
    user = SimpleNamespace(
        role="user",
        content="hello",
        created_at=1,
        metrics=SimpleNamespace(input_tokens=10),
    )
    assistant = SimpleNamespace(
        role="assistant",
        content="hi",
        reasoning_content="thinking",
        created_at=2,
        tool_calls=[{"id": "t1"}],
        metrics=SimpleNamespace(output_tokens=5),
    )
    system = SimpleNamespace(role="system", content="prompt")
    tool = SimpleNamespace(role="tool", content="{}", tool_call_id="t1")

    run = SimpleNamespace(
        messages=[system, user, tool, assistant],
        metrics=SimpleNamespace(total_tokens=15),
        events=[{"event": "run_started"}],
        tools=[{"name": "search"}],
        session_state={
            "tenant_id": "t1",
            "phase": "triage",
            "_debug_request": False,
            "_tenant_run_cache": {"secret": True},
        },
        metadata={"debug_request": False},
    )

    with (
        patch("agno.utils.agent.scrub_media_from_run_output"),
        patch("agno.utils.agent.scrub_tool_results_from_run_output"),
        patch("agno.utils.agent.scrub_history_messages_from_run_output"),
    ):
        apply_slim_storage_scrub(SimpleNamespace(), run)

    assert [msg.role for msg in run.messages] == ["user", "assistant"]
    assert run.messages[1].reasoning_content == "thinking"
    assert not hasattr(run.messages[0], "metrics") or run.messages[0].metrics is None
    assert run.metrics is None
    assert run.events is None
    assert run.tools is None
    assert run.session_state == {"tenant_id": "t1", "phase": "triage"}

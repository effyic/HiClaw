"""Tests for request filter pipeline."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agno_worker.hooks.filters import RequestFilterPipeline, RequestRejectedError
from agno_worker.hooks.protocols import UserContext
from agno_worker.hooks.registry import HookRegistry


def test_pre_filter_passes_through_without_hook(tmp_path: Path) -> None:
    registry = HookRegistry(tmp_path / "missing")
    pipeline = RequestFilterPipeline(registry)
    meta = pipeline.apply_pre_filter(UserContext(user_id="u1", tenant_id="t1"), {"k": "v"})
    assert meta == {"k": "v"}


def test_pre_filter_can_reject_request(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "filters.py").write_text(
        textwrap.dedent(
            """
            def request_pre_filter_hook(user_context, metadata):
                if not user_context.user_id:
                    return {"allowed": False, "reason": "missing user"}
                return {"metadata": {"verified": True}}
            """
        ),
        encoding="utf-8",
    )
    registry = HookRegistry(hooks_dir)
    pipeline = RequestFilterPipeline(registry)

    with pytest.raises(RequestRejectedError):
        pipeline.apply_pre_filter(UserContext(tenant_id="t1"), {})

    meta = pipeline.apply_pre_filter(UserContext(user_id="u1", tenant_id="t1"), {})
    assert meta.get("verified") is True


def test_post_filter_modifies_output(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "filters.py").write_text(
        "def request_post_filter_hook(user_context, run_output, run_context=None):\n"
        "    return {'reply': run_output['reply'] + '!'}",
        encoding="utf-8",
    )
    registry = HookRegistry(hooks_dir)
    pipeline = RequestFilterPipeline(registry)
    out = pipeline.apply_post_filter(
        UserContext(user_id="u1"),
        {"reply": "hello", "session_id": "s1"},
    )
    assert out["reply"] == "hello!"

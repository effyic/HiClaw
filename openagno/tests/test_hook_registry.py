"""Tests for optional extension hook registry."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agno_worker.hooks.registry import HookRegistry


def test_registry_optional_when_directory_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no_hooks"
    registry = HookRegistry(missing)
    assert not registry.has("transform_prompt_hook")
    assert registry.hooks.hooks == {}


def test_registry_loads_extension_hooks(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "transform.py").write_text(
        textwrap.dedent(
            """
            def transform_prompt_hook(run_context, prompt_bundle):
                prompt_bundle = dict(prompt_bundle)
                prompt_bundle["system_prompt"] = prompt_bundle.get("system_prompt", "") + " [hook]"
                return prompt_bundle
            """
        ),
        encoding="utf-8",
    )
    registry = HookRegistry(hooks_dir)
    assert registry.has("transform_prompt_hook")
    result = registry.call(
        "transform_prompt_hook",
        None,
        {"system_prompt": "base", "instructions": "instr"},
    )
    assert "[hook]" in result["system_prompt"]


def test_registry_wraps_execution_errors(tmp_path: Path) -> None:
    from agno_worker.hooks.errors import HookExecutionError

    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "filters.py").write_text(
        "def request_pre_filter_hook(user_context, metadata):\n    raise ValueError('boom')\n",
        encoding="utf-8",
    )
    registry = HookRegistry(hooks_dir)
    with pytest.raises(HookExecutionError):
        registry.call("request_pre_filter_hook", None, {})

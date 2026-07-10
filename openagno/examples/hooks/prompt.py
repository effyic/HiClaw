"""Legacy example file — prompt is now built by tenant pipeline.

Use transform_prompt_hook in transform.py for secondary customization.
"""
from __future__ import annotations

from typing import Any


def transform_prompt_hook(run_context: Any, prompt_bundle: dict[str, Any]) -> dict[str, Any] | None:
    del run_context
    return None

"""Legacy example file — skills are now resolved by tenant pipeline.

Use transform_skills_hook in transform.py for secondary customization.
"""
from __future__ import annotations

from typing import Any


def transform_skills_hook(run_context: Any, catalog: list[Any]) -> list[Any] | None:
    del run_context, catalog
    return None

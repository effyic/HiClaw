"""Skills hooks — optional domain skill loading."""
from __future__ import annotations

from typing import Any


def get_skills_hook(run_context: Any, user_requirements: str) -> list[Any]:
    del run_context, user_requirements
    return []


def skill_instruction_hook(skill_name: str, run_context: Any) -> str:
    del skill_name, run_context
    return ""


def skill_script_hook(
    skill_name: str,
    script_name: str,
    run_context: Any,
    *,
    execute: bool = False,
) -> Any:
    del skill_name, script_name, run_context, execute
    return ""

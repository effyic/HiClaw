"""Agno Agent subclass with per-request storage scrubbing."""
from __future__ import annotations

from typing import Any

from agno_worker.runtime.storage import apply_slim_storage_scrub, is_debug_storage

_STORAGE_AWARE_ATTR = "_effyic_storage_aware"
_ORIGINAL_SCRUB_ATTR = "_effyic_original_scrub_run_output_for_storage"
_PATCHED_ATTR = "_effyic_storage_scrub_patched"


def _patch_agno_storage_scrub_dispatch() -> None:
    """Route Agno module-level scrub to storage-aware handler when marked."""
    from agno.agent import _run as agent_run

    if getattr(agent_run, _PATCHED_ATTR, False):
        return

    original = agent_run.scrub_run_output_for_storage
    setattr(agent_run, _ORIGINAL_SCRUB_ATTR, original)

    def dispatch(agent: Any, run_response: Any) -> None:
        if not getattr(agent, _STORAGE_AWARE_ATTR, False):
            original(agent, run_response)
            return
        if is_debug_storage(run_response):
            original(agent, run_response)
            return
        apply_slim_storage_scrub(agent, run_response)

    agent_run.scrub_run_output_for_storage = dispatch
    setattr(agent_run, _PATCHED_ATTR, True)


class StorageAwareAgent:
    """Factory: returns an Agent marked for per-request storage control."""

    @staticmethod
    def create(**kwargs: Any) -> Any:
        from agno.agent import Agent

        _patch_agno_storage_scrub_dispatch()
        agent = Agent(**kwargs)
        setattr(agent, _STORAGE_AWARE_ATTR, True)
        return agent

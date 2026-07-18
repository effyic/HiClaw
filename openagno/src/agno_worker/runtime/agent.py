"""Agno Agent factory with per-request storage scrubbing and ignore-db I/O skip."""
from __future__ import annotations

import os
from typing import Any

from agno_worker.runtime.ignore_db import get_skip_session_read, get_skip_session_write
from agno_worker.runtime.storage import apply_slim_storage_scrub, is_debug_storage

_STORAGE_AWARE_ATTR = "_effyic_storage_aware"
_PATCHED_SCRUB_ATTR = "_effyic_storage_scrub_patched"
_PATCHED_IO_ATTR = "_effyic_ignore_db_io_patched"


def _patch_agno_storage_scrub_dispatch() -> None:
    """Route Agno module-level scrub to storage-aware handler when marked."""
    from agno.agent import _run as agent_run

    if getattr(agent_run, _PATCHED_SCRUB_ATTR, False):
        return

    original = agent_run.scrub_run_output_for_storage

    def dispatch(agent: Any, run_response: Any) -> None:
        if not getattr(agent, _STORAGE_AWARE_ATTR, False):
            original(agent, run_response)
            return
        # Nothing is written when skip_write; avoid scrub work.
        if get_skip_session_write():
            return
        if is_debug_storage(run_response):
            original(agent, run_response)
            return
        apply_slim_storage_scrub(agent, run_response)

    agent_run.scrub_run_output_for_storage = dispatch
    setattr(agent_run, _PATCHED_SCRUB_ATTR, True)


def _patch_agno_session_io_for_ignore_db() -> None:
    """Apply ``x-ignore-db`` read/write policy to Agno session I/O.

    - skip_write → short-circuit upsert (all persist paths)
    - skip_read → short-circuit read (ephemeral session when no session_id)
    """
    from agno.agent import _storage as agent_storage

    if getattr(agent_storage, _PATCHED_IO_ATTR, False):
        return

    original_upsert = agent_storage.upsert_session
    original_aupsert = agent_storage.aupsert_session
    original_read = agent_storage.read_session
    original_aread = agent_storage.aread_session

    def upsert_session(agent: Any, session: Any) -> Any:
        if get_skip_session_write():
            return session
        return original_upsert(agent, session)

    async def aupsert_session(agent: Any, session: Any) -> Any:
        if get_skip_session_write():
            return session
        return await original_aupsert(agent, session)

    def read_session(agent: Any, *args: Any, **kwargs: Any) -> Any:
        if get_skip_session_read():
            return None
        return original_read(agent, *args, **kwargs)

    async def aread_session(agent: Any, *args: Any, **kwargs: Any) -> Any:
        if get_skip_session_read():
            return None
        return await original_aread(agent, *args, **kwargs)

    agent_storage.upsert_session = upsert_session
    agent_storage.aupsert_session = aupsert_session
    agent_storage.read_session = read_session
    agent_storage.aread_session = aread_session
    setattr(agent_storage, _PATCHED_IO_ATTR, True)


class StorageAwareAgent:
    """Factory: returns an Agent marked for per-request storage control."""

    @staticmethod
    def create(**kwargs: Any) -> Any:
        from agno.agent import Agent

        _patch_agno_storage_scrub_dispatch()
        _patch_agno_session_io_for_ignore_db()
        # Default off: outbound posts to os-api.agno.com after each run.
        # Unrelated to AGNO_ENABLE_AGENTOS (console API). Override with
        # AGNO_TELEMETRY=true when needed.
        if "telemetry" not in kwargs:
            telem = os.environ.get("AGNO_TELEMETRY")
            kwargs["telemetry"] = (
                telem.lower() == "true" if telem is not None else False
            )
        agent = Agent(**kwargs)
        setattr(agent, _STORAGE_AWARE_ATTR, True)
        return agent

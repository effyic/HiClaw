"""TTL cache and run-scoped cache helpers for tenant pipeline."""
from __future__ import annotations

import os
import time
from typing import Any

# Stored on run_context as a private attribute — MUST NOT live in
# ``dependencies`` (those are injected into the LLM prompt when
# ``add_dependencies_to_context=True`` and would explode token usage).
RUN_CACHE_ATTR = "_effyic_tenant_run_cache"
# Legacy key kept for docs / grep; no longer written into dependencies.
RUN_CACHE_KEY = "_tenant_run_cache"
_MISSING = object()


class TTLCache:
    """Simple in-process TTL cache for tenant configuration reads."""

    def __init__(self, ttl_seconds: float | None = None, maxsize: int = 512) -> None:
        if ttl_seconds is None:
            ttl_seconds = float(os.environ.get("AGNO_AGENT_CONFIG_CACHE_TTL", "60"))
        self._ttl = max(0.0, ttl_seconds)
        self._maxsize = max(1, maxsize)
        self._entries: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            return _MISSING
        expires_at, value = entry
        if self._ttl > 0 and time.monotonic() >= expires_at:
            self._entries.pop(key, None)
            return _MISSING
        return value

    def set(self, key: str, value: Any) -> None:
        if self._ttl <= 0:
            return
        if len(self._entries) >= self._maxsize:
            oldest_key = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(oldest_key, None)
        expires_at = time.monotonic() + self._ttl
        self._entries[key] = (expires_at, value)

    def clear(self) -> None:
        self._entries.clear()


def ensure_run_cache(run_context: Any) -> dict[str, Any]:
    """Return per-run cache dict stored privately on run_context."""
    cache = getattr(run_context, RUN_CACHE_ATTR, None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(run_context, RUN_CACHE_ATTR, cache)
    # Drop legacy dependency key if a previous code path left it behind.
    deps = getattr(run_context, "dependencies", None)
    if isinstance(deps, dict):
        deps.pop(RUN_CACHE_KEY, None)
    return cache


def is_run_prepared(run_context: Any) -> bool:
    cache = ensure_run_cache(run_context)
    return bool(cache.get("_prepared"))

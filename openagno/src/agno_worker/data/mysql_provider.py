"""MySQL-backed data context provider driven by data hooks."""
from __future__ import annotations

import logging
from typing import Any

from agno_worker.hooks.registry import HookRegistry

logger = logging.getLogger(__name__)


class MySQLDataContextProvider:
    """Wrap data hooks as an Agno ContextProvider-compatible tool source."""

    def __init__(self, registry: HookRegistry, provider_id: str = "mysql_data") -> None:
        self.registry = registry
        self.provider_id = provider_id
        self._provider: Any | None = None

    def get_tools(self) -> list[Any]:
        provider = self._ensure_provider()
        if provider is None:
            return []
        return provider.get_tools()

    def _ensure_provider(self) -> Any | None:
        if self._provider is not None:
            return self._provider
        try:
            from agno.context import Answer, ContextProvider, Status
        except ImportError:
            logger.warning("agno.context unavailable; data provider disabled")
            return None

        registry = self.registry
        provider_id = self.provider_id

        class _Provider(ContextProvider):
            def __init__(self) -> None:
                super().__init__(provider_id)

            def query(self, question: str, *, run_context=None) -> Answer:
                raw = registry.call("data_query_hook", question, run_context)
                processed = registry.call("result_processing_hook", raw, run_context)
                return Answer(text=str(processed.get("text", raw)))

            async def aquery(self, question: str, *, run_context=None) -> Answer:
                return self.query(question, run_context=run_context)

            def status(self) -> Status:
                conn = registry.call("get_db_connection_hook", None)
                ok = bool(conn.url)
                return Status(ok=ok, detail=f"driver={conn.driver} configured={ok}")

            async def astatus(self) -> Status:
                return self.status()

        self._provider = _Provider()
        return self._provider

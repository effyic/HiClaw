"""MySQL-backed data hooks exposed as synchronous Agno tools."""
from __future__ import annotations

import logging
from typing import Any

from agno_worker.hooks.registry import HookRegistry

logger = logging.getLogger(__name__)


class MySQLDataContextProvider:
    """Wrap data hooks as sync tools compatible with Agent.run()."""

    def __init__(self, registry: HookRegistry, provider_id: str = "mysql_data") -> None:
        self.registry = registry
        self.provider_id = provider_id

    def get_tools(self) -> list[Any]:
        try:
            from agno.tools import tool
        except ImportError:
            logger.warning("agno.tools unavailable; data provider disabled")
            return []

        registry = self.registry
        provider_id = self.provider_id

        @tool(name=f"query_{provider_id}", description="Query tenant data via data hooks")
        def query_data(question: str, run_context: Any = None) -> str:
            raw = registry.call("data_query_hook", question, run_context)
            processed = registry.call("result_processing_hook", raw, run_context)
            return str(processed.get("text", raw))

        return [query_data]

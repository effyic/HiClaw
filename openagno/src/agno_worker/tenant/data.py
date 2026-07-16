"""Standard tenant data query tools backed by agno_agent configuration."""
from __future__ import annotations

from typing import Any

from agno_worker.tenant.collection import (
    build_collection_tools,
    is_collection_enabled,
    workflow_from_run_context,
)
from agno_worker.tenant.context import TenantContextResolver
from agno_worker.tenant.db import agent_db_driver, agent_db_url


class TenantDataProvider:
    """Expose tenant-aware data query as Agno tools."""

    def __init__(self, resolver: TenantContextResolver | None = None) -> None:
        self._resolver = resolver or TenantContextResolver()

    def query(self, question: str, run_context: Any) -> str:
        ctx = self._resolver.resolve(run_context)
        cfg = ctx.agent_config
        return (
            f"[db:agno_agent] tenant={cfg.get('display_name')} "
            f"role={cfg.get('role_code')} knowledge_ids={cfg.get('knowledge_ids')} "
            f"query={question!r}"
        )

    def process_result(self, results: Any, run_context: Any) -> dict[str, Any]:
        del run_context
        return {"text": str(results)}

    def db_connection_info(self) -> dict[str, str]:
        return {"url": agent_db_url(), "driver": agent_db_driver()}

    def get_tools(self, run_context: Any = None) -> list[Any]:
        try:
            from agno.tools import tool
        except ImportError:
            return []

        provider = self

        @tool(name="query_tenant_data", description="Query tenant data via agno_agent configuration")
        def query_data(question: str, run_context: Any = None) -> str:
            raw = provider.query(question, run_context)
            processed = provider.process_result(raw, run_context)
            return str(processed.get("text", raw))

        tools: list[Any] = [query_data]
        if run_context is not None and is_collection_enabled(
            workflow_from_run_context(run_context)
        ):
            tools.extend(build_collection_tools())
        return tools

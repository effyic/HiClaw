"""Orchestrate tenant standard flow with optional extension hook transforms."""
from __future__ import annotations

from typing import Any

from agno_worker.hooks.protocols import MCPServerConfig, SkillRef
from agno_worker.hooks.registry import HookRegistry
from agno_worker.tenant.cache import ensure_run_cache, is_run_prepared
from agno_worker.tenant.context import TenantContext, TenantContextResolver
from agno_worker.tenant.data import TenantDataProvider
from agno_worker.tenant.mcp import TenantMCPBuilder
from agno_worker.tenant.prompt import TenantPromptBuilder
from agno_worker.tenant.session import TenantSessionManager
from agno_worker.tenant.skills import TenantSkillCatalog
from agno_worker.tenant.store import AgentStore


class TenantAgentService:
    """Standard tenant agent pipeline; extension hooks may transform outputs."""

    def __init__(
        self,
        registry: HookRegistry | None = None,
        store: AgentStore | None = None,
    ) -> None:
        self.registry = registry or HookRegistry()
        self._store = store or AgentStore()
        self._resolver = TenantContextResolver(self._store)
        self.prompt = TenantPromptBuilder(self._store, self._resolver)
        self.mcp = TenantMCPBuilder(self._resolver)
        self.session = TenantSessionManager(self._resolver)
        self.skills = TenantSkillCatalog(self._resolver)
        self.data = TenantDataProvider(self._resolver)

    def clear_cache(self) -> None:
        self._store.clear_cache()

    def prepare_run_context(
        self,
        run_context: Any,
        session_state: dict[str, Any] | None = None,
        *,
        user_requirements: str = "",
        business_scenario: str = "",
    ) -> dict[str, Any]:
        """Populate run-scoped cache once per Agno run (called from pre_hook)."""
        cache = ensure_run_cache(run_context)
        if cache.get("_prepared"):
            return cache

        state = session_state if session_state is not None else (
            getattr(run_context, "session_state", None) or {}
        )
        tenant_ctx = self._resolver.resolve(run_context)
        # Sync collection FSM before MCP/prompt so confirm headers unlock write tools
        # on this same run (required for multi-replica chat continuity).
        from agno_worker.tenant.collection import (
            is_collection_enabled,
            sync_collection_into_session_state,
        )

        workflow = dict(tenant_ctx.agent_config.get("workflow") or {})
        if is_collection_enabled(workflow):
            current_state = (
                session_state
                if session_state is not None
                else (getattr(run_context, "session_state", None) or {})
            )
            synced = sync_collection_into_session_state(
                dict(current_state or {}),
                run_context,
                workflow,
            )
            synced["workflow"] = workflow
            run_context.session_state = synced
            state = synced
            session_state = synced
        elif getattr(run_context, "session_state", None) is not None:
            run_context.session_state.setdefault("workflow", workflow)

        business = self._build_business_context(run_context, tenant_ctx)
        cache["business_context"] = business

        bundle = self.prompt.build_prompt_bundle(
            run_context, state, business_context=business
        )
        cache["prompt_bundle"] = self._apply_transform(
            "transform_prompt_hook",
            run_context,
            bundle,
            default=bundle,
        )

        scenario = business_scenario or str(
            (getattr(run_context, "metadata", None) or {}).get("business_scenario") or ""
        )
        servers = self.mcp.build_servers(run_context, scenario)
        cache["mcp_servers"] = self._finalize_mcp_servers(run_context, servers)

        catalog = self.skills.resolve_catalog(run_context, user_requirements)
        cache["skill_catalog"] = self._apply_transform(
            "transform_skills_hook",
            run_context,
            catalog,
            default=catalog,
        )

        cache["_prepared"] = True
        return cache

    def get_prompt_bundle(
        self,
        run_context: Any,
        session_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not is_run_prepared(run_context):
            self.prepare_run_context(run_context, session_state)
        bundle = ensure_run_cache(run_context).get("prompt_bundle")
        return bundle if isinstance(bundle, dict) else {}

    def get_business_context(self, run_context: Any) -> dict[str, Any]:
        if not is_run_prepared(run_context):
            self.prepare_run_context(run_context)
        business = ensure_run_cache(run_context).get("business_context")
        return business if isinstance(business, dict) else {}

    def get_mcp_servers(self, run_context: Any) -> list[MCPServerConfig]:
        if not is_run_prepared(run_context):
            self.prepare_run_context(run_context)
        # Base servers are cached without collection write-gates so that mid-run
        # collection_complete can unlock tools on the next Agno tools() resolve.
        from agno_worker.tenant.collection import apply_collection_mcp_excludes

        servers = ensure_run_cache(run_context).get("mcp_servers")
        base = list(servers) if isinstance(servers, list) else []
        return apply_collection_mcp_excludes(run_context, base)

    def get_skill_catalog(
        self,
        run_context: Any,
        user_requirements: str = "",
    ) -> list[SkillRef]:
        if not is_run_prepared(run_context):
            self.prepare_run_context(run_context, user_requirements=user_requirements)
        catalog = ensure_run_cache(run_context).get("skill_catalog")
        return list(catalog) if isinstance(catalog, list) else []

    def build_prompt_bundle(
        self,
        run_context: Any,
        session_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if is_run_prepared(run_context):
            return self.get_prompt_bundle(run_context, session_state)
        business = self.resolve_business_context(run_context)
        bundle = self.prompt.build_prompt_bundle(
            run_context, session_state, business_context=business
        )
        return self._apply_transform(
            "transform_prompt_hook",
            run_context,
            bundle,
            default=bundle,
        )

    def resolve_business_context(self, run_context: Any) -> dict[str, Any]:
        """Standard context from agno_agent; hook may enrich with tenant business data."""
        if is_run_prepared(run_context):
            return self.get_business_context(run_context)
        ctx = self._resolver.resolve(run_context)
        return self._build_business_context(run_context, ctx)

    def _build_business_context(
        self,
        run_context: Any,
        ctx: TenantContext,
    ) -> dict[str, Any]:
        base: dict[str, Any] = {
            "tenant_id": ctx.tenant_id,
            "role_code": ctx.role_code,
            "agent_config": ctx.agent_config,
            "agents": self._store.list_agents(ctx.tenant_id),
        }
        if not self.registry.has("enrich_business_context_hook"):
            return base
        result = self.registry.call("enrich_business_context_hook", run_context, base)
        if not isinstance(result, dict):
            return base
        merged = dict(base)
        merged.update(result)
        return merged

    def build_mcp_servers(
        self,
        run_context: Any,
        business_scenario: str = "",
    ) -> list[MCPServerConfig]:
        if is_run_prepared(run_context):
            return self.get_mcp_servers(run_context)
        servers = self.mcp.build_servers(run_context, business_scenario)
        return self._finalize_mcp_servers(run_context, servers)

    def resolve_skill_catalog(
        self,
        run_context: Any,
        user_requirements: str = "",
    ) -> list[SkillRef]:
        if is_run_prepared(run_context):
            return self.get_skill_catalog(run_context, user_requirements)
        catalog = self.skills.resolve_catalog(run_context, user_requirements)
        transformed = self._apply_transform(
            "transform_skills_hook",
            run_context,
            catalog,
            default=catalog,
        )
        return transformed if isinstance(transformed, list) else catalog

    def build_session_updates(
        self,
        session_state: dict[str, Any],
        run_context: Any,
    ) -> dict[str, Any]:
        updates = self.session.build_session_updates(session_state, run_context)
        ctx = self._resolver.resolve(run_context)
        workflow_payload = {
            "workflow": updates.get("workflow") or ctx.agent_config.get("workflow") or {},
            "session_state": updates,
        }
        transformed = self._apply_transform(
            "transform_workflow_hook",
            run_context,
            workflow_payload,
            default=workflow_payload,
        )
        if isinstance(transformed, dict):
            if "session_state" in transformed and isinstance(transformed["session_state"], dict):
                return transformed["session_state"]
            if "workflow" in transformed:
                updates["workflow"] = transformed["workflow"]
        state_transform = self._apply_transform(
            "transform_session_state_hook",
            run_context,
            updates,
            default=updates,
        )
        return state_transform if isinstance(state_transform, dict) else updates

    def load_skill_instruction(self, skill_name: str, run_context: Any) -> str:
        return self.skills.load_instruction(skill_name, run_context)

    def load_skill_script(
        self,
        skill_name: str,
        script_name: str,
        run_context: Any,
        *,
        execute: bool = False,
    ) -> Any:
        return self.skills.load_script(
            skill_name, script_name, run_context, execute=execute
        )

    def filter_mcp_tools(self, run_context: Any, tools: list[Any]) -> list[Any]:
        if not self.registry.has("mcp_tool_filter_hook"):
            return tools
        filtered = self.registry.call("mcp_tool_filter_hook", run_context, tools)
        return filtered if filtered is not None else tools

    def on_mcp_connection(self, server_config: MCPServerConfig) -> None:
        TenantMCPBuilder.apply_connection_defaults(server_config)
        if self.registry.has("mcp_connection_hook"):
            self.registry.call("mcp_connection_hook", server_config)

    def process_data_result(self, results: Any, run_context: Any) -> dict[str, Any]:
        processed = self.data.process_result(results, run_context)
        if self.registry.has("result_processing_hook"):
            hook_result = self.registry.call("result_processing_hook", results, run_context)
            if isinstance(hook_result, dict):
                return hook_result
        return processed

    def _finalize_mcp_servers(
        self,
        run_context: Any,
        servers: list[MCPServerConfig],
    ) -> list[MCPServerConfig]:
        """Transform servers, inject default forward headers, then mcp_headers_hook."""
        transformed = self._apply_transform(
            "transform_mcp_servers_hook",
            run_context,
            servers,
            default=servers,
        )
        finalized: list[MCPServerConfig] = (
            list(transformed) if isinstance(transformed, list) else list(servers)
        )
        self.mcp.apply_forwarded_headers(run_context, finalized)
        return self._apply_mcp_headers_hook(run_context, finalized)

    def _apply_mcp_headers_hook(
        self,
        run_context: Any,
        servers: list[MCPServerConfig],
    ) -> list[MCPServerConfig]:
        if not self.registry.has("mcp_headers_hook"):
            return servers
        for server in servers:
            if not server.url:
                continue
            current = dict(server.headers or {})
            result = self.registry.call(
                "mcp_headers_hook",
                run_context,
                server,
                current,
            )
            if isinstance(result, dict):
                server.headers = {
                    str(key): str(value) for key, value in result.items()
                }
        return servers

    def _apply_transform(
        self,
        hook_name: str,
        run_context: Any,
        payload: Any,
        *,
        default: Any,
    ) -> Any:
        if not self.registry.has(hook_name):
            return default
        result = self.registry.call(hook_name, run_context, payload)
        return default if result is None else result

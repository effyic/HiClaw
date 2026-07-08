"""Dynamic Agent builder using hook registry."""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from agno_worker.agentspec.schema import AgentDef, AgentSpec
from agno_worker.data import MySQLDataContextProvider
from agno_worker.hooks.registry import HookRegistry
from agno_worker.mcp.loader import build_mcp_tools

logger = logging.getLogger(__name__)


class AgentBuilder:
    """Compose Agno Agent instances from hooks and AgentSpec."""

    def __init__(
        self,
        registry: HookRegistry,
        spec: AgentSpec,
        db: Any,
    ) -> None:
        self.registry = registry
        self.spec = spec
        self.db = db
        self._data_provider = MySQLDataContextProvider(registry)

    def build_agent(self, defn: AgentDef) -> Any:
        from agno.agent import Agent

        base_instructions = self._base_instructions(defn)
        return Agent(
            name=defn.name,
            model=self._resolve_model(defn.model or self.spec.model),
            instructions=self._make_instructions(base_instructions),
            tools=self._make_tools(),
            db=self.db,
            pre_hooks=[self._make_pre_hook()],
            post_hooks=[self._make_post_hook()],
            add_history_to_context=True,
            markdown=True,
            dependencies={"user_profile": {}, "agentspec_agent": defn.name},
        )

    def _base_instructions(self, defn: AgentDef) -> str:
        parts = [defn.instructions] if defn.instructions else []
        if defn.knowledge and defn.knowledge.knowledge_id:
            parts.append(
                f"Use knowledge base provider={defn.knowledge.provider} "
                f"knowledge_id={defn.knowledge.knowledge_id} for retrieval."
            )
        return "\n\n".join(p for p in parts if p)

    def _make_instructions(self, base_instructions: str) -> Callable[..., str]:
        registry = self.registry

        def _instructions(run_context: Any) -> str:
            session_state = run_context.session_state or {}
            user_profile = (run_context.dependencies or {}).get("user_profile") or {}
            system = registry.call("get_system_prompt_hook", run_context, session_state)
            dynamic = registry.call("get_instructions_hook", run_context, user_profile)
            parts = [p for p in (system, base_instructions, dynamic) if p]
            return "\n\n".join(parts)

        return _instructions

    def _make_tools(self) -> Callable[..., list[Any]]:
        registry = self.registry
        data_provider = self._data_provider

        def _tools(run_context: Any) -> list[Any]:
            scenario = (run_context.metadata or {}).get("business_scenario", "default")
            servers = registry.call("get_mcp_servers_hook", run_context, scenario)
            tools: list[Any] = []
            tools.extend(build_mcp_tools(servers, registry))
            tools.extend(data_provider.get_tools())
            return registry.call("mcp_tool_filter_hook", run_context, tools) or tools

        return _tools

    def _make_pre_hook(self) -> Callable[..., None]:
        registry = self.registry

        def _pre_hook(run_input: Any, run_context: Any, session: Any = None, **_: Any) -> None:
            session_id = _extract_session_id(session, run_context)
            user_context = {
                "user_id": getattr(run_context, "user_id", None),
                "session_id": session_id,
            }
            if session_id:
                registry.call("session_init_hook", session_id, user_context)

            filters = registry.call("get_context_filter_hook", run_context)
            if filters:
                run_context.knowledge_filters = filters

            if run_context.session_state is None:
                run_context.session_state = {}

        return _pre_hook

    def _make_post_hook(self) -> Callable[..., None]:
        registry = self.registry

        def _post_hook(
            run_output: Any,
            run_context: Any,
            session: Any = None,
            **_: Any,
        ) -> None:
            if run_context.session_state is None:
                run_context.session_state = {}
            updates = registry.call(
                "session_update_hook", run_context.session_state, run_context
            )
            if isinstance(updates, dict) and updates:
                run_context.session_state.update(updates)

        return _post_hook

    def _resolve_model(self, model_id: str) -> Any:
        default_model = (
            os.environ.get("HICLAW_DEFAULT_MODEL", "")
            or os.environ.get("AGNO_DEFAULT_MODEL", "")
            or model_id
            or "qwen3.6-plus"
        )
        gateway_url = os.environ.get("HICLAW_AI_GATEWAY_URL", "").rstrip("/")
        gateway_key = os.environ.get("HICLAW_WORKER_GATEWAY_KEY", "")
        if gateway_url and gateway_key:
            from agno.models.openai import OpenAIChat

            return OpenAIChat(
                id=default_model,
                api_key=gateway_key,
                base_url=f"{gateway_url}/v1",
            )
        if ":" not in default_model:
            return f"openai:{default_model}"
        return default_model


def _extract_session_id(session: Any, run_context: Any) -> str:
    if session is not None:
        for attr in ("session_id", "id"):
            value = getattr(session, attr, None)
            if value:
                return str(value)
    metadata = getattr(run_context, "metadata", None) or {}
    if session_id := metadata.get("session_id"):
        return str(session_id)
    return ""

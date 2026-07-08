"""Dynamic Agent builder: one Agent, N roles, hooks override AgentSpec."""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from agno_worker.agentspec.schema import AgentDef, AgentSpec
from agno_worker.data import MySQLDataContextProvider
from agno_worker.hooks.compose import (
    has_hook_data,
    pick_hook_or_spec,
    resolve_active_role,
    role_def,
    spec_instructions,
    spec_system_prompt,
)
from agno_worker.hooks.registry import HookRegistry
from agno_worker.mcp.loader import build_mcp_tools

logger = logging.getLogger(__name__)


class AgentBuilder:
    """Compose a single dynamic Agno Agent; spec.agents is a role catalog."""

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

    def build_dynamic_agent(self) -> Any:
        """Build one Agent whose prompt/tools are resolved per run via hooks."""
        from agno.agent import Agent

        agent_name = self.spec.name or "agent"
        default_role = next(iter(self.spec.agents), "default")
        default_defn = self.spec.agents.get(default_role)

        return Agent(
            name=agent_name,
            description=self.spec.description or "Dynamic multi-role agent",
            model=self._resolve_model(
                (default_defn.model if default_defn else "") or self.spec.model
            ),
            system_message=self._make_system_message(),
            instructions=self._make_instructions(),
            tools=self._make_tools(),
            db=self.db,
            pre_hooks=[self._make_pre_hook()],
            post_hooks=[self._make_post_hook()],
            add_history_to_context=True,
            markdown=True,
            dependencies={
                "user_profile": {},
                "role_catalog": list(self.spec.agents.keys()),
            },
        )

    def build_agent(self, defn: AgentDef) -> Any:
        """Backward-compatible alias; always returns the single dynamic agent."""
        return self.build_dynamic_agent()

    def _make_system_message(self) -> Callable[..., str]:
        registry = self.registry
        spec = self.spec

        def _system_message(run_context: Any) -> str:
            session_state = run_context.session_state or {}
            active_role = resolve_active_role(session_state, spec)
            defn = role_def(spec, active_role)

            hook_value = registry.call(
                "get_system_prompt_hook", run_context, session_state
            )
            spec_value = spec_system_prompt(defn)
            resolved = pick_hook_or_spec(hook_value, spec_value)

            if has_hook_data(hook_value):
                logger.debug("system_message overridden by hook (role=%s)", active_role)
            else:
                logger.debug("system_message from AgentSpec role=%s", active_role)

            return str(resolved or "")

        return _system_message

    def _make_instructions(self) -> Callable[..., str]:
        registry = self.registry
        spec = self.spec

        def _instructions(run_context: Any) -> str:
            session_state = run_context.session_state or {}
            user_profile = (run_context.dependencies or {}).get("user_profile") or {}
            active_role = resolve_active_role(session_state, spec)
            defn = role_def(spec, active_role)

            hook_value = registry.call(
                "get_instructions_hook", run_context, user_profile
            )
            spec_value = spec_instructions(defn)
            resolved = pick_hook_or_spec(hook_value, spec_value)

            if has_hook_data(hook_value):
                logger.debug("instructions overridden by hook (role=%s)", active_role)
            else:
                logger.debug("instructions from AgentSpec role=%s", active_role)

            return str(resolved or "")

        return _instructions

    def _make_tools(self) -> Callable[..., list[Any]]:
        registry = self.registry
        data_provider = self._data_provider

        def _tools(run_context: Any) -> list[Any]:
            session_state = run_context.session_state or {}
            active_role = resolve_active_role(session_state, self.spec)
            scenario = (run_context.metadata or {}).get(
                "business_scenario", active_role
            )

            hook_servers = registry.call(
                "get_mcp_servers_hook", run_context, scenario
            )
            tools: list[Any] = []

            if has_hook_data(hook_servers):
                tools.extend(build_mcp_tools(hook_servers, registry))
            elif role_defn := role_def(self.spec, active_role):
                # AgentSpec tool names are declarative placeholders for now.
                if role_defn.tools:
                    logger.debug(
                        "MCP hook empty; spec role=%s declares tools=%s (no auto-load)",
                        active_role,
                        role_defn.tools,
                    )

            tools.extend(data_provider.get_tools())
            filtered = registry.call("mcp_tool_filter_hook", run_context, tools)
            return filtered if filtered is not None else tools

        return _tools

    def _make_pre_hook(self) -> Callable[..., None]:
        registry = self.registry
        spec = self.spec

        def _pre_hook(run_input: Any, run_context: Any, session: Any = None, **_: Any) -> None:
            session_id = _extract_session_id(session, run_context)
            user_context = {
                "user_id": getattr(run_context, "user_id", None),
                "session_id": session_id,
            }
            if session_id:
                registry.call("session_init_hook", session_id, user_context)

            if run_context.session_state is None:
                run_context.session_state = {}

            active_role = resolve_active_role(run_context.session_state, spec)
            run_context.session_state.setdefault("active_role", active_role)

            hook_filters = registry.call("get_context_filter_hook", run_context)
            spec_filters = _spec_context_filters(active_role, spec)
            run_context.knowledge_filters = pick_hook_or_spec(hook_filters, spec_filters)

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


def _spec_context_filters(active_role: str, spec: AgentSpec) -> dict[str, Any]:
    defn = role_def(spec, active_role)
    filters: dict[str, Any] = {"role": active_role}
    if defn and defn.knowledge and defn.knowledge.knowledge_id:
        filters["knowledge_id"] = defn.knowledge.knowledge_id
        filters["provider"] = defn.knowledge.provider
    return filters


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

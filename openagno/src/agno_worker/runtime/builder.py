"""Dynamic Agent builder: standard tenant pipeline + optional extension hooks."""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from agno_worker.agentspec.schema import AgentDef, AgentSpec
from agno_worker.hooks.compose import (
    build_run_dependencies,
    has_hook_data,
    normalize_knowledge_filters,
    pick_hook_or_spec,
    resolve_active_role,
    role_def,
    spec_context_filters,
    spec_instructions,
    spec_system_prompt,
)
from agno_worker.hooks.registry import HookRegistry
from agno_worker.mcp.loader import build_mcp_tools
from agno_worker.skills import DynamicSkillsManager, normalize_skill_refs, skill_catalog_summary
from agno_worker.api.identity import default_debug_request
from agno_worker.runtime.agent import StorageAwareAgent
from agno_worker.runtime.ignore_db import get_ignore_db
from agno_worker.runtime.storage import slim_session_state, sync_debug_request_to_session_state
from agno_worker.runtime.thinking import attach_thinking_request_params
from agno_worker.tenant.collection import filter_collection_gated_tools
from agno_worker.tenant.service import TenantAgentService

logger = logging.getLogger(__name__)


def _default_role_name(spec: AgentSpec) -> str:
    return next(iter(spec.agents), "default")


def _static_instructions_from_spec(
    spec: AgentSpec,
    role_name: str | None = None,
) -> str:
    active = role_name or _default_role_name(spec)
    defn = role_def(spec, active)
    parts = [
        part
        for part in (spec_system_prompt(defn), spec_instructions(defn))
        if part.strip()
    ]
    return "\n\n".join(parts)


class AgentBuilder:
    """Compose a single dynamic Agno Agent using tenant DB config + AgentSpec fallback."""

    def __init__(
        self,
        registry: HookRegistry,
        spec: AgentSpec,
        db: Any,
        tenant_service: TenantAgentService | None = None,
    ) -> None:
        self.registry = registry
        self.spec = spec
        self.db = db
        self.tenant = tenant_service or TenantAgentService(registry)
        self._skills_manager = DynamicSkillsManager(self.tenant)

    def build_dynamic_agent(self) -> Any:
        agent_name = self.spec.name or "agent"
        default_role = next(iter(self.spec.agents), "default")
        default_defn = self.spec.agents.get(default_role)

        return StorageAwareAgent.create(
            name=agent_name,
            description=self.spec.description or "Dynamic multi-role agent",
            model=self._resolve_model(
                (default_defn.model if default_defn else "") or self.spec.model
            ),
            instructions=self._make_instructions(),
            tools=self._make_tools(),
            db=self.db,
            pre_hooks=[self._make_pre_hook()],
            post_hooks=[self._make_post_hook()],
            add_history_to_context=True,
            # Only slim public deps (tenant / user_profile / role_catalog) belong in
            # the LLM prompt. Heavy objects stay in dependencies for hooks/tools but
            # must not be serialized into <additional context>.
            add_dependencies_to_context=False,
            markdown=True,
            cache_callables=False,
            dependencies={
                "role_catalog": list(self.spec.agents.keys()),
            },
        )

    def build_agent(self, defn: AgentDef) -> Any:
        return self.build_dynamic_agent()

    def _make_instructions(self) -> Callable[..., str]:
        spec = self.spec
        tenant = self.tenant

        def _instructions(run_context: Any = None, **_: Any) -> str:
            if run_context is None:
                return _static_instructions_from_spec(spec)

            session_state = run_context.session_state or {}
            user_profile = (run_context.dependencies or {}).get("user_profile") or {}
            active_role = resolve_active_role(session_state, spec)
            defn = role_def(spec, active_role)

            bundle = tenant.get_prompt_bundle(run_context, session_state)
            system_text = str(bundle.get("system_prompt") or "")
            instr_text = str(bundle.get("instructions") or "")

            spec_system = spec_system_prompt(defn)
            spec_instr = spec_instructions(defn)
            system_text = str(pick_hook_or_spec(system_text, spec_system) or "")
            instr_text = str(pick_hook_or_spec(instr_text, spec_instr) or "")

            parts = [part for part in (system_text, instr_text) if part.strip()]

            catalog = normalize_skill_refs(
                (run_context.dependencies or {}).get("skill_catalog")
            )
            summary = skill_catalog_summary(catalog)
            if summary:
                parts.append(summary)

            logger.debug("prompt from tenant pipeline (role=%s, tenant=%s)", active_role, user_profile.get("tenant_id"))
            return "\n\n".join(parts)

        return _instructions

    def _make_tools(self) -> Callable[..., list[Any]]:
        spec = self.spec
        tenant = self.tenant
        skills_manager = self._skills_manager

        def _tools(run_context: Any = None, **_: Any) -> list[Any]:
            if run_context is None:
                return []

            session_state = run_context.session_state or {}
            active_role = resolve_active_role(session_state, spec)

            servers = tenant.get_mcp_servers(run_context)
            tools: list[Any] = []

            if has_hook_data(servers):
                tools.extend(build_mcp_tools(servers, tenant))
            elif role_defn := role_def(spec, active_role):
                if role_defn.tools:
                    logger.debug(
                        "No MCP servers for role=%s; spec declares tools=%s (no auto-load)",
                        active_role,
                        role_defn.tools,
                    )

            catalog = normalize_skill_refs(
                (run_context.dependencies or {}).get("skill_catalog")
            )
            if not catalog:
                user_requirements = str(
                    (run_context.metadata or {}).get("user_requirements") or ""
                )
                catalog = tenant.get_skill_catalog(run_context, user_requirements)
                if run_context.dependencies is None:
                    run_context.dependencies = {}
                run_context.dependencies["skill_catalog"] = [
                    {
                        "name": ref.name,
                        "description": ref.description,
                        "source_path": ref.source_path,
                        "scripts": list(ref.scripts),
                    }
                    for ref in catalog
                ]

            tools.extend(skills_manager.build_tools(run_context, catalog))
            tools.extend(tenant.data.get_tools(run_context))
            tools = tenant.filter_mcp_tools(run_context, tools)
            # Collection FSM hard-gate: hide required_actions write tools until
            # when-condition (missing empty + probe done) is met.
            return filter_collection_gated_tools(run_context, tools)

        return _tools

    def _make_pre_hook(self) -> Callable[..., None]:
        spec = self.spec
        tenant = self.tenant

        def _pre_hook(run_input: Any, run_context: Any, session: Any = None, **_: Any) -> None:
            session_id = _extract_session_id(session, run_context)
            user_context = {
                "user_id": getattr(run_context, "user_id", None),
                "session_id": session_id,
            }
            if session_id:
                tenant.session.init_session(session_id, user_context)

            if run_context.session_state is None:
                run_context.session_state = {}

            metadata = getattr(run_context, "metadata", None) or {}
            if "debug_request" in metadata:
                sync_debug_request_to_session_state(
                    run_context,
                    bool(metadata["debug_request"]),
                )

            active_role = resolve_active_role(run_context.session_state, spec)
            run_context.session_state.setdefault("active_role", active_role)

            user_requirements = _extract_user_message(run_input)
            if user_requirements:
                metadata = getattr(run_context, "metadata", None)
                if metadata is None:
                    run_context.metadata = {}
                    metadata = run_context.metadata
                metadata["user_requirements"] = user_requirements

            tenant.prepare_run_context(
                run_context,
                run_context.session_state,
                user_requirements=user_requirements,
                business_scenario=resolve_active_role(run_context.session_state, spec),
            )
            bundle = tenant.get_prompt_bundle(run_context, run_context.session_state)
            spec_filters = spec_context_filters(active_role, spec)
            tenant_filters = bundle.get("context_filters") or {}
            merged = pick_hook_or_spec(tenant_filters, spec_filters)
            if not isinstance(merged, dict):
                merged = spec_filters
            normalized = normalize_knowledge_filters(merged)
            run_context.knowledge_filters = normalized

            if run_context.dependencies is None:
                run_context.dependencies = {}
            run_context.dependencies.update(build_run_dependencies(run_context, normalized))
            run_context.dependencies.setdefault("role_catalog", list(spec.agents.keys()))
            run_context.dependencies["agent_config"] = bundle.get("agent_config") or {}
            run_context.dependencies["business_context"] = bundle.get("business_context") or tenant.get_business_context(run_context)

            catalog = tenant.get_skill_catalog(run_context, user_requirements)
            run_context.dependencies["skill_catalog"] = [
                {
                    "name": ref.name,
                    "description": ref.description,
                    "source_path": ref.source_path,
                    "scripts": list(ref.scripts),
                }
                for ref in catalog
            ]

        return _pre_hook

    def _make_post_hook(self) -> Callable[..., None]:
        tenant = self.tenant

        def _post_hook(
            run_output: Any,
            run_context: Any,
            session: Any = None,
            **_: Any,
        ) -> None:
            if run_context.session_state is None:
                run_context.session_state = {}
            updates = tenant.build_session_updates(run_context.session_state, run_context)
            if isinstance(updates, dict) and updates:
                run_context.session_state.update(updates)

            from agno_worker.tenant.collection import (
                COLLECTION_STATE_KEY,
                append_status_marker,
                apply_probe_salvage_and_nudge,
                apply_required_actions_from_run,
                apply_scripts_progress_from_run,
                apply_ask_quality_tracking,
                collection_status_payload,
                extract_successful_tool_names,
                is_collection_enabled,
                resolve_collection_config,
                sanitize_patient_visible_reply,
            )

            workflow = (run_context.session_state or {}).get("workflow") or {}
            if is_collection_enabled(workflow if isinstance(workflow, dict) else {}):
                coll_cfg = resolve_collection_config(
                    workflow if isinstance(workflow, dict) else {}
                )
                coll = (run_context.session_state or {}).get(COLLECTION_STATE_KEY) or {}
                if not isinstance(coll, dict):
                    coll = {}
                # Scrub has not run yet — record required MCP successes from this turn.
                coll = apply_required_actions_from_run(coll, coll_cfg, run_output)
                reply_text = None
                if run_output is not None and hasattr(run_output, "content"):
                    from agno_worker.runtime.structured_output import (
                        content_to_reply_text,
                        prefer_last_assistant_after_tools,
                    )

                    preferred = prefer_last_assistant_after_tools(run_output)
                    if preferred is not None:
                        reply_text = preferred
                    else:
                        reply_text = content_to_reply_text(
                            getattr(run_output, "content", None)
                        )
                had_reply = bool((reply_text or "").strip())
                was_closing = bool(coll.get("closing_delivered"))
                coll = apply_scripts_progress_from_run(
                    coll, coll_cfg, had_patient_reply=had_reply
                )

                salvaged: list = []
                if reply_text is not None:
                    # Never leak operator nudges / fake tool-call text to patients.
                    reply_text, salvaged = sanitize_patient_visible_reply(reply_text)
                tools_ok = set(extract_successful_tool_names(run_output))
                coll = apply_probe_salvage_and_nudge(
                    coll,
                    coll_cfg,
                    tools_ok=tools_ok,
                    salvaged=salvaged,
                )
                if reply_text is not None:
                    coll = apply_ask_quality_tracking(coll, coll_cfg, reply_text)
                # Closing turn: restore reply-field substance if tools kept only
                # scripts.closing as the last assistant segment.
                just_closed = (not was_closing) and bool(coll.get("closing_delivered"))
                if reply_text is not None and just_closed:
                    from agno_worker.tenant.collection.kinds.dialogue.scripts import (
                        ensure_reply_fields_visible,
                    )

                    reply_text = ensure_reply_fields_visible(
                        reply_text, coll, coll_cfg
                    )
                run_context.session_state[COLLECTION_STATE_KEY] = coll
                if isinstance(coll, dict) and coll.get("phase"):
                    run_context.session_state["phase"] = coll["phase"]

                if reply_text is not None:
                    # Pending-action / probe reminders stay INTERNAL (next-turn
                    # instructions via probe_nudge_due). Do not append [系统提示]
                    # into patient-visible content — that caused models to print
                    # collection_probe_note(...) as chat text.
                    run_output.content = append_status_marker(
                        reply_text, coll, config=coll_cfg
                    )
                    reply_text = str(run_output.content or "")
                # Prefer metadata for streaming H5 clients (reply chunks omit marker).
                # Include dept_code parsed from collected slots and/or reply text.
                status = collection_status_payload(
                    coll, reply_text=reply_text, config=coll_cfg
                )
                if run_output is not None:
                    if not isinstance(getattr(run_output, "metadata", None), dict):
                        run_output.metadata = {}
                    run_output.metadata["collection_status"] = status
                metadata = getattr(run_context, "metadata", None)
                if metadata is None:
                    run_context.metadata = {"collection_status": status}
                elif isinstance(metadata, dict):
                    metadata["collection_status"] = status

            metadata = getattr(run_context, "metadata", None) or {}
            debug_request = bool(metadata.get("debug_request", default_debug_request()))
            sync_debug_request_to_session_state(run_context, debug_request)
            if not debug_request and not get_ignore_db():
                run_context.session_state = slim_session_state(run_context.session_state)
            if run_output is not None:
                run_output.session_state = dict(run_context.session_state)
                if not isinstance(getattr(run_output, "metadata", None), dict):
                    run_output.metadata = {}
                run_output.metadata["debug_request"] = debug_request
                run_output.metadata["ignore_db"] = get_ignore_db()

        return _post_hook

    def _resolve_model(self, model_id: str) -> Any:
        default_model = (
            os.environ.get("HICLAW_DEFAULT_MODEL", "")
            or os.environ.get("AGNO_DEFAULT_MODEL", "")
            or model_id
            or "qwen3.6-plus"
        )
        if ":" in default_model:
            default_model = default_model.split(":", 1)[-1]

        gateway_url = os.environ.get("HICLAW_AI_GATEWAY_URL", "").rstrip("/")
        gateway_key = os.environ.get("HICLAW_WORKER_GATEWAY_KEY", "")
        api_key = gateway_key or os.environ.get("OPENAI_API_KEY", "")
        base_url = (
            f"{gateway_url}/v1"
            if gateway_url and gateway_key
            else (os.environ.get("OPENAI_BASE_URL", "") or "").rstrip("/")
        )

        # Always build a concrete model and patch enable_thinking. Passing a bare
        # ``openai:xxx`` string skips the patch; DashScope then keeps Qwen thinking
        # on by default and each turn costs several extra seconds.
        try:
            from agno.models.openai.responses import OpenAIResponses

            kwargs: dict[str, Any] = {
                "id": default_model,
                "extra_body": {"enable_thinking": False},
            }
            if api_key:
                kwargs["api_key"] = api_key
            if base_url:
                kwargs["base_url"] = base_url
            return attach_thinking_request_params(OpenAIResponses(**kwargs))
        except Exception:
            logger.exception("Failed to build OpenAIResponses; falling back to model id string")
            return f"openai:{default_model}"


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


def _extract_user_message(run_input: Any) -> str:
    if run_input is None:
        return ""
    for attr in ("input_content", "message", "content", "input"):
        value = getattr(run_input, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(run_input, str):
        return run_input.strip()
    return ""

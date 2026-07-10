"""Standard tenant prompt assembly from agno_agent configuration."""
from __future__ import annotations

import os
from typing import Any

from agno_worker.tenant.context import TenantContext, TenantContextResolver
from agno_worker.tenant.store import AgentStore

DEFAULT_KNOWLEDGE_PROVIDER = os.environ.get("AGNO_KNOWLEDGE_PROVIDER", "").strip()


class TenantPromptBuilder:
    """Build system prompt, instructions, and knowledge filters from DB config.

    Scenario-specific text must come from agno_agent.workflow.prompt_append /
    instructions_append, or enrich_business_context_hook via prompt_supplements.
    """

    def __init__(
        self,
        store: AgentStore | None = None,
        resolver: TenantContextResolver | None = None,
    ) -> None:
        self._store = store or AgentStore()
        self._resolver = resolver or TenantContextResolver(self._store)

    def build_prompt_bundle(
        self,
        run_context: Any,
        session_state: dict[str, Any] | None = None,
        business_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ctx = self._resolver.resolve(run_context)
        cfg = ctx.agent_config
        business = dict(business_context or {})
        state = session_state or getattr(run_context, "session_state", None) or {}
        workflow = cfg.get("workflow") or {}

        system_prompt = self._build_system_prompt(cfg, ctx, state, workflow, business)
        instructions = self._build_instructions(
            run_context, ctx, cfg, workflow, business
        )
        context_filters = self._build_context_filters(run_context, ctx, cfg, state)

        return {
            "system_prompt": system_prompt,
            "instructions": instructions,
            "context_filters": context_filters,
            "agent_config": cfg,
            "business_context": business,
        }

    def _collect_prompt_supplements(
        self,
        workflow: dict[str, Any],
        business: dict[str, Any],
    ) -> dict[str, str]:
        """Merge DB workflow append fields with hook-provided supplements."""
        supplements: dict[str, str] = {}
        for key in ("prompt_append", "instructions_append"):
            if value := workflow.get(key):
                target = "system_prompt_append" if key == "prompt_append" else key
                supplements[target] = str(value).strip()
        hook_supplements = business.get("prompt_supplements")
        if isinstance(hook_supplements, dict):
            for key, value in hook_supplements.items():
                if value is not None and str(value).strip():
                    supplements[key] = str(value).strip()
        return supplements

    def _build_system_prompt(
        self,
        cfg: dict[str, Any],
        ctx: TenantContext,
        session_state: dict[str, Any],
        workflow: dict[str, Any],
        business: dict[str, Any],
    ) -> str:
        base = str(cfg.get("system_prompt") or "").strip()
        supplements = self._collect_prompt_supplements(workflow, business)

        append_parts = [
            supplements[key]
            for key in ("system_prompt_append",)
            if supplements.get(key)
        ]
        if append_parts:
            base = "\n\n".join([base, *append_parts]) if base else "\n\n".join(append_parts)

        role = ctx.role_code or session_state.get("role_code") or cfg.get("role_code", "default")
        display = cfg.get("display_name") or role
        role_line = f"当前角色: {display} ({role})。"
        return f"{base}\n{role_line}" if base else role_line

    def _build_instructions(
        self,
        run_context: Any,
        ctx: TenantContext,
        cfg: dict[str, Any],
        workflow: dict[str, Any],
        business: dict[str, Any],
    ) -> str:
        metadata = getattr(run_context, "metadata", None) or {}
        deps = getattr(run_context, "dependencies", None) or {}
        user_profile = deps.get("user_profile") or {}
        tenant_id = user_profile.get("tenant_id") or metadata.get("tenant_id") or ctx.tenant_id
        supplements = self._collect_prompt_supplements(workflow, business)

        parts = [
            str(cfg.get("instructions") or "").strip(),
            f"租户标识: {tenant_id}",
            f"角色: {ctx.role_code}",
        ]
        if route_label := business.get("route_label"):
            parts.append(f"科室: {route_label}")
        kb = cfg.get("knowledge_ids") or []
        parts.append(f"可用知识库: {', '.join(kb) if kb else '（未启用）'}")
        if instructions_append := supplements.get("instructions_append"):
            parts.append(instructions_append)
        return "\n".join(part for part in parts if part)

    def _build_context_filters(
        self,
        run_context: Any,
        ctx: TenantContext,
        cfg: dict[str, Any],
        session_state: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = getattr(run_context, "metadata", None) or {}
        workflow = cfg.get("workflow") or {}
        provider = (
            str(workflow.get("knowledge_provider") or "").strip()
            or DEFAULT_KNOWLEDGE_PROVIDER
        )
        filters: dict[str, Any] = {
            "tenant_id": metadata.get("tenant_id") or ctx.tenant_id,
            "knowledge_ids": list(cfg.get("knowledge_ids") or []),
            "role": ctx.role_code or session_state.get("role_code") or cfg.get("role_code", "default"),
            "role_code": ctx.role_code,
            "workflow": workflow,
        }
        if provider:
            filters["provider"] = provider
        return filters

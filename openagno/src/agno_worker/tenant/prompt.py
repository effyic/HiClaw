"""Standard tenant prompt assembly from agno_agent configuration."""
from __future__ import annotations

from typing import Any

from agno_worker.tenant.context import TenantContext, TenantContextResolver
from agno_worker.tenant.store import AgentStore, workflow_kind, workflow_route_key


class TenantPromptBuilder:
    """Build system prompt, instructions, and knowledge filters from DB config."""

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
        kind = workflow_kind(workflow)
        route_key = workflow_route_key(workflow)
        role_label = self._resolve_role_label(cfg, route_key, business)

        system_prompt = self._build_system_prompt(ctx, cfg, state, kind, role_label, business)
        instructions = self._build_instructions(run_context, ctx, cfg, kind, route_key, role_label)
        context_filters = self._build_context_filters(run_context, ctx, cfg, state)

        return {
            "system_prompt": system_prompt,
            "instructions": instructions,
            "context_filters": context_filters,
            "agent_config": cfg,
            "business_context": business,
        }

    def _resolve_role_label(
        self,
        cfg: dict[str, Any],
        route_key: str | None,
        business: dict[str, Any],
    ) -> str:
        if name := business.get("department_name"):
            return str(name)
        if route_key:
            return str(cfg.get("display_name") or route_key)
        return str(cfg.get("display_name") or "")

    def _build_system_prompt(
        self,
        ctx: TenantContext,
        cfg: dict[str, Any],
        session_state: dict[str, Any],
        kind: str,
        role_label: str,
        business: dict[str, Any],
    ) -> str:
        base = cfg.get("system_prompt") or ""
        if kind == "triage":
            catalog = business.get("prompt_supplements", {}).get("triage_catalog")
            if not catalog:
                catalog = self._expert_catalog_prompt_block(business)
            if catalog:
                base = f"{base}\n\n{catalog}"
        elif kind == "expert" and role_label:
            expert_header = business.get("prompt_supplements", {}).get("expert_header")
            if expert_header:
                base = f"{base}\n\n{expert_header}"
            else:
                base = (
                    f"{base}\n\n当前科室：{role_label}。"
                    "请先阅读会话中分诊阶段的对话历史，再开始专科追问。"
                )
        role = session_state.get("active_role") or cfg.get("role_code", "default")
        return f"{base}\n当前角色: {role}（{kind}）。"

    def _build_instructions(
        self,
        run_context: Any,
        ctx: TenantContext,
        cfg: dict[str, Any],
        kind: str,
        route_key: str | None,
        role_label: str,
    ) -> str:
        metadata = getattr(run_context, "metadata", None) or {}
        deps = getattr(run_context, "dependencies", None) or {}
        user_profile = deps.get("user_profile") or {}
        tenant_id = user_profile.get("tenant_id") or metadata.get("tenant_id") or ctx.tenant_id
        parts = [
            cfg.get("instructions") or "",
            f"租户标识: {tenant_id}",
            f"工作流: {kind}",
        ]
        if role_label and kind == "expert":
            parts.append(f"当前科室: {role_label} ({route_key or ''})")
        kb = cfg.get("knowledge_ids") or []
        parts.append(f"可用知识库: {', '.join(kb) if kb else '（未启用）'}")
        return "\n".join(parts)

    def _build_context_filters(
        self,
        run_context: Any,
        ctx: TenantContext,
        cfg: dict[str, Any],
        session_state: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = getattr(run_context, "metadata", None) or {}
        return {
            "tenant_id": metadata.get("tenant_id") or ctx.tenant_id,
            "provider": "weknora",
            "knowledge_ids": list(cfg.get("knowledge_ids") or []),
            "role": session_state.get("active_role") or cfg.get("role_code", "default"),
            "workflow": cfg.get("workflow") or {},
        }

    def _expert_catalog_prompt_block(self, business: dict[str, Any]) -> str:
        """Build triage catalog from expert agents; hook may override via triage_departments."""
        items = business.get("triage_departments") or business.get("expert_agents") or []
        if not items:
            return "当前租户未配置任何专家 Agent，分诊阶段不得输出 departments 推荐。"
        codes = ", ".join(
            str(item.get("department_code") or item.get("route_key") or "")
            for item in items
            if item.get("department_code") or item.get("route_key")
        )
        lines = [
            "已配置专家 Agent（分诊 **只能** 从下列科室推荐，禁止推荐列表外科室）：",
            f"允许 department_code: {codes}",
        ]
        for item in items:
            code = item.get("department_code") or item.get("route_key") or ""
            name = item.get("department_name") or item.get("display_name") or code
            lines.append(
                f"- {code}: {name} "
                f"(Agent: {item.get('role_code', '')}) — {item.get('description', '')}"
            )
        return "\n".join(lines)

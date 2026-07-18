"""Build a single dynamic Agno Agent from AgentSpec role catalog + tenant pipeline."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agno_worker.agentspec.schema import AgentSpec
from agno_worker.db import create_agno_db
from agno_worker.hooks.filters import RequestFilterPipeline
from agno_worker.hooks.protocols import UserContext
from agno_worker.hooks.registry import HookRegistry
from agno_worker.api.identity import resolve_debug_request, resolve_enable_thinking, resolve_ignore_db
from agno_worker.moderation.context import (
    RequestContext as ModerationRequestContext,
    new_request_id,
    reset_request_context,
    set_request_context,
)
from agno_worker.moderation.errors import SensitivePolicyUnavailableError
from agno_worker.moderation.models import DecisionKind
from agno_worker.runtime.builder import AgentBuilder
from agno_worker.runtime.ignore_db import reset_ignore_db, set_ignore_db
from agno_worker.runtime.thinking import reset_enable_thinking, set_enable_thinking
from agno_worker.mcp.pool import clear_mcp_tools_pool
from agno_worker.tenant.service import TenantAgentService
from agno_worker.tenant.store import clear_agent_store_cache

logger = logging.getLogger(__name__)


class AgnoRuntime:
    """Materialize one dynamic Agno Agent; spec.agents is a role fallback catalog."""

    def __init__(
        self,
        spec: AgentSpec,
        db_url: str,
        *,
        db_type: str = "postgres",
        db_schema: str = "",
        db_session_table: str = "agno_sessions",
        db_create_schema: bool = True,
        hooks_dir: Path | None = None,
        registry: HookRegistry | None = None,
    ) -> None:
        self._spec = spec
        self._db_url = db_url
        self._db_type = db_type
        self._db_schema = db_schema
        self._db_session_table = db_session_table
        self._db_create_schema = db_create_schema
        self._hooks_dir = hooks_dir
        self._registry = registry or HookRegistry(hooks_dir)
        self._tenant_service = TenantAgentService(self._registry)
        self._request_filters = RequestFilterPipeline(self._registry)
        self._db: Any = None
        self._primary_agent: Any = None
        self._agents: dict[str, Any] = {}
        self._builder: AgentBuilder | None = None

    @property
    def spec(self) -> AgentSpec:
        return self._spec

    @property
    def registry(self) -> HookRegistry:
        return self._registry

    @property
    def tenant_service(self) -> TenantAgentService:
        return self._tenant_service

    @property
    def primary_agent(self) -> Any:
        return self._primary_agent

    @property
    def role_catalog(self) -> list[str]:
        return list(self._spec.agents.keys())

    def build(self) -> None:
        self._db = self._create_db()
        self._builder = AgentBuilder(
            self._registry,
            self._spec,
            self._db,
            tenant_service=self._tenant_service,
        )
        self._primary_agent = self._builder.build_dynamic_agent()
        agent_name = self._primary_agent.name
        self._agents = {agent_name: self._primary_agent}
        logger.info(
            "Dynamic agent built: name=%s roles=%s",
            agent_name,
            self.role_catalog,
        )

    def reload(self, spec: AgentSpec | None = None) -> None:
        if spec is not None:
            self._spec = spec
        self._registry.reload()
        self._tenant_service.clear_cache()
        clear_agent_store_cache()
        clear_mcp_tools_pool()
        self.build()

    def reload_hooks(self) -> None:
        self._registry.reload()
        self._tenant_service.clear_cache()
        clear_agent_store_cache()
        clear_mcp_tools_pool()
        self.build()

    @property
    def agents(self) -> dict[str, Any]:
        return self._agents

    @property
    def db(self) -> Any:
        return self._db

    def _resolve_moderation_agent_id(self, tenant_id: str, role_code: str) -> int:
        try:
            return self._tenant_service.resolve_agent_id(
                tenant_id or "default", role_code or "default"
            )
        except RuntimeError as exc:
            logger.warning("Unable to resolve moderation agent id: %s", exc)
            return 0

    def run(
        self,
        message: str,
        *,
        session_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        metadata: dict[str, Any] | None = None,
        user_context: UserContext | None = None,
    ) -> tuple[str, str]:
        return asyncio.run(
            self.arun(
                message,
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                metadata=metadata,
                user_context=user_context,
            )
        )

    async def arun(
        self,
        message: str,
        *,
        session_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        metadata: dict[str, Any] | None = None,
        user_context: UserContext | None = None,
    ) -> tuple[str, str]:
        ctx = user_context or UserContext(
            user_id=user_id,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        run_metadata = self._request_filters.apply_pre_filter(ctx, metadata)
        run_metadata["debug_request"] = resolve_debug_request(ctx.headers)
        ignore_db = resolve_ignore_db(ctx.headers)
        run_metadata["ignore_db"] = ignore_db
        enable_thinking = self._resolve_enable_thinking(ctx, run_metadata)
        run_metadata["enable_thinking"] = enable_thinking
        self._attach_request_headers(ctx, run_metadata)
        target = self._resolve_run_target()
        resolved_session_id = ctx.session_id or session_id
        kwargs = self._build_run_kwargs(
            session_id=resolved_session_id,
            user_id=ctx.user_id or user_id,
            tenant_id=ctx.tenant_id or tenant_id,
            role_code=ctx.role_code,
            metadata=run_metadata,
        )
        thinking_token = set_enable_thinking(enable_thinking)
        # 每个请求生成独立 request_id（uuid4）注入 contextvars 供敏感内容
        # Guardrail 读取；request_fingerprint 由它派生，不得从 session_id 派生
        moderation_tenant = str(
            ctx.tenant_id or tenant_id or run_metadata.get("tenant_id") or "default"
        )
        moderation_role = str(
            ctx.role_code or run_metadata.get("role_code") or "default"
        )
        mod_ctx, mod_token = set_request_context(
            tenant_id=moderation_tenant,
            agent_id=self._resolve_moderation_agent_id(moderation_tenant, moderation_role),
            user_id=ctx.user_id or user_id,
            session_id=ctx.session_id or session_id,
            request_id=new_request_id(),
        )
        ignore_token = set_ignore_db(ignore_db, session_id=resolved_session_id)
        try:
            response = await target.arun(message, **kwargs)
        finally:
            reset_enable_thinking(thinking_token)
            reset_request_context(mod_token)
            reset_ignore_db(ignore_token)
        resolved_session_id = (
            str(getattr(response, "session_id", "") or "")
            or ctx.session_id
            or session_id
        )
        # 敏感内容决策映射：agno 在内部捕获 InputCheckError 并返回错误
        # RunOutput，真正的决策由 Guardrail 写回请求上下文
        decision_reply = self._resolve_moderation_outcome(mod_ctx)
        if decision_reply is not None:
            reply = decision_reply
        elif hasattr(response, "content"):
            reply = str(response.content)
        else:
            reply = str(response)
        output = self._request_filters.apply_post_filter(
            ctx,
            {"reply": reply, "session_id": resolved_session_id},
        )
        return str(output.get("reply", reply)), str(output.get("session_id", resolved_session_id))

    @staticmethod
    def _resolve_moderation_outcome(
        mod_ctx: ModerationRequestContext,
    ) -> str | None:
        """读取 Guardrail 写回的决策：返回固定文案，或抛出映射异常。

        - 无快照 fail-closed / 正则超时 fail-closed → ``SensitivePolicyUnavailableError``
          （server 映射 503 ``sensitive_policy_unavailable``）
        - ``BLOCK_REQUEST``（及 fail-closed 的 BUSINESS_ACTION 缺处理器）→
          重新抛出决策异常（server 映射 422 ``sensitive_content_blocked``）
        - ``FIXED_REPLY`` / ``CUSTOM_RESPONSE`` / ``END_CONVERSATION`` → 返回配置文案
          （200，不调用 LLM；END_CONVERSATION 只结束当前请求，不永久关闭会话）
        - 未命中 / 放行 → None
        """
        if mod_ctx.policy_unavailable:
            raise SensitivePolicyUnavailableError()
        decision = mod_ctx.pending_decision
        if decision is None:
            return None
        if decision.kind == DecisionKind.REJECT:
            from agno_worker.moderation.guardrail import SensitiveContentDecisionError

            raise SensitiveContentDecisionError(decision)
        # respond / terminate：返回配置文案
        return str(decision.message or "")

    async def astream(
        self,
        message: str,
        *,
        session_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        metadata: dict[str, Any] | None = None,
        user_context: UserContext | None = None,
        stream_events: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        from agno.run.agent import RunEvent

        ctx = user_context or UserContext(
            user_id=user_id,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        run_metadata = self._request_filters.apply_pre_filter(ctx, metadata)
        run_metadata["debug_request"] = resolve_debug_request(ctx.headers)
        ignore_db = resolve_ignore_db(ctx.headers)
        run_metadata["ignore_db"] = ignore_db
        enable_thinking = self._resolve_enable_thinking(ctx, run_metadata)
        run_metadata["enable_thinking"] = enable_thinking
        self._attach_request_headers(ctx, run_metadata)
        # Agno only emits ReasoningContentDelta when stream_events=True.
        agno_stream_events = bool(stream_events or enable_thinking)
        target = self._resolve_run_target()
        resolved_session_id = ctx.session_id or session_id
        kwargs = self._build_run_kwargs(
            session_id=resolved_session_id,
            user_id=ctx.user_id or user_id,
            tenant_id=ctx.tenant_id or tenant_id,
            role_code=ctx.role_code,
            metadata=run_metadata,
            stream=True,
            stream_events=agno_stream_events,
        )
        final_reply_parts: list[str] = []

        thinking_token = set_enable_thinking(enable_thinking)
        # 与 arun 一致：请求级 request_id 注入 contextvars 供 Guardrail 读取
        moderation_tenant = str(
            ctx.tenant_id or tenant_id or run_metadata.get("tenant_id") or "default"
        )
        moderation_role = str(
            ctx.role_code or run_metadata.get("role_code") or "default"
        )
        mod_ctx, mod_token = set_request_context(
            tenant_id=moderation_tenant,
            agent_id=self._resolve_moderation_agent_id(moderation_tenant, moderation_role),
            user_id=ctx.user_id or user_id,
            session_id=ctx.session_id or session_id,
            request_id=new_request_id(),
        )
        ignore_token = set_ignore_db(ignore_db, session_id=resolved_session_id)
        try:
            async for event in target.arun(message, **kwargs):
                if sid := getattr(event, "session_id", None):
                    if str(sid).strip():
                        resolved_session_id = str(sid)

                event_name = str(getattr(event, "event", "") or "")

                if event_name == RunEvent.reasoning_content_delta.value:
                    if enable_thinking:
                        delta = getattr(event, "reasoning_content", None)
                        if delta is not None and str(delta):
                            yield self._agno_sse_payload(
                                "ReasoningContentDelta",
                                session_id=resolved_session_id,
                                reasoning_content=str(delta),
                            )
                    continue

                if event_name in {
                    RunEvent.reasoning_started.value,
                    RunEvent.reasoning_completed.value,
                    RunEvent.reasoning_step.value,
                }:
                    # Lifecycle markers only when thinking + stream_events.
                    if enable_thinking and stream_events:
                        yield self._agno_event_passthrough(
                            event,
                            event_name,
                            session_id=resolved_session_id,
                        )
                    continue

                if event_name == RunEvent.run_content.value:
                    # Agno dual-field standard: content + reasoning_content
                    # on the same RunContent event (Qwen puts thinking here).
                    content = getattr(event, "content", None)
                    content_str = (
                        str(content) if content is not None and str(content) else None
                    )
                    reasoning_str = None
                    if enable_thinking:
                        reasoning = getattr(event, "reasoning_content", None)
                        if reasoning is not None and str(reasoning):
                            reasoning_str = str(reasoning)
                    if content_str:
                        final_reply_parts.append(content_str)
                    if not content_str and not reasoning_str:
                        continue
                    yield {
                        "event": "RunContent",
                        "content": content_str,
                        "reasoning_content": reasoning_str,
                        "session_id": resolved_session_id or None,
                    }
                    continue

                if event_name == RunEvent.run_error.value:
                    # 敏感内容决策映射（流式）：固定/自定义/终止话术输出单个
                    # 响应事件后正常结束；阻断与策略不可用发 RunError 事件
                    moderation_events = self._moderation_stream_events(
                        mod_ctx, resolved_session_id
                    )
                    if moderation_events is not None:
                        for payload in moderation_events:
                            yield payload
                        return
                    yield {
                        "event": "RunError",
                        "content": str(
                            getattr(event, "content", None) or "run error"
                        ),
                        "session_id": resolved_session_id or None,
                    }
                    return

                if event_name == RunEvent.run_completed.value:
                    # Terminal event is synthesized after post_filter below.
                    continue

                if stream_events and event_name not in {
                    RunEvent.run_started.value,
                    RunEvent.run_content_completed.value,
                }:
                    yield self._agno_event_passthrough(
                        event,
                        event_name,
                        session_id=resolved_session_id,
                    )
        finally:
            reset_enable_thinking(thinking_token)
            reset_request_context(mod_token)
            reset_ignore_db(ignore_token)

        # 兜底：agno 未发 run_error 事件但 Guardrail 已写回决策的场景
        moderation_events = self._moderation_stream_events(mod_ctx, resolved_session_id)
        if moderation_events is not None:
            for payload in moderation_events:
                yield payload
            return

        reply = "".join(final_reply_parts)
        output = self._request_filters.apply_post_filter(
            ctx,
            {"reply": reply, "session_id": resolved_session_id},
        )
        yield {
            "event": "RunCompleted",
            "session_id": str(output.get("session_id", resolved_session_id)),
        }

    @staticmethod
    def _moderation_stream_events(
        mod_ctx: ModerationRequestContext,
        session_id: str,
    ) -> list[dict[str, Any]] | None:
        """把 Guardrail 决策映射为流式事件序列；无决策返回 None。

        - respond / terminate：单个 RunContent（配置文案）后正常 RunCompleted
        - reject：RunError ``sensitive_content_blocked``（不含敏感词与原文）
        - 策略不可用（fail-closed）：RunError ``sensitive_policy_unavailable``
        """
        if mod_ctx.policy_unavailable:
            return [
                {
                    "event": "RunError",
                    "content": "sensitive_policy_unavailable",
                    "session_id": session_id or None,
                }
            ]
        decision = mod_ctx.pending_decision
        if decision is None:
            return None
        if decision.kind == DecisionKind.REJECT:
            return [
                {
                    "event": "RunError",
                    "content": "sensitive_content_blocked",
                    "session_id": session_id or None,
                }
            ]
        return [
            {
                "event": "RunContent",
                "content": str(decision.message or ""),
                "reasoning_content": None,
                "session_id": session_id or None,
            },
            {
                "event": "RunCompleted",
                "session_id": session_id or None,
            },
        ]

    def _build_run_kwargs(
        self,
        *,
        session_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        role_code: str = "",
        metadata: dict[str, Any] | None = None,
        stream: bool = False,
        stream_events: bool = False,
    ) -> dict[str, Any]:
        run_metadata: dict[str, Any] = dict(metadata or {})
        kwargs: dict[str, Any] = {"metadata": run_metadata}
        if stream:
            kwargs["stream"] = True
        if stream_events:
            kwargs["stream_events"] = True
        if session_id:
            run_metadata["session_id"] = session_id
            kwargs["session_id"] = session_id
        if user_id:
            run_metadata["user_id"] = user_id
            kwargs["user_id"] = user_id
        if tenant_id:
            run_metadata["tenant_id"] = tenant_id
        if role_code:
            run_metadata["role_code"] = role_code
        return kwargs

    @staticmethod
    def _attach_request_headers(
        ctx: UserContext,
        run_metadata: dict[str, Any],
    ) -> None:
        """Preserve inbound HTTP headers for MCP forward / mcp_headers_hook."""
        if not ctx.headers:
            return
        run_metadata["request_headers"] = {
            str(key): str(value) for key, value in ctx.headers.items()
        }

    @staticmethod
    def _resolve_enable_thinking(
        ctx: UserContext,
        run_metadata: dict[str, Any],
    ) -> bool:
        """Prefer body/extra explicit flag (already resolved by API), else headers."""
        if "enable_thinking" in (ctx.extra or {}):
            return bool(ctx.extra["enable_thinking"])
        if "enable_thinking" in run_metadata:
            return bool(run_metadata["enable_thinking"])
        return resolve_enable_thinking(headers=ctx.headers)

    @staticmethod
    def _agno_sse_payload(
        event_name: str,
        *,
        session_id: str = "",
        **fields: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"event": event_name}
        for key, value in fields.items():
            if value is not None:
                payload[key] = value
        if session_id:
            payload["session_id"] = session_id
        return payload

    @classmethod
    def _agno_event_passthrough(
        cls,
        event: Any,
        event_name: str,
        *,
        session_id: str = "",
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if content := getattr(event, "content", None):
            fields["content"] = str(content)
        if reasoning := getattr(event, "reasoning_content", None):
            fields["reasoning_content"] = str(reasoning)
        return cls._agno_sse_payload(
            event_name,
            session_id=session_id,
            **fields,
        )

    def _resolve_run_target(self) -> Any:
        if self._primary_agent is None:
            raise RuntimeError("No Agno agent configured")
        return self._primary_agent

    def _create_db(self) -> Any:
        if not self._db_url:
            raise RuntimeError("AGNO_DB_URL is required for session persistence")
        return create_agno_db(
            db_url=self._db_url,
            db_type=self._db_type,
            db_schema=self._db_schema,
            session_table=self._db_session_table,
            create_schema=self._db_create_schema,
        )

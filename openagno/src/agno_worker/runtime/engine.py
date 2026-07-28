"""Build a single dynamic Agno Agent from AgentSpec role catalog + tenant pipeline."""
from __future__ import annotations

import asyncio
import re
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
from agno_worker.runtime.structured_output import (
    content_to_reply_text,
    join_content_segments,
    normalize_output_schema,
    prefer_last_assistant_after_tools,
)
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
            output_schema=self._resolve_output_schema(ctx),
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
        json_mode_token = self._apply_use_json_mode(target, ctx, kwargs.get("output_schema"))
        try:
            response = await target.arun(message, **kwargs)
        finally:
            self._restore_use_json_mode(target, json_mode_token)
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
        else:
            # Prefer post-hook ``content`` (already tool-deduped + COLLECTION_STATUS).
            # Re-running prefer_last_assistant_after_tools here would strip the marker
            # and any post_hook gate notes by reading raw assistant messages.
            if hasattr(response, "content") and getattr(response, "content", None) is not None:
                reply = content_to_reply_text(response.content)
            else:
                preferred = prefer_last_assistant_after_tools(response)
                reply = (
                    preferred
                    if preferred is not None
                    else content_to_reply_text(response)
                )
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
        # Always enable Agno stream_events so RunCompleted is yielded *after*
        # post_hooks (protocol compose). Without it, the iterator ends before
        # RunCompleted and SSE never receives the summary replace — only leaked
        # closing deltas. User-facing passthrough of lifecycle events still
        # respects the request ``stream_events`` flag below.
        agno_stream_events = True
        user_stream_events = bool(stream_events or enable_thinking)
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
            output_schema=self._resolve_output_schema(ctx),
        )
        content_segments: list[list[str]] = [[]]
        saw_assistant_content = False
        tools_after_content = False
        completed_content: Any = None
        completed_event: Any = None
        completed_session_state: dict[str, Any] | None = self._session_state_from_any(
            target
        )
        collection_run = self._is_collection_workflow(
            completed_session_state
        ) or self._request_uses_collection(ctx, run_metadata)
        streamed_visible = ""
        reply_delivered_at_start = False
        reply_delivered_locked = False
        if collection_run and completed_session_state:
            from agno_worker.tenant.collection.kinds.dialogue.scripts import (
                collection_state_from_session,
            )

            _coll0 = collection_state_from_session(completed_session_state) or {}
            if _coll0:
                reply_delivered_at_start = bool(_coll0.get("result_reply_delivered"))
                reply_delivered_locked = True

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
        json_mode_token = self._apply_use_json_mode(target, ctx, kwargs.get("output_schema"))
        try:
            async for event in target.arun(message, **kwargs):
                if sid := getattr(event, "session_id", None):
                    if str(sid).strip():
                        resolved_session_id = str(sid)

                event_name = str(getattr(event, "event", "") or "")
                # Prefer agent session_state (pre_hooks / tools) over sparse event
                # payloads so collection_run flips before the first patient delta.
                completed_session_state = self._merge_session_state(
                    completed_session_state,
                    self._session_state_from_any(target),
                )
                completed_session_state = self._merge_session_state(
                    completed_session_state,
                    self._session_state_from_any(event),
                )
                if self._is_collection_workflow(completed_session_state):
                    collection_run = True
                if (
                    collection_run
                    and not reply_delivered_locked
                    and completed_session_state
                ):
                    from agno_worker.tenant.collection.kinds.dialogue.scripts import (
                        collection_state_from_session,
                    )

                    _coll = collection_state_from_session(completed_session_state) or {}
                    if _coll:
                        reply_delivered_at_start = bool(
                            _coll.get("result_reply_delivered")
                        )
                        reply_delivered_locked = True

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
                    if enable_thinking and user_stream_events:
                        yield self._agno_event_passthrough(
                            event,
                            event_name,
                            session_id=resolved_session_id,
                        )
                    continue

                if event_name in {
                    RunEvent.tool_call_started.value,
                    RunEvent.tool_call_completed.value,
                    RunEvent.tool_call_error.value,
                }:
                    tools_after_content = True
                    if saw_assistant_content and content_segments[-1]:
                        content_segments.append([])
                    if event_name == RunEvent.tool_call_completed.value:
                        tool_ss = self._merge_session_state(
                            completed_session_state,
                            self._session_state_from_any(target),
                        )
                        if tool_ss:
                            completed_session_state = tool_ss
                        speech = self._protocol_patient_speech(
                            completed_session_state,
                            model_text="",
                            reply_delivered_at_start=reply_delivered_at_start,
                        )
                        for item in self._yield_patient_speech(
                            speech,
                            streamed_visible=streamed_visible,
                            session_id=resolved_session_id,
                        ):
                            if item.get("replace") and item.get("content") is not None:
                                streamed_visible = str(item["content"])
                            yield item
                    if user_stream_events:
                        yield self._agno_event_passthrough(
                            event,
                            event_name,
                            session_id=resolved_session_id,
                        )
                    continue

                if event_name == RunEvent.run_content.value:
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
                        content_segments[-1].append(content_str)
                        saw_assistant_content = True
                    if not content_str and not reasoning_str:
                        continue
                    # collection_dialogue: buffer model tokens only. Patient text
                    # is emitted once via protocol speech (tool / completed / finale).
                    if collection_run:
                        if reasoning_str is not None and enable_thinking:
                            yield {
                                "event": "RunContent",
                                "session_id": resolved_session_id or None,
                                "reasoning_content": reasoning_str,
                            }
                        continue
                    payload: dict[str, Any] = {
                        "event": "RunContent",
                        "session_id": resolved_session_id or None,
                    }
                    if content_str is not None:
                        payload["content"] = content_str
                        streamed_visible += content_str
                    if reasoning_str is not None:
                        payload["reasoning_content"] = reasoning_str
                    yield payload
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
                    completed_event = event
                    completed_content = getattr(event, "content", None)
                    meta = getattr(event, "metadata", None)
                    if isinstance(meta, dict):
                        speech_meta = str(meta.get("patient_speech") or "").strip()
                        if speech_meta:
                            completed_content = speech_meta
                    completed_session_state = self._merge_session_state(
                        completed_session_state,
                        self._session_state_from_any(event),
                    )
                    model_text = ""
                    if completed_content is not None:
                        model_text = content_to_reply_text(completed_content).strip()
                    if not model_text:
                        model_text = (
                            "".join(content_segments[-1]) if content_segments else ""
                        )
                    speech = self._protocol_patient_speech(
                        completed_session_state,
                        model_text=model_text,
                        reply_delivered_at_start=reply_delivered_at_start,
                    )
                    for item in self._yield_patient_speech(
                        speech,
                        streamed_visible=streamed_visible,
                        session_id=resolved_session_id,
                    ):
                        if item.get("replace") and item.get("content") is not None:
                            streamed_visible = str(item["content"])
                        yield item
                    continue

                if user_stream_events and event_name not in {
                    RunEvent.run_started.value,
                    RunEvent.run_content_completed.value,
                }:
                    yield self._agno_event_passthrough(
                        event,
                        event_name,
                        session_id=resolved_session_id,
                    )
        finally:
            self._restore_use_json_mode(target, json_mode_token)
            reset_enable_thinking(thinking_token)
            reset_request_context(mod_token)
            reset_ignore_db(ignore_token)

        # 兜底：agno 未发 run_error 事件但 Guardrail 已写回决策的场景
        moderation_events = self._moderation_stream_events(mod_ctx, resolved_session_id)
        if moderation_events is not None:
            for payload in moderation_events:
                yield payload
            return

        # Finale: protocol speech must reach the client even if RunCompleted
        # was skipped or mid-turn deltas were buffered.
        segments = ["".join(parts) for parts in content_segments]
        reply = join_content_segments(
            segments, tools_intervened=tools_after_content
        )
        if completed_event is not None:
            completed_content = (
                getattr(completed_event, "content", None) or completed_content
            )
            completed_session_state = self._merge_session_state(
                completed_session_state,
                self._session_state_from_any(completed_event),
            )
        post_hook_content, post_hook_ss = self._post_hook_run_snapshot(
            target, completed_event
        )
        completed_session_state = self._merge_session_state(
            completed_session_state, post_hook_ss
        )
        completed_session_state = self._merge_session_state(
            completed_session_state, self._session_state_from_any(target)
        )
        if self._is_collection_workflow(completed_session_state):
            collection_run = True

        model_text = self._strip_status_marker(
            post_hook_content
            or (
                content_to_reply_text(completed_content).strip()
                if completed_content is not None
                else ""
            )
            or str(reply or "")
        )
        speech = self._protocol_patient_speech(
            completed_session_state,
            model_text=model_text,
            reply_delivered_at_start=reply_delivered_at_start,
        )
        # Prefer post_hook/composed body for first delivery when available.
        composed = self._compose_reply_from_session(completed_session_state)
        if composed and not reply_delivered_at_start:
            from agno_worker.tenant.collection.kinds.dialogue.scripts import (
                PatientTurnSpeech,
                SPEECH_COMPOSE,
            )

            speech = PatientTurnSpeech(SPEECH_COMPOSE, composed)

        final_text = self._strip_status_marker(speech.text if speech else "")
        if final_text:
            from agno_worker.runtime.structured_output import (
                collapse_duplicate_paragraphs,
            )

            final_text = collapse_duplicate_paragraphs(final_text)
        output = self._request_filters.apply_post_filter(
            ctx,
            {"reply": final_text, "session_id": resolved_session_id},
        )
        final_text = self._strip_status_marker(
            str(output.get("reply", final_text) or "")
        )
        if composed and not reply_delivered_at_start:
            final_text = composed
        if final_text and final_text != str(streamed_visible or "").strip():
            yield {
                "event": "RunContent",
                "replace": True,
                "content": final_text,
                "session_id": str(output.get("session_id", resolved_session_id)) or None,
            }
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

    @staticmethod
    def _is_collection_workflow(session_state: dict[str, Any] | None) -> bool:
        from agno_worker.tenant.collection import is_collection_enabled

        if not isinstance(session_state, dict):
            return False
        if isinstance(session_state.get("collection"), dict):
            return True
        workflow = session_state.get("workflow") or {}
        return isinstance(workflow, dict) and is_collection_enabled(workflow)

    def _request_uses_collection(
        self,
        ctx: UserContext,
        metadata: dict[str, Any] | None,
    ) -> bool:
        """True when the active role's agent config enables collection_dialogue."""
        from agno_worker.tenant.collection import is_collection_enabled

        role = str(
            getattr(ctx, "role_code", "")
            or (metadata or {}).get("role_code")
            or ""
        ).strip()
        tenant_id = str(
            getattr(ctx, "tenant_id", "")
            or (metadata or {}).get("tenant_id")
            or ""
        ).strip()
        if not role:
            return False
        try:
            agent_config = self._tenant_service._store.load_agent_resolved(
                tenant_id, role_code=role
            )
        except Exception:
            return False
        workflow = (agent_config or {}).get("workflow") or {}
        return isinstance(workflow, dict) and is_collection_enabled(workflow)

    @staticmethod
    def _merge_session_state(
        base: dict[str, Any] | None,
        incoming: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Merge session snapshots; keep workflow/collection when incoming omits them."""
        if not incoming:
            return base
        if not base:
            return dict(incoming)
        merged = dict(base)
        for key, value in incoming.items():
            if key == "collection" and isinstance(value, dict):
                prev = merged.get("collection")
                if isinstance(prev, dict):
                    coll = dict(prev)
                    coll.update(value)
                    # Prefer richer collected maps.
                    prev_c = prev.get("collected") if isinstance(prev.get("collected"), dict) else {}
                    new_c = value.get("collected") if isinstance(value.get("collected"), dict) else {}
                    if new_c:
                        combined = dict(prev_c)
                        combined.update(new_c)
                        coll["collected"] = combined
                    merged["collection"] = coll
                else:
                    merged[key] = value
            elif key == "workflow":
                if isinstance(value, dict) and value:
                    merged[key] = value
                elif key not in merged:
                    merged[key] = value
            elif value is not None:
                merged[key] = value
        return merged

    @classmethod
    def _collection_ctx(
        cls,
        session_state: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        from agno_worker.tenant.collection import (
            is_collection_enabled,
            resolve_collection_config,
        )
        from agno_worker.tenant.collection.kinds.dialogue.scripts import (
            collection_state_from_session,
        )

        if not session_state:
            return None
        workflow = session_state.get("workflow") or {}
        if not isinstance(workflow, dict) or not is_collection_enabled(workflow):
            return None
        coll = collection_state_from_session(session_state) or {}
        return coll, resolve_collection_config(workflow)

    @classmethod
    def _compose_reply_from_session(
        cls,
        session_state: dict[str, Any] | None,
    ) -> str | None:
        """Patient text from completed user_visible reply slots only.

        Does **not** fall back to ``patient_speech`` — that field may still hold
        the previous turn's opening/question and must not overwrite this turn.
        """
        from agno_worker.tenant.collection.kinds.dialogue.scripts import (
            compose_patient_reply_progress,
        )

        ctx = cls._collection_ctx(session_state)
        if not ctx:
            return None
        coll, config = ctx
        text = compose_patient_reply_progress(coll, config)
        if not text:
            return None
        cleaned = str(text).strip()
        if "<!--COLLECTION_STATUS" in cleaned:
            cleaned = re.sub(
                r"<!--COLLECTION_STATUS\s+\{.*?\}\s*-->",
                "",
                cleaned,
                flags=re.S,
            ).strip()
        return cleaned or None

    @classmethod
    def _post_hook_run_snapshot(
        cls,
        target: Any,
        completed_event: Any = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Best-effort content + session_state after post_hook rewrote the run."""
        content = ""
        session_state: dict[str, Any] | None = None

        def _take(obj: Any) -> None:
            nonlocal content, session_state
            if obj is None:
                return
            meta = getattr(obj, "metadata", None)
            if isinstance(meta, dict):
                speech = str(meta.get("patient_speech") or "").strip()
                if speech:
                    content = speech
            raw = getattr(obj, "content", None)
            if raw is not None and str(raw).strip() and not content:
                content = content_to_reply_text(raw).strip()
            ss = getattr(obj, "session_state", None)
            if isinstance(ss, dict) and ss:
                session_state = ss

        _take(completed_event)
        for attr in (
            "run_response",
            "last_run_output",
            "_last_run_output",
            "run_output",
        ):
            _take(getattr(target, attr, None))
            if content and session_state:
                break
        if not session_state:
            session_state = cls._session_state_from_any(completed_event)
        if not session_state:
            session_state = cls._session_state_from_any(target)
        return content, session_state


    @staticmethod
    def _strip_status_marker(text: str) -> str:
        out = str(text or "").strip()
        if "<!--COLLECTION_STATUS" in out:
            out = re.sub(
                r"<!--COLLECTION_STATUS\s+\{.*?\}\s*-->",
                "",
                out,
                flags=re.S,
            ).strip()
        return out

    @classmethod
    def _protocol_patient_speech(
        cls,
        session_state: dict[str, Any] | None,
        *,
        model_text: str = "",
        reply_delivered_at_start: bool = False,
    ):
        from agno_worker.tenant.collection.kinds.dialogue.scripts import (
            PatientTurnSpeech,
            SPEECH_STREAM,
            resolve_patient_turn_speech,
        )

        ctx = cls._collection_ctx(session_state)
        if not ctx:
            return PatientTurnSpeech(SPEECH_STREAM, str(model_text or "").strip())
        coll, config = ctx
        return resolve_patient_turn_speech(
            coll,
            config,
            model_text=model_text,
            reply_delivered_at_start=reply_delivered_at_start,
        )

    @classmethod
    def _yield_patient_speech(
        cls,
        speech,
        *,
        streamed_visible: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        from agno_worker.tenant.collection.kinds.dialogue.scripts import (
            patient_sse_replace_content,
        )

        content = patient_sse_replace_content(
            speech=speech, streamed_visible=streamed_visible
        )
        if not content:
            return []
        return [
            {
                "event": "RunContent",
                "replace": True,
                "content": content,
                "session_id": session_id or None,
            }
        ]

    @classmethod
    def _should_suppress_patient_deltas(
        cls,
        session_state: dict[str, Any] | None,
    ) -> bool:
        from agno_worker.tenant.collection.kinds.dialogue.scripts import (
            should_suppress_patient_deltas,
        )

        ctx = cls._collection_ctx(session_state)
        if not ctx:
            return False
        coll, config = ctx
        return should_suppress_patient_deltas(coll, config)

    @classmethod
    def _patient_speech_replace_from_session(
        cls,
        session_state: dict[str, Any] | None,
        *,
        streamed_visible: str,
        reply_delivered_at_start: bool,
        model_text: str | None = None,
    ) -> str | None:
        from agno_worker.tenant.collection.kinds.dialogue.scripts import (
            patient_sse_replace_content,
            resolve_patient_turn_speech,
        )

        ctx = cls._collection_ctx(session_state)
        if not ctx:
            return None
        coll, config = ctx
        speech = resolve_patient_turn_speech(
            coll,
            config,
            model_text=model_text,
            reply_delivered_at_start=reply_delivered_at_start,
        )
        return patient_sse_replace_content(
            speech=speech, streamed_visible=streamed_visible
        )

    @classmethod
    def _resolve_patient_final_text(
        cls,
        session_state: dict[str, Any] | None,
        *,
        model_text: str,
        reply_delivered_at_start: bool,
        suppress_result_stream: bool,
    ) -> str:
        """Authoritative patient text for post_filter / terminal SSE."""
        from agno_worker.tenant.collection.kinds.dialogue.scripts import (
            SPEECH_COMPOSE,
            SPEECH_SILENT,
            resolve_patient_turn_speech,
            strip_premature_result_speech,
        )

        raw_model = str(model_text or "").strip()
        ctx = cls._collection_ctx(session_state)
        if not ctx:
            return raw_model
        coll, config = ctx
        speech = resolve_patient_turn_speech(
            coll,
            config,
            model_text=raw_model,
            reply_delivered_at_start=reply_delivered_at_start,
        )
        if speech.mode == SPEECH_SILENT:
            # First-delivery safety: post_hook may already have written protocol
            # compose into run content while event session_state was still stale
            # (resolve → silent). Never drop that non-empty body on this turn.
            if not reply_delivered_at_start and raw_model:
                return raw_model
            return ""
        if speech.mode == SPEECH_COMPOSE:
            return str(speech.text or "").strip() or raw_model
        text = str(speech.text or raw_model or "").strip()
        if suppress_result_stream and text:
            text = strip_premature_result_speech(text, coll, config)
        return str(text or "").strip()

    @classmethod
    def _patient_final_sse_content(
        cls,
        session_state: dict[str, Any] | None,
        *,
        final_text: str,
        streamed_visible: str,
        reply_delivered_at_start: bool,
    ) -> str | None:
        """Terminal replace content, or None to skip. Never empty-wipe."""
        from agno_worker.tenant.collection.kinds.dialogue.scripts import (
            PatientTurnSpeech,
            SPEECH_COMPOSE,
            SPEECH_SILENT,
            SPEECH_STREAM,
            patient_sse_replace_content,
            resolve_patient_turn_speech,
        )

        text = str(final_text or "").strip()
        visible = str(streamed_visible or "").strip()
        ctx = cls._collection_ctx(session_state)
        if not ctx:
            if not text or text == visible:
                return None
            return text
        coll, config = ctx
        speech = resolve_patient_turn_speech(
            coll,
            config,
            model_text=text,
            reply_delivered_at_start=reply_delivered_at_start,
        )
        if speech.mode == SPEECH_SILENT and text and not reply_delivered_at_start:
            # Align with _resolve_patient_final_text safety net.
            speech = PatientTurnSpeech(SPEECH_COMPOSE, text)
        elif speech.mode == SPEECH_STREAM and text:
            speech = PatientTurnSpeech(SPEECH_STREAM, text)
        elif speech.mode == SPEECH_COMPOSE and text:
            # Prefer post_filter-adjusted body when present.
            speech = PatientTurnSpeech(SPEECH_COMPOSE, text)
        return patient_sse_replace_content(speech=speech, streamed_visible=visible)

    @classmethod
    def _is_premature_result_chunk(
        cls,
        content_str: str,
        session_state: dict[str, Any] | None,
    ) -> bool:
        """True when chunk matches protocol closing / reply pattern / template."""
        from agno_worker.tenant.collection.kinds.dialogue.scripts import (
            looks_like_result_speech_chunk,
            reply_action_complete,
        )

        text = str(content_str or "").strip()
        ctx = cls._collection_ctx(session_state)
        if not text or not ctx:
            return False
        coll, config = ctx
        if reply_action_complete(coll, config):
            return False
        return looks_like_result_speech_chunk(text, coll, config)

    @staticmethod
    def _session_state_from_any(obj: Any) -> dict[str, Any] | None:
        from agno_worker.tenant.collection.kinds.dialogue.scripts import (
            session_state_from_run_event,
        )

        if obj is None:
            return None
        if isinstance(obj, dict) and (
            "collection" in obj or "workflow" in obj or "session_state" in obj
        ):
            if "collection" in obj or "workflow" in obj:
                return obj
            nested = obj.get("session_state")
            return nested if isinstance(nested, dict) else None
        ss = session_state_from_run_event(obj)
        if ss:
            return ss
        for attr in ("session_state", "_session_state"):
            nested = getattr(obj, attr, None)
            if isinstance(nested, dict):
                return nested
        return None

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
        output_schema: Any = None,
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
        if output_schema is not None:
            kwargs["output_schema"] = output_schema
        return kwargs

    @staticmethod
    def _resolve_output_schema(ctx: UserContext) -> Any:
        extra = ctx.extra or {}
        return normalize_output_schema(extra.get("output_schema"))

    @staticmethod
    def _resolve_use_json_mode(ctx: UserContext, output_schema: Any) -> bool | None:
        """Prefer explicit body flag; default True when schema is present."""
        if output_schema is None:
            return None
        extra = ctx.extra or {}
        if "use_json_mode" in extra:
            return bool(extra["use_json_mode"])
        return True

    @classmethod
    def _apply_use_json_mode(
        cls,
        target: Any,
        ctx: UserContext,
        output_schema: Any,
    ) -> tuple[bool, bool] | None:
        """Temporarily set Agent.use_json_mode for this run; return restore token."""
        desired = cls._resolve_use_json_mode(ctx, output_schema)
        if desired is None or not hasattr(target, "use_json_mode"):
            return None
        previous = bool(getattr(target, "use_json_mode", False))
        target.use_json_mode = desired
        return previous, True

    @staticmethod
    def _restore_use_json_mode(target: Any, token: tuple[bool, bool] | None) -> None:
        if token is None or not hasattr(target, "use_json_mode"):
            return
        previous, _applied = token
        target.use_json_mode = previous

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

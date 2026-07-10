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
from agno_worker.runtime.builder import AgentBuilder
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
        self.build()

    def reload_hooks(self) -> None:
        self._registry.reload()
        self._tenant_service.clear_cache()
        clear_agent_store_cache()
        self.build()

    @property
    def agents(self) -> dict[str, Any]:
        return self._agents

    @property
    def db(self) -> Any:
        return self._db

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
        target = self._resolve_run_target()
        kwargs = self._build_run_kwargs(
            session_id=ctx.session_id or session_id,
            user_id=ctx.user_id or user_id,
            tenant_id=ctx.tenant_id or tenant_id,
            role_code=ctx.role_code,
            metadata=run_metadata,
        )
        response = await target.arun(message, **kwargs)
        if hasattr(response, "content"):
            reply = str(response.content)
        else:
            reply = str(response)
        resolved_session_id = (
            str(getattr(response, "session_id", "") or "")
            or ctx.session_id
            or session_id
        )
        output = self._request_filters.apply_post_filter(
            ctx,
            {"reply": reply, "session_id": resolved_session_id},
        )
        return str(output.get("reply", reply)), str(output.get("session_id", resolved_session_id))

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
        target = self._resolve_run_target()
        kwargs = self._build_run_kwargs(
            session_id=ctx.session_id or session_id,
            user_id=ctx.user_id or user_id,
            tenant_id=ctx.tenant_id or tenant_id,
            role_code=ctx.role_code,
            metadata=run_metadata,
            stream=True,
            stream_events=stream_events,
        )
        resolved_session_id = ctx.session_id or session_id
        final_reply_parts: list[str] = []

        async for event in target.arun(message, **kwargs):
            if sid := getattr(event, "session_id", None):
                if str(sid).strip():
                    resolved_session_id = str(sid)

            event_name = str(getattr(event, "event", "") or "")

            if event_name == RunEvent.run_content.value:
                content = getattr(event, "content", None)
                if content is not None and str(content):
                    text = str(content)
                    final_reply_parts.append(text)
                    yield {"event": "content", "delta": text}
                continue

            if event_name == RunEvent.run_error.value:
                yield {
                    "event": "error",
                    "message": str(getattr(event, "content", None) or "run error"),
                }
                return

            if stream_events and event_name not in {
                RunEvent.run_started.value,
                RunEvent.run_content_completed.value,
                RunEvent.run_completed.value,
            }:
                payload: dict[str, Any] = {
                    "event": "agent",
                    "agent_event": event_name,
                }
                if content := getattr(event, "content", None):
                    payload["content"] = str(content)
                yield payload

        reply = "".join(final_reply_parts)
        output = self._request_filters.apply_post_filter(
            ctx,
            {"reply": reply, "session_id": resolved_session_id},
        )
        yield {"event": "done", "session_id": str(output.get("session_id", resolved_session_id))}

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

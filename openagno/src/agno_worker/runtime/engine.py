"""Build a single dynamic Agno Agent from AgentSpec role catalog + hooks."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agno_worker.agentspec.schema import AgentSpec
from agno_worker.db import create_agno_db
from agno_worker.hooks.registry import HookRegistry
from agno_worker.runtime.builder import AgentBuilder

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
        self._db: Any = None
        self._primary_agent: Any = None
        self._agents: dict[str, Any] = {}
        self._team: Any = None
        self._workflow: Any = None
        self._builder: AgentBuilder | None = None

    @property
    def spec(self) -> AgentSpec:
        return self._spec

    @property
    def registry(self) -> HookRegistry:
        return self._registry

    @property
    def primary_agent(self) -> Any:
        return self._primary_agent

    @property
    def role_catalog(self) -> list[str]:
        return list(self._spec.agents.keys())

    def build(self) -> None:
        self._db = self._create_db()
        self._builder = AgentBuilder(self._registry, self._spec, self._db)
        self._primary_agent = self._builder.build_dynamic_agent()
        agent_name = self._primary_agent.name
        self._agents = {agent_name: self._primary_agent}
        if self._spec.team:
            self._team = self._create_team()
        if self._spec.workflow:
            self._workflow = self._create_workflow()
        logger.info(
            "Dynamic agent built: name=%s roles=%s",
            agent_name,
            self.role_catalog,
        )

    def reload(self, spec: AgentSpec | None = None) -> None:
        if spec is not None:
            self._spec = spec
        self._registry.reload()
        self.build()

    def reload_hooks(self) -> None:
        self._registry.reload()
        self.build()

    @property
    def team(self) -> Any:
        return self._team

    @property
    def agents(self) -> dict[str, Any]:
        return self._agents

    @property
    def workflow(self) -> Any:
        return self._workflow

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
    ) -> tuple[str, str]:
        """Sync wrapper; MCP tools require the async agent run path."""
        return asyncio.run(
            self.arun(
                message,
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                metadata=metadata,
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
    ) -> tuple[str, str]:
        target = self._resolve_run_target()
        kwargs = self._build_run_kwargs(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            metadata=metadata,
        )
        response = await target.arun(message, **kwargs)
        if hasattr(response, "content"):
            reply = str(response.content)
        else:
            reply = str(response)
        resolved_session_id = (
            str(getattr(response, "session_id", "") or "")
            or session_id
        )
        return reply, resolved_session_id

    async def astream(
        self,
        message: str,
        *,
        session_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        metadata: dict[str, Any] | None = None,
        stream_events: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream Agno run events as JSON-serializable dicts for SSE consumers."""
        from agno.run.agent import RunEvent

        target = self._resolve_run_target()
        kwargs = self._build_run_kwargs(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            metadata=metadata,
            stream=True,
            stream_events=stream_events,
        )
        resolved_session_id = session_id

        async for event in target.arun(message, **kwargs):
            if sid := getattr(event, "session_id", None):
                if str(sid).strip():
                    resolved_session_id = str(sid)

            event_name = str(getattr(event, "event", "") or "")

            if event_name == RunEvent.run_content.value:
                content = getattr(event, "content", None)
                if content is not None and str(content):
                    yield {"event": "content", "delta": str(content)}
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

        yield {"event": "done", "session_id": resolved_session_id}

    def _build_run_kwargs(
        self,
        *,
        session_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
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
        return kwargs

    def _resolve_run_target(self) -> Any:
        """Return the executor for chat runs.

        Team/workflow blocks in AgentSpec are parsed but not fully orchestrated yet;
        always use the single dynamic agent to avoid silently broken Team routing.
        """
        if self._primary_agent is None:
            raise RuntimeError("No Agno agent configured")
        if self._team is not None:
            logger.debug(
                "AgentSpec defines team mode=%s; using dynamic agent until multi-agent orchestration is wired",
                getattr(self._spec.team, "mode", ""),
            )
        if self._workflow is not None and self._spec.workflow and self._spec.workflow.steps:
            logger.debug(
                "AgentSpec defines workflow steps=%d; using dynamic agent until workflow engine is wired",
                len(self._spec.workflow.steps),
            )
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

    def _create_team(self) -> Any:
        from agno.team import Team

        team_def = self._spec.team
        assert team_def is not None
        members = [self._primary_agent] if self._primary_agent else []
        return Team(
            name=self._spec.name or "orchestrator",
            members=members,
            instructions=team_def.instructions,
            db=self._db,
            add_history_to_context=True,
        )

    def _create_workflow(self) -> Any:
        from agno.workflow import Workflow

        wf = self._spec.workflow
        assert wf is not None
        return Workflow(name=wf.name or "workflow", db=self._db)

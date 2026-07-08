"""Build a single dynamic Agno Agent from AgentSpec role catalog + hooks."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agno_worker.agentspec.schema import AgentSpec
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
        hooks_dir: Path | None = None,
        registry: HookRegistry | None = None,
    ) -> None:
        self._spec = spec
        self._db_url = db_url or spec.db.url
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
        session_id: str,
        user_id: str = "",
        tenant_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        target = self._resolve_run_target()
        run_metadata: dict[str, Any] = {"session_id": session_id, **(metadata or {})}
        if user_id:
            run_metadata["user_id"] = user_id
        if tenant_id:
            run_metadata["tenant_id"] = tenant_id
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "metadata": run_metadata,
        }
        if user_id:
            kwargs["user_id"] = user_id
        response = target.run(message, **kwargs)
        if hasattr(response, "content"):
            return str(response.content)
        return str(response)

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
            raise RuntimeError("AGNO_DB_URL or spec.db.url is required for session persistence")
        db_type = self._spec.db.type.lower()
        if db_type in ("postgres", "postgresql"):
            from agno.db.postgres import PostgresDb

            return PostgresDb(
                db_url=self._db_url,
                session_table=self._spec.db.session_table,
            )
        if db_type == "mysql":
            from agno.db.mysql import MySQLDb

            return MySQLDb(db_url=self._db_url, session_table=self._spec.db.session_table)
        if db_type == "sqlite":
            from agno.db.sqlite import SqliteDb

            return SqliteDb(db_file=self._db_url.removeprefix("sqlite:///"))
        raise RuntimeError(f"Unsupported database type: {db_type}")

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

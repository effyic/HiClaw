"""Build Agno Agent / Team / Workflow from AgentSpec and dynamic hooks."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from agno_worker.agentspec.schema import AgentSpec
from agno_worker.hooks.registry import HookRegistry
from agno_worker.runtime.builder import AgentBuilder

logger = logging.getLogger(__name__)


class AgnoRuntime:
    """Materialize Agno runtime objects from AgentSpec and hook registry."""

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

    def build(self) -> None:
        self._db = self._create_db()
        self._builder = AgentBuilder(self._registry, self._spec, self._db)
        self._agents = {
            name: self._builder.build_agent(defn) for name, defn in self._spec.agents.items()
        }
        if self._spec.team:
            self._team = self._create_team()
        if self._spec.workflow:
            self._workflow = self._create_workflow()

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
        metadata: dict[str, Any] | None = None,
    ) -> str:
        target = self._team or next(iter(self._agents.values()), None)
        if target is None:
            raise RuntimeError("No Agno agent or team configured")
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "metadata": {"session_id": session_id, **(metadata or {})},
        }
        if user_id:
            kwargs["user_id"] = user_id
        response = target.run(message, **kwargs)
        if hasattr(response, "content"):
            return str(response.content)
        return str(response)

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
        members = [self._agents[name] for name in team_def.members if name in self._agents]
        leader = self._agents.get(team_def.leader) if team_def.leader else None
        return Team(
            name=self._spec.name or "orchestrator",
            members=members,
            leader=leader,
            instructions=team_def.instructions,
            db=self._db,
            add_history_to_context=True,
        )

    def _create_workflow(self) -> Any:
        from agno.workflow import Workflow

        wf = self._spec.workflow
        assert wf is not None
        return Workflow(name=wf.name or "workflow", db=self._db)

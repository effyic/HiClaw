"""Build Agno Agent / Team / Workflow from AgentSpec."""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from agno_worker.agentspec.schema import AgentDef, AgentSpec

logger = logging.getLogger(__name__)


class AgnoRuntime:
    """Materialize Agno runtime objects from a parsed AgentSpec."""

    def __init__(self, spec: AgentSpec, db_url: str) -> None:
        self._spec = spec
        self._db_url = db_url or spec.db.url
        self._db: Any = None
        self._agents: dict[str, Any] = {}
        self._team: Any = None
        self._workflow: Any = None

    @property
    def spec(self) -> AgentSpec:
        return self._spec

    def build(self) -> None:
        self._db = self._create_db()
        self._agents = {name: self._create_agent(defn) for name, defn in self._spec.agents.items()}
        if self._spec.team:
            self._team = self._create_team()
        if self._spec.workflow:
            self._workflow = self._create_workflow()

    def reload(self, spec: AgentSpec) -> None:
        self._spec = spec
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

    def run(self, message: str, *, session_id: str, user_id: str = "") -> str:
        target = self._team or next(iter(self._agents.values()), None)
        if target is None:
            raise RuntimeError("No Agno agent or team configured")
        kwargs: dict[str, Any] = {"session_id": session_id}
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
            from agno.db.mysql import MysqlDb

            return MysqlDb(db_url=self._db_url, session_table=self._spec.db.session_table)
        raise RuntimeError(f"Unsupported database type: {db_type}")

    def _create_agent(self, defn: AgentDef) -> Any:
        from agno.agent import Agent

        model = self._resolve_model(defn.model or self._spec.model)
        instructions = self._build_instructions(defn)
        return Agent(
            name=defn.name,
            model=model,
            instructions=instructions,
            db=self._db,
            add_history_to_context=True,
            markdown=True,
        )

    def _resolve_model(self, model_id: str) -> Any:
        """Route LLM calls through HiClaw AI gateway when env vars are set."""
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

    def _build_instructions(self, defn: AgentDef) -> str:
        parts = [defn.instructions] if defn.instructions else []
        if defn.knowledge and defn.knowledge.knowledge_id:
            parts.append(
                f"Use knowledge base provider={defn.knowledge.provider} "
                f"knowledge_id={defn.knowledge.knowledge_id} for retrieval."
            )
        return "\n\n".join(p for p in parts if p)

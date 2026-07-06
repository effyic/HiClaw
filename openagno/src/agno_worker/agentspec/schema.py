"""AgentSpec YAML schema for medical orchestration and general multi-agent setups."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import yaml


@dataclass
class KnowledgeRef:
    provider: str = "weknora"
    knowledge_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentDef:
    name: str
    role: str = ""
    instructions: str = ""
    model: str = ""
    knowledge: Optional[KnowledgeRef] = None
    tools: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TeamDef:
    mode: str = "route"  # route | coordinate | collaborate
    leader: str = ""
    members: list[str] = field(default_factory=list)
    instructions: str = ""


@dataclass
class WorkflowStep:
    name: str
    agent: str = ""
    action: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowDef:
    name: str = ""
    steps: list[WorkflowStep] = field(default_factory=list)


@dataclass
class DatabaseDef:
    type: str = "postgres"
    url: str = ""
    session_table: str = "agno_sessions"


@dataclass
class AgentSpec:
    api_version: str = "hiclaw.agno/v1"
    runtime: str = "agno"
    name: str = ""
    description: str = ""
    model: str = ""
    db: DatabaseDef = field(default_factory=DatabaseDef)
    agents: dict[str, AgentDef] = field(default_factory=dict)
    team: Optional[TeamDef] = None
    workflow: Optional[WorkflowDef] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        import hashlib

        payload = yaml.dump(
            {
                "agents": {k: _agent_to_dict(v) for k, v in self.agents.items()},
                "team": _team_to_dict(self.team) if self.team else None,
                "workflow": _workflow_to_dict(self.workflow) if self.workflow else None,
                "model": self.model,
                "db": _db_to_dict(self.db),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def parse_agentspec_yaml(text: str) -> AgentSpec:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError("AgentSpec must be a YAML mapping")

    db_raw = raw.get("db") or {}
    db = DatabaseDef(
        type=str(db_raw.get("type", "postgres")),
        url=str(db_raw.get("url", "")),
        session_table=str(db_raw.get("session_table", "agno_sessions")),
    )

    agents: dict[str, AgentDef] = {}
    for name, cfg in (raw.get("agents") or {}).items():
        if not isinstance(cfg, dict):
            continue
        kn_raw = cfg.get("knowledge")
        knowledge = None
        if isinstance(kn_raw, dict):
            knowledge = KnowledgeRef(
                provider=str(kn_raw.get("provider", "weknora")),
                knowledge_id=str(kn_raw.get("knowledge_id", "")),
                metadata=dict(kn_raw.get("metadata") or {}),
            )
        agents[name] = AgentDef(
            name=name,
            role=str(cfg.get("role", "")),
            instructions=str(cfg.get("instructions", "")),
            model=str(cfg.get("model", "")),
            knowledge=knowledge,
            tools=[str(t) for t in (cfg.get("tools") or [])],
            metadata=dict(cfg.get("metadata") or {}),
        )

    team = None
    if isinstance(raw.get("team"), dict):
        t = raw["team"]
        team = TeamDef(
            mode=str(t.get("mode", "route")),
            leader=str(t.get("leader", "")),
            members=[str(m) for m in (t.get("members") or [])],
            instructions=str(t.get("instructions", "")),
        )

    workflow = None
    if isinstance(raw.get("workflow"), dict):
        w = raw["workflow"]
        steps = []
        for step in w.get("steps") or []:
            if not isinstance(step, dict):
                continue
            steps.append(
                WorkflowStep(
                    name=str(step.get("name", "")),
                    agent=str(step.get("agent", "")),
                    action=str(step.get("action", "")),
                    metadata=dict(step.get("metadata") or {}),
                )
            )
        workflow = WorkflowDef(name=str(w.get("name", "")), steps=steps)

    return AgentSpec(
        api_version=str(raw.get("apiVersion", "hiclaw.agno/v1")),
        runtime=str(raw.get("runtime", "agno")),
        name=str(raw.get("name", "")),
        description=str(raw.get("description", "")),
        model=str(raw.get("model", "")),
        db=db,
        agents=agents,
        team=team,
        workflow=workflow,
        metadata=dict(raw.get("metadata") or {}),
    )


def _agent_to_dict(agent: AgentDef) -> dict[str, Any]:
    out: dict[str, Any] = {
        "role": agent.role,
        "instructions": agent.instructions,
        "model": agent.model,
        "tools": agent.tools,
    }
    if agent.knowledge:
        out["knowledge"] = {
            "provider": agent.knowledge.provider,
            "knowledge_id": agent.knowledge.knowledge_id,
        }
    return out


def _team_to_dict(team: TeamDef) -> dict[str, Any]:
    return {
        "mode": team.mode,
        "leader": team.leader,
        "members": team.members,
        "instructions": team.instructions,
    }


def _workflow_to_dict(workflow: WorkflowDef) -> dict[str, Any]:
    return {
        "name": workflow.name,
        "steps": [
            {"name": s.name, "agent": s.agent, "action": s.action} for s in workflow.steps
        ],
    }


def _db_to_dict(db: DatabaseDef) -> dict[str, Any]:
    return {"type": db.type, "url": db.url, "session_table": db.session_table}

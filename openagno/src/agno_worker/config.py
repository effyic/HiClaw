"""Worker configuration from environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agno_worker.db import default_db_schema, infer_db_type, parse_db_url


@dataclass(frozen=True)
class WorkerConfig:
    worker_name: str
    agentspec_dir: Path
    hooks_dir: Path
    db_url: str
    agent_db_url: str = ""
    db_type: str = "postgres"
    db_schema: str = "public"
    db_session_table: str = "agno_sessions"
    db_create_schema: bool = True
    api_port: int = 8090
    api_bind: str = "0.0.0.0"
    watch_interval: int = 30
    enable_agentos: bool = False
    enable_session_api: bool = True
    require_tenant_id: bool = False

    @classmethod
    def from_env(cls, worker_name: str) -> WorkerConfig:
        enable_agentos = os.environ.get("AGNO_ENABLE_AGENTOS", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        enable_session_api = os.environ.get("AGNO_ENABLE_SESSION_API", "true").lower() in (
            "1",
            "true",
            "yes",
        )
        db_url = os.environ.get(
            "AGNO_DB_URL",
            "postgresql+psycopg://root:vector_store@localhost:5432/postgres",
        )
        parsed = parse_db_url(db_url)
        db_type = os.environ.get("AGNO_DB_TYPE", "") or infer_db_type(db_url)
        db_schema = os.environ.get("AGNO_DB_SCHEMA", "") or default_db_schema(
            db_type, parsed.database
        )
        db_create_schema = os.environ.get("AGNO_DB_CREATE_SCHEMA", "true").lower() in (
            "1",
            "true",
            "yes",
        )
        agent_db_url = os.environ.get("AGNO_AGENT_DB_URL", "").strip()
        require_tenant_id = os.environ.get("AGNO_REQUIRE_TENANT_ID", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        return cls(
            worker_name=worker_name,
            agentspec_dir=Path(
                os.environ.get("AGNO_AGENTSPEC_DIR", "/etc/hiclaw/agentspec")
            ),
            hooks_dir=Path(os.environ.get("AGNO_HOOKS_DIR", "/etc/hiclaw/hooks")),
            db_url=db_url,
            agent_db_url=agent_db_url,
            db_type=db_type,
            db_schema=db_schema,
            db_session_table=os.environ.get("AGNO_DB_SESSION_TABLE", "agno_sessions"),
            db_create_schema=db_create_schema,
            api_port=int(os.environ.get("AGNO_CONTROL_PORT", "8090")),
            api_bind=os.environ.get("AGNO_CONTROL_BIND", "0.0.0.0"),
            watch_interval=int(os.environ.get("AGNO_SPEC_WATCH_INTERVAL", "30")),
            enable_agentos=enable_agentos,
            enable_session_api=enable_session_api,
            require_tenant_id=require_tenant_id,
        )

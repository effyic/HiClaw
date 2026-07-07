"""Worker configuration from environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkerConfig:
    worker_name: str
    agentspec_dir: Path
    db_url: str
    api_port: int = 8090
    api_bind: str = "0.0.0.0"
    watch_interval: int = 30
    enable_agentos: bool = False

    @classmethod
    def from_env(cls, worker_name: str) -> WorkerConfig:
        enable_agentos = os.environ.get("AGNO_ENABLE_AGENTOS", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        return cls(
            worker_name=worker_name,
            agentspec_dir=Path(
                os.environ.get("AGNO_AGENTSPEC_DIR", "/etc/hiclaw/agentspec")
            ),
            db_url=os.environ.get(
                "AGNO_DB_URL",
                "postgresql+psycopg://root:vector_store@localhost:5432/postgres",
            ),
            api_port=int(os.environ.get("AGNO_CONTROL_PORT", "8090")),
            api_bind=os.environ.get("AGNO_CONTROL_BIND", "0.0.0.0"),
            watch_interval=int(os.environ.get("AGNO_SPEC_WATCH_INTERVAL", "30")),
            enable_agentos=enable_agentos,
        )

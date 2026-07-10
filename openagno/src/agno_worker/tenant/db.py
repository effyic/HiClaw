"""Shared SQLAlchemy connection pool for tenant configuration (agno_agent table)."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import unquote

_engine: Any = None


def agent_db_url() -> str:
    """PostgreSQL URL for agno_agent tenant configuration."""
    url = (
        os.environ.get("AGNO_AGENT_DB_URL", "").strip()
        or os.environ.get("AGNO_DB_URL", "").strip()
    )
    if not url:
        raise RuntimeError("AGNO_AGENT_DB_URL or AGNO_DB_URL is required")
    return url


def agent_db_driver() -> str:
    """Return normalized driver name: postgres | mysql | …"""
    from sqlalchemy.engine import make_url

    parsed = make_url(agent_db_url())
    driver = parsed.drivername.split("+", 1)[0]
    if driver == "postgresql":
        return "postgres"
    return driver


def agent_db_settings() -> dict[str, Any]:
    """Parse agent DB URL for connection kwargs."""
    from sqlalchemy.engine import make_url

    parsed = make_url(agent_db_url())
    driver = agent_db_driver()
    if driver not in ("postgres", "mysql"):
        raise RuntimeError(
            f"Agent config DB must use PostgreSQL or MySQL (agno_agent table), got: {driver}. "
            "Set AGNO_AGENT_DB_URL=postgresql+psycopg://..."
        )
    if not parsed.database:
        raise RuntimeError("AGNO_AGENT_DB_URL must include a database name")
    settings: dict[str, Any] = {
        "host": parsed.host or "localhost",
        "port": int(parsed.port or (5432 if driver == "postgres" else 3306)),
        "user": unquote(parsed.username or "root"),
        "password": unquote(parsed.password or ""),
        "database": parsed.database,
        "driver": driver,
    }
    if driver == "mysql":
        settings["charset"] = "utf8mb4"
    return settings


def mysql_settings() -> dict[str, Any]:
    """Backward-compatible alias for agent_db_settings()."""
    return agent_db_settings()


def get_engine() -> Any:
    """Return a process-wide SQLAlchemy engine with connection pooling."""
    global _engine
    if _engine is None:
        from sqlalchemy import create_engine

        _engine = create_engine(
            agent_db_url(),
            pool_size=int(os.environ.get("AGNO_AGENT_DB_POOL_SIZE", "5")),
            max_overflow=int(os.environ.get("AGNO_AGENT_DB_POOL_OVERFLOW", "10")),
            pool_pre_ping=True,
            pool_recycle=int(os.environ.get("AGNO_AGENT_DB_POOL_RECYCLE", "3600")),
        )
    return _engine


def reset_engine() -> None:
    """Dispose pooled connections (e.g. on worker reload)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


@contextmanager
def agent_db_connection() -> Iterator[Any]:
    """Borrow a SQLAlchemy connection from the pool; returns it on exit."""
    conn = get_engine().connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

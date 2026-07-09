"""Database URL parsing and schema resolution for Agno persistence."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedDbUrl:
    driver: str
    database: str
    url: str


def infer_db_type(db_url: str) -> str:
    lowered = db_url.lower()
    if lowered.startswith("postgresql") or lowered.startswith("postgres"):
        return "postgres"
    if lowered.startswith("mysql"):
        return "mysql"
    if lowered.startswith("sqlite"):
        return "sqlite"
    scheme = db_url.split(":", 1)[0]
    raise ValueError(f"Unsupported AGNO_DB_URL driver: {scheme}")


def parse_db_url(db_url: str) -> ParsedDbUrl:
    from sqlalchemy.engine import make_url

    parsed = make_url(db_url)
    driver = parsed.drivername.split("+", 1)[0]
    if driver == "postgresql":
        driver = "postgres"
    return ParsedDbUrl(
        driver=driver,
        database=parsed.database or "",
        url=db_url,
    )


def default_db_schema(db_type: str, database: str) -> str:
    """Agno defaults to schema/database ``ai``; align with AGNO_DB_URL instead."""
    if db_type == "mysql":
        if not database:
            raise ValueError("AGNO_DB_URL must include a database name for MySQL")
        return database
    if db_type == "postgres":
        return "public"
    return ""


def create_agno_db(
    *,
    db_url: str,
    db_type: str,
    db_schema: str,
    session_table: str,
    create_schema: bool,
) -> object:
    db_type = db_type.lower()
    if db_type in ("postgres", "postgresql"):
        from agno.db.postgres import PostgresDb

        return PostgresDb(
            db_url=db_url,
            db_schema=db_schema or None,
            session_table=session_table,
            create_schema=create_schema,
        )
    if db_type == "mysql":
        from agno.db.mysql import MySQLDb

        return MySQLDb(
            db_url=db_url,
            db_schema=db_schema or None,
            session_table=session_table,
            create_schema=create_schema,
        )
    if db_type == "sqlite":
        from agno.db.sqlite import SqliteDb

        return SqliteDb(db_file=db_url.removeprefix("sqlite:///"))
    raise RuntimeError(f"Unsupported database type: {db_type}")

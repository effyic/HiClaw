"""Load tenant agent configuration from PostgreSQL agno_agent table."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from agno_worker.tenant.cache import TTLCache, _MISSING
from agno_worker.tenant.db import agent_db_connection, agent_db_url, reset_engine

DEFAULT_ROW: dict[str, Any] = {
    "tenant_id": "default",
    "role_code": "default",
    "workflow": {},
    "display_name": "默认租户",
    "description": "",
    "system_prompt": "你是默认租户助手。",
    "instructions": "提供通用帮助。",
    "knowledge_ids": [],
    "mcp_enabled": False,
    "mcp_config": None,
}

_AGENT_SELECT = """
    SELECT tenant_id, role_code,
           display_name, description, system_prompt, instructions,
           knowledge_ids, mcp_enabled, mcp_config, workflow
    FROM agno_agent
    WHERE enabled IS TRUE
"""


def _row_mapping(row: Any) -> dict[str, Any]:
    return dict(row)


def _fetch_one(conn: Any, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    result = conn.execute(text(sql), params)
    row = result.mappings().first()
    return _row_mapping(row) if row else None


def _fetch_all(conn: Any, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = conn.execute(text(sql), params or {})
    return [_row_mapping(row) for row in result.mappings()]


def _parse_json_object(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def parse_workflow(raw: Any) -> dict[str, Any]:
    return _parse_json_object(raw)


def workflow_phase(workflow: dict[str, Any] | None) -> str | None:
    phase = (workflow or {}).get("phase")
    return str(phase).strip() if phase else None


def _parse_knowledge_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return []


def _row_to_config(row: dict[str, Any], tenant_id: str | None = None) -> dict[str, Any]:
    mcp_config = row.get("mcp_config")
    if isinstance(mcp_config, str):
        try:
            mcp_config = json.loads(mcp_config)
        except json.JSONDecodeError:
            mcp_config = None
    workflow = parse_workflow(row.get("workflow"))
    tid = row.get("tenant_id", tenant_id or "default")
    return {
        "tenant_id": str(tid),
        "role_code": str(row.get("role_code") or "default"),
        "workflow": workflow,
        "display_name": row.get("display_name") or row.get("tenant_id", ""),
        "description": row.get("description") or "",
        "system_prompt": row.get("system_prompt") or "",
        "instructions": row.get("instructions") or "",
        "knowledge_ids": _parse_knowledge_ids(row.get("knowledge_ids")),
        "mcp_enabled": bool(row.get("mcp_enabled")),
        "mcp_config": mcp_config,
    }


class AgentStore:
    """PostgreSQL-backed tenant agent configuration store (agno_agent table only)."""

    def __init__(self) -> None:
        self._cache = TTLCache()

    def clear_cache(self) -> None:
        self._cache.clear()

    def load_agent(self, tenant_id: str, role_code: str = "default") -> dict[str, Any]:
        return self.load_agent_resolved(tenant_id, role_code=role_code)

    def load_agent_resolved(
        self,
        tenant_id: str,
        *,
        role_code: str = "default",
    ) -> dict[str, Any]:
        cache_key = f"agent:{tenant_id}:{role_code}"
        cached = self._cache.get(cache_key)
        if cached is not _MISSING:
            return cached

        try:
            config = _load_agent_resolved_uncached(tenant_id, role_code=role_code)
        except Exception as exc:
            raise RuntimeError(
                f"connect agent database failed ({agent_db_url()}): {exc}"
            ) from exc

        self._cache.set(cache_key, config)
        return config

    def list_enabled_tenants(self) -> list[str]:
        return list_enabled_tenants()

    def list_agents(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        return list_agents(tenant_id)


def _load_agent_resolved_uncached(
    tenant_id: str,
    *,
    role_code: str = "default",
) -> dict[str, Any]:
    with agent_db_connection() as conn:
        row = None
        if role_code:
            row = _fetch_one(
                conn,
                f"{_AGENT_SELECT} AND tenant_id = :tenant_id AND role_code = :role_code LIMIT 1",
                {"tenant_id": tenant_id, "role_code": role_code},
            )

        if row is None and role_code != "default":
            row = _fetch_one(
                conn,
                f"{_AGENT_SELECT} AND tenant_id = :tenant_id AND role_code = 'default' LIMIT 1",
                {"tenant_id": tenant_id},
            )

    if row is None and tenant_id != "default":
        return _load_agent_resolved_uncached("default", role_code="default")

    if row is None:
        return _row_to_config(dict(DEFAULT_ROW))

    return _row_to_config(row, tenant_id=tenant_id)


def list_enabled_tenants() -> list[str]:
    with agent_db_connection() as conn:
        rows = _fetch_all(
            conn,
            """
            SELECT DISTINCT tenant_id
            FROM agno_agent
            WHERE enabled IS TRUE
            ORDER BY tenant_id
            """,
        )
        return [str(row["tenant_id"]) for row in rows]


def list_agents(tenant_id: str | None = None) -> list[dict[str, Any]]:
    with agent_db_connection() as conn:
        if tenant_id:
            rows = _fetch_all(
                conn,
                """
                SELECT tenant_id, role_code, display_name, description, workflow
                FROM agno_agent
                WHERE enabled IS TRUE AND tenant_id = :tenant_id
                ORDER BY role_code
                """,
                {"tenant_id": tenant_id},
            )
        else:
            rows = _fetch_all(
                conn,
                """
                SELECT tenant_id, role_code, display_name, description, workflow
                FROM agno_agent
                WHERE enabled IS TRUE
                ORDER BY tenant_id, role_code
                """,
            )
        return [
            {
                "tenant_id": str(row["tenant_id"]),
                "role_code": str(row.get("role_code") or "default"),
                "workflow": parse_workflow(row.get("workflow")),
                "display_name": str(row.get("display_name") or row["tenant_id"]),
                "description": str(row.get("description") or ""),
            }
            for row in rows
        ]


def clear_agent_store_cache() -> None:
    """Clear config TTL cache and reset DB pool (call on worker reload)."""
    reset_engine()

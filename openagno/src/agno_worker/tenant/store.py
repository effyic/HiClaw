"""Load tenant agent configuration from MySQL agno_agent table."""
from __future__ import annotations

import json
from typing import Any

import pymysql.cursors

from agno_worker.tenant.cache import TTLCache, _MISSING
from agno_worker.tenant.db import agent_db_connection, agent_db_url, mysql_settings, reset_engine

DEFAULT_WORKFLOW: dict[str, Any] = {"kind": "default"}

DEFAULT_ROW: dict[str, Any] = {
    "tenant_id": "default",
    "role_code": "default",
    "workflow": dict(DEFAULT_WORKFLOW),
    "display_name": "默认租户",
    "description": "",
    "system_prompt": "你是默认租户助手。",
    "instructions": "提供通用帮助。",
    "knowledge_ids": ["weknora-kb-general"],
    "mcp_enabled": False,
    "mcp_config": None,
}

_AGENT_SELECT = """
    SELECT tenant_id, role_code,
           display_name, description, system_prompt, instructions,
           knowledge_ids, mcp_enabled, mcp_config, workflow
    FROM agno_agent
    WHERE enabled = 1
"""


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
    workflow = _parse_json_object(raw)
    kind = str(workflow.get("kind") or "default").strip() or "default"
    workflow["kind"] = kind
    return workflow


def workflow_kind(workflow: dict[str, Any] | None) -> str:
    return str((workflow or {}).get("kind") or "default")


def workflow_route_key(workflow: dict[str, Any] | None) -> str | None:
    key = (workflow or {}).get("route_key")
    return str(key).strip() if key else None


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


def _expert_agent_summary(row: dict[str, Any], tenant_id: str) -> dict[str, Any] | None:
    workflow = parse_workflow(row.get("workflow"))
    route_key = workflow_route_key(workflow)
    if not route_key:
        return None
    return {
        "tenant_id": tenant_id,
        "role_code": str(row.get("role_code") or ""),
        "route_key": route_key,
        "display_name": str(row.get("display_name") or route_key),
        "description": str(row.get("description") or ""),
        "workflow": workflow,
    }


class AgentStore:
    """MySQL-backed tenant agent configuration store (agno_agent table only)."""

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
        kind: str | None = None,
        route_key: str | None = None,
    ) -> dict[str, Any]:
        cache_key = f"agent:{tenant_id}:{role_code}:{kind}:{route_key}"
        cached = self._cache.get(cache_key)
        if cached is not _MISSING:
            return cached

        try:
            config = _load_agent_resolved_uncached(
                tenant_id,
                role_code=role_code,
                kind=kind,
                route_key=route_key,
            )
        except Exception as exc:
            raise RuntimeError(
                f"connect agent database failed ({agent_db_url()}): {exc}"
            ) from exc

        self._cache.set(cache_key, config)
        return config

    def list_expert_agents(self, tenant_id: str) -> list[dict[str, Any]]:
        """List enabled expert agents for triage routing (from agno_agent only)."""
        cache_key = f"experts:{tenant_id}"
        cached = self._cache.get(cache_key)
        if cached is not _MISSING:
            return cached

        experts = list_expert_agents(tenant_id)
        self._cache.set(cache_key, experts)
        return experts

    def list_enabled_tenants(self) -> list[str]:
        return list_enabled_tenants()

    def list_agents(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        return list_agents(tenant_id)


def _load_agent_resolved_uncached(
    tenant_id: str,
    *,
    role_code: str = "default",
    kind: str | None = None,
    route_key: str | None = None,
) -> dict[str, Any]:
    with agent_db_connection() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            row = None
            if role_code:
                cur.execute(
                    f"{_AGENT_SELECT} AND tenant_id = %s AND role_code = %s LIMIT 1",
                    (tenant_id, role_code),
                )
                row = cur.fetchone()

            if row is None and route_key and (
                kind == "expert" or role_code.startswith("expert")
            ):
                cur.execute(
                    f"{_AGENT_SELECT} AND tenant_id = %s "
                    "AND JSON_UNQUOTE(JSON_EXTRACT(workflow, '$.kind')) = 'expert' "
                    "AND JSON_UNQUOTE(JSON_EXTRACT(workflow, '$.route_key')) = %s LIMIT 1",
                    (tenant_id, route_key),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        f"{_AGENT_SELECT} AND tenant_id = %s AND role_code = %s LIMIT 1",
                        (tenant_id, f"expert_{route_key}"),
                    )
                    row = cur.fetchone()

            if row is None and kind == "triage":
                cur.execute(
                    f"{_AGENT_SELECT} AND tenant_id = %s "
                    "AND JSON_UNQUOTE(JSON_EXTRACT(workflow, '$.kind')) = 'triage' LIMIT 1",
                    (tenant_id,),
                )
                row = cur.fetchone()

            if row is None and role_code != "default":
                cur.execute(
                    f"{_AGENT_SELECT} AND tenant_id = %s AND role_code = 'default' LIMIT 1",
                    (tenant_id,),
                )
                row = cur.fetchone()

    if row is None and tenant_id != "default":
        return _load_agent_resolved_uncached("default", role_code="default")

    if row is None:
        return _row_to_config(dict(DEFAULT_ROW))

    return _row_to_config(row, tenant_id=tenant_id)


def list_expert_agents(tenant_id: str) -> list[dict[str, Any]]:
    with agent_db_connection() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"""
                {_AGENT_SELECT}
                  AND tenant_id = %s
                  AND JSON_UNQUOTE(JSON_EXTRACT(workflow, '$.kind')) = 'expert'
                ORDER BY JSON_UNQUOTE(JSON_EXTRACT(workflow, '$.route_key')), role_code
                """,
                (tenant_id,),
            )
            rows = cur.fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                summary = _expert_agent_summary(row, tenant_id)
                if summary is not None:
                    result.append(summary)
            return result


def list_enabled_tenants() -> list[str]:
    with agent_db_connection() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT tenant_id
                FROM agno_agent
                WHERE enabled = 1
                ORDER BY tenant_id
                """
            )
            rows = cur.fetchall()
            return [str(row["tenant_id"]) for row in rows]


def list_agents(tenant_id: str | None = None) -> list[dict[str, Any]]:
    with agent_db_connection() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if tenant_id:
                cur.execute(
                    """
                    SELECT tenant_id, role_code, display_name, description, workflow
                    FROM agno_agent
                    WHERE enabled = 1 AND tenant_id = %s
                    ORDER BY role_code
                    """,
                    (tenant_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT tenant_id, role_code, display_name, description, workflow
                    FROM agno_agent
                    WHERE enabled = 1
                    ORDER BY tenant_id, role_code
                    """
                )
            rows = cur.fetchall()
            result = []
            for row in rows:
                workflow = parse_workflow(row.get("workflow"))
                tid = str(row["tenant_id"])
                result.append(
                    {
                        "tenant_id": tid,
                        "role_code": str(row.get("role_code") or "default"),
                        "workflow": workflow,
                        "display_name": str(row.get("display_name") or row["tenant_id"]),
                        "description": str(row.get("description") or ""),
                    }
                )
            kind_order = {"triage": 0, "expert": 1, "default": 2, "general": 3}
            result.sort(
                key=lambda item: (
                    item["tenant_id"],
                    kind_order.get(workflow_kind(item["workflow"]), 9),
                    workflow_route_key(item["workflow"]) or "",
                    item["role_code"],
                )
            )
            return result


def clear_agent_store_cache() -> None:
    """Clear config TTL cache and reset DB pool (call on worker reload)."""
    reset_engine()

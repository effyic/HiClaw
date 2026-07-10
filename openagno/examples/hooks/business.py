"""Tenant business data enrichment — e.g. department table (not part of standard flow).

Mount this file under AGNO_HOOKS_DIR when a tenant needs business tables
beyond agno_agent. Standard pipeline only reads agno_agent; this hook
injects department catalog, display names, or custom prompt sections.
"""
from __future__ import annotations

from typing import Any

import pymysql.cursors

from agno_worker.tenant.db import agent_db_connection


def _list_departments(tenant_id: str) -> list[dict[str, Any]]:
    try:
        with agent_db_connection() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    """
                    SELECT department_code, department_name, description, sort_order
                    FROM department
                    WHERE tenant_id = %s AND enabled = 1
                    ORDER BY sort_order, department_code
                    """,
                    (tenant_id,),
                )
                return [
                    {
                        "department_code": str(row["department_code"]),
                        "department_name": str(row["department_name"]),
                        "description": str(row.get("description") or ""),
                        "sort_order": int(row.get("sort_order") or 0),
                    }
                    for row in cur.fetchall()
                ]
    except Exception:
        return []


def _merge_triage_departments(
    expert_agents: list[dict[str, Any]],
    departments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join expert agents with department table for triage whitelist."""
    dept_by_code = {d["department_code"]: d for d in departments}
    result: list[dict[str, Any]] = []
    for agent in expert_agents:
        route_key = agent.get("route_key") or ""
        dept = dept_by_code.get(route_key, {})
        result.append(
            {
                "role_code": agent.get("role_code", ""),
                "route_key": route_key,
                "department_code": route_key,
                "department_name": dept.get("department_name") or agent.get("display_name") or route_key,
                "display_name": agent.get("display_name") or route_key,
                "description": dept.get("description") or agent.get("description") or "",
                "sort_order": dept.get("sort_order", 999),
            }
        )
    result.sort(key=lambda item: (item.get("sort_order", 999), item.get("department_code", "")))
    return result


def enrich_business_context_hook(
    run_context: Any,
    base_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Enrich standard context with department business data.

    base_context (from standard pipeline):
      - tenant_id, role_code, workflow_kind, route_key
      - agent_config: row from agno_agent
      - expert_agents: expert rows from agno_agent

    Return partial dict merged into business_context:
      - departments: full department catalog
      - triage_departments: expert agents enriched with department names
      - department_name: resolved name for current expert route
      - prompt_supplements: optional {"triage_catalog": "...", "expert_header": "..."}
    """
    del run_context
    tenant_id = str(base_context.get("tenant_id") or "")
    if not tenant_id:
        return None

    departments = _list_departments(tenant_id)
    if not departments:
        return None

    expert_agents = list(base_context.get("expert_agents") or [])
    triage_departments = _merge_triage_departments(expert_agents, departments)

    route_key = base_context.get("route_key")
    department_name = None
    if route_key:
        for dept in departments:
            if dept["department_code"] == route_key:
                department_name = dept["department_name"]
                break

    return {
        "departments": departments,
        "triage_departments": triage_departments,
        "department_name": department_name,
    }

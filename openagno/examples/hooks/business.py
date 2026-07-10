"""Medical tenant business enrichment — department table + triage/expert prompt text.

Mount under AGNO_HOOKS_DIR for healthcare scenarios. Standard prompt.py stays
domain-agnostic; this hook injects scenario-specific prompt_supplements.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from agno_worker.tenant.db import agent_db_connection


def _list_departments(tenant_id: str) -> list[dict[str, Any]]:
    try:
        with agent_db_connection() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT department_code, department_name, description, sort_order
                    FROM department
                    WHERE tenant_id = :tenant_id AND enabled IS TRUE
                    ORDER BY sort_order, department_code
                    """
                ),
                {"tenant_id": tenant_id},
            )
            return [
                {
                    "department_code": str(row["department_code"]),
                    "department_name": str(row["department_name"]),
                    "description": str(row.get("description") or ""),
                    "sort_order": int(row.get("sort_order") or 0),
                }
                for row in result.mappings()
            ]
    except Exception:
        return []


def _department_code_from_role(role_code: str) -> str:
    if role_code.startswith("expert_"):
        return role_code[len("expert_") :]
    return role_code


def _merge_triage_departments(
    expert_agents: list[dict[str, Any]],
    departments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    dept_by_code = {d["department_code"]: d for d in departments}
    result: list[dict[str, Any]] = []
    for agent in expert_agents:
        role_code = str(agent.get("role_code") or "")
        department_code = _department_code_from_role(role_code)
        dept = dept_by_code.get(department_code, {})
        result.append(
            {
                "role_code": role_code,
                "department_code": department_code,
                "department_name": dept.get("department_name")
                or agent.get("display_name")
                or department_code,
                "display_name": agent.get("display_name") or department_code,
                "description": dept.get("description") or agent.get("description") or "",
                "sort_order": dept.get("sort_order", 999),
            }
        )
    result.sort(key=lambda item: (item.get("sort_order", 999), item.get("department_code", "")))
    return result


def _build_triage_catalog(items: list[dict[str, Any]]) -> str:
    if not items:
        return "当前租户未配置任何专家科室，分诊阶段不得输出 departments 推荐。"
    codes = ", ".join(
        str(item.get("department_code") or "")
        for item in items
        if item.get("department_code")
    )
    lines = [
        "已配置专家科室（分诊 **只能** 从下列科室推荐，禁止推荐列表外科室）：",
        f"允许 department_code: {codes}",
    ]
    for item in items:
        code = item.get("department_code") or ""
        name = item.get("department_name") or item.get("display_name") or code
        lines.append(
            f"- {code}: {name} "
            f"(Agent: {item.get('role_code', '')}) — {item.get('description', '')}"
        )
    return "\n".join(lines)


def _build_expert_header(route_label: str) -> str:
    return (
        f"当前科室：{route_label}。"
        "请先阅读会话中分诊阶段的对话历史，再开始专科追问。"
    )


def enrich_business_context_hook(
    run_context: Any,
    base_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Enrich with department data and medical prompt supplements."""
    del run_context
    tenant_id = str(base_context.get("tenant_id") or "")
    if not tenant_id:
        return None

    role_code = str(base_context.get("role_code") or "")
    agents = list(base_context.get("agents") or [])
    expert_agents = [
        agent
        for agent in agents
        if str(agent.get("role_code") or "") not in ("", "default", "triage")
    ]

    departments = _list_departments(tenant_id)
    triage_departments = (
        _merge_triage_departments(expert_agents, departments) if departments else expert_agents
    )

    department_code = _department_code_from_role(role_code)
    route_label = None
    if role_code.startswith("expert_"):
        for dept in departments:
            if dept["department_code"] == department_code:
                route_label = dept["department_name"]
                break
        if not route_label:
            for agent in expert_agents:
                if agent.get("role_code") == role_code:
                    route_label = agent.get("display_name") or department_code
                    break

    prompt_supplements: dict[str, str] = {}
    if role_code == "triage":
        catalog_items = triage_departments or expert_agents
        prompt_supplements["system_prompt_append"] = _build_triage_catalog(catalog_items)
    elif role_code.startswith("expert_") and route_label:
        prompt_supplements["system_prompt_append"] = _build_expert_header(str(route_label))

    payload: dict[str, Any] = {}
    if departments:
        payload["departments"] = departments
    if triage_departments:
        payload["triage_departments"] = triage_departments
    if route_label:
        payload["route_label"] = route_label
    if prompt_supplements:
        payload["prompt_supplements"] = prompt_supplements
    return payload or None

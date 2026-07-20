"""集成测试共用的 API 调用辅助函数。"""
from __future__ import annotations

import uuid
from typing import Any

from conftest import ADMIN_HEADERS, RUNTIME_HEADERS


def create_type(client, tenant: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "code": overrides.pop("code", f"type-{uuid.uuid4().hex[:8]}"),
        "name": overrides.pop("name", "测试类型"),
        "action": overrides.pop("action", "BLOCK_REQUEST"),
    }
    body.update(overrides)
    resp = client.post(
        f"/api/v1/tenants/{tenant}/sensitive-types", json=body, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_rule(client, tenant: str, type_id: int, **overrides: Any) -> dict[str, Any]:
    body = {
        "type_id": type_id,
        "pattern": overrides.pop("pattern", f"词-{uuid.uuid4().hex[:8]}"),
    }
    body.update(overrides)
    resp = client.post(
        f"/api/v1/tenants/{tenant}/sensitive-rules", json=body, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_agent(tenant: str, role_code: str | None = None) -> int:
    from sqlalchemy import text

    from sensitive_content.db import db_connection

    role = role_code or "snapshot-default"
    with db_connection() as conn:
        row = conn.execute(
            text(
                "INSERT INTO agno_agent (tenant_id, role_code, enabled) "
                "VALUES (:tenant_id, :role_code, TRUE) "
                "ON CONFLICT (tenant_id, role_code) DO UPDATE SET enabled = TRUE "
                "RETURNING id"
            ),
            {"tenant_id": tenant, "role_code": role},
        ).first()
    assert row is not None
    return int(row[0])


def agent_role(agent_id: int) -> str:
    from sqlalchemy import text

    from sensitive_content.db import db_connection

    with db_connection() as conn:
        row = conn.execute(
            text("SELECT role_code FROM agno_agent WHERE id = :agent_id"),
            {"agent_id": agent_id},
        ).first()
    assert row is not None
    return str(row[0])


def bind_all_assignable_types(client, tenant: str, role_code: str) -> list[int]:
    options = client.get(
        f"/api/v1/tenants/{tenant}/agents/{role_code}/sensitive-types",
        headers=ADMIN_HEADERS,
        params={"page_size": 500},
    )
    assert options.status_code == 200, options.text
    type_ids = [item["id"] for item in options.json()["items"] if item["assignable"]]
    bound = client.put(
        f"/api/v1/tenants/{tenant}/agents/{role_code}/sensitive-types",
        headers=ADMIN_HEADERS,
        json={"type_ids": type_ids},
    )
    assert bound.status_code == 200, bound.text
    return type_ids


def get_snapshot(client, tenant: str, **kwargs: Any):
    agent_id = int(kwargs.pop("agent_id", 0) or create_agent(tenant))
    if kwargs.pop("bind_all", True):
        bind_all_assignable_types(client, tenant, agent_role(agent_id))
    headers = dict(RUNTIME_HEADERS)
    headers.update(kwargs.pop("headers", {}))
    return client.get(
        f"/internal/v1/tenants/{tenant}/agents/{agent_id}/policy-snapshot",
        headers=headers,
        **kwargs,
    )


def post_events(client, events: list[dict[str, Any]]):
    return client.post(
        "/internal/v1/hit-events:batch",
        json={"events": events},
        headers=RUNTIME_HEADERS,
    )

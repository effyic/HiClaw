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


def get_snapshot(client, tenant: str, **kwargs: Any):
    headers = dict(RUNTIME_HEADERS)
    headers.update(kwargs.pop("headers", {}))
    return client.get(
        f"/internal/v1/tenants/{tenant}/policy-snapshot", headers=headers, **kwargs
    )


def post_events(client, events: list[dict[str, Any]]):
    return client.post(
        "/internal/v1/hit-events:batch",
        json={"events": events},
        headers=RUNTIME_HEADERS,
    )

"""Agent 规则绑定、专属快照与自动解绑集成测试。"""
from __future__ import annotations

import uuid

from conftest import ADMIN_HEADERS, RUNTIME_HEADERS
from helpers import create_agent, create_rule, create_type, post_events


def _binding_url(tenant: str, agent_id: int) -> str:
    return f"/api/v1/tenants/{tenant}/agents/{agent_id}/sensitive-rules"


def _snapshot(client, tenant: str, agent_id: int) -> dict:
    response = client.get(
        f"/internal/v1/tenants/{tenant}/agents/{agent_id}/policy-snapshot",
        headers=RUNTIME_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_unbound_agent_has_valid_empty_snapshot(client):
    agent_id = create_agent("t1")
    t = create_type(client, "t1")
    create_rule(client, "t1", t["id"], pattern="不会全局生效")

    body = _snapshot(client, "t1", agent_id)
    assert body["agent_id"] == agent_id
    assert body["binding_rule_ids"] == []
    assert body["rules"] == []
    assert body["types"] == []
    assert body["version"].endswith(":agent-0")


def test_replace_is_idempotent_and_agents_are_isolated(client):
    first = create_agent("t1", "first")
    second = create_agent("t1", "second")
    t = create_type(client, "t1")
    r1 = create_rule(client, "t1", t["id"], pattern="one")
    r2 = create_rule(client, "t1", t["id"], pattern="two")

    response = client.put(
        _binding_url("t1", first), headers=ADMIN_HEADERS, json={"rule_ids": [r1["id"]]}
    )
    assert response.status_code == 200
    version = response.json()["version"]
    again = client.put(
        _binding_url("t1", first), headers=ADMIN_HEADERS, json={"rule_ids": [r1["id"]]}
    )
    assert again.json()["version"] == version
    client.put(
        _binding_url("t1", second), headers=ADMIN_HEADERS, json={"rule_ids": [r2["id"]]}
    )

    assert {item["pattern"] for item in _snapshot(client, "t1", first)["rules"]} == {"one"}
    assert {item["pattern"] for item in _snapshot(client, "t1", second)["rules"]} == {"two"}


def test_global_binding_follows_enabled_and_disabled_override(client):
    agent_id = create_agent("t1")
    t = create_type(client, "global")
    global_rule = create_rule(client, "global", t["id"], pattern="global")
    override = create_rule(
        client,
        "t1",
        t["id"],
        pattern="tenant",
        overrides_global_rule_id=global_rule["id"],
    )
    bound = client.put(
        _binding_url("t1", agent_id),
        headers=ADMIN_HEADERS,
        json={"rule_ids": [global_rule["id"]]},
    )
    assert bound.status_code == 200, bound.text
    assert {item["pattern"] for item in _snapshot(client, "t1", agent_id)["rules"]} == {"tenant"}

    client.post(
        f"/api/v1/tenants/t1/sensitive-rules/{override['id']}:disable",
        headers=ADMIN_HEADERS,
    )
    assert _snapshot(client, "t1", agent_id)["rules"] == []


def test_disabled_existing_binding_is_retained_but_cannot_be_added(client):
    first = create_agent("t1", "first")
    second = create_agent("t1", "second")
    t = create_type(client, "t1")
    rule = create_rule(client, "t1", t["id"], pattern="later-disabled")
    client.put(
        _binding_url("t1", first), headers=ADMIN_HEADERS, json={"rule_ids": [rule["id"]]}
    )
    client.post(
        f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}:disable",
        headers=ADMIN_HEADERS,
    )

    options = client.get(_binding_url("t1", first), headers=ADMIN_HEADERS).json()
    item = next(item for item in options["items"] if item["id"] == rule["id"])
    assert item["selected"] is True
    assert item["assignable"] is False
    assert item["inactive_reason"] == "rule_disabled"
    retained = client.put(
        _binding_url("t1", first), headers=ADMIN_HEADERS, json={"rule_ids": [rule["id"]]}
    )
    assert retained.status_code == 200
    rejected = client.put(
        _binding_url("t1", second), headers=ADMIN_HEADERS, json={"rule_ids": [rule["id"]]}
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "rule_not_assignable"


def test_delete_rule_unbinds_and_audits(client):
    agent_id = create_agent("t1")
    t = create_type(client, "t1")
    rule = create_rule(client, "t1", t["id"], pattern="delete-me")
    client.put(
        _binding_url("t1", agent_id), headers=ADMIN_HEADERS, json={"rule_ids": [rule["id"]]}
    )
    deleted = client.delete(
        f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}", headers=ADMIN_HEADERS
    )
    assert deleted.status_code == 204
    assert _snapshot(client, "t1", agent_id)["binding_rule_ids"] == []
    logs = client.get(
        "/api/v1/tenants/t1/audit-logs",
        headers=ADMIN_HEADERS,
        params={"target_kind": "agent_binding", "target_id": agent_id},
    ).json()["items"]
    assert logs


def test_hit_event_agent_filter(client):
    event = {
        "event_id": str(uuid.uuid4()),
        "rule_id": 1,
        "type_id": 1,
        "rule_action": "LOG_ONLY",
        "final_action": "LOG_ONLY",
        "selected": True,
        "final_rule_id": 1,
        "tenant_id": "t1",
        "agent_id": 42,
        "request_fingerprint": "r",
        "session_fingerprint": "s",
        "policy_version": "global-1:tenant-1:agent-1",
    }
    assert post_events(client, [event]).status_code == 200
    found = client.get(
        "/api/v1/tenants/t1/hit-events",
        headers=ADMIN_HEADERS,
        params={"agent_id": 42},
    ).json()
    assert found["total"] == 1
    assert found["items"][0]["agent_id"] == 42
    missing = client.get(
        "/api/v1/tenants/t1/hit-events",
        headers=ADMIN_HEADERS,
        params={"agent_id": 43},
    ).json()
    assert missing["total"] == 0

"""Agent 类型绑定、专属快照与关联清理集成测试。"""
from __future__ import annotations

import uuid

from conftest import ADMIN_HEADERS, RUNTIME_HEADERS, MIGRATIONS_DIR
from helpers import create_agent, create_rule, create_type, post_events
from sqlalchemy import text


def _binding_url(tenant: str, role_code: str) -> str:
    return f"/api/v1/tenants/{tenant}/agents/{role_code}/sensitive-types"


def _snapshot(client, tenant: str, agent_id: int) -> dict:
    response = client.get(
        f"/internal/v1/tenants/{tenant}/agents/{agent_id}/policy-snapshot",
        headers=RUNTIME_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _agent_version(tenant: str, agent_id: int) -> int:
    from sensitive_content.db import db_connection

    with db_connection() as conn:
        row = conn.execute(
            text(
                "SELECT version FROM sensitive_content.agent_policy_version "
                "WHERE tenant_id = :tenant_id AND agent_id = :agent_id"
            ),
            {"tenant_id": tenant, "agent_id": agent_id},
        ).first()
    return int(row[0]) if row else 0


def test_unbound_agent_has_valid_empty_snapshot(client):
    agent_id = create_agent("t1")
    t = create_type(client, "t1")
    create_rule(client, "t1", t["id"], pattern="不会全局生效")

    body = _snapshot(client, "t1", agent_id)
    assert body["agent_id"] == agent_id
    assert body["binding_type_ids"] == []
    assert body["rules"] == []
    assert body["types"] == []
    assert body["version"].endswith(":agent-0")


def test_replace_is_idempotent_and_agents_are_isolated(client):
    first = create_agent("t1", "first")
    second = create_agent("t1", "second")
    t1 = create_type(client, "t1", code="type-a", name="类型A")
    t2 = create_type(client, "t1", code="type-b", name="类型B")
    create_rule(client, "t1", t1["id"], pattern="one")
    create_rule(client, "t1", t2["id"], pattern="two")

    response = client.put(
        _binding_url("t1", "first"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t1["id"]]},
    )
    assert response.status_code == 200
    assert response.json()["role_code"] == "first"
    version = response.json()["version"]
    again = client.put(
        _binding_url("t1", "first"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t1["id"]]},
    )
    assert again.json()["version"] == version
    client.put(
        _binding_url("t1", "second"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t2["id"]]},
    )

    assert {item["pattern"] for item in _snapshot(client, "t1", first)["rules"]} == {"one"}
    assert {item["pattern"] for item in _snapshot(client, "t1", second)["rules"]} == {"two"}


def test_numeric_agent_id_is_not_a_binding_identifier(client):
    agent_id = create_agent("t1", "medical-triage-18")

    response = client.get(
        _binding_url("t1", str(agent_id)),
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_not_found"


def test_global_binding_follows_enabled_and_disabled_override(client):
    agent_id = create_agent("t1", "override-agent")
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
        _binding_url("t1", "override-agent"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t["id"]]},
    )
    assert bound.status_code == 200, bound.text
    assert {item["pattern"] for item in _snapshot(client, "t1", agent_id)["rules"]} == {"tenant"}

    client.post(
        f"/api/v1/tenants/t1/sensitive-rules/{override['id']}:disable",
        headers=ADMIN_HEADERS,
    )
    assert _snapshot(client, "t1", agent_id)["rules"] == []


def test_disabled_type_binding_retained_but_cannot_be_added(client):
    first = create_agent("t1", "first")
    second = create_agent("t1", "second")
    t = create_type(client, "t1")
    create_rule(client, "t1", t["id"], pattern="later-disabled")
    client.put(
        _binding_url("t1", "first"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t["id"]]},
    )
    client.post(
        f"/api/v1/tenants/t1/sensitive-types/{t['id']}:disable",
        headers=ADMIN_HEADERS,
    )

    options = client.get(_binding_url("t1", "first"), headers=ADMIN_HEADERS).json()
    item = next(item for item in options["items"] if item["id"] == t["id"])
    assert item["selected"] is True
    assert item["assignable"] is False
    assert item["inactive_reason"] == "type_disabled"
    # 禁用后快照不含该类型规则，但绑定保留
    assert _snapshot(client, "t1", first)["rules"] == []
    assert _snapshot(client, "t1", first)["binding_type_ids"] == [t["id"]]

    retained = client.put(
        _binding_url("t1", "first"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t["id"]]},
    )
    assert retained.status_code == 200
    rejected = client.put(
        _binding_url("t1", "second"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t["id"]]},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "type_not_assignable"

    # 重新启用后自动恢复
    client.post(
        f"/api/v1/tenants/t1/sensitive-types/{t['id']}:enable",
        headers=ADMIN_HEADERS,
    )
    assert {item["pattern"] for item in _snapshot(client, "t1", first)["rules"]} == {
        "later-disabled"
    }


def test_delete_rule_does_not_unbind_type(client):
    agent_id = create_agent("t1", "delete-agent")
    t = create_type(client, "t1")
    rule = create_rule(client, "t1", t["id"], pattern="delete-me")
    other = create_rule(client, "t1", t["id"], pattern="keep-me")
    client.put(
        _binding_url("t1", "delete-agent"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t["id"]]},
    )
    deleted = client.delete(
        f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}", headers=ADMIN_HEADERS
    )
    assert deleted.status_code == 204
    body = _snapshot(client, "t1", agent_id)
    assert body["binding_type_ids"] == [t["id"]]
    assert {item["pattern"] for item in body["rules"]} == {"keep-me"}


def test_new_rule_under_bound_type_auto_enters_snapshot(client):
    agent_id = create_agent("t1", "auto-rule")
    t = create_type(client, "t1")
    create_rule(client, "t1", t["id"], pattern="first")
    client.put(
        _binding_url("t1", "auto-rule"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t["id"]]},
    )
    etag = _snapshot(client, "t1", agent_id)["etag"]
    create_rule(client, "t1", t["id"], pattern="second")
    resp = client.get(
        f"/internal/v1/tenants/t1/agents/{agent_id}/policy-snapshot",
        headers={**RUNTIME_HEADERS, "If-None-Match": etag},
    )
    assert resp.status_code == 200
    assert {item["pattern"] for item in resp.json()["rules"]} == {"first", "second"}


def test_rule_type_change_enters_and_exits_snapshot(client):
    agent_id = create_agent("t1", "move-rule")
    ta = create_type(client, "t1", code="ta", name="A")
    tb = create_type(client, "t1", code="tb", name="B")
    rule = create_rule(client, "t1", ta["id"], pattern="movable")
    client.put(
        _binding_url("t1", "move-rule"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [ta["id"]]},
    )
    assert {item["pattern"] for item in _snapshot(client, "t1", agent_id)["rules"]} == {
        "movable"
    }

    client.put(
        f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}",
        headers=ADMIN_HEADERS,
        json={"type_id": tb["id"]},
    )
    assert _snapshot(client, "t1", agent_id)["rules"] == []

    client.put(
        _binding_url("t1", "move-rule"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [tb["id"]]},
    )
    assert {item["pattern"] for item in _snapshot(client, "t1", agent_id)["rules"]} == {
        "movable"
    }


def test_selected_type_ids_complete_across_pages(client):
    create_agent("t1", "paged")
    types = [
        create_type(client, "t1", code=f"p-{i}", name=f"分页类型{i}")
        for i in range(5)
    ]
    selected = [types[0]["id"], types[2]["id"], types[4]["id"]]
    client.put(
        _binding_url("t1", "paged"),
        headers=ADMIN_HEADERS,
        json={"type_ids": selected},
    )
    page = client.get(
        _binding_url("t1", "paged"),
        headers=ADMIN_HEADERS,
        params={"page": 1, "page_size": 2},
    ).json()
    assert page["page_size"] == 2
    assert len(page["items"]) == 2
    assert page["selected_type_ids"] == sorted(selected)


def test_delete_global_type_unbinds_across_tenants(client):
    a1 = create_agent("t1", "g-unbind-1")
    a2 = create_agent("t2", "g-unbind-2")
    t = create_type(client, "global", code="empty-global", name="空全局类型")
    client.put(
        _binding_url("t1", "g-unbind-1"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t["id"]]},
    )
    client.put(
        _binding_url("t2", "g-unbind-2"),
        headers=ADMIN_HEADERS,
        json={"type_ids": [t["id"]]},
    )
    v1 = _agent_version("t1", a1)
    v2 = _agent_version("t2", a2)

    deleted = client.delete(
        f"/api/v1/tenants/global/sensitive-types/{t['id']}", headers=ADMIN_HEADERS
    )
    assert deleted.status_code == 204
    assert _snapshot(client, "t1", a1)["binding_type_ids"] == []
    assert _snapshot(client, "t2", a2)["binding_type_ids"] == []
    assert _agent_version("t1", a1) == v1 + 1
    assert _agent_version("t2", a2) == v2 + 1


def test_hit_event_role_code_filter(client):
    agent_id = create_agent("t1", "event-agent")
    event = {
        "event_id": str(uuid.uuid4()),
        "rule_id": 1,
        "type_id": 1,
        "rule_action": "LOG_ONLY",
        "final_action": "LOG_ONLY",
        "selected": True,
        "final_rule_id": 1,
        "tenant_id": "t1",
        "agent_id": agent_id,
        "request_fingerprint": "r",
        "session_fingerprint": "s",
        "policy_version": "global-1:tenant-1:agent-1",
    }
    assert post_events(client, [event]).status_code == 200
    found = client.get(
        "/api/v1/tenants/t1/hit-events",
        headers=ADMIN_HEADERS,
        params={"role_code": "event-agent"},
    ).json()
    assert found["total"] == 1
    assert found["items"][0]["role_code"] == "event-agent"
    missing = client.get(
        "/api/v1/tenants/t1/hit-events",
        headers=ADMIN_HEADERS,
        params={"role_code": "missing-agent"},
    ).json()
    assert missing["total"] == 0


def test_override_type_mismatch_rejected(client):
    type_a = create_type(client, "global", code="oa", name="覆盖A")
    type_b = create_type(client, "global", code="ob", name="覆盖B")
    global_rule = create_rule(client, "global", type_a["id"], pattern="target")
    resp = client.post(
        "/api/v1/tenants/t1/sensitive-rules",
        headers=ADMIN_HEADERS,
        json={
            "type_id": type_b["id"],
            "pattern": "cross",
            "overrides_global_rule_id": global_rule["id"],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "override_type_mismatch"

    same = create_rule(
        client,
        "t1",
        type_a["id"],
        pattern="same-type",
        overrides_global_rule_id=global_rule["id"],
    )
    moved = client.put(
        f"/api/v1/tenants/t1/sensitive-rules/{same['id']}",
        headers=ADMIN_HEADERS,
        json={"type_id": type_b["id"]},
    )
    assert moved.status_code == 400
    assert moved.json()["error"]["code"] == "override_type_mismatch"


class TestMigrate0005:
    """0005：规则绑定 → 类型绑定的数据迁移与预检。"""

    def _ensure_old_binding_table(self, conn) -> None:
        conn.execute(text("DROP TABLE IF EXISTS sensitive_content.agent_type_binding"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS sensitive_content.agent_rule_binding (
                    tenant_id  TEXT NOT NULL,
                    agent_id   BIGINT NOT NULL,
                    rule_id    BIGINT NOT NULL REFERENCES sensitive_content.sensitive_rule(id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, agent_id, rule_id)
                )
                """
            )
        )
        conn.execute(text("TRUNCATE sensitive_content.agent_rule_binding"))
        conn.execute(text("DELETE FROM sensitive_content.agent_policy_version"))
        conn.execute(
            text("DELETE FROM sensitive_content.schema_migration WHERE version = 5")
        )

    def _run_0005(self, conn) -> None:
        sql = (MIGRATIONS_DIR / "0005_agent_type_binding.sql").read_text(encoding="utf-8")
        conn.exec_driver_sql(sql)
        conn.execute(
            text(
                "INSERT INTO sensitive_content.schema_migration (version) "
                "VALUES (5) ON CONFLICT DO NOTHING"
            )
        )

    def _ensure_post_0005(self, conn) -> None:
        """测试结束后回到 0005 后的表结构，避免污染后续用例。"""
        conn.execute(text("DROP TABLE IF EXISTS sensitive_content.agent_rule_binding"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS sensitive_content.agent_type_binding (
                    tenant_id  TEXT NOT NULL,
                    agent_id   BIGINT NOT NULL,
                    type_id    BIGINT NOT NULL REFERENCES sensitive_content.sensitive_type(id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, agent_id, type_id)
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO sensitive_content.schema_migration (version) "
                "VALUES (5) ON CONFLICT DO NOTHING"
            )
        )

    def test_dedup_and_skip_deleted_and_bump_version(self, db: str):
        from sensitive_content.db import db_connection

        agent_id = create_agent("t1", "mig-dedup")
        with db_connection() as conn:
            type_id = conn.execute(
                text(
                    """
                    INSERT INTO sensitive_content.sensitive_type
                        (tenant_id, code, name, action, action_config, priority, enabled)
                    VALUES ('t1', 'mig-t', '迁移类型', 'LOG_ONLY', '{}', 0, TRUE)
                    RETURNING id
                    """
                )
            ).scalar()
            r1 = conn.execute(
                text(
                    """
                    INSERT INTO sensitive_content.sensitive_rule
                        (tenant_id, type_id, pattern, match_mode, enabled, deleted)
                    VALUES ('t1', :tid, 'r1', 'text', TRUE, FALSE)
                    RETURNING id
                    """
                ),
                {"tid": type_id},
            ).scalar()
            r2 = conn.execute(
                text(
                    """
                    INSERT INTO sensitive_content.sensitive_rule
                        (tenant_id, type_id, pattern, match_mode, enabled, deleted)
                    VALUES ('t1', :tid, 'r2', 'text', FALSE, FALSE)
                    RETURNING id
                    """
                ),
                {"tid": type_id},
            ).scalar()
            r_deleted = conn.execute(
                text(
                    """
                    INSERT INTO sensitive_content.sensitive_rule
                        (tenant_id, type_id, pattern, match_mode, enabled, deleted)
                    VALUES ('t1', :tid, 'r-del', 'text', TRUE, TRUE)
                    RETURNING id
                    """
                ),
                {"tid": type_id},
            ).scalar()

            self._ensure_old_binding_table(conn)
            for rid in (r1, r2, r_deleted):
                conn.execute(
                    text(
                        "INSERT INTO sensitive_content.agent_rule_binding "
                        "(tenant_id, agent_id, rule_id) VALUES ('t1', :aid, :rid)"
                    ),
                    {"aid": agent_id, "rid": rid},
                )
            conn.execute(
                text(
                    "INSERT INTO sensitive_content.agent_policy_version "
                    "(tenant_id, agent_id, version) VALUES ('t1', :aid, 3)"
                ),
                {"aid": agent_id},
            )
            self._run_0005(conn)

            rows = conn.execute(
                text(
                    "SELECT type_id FROM sensitive_content.agent_type_binding "
                    "WHERE tenant_id = 't1' AND agent_id = :aid"
                ),
                {"aid": agent_id},
            ).all()
            assert [int(r[0]) for r in rows] == [int(type_id)]
            version = conn.execute(
                text(
                    "SELECT version FROM sensitive_content.agent_policy_version "
                    "WHERE tenant_id = 't1' AND agent_id = :aid"
                ),
                {"aid": agent_id},
            ).scalar()
            assert int(version) == 4
            exists = conn.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'sensitive_content'
                          AND table_name = 'agent_rule_binding'
                    )
                    """
                )
            ).scalar()
            assert exists is False

    def test_cross_type_override_blocks_migration(self, db: str):
        from sensitive_content.db import db_connection
        import pytest

        with db_connection() as conn:
            ta = conn.execute(
                text(
                    """
                    INSERT INTO sensitive_content.sensitive_type
                        (tenant_id, code, name, action, action_config, enabled)
                    VALUES ('', 'x-a', 'XA', 'LOG_ONLY', '{}', TRUE)
                    RETURNING id
                    """
                )
            ).scalar()
            tb = conn.execute(
                text(
                    """
                    INSERT INTO sensitive_content.sensitive_type
                        (tenant_id, code, name, action, action_config, enabled)
                    VALUES ('', 'x-b', 'XB', 'LOG_ONLY', '{}', TRUE)
                    RETURNING id
                    """
                )
            ).scalar()
            gid = conn.execute(
                text(
                    """
                    INSERT INTO sensitive_content.sensitive_rule
                        (tenant_id, type_id, pattern, match_mode, enabled, deleted)
                    VALUES ('', :tid, 'g', 'text', TRUE, FALSE)
                    RETURNING id
                    """
                ),
                {"tid": ta},
            ).scalar()
            # 故意写入跨类型 override（绕过 API）
            conn.execute(
                text(
                    """
                    INSERT INTO sensitive_content.sensitive_rule
                        (tenant_id, type_id, pattern, match_mode, enabled, deleted,
                         overrides_global_rule_id)
                    VALUES ('t1', :tid, 'o', 'text', TRUE, FALSE, :gid)
                    """
                ),
                {"tid": tb, "gid": gid},
            )
            self._ensure_old_binding_table(conn)
            try:
                with pytest.raises(Exception, match="0005 blocked: cross-type override"):
                    self._run_0005(conn)
            finally:
                # RAISE EXCEPTION 会使当前事务 aborted，先回滚再恢复表结构
                conn.rollback()
                conn.execute(
                    text(
                        "DELETE FROM sensitive_content.sensitive_rule "
                        "WHERE overrides_global_rule_id = :gid"
                    ),
                    {"gid": gid},
                )
                self._ensure_post_0005(conn)

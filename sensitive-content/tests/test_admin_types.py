"""集成测试：类型 CRUD、启停、逻辑删除、鉴权与跨租户约束。"""
from __future__ import annotations

from conftest import ADMIN_HEADERS
from helpers import create_rule, create_type


class TestAuth:
    def test_missing_token_rejected(self, client):
        resp = client.get("/api/v1/tenants/global/sensitive-types")
        assert resp.status_code == 401

    def test_wrong_token_rejected(self, client):
        resp = client.get(
            "/api/v1/tenants/global/sensitive-types",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401


class TestTypeCrud:
    def test_create_and_get(self, client):
        created = create_type(
            client, "t1", code="my-type", name="我的类型",
            action="FIXED_REPLY", action_config={"reply_text": "抱歉"}, priority=7,
        )
        assert created["tenant_id"] == "t1"
        assert created["action_config"] == {"reply_text": "抱歉"}
        got = client.get(
            f"/api/v1/tenants/t1/sensitive-types/{created['id']}", headers=ADMIN_HEADERS
        )
        assert got.status_code == 200
        assert got.json()["code"] == "my-type"

    def test_global_tenant_maps_to_empty(self, client):
        created = create_type(client, "global")
        assert created["tenant_id"] == ""

    def test_seed_types_present(self, client):
        resp = client.get(
            "/api/v1/tenants/global/sensitive-types",
            headers=ADMIN_HEADERS,
            params={"page_size": 100},
        )
        codes = {t["code"] for t in resp.json()["items"]}
        assert {"politics", "porn", "violence", "privacy", "abuse", "general"} <= codes

    def test_list_includes_global_for_tenant(self, client):
        create_type(client, "t1", code="own-type")
        resp = client.get(
            "/api/v1/tenants/t1/sensitive-types",
            headers=ADMIN_HEADERS,
            params={"page_size": 100},
        )
        items = resp.json()["items"]
        tenants = {t["tenant_id"] for t in items}
        assert tenants == {"", "t1"}

    def test_duplicate_code_rejected(self, client):
        create_type(client, "t1", code="dup-code")
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-types",
            json={"code": "dup-code", "name": "n", "action": "LOG_ONLY"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "duplicate_code"

    def test_update(self, client):
        created = create_type(client, "t1")
        resp = client.put(
            f"/api/v1/tenants/t1/sensitive-types/{created['id']}",
            json={"name": "改名", "action": "LOG_ONLY", "priority": 3},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "改名"
        assert body["action"] == "LOG_ONLY"

    def test_invalid_action_config_rejected(self, client):
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-types",
            json={
                "code": "bad-cfg", "name": "n", "action": "LOG_ONLY",
                "action_config": {"not_allowed": 1},
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_action_config"

    def test_enable_disable(self, client):
        created = create_type(client, "t1")
        resp = client.post(
            f"/api/v1/tenants/t1/sensitive-types/{created['id']}:disable",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        resp = client.post(
            f"/api/v1/tenants/t1/sensitive-types/{created['id']}:enable",
            headers=ADMIN_HEADERS,
        )
        assert resp.json()["enabled"] is True

    def test_enabled_filter(self, client):
        a = create_type(client, "t1")
        create_type(client, "t1")
        client.post(
            f"/api/v1/tenants/t1/sensitive-types/{a['id']}:disable", headers=ADMIN_HEADERS
        )
        resp = client.get(
            "/api/v1/tenants/t1/sensitive-types",
            headers=ADMIN_HEADERS,
            params={"enabled": False, "include_global": False},
        )
        items = resp.json()["items"]
        assert [t["id"] for t in items] == [a["id"]]

    def test_logical_delete(self, client):
        created = create_type(client, "t1")
        resp = client.delete(
            f"/api/v1/tenants/t1/sensitive-types/{created['id']}", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 204
        got = client.get(
            f"/api/v1/tenants/t1/sensitive-types/{created['id']}", headers=ADMIN_HEADERS
        )
        assert got.status_code == 404
        # 逻辑删除后可复用 code
        create_type(client, "t1", code=created["code"])


class TestGlobalTypeProtection:
    def test_tenant_cannot_update_global_type(self, client):
        gtype = create_type(client, "global")
        resp = client.put(
            f"/api/v1/tenants/t1/sensitive-types/{gtype['id']}",
            json={"name": "篡改"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden_global_type"

    def test_tenant_cannot_delete_or_toggle_global_type(self, client):
        gtype = create_type(client, "global")
        resp = client.delete(
            f"/api/v1/tenants/t1/sensitive-types/{gtype['id']}", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 403
        resp = client.post(
            f"/api/v1/tenants/t1/sensitive-types/{gtype['id']}:disable",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 403

    def test_global_context_can_update_global_type(self, client):
        gtype = create_type(client, "global")
        resp = client.put(
            f"/api/v1/tenants/global/sensitive-types/{gtype['id']}",
            json={"name": "全局改名"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200


class TestTypeDeleteConstraint:
    def test_global_type_in_use_returns_409_with_count(self, client):
        gtype = create_type(client, "global")
        create_rule(client, "t1", gtype["id"])
        create_rule(client, "t2", gtype["id"])
        resp = client.delete(
            f"/api/v1/tenants/global/sensitive-types/{gtype['id']}", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 409
        error = resp.json()["error"]
        assert error["code"] == "type_in_use"
        assert error["references"] == 2

    def test_tenant_type_in_use_returns_409(self, client):
        ttype = create_type(client, "t1")
        create_rule(client, "t1", ttype["id"])
        resp = client.delete(
            f"/api/v1/tenants/t1/sensitive-types/{ttype['id']}", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 409

    def test_deletable_after_rules_deleted(self, client):
        ttype = create_type(client, "t1")
        rule = create_rule(client, "t1", ttype["id"])
        client.delete(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}", headers=ADMIN_HEADERS
        )
        resp = client.delete(
            f"/api/v1/tenants/t1/sensitive-types/{ttype['id']}", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 204

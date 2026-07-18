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
        items = resp.json()["items"]
        codes = {t["code"] for t in items}
        assert {
            "politics",
            "porn",
            "violence",
            "privacy",
            "abuse",
            "general",
            "self_harm",
        } <= codes
        self_harm = next(t for t in items if t["code"] == "self_harm")
        assert self_harm["action"] == "ADJUST_PROMPT"
        assert self_harm["priority"] == 70
        assert self_harm["action_config"].get("prompt_guidance")

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

    def test_create_without_code_auto_generates(self, client):
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-types",
            json={"name": "自动编码类型", "action": "LOG_ONLY"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["code"]
        assert body["code"].startswith("t_")
        assert 1 <= len(body["code"]) <= 64
        assert body["name"] == "自动编码类型"

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

    def test_non_adjust_with_prompt_guidance_rejected(self, client):
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-types",
            json={
                "code": "bad-guidance",
                "name": "n",
                "action": "LOG_ONLY",
                "action_config": {"prompt_guidance": "不应出现"},
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_action_config"

    def test_create_adjust_prompt_requires_guidance(self, client):
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-types",
            json={"code": "need-guide", "name": "n", "action": "ADJUST_PROMPT"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_action_config"

    def test_merge_then_validate_switch_away_without_clearing_guidance(self, client):
        """ADJUST → 其它 action 且未清除 prompt_guidance → 400。"""
        created = create_type(
            client,
            "t1",
            code="adjust-then-log",
            action="ADJUST_PROMPT",
            action_config={"prompt_guidance": "关怀指引"},
        )
        resp = client.put(
            f"/api/v1/tenants/t1/sensitive-types/{created['id']}",
            json={"action": "LOG_ONLY"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_action_config"

    def test_merge_then_validate_switch_to_adjust_without_guidance(self, client):
        """其它 → ADJUST 且未提交 prompt_guidance → 400。"""
        created = create_type(client, "t1", code="log-then-adjust", action="LOG_ONLY")
        resp = client.put(
            f"/api/v1/tenants/t1/sensitive-types/{created['id']}",
            json={"action": "ADJUST_PROMPT"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_action_config"

    def test_merge_then_validate_patch_config_keeps_db_action(self, client):
        """只改 action_config 时结合 DB 原 action 校验。"""
        created = create_type(
            client,
            "t1",
            code="patch-cfg",
            action="ADJUST_PROMPT",
            action_config={"prompt_guidance": "旧指引"},
        )
        resp = client.put(
            f"/api/v1/tenants/t1/sensitive-types/{created['id']}",
            json={"action_config": {"prompt_guidance": "新指引"}},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["action_config"]["prompt_guidance"] == "新指引"

    def test_merge_then_validate_switch_away_clearing_guidance(self, client):
        created = create_type(
            client,
            "t1",
            code="adjust-clear",
            action="ADJUST_PROMPT",
            action_config={"prompt_guidance": "关怀指引"},
        )
        resp = client.put(
            f"/api/v1/tenants/t1/sensitive-types/{created['id']}",
            json={"action": "LOG_ONLY", "action_config": {}},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "LOG_ONLY"
        assert resp.json()["action_config"] == {}

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

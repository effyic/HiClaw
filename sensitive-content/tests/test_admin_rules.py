"""集成测试：规则 CRUD、筛选、校验（type 归属 / 正则 / 重复 / 覆盖目标）。"""
from __future__ import annotations

from conftest import ADMIN_HEADERS
from helpers import create_rule, create_type


class TestRuleCrud:
    def test_create_and_get(self, client):
        ttype = create_type(client, "t1")
        rule = create_rule(
            client, "t1", ttype["id"],
            pattern="敏感词", description="描述", priority=5, remark="备注",
        )
        assert rule["tenant_id"] == "t1"
        assert rule["effective_status"] == "active"
        got = client.get(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}", headers=ADMIN_HEADERS
        )
        assert got.status_code == 200
        assert got.json()["pattern"] == "敏感词"

    def test_update(self, client):
        ttype = create_type(client, "t1")
        rule = create_rule(client, "t1", ttype["id"], pattern="旧词")
        resp = client.put(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}",
            json={"pattern": "新词", "priority": 9},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern"] == "新词"
        assert body["priority"] == 9

    def test_enable_disable(self, client):
        ttype = create_type(client, "t1")
        rule = create_rule(client, "t1", ttype["id"])
        resp = client.post(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}:disable", headers=ADMIN_HEADERS
        )
        assert resp.json()["enabled"] is False
        resp = client.post(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}:enable", headers=ADMIN_HEADERS
        )
        assert resp.json()["enabled"] is True

    def test_logical_delete(self, client):
        ttype = create_type(client, "t1")
        rule = create_rule(client, "t1", ttype["id"])
        resp = client.delete(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 204
        got = client.get(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}", headers=ADMIN_HEADERS
        )
        assert got.status_code == 404

    def test_tenant_cannot_modify_global_rule(self, client):
        gtype = create_type(client, "global")
        grule = create_rule(client, "global", gtype["id"])
        resp = client.put(
            f"/api/v1/tenants/t1/sensitive-rules/{grule['id']}",
            json={"pattern": "篡改"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden_global_rule"

    def test_other_tenant_rule_not_visible(self, client):
        ttype = create_type(client, "t1")
        rule = create_rule(client, "t1", ttype["id"])
        got = client.get(
            f"/api/v1/tenants/t2/sensitive-rules/{rule['id']}", headers=ADMIN_HEADERS
        )
        assert got.status_code == 404


class TestRuleFilters:
    def test_keyword_type_enabled_filters_and_pagination(self, client):
        type_a = create_type(client, "t1")
        type_b = create_type(client, "t1")
        r1 = create_rule(client, "t1", type_a["id"], pattern="苹果手机")
        r2 = create_rule(client, "t1", type_a["id"], pattern="香蕉")
        r3 = create_rule(client, "t1", type_b["id"], pattern="苹果电脑")
        client.post(
            f"/api/v1/tenants/t1/sensitive-rules/{r2['id']}:disable", headers=ADMIN_HEADERS
        )

        # keyword 筛选
        resp = client.get(
            "/api/v1/tenants/t1/sensitive-rules",
            headers=ADMIN_HEADERS, params={"keyword": "苹果"},
        )
        assert {r["id"] for r in resp.json()["items"]} == {r1["id"], r3["id"]}

        # type_id 筛选
        resp = client.get(
            "/api/v1/tenants/t1/sensitive-rules",
            headers=ADMIN_HEADERS, params={"type_id": type_b["id"]},
        )
        assert {r["id"] for r in resp.json()["items"]} == {r3["id"]}

        # enabled 筛选
        resp = client.get(
            "/api/v1/tenants/t1/sensitive-rules",
            headers=ADMIN_HEADERS, params={"enabled": False},
        )
        assert {r["id"] for r in resp.json()["items"]} == {r2["id"]}

        # 分页
        resp = client.get(
            "/api/v1/tenants/t1/sensitive-rules",
            headers=ADMIN_HEADERS, params={"page": 1, "page_size": 2},
        )
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2


class TestRuleValidation:
    def test_type_must_exist(self, client):
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-rules",
            json={"type_id": 999999, "pattern": "x"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "type_not_found"

    def test_type_of_other_tenant_rejected(self, client):
        other_type = create_type(client, "t2")
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-rules",
            json={"type_id": other_type["id"], "pattern": "x"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404  # 其他租户类型对本租户不可见
        # 全局类型可用
        gtype = create_type(client, "global")
        create_rule(client, "t1", gtype["id"])

    def test_invalid_regex_rejected(self, client):
        ttype = create_type(client, "t1")
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-rules",
            json={"type_id": ttype["id"], "pattern": "([a-z", "match_mode": "regex"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_regex"

    def test_too_long_pattern_rejected(self, client):
        ttype = create_type(client, "t1")
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-rules",
            json={"type_id": ttype["id"], "pattern": "x" * 513},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "pattern_too_long"

    def test_nested_quantifier_rejected(self, client):
        ttype = create_type(client, "t1")
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-rules",
            json={"type_id": ttype["id"], "pattern": "(a+)+", "match_mode": "regex"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "regex_too_complex"

    def test_duplicate_rule_rejected(self, client):
        ttype = create_type(client, "t1")
        create_rule(client, "t1", ttype["id"], pattern="重复词")
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-rules",
            json={"type_id": ttype["id"], "pattern": "重复词"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "duplicate_rule"

    def test_normalized_duplicate_rejected(self, client):
        # 规范化后相等（全角/大小写）也视为重复
        ttype = create_type(client, "t1")
        create_rule(client, "t1", ttype["id"], pattern="abc")
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-rules",
            json={"type_id": ttype["id"], "pattern": "ＡＢＣ"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409

    def test_same_pattern_in_other_tenant_allowed(self, client):
        gtype = create_type(client, "global")
        create_rule(client, "t1", gtype["id"], pattern="共用词")
        create_rule(client, "t2", gtype["id"], pattern="共用词")

    def test_update_to_invalid_regex_rejected(self, client):
        ttype = create_type(client, "t1")
        rule = create_rule(
            client, "t1", ttype["id"], pattern=r"\d+", match_mode="regex"
        )
        resp = client.put(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}",
            json={"pattern": "([a-z"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400


class TestOverrideValidation:
    def test_override_must_reference_global_rule(self, client):
        ttype = create_type(client, "t1")
        own = create_rule(client, "t1", ttype["id"])
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-rules",
            json={"type_id": ttype["id"], "pattern": "x", "overrides_global_rule_id": own["id"]},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_override_target"

    def test_override_must_reference_existing_rule(self, client):
        ttype = create_type(client, "t1")
        resp = client.post(
            "/api/v1/tenants/t1/sensitive-rules",
            json={"type_id": ttype["id"], "pattern": "x", "overrides_global_rule_id": 999999},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400

    def test_global_rule_cannot_set_override(self, client):
        gtype = create_type(client, "global")
        grule = create_rule(client, "global", gtype["id"])
        resp = client.post(
            "/api/v1/tenants/global/sensitive-rules",
            json={
                "type_id": gtype["id"], "pattern": "y",
                "overrides_global_rule_id": grule["id"],
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_override"

    def test_orphaned_status_annotated_after_global_rule_deleted(self, client):
        gtype = create_type(client, "global")
        grule = create_rule(client, "global", gtype["id"], pattern="全局词")
        override = create_rule(
            client, "t1", gtype["id"], pattern="租户词",
            overrides_global_rule_id=grule["id"],
        )
        # 删除全局规则后，覆盖规则标注 orphaned
        client.delete(
            f"/api/v1/tenants/global/sensitive-rules/{grule['id']}", headers=ADMIN_HEADERS
        )
        got = client.get(
            f"/api/v1/tenants/t1/sensitive-rules/{override['id']}", headers=ADMIN_HEADERS
        )
        assert got.json()["effective_status"] == "orphaned"

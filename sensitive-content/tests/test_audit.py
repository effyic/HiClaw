"""集成测试：审计写入（不含明文）与策略版本递增。"""
from __future__ import annotations

import hashlib
import json

from conftest import ADMIN_HEADERS
from helpers import create_rule, create_type, get_snapshot


def _audit_items(client, tenant: str, **params):
    resp = client.get(
        f"/api/v1/tenants/{tenant}/audit-logs", headers=ADMIN_HEADERS, params=params
    )
    assert resp.status_code == 200
    return resp.json()["items"]


def _tenant_version(client, tenant: str) -> str:
    resp = get_snapshot(client, tenant)
    assert resp.status_code == 200
    return resp.json()["version"]


class TestAuditLog:
    def test_write_operations_audited(self, client):
        ttype = create_type(client, "t1")
        rule = create_rule(client, "t1", ttype["id"], pattern="审计词")
        client.post(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}:disable", headers=ADMIN_HEADERS
        )
        client.delete(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}", headers=ADMIN_HEADERS
        )
        actions = {(i["action"], i["target_kind"]) for i in _audit_items(client, "t1")}
        assert {("create", "type"), ("create", "rule"),
                ("disable", "rule"), ("delete", "rule")} <= actions

    def test_operator_recorded_from_header(self, client):
        create_type(client, "t1")
        items = _audit_items(client, "t1", target_kind="type")
        assert items[0]["operator"] == "tester"

    def test_changes_contain_no_plaintext(self, client):
        secret = "绝密敏感词汇"
        ttype = create_type(client, "t1")
        create_rule(client, "t1", ttype["id"], pattern=secret)
        items = _audit_items(client, "t1", target_kind="rule", action="create")
        changes = items[0]["changes"]
        dumped = json.dumps(changes, ensure_ascii=False)
        assert secret not in dumped
        # pattern 字段只存 sha256 哈希与长度
        assert changes["pattern"]["sha256"] == hashlib.sha256(secret.encode()).hexdigest()
        assert changes["pattern"]["length"] == len(secret)

    def test_filters(self, client):
        ttype = create_type(client, "t1")
        rule = create_rule(client, "t1", ttype["id"])
        items = _audit_items(
            client, "t1", target_kind="rule", target_id=rule["id"], action="create"
        )
        assert len(items) == 1
        assert items[0]["target_id"] == rule["id"]
        assert _audit_items(client, "t1", action="delete") == []


class TestPolicyVersionBump:
    def test_tenant_write_bumps_tenant_version(self, client):
        ttype = create_type(client, "t1")
        v1 = _tenant_version(client, "t1")
        create_rule(client, "t1", ttype["id"])
        v2 = _tenant_version(client, "t1")
        assert v1 != v2
        # 全局侧不变，租户侧递增
        assert v1.split(":")[0] == v2.split(":")[0]
        assert v1.split(":")[1] != v2.split(":")[1]

    def test_global_write_bumps_global_version(self, client):
        v1 = _tenant_version(client, "t1")
        create_type(client, "global")
        v2 = _tenant_version(client, "t1")
        assert v1.split(":")[0] != v2.split(":")[0]

    def test_every_write_kind_bumps_version(self, client):
        ttype = create_type(client, "t1")
        rule = create_rule(client, "t1", ttype["id"])
        versions = [_tenant_version(client, "t1")]
        client.put(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}",
            json={"priority": 8}, headers=ADMIN_HEADERS,
        )
        versions.append(_tenant_version(client, "t1"))
        client.post(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}:disable", headers=ADMIN_HEADERS
        )
        versions.append(_tenant_version(client, "t1"))
        client.delete(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}", headers=ADMIN_HEADERS
        )
        versions.append(_tenant_version(client, "t1"))
        assert len(set(versions)) == len(versions)

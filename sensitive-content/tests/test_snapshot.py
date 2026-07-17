"""集成测试：策略快照（覆盖语义、组合版本、规范化 ETag、304）。"""
from __future__ import annotations

from conftest import ADMIN_HEADERS
from helpers import create_rule, create_type, get_snapshot


def _snapshot_patterns(client, tenant: str) -> set[str]:
    resp = get_snapshot(client, tenant)
    assert resp.status_code == 200
    return {r["pattern"] for r in resp.json()["rules"]}


class TestSnapshotAuth:
    def test_runtime_token_required(self, client):
        resp = client.get("/internal/v1/tenants/t1/policy-snapshot")
        assert resp.status_code == 401

    def test_admin_token_not_accepted_for_internal(self, client):
        resp = client.get(
            "/internal/v1/tenants/t1/policy-snapshot",
            headers={"Authorization": "Bearer test-admin"},
        )
        assert resp.status_code == 401


class TestOverrideSemantics:
    def test_enabled_override_replaces_global_rule(self, client):
        gtype = create_type(client, "global")
        grule = create_rule(client, "global", gtype["id"], pattern="全局词")
        create_rule(
            client, "t1", gtype["id"], pattern="租户替换词",
            overrides_global_rule_id=grule["id"],
        )
        patterns = _snapshot_patterns(client, "t1")
        assert "租户替换词" in patterns
        assert "全局词" not in patterns

    def test_disabled_override_removes_global_rule(self, client):
        # enabled=FALSE 的覆盖规则相当于"对本租户禁用某条全局规则"
        gtype = create_type(client, "global")
        grule = create_rule(client, "global", gtype["id"], pattern="要禁用的全局词")
        create_rule(
            client, "t1", gtype["id"], pattern="占位",
            overrides_global_rule_id=grule["id"], enabled=False,
        )
        assert "要禁用的全局词" not in _snapshot_patterns(client, "t1")
        # 其他租户不受影响
        assert "要禁用的全局词" in _snapshot_patterns(client, "t2")

    def test_orphaned_override_not_in_snapshot(self, client):
        gtype = create_type(client, "global")
        grule = create_rule(client, "global", gtype["id"], pattern="将删全局词")
        create_rule(
            client, "t1", gtype["id"], pattern="覆盖词",
            overrides_global_rule_id=grule["id"],
        )
        client.delete(
            f"/api/v1/tenants/global/sensitive-rules/{grule['id']}", headers=ADMIN_HEADERS
        )
        patterns = _snapshot_patterns(client, "t1")
        # 全局规则已删、覆盖记录 orphaned：两者都不进快照
        assert "将删全局词" not in patterns
        assert "覆盖词" not in patterns

    def test_orphaned_audit_and_rule_retained(self, client):
        gtype = create_type(client, "global")
        grule = create_rule(client, "global", gtype["id"], pattern="全局词2")
        override = create_rule(
            client, "t1", gtype["id"], pattern="覆盖词2",
            overrides_global_rule_id=grule["id"],
        )
        client.delete(
            f"/api/v1/tenants/global/sensitive-rules/{grule['id']}", headers=ADMIN_HEADERS
        )
        # 覆盖规则本身保留（仅标注 orphaned），审计记录保留
        got = client.get(
            f"/api/v1/tenants/t1/sensitive-rules/{override['id']}", headers=ADMIN_HEADERS
        )
        assert got.status_code == 200
        logs = client.get(
            "/api/v1/tenants/t1/audit-logs", headers=ADMIN_HEADERS,
            params={"target_kind": "rule", "target_id": override["id"]},
        )
        assert len(logs.json()["items"]) >= 1

    def test_duplicate_pattern_deduped_tenant_wins(self, client):
        gtype = create_type(client, "global")
        create_rule(client, "global", gtype["id"], pattern="同一个词")
        tenant_rule = create_rule(client, "t1", gtype["id"], pattern="同一个词")
        resp = get_snapshot(client, "t1")
        rules = [r for r in resp.json()["rules"] if r["pattern"] == "同一个词"]
        assert len(rules) == 1
        assert rules[0]["id"] == tenant_rule["id"]
        assert rules[0]["tenant_id"] == "t1"

    def test_disabled_global_rule_not_in_snapshot(self, client):
        gtype = create_type(client, "global")
        grule = create_rule(client, "global", gtype["id"], pattern="禁用全局词")
        client.post(
            f"/api/v1/tenants/global/sensitive-rules/{grule['id']}:disable",
            headers=ADMIN_HEADERS,
        )
        assert "禁用全局词" not in _snapshot_patterns(client, "t1")

    def test_disabled_type_excludes_rules(self, client):
        ttype = create_type(client, "t1")
        create_rule(client, "t1", ttype["id"], pattern="类型禁用词")
        client.post(
            f"/api/v1/tenants/t1/sensitive-types/{ttype['id']}:disable",
            headers=ADMIN_HEADERS,
        )
        assert "类型禁用词" not in _snapshot_patterns(client, "t1")


class TestSelfHarmSeedInSnapshot:
    """0003 种子：self_harm 类型 + 中文基础规则集进入快照。"""

    def test_self_harm_type_and_chinese_rules(self, client):
        resp = get_snapshot(client, "t1")
        assert resp.status_code == 200
        body = resp.json()
        types_by_code = {t["code"]: t for t in body["types"]}
        assert "self_harm" in types_by_code
        sh = types_by_code["self_harm"]
        assert sh["action"] == "ADJUST_PROMPT"
        assert sh["priority"] == 70
        assert sh["action_config"].get("prompt_guidance")

        patterns = {
            r["pattern"]
            for r in body["rules"]
            if r["type_id"] == sh["id"]
        }
        # 中文基础规则集（关键词覆盖，非完整分类器）
        assert {
            "自杀",
            "轻生",
            "结束生命",
            "不想活",
            "自我伤害",
            "割腕",
            "寻死",
        } <= patterns


class TestCombinedVersionAndEtag:
    def test_version_format_and_types_included(self, client):
        ttype = create_type(client, "t1", action="FIXED_REPLY",
                            action_config={"reply_text": "test"})
        create_rule(client, "t1", ttype["id"])
        resp = get_snapshot(client, "t1")
        body = resp.json()
        assert body["version"].startswith("global-")
        assert ":tenant-" in body["version"]
        assert resp.headers["ETag"] == body["etag"]
        type_ids = {t["id"] for t in body["types"]}
        assert ttype["id"] in type_ids

    def test_global_change_bumps_all_tenants_version(self, client):
        v_t1 = get_snapshot(client, "t1").json()["version"]
        v_t2 = get_snapshot(client, "t2").json()["version"]
        gtype = create_type(client, "global")
        create_rule(client, "global", gtype["id"], pattern="新全局词")
        assert get_snapshot(client, "t1").json()["version"] != v_t1
        assert get_snapshot(client, "t2").json()["version"] != v_t2

    def test_if_none_match_returns_304(self, client):
        ttype = create_type(client, "t1")
        create_rule(client, "t1", ttype["id"])
        first = get_snapshot(client, "t1")
        etag = first.headers["ETag"]
        second = get_snapshot(client, "t1", headers={"If-None-Match": etag})
        assert second.status_code == 304

    def test_content_change_returns_new_snapshot(self, client):
        ttype = create_type(client, "t1")
        create_rule(client, "t1", ttype["id"])
        etag = get_snapshot(client, "t1").headers["ETag"]
        create_rule(client, "t1", ttype["id"], pattern="又一个词")
        resp = get_snapshot(client, "t1", headers={"If-None-Match": etag})
        assert resp.status_code == 200
        assert resp.headers["ETag"] != etag

    def test_equivalent_content_304_despite_version_bump(self, client):
        # remark 变更递增版本但不改变快照内容：ETag 不变 → 304（与版本号解耦）
        ttype = create_type(client, "t1")
        rule = create_rule(client, "t1", ttype["id"])
        first = get_snapshot(client, "t1")
        etag = first.headers["ETag"]
        version = first.json()["version"]
        client.put(
            f"/api/v1/tenants/t1/sensitive-rules/{rule['id']}",
            json={"remark": "只改备注"}, headers=ADMIN_HEADERS,
        )
        resp = get_snapshot(client, "t1", headers={"If-None-Match": etag})
        assert resp.status_code == 304
        # 版本号确实递增了
        fresh = get_snapshot(client, "t1")
        assert fresh.json()["version"] != version
        assert fresh.headers["ETag"] == etag

    def test_weak_etag_prefix_accepted(self, client):
        etag = get_snapshot(client, "t1").headers["ETag"]
        resp = get_snapshot(client, "t1", headers={"If-None-Match": f"W/{etag}"})
        assert resp.status_code == 304

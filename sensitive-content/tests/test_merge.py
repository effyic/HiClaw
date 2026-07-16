"""纯逻辑测试：五步覆盖合并算法（不依赖 DB）。"""
from __future__ import annotations

from typing import Any

from sensitive_content.store import merge_rules


def _rule(
    rid: int,
    tenant_id: str = "",
    pattern: str = "word",
    enabled: bool = True,
    overrides: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": rid,
        "tenant_id": tenant_id,
        "type_id": 1,
        "pattern": pattern,
        "match_mode": "text",
        "case_sensitive": False,
        "normalize": True,
        "priority": 0,
        "enabled": enabled,
        "overrides_global_rule_id": overrides,
    }
    base.update(extra)
    return base


def _ids(rules: list[dict[str, Any]]) -> set[int]:
    return {r["id"] for r in rules}


class TestMergeSteps:
    def test_step1_global_rules_as_base(self):
        merged = merge_rules([_rule(1, pattern="a"), _rule(2, pattern="b")], [])
        assert _ids(merged) == {1, 2}

    def test_step3_disabled_override_removes_global_rule(self):
        # enabled=FALSE 的覆盖规则：从基础集中剔除对应全局规则（禁用生效）
        merged = merge_rules(
            [_rule(1, pattern="a"), _rule(2, pattern="b")],
            [_rule(10, "t1", pattern="a2", enabled=False, overrides=1)],
        )
        assert _ids(merged) == {2}

    def test_step4_enabled_override_replaces_global_rule(self):
        merged = merge_rules(
            [_rule(1, pattern="a")],
            [_rule(10, "t1", pattern="a-tenant", enabled=True, overrides=1)],
        )
        assert _ids(merged) == {10}
        assert merged[0]["pattern"] == "a-tenant"

    def test_step5_plain_tenant_rules_appended(self):
        merged = merge_rules(
            [_rule(1, pattern="a")],
            [_rule(10, "t1", pattern="c"), _rule(11, "t1", pattern="d", enabled=False)],
        )
        # 禁用的普通租户规则不进快照
        assert _ids(merged) == {1, 10}

    def test_orphaned_override_skipped(self):
        # 指向已删除全局规则的覆盖记录整条跳过：不剔除、不替换
        merged = merge_rules(
            [_rule(2, pattern="b")],
            [_rule(10, "t1", pattern="x", enabled=True, overrides=1)],
            deleted_global_rule_ids={1},
        )
        assert _ids(merged) == {2}

    def test_disabled_override_on_disabled_global_noop(self):
        # 覆盖目标本身未启用（不在基础集）时剔除为空操作
        merged = merge_rules(
            [_rule(2, pattern="b")],
            [_rule(10, "t1", pattern="y", enabled=False, overrides=1)],
        )
        assert _ids(merged) == {2}


class TestMergeDedup:
    def test_same_pattern_tenant_rule_wins(self):
        # 无覆盖关系但规范化后 pattern 相同：租户规则优先保留
        merged = merge_rules(
            [_rule(1, pattern="ＡＢＣ")],
            [_rule(10, "t1", pattern="abc")],
        )
        assert _ids(merged) == {10}

    def test_different_flags_not_deduped(self):
        merged = merge_rules(
            [_rule(1, pattern="abc")],
            [_rule(10, "t1", pattern="abc", case_sensitive=True)],
        )
        assert _ids(merged) == {1, 10}

    def test_replacement_rule_dedups_plain_rule(self):
        # 覆盖替换规则与普通租户规则 pattern 相同时只保留一条
        merged = merge_rules(
            [_rule(1, pattern="a")],
            [
                _rule(10, "t1", pattern="same", enabled=True, overrides=1),
                _rule(11, "t1", pattern="same"),
            ],
        )
        assert _ids(merged) == {10}


class TestMergeCombined:
    def test_full_scenario(self):
        global_rules = [
            _rule(1, pattern="g1"),
            _rule(2, pattern="g2"),
            _rule(3, pattern="g3"),
        ]
        tenant_rules = [
            _rule(10, "t1", pattern="g1-替换", enabled=True, overrides=1),   # 替换 1
            _rule(11, "t1", pattern="x", enabled=False, overrides=2),        # 禁用 2
            _rule(12, "t1", pattern="t-own"),                                # 追加
            _rule(13, "t1", pattern="orphan", enabled=True, overrides=99),   # orphaned
            _rule(14, "t1", pattern="g3"),                                   # 与全局 3 重复
        ]
        merged = merge_rules(global_rules, tenant_rules, deleted_global_rule_ids={99})
        assert _ids(merged) == {10, 12, 14}

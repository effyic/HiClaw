"""纯逻辑测试：组合版本字符串与规范化快照 SHA-256 ETag。"""
from __future__ import annotations

import hashlib
import json

from sensitive_content.store import (
    canonical_snapshot_json,
    combined_version,
    compute_etag,
)


class TestCombinedVersion:
    def test_format(self):
        assert combined_version(12, 37) == "global-12:tenant-37"

    def test_zero_versions(self):
        assert combined_version(0, 0) == "global-0:tenant-0"


def _content(rules_order: bool = False) -> dict:
    rules = [
        {"id": 2, "tenant_id": "t1", "pattern": "b", "priority": 1},
        {"id": 1, "tenant_id": "", "pattern": "a", "priority": 0},
    ]
    if rules_order:
        rules = list(reversed(rules))
    return {"tenant_id": "t1", "types": [], "rules": rules}


class TestCanonicalJson:
    def test_list_order_independent(self):
        # rules/types 按 (tenant_id, id) 排序，输入顺序不影响序列化结果
        assert canonical_snapshot_json(_content()) == canonical_snapshot_json(
            _content(rules_order=True)
        )

    def test_key_order_independent(self):
        a = {"tenant_id": "t", "rules": [], "types": []}
        b = {"types": [], "rules": [], "tenant_id": "t"}
        assert canonical_snapshot_json(a) == canonical_snapshot_json(b)

    def test_compact_and_sorted_keys(self):
        out = canonical_snapshot_json({"b": 1, "a": 2, "rules": [], "types": []})
        assert out == '{"a":2,"b":1,"rules":[],"types":[]}'


class TestEtag:
    def test_equal_content_equal_etag(self):
        assert compute_etag(_content()) == compute_etag(_content(rules_order=True))

    def test_different_content_different_etag(self):
        other = _content()
        other["rules"][0]["pattern"] = "changed"
        assert compute_etag(_content()) != compute_etag(other)

    def test_etag_is_quoted_sha256(self):
        etag = compute_etag(_content())
        assert etag.startswith('"') and etag.endswith('"')
        digest = etag.strip('"')
        expected = hashlib.sha256(
            canonical_snapshot_json(_content()).encode("utf-8")
        ).hexdigest()
        assert digest == expected

    def test_etag_decoupled_from_version(self):
        # ETag 只依赖内容，与版本号无关（版本递增但内容等价时不应变化）
        content = _content()
        etag1 = compute_etag(content)
        etag2 = compute_etag(json.loads(json.dumps(content)))
        assert etag1 == etag2

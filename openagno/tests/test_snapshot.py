"""snapshot 测试：缓存降级、过期告警、原子写入、损坏自愈、目录不可写、ETag 刷新。"""
from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import pytest

from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.snapshot import SnapshotClient

from conftest import make_rule, make_snapshot, make_type


TENANT = "tenant-a"


def make_config(tmp_path, **kwargs) -> ModerationConfig:
    defaults = dict(
        service_url="http://sensitive-content.test",
        cache_path=str(tmp_path / "moderation" / "policy-snapshot.json"),
        max_stale=600.0,
    )
    defaults.update(kwargs)
    return ModerationConfig(**defaults)


def snapshot_payload(version="global-2:tenant-3", etag='"etag-1"'):
    return {
        "version": version,
        "etag": etag,
        "types": [make_type(1).to_payload()],
        "rules": [make_rule(1, 1, "敏感词").to_payload()],
    }


class TestMemoryAndDisk:
    def test_install_and_get_policy(self, tmp_path):
        client = SnapshotClient(make_config(tmp_path))
        client._install_snapshot(
            make_snapshot([make_type(1)], [make_rule(1, 1, "敏感词")], tenant_id=TENANT),
            persist=False,
        )
        policy = client.get_policy(TENANT)
        assert policy is not None
        assert policy.version == "global-1:tenant-1"

    def test_no_snapshot_returns_none(self, tmp_path):
        client = SnapshotClient(make_config(tmp_path))
        assert client.get_policy(TENANT) is None

    def test_agent_caches_are_isolated(self, tmp_path):
        client = SnapshotClient(make_config(tmp_path))
        client._install_snapshot(
            make_snapshot(
                [make_type(1)], [make_rule(1, 1, "one")],
                tenant_id=TENANT, agent_id=1, binding_rule_ids=[1],
            ),
            persist=False,
        )
        client._install_snapshot(
            make_snapshot(
                [make_type(1)], [make_rule(2, 1, "two")],
                tenant_id=TENANT, agent_id=2, binding_rule_ids=[2],
            ),
            persist=False,
        )
        assert client.get_policy(TENANT, 1).snapshot.rules[0].pattern == "one"
        assert client.get_policy(TENANT, 2).snapshot.rules[0].pattern == "two"

    def test_legacy_tenant_cache_is_discarded(self, tmp_path):
        cfg = make_config(tmp_path)
        os.makedirs(os.path.dirname(cfg.cache_path), exist_ok=True)
        with open(cfg.cache_path, "w", encoding="utf-8") as fh:
            json.dump({"tenants": {TENANT: snapshot_payload()}}, fh)
        client = SnapshotClient(cfg)
        assert not os.path.exists(cfg.cache_path)
        assert client.get_policy(TENANT, 1) is None

    def test_stale_snapshot_returns_none_and_warns(self, tmp_path, caplog):
        client = SnapshotClient(make_config(tmp_path, max_stale=10))
        stale = make_snapshot(
            [make_type(1)], [make_rule(1, 1, "x")], tenant_id=TENANT,
            fetched_at=time.time() - 100,
        )
        client._install_snapshot(stale, persist=False)
        with caplog.at_level("WARNING"):
            assert client.get_policy(TENANT) is None
        assert any("过期上限" in r.message for r in caplog.records)

    def test_atomic_persist_and_reload(self, tmp_path):
        cfg = make_config(tmp_path)
        client = SnapshotClient(cfg)
        client._install_snapshot(
            make_snapshot([make_type(1)], [make_rule(1, 1, "敏感词")], tenant_id=TENANT),
            persist=True,
        )
        # 文件存在且为合法 JSON，无残留临时文件
        assert os.path.exists(cfg.cache_path)
        with open(cfg.cache_path, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["format_version"] == 2
        assert data["snapshots"][0]["tenant_id"] == TENANT
        leftovers = [f for f in os.listdir(os.path.dirname(cfg.cache_path)) if f.endswith(".tmp")]
        assert leftovers == []
        # 新客户端启动时加载落盘缓存
        client2 = SnapshotClient(cfg)
        assert client2.get_policy(TENANT) is not None

    def test_corrupted_cache_self_heals(self, tmp_path, caplog):
        cfg = make_config(tmp_path)
        os.makedirs(os.path.dirname(cfg.cache_path), exist_ok=True)
        with open(cfg.cache_path, "w", encoding="utf-8") as fh:
            fh.write("{corrupted json!!")
        with caplog.at_level("WARNING"):
            client = SnapshotClient(cfg)
        # 损坏文件被删除自愈，客户端可正常使用
        assert not os.path.exists(cfg.cache_path)
        assert client.get_policy(TENANT) is None
        assert any("损坏" in r.message for r in caplog.records)

    def test_unwritable_dir_degrades_to_memory_only(self, tmp_path, caplog):
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        os.chmod(readonly_dir, 0o500)
        try:
            cfg = make_config(
                tmp_path,
                cache_path=str(readonly_dir / "sub" / "policy-snapshot.json"),
            )
            with caplog.at_level("WARNING"):
                client = SnapshotClient(cfg)
            assert client._disk_enabled is False
            assert any("纯内存" in r.message for r in caplog.records)
            # 纯内存模式下 install/persist 不崩溃
            client._install_snapshot(
                make_snapshot([make_type(1)], [make_rule(1, 1, "x")], tenant_id=TENANT),
                persist=True,
            )
            assert client.get_policy(TENANT) is not None
        finally:
            os.chmod(readonly_dir, 0o700)

    def test_cache_dir_created_with_0700(self, tmp_path):
        cfg = make_config(tmp_path)
        SnapshotClient(cfg)
        mode = os.stat(os.path.dirname(cfg.cache_path)).st_mode & 0o777
        assert mode == 0o700


class TestRefresh:
    def _client_with_transport(self, tmp_path, handler, **cfg_kwargs):
        cfg = make_config(tmp_path, **cfg_kwargs)
        client = SnapshotClient(cfg)
        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        return client, http

    def test_fetch_installs_snapshot(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"/internal/v1/tenants/{TENANT}/agents/0/policy-snapshot"
            return httpx.Response(200, json=snapshot_payload())

        client, http = self._client_with_transport(tmp_path, handler)
        ok = asyncio.run(client.fetch(TENANT, client=http))
        assert ok is True
        policy = client.get_policy(TENANT)
        assert policy is not None
        assert policy.version == "global-2:tenant-3"

    def test_304_renews_cached_snapshot(self, tmp_path):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(dict(request.headers))
            if "if-none-match" in request.headers:
                return httpx.Response(304)
            return httpx.Response(200, json=snapshot_payload())

        client, http = self._client_with_transport(tmp_path, handler, max_stale=5)
        asyncio.run(client.fetch(TENANT, client=http))
        snapshot = client._snapshots[(TENANT, 0)]
        # 人为做旧后 304 续期
        snapshot.fetched_at = time.time() - 100
        assert client.get_policy(TENANT) is None  # 已过期
        ok = asyncio.run(client.fetch(TENANT, client=http))
        assert ok is True
        assert client.get_policy(TENANT) is not None
        # 第二次请求带了 If-None-Match
        assert calls[1].get("if-none-match") == '"etag-1"'

    def test_service_error_keeps_cached_snapshot(self, tmp_path):
        """管理服务不可用时沿用最后有效快照。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client, http = self._client_with_transport(tmp_path, handler)
        client._install_snapshot(
            make_snapshot([make_type(1)], [make_rule(1, 1, "x")], tenant_id=TENANT),
            persist=False,
        )
        with pytest.raises(httpx.ConnectError):
            asyncio.run(client.fetch(TENANT, client=http))
        # 快照仍然有效
        assert client.get_policy(TENANT) is not None

    def test_runtime_token_sent(self, tmp_path):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json=snapshot_payload())

        client, http = self._client_with_transport(
            tmp_path, handler, runtime_token="secret-token"
        )
        asyncio.run(client.fetch(TENANT, client=http))
        assert seen["auth"] == "Bearer secret-token"

"""跨包契约测试：openagno 检测端 vs sensitive-content 管理服务内部 API。

通过 sys.path 引入 sensitive-content/src，用 httpx ASGITransport 把真实的
FastAPI 内部 API 应用挂给 openagno 的 SnapshotClient / HitReporter，验证：

- 快照拉取：URL 路径、Bearer Runtime Token、If-None-Match/ETag/304 语义、
  组合版本字符串格式，以及服务端序列化字段能被客户端逐一解析；
- 事件上报：reporter 构造的请求体能通过服务端 pydantic 校验并落到
  insert_hit_events 的入参；
- 字段集合防漂移断言：服务端快照规则字段集 / HitEventIn 模型字段集与
  客户端序列化保持一致。

DB 访问通过 monkeypatch store 的装载/写入函数替代（内部 API 契约与
存储实现无关）；快照内容用服务端真实的纯函数（merge_rules /
combined_version / compute_etag）生成，保证序列化路径与生产一致。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

# 引入 sensitive-content 源码（与 openagno 同仓库，路径固定）
_SENSITIVE_CONTENT_SRC = Path(__file__).resolve().parents[2] / "sensitive-content" / "src"
if str(_SENSITIVE_CONTENT_SRC) not in sys.path:
    sys.path.insert(0, str(_SENSITIVE_CONTENT_SRC))

from sensitive_content import store as sc_store  # noqa: E402
from sensitive_content.api import create_app  # noqa: E402
from sensitive_content.models import HitEventIn  # noqa: E402

from agno_worker.moderation.config import ModerationConfig  # noqa: E402
from agno_worker.moderation.models import (  # noqa: E402
    ActionType,
    DecisionKind,
    GuardrailDecision,
    Match,
    PolicySnapshot,
    SensitiveRule,
    SensitiveType,
)
from agno_worker.moderation.reporter import HitReporter, build_hit_events  # noqa: E402
from agno_worker.moderation.snapshot import SnapshotClient  # noqa: E402

RUNTIME_TOKEN = "contract-runtime-token"
SERVICE_URL = "http://sensitive-content.test"
TENANT = "tenant-a"


# ---------------------------------------------------------------------------
# 服务端快照构造：走真实纯函数（merge_rules / combined_version / compute_etag）
# ---------------------------------------------------------------------------

def _rule_row(
    rule_id: int,
    tenant_id: str,
    type_id: int,
    pattern: str,
    *,
    match_mode: str = "text",
    case_sensitive: bool = False,
    normalize: bool = True,
    priority: int = 0,
    enabled: bool = True,
    overrides: int | None = None,
) -> dict[str, Any]:
    """模拟 load_policy_snapshot 从数据库取出的规则行。"""
    return {
        "id": rule_id,
        "tenant_id": tenant_id,
        "type_id": type_id,
        "pattern": pattern,
        "match_mode": match_mode,
        "case_sensitive": case_sensitive,
        "normalize": normalize,
        "priority": priority,
        "enabled": enabled,
        "overrides_global_rule_id": overrides,
    }


def _type_row(
    type_id: int,
    tenant_id: str,
    code: str,
    *,
    action: str = "LOG_ONLY",
    action_config: dict[str, Any] | None = None,
    priority: int = 0,
) -> dict[str, Any]:
    return {
        "id": type_id,
        "tenant_id": tenant_id,
        "code": code,
        "name": f"类型{type_id}",
        "action": action,
        "action_config": action_config or {},
        "priority": priority,
    }


def build_server_snapshot(
    tenant_id: str,
    *,
    global_rules: list[dict[str, Any]],
    tenant_rules: list[dict[str, Any]],
    types: list[dict[str, Any]],
    global_version: int,
    tenant_version: int,
) -> dict[str, Any]:
    """复刻 store.load_policy_snapshot 的序列化路径（合并 → 字段裁剪 → ETag）。"""
    merged = sc_store.merge_rules(global_rules, tenant_rules, set())
    content = {
        "tenant_id": tenant_id,
        "types": types,
        "rules": [sc_store._snapshot_rule(r) for r in merged],
    }
    return {
        "version": sc_store.combined_version(global_version, tenant_version),
        "etag": sc_store.compute_etag(content),
        **content,
    }


SAMPLE_SNAPSHOT = build_server_snapshot(
    TENANT,
    global_rules=[
        _rule_row(1, "", 10, "全局敏感词", priority=5),
        _rule_row(
            2, "", 10, r"\d{11}", match_mode="regex", case_sensitive=True, normalize=False
        ),
    ],
    tenant_rules=[
        _rule_row(30, TENANT, 20, "租户敏感词", priority=9),
    ],
    types=[
        _type_row(10, "", "politics", action="BLOCK_REQUEST", priority=100),
        _type_row(
            20,
            TENANT,
            "custom",
            action="FIXED_REPLY",
            action_config={"reply_text": "该话题不予讨论"},
            priority=50,
        ),
    ],
    global_version=12,
    tenant_version=37,
)


# ---------------------------------------------------------------------------
# 夹具：真实内部 API 应用 + ASGI 客户端
# ---------------------------------------------------------------------------

@pytest.fixture()
def server_app(monkeypatch):
    monkeypatch.setenv("SENSITIVE_CONTENT_RUNTIME_TOKEN", RUNTIME_TOKEN)
    return create_app(enable_cleanup=False)


@pytest.fixture()
def snapshot_calls(monkeypatch):
    """monkeypatch 快照装载：记录 tenant 入参并返回真实序列化的快照。"""
    calls: list[str] = []

    def fake_load(tenant_id: str) -> dict[str, Any]:
        calls.append(tenant_id)
        return SAMPLE_SNAPSHOT

    monkeypatch.setattr(sc_store, "load_policy_snapshot", fake_load)
    return calls


@pytest.fixture()
def inserted_events(monkeypatch):
    """monkeypatch 事件写入：记录服务端校验后的事件行。"""
    rows: list[dict[str, Any]] = []

    def fake_insert(events: list[dict[str, Any]]) -> tuple[int, int]:
        rows.extend(events)
        return len(events), 0

    monkeypatch.setattr(sc_store, "insert_hit_events", fake_insert)
    return rows


def make_asgi_client(app, statuses: list[int] | None = None) -> httpx.AsyncClient:
    hooks = {}
    if statuses is not None:
        async def record(response: httpx.Response) -> None:
            statuses.append(response.status_code)

        hooks = {"response": [record]}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), event_hooks=hooks
    )


def make_client_config(tmp_path, **kwargs) -> ModerationConfig:
    defaults = dict(
        service_url=SERVICE_URL,
        runtime_token=RUNTIME_TOKEN,
        cache_path=str(tmp_path / "moderation" / "policy-snapshot.json"),
        fingerprint_key="contract-fingerprint-key",
    )
    defaults.update(kwargs)
    return ModerationConfig(**defaults)


# ---------------------------------------------------------------------------
# 快照契约
# ---------------------------------------------------------------------------

class TestSnapshotContract:
    def test_fetch_parses_real_server_response(self, server_app, snapshot_calls, tmp_path):
        """真实客户端 → 真实服务端：URL / 鉴权 / 字段逐一解析。"""
        client = SnapshotClient(make_client_config(tmp_path))
        http = make_asgi_client(server_app)
        ok = asyncio.run(client.fetch(TENANT, client=http))
        assert ok is True
        assert snapshot_calls == [TENANT]

        snapshot = client._snapshots[TENANT]
        # 组合版本字符串格式与 ETag 均来自服务端
        assert snapshot.version == "global-12:tenant-37"
        assert snapshot.etag == SAMPLE_SNAPSHOT["etag"]

        # 规则字段逐一对齐（含合并语义：租户规则 + 全局规则）
        rules = {r.id: r for r in snapshot.rules}
        assert set(rules) == {1, 2, 30}
        assert rules[1].pattern == "全局敏感词"
        assert rules[1].tenant_id == ""
        assert rules[1].type_id == 10
        assert rules[1].priority == 5
        assert rules[1].match_mode == "text"
        assert rules[1].case_sensitive is False
        assert rules[1].normalize is True
        assert rules[2].match_mode == "regex"
        assert rules[2].case_sensitive is True
        assert rules[2].normalize is False
        assert rules[30].tenant_id == TENANT
        assert rules[30].priority == 9

        # 类型字段逐一对齐（action / action_config / priority）
        types = snapshot.types
        assert set(types) == {10, 20}
        assert types[10].action is ActionType.BLOCK_REQUEST
        assert types[10].priority == 100
        assert types[20].action is ActionType.FIXED_REPLY
        assert types[20].action_config == {"reply_text": "该话题不予讨论"}
        assert types[20].tenant_id == TENANT

        # 快照可直接编译为可执行策略（detector 消费端）
        assert client.get_policy(TENANT) is not None

    def test_if_none_match_returns_304_and_renews(self, server_app, snapshot_calls, tmp_path):
        """第二次拉取带 If-None-Match，服务端内容未变返回 304，客户端续期。"""
        statuses: list[int] = []
        client = SnapshotClient(make_client_config(tmp_path))
        http = make_asgi_client(server_app, statuses)
        asyncio.run(client.fetch(TENANT, client=http))
        first_version = client._snapshots[TENANT].version
        asyncio.run(client.fetch(TENANT, client=http))
        assert statuses == [200, 304]
        assert client._snapshots[TENANT].version == first_version
        assert client.get_policy(TENANT) is not None

    def test_invalid_runtime_token_rejected(self, server_app, snapshot_calls, tmp_path):
        """Bearer Runtime Token 不匹配时服务端返回 401。"""
        client = SnapshotClient(make_client_config(tmp_path, runtime_token="wrong-token"))
        http = make_asgi_client(server_app)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(client.fetch(TENANT, client=http))
        assert exc_info.value.response.status_code == 401

    def test_empty_tenant_maps_to_global_path(self, server_app, snapshot_calls, tmp_path):
        """客户端空租户请求 /tenants/global/…，服务端映射回库内 ''。"""
        client = SnapshotClient(make_client_config(tmp_path))
        http = make_asgi_client(server_app)
        asyncio.run(client.fetch("", client=http))
        assert snapshot_calls == [""]

    def test_snapshot_rule_fields_subset_of_client_model(self):
        """防漂移：服务端快照规则字段集必须被客户端模型完整覆盖。"""
        client_rule_fields = set(
            SensitiveRule(id=1, type_id=1, pattern="x").to_payload()
        )
        assert set(sc_store._SNAPSHOT_RULE_FIELDS) <= client_rule_fields


# ---------------------------------------------------------------------------
# 命中事件上报契约
# ---------------------------------------------------------------------------

def make_decision() -> GuardrailDecision:
    """两条命中：final_action 一致，仅排序第一条 selected=TRUE。"""
    matches = [
        Match(
            rule_id=1, type_id=10, action=ActionType.BLOCK_REQUEST,
            spans=((0, 4),), rule_priority=5, type_priority=100,
        ),
        Match(
            rule_id=30, type_id=20, action=ActionType.LOG_ONLY,
            spans=((6, 10), (12, 16)), rule_priority=9, type_priority=50,
        ),
    ]
    return GuardrailDecision(
        kind=DecisionKind.REJECT,
        action=ActionType.BLOCK_REQUEST,
        rule_id=1,
        type_id=10,
        matches=matches,
        policy_version="global-12:tenant-37",
    )


class TestHitEventContract:
    def _events(self):
        return build_hit_events(
            make_decision(),
            tenant_id=TENANT,
            request_id=uuid.uuid4().hex,
            session_id="session-1",
            fingerprint_key="contract-fingerprint-key",
        )

    def test_reporter_post_passes_server_validation(
        self, server_app, inserted_events, tmp_path
    ):
        """真实 reporter._post 请求体通过服务端 pydantic 校验并落库入参正确。"""
        events = self._events()
        http = make_asgi_client(server_app)
        reporter = HitReporter(make_client_config(tmp_path), client=http)
        asyncio.run(reporter._post(events))
        asyncio.run(http.aclose())

        assert len(inserted_events) == 2
        by_rule = {row["rule_id"]: row for row in inserted_events}
        assert by_rule[1]["rule_action"] == "BLOCK_REQUEST"
        assert by_rule[30]["rule_action"] == "LOG_ONLY"
        for row in inserted_events:
            # final_action / final_rule_id 一致；仅一条 selected=TRUE
            assert row["final_action"] == "BLOCK_REQUEST"
            assert row["final_rule_id"] == 1
            assert row["tenant_id"] == TENANT
            assert row["policy_version"] == "global-12:tenant-37"
            # 明文 session_id 通过服务端校验并原样落到写入入参
            assert row["session_id"] == "session-1"
            # event_id 为合法 UUID（服务端已完成 UUID 解析）
            uuid.UUID(row["event_id"])
            # hit_at ISO 时间串通过服务端 datetime 校验
            assert row["hit_at"] is not None
        assert [row["selected"] for row in inserted_events].count(True) == 1
        assert by_rule[30]["hit_count"] == 2

    def test_invalid_token_rejected(self, server_app, inserted_events, tmp_path):
        http = make_asgi_client(server_app)
        reporter = HitReporter(
            make_client_config(tmp_path, runtime_token="wrong-token"), client=http
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(reporter._post(self._events()))
        asyncio.run(http.aclose())
        assert exc_info.value.response.status_code == 401
        assert inserted_events == []

    def test_event_payload_fields_match_server_model(self):
        """防漂移：客户端事件字段集与服务端 HitEventIn 模型字段集一致。"""
        payload_fields = set(self._events()[0].to_payload())
        server_fields = set(HitEventIn.model_fields)
        assert payload_fields == server_fields

    def test_snapshot_roundtrip_no_plaintext_loss(self):
        """服务端响应 → 客户端解析 → 客户端落盘 payload 再解析，字段不丢失。"""
        parsed = PolicySnapshot.from_payload(TENANT, SAMPLE_SNAPSHOT)
        reparsed = PolicySnapshot.from_payload(TENANT, parsed.to_payload())
        assert reparsed.version == parsed.version
        assert [r.to_payload() for r in reparsed.rules] == [
            r.to_payload() for r in parsed.rules
        ]
        assert {t.id: t.to_payload() for t in reparsed.types.values()} == {
            t.id: t.to_payload() for t in parsed.types.values()
        }

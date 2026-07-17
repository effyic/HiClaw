"""集成测试：统计查询（summary/by-rule/by-type/by-action/trend、去重、保留期清理）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from conftest import ADMIN_HEADERS
from helpers import post_events

# 固定时间基线，便于断言 trend 分桶与时间范围
DAY1 = "2026-07-10"
DAY2 = "2026-07-11"


def _event(
    rule_id: int,
    type_id: int,
    rule_action: str,
    final_action: str,
    *,
    selected: bool,
    final_rule_id: int,
    fp: str,
    hit_at: str,
    tenant_id: str = "mt",
    hit_count: int = 1,
    session_id: str = "",
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "rule_id": rule_id,
        "type_id": type_id,
        "rule_action": rule_action,
        "final_action": final_action,
        "selected": selected,
        "final_rule_id": final_rule_id,
        "tenant_id": tenant_id,
        "request_fingerprint": fp,
        "session_fingerprint": "sess",
        "session_id": session_id,
        "policy_version": "global-1:tenant-1",
        "hit_count": hit_count,
        "hit_at": hit_at,
    }


@pytest.fixture()
def seeded_events(client):
    """构造统计数据集：
    - R1：同请求命中 LOG_ONLY(规则101) + BLOCK_REQUEST(规则102)，最终 BLOCK，仅 102 selected
    - R2：规则101 命中 2 次（hit_count=2），最终 LOG_ONLY
    - R3：次日命中规则103（type 2），最终 REDACT_AND_CONTINUE
    - 另一租户 ot 一条事件
    """
    events = [
        _event(101, 1, "LOG_ONLY", "BLOCK_REQUEST",
               selected=False, final_rule_id=102, fp="fp-r1",
               hit_at=f"{DAY1}T10:00:00Z", session_id="sess-1"),
        _event(102, 1, "BLOCK_REQUEST", "BLOCK_REQUEST",
               selected=True, final_rule_id=102, fp="fp-r1",
               hit_at=f"{DAY1}T10:00:00Z", session_id="sess-1"),
        _event(101, 1, "LOG_ONLY", "LOG_ONLY",
               selected=True, final_rule_id=101, fp="fp-r2",
               hit_at=f"{DAY1}T11:00:00Z", hit_count=2, session_id="sess-2"),
        _event(103, 2, "REDACT_AND_CONTINUE", "REDACT_AND_CONTINUE",
               selected=True, final_rule_id=103, fp="fp-r3",
               hit_at=f"{DAY2}T10:00:00Z"),
        _event(201, 3, "LOG_ONLY", "LOG_ONLY",
               selected=True, final_rule_id=201, fp="fp-other",
               hit_at=f"{DAY1}T12:00:00Z", tenant_id="ot"),
    ]
    resp = post_events(client, events)
    assert resp.json()["accepted"] == 5
    return client


def _get(client, tenant: str, path: str, **params):
    resp = client.get(
        f"/api/v1/tenants/{tenant}/metrics/{path}", headers=ADMIN_HEADERS, params=params
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestSummary:
    def test_admin_token_required(self, client):
        resp = client.get("/api/v1/tenants/mt/metrics/summary")
        assert resp.status_code == 401

    def test_summary_counts(self, seeded_events):
        body = _get(seeded_events, "mt", "summary")
        assert body["total_events"] == 4
        assert body["total_hits"] == 5
        # 命中请求数按 request_fingerprint 去重（R1 两条事件同一指纹）
        assert body["hit_requests"] == 3
        assert body["final_actions"] == {
            "BLOCK_REQUEST": 1,
            "LOG_ONLY": 1,
            "REDACT_AND_CONTINUE": 1,
        }

    def test_global_aggregates_all_tenants(self, seeded_events):
        body = _get(seeded_events, "global", "summary")
        assert body["total_events"] == 5
        assert body["hit_requests"] == 4

    def test_time_range(self, seeded_events):
        body = _get(
            seeded_events, "mt", "summary",
            **{"from": f"{DAY2}T00:00:00Z", "to": f"{DAY2}T23:59:59Z"},
        )
        assert body["total_events"] == 1
        assert body["final_actions"] == {"REDACT_AND_CONTINUE": 1}


class TestByRule:
    def test_ranking_hits_and_last_hit(self, seeded_events):
        items = _get(seeded_events, "mt", "by-rule")["items"]
        assert [i["rule_id"] for i in items] == [101, 102, 103]
        top = items[0]
        assert top["hits"] == 3          # 1 + hit_count 2
        assert top["events"] == 2        # 事件行数
        assert top["last_hit_at"].startswith(f"{DAY1}T11:00:00")
        # 最近一次命中的会话标识（规则 101 最近命中在 sess-2）
        assert top["last_session_id"] == "sess-2"
        # 规则 103 的事件未带 session_id：示例会话为空
        by_id = {i["rule_id"]: i for i in items}
        assert by_id[103]["last_session_id"] is None

    def test_top_n(self, seeded_events):
        items = _get(seeded_events, "mt", "by-rule", top=1)["items"]
        assert len(items) == 1
        assert items[0]["rule_id"] == 101


class TestByType:
    def test_grouping(self, seeded_events):
        items = _get(seeded_events, "mt", "by-type")["items"]
        by_id = {i["type_id"]: i for i in items}
        assert by_id[1]["hits"] == 4
        assert by_id[2]["hits"] == 1
        assert by_id[2]["last_hit_at"].startswith(f"{DAY2}T10:00:00")


class TestByAction:
    def test_dual_dimensions(self, seeded_events):
        body = _get(seeded_events, "mt", "by-action")
        rule_dim = {i["rule_action"]: i for i in body["by_rule_action"]}
        final_dim = {i["final_action"]: i["requests"] for i in body["by_final_action"]}
        # rule_action 维度：配置行为的命中次数（全部事件行）
        assert rule_dim["LOG_ONLY"]["hits"] == 3
        assert rule_dim["LOG_ONLY"]["events"] == 2
        assert rule_dim["BLOCK_REQUEST"]["hits"] == 1
        # final_action 维度：实际执行的请求数——R1 同时命中 LOG_ONLY 和
        # BLOCK_REQUEST，最终行为只计 BLOCK_REQUEST 一次
        assert final_dim == {
            "BLOCK_REQUEST": 1,
            "LOG_ONLY": 1,
            "REDACT_AND_CONTINUE": 1,
        }
        assert "LOG_ONLY" in rule_dim  # LOG_ONLY 作为配置行为仍被统计


class TestTrend:
    def test_daily_buckets(self, seeded_events):
        items = _get(seeded_events, "mt", "trend", granularity="day")["items"]
        assert len(items) == 2
        first, second = items
        assert first["bucket"].startswith(DAY1)
        assert first["events"] == 3
        assert first["requests"] == 2
        assert second["bucket"].startswith(DAY2)
        assert second["events"] == 1

    def test_hour_granularity(self, seeded_events):
        items = _get(seeded_events, "mt", "trend", granularity="hour")["items"]
        assert len(items) == 3

    def test_invalid_granularity_rejected(self, seeded_events):
        resp = seeded_events.get(
            "/api/v1/tenants/mt/metrics/trend",
            headers=ADMIN_HEADERS,
            params={"granularity": "minute; DROP TABLE x"},
        )
        assert resp.status_code == 400


class TestRetention:
    def test_purge_expired_events_default_90_days(self, client):
        from sensitive_content.config import retention_days
        from sensitive_content.store import purge_expired_hit_events

        assert retention_days() == 90
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=100)).isoformat()
        recent = (now - timedelta(days=1)).isoformat()
        post_events(client, [
            _event(1, 1, "LOG_ONLY", "LOG_ONLY", selected=True,
                   final_rule_id=1, fp="old", hit_at=old),
            _event(1, 1, "LOG_ONLY", "LOG_ONLY", selected=True,
                   final_rule_id=1, fp="recent", hit_at=recent),
        ])
        deleted = purge_expired_hit_events(retention_days())
        assert deleted == 1
        body = _get(client, "global", "summary")
        assert body["total_events"] == 1

"""集成测试：命中事件批量采集（event_id 幂等）与管理端明细查询。"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from conftest import ADMIN_HEADERS
from helpers import post_events


def _event(**overrides: Any) -> dict[str, Any]:
    body = {
        "event_id": str(uuid.uuid4()),
        "rule_id": 1,
        "type_id": 1,
        "rule_action": "BLOCK_REQUEST",
        "final_action": "BLOCK_REQUEST",
        "selected": True,
        "final_rule_id": 1,
        "tenant_id": "t1",
        "request_fingerprint": "req-fp-1",
        "session_fingerprint": "sess-fp-1",
        "policy_version": "global-1:tenant-1",
        "hit_count": 1,
    }
    body.update(overrides)
    return body


def _event_rows() -> int:
    from sensitive_content.db import db_connection

    with db_connection() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM sensitive_content.hit_event")
        ).first()
    return int(row[0])


class TestHitEventsBatch:
    def test_runtime_token_required(self, client):
        resp = client.post("/internal/v1/hit-events:batch", json={"events": []})
        assert resp.status_code == 401

    def test_batch_insert(self, client):
        resp = post_events(client, [_event(), _event(), _event()])
        assert resp.status_code == 200
        assert resp.json() == {"accepted": 3, "duplicates": 0}
        assert _event_rows() == 3

    def test_same_event_id_idempotent(self, client):
        event = _event()
        assert post_events(client, [event]).json() == {"accepted": 1, "duplicates": 0}
        # 重复上报同一 event_id：ON CONFLICT DO NOTHING
        assert post_events(client, [event]).json() == {"accepted": 0, "duplicates": 1}
        assert _event_rows() == 1

    def test_partial_duplicates_in_one_batch(self, client):
        dup = _event()
        post_events(client, [dup])
        resp = post_events(client, [dup, _event()])
        assert resp.json() == {"accepted": 1, "duplicates": 1}
        assert _event_rows() == 2

    def test_event_fields_persisted(self, client):
        from sensitive_content.db import db_connection

        event = _event(
            rule_action="LOG_ONLY", final_action="BLOCK_REQUEST",
            selected=False, final_rule_id=42, hit_count=3,
            hit_at="2026-07-01T08:00:00Z",
        )
        post_events(client, [event])
        with db_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT rule_action, final_action, selected, final_rule_id, "
                    "hit_count, policy_version FROM sensitive_content.hit_event"
                )
            ).mappings().first()
        assert row["rule_action"] == "LOG_ONLY"
        assert row["final_action"] == "BLOCK_REQUEST"
        assert row["selected"] is False
        assert row["final_rule_id"] == 42
        assert row["hit_count"] == 3
        assert row["policy_version"] == "global-1:tenant-1"

    def test_invalid_action_rejected(self, client):
        resp = post_events(client, [_event(rule_action="NOT_AN_ACTION")])
        assert resp.status_code == 422

    def test_session_id_persisted(self, client):
        from sensitive_content.db import db_connection

        post_events(client, [_event(session_id="sess-abc")])
        with db_connection() as conn:
            row = conn.execute(
                text("SELECT session_id FROM sensitive_content.hit_event")
            ).first()
        assert row[0] == "sess-abc"

    def test_session_id_optional_defaults_empty(self, client):
        """旧检测端不带 session_id 字段时兼容写入空字符串。"""
        from sensitive_content.db import db_connection

        post_events(client, [_event()])
        with db_connection() as conn:
            row = conn.execute(
                text("SELECT session_id FROM sensitive_content.hit_event")
            ).first()
        assert row[0] == ""


class TestHitEventsAdminQuery:
    """管理端命中事件明细查询：分页 + rule/type/session/时间范围筛选。"""

    def _seed(self, client) -> None:
        self.events = [
            _event(rule_id=1, type_id=1, session_id="sess-a",
                   hit_at="2026-07-10T10:00:00Z", tenant_id="t1"),
            _event(rule_id=2, type_id=1, session_id="sess-a",
                   hit_at="2026-07-10T11:00:00Z", tenant_id="t1"),
            _event(rule_id=2, type_id=2, session_id="sess-b",
                   hit_at="2026-07-11T10:00:00Z", tenant_id="t1"),
            _event(rule_id=9, type_id=9, session_id="sess-other",
                   hit_at="2026-07-10T10:00:00Z", tenant_id="t2"),
        ]
        assert post_events(client, self.events).json()["accepted"] == 4

    def _get(self, client, tenant: str, **params):
        resp = client.get(
            f"/api/v1/tenants/{tenant}/hit-events",
            headers=ADMIN_HEADERS,
            params=params,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_admin_token_required(self, client):
        resp = client.get("/api/v1/tenants/t1/hit-events")
        assert resp.status_code == 401

    def test_list_returns_session_id_desc_by_hit_at(self, client):
        self._seed(client)
        body = self._get(client, "t1")
        assert body["total"] == 3
        items = body["items"]
        # 按 hit_at 倒序，响应携带明文 session_id 供前端跳转会话详情
        assert [i["session_id"] for i in items] == ["sess-b", "sess-a", "sess-a"]
        first = items[0]
        assert first["rule_id"] == 2
        assert first["type_id"] == 2
        assert first["tenant_id"] == "t1"
        assert uuid.UUID(first["event_id"])
        assert first["hit_at"].startswith("2026-07-11T10:00:00")

    def test_filter_by_rule_and_type(self, client):
        self._seed(client)
        assert self._get(client, "t1", rule_id=2)["total"] == 2
        assert self._get(client, "t1", rule_id=2, type_id=2)["total"] == 1
        assert self._get(client, "t1", rule_id=404)["total"] == 0

    def test_filter_by_session_id(self, client):
        self._seed(client)
        body = self._get(client, "t1", session_id="sess-a")
        assert body["total"] == 2
        assert {i["session_id"] for i in body["items"]} == {"sess-a"}

    def test_filter_by_time_range(self, client):
        self._seed(client)
        body = self._get(
            client, "t1",
            **{"from": "2026-07-11T00:00:00Z", "to": "2026-07-11T23:59:59Z"},
        )
        assert body["total"] == 1
        assert body["items"][0]["session_id"] == "sess-b"

    def test_tenant_isolation_and_global(self, client):
        self._seed(client)
        # 租户视角只见本租户事件
        assert self._get(client, "t2")["total"] == 1
        # global 跨全部租户
        assert self._get(client, "global")["total"] == 4

    def test_pagination(self, client):
        self._seed(client)
        body = self._get(client, "t1", page=2, page_size=2)
        assert body["total"] == 3
        assert len(body["items"]) == 1
        assert body["page"] == 2

"""集成测试：命中事件批量采集（event_id 幂等）。"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

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

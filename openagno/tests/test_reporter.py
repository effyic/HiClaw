"""reporter 测试：HMAC 指纹稳定性、有界队列丢弃计数、批量上报与退避重试。"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.models import ActionType, GuardrailDecision, DecisionKind, Match
from agno_worker.moderation.reporter import (
    HitReporter,
    build_hit_events,
    hmac_fingerprint,
)


def make_decision(num_matches: int = 2) -> GuardrailDecision:
    matches = [
        Match(
            rule_id=i + 1,
            type_id=1,
            action=ActionType.LOG_ONLY if i else ActionType.BLOCK_REQUEST,
            spans=((0, 3),),
        )
        for i in range(num_matches)
    ]
    return GuardrailDecision(
        kind=DecisionKind.REJECT,
        action=ActionType.BLOCK_REQUEST,
        rule_id=1,
        type_id=1,
        matches=matches,
        policy_version="global-1:tenant-1",
    )


def make_events(num: int = 2):
    return build_hit_events(
        make_decision(num),
        tenant_id="tenant-a",
        request_id="req-1",
        session_id="session-1",
        fingerprint_key="key",
    )


class TestFingerprint:
    def test_hmac_stability(self):
        a = hmac_fingerprint("key", "value")
        b = hmac_fingerprint("key", "value")
        assert a == b
        assert len(a) == 64

    def test_different_key_different_output(self):
        assert hmac_fingerprint("key1", "value") != hmac_fingerprint("key2", "value")

    def test_different_value_different_output(self):
        assert hmac_fingerprint("key", "v1") != hmac_fingerprint("key", "v2")

    def test_empty_value_empty_fingerprint(self):
        assert hmac_fingerprint("key", "") == ""


class TestBuildEvents:
    def test_event_ids_unique(self):
        events = make_events(3)
        assert len({e.event_id for e in events}) == 3

    def test_selected_only_first(self):
        events = make_events(3)
        assert [e.selected for e in events] == [True, False, False]

    def test_request_fingerprint_not_raw_id(self):
        """request_id 不得以明文出现；session 指纹也不等于原始标识。"""
        events = make_events(1)
        payload = events[0].to_payload()
        assert "req-1" not in json.dumps(payload)
        assert payload["session_fingerprint"] != "session-1"

    def test_plain_session_id_carried(self):
        """明文 session_id 随事件携带（后台跳转会话详情用），与指纹并存。"""
        events = make_events(2)
        for event in events:
            payload = event.to_payload()
            assert payload["session_id"] == "session-1"
            assert len(payload["session_fingerprint"]) == 64


def run_async(coro):
    return asyncio.run(coro)


class TestQueue:
    def test_bounded_queue_drops_and_counts(self):
        config = ModerationConfig(
            service_url="http://svc.test", reporter_queue_size=4
        )
        reporter = HitReporter(config)
        dropped_before = reporter.dropped_count
        accepted = reporter.enqueue(make_events(2) * 5)  # 10 条
        assert accepted == 4
        assert reporter.dropped_count == dropped_before + 6


class TestBatchPost:
    def test_batch_post_payload(self):
        received: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/internal/v1/hit-events:batch"
            assert request.headers.get("authorization") == "Bearer rt-token"
            received.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": True})

        config = ModerationConfig(
            service_url="http://svc.test", runtime_token="rt-token"
        )

        async def scenario():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            reporter = HitReporter(config, client=client)
            reporter.enqueue(make_events(2))
            reporter.ensure_started()
            # 等待后台任务消费
            for _ in range(50):
                await asyncio.sleep(0.01)
                if received:
                    break
            await reporter.stop()

        run_async(scenario())
        assert received
        events = received[0]["events"]
        assert len(events) == 2
        assert all("event_id" in e for e in events)

    def test_retry_then_success(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(500)
            return httpx.Response(200)

        config = ModerationConfig(service_url="http://svc.test")

        async def scenario():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            reporter = HitReporter(config, client=client)
            # 直接调用发送逻辑（缩短退避由 monkey 时间控制不必要，重试间隔 0.5s 起）
            await reporter._send_with_retry(make_events(1))
            await reporter.stop()

        run_async(scenario())
        assert attempts["n"] == 3

    def test_retry_exhausted_drops_batch(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        config = ModerationConfig(
            service_url="http://svc.test", reporter_max_retries=2
        )

        async def scenario():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            reporter = HitReporter(config, client=client)
            await reporter._send_with_retry(make_events(2))
            dropped = reporter.dropped_count
            await reporter.stop()
            return dropped

        assert run_async(scenario()) == 2

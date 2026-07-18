"""server 测试：固定回复 / 阻断 / 策略不可用 / 流式 SSE 响应映射。"""
from __future__ import annotations

from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from agno_worker.api.server import AgnoAPIServer
from agno_worker.moderation.errors import SensitivePolicyUnavailableError
from agno_worker.moderation.guardrail import SensitiveContentDecisionError
from agno_worker.moderation.models import (
    ActionType,
    DecisionKind,
    GuardrailDecision,
    Match,
)


def make_block_decision() -> GuardrailDecision:
    return GuardrailDecision(
        kind=DecisionKind.REJECT,
        action=ActionType.BLOCK_REQUEST,
        rule_id=1,
        type_id=1,
        matches=[
            Match(rule_id=1, type_id=1, action=ActionType.BLOCK_REQUEST, spans=((0, 3),))
        ],
    )


def build_server(chat_async=None, stream_async=None) -> AgnoAPIServer:
    def chat_sync(message, session_id, user_id, tenant_id="", **kwargs):
        return ("sync", session_id)

    return AgnoAPIServer(
        bind="127.0.0.1",
        port=0,
        token="",
        chat_handler=chat_sync,
        chat_handler_async=chat_async,
        chat_stream_handler_async=stream_async,
        status_handler=lambda: {"status": "ok"},
    )


def client_for(server: AgnoAPIServer) -> TestClient:
    return TestClient(server._app, raise_server_exceptions=False)


class TestChatMapping:
    def test_fixed_reply_returns_200_with_message(self):
        async def handler(message, session_id, user_id, tenant_id="", **kwargs):
            # engine 已把 FIXED_REPLY 决策映射为普通回复（不调用 LLM）
            return ("请换个话题", session_id or "s1")

        client = client_for(build_server(chat_async=handler))
        resp = client.post("/effyic/v1/chat", json={"message": "有敏感词"})
        assert resp.status_code == 200
        assert resp.json()["reply"] == "请换个话题"

    def test_block_request_returns_422(self):
        async def handler(message, session_id, user_id, tenant_id="", **kwargs):
            raise SensitiveContentDecisionError(make_block_decision())

        client = client_for(build_server(chat_async=handler))
        resp = client.post("/effyic/v1/chat", json={"message": "有敏感词违禁内容"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "sensitive_content_blocked"
        # 响应不含敏感词与用户原文
        assert "敏感词" not in resp.text
        assert "违禁" not in resp.text

    def test_policy_unavailable_returns_503(self):
        async def handler(message, session_id, user_id, tenant_id="", **kwargs):
            raise SensitivePolicyUnavailableError()

        client = client_for(build_server(chat_async=handler))
        resp = client.post("/effyic/v1/chat", json={"message": "任意"})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "sensitive_policy_unavailable"


class TestStreamMapping:
    def _parse_sse(self, text: str) -> list[tuple[str, str]]:
        events = []
        current_event = ""
        for line in text.splitlines():
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((current_event, line.removeprefix("data: ")))
        return events

    def test_fixed_reply_stream_single_event_then_completed(self):
        async def stream(message, session_id, user_id, tenant_id="", **kwargs) -> AsyncIterator[dict[str, Any]]:
            # engine 已把决策映射为单个 RunContent + RunCompleted
            yield {"event": "RunContent", "content": "请换个话题", "session_id": "s1"}
            yield {"event": "RunCompleted", "session_id": "s1"}

        client = client_for(build_server(stream_async=stream))
        resp = client.post("/effyic/v1/chat/stream", json={"message": "有敏感词"})
        assert resp.status_code == 200
        events = self._parse_sse(resp.text)
        assert [name for name, _ in events] == ["RunContent", "RunCompleted"]
        assert "请换个话题" in events[0][1]

    def test_block_stream_run_error(self):
        async def stream(message, session_id, user_id, tenant_id="", **kwargs) -> AsyncIterator[dict[str, Any]]:
            yield {
                "event": "RunError",
                "content": "sensitive_content_blocked",
                "session_id": "s1",
            }

        client = client_for(build_server(stream_async=stream))
        resp = client.post("/effyic/v1/chat/stream", json={"message": "有敏感词"})
        events = self._parse_sse(resp.text)
        assert [name for name, _ in events] == ["RunError"]
        assert "sensitive_content_blocked" in events[0][1]
        assert "敏感词" not in resp.text

    def test_stream_handler_exception_maps_run_error(self):
        async def stream(message, session_id, user_id, tenant_id="", **kwargs) -> AsyncIterator[dict[str, Any]]:
            raise SensitivePolicyUnavailableError()
            yield  # pragma: no cover

        client = client_for(build_server(stream_async=stream))
        resp = client.post("/effyic/v1/chat/stream", json={"message": "x"})
        events = self._parse_sse(resp.text)
        assert [name for name, _ in events] == ["RunError"]
        assert "sensitive_policy_unavailable" in events[0][1]


class TestBackwardCompatibility:
    def test_normal_chat_unaffected(self):
        async def handler(message, session_id, user_id, tenant_id="", **kwargs):
            return (f"echo:{message}", session_id or "s1")

        client = client_for(build_server(chat_async=handler))
        resp = client.post("/effyic/v1/chat", json={"message": "你好"})
        assert resp.status_code == 200
        assert resp.json()["reply"] == "echo:你好"

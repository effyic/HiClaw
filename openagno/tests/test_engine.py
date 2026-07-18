"""engine 测试：contextvars 注入、request_id 语义与敏感内容决策映射（同步/流式）。

用 FakeAgent 模拟 agno Agent 的关键行为：pre_hooks 首位执行 Guardrail、
捕获 InputCheckError 后返回错误 RunOutput（同步）/ 发 run_error 事件（流式）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from agno.exceptions import InputCheckError
from agno.run.agent import RunEvent, RunInput

from agno_worker.agentspec.schema import AgentSpec
from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.errors import SensitivePolicyUnavailableError
from agno_worker.moderation.guardrail import (
    SensitiveContentDecisionError,
    SensitiveContentGuardrail,
)
from agno_worker.moderation.models import ActionType
from agno_worker.moderation.snapshot import SnapshotClient
from agno_worker.runtime.engine import AgnoRuntime

from conftest import CaptureReporter, make_rule, make_snapshot, make_type


TENANT = "tenant-a"


class FakeAgent:
    """模拟 agno Agent：执行 Guardrail 并按 agno 语义吞掉 InputCheckError。"""

    name = "fake-agent"

    def __init__(self, guardrail: SensitiveContentGuardrail) -> None:
        self.guardrail = guardrail
        self.llm_called = 0

    def arun(self, message: str, **kwargs: Any):
        if kwargs.get("stream"):
            return self._astream(message, **kwargs)
        return self._arun(message, **kwargs)

    async def _arun(self, message: str, **kwargs: Any):
        run_input = RunInput(input_content=message)
        try:
            await self.guardrail.async_check(run_input)
        except InputCheckError as exc:
            # agno 捕获 InputCheckError：返回错误 RunOutput，不向调用方重抛
            return SimpleNamespace(
                content=str(exc), session_id=kwargs.get("session_id", "")
            )
        self.llm_called += 1
        return SimpleNamespace(
            content=f"LLM:{run_input.input_content}",
            session_id=kwargs.get("session_id", ""),
        )

    async def _astream(self, message: str, **kwargs: Any):
        run_input = RunInput(input_content=message)
        try:
            await self.guardrail.async_check(run_input)
        except InputCheckError as exc:
            # agno 流式路径：发 run_error 事件后结束
            yield SimpleNamespace(
                event=RunEvent.run_error.value,
                content=str(exc),
                session_id=kwargs.get("session_id", ""),
            )
            return
        self.llm_called += 1
        yield SimpleNamespace(
            event=RunEvent.run_content.value,
            content=f"LLM:{run_input.input_content}",
            session_id=kwargs.get("session_id", ""),
        )
        yield SimpleNamespace(
            event=RunEvent.run_completed.value,
            session_id=kwargs.get("session_id", ""),
        )


def build_runtime(tmp_path, action: ActionType, action_config=None):
    cfg = ModerationConfig(
        service_url="http://sensitive-content.test",
        fingerprint_key="fp-key",
        cache_path=str(tmp_path / "cache.json"),
    )
    client = SnapshotClient(cfg)
    client._install_snapshot(
        make_snapshot(
            [make_type(1, action, action_config=action_config)],
            [make_rule(1, 1, "敏感词")],
            tenant_id=TENANT,
        ),
        persist=False,
    )
    reporter = CaptureReporter()
    guardrail = SensitiveContentGuardrail(
        cfg, snapshot_client=client, reporter=reporter
    )
    runtime = AgnoRuntime(AgentSpec(name="t"), db_url="sqlite:///:memory:")
    agent = FakeAgent(guardrail)
    runtime._primary_agent = agent
    return runtime, agent, reporter


def arun(runtime, message, session_id="session-1"):
    return asyncio.run(
        runtime.arun(
            message,
            session_id=session_id,
            user_id="user-1",
            tenant_id=TENANT,
        )
    )


def astream(runtime, message, session_id="session-1"):
    async def collect():
        events = []
        async for event in runtime.astream(
            message,
            session_id=session_id,
            user_id="user-1",
            tenant_id=TENANT,
        ):
            events.append(event)
        return events

    return asyncio.run(collect())


class TestArunMapping:
    def test_clean_input_passes_to_llm(self, tmp_path):
        runtime, agent, _ = build_runtime(tmp_path, ActionType.BLOCK_REQUEST)
        reply, session_id = arun(runtime, "干净输入")
        assert reply == "LLM:干净输入"
        assert agent.llm_called == 1

    def test_fixed_reply_returns_message_without_llm(self, tmp_path):
        runtime, agent, _ = build_runtime(
            tmp_path, ActionType.FIXED_REPLY, {"reply_text": "请换个话题"}
        )
        reply, _ = arun(runtime, "有敏感词")
        assert reply == "请换个话题"
        assert agent.llm_called == 0  # 不调用 LLM

    def test_end_conversation_returns_message_without_llm(self, tmp_path):
        runtime, agent, _ = build_runtime(
            tmp_path, ActionType.END_CONVERSATION, {"reply_text": "会话已结束"}
        )
        reply, _ = arun(runtime, "有敏感词")
        assert reply == "会话已结束"
        assert agent.llm_called == 0

    def test_block_request_raises_decision_error(self, tmp_path):
        runtime, agent, _ = build_runtime(tmp_path, ActionType.BLOCK_REQUEST)
        with pytest.raises(SensitiveContentDecisionError):
            arun(runtime, "有敏感词")
        assert agent.llm_called == 0

    def test_redact_continues_with_redacted_text(self, tmp_path):
        runtime, agent, _ = build_runtime(tmp_path, ActionType.REDACT_AND_CONTINUE)
        reply, _ = arun(runtime, "前缀敏感词后缀")
        # LLM 看到的是脱敏后文本
        assert reply == "LLM:前缀***后缀"
        assert agent.llm_called == 1

    def test_policy_unavailable_fail_closed(self, tmp_path):
        cfg = ModerationConfig(
            service_url="http://sensitive-content.test",
            fail_mode="closed",
            cache_path=str(tmp_path / "cache.json"),
        )
        guardrail = SensitiveContentGuardrail(
            cfg, snapshot_client=SnapshotClient(cfg), reporter=CaptureReporter()
        )
        runtime = AgnoRuntime(AgentSpec(name="t"), db_url="sqlite:///:memory:")
        runtime._primary_agent = FakeAgent(guardrail)
        with pytest.raises(SensitivePolicyUnavailableError):
            arun(runtime, "任意输入")

    def test_request_id_differs_per_request(self, tmp_path):
        """同一 session 两次请求产生不同 request_fingerprint。"""
        runtime, _, reporter = build_runtime(tmp_path, ActionType.LOG_ONLY)
        arun(runtime, "有敏感词", session_id="same-session")
        arun(runtime, "有敏感词", session_id="same-session")
        assert len(reporter.events) == 2
        first, second = reporter.events
        assert first.request_fingerprint != second.request_fingerprint
        assert first.session_fingerprint == second.session_fingerprint


class TestAstreamMapping:
    def test_fixed_reply_stream_single_content_then_completed(self, tmp_path):
        runtime, agent, _ = build_runtime(
            tmp_path, ActionType.FIXED_REPLY, {"reply_text": "请换个话题"}
        )
        events = astream(runtime, "有敏感词")
        assert [e["event"] for e in events] == ["RunContent", "RunCompleted"]
        assert events[0]["content"] == "请换个话题"
        assert agent.llm_called == 0

    def test_end_conversation_stream(self, tmp_path):
        runtime, _, _ = build_runtime(
            tmp_path, ActionType.END_CONVERSATION, {"reply_text": "会话已结束"}
        )
        events = astream(runtime, "有敏感词")
        assert [e["event"] for e in events] == ["RunContent", "RunCompleted"]
        assert events[0]["content"] == "会话已结束"

    def test_block_request_stream_run_error(self, tmp_path):
        runtime, agent, _ = build_runtime(tmp_path, ActionType.BLOCK_REQUEST)
        events = astream(runtime, "有敏感词")
        assert len(events) == 1
        assert events[0]["event"] == "RunError"
        assert events[0]["content"] == "sensitive_content_blocked"
        # 响应不含敏感词与原文
        assert "敏感词" not in str(events)
        assert agent.llm_called == 0

    def test_clean_input_stream_normal(self, tmp_path):
        runtime, _, _ = build_runtime(tmp_path, ActionType.BLOCK_REQUEST)
        events = astream(runtime, "干净输入")
        assert [e["event"] for e in events] == ["RunContent", "RunCompleted"]
        assert events[0]["content"] == "LLM:干净输入"

    def test_policy_unavailable_stream(self, tmp_path):
        cfg = ModerationConfig(
            service_url="http://sensitive-content.test",
            fail_mode="closed",
            cache_path=str(tmp_path / "cache.json"),
        )
        guardrail = SensitiveContentGuardrail(
            cfg, snapshot_client=SnapshotClient(cfg), reporter=CaptureReporter()
        )
        runtime = AgnoRuntime(AgentSpec(name="t"), db_url="sqlite:///:memory:")
        runtime._primary_agent = FakeAgent(guardrail)
        events = astream(runtime, "任意输入")
        assert len(events) == 1
        assert events[0]["event"] == "RunError"
        assert events[0]["content"] == "sensitive_policy_unavailable"

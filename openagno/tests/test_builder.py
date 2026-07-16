"""builder 测试：Guardrail 挂载位置与未配置时的向后兼容。"""
from __future__ import annotations

from types import SimpleNamespace

from agno_worker.agentspec.schema import AgentSpec
from agno_worker.hooks.registry import HookRegistry
from agno_worker.moderation.guardrail import (
    SensitiveContentGuardrail,
    reset_guardrail_singleton,
)
from agno_worker.runtime.builder import AgentBuilder


def build_and_capture(monkeypatch) -> dict:
    """monkeypatch StorageAwareAgent.create，捕获 Agent 构造参数。"""
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(name=kwargs.get("name", "agent"))

    monkeypatch.setattr(
        "agno_worker.runtime.builder.StorageAwareAgent.create",
        staticmethod(fake_create),
    )
    builder = AgentBuilder(HookRegistry(None), AgentSpec(name="t"), db=None)
    builder.build_dynamic_agent()
    return captured


class TestGuardrailMount:
    def test_without_env_behavior_unchanged(self, monkeypatch):
        """未配置 SENSITIVE_CONTENT_SERVICE_URL：pre_hooks 与原来一致。"""
        reset_guardrail_singleton()
        monkeypatch.delenv("SENSITIVE_CONTENT_SERVICE_URL", raising=False)
        captured = build_and_capture(monkeypatch)
        pre_hooks = captured["pre_hooks"]
        assert len(pre_hooks) == 1
        assert callable(pre_hooks[0])
        assert not isinstance(pre_hooks[0], SensitiveContentGuardrail)

    def test_with_env_guardrail_first(self, monkeypatch, tmp_path):
        """配置服务地址后：Guardrail 插入 pre_hooks 首位（业务 pre_hook 之前）。"""
        reset_guardrail_singleton()
        monkeypatch.setenv("SENSITIVE_CONTENT_SERVICE_URL", "http://svc.test")
        monkeypatch.setenv(
            "SENSITIVE_CONTENT_CACHE_PATH", str(tmp_path / "cache.json")
        )
        try:
            captured = build_and_capture(monkeypatch)
            pre_hooks = captured["pre_hooks"]
            assert len(pre_hooks) == 2
            assert isinstance(pre_hooks[0], SensitiveContentGuardrail)
            assert callable(pre_hooks[1])
        finally:
            reset_guardrail_singleton()

"""builder 测试：Guardrail 挂载、ADJUST_PROMPT 注入与未配置时的向后兼容。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from agno.run.agent import RunInput

from agno_worker.agentspec.schema import AgentDef, AgentSpec
from agno_worker.hooks.registry import HookRegistry
from agno_worker.moderation.context import (
    new_request_id,
    reset_request_context,
    set_request_context,
)
from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.guardrail import (
    SensitiveContentGuardrail,
    reset_guardrail_singleton,
)
from agno_worker.moderation.models import ActionType
from agno_worker.moderation.snapshot import SnapshotClient
from agno_worker.runtime.builder import AgentBuilder, _PROMPT_GUIDANCE_HEADER
from agno_worker.tenant.cache import RUN_CACHE_KEY

from conftest import CaptureReporter, config, make_rule, make_snapshot, make_type


def build_and_capture(monkeypatch, spec: AgentSpec | None = None) -> dict:
    """monkeypatch StorageAwareAgent.create，捕获 Agent 构造参数。"""
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(name=kwargs.get("name", "agent"))

    monkeypatch.setattr(
        "agno_worker.runtime.builder.StorageAwareAgent.create",
        staticmethod(fake_create),
    )
    builder = AgentBuilder(
        HookRegistry(None), spec or AgentSpec(name="t"), db=None
    )
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


class TestAdjustPromptInjection:
    """Agno 真实路径：Guardrail pre_hook → instructions callable 注入指引块。"""

    @pytest.fixture(autouse=True)
    def request_ctx(self):
        ctx, token = set_request_context(
            tenant_id="tenant-a",
            user_id="user-1",
            session_id="session-1",
            request_id=new_request_id(),
        )
        yield ctx
        reset_request_context(token)

    def test_guardrail_then_instructions_inject_guidance(
        self, monkeypatch, config, tmp_path, request_ctx
    ):
        guidance = "请以温暖、关心的语气回应用户。"
        cfg = ModerationConfig(
            service_url=config.service_url,
            fingerprint_key=config.fingerprint_key,
            fail_mode=config.fail_mode,
            cache_path=str(tmp_path / "policy-snapshot.json"),
        )
        client = SnapshotClient(cfg)
        client._install_snapshot(
            make_snapshot(
                [
                    make_type(
                        1,
                        ActionType.ADJUST_PROMPT,
                        action_config={"prompt_guidance": guidance},
                    )
                ],
                [make_rule(1, 1, "轻生")],
                tenant_id="tenant-a",
            ),
            persist=False,
        )
        guardrail = SensitiveContentGuardrail(
            cfg, snapshot_client=client, reporter=CaptureReporter()
        )

        # 模拟 Agno：先跑 Guardrail pre_hook
        guardrail.check(RunInput(input_content="提到轻生"))
        assert request_ctx.prompt_guidances == [guidance]

        # 再取 builder 的 instructions callable（跳过租户 DB，走已 prepared 缓存）
        reset_guardrail_singleton()
        monkeypatch.delenv("SENSITIVE_CONTENT_SERVICE_URL", raising=False)
        spec = AgentSpec(
            name="tone-agent",
            agents={
                "default": AgentDef(
                    name="default",
                    role="你是助手",
                    instructions="提供帮助。",
                )
            },
        )
        captured = build_and_capture(monkeypatch, spec=spec)
        instructions_fn = captured["instructions"]
        assert callable(instructions_fn)

        run_context = SimpleNamespace(
            session_state={"active_role": "default"},
            dependencies={
                RUN_CACHE_KEY: {
                    "_prepared": True,
                    "prompt_bundle": {
                        "system_prompt": "",
                        "instructions": "",
                    },
                    "skill_catalog": [],
                }
            },
            metadata={},
            user_id="user-1",
        )
        text = instructions_fn(run_context=run_context)
        assert "如有冲突，以更靠前的要求为准" in text
        assert _PROMPT_GUIDANCE_HEADER.split("\n")[0] in text
        assert guidance in text
        assert "提供帮助。" in text

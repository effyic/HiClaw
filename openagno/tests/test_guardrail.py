"""guardrail 测试：八种行为、同步/异步 check、脱敏改写、命中事件字段与明文泄漏断言。"""
from __future__ import annotations

import asyncio
import json

import pytest
from agno.exceptions import InputCheckError
from agno.run.agent import RunInput

from agno_worker.moderation import actions
from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.context import (
    get_request_context,
    new_request_id,
    reset_request_context,
    set_request_context,
)
from agno_worker.moderation.guardrail import (
    BLOCKED_INPUT_PLACEHOLDER,
    SensitiveContentDecisionError,
    SensitiveContentGuardrail,
    maybe_build_guardrail,
    reset_guardrail_singleton,
)
from agno_worker.moderation.models import ActionType, DecisionKind
from agno_worker.moderation.snapshot import SnapshotClient

from conftest import CaptureReporter, config, closed_config, make_rule, make_snapshot, make_type  # noqa: F401


TENANT = "tenant-a"


def build_guardrail(
    cfg: ModerationConfig,
    types,
    rules,
    *,
    tmp_path,
    tenant_id: str = TENANT,
    snapshot=None,
) -> tuple[SensitiveContentGuardrail, CaptureReporter]:
    cfg = ModerationConfig(
        service_url=cfg.service_url,
        fingerprint_key=cfg.fingerprint_key,
        fail_mode=cfg.fail_mode,
        cache_path=str(tmp_path / "policy-snapshot.json"),
    )
    client = SnapshotClient(cfg)
    if snapshot is None:
        snapshot = make_snapshot(types, rules, tenant_id=tenant_id)
    client._install_snapshot(snapshot, persist=False)
    reporter = CaptureReporter()
    guardrail = SensitiveContentGuardrail(
        cfg, snapshot_client=client, reporter=reporter
    )
    return guardrail, reporter


@pytest.fixture(autouse=True)
def request_ctx():
    """每个测试注入独立请求上下文。"""
    ctx, token = set_request_context(
        tenant_id=TENANT,
        user_id="user-1",
        session_id="session-1",
        request_id=new_request_id(),
    )
    yield ctx
    reset_request_context(token)


class TestEightActions:
    def test_log_only_passes_through(self, config, tmp_path):
        guardrail, reporter = build_guardrail(
            config,
            [make_type(1, ActionType.LOG_ONLY)],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        run_input = RunInput(input_content="包含敏感词的输入")
        guardrail.check(run_input)  # 不抛异常
        assert run_input.input_content == "包含敏感词的输入"
        assert len(reporter.events) == 1  # 上报后放行

    def test_redact_and_continue_rewrites_input(self, config, tmp_path):
        guardrail, reporter = build_guardrail(
            config,
            [make_type(1, ActionType.REDACT_AND_CONTINUE)],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        run_input = RunInput(input_content="前缀敏感词后缀")
        guardrail.check(run_input)
        # 业务 pre_hook（在 Guardrail 之后）看到的是脱敏文本
        assert run_input.input_content == "前缀***后缀"
        assert reporter.events

    def test_fixed_reply_raises_decision(self, config, tmp_path):
        guardrail, _ = build_guardrail(
            config,
            [make_type(1, ActionType.FIXED_REPLY, action_config={"reply": "换个话题"})],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        run_input = RunInput(input_content="有敏感词")
        with pytest.raises(SensitiveContentDecisionError) as exc_info:
            guardrail.check(run_input)
        assert exc_info.value.decision.kind == DecisionKind.RESPOND
        assert exc_info.value.decision.message == "换个话题"
        # 原始输入被替换为占位文本，防止落库
        assert run_input.input_content == BLOCKED_INPUT_PLACEHOLDER

    def test_custom_response_raises_decision(self, config, tmp_path):
        guardrail, _ = build_guardrail(
            config,
            [make_type(1, ActionType.CUSTOM_RESPONSE, action_config={"message": "自定义"})],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        with pytest.raises(SensitiveContentDecisionError) as exc_info:
            guardrail.check(RunInput(input_content="有敏感词"))
        assert exc_info.value.decision.action == ActionType.CUSTOM_RESPONSE
        assert exc_info.value.decision.message == "自定义"

    def test_end_conversation_raises_decision(self, config, tmp_path):
        guardrail, _ = build_guardrail(
            config,
            [make_type(1, ActionType.END_CONVERSATION)],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        with pytest.raises(SensitiveContentDecisionError) as exc_info:
            guardrail.check(RunInput(input_content="有敏感词"))
        assert exc_info.value.decision.kind == DecisionKind.TERMINATE

    def test_block_request_raises_decision(self, config, tmp_path):
        guardrail, _ = build_guardrail(
            config,
            [make_type(1, ActionType.BLOCK_REQUEST)],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        run_input = RunInput(input_content="有敏感词")
        with pytest.raises(SensitiveContentDecisionError) as exc_info:
            guardrail.check(run_input)
        assert exc_info.value.decision.kind == DecisionKind.REJECT
        assert run_input.input_content == BLOCKED_INPUT_PLACEHOLDER
        # 异常信息不含敏感词与原文
        assert "敏感词" not in str(exc_info.value)

    def test_business_action_with_handler_continues(self, config, tmp_path):
        called = []
        actions.register_business_action("audit", lambda *a: called.append(a))
        try:
            guardrail, _ = build_guardrail(
                config,
                [
                    make_type(
                        1,
                        ActionType.BUSINESS_ACTION,
                        action_config={"handler": "audit"},
                    )
                ],
                [make_rule(1, 1, "敏感词")],
                tmp_path=tmp_path,
            )
            run_input = RunInput(input_content="有敏感词")
            guardrail.check(run_input)  # 放行
            assert called
        finally:
            actions.unregister_business_action("audit")

    def test_business_action_missing_handler_fail_open(self, config, tmp_path, caplog):
        guardrail, _ = build_guardrail(
            config,
            [
                make_type(
                    1, ActionType.BUSINESS_ACTION, action_config={"handler": "nope"}
                )
            ],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        with caplog.at_level("WARNING"):
            guardrail.check(RunInput(input_content="有敏感词"))  # 降级放行但有告警
        assert any("未注册" in r.message for r in caplog.records)

    def test_business_action_missing_handler_fail_closed(self, closed_config, tmp_path):
        guardrail, _ = build_guardrail(
            closed_config,
            [
                make_type(
                    1, ActionType.BUSINESS_ACTION, action_config={"handler": "nope"}
                )
            ],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        with pytest.raises(SensitiveContentDecisionError):
            guardrail.check(RunInput(input_content="有敏感词"))

    def test_adjust_prompt_passes_and_writes_guidances(self, config, tmp_path, request_ctx):
        guardrail, _ = build_guardrail(
            config,
            [
                make_type(
                    1,
                    ActionType.ADJUST_PROMPT,
                    action_config={"prompt_guidance": "以关怀语气回应"},
                )
            ],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        run_input = RunInput(input_content="有敏感词")
        guardrail.check(run_input)
        assert run_input.input_content == "有敏感词"
        assert request_ctx.prompt_guidances == ["以关怀语气回应"]


class TestPromptGuidancesContext:
    """每轮开头清空；放行写入；阻断保持空；复用上下文无污染。"""

    def test_no_hit_leaves_guidances_empty(self, config, tmp_path, request_ctx):
        request_ctx.prompt_guidances = ["上一轮残留"]
        guardrail, _ = build_guardrail(
            config,
            [
                make_type(
                    1,
                    ActionType.ADJUST_PROMPT,
                    action_config={"prompt_guidance": "不应写入"},
                )
            ],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        guardrail.check(RunInput(input_content="干净的输入"))
        assert request_ctx.prompt_guidances == []

    def test_allow_writes_guidances(self, config, tmp_path, request_ctx):
        guardrail, _ = build_guardrail(
            config,
            [
                make_type(
                    1,
                    ActionType.ADJUST_PROMPT,
                    priority=70,
                    action_config={"prompt_guidance": "关怀指引"},
                )
            ],
            [make_rule(1, 1, "轻生")],
            tmp_path=tmp_path,
        )
        guardrail.check(RunInput(input_content="提到轻生"))
        assert request_ctx.prompt_guidances == ["关怀指引"]

    def test_block_leaves_guidances_empty(self, config, tmp_path, request_ctx):
        # violence(80) + self_harm(70)：BLOCK 胜出，不注入
        guardrail, _ = build_guardrail(
            config,
            [
                make_type(
                    1,
                    ActionType.ADJUST_PROMPT,
                    priority=70,
                    action_config={"prompt_guidance": "关怀指引"},
                ),
                make_type(2, ActionType.BLOCK_REQUEST, priority=80),
            ],
            [make_rule(1, 1, "轻生"), make_rule(2, 2, "暴力")],
            tmp_path=tmp_path,
        )
        with pytest.raises(SensitiveContentDecisionError):
            guardrail.check(RunInput(input_content="暴力与轻生"))
        assert request_ctx.prompt_guidances == []

    def test_reused_context_no_pollution(self, config, tmp_path, request_ctx):
        guardrail, _ = build_guardrail(
            config,
            [
                make_type(
                    1,
                    ActionType.ADJUST_PROMPT,
                    action_config={"prompt_guidance": "关怀指引"},
                )
            ],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        guardrail.check(RunInput(input_content="有敏感词"))
        assert request_ctx.prompt_guidances == ["关怀指引"]
        # 同一上下文再次检测未命中：开头清空，不应残留
        guardrail.check(RunInput(input_content="干净的输入"))
        assert request_ctx.prompt_guidances == []

    def test_redact_wins_still_writes_guidances(self, config, tmp_path, request_ctx):
        # privacy 胜出且同时命中 ADJUST：脱敏全部区间，仍写入叠加指引
        guardrail, _ = build_guardrail(
            config,
            [
                make_type(
                    1,
                    ActionType.REDACT_AND_CONTINUE,
                    priority=60,
                    action_config={"replacement": "*"},
                ),
                make_type(
                    2,
                    ActionType.ADJUST_PROMPT,
                    priority=50,
                    action_config={"prompt_guidance": "关怀指引"},
                ),
            ],
            [make_rule(1, 1, "手机"), make_rule(2, 2, "轻生")],
            tmp_path=tmp_path,
        )
        run_input = RunInput(input_content="手机与轻生")
        guardrail.check(run_input)
        assert "***" in run_input.input_content or "**" in run_input.input_content
        assert "手机" not in run_input.input_content
        assert "轻生" not in run_input.input_content
        assert request_ctx.prompt_guidances == ["关怀指引"]


class TestSyncAsyncCheck:
    def test_async_check(self, config, tmp_path):
        guardrail, reporter = build_guardrail(
            config,
            [make_type(1, ActionType.REDACT_AND_CONTINUE)],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        run_input = RunInput(input_content="有敏感词啊")

        asyncio.run(guardrail.async_check(run_input))
        assert run_input.input_content == "有***啊"
        assert reporter.started  # async 路径启动后台任务

    def test_no_match_no_events(self, config, tmp_path):
        guardrail, reporter = build_guardrail(
            config,
            [make_type(1, ActionType.BLOCK_REQUEST)],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        run_input = RunInput(input_content="干净的输入")
        guardrail.check(run_input)
        assert reporter.events == []

    def test_non_text_input_passes(self, config, tmp_path):
        guardrail, _ = build_guardrail(
            config,
            [make_type(1, ActionType.BLOCK_REQUEST)],
            [make_rule(1, 1, "敏感词")],
            tmp_path=tmp_path,
        )
        run_input = RunInput(input_content={"structured": "有敏感词"})
        guardrail.check(run_input)  # 非纯文本输入本期直接放行


class TestNoSnapshotFailModes:
    def test_fail_open_skips_detection(self, config, tmp_path, caplog):
        cfg = ModerationConfig(
            service_url=config.service_url,
            fail_mode="open",
            cache_path=str(tmp_path / "cache.json"),
        )
        guardrail = SensitiveContentGuardrail(
            cfg, snapshot_client=SnapshotClient(cfg), reporter=CaptureReporter()
        )
        run_input = RunInput(input_content="有敏感词")
        with caplog.at_level("WARNING"):
            guardrail.check(run_input)  # 无快照：跳过检测放行
        assert run_input.input_content == "有敏感词"
        assert any("无有效敏感内容策略快照" in r.message for r in caplog.records)

    def test_fail_closed_raises(self, tmp_path, request_ctx):
        cfg = ModerationConfig(
            service_url="http://sensitive-content.test",
            fail_mode="closed",
            cache_path=str(tmp_path / "cache.json"),
        )
        guardrail = SensitiveContentGuardrail(
            cfg, snapshot_client=SnapshotClient(cfg), reporter=CaptureReporter()
        )
        run_input = RunInput(input_content="有敏感词")
        with pytest.raises(InputCheckError):
            guardrail.check(run_input)
        assert request_ctx.policy_unavailable is True
        assert run_input.input_content == BLOCKED_INPUT_PLACEHOLDER

    def test_stale_snapshot_treated_as_missing(self, config, tmp_path, caplog):
        stale = make_snapshot(
            [make_type(1, ActionType.BLOCK_REQUEST)],
            [make_rule(1, 1, "敏感词")],
            tenant_id=TENANT,
            fetched_at=0.0,  # 远古快照，超过 max_stale
        )
        guardrail, _ = build_guardrail(
            config,
            [],
            [],
            tmp_path=tmp_path,
            snapshot=stale,
        )
        run_input = RunInput(input_content="有敏感词")
        with caplog.at_level("WARNING"):
            guardrail.check(run_input)  # fail-open：过期视为无快照，放行
        assert run_input.input_content == "有敏感词"
        assert any("过期上限" in r.message for r in caplog.records)


class TestHitEvents:
    def _multi_hit_guardrail(self, config, tmp_path):
        # 两条规则：类型 2 优先级更高（BLOCK），类型 1 为 LOG_ONLY
        return build_guardrail(
            config,
            [
                make_type(1, ActionType.LOG_ONLY, priority=1),
                make_type(2, ActionType.BLOCK_REQUEST, priority=9),
            ],
            [make_rule(1, 1, "低危词"), make_rule(2, 2, "高危词")],
            tmp_path=tmp_path,
        )

    def test_multi_hit_event_fields(self, config, tmp_path):
        guardrail, reporter = self._multi_hit_guardrail(config, tmp_path)
        with pytest.raises(SensitiveContentDecisionError):
            guardrail.check(RunInput(input_content="低危词和高危词都有"))
        events = reporter.events
        assert len(events) == 2
        # rule_action 各异，final_action / final_rule_id 一致
        assert {e.rule_action for e in events} == {"LOG_ONLY", "BLOCK_REQUEST"}
        assert {e.final_action for e in events} == {"BLOCK_REQUEST"}
        assert {e.final_rule_id for e in events} == {2}
        # 仅排序第一条 selected=TRUE
        selected = [e for e in events if e.selected]
        assert len(selected) == 1
        assert selected[0].rule_id == 2

    def test_events_contain_no_plaintext(self, config, tmp_path):
        guardrail, reporter = self._multi_hit_guardrail(config, tmp_path)
        original = "低危词和高危词都有"
        with pytest.raises(SensitiveContentDecisionError):
            guardrail.check(RunInput(input_content=original))
        serialized = json.dumps(
            [e.to_payload() for e in reporter.events], ensure_ascii=False
        )
        # 不出现用户原文与规则明文
        assert original not in serialized
        assert "低危词" not in serialized
        assert "高危词" not in serialized

    def test_fingerprints_hmac_derived(self, config, tmp_path):
        guardrail, reporter = self._multi_hit_guardrail(config, tmp_path)
        with pytest.raises(SensitiveContentDecisionError):
            guardrail.check(RunInput(input_content="高危词"))
        event = reporter.events[0]
        ctx = get_request_context()
        # 指纹为 HMAC 十六进制串，且不等于原始标识
        assert len(event.request_fingerprint) == 64
        assert event.request_fingerprint != ctx.request_id
        assert event.session_fingerprint != ctx.session_id

    def test_events_carry_plain_session_id(self, config, tmp_path):
        """明文 session_id 取自请求上下文并随事件上报（后台会话跳转用）。"""
        guardrail, reporter = self._multi_hit_guardrail(config, tmp_path)
        with pytest.raises(SensitiveContentDecisionError):
            guardrail.check(RunInput(input_content="低危词和高危词都有"))
        ctx = get_request_context()
        assert ctx.session_id == "session-1"
        assert {e.session_id for e in reporter.events} == {"session-1"}

    def test_two_requests_same_session_differ_in_request_fingerprint(
        self, config, tmp_path
    ):
        """request_id 语义：同一 session 两次请求产生不同 request_fingerprint。"""
        guardrail, reporter = self._multi_hit_guardrail(config, tmp_path)
        fingerprints = []
        for _ in range(2):
            # 模拟 engine 行为：每个请求独立 request_id、同一 session_id
            ctx, token = set_request_context(
                tenant_id=TENANT,
                user_id="user-1",
                session_id="session-1",
                request_id=new_request_id(),
            )
            try:
                with pytest.raises(SensitiveContentDecisionError):
                    guardrail.check(RunInput(input_content="高危词"))
            finally:
                reset_request_context(token)
            fingerprints.append(reporter.events[-1])
        assert (
            fingerprints[0].request_fingerprint
            != fingerprints[1].request_fingerprint
        )
        assert (
            fingerprints[0].session_fingerprint
            == fingerprints[1].session_fingerprint
        )


class TestSingleton:
    def test_disabled_without_service_url(self, monkeypatch):
        reset_guardrail_singleton()
        monkeypatch.delenv("SENSITIVE_CONTENT_SERVICE_URL", raising=False)
        assert maybe_build_guardrail() is None

    def test_enabled_with_service_url(self, monkeypatch, tmp_path):
        reset_guardrail_singleton()
        monkeypatch.setenv("SENSITIVE_CONTENT_SERVICE_URL", "http://svc.test")
        monkeypatch.setenv(
            "SENSITIVE_CONTENT_CACHE_PATH", str(tmp_path / "cache.json")
        )
        guardrail = maybe_build_guardrail()
        assert isinstance(guardrail, SensitiveContentGuardrail)
        # 单例复用
        assert maybe_build_guardrail() is guardrail
        reset_guardrail_singleton()

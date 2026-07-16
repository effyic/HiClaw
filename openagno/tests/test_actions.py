"""actions 测试：七种行为映射与 BUSINESS_ACTION 白名单 fail 模式。"""
from __future__ import annotations

from agno_worker.moderation import actions
from agno_worker.moderation.models import ActionType, DecisionKind, Match

from conftest import config, closed_config  # noqa: F401  (fixtures)


def make_match(action: ActionType, rule_id: int = 1, type_id: int = 1) -> Match:
    return Match(
        rule_id=rule_id,
        type_id=type_id,
        action=action,
        spans=((0, 3),),
    )


def decide_one(action: ActionType, cfg, action_config=None, text="bad text"):
    match = make_match(action)
    return actions.decide(
        [match], text, {1: dict(action_config or {})}, cfg
    )


class TestActionMapping:
    def test_log_only(self, config):
        decision = decide_one(ActionType.LOG_ONLY, config)
        assert decision.kind == DecisionKind.CONTINUE
        assert decision.action == ActionType.LOG_ONLY

    def test_block_request(self, config):
        decision = decide_one(ActionType.BLOCK_REQUEST, config)
        assert decision.kind == DecisionKind.REJECT

    def test_fixed_reply_uses_config_text(self, config):
        decision = decide_one(
            ActionType.FIXED_REPLY, config, {"reply": "请换个话题"}
        )
        assert decision.kind == DecisionKind.RESPOND
        assert decision.message == "请换个话题"

    def test_custom_response(self, config):
        decision = decide_one(
            ActionType.CUSTOM_RESPONSE, config, {"message": "自定义回复"}
        )
        assert decision.kind == DecisionKind.RESPOND
        assert decision.message == "自定义回复"

    def test_end_conversation(self, config):
        decision = decide_one(
            ActionType.END_CONVERSATION, config, {"reply": "会话结束"}
        )
        assert decision.kind == DecisionKind.TERMINATE
        assert decision.message == "会话结束"

    def test_redact_and_continue_merges_spans(self, config):
        m1 = Match(rule_id=1, type_id=1, action=ActionType.REDACT_AND_CONTINUE, spans=((0, 3),))
        m2 = Match(rule_id=2, type_id=1, action=ActionType.LOG_ONLY, spans=((2, 6),))
        decision = actions.decide([m1, m2], "abcdef xyz", {1: {}}, config)
        assert decision.kind == DecisionKind.REDACT
        # 重叠区间 (0,3)+(2,6) 合并为 (0,6) 统一替换
        assert decision.redacted_text == "****** xyz"

    def test_redact_custom_replacement(self, config):
        decision = decide_one(
            ActionType.REDACT_AND_CONTINUE, config, {"replacement": "#"}
        )
        assert decision.redacted_text.startswith("###")


class TestBusinessAction:
    def teardown_method(self):
        actions.unregister_business_action("notify")

    def test_registered_handler_invoked(self, config):
        called = {}

        def handler(match, matches, text, action_config):
            called["match"] = match
            called["config"] = action_config

        actions.register_business_action("notify", handler)
        decision = decide_one(
            ActionType.BUSINESS_ACTION, config, {"handler": "notify", "level": "high"}
        )
        assert decision.kind == DecisionKind.BUSINESS_ACTION
        assert called["config"]["level"] == "high"

    def test_missing_handler_fail_open_degrades_to_log_only(self, config, caplog):
        with caplog.at_level("WARNING"):
            decision = decide_one(
                ActionType.BUSINESS_ACTION, config, {"handler": "missing"}
            )
        # fail-open：记告警并按 LOG_ONLY 语义放行，但不静默（有告警日志）
        assert decision.kind == DecisionKind.CONTINUE
        assert any("未注册" in r.message for r in caplog.records)

    def test_missing_handler_fail_closed_blocks(self, closed_config, caplog):
        with caplog.at_level("ERROR"):
            decision = decide_one(
                ActionType.BUSINESS_ACTION, closed_config, {"handler": "missing"}
            )
        assert decision.kind == DecisionKind.REJECT

    def test_handler_exception_does_not_break_flow(self, config):
        def broken(match, matches, text, action_config):
            raise RuntimeError("boom")

        actions.register_business_action("notify", broken)
        decision = decide_one(
            ActionType.BUSINESS_ACTION, config, {"handler": "notify"}
        )
        assert decision.kind == DecisionKind.BUSINESS_ACTION

"""actions 测试：八种行为映射、ADJUST_PROMPT 指引收集与 BUSINESS_ACTION 白名单 fail 模式。"""
from __future__ import annotations

from agno_worker.moderation import actions
from agno_worker.moderation.models import ActionType, DecisionKind, Match

from conftest import config, closed_config  # noqa: F401  (fixtures)


def make_match(
    action: ActionType,
    rule_id: int = 1,
    type_id: int = 1,
    *,
    type_priority: int = 0,
    spans: tuple[tuple[int, int], ...] = ((0, 3),),
) -> Match:
    return Match(
        rule_id=rule_id,
        type_id=type_id,
        action=action,
        spans=spans,
        type_priority=type_priority,
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
            ActionType.FIXED_REPLY, config, {"reply_text": "请换个话题"}
        )
        assert decision.kind == DecisionKind.RESPOND
        assert decision.message == "请换个话题"

    def test_custom_response(self, config):
        decision = decide_one(
            ActionType.CUSTOM_RESPONSE, config, {"reply_text": "自定义回复"}
        )
        assert decision.kind == DecisionKind.RESPOND
        assert decision.message == "自定义回复"

    def test_end_conversation(self, config):
        decision = decide_one(
            ActionType.END_CONVERSATION, config, {"reply_text": "会话结束"}
        )
        assert decision.kind == DecisionKind.TERMINATE
        assert decision.message == "会话结束"

    def test_legacy_reply_key_remains_compatible(self, config):
        decision = decide_one(
            ActionType.FIXED_REPLY, config, {"reply": "旧配置回复"}
        )
        assert decision.message == "旧配置回复"

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

    def test_business_action_uses_management_api_field(self, config):
        called = []
        actions.register_business_action("audit", lambda *args: called.append(args))
        try:
            decision = decide_one(
                ActionType.BUSINESS_ACTION,
                config,
                {"business_action": "audit"},
            )
        finally:
            actions.unregister_business_action("audit")
        assert decision.kind == DecisionKind.BUSINESS_ACTION
        assert called

    def test_adjust_prompt_continues(self, config):
        decision = decide_one(
            ActionType.ADJUST_PROMPT, config, {"prompt_guidance": "以关怀语气回应"}
        )
        assert decision.kind == DecisionKind.CONTINUE
        assert decision.action == ActionType.ADJUST_PROMPT
        assert decision.prompt_guidances == ["以关怀语气回应"]
        assert decision.redacted_text == ""


class TestPromptGuidanceCollection:
    """ADJUST_PROMPT 指引：按 type_priority DESC / type_id ASC 排序，按 type_id 去重。"""

    def test_sort_dedup_by_type_priority_and_type_id(self, config):
        # matches[0] 决定最终行为；收集顺序独立于列表原序
        matches = [
            make_match(ActionType.ADJUST_PROMPT, rule_id=1, type_id=2, type_priority=50),
            make_match(ActionType.ADJUST_PROMPT, rule_id=2, type_id=1, type_priority=90),
            make_match(ActionType.ADJUST_PROMPT, rule_id=3, type_id=3, type_priority=90),
            make_match(ActionType.ADJUST_PROMPT, rule_id=4, type_id=1, type_priority=90),
        ]
        types_config = {
            1: {"prompt_guidance": "高优A"},
            2: {"prompt_guidance": "低优"},
            3: {"prompt_guidance": "高优B"},
        }
        decision = actions.decide(matches, "text", types_config, config)
        # 90 档 type_id ASC → 1 再 3，然后 50 档 type 2；type 1 去重
        assert decision.prompt_guidances == ["高优A", "高优B", "低优"]

    def test_empty_guidance_ignored(self, config):
        matches = [make_match(ActionType.ADJUST_PROMPT, type_id=1)]
        types_config = {1: {"prompt_guidance": "   "}}
        decision = actions.decide(matches, "text", types_config, config)
        assert decision.prompt_guidances == []

    def test_missing_guidance_ignored(self, config):
        matches = [make_match(ActionType.ADJUST_PROMPT, type_id=1)]
        decision = actions.decide(matches, "text", {1: {}}, config)
        assert decision.prompt_guidances == []


class TestComboPriority:
    """priority=70 组合语义：首条决定行为；放行类仍叠加收集 ADJUST 指引。"""

    def test_self_harm_beats_privacy_adjust_no_redact(self, config):
        # self_harm(70) + privacy(60) → ADJUST 胜出，不做隐私脱敏
        matches = [
            make_match(
                ActionType.ADJUST_PROMPT,
                rule_id=1,
                type_id=10,
                type_priority=70,
                spans=((0, 2),),
            ),
            make_match(
                ActionType.REDACT_AND_CONTINUE,
                rule_id=2,
                type_id=20,
                type_priority=60,
                spans=((3, 6),),
            ),
        ]
        types_config = {
            10: {"prompt_guidance": "关怀指引"},
            20: {"replacement": "*"},
        }
        decision = actions.decide(matches, "自杀 138", types_config, config)
        assert decision.action == ActionType.ADJUST_PROMPT
        assert decision.kind == DecisionKind.CONTINUE
        assert decision.redacted_text == ""
        assert decision.prompt_guidances == ["关怀指引"]

    def test_violence_beats_self_harm_block_collects_but_reject(self, config):
        # self_harm(70) + violence(80) → BLOCK 胜出；decide 仍收集指引（注入由 Guardrail 抑制）
        matches = [
            make_match(
                ActionType.BLOCK_REQUEST,
                rule_id=1,
                type_id=30,
                type_priority=80,
                spans=((0, 2),),
            ),
            make_match(
                ActionType.ADJUST_PROMPT,
                rule_id=2,
                type_id=10,
                type_priority=70,
                spans=((3, 5),),
            ),
        ]
        types_config = {
            30: {},
            10: {"prompt_guidance": "关怀指引"},
        }
        decision = actions.decide(matches, "暴力 自杀", types_config, config)
        assert decision.action == ActionType.BLOCK_REQUEST
        assert decision.kind == DecisionKind.REJECT
        assert decision.prompt_guidances == ["关怀指引"]

    def test_redact_wins_still_collects_guidances(self, config):
        # privacy 胜出且同时命中 ADJUST → REDACT，仍叠加收集指引
        matches = [
            make_match(
                ActionType.REDACT_AND_CONTINUE,
                rule_id=1,
                type_id=20,
                type_priority=60,
                spans=((0, 2),),
            ),
            make_match(
                ActionType.ADJUST_PROMPT,
                rule_id=2,
                type_id=10,
                type_priority=50,
                spans=((3, 5),),
            ),
        ]
        types_config = {
            20: {"replacement": "*"},
            10: {"prompt_guidance": "关怀指引"},
        }
        decision = actions.decide(matches, "手机 轻生", types_config, config)
        assert decision.action == ActionType.REDACT_AND_CONTINUE
        assert decision.kind == DecisionKind.REDACT
        assert decision.redacted_text.startswith("**")
        assert decision.prompt_guidances == ["关怀指引"]


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

"""detector 测试：归一化、文本/正则匹配、多命中、排序、熔断与超时。"""
from __future__ import annotations

import time

import pytest

from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.detector import (
    CompiledPolicy,
    SensitiveContentDetector,
    _CompiledRegexRule,
    compile_policy,
    merge_spans,
    redact_text,
)
from agno_worker.moderation.errors import SensitivePolicyUnavailableError
from agno_worker.moderation.models import ActionType

from conftest import make_policy, make_rule, make_snapshot, make_type


def detect(policy, text, config=None):
    return SensitiveContentDetector(config or ModerationConfig()).detect(text, policy)


class TestNormalization:
    def test_nfkc_fullwidth_match(self):
        """全角字符经 NFKC 归一化后命中半角规则。"""
        policy = make_policy([make_type(1)], [make_rule(1, 1, "abc")])
        matches = detect(policy, "前缀ＡＢＣ后缀")
        assert len(matches) == 1
        assert matches[0].rule_id == 1

    def test_zero_width_chars_stripped(self):
        """零宽字符插入的敏感词仍能命中。"""
        policy = make_policy([make_type(1)], [make_rule(1, 1, "敏感词")])
        matches = detect(policy, "有敏\u200b感\u200c词啊")
        assert len(matches) == 1
        # 命中区间回写到原文坐标，覆盖零宽字符
        start, end = matches[0].spans[0]
        assert "敏" in "有敏\u200b感\u200c词啊"[start:end]
        assert "词" in "有敏\u200b感\u200c词啊"[start:end]

    def test_case_insensitive_match(self):
        policy = make_policy([make_type(1)], [make_rule(1, 1, "badword")])
        assert detect(policy, "This is BadWord here")

    def test_case_sensitive_no_match(self):
        policy = make_policy(
            [make_type(1)], [make_rule(1, 1, "BadWord", case_sensitive=True)]
        )
        assert not detect(policy, "this is badword here")
        assert detect(policy, "this is BadWord here")

    def test_whitespace_folding(self):
        """连续空白折叠为单个空格后命中。"""
        policy = make_policy([make_type(1)], [make_rule(1, 1, "bad word")])
        assert detect(policy, "so bad \n\t word indeed")

    def test_no_normalize_raw_match(self):
        """normalize=False 时按原文匹配，零宽字符不剔除。"""
        policy = make_policy(
            [make_type(1)], [make_rule(1, 1, "敏感词", normalize=False)]
        )
        assert not detect(policy, "敏\u200b感词")
        assert detect(policy, "敏感词")


class TestMatching:
    def test_text_and_regex_match(self):
        policy = make_policy(
            [make_type(1), make_type(2)],
            [
                make_rule(1, 1, "违禁"),
                make_rule(2, 2, r"\d{11}", match_mode="regex", normalize=False),
            ],
        )
        matches = detect(policy, "违禁内容，电话13800138000")
        assert {m.rule_id for m in matches} == {1, 2}

    def test_multiple_hits_single_rule(self):
        policy = make_policy([make_type(1)], [make_rule(1, 1, "bad")])
        matches = detect(policy, "bad things bad people bad")
        assert len(matches) == 1
        assert matches[0].hit_count == 3

    def test_disabled_rule_not_detected(self):
        policy = make_policy(
            [make_type(1)], [make_rule(1, 1, "敏感词", enabled=False)]
        )
        assert not detect(policy, "敏感词")

    def test_disabled_type_not_detected(self):
        policy = make_policy(
            [make_type(1, enabled=False)], [make_rule(1, 1, "敏感词")]
        )
        assert not detect(policy, "敏感词")

    def test_regex_ignorecase(self):
        policy = make_policy(
            [make_type(1)],
            [make_rule(1, 1, r"secret\s+plan", match_mode="regex")],
        )
        assert detect(policy, "the SECRET Plan is here")


class TestSpans:
    def test_merge_overlapping_spans(self):
        assert merge_spans([(0, 5), (3, 8), (10, 12)]) == [(0, 8), (10, 12)]
        assert merge_spans([(3, 8), (0, 5)]) == [(0, 8)]
        assert merge_spans([]) == []

    def test_redact_text(self):
        assert redact_text("hello world", [(0, 5)]) == "***** world"
        assert redact_text("abcdef", [(0, 3), (2, 5)]) == "*****f"

    def test_redact_with_zero_width_source(self):
        """脱敏区间回写覆盖原文中的零宽字符。"""
        text = "有敏\u200b感\u200c词啊"
        policy = make_policy([make_type(1)], [make_rule(1, 1, "敏感词")])
        matches = detect(policy, text)
        spans = [s for m in matches for s in m.spans]
        result = redact_text(text, spans)
        assert "敏" not in result
        assert "感" not in result
        assert "词" not in result
        assert result.startswith("有")
        assert result.endswith("啊")


class TestOrdering:
    def test_type_priority_desc_first(self):
        policy = make_policy(
            [make_type(1, priority=1), make_type(2, priority=9)],
            [make_rule(1, 1, "aaa"), make_rule(2, 2, "bbb")],
        )
        matches = detect(policy, "aaa bbb")
        assert [m.rule_id for m in matches] == [2, 1]

    def test_rule_priority_breaks_tie(self):
        policy = make_policy(
            [make_type(1, priority=5)],
            [make_rule(1, 1, "aaa", priority=1), make_rule(2, 1, "bbb", priority=9)],
        )
        matches = detect(policy, "aaa bbb")
        assert [m.rule_id for m in matches] == [2, 1]

    def test_first_position_breaks_tie(self):
        policy = make_policy(
            [make_type(1)],
            [make_rule(1, 1, "zzz"), make_rule(2, 1, "aaa")],
        )
        matches = detect(policy, "aaa zzz")
        # 同优先级：首个匹配位置靠前者优先
        assert [m.rule_id for m in matches] == [2, 1]

    def test_rule_id_breaks_final_tie(self):
        policy = make_policy(
            [make_type(1)],
            [make_rule(7, 1, "abc"), make_rule(3, 1, "abcd")],
        )
        # abc 与 abcd 在位置 0 同时命中：规则 ID 小者优先
        matches = detect(policy, "abcd")
        assert [m.rule_id for m in matches] == [3, 7]

    def test_deterministic_across_runs(self):
        policy = make_policy(
            [make_type(1, priority=2), make_type(2, priority=2)],
            [
                make_rule(5, 1, "aaa"),
                make_rule(4, 2, "aaa"),
                make_rule(9, 1, "bbb", priority=3),
            ],
        )
        results = [
            [m.rule_id for m in detect(policy, "aaa bbb")] for _ in range(5)
        ]
        assert all(r == results[0] for r in results)


class _FakeCompiled:
    """可控的假正则对象：按脚本决定超时或返回命中。"""

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.calls = 0

    def finditer(self, text: str, timeout: float = 0.0):
        self.calls += 1
        step = self.script.pop(0) if self.script else "match"
        if step == "timeout":
            raise TimeoutError("regex timed out")
        if step == "match":

            class _M:
                @staticmethod
                def span():
                    return (0, 3)

            yield _M()


def _policy_with_fake_regex(script: list[str]) -> tuple[CompiledPolicy, _FakeCompiled]:
    snapshot = make_snapshot(
        [make_type(1)],
        [make_rule(1, 1, r"placeholder", match_mode="regex", normalize=False)],
    )
    policy = compile_policy(snapshot)
    fake = _FakeCompiled(script)
    policy.regex_rules = [
        _CompiledRegexRule(
            rule=policy.regex_rules[0].rule,
            type_=policy.regex_rules[0].type_,
            compiled=fake,
            view_key=policy.regex_rules[0].view_key,
        )
    ]
    return policy, fake


class TestRegexTimeout:
    def test_fail_open_skips_and_warns(self, caplog):
        policy, _ = _policy_with_fake_regex(["timeout"])
        detector = SensitiveContentDetector(ModerationConfig(fail_mode="open"))
        with caplog.at_level("WARNING"):
            matches = detector.detect("any text", policy)
        assert matches == []
        assert any("超时" in r.message for r in caplog.records)

    def test_fail_closed_raises_policy_unavailable(self):
        policy, _ = _policy_with_fake_regex(["timeout"])
        detector = SensitiveContentDetector(ModerationConfig(fail_mode="closed"))
        with pytest.raises(SensitivePolicyUnavailableError):
            detector.detect("any text", policy)

    def test_breaker_trips_after_consecutive_timeouts(self, caplog):
        policy, fake = _policy_with_fake_regex(["timeout"] * 3 + ["match"])
        detector = SensitiveContentDetector(
            ModerationConfig(fail_mode="open", breaker_threshold=3, breaker_cooldown=60)
        )
        with caplog.at_level("ERROR"):
            for _ in range(3):
                detector.detect("text", policy)
        assert any("熔断" in r.message for r in caplog.records)
        # 熔断期间规则被跳过，不再调用正则
        calls_before = fake.calls
        detector.detect("text", policy)
        assert fake.calls == calls_before

    def test_breaker_recovers_after_cooldown(self):
        policy, fake = _policy_with_fake_regex(["timeout"] * 3 + ["match", "match"])
        detector = SensitiveContentDetector(
            ModerationConfig(
                fail_mode="open", breaker_threshold=3, breaker_cooldown=0.05
            )
        )
        for _ in range(3):
            detector.detect("text", policy)
        # 冷却期内跳过
        calls_before = fake.calls
        detector.detect("text", policy)
        assert fake.calls == calls_before
        # 冷却期后自动重试并恢复
        time.sleep(0.06)
        matches = detector.detect("text", policy)
        assert fake.calls == calls_before + 1
        assert len(matches) == 1
        # 恢复后计数清零，可继续正常匹配
        matches = detector.detect("text", policy)
        assert len(matches) == 1


class TestCompile:
    def test_invalid_regex_skipped(self, caplog):
        with caplog.at_level("WARNING"):
            policy = make_policy(
                [make_type(1)],
                [make_rule(1, 1, r"([unclosed", match_mode="regex")],
            )
        assert policy.regex_rules == []

    def test_only_enabled_rules_compiled(self):
        policy = make_policy(
            [make_type(1)],
            [
                make_rule(1, 1, "aaa"),
                make_rule(2, 1, "bbb", enabled=False),
                make_rule(3, 1, r"ccc", match_mode="regex", enabled=False),
            ],
        )
        assert len(policy.text_rules) == 1
        assert policy.regex_rules == []

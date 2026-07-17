"""纯逻辑测试：pattern 校验、规范化、action_config 白名单、审计脱敏。"""
from __future__ import annotations

import hashlib
import json

import pytest

from sensitive_content.audit import sanitize_changes
from sensitive_content.models import (
    MAX_PATTERN_LENGTH,
    MAX_PROMPT_GUIDANCE_LENGTH,
    Action,
    ValidationFailure,
    canonical_pattern,
    dedup_key,
    has_nested_quantifier,
    validate_action_config,
    validate_pattern,
)


class TestValidatePattern:
    def test_valid_text_pattern(self):
        validate_pattern("敏感词", "text")

    def test_valid_regex(self):
        validate_pattern(r"\d{11}", "regex")

    def test_empty_pattern_rejected(self):
        with pytest.raises(ValidationFailure) as exc:
            validate_pattern("   ", "text")
        assert exc.value.code == "empty_pattern"

    def test_pattern_too_long_rejected(self):
        with pytest.raises(ValidationFailure) as exc:
            validate_pattern("x" * (MAX_PATTERN_LENGTH + 1), "text")
        assert exc.value.code == "pattern_too_long"

    def test_pattern_at_limit_accepted(self):
        validate_pattern("x" * MAX_PATTERN_LENGTH, "text")

    def test_invalid_regex_syntax_rejected(self):
        with pytest.raises(ValidationFailure) as exc:
            validate_pattern("([a-z", "regex")
        assert exc.value.code == "invalid_regex"

    def test_nested_quantifier_rejected(self):
        with pytest.raises(ValidationFailure) as exc:
            validate_pattern("(a+)+b", "regex")
        assert exc.value.code == "regex_too_complex"

    def test_text_mode_ignores_regex_syntax(self):
        # 文本规则不做正则语法校验
        validate_pattern("([a-z", "text")


class TestNestedQuantifier:
    @pytest.mark.parametrize(
        "pattern",
        ["(a+)+", "(a*)*", "(ab+)*", "(a{2,})+", "x(a+){3}"],
    )
    def test_detects_nested(self, pattern: str):
        assert has_nested_quantifier(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [r"\d+", "(abc)+", "a+b*", r"(a\+)+", "[+*]+", "(a|b)?c+"],
    )
    def test_allows_safe(self, pattern: str):
        assert not has_nested_quantifier(pattern)


class TestCanonicalPattern:
    def test_nfkc_and_case_fold(self):
        # 全角字符 NFKC 归一化 + 大小写折叠
        assert canonical_pattern("ＡＢＣ", "text", False, True) == "abc"

    def test_zero_width_stripped(self):
        assert canonical_pattern("敏\u200b感", "text", False, True) == "敏感"

    def test_whitespace_collapsed(self):
        assert canonical_pattern("  a   b ", "text", False, True) == "a b"

    def test_case_sensitive_keeps_case(self):
        assert canonical_pattern("AbC", "text", True, False) == "AbC"

    def test_regex_untouched(self):
        assert canonical_pattern("A+\u200b", "regex", False, True) == "A+\u200b"

    def test_dedup_key_equivalence(self):
        a = {"pattern": "ＡＢＣ", "match_mode": "text", "case_sensitive": False, "normalize": True}
        b = {"pattern": "abc", "match_mode": "text", "case_sensitive": False, "normalize": True}
        assert dedup_key(a) == dedup_key(b)

    def test_dedup_key_differs_by_flags(self):
        a = {"pattern": "abc", "match_mode": "text", "case_sensitive": False, "normalize": True}
        b = {"pattern": "abc", "match_mode": "regex", "case_sensitive": False, "normalize": True}
        assert dedup_key(a) != dedup_key(b)


class TestActionConfig:
    def test_whitelist_keys_accepted_for_log_only(self):
        validate_action_config(
            Action.LOG_ONLY,
            {"reply_text": "抱歉", "replacement": "*", "business_action": "notify"},
        )

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationFailure) as exc:
            validate_action_config(Action.LOG_ONLY, {"evil": "x"})
        assert exc.value.code == "invalid_action_config"

    def test_adjust_prompt_requires_guidance(self):
        with pytest.raises(ValidationFailure) as exc:
            validate_action_config(Action.ADJUST_PROMPT, {})
        assert exc.value.code == "invalid_action_config"

    def test_adjust_prompt_rejects_blank_guidance(self):
        with pytest.raises(ValidationFailure) as exc:
            validate_action_config(Action.ADJUST_PROMPT, {"prompt_guidance": "  "})
        assert exc.value.code == "invalid_action_config"

    def test_adjust_prompt_accepts_guidance(self):
        validate_action_config(
            Action.ADJUST_PROMPT, {"prompt_guidance": "以关怀语气回应"}
        )

    def test_adjust_prompt_guidance_too_long(self):
        with pytest.raises(ValidationFailure) as exc:
            validate_action_config(
                Action.ADJUST_PROMPT,
                {"prompt_guidance": "x" * (MAX_PROMPT_GUIDANCE_LENGTH + 1)},
            )
        assert exc.value.code == "invalid_action_config"

    def test_non_adjust_rejects_prompt_guidance(self):
        with pytest.raises(ValidationFailure) as exc:
            validate_action_config(
                Action.LOG_ONLY, {"prompt_guidance": "不应出现"}
            )
        assert exc.value.code == "invalid_action_config"

    def test_string_action_accepted(self):
        validate_action_config(
            "ADJUST_PROMPT", {"prompt_guidance": "以关怀语气回应"}
        )


class TestSanitizeChanges:
    def test_no_plaintext_only_hash_and_length(self):
        secret = "机密敏感词"
        out = sanitize_changes({"pattern": secret, "priority": 5})
        dumped = json.dumps(out, ensure_ascii=False)
        assert secret not in dumped
        assert out["pattern"]["sha256"] == hashlib.sha256(secret.encode()).hexdigest()
        assert out["pattern"]["length"] == len(secret)
        assert set(out["pattern"]) == {"sha256", "length"}

    def test_none_value(self):
        assert sanitize_changes({"x": None}) == {"x": {"null": True}}

    def test_dict_value_hashed(self):
        out = sanitize_changes({"action_config": {"reply_text": "秘密文案"}})
        assert "秘密文案" not in json.dumps(out, ensure_ascii=False)
        assert "sha256" in out["action_config"]

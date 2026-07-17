"""Pydantic 模型、行为枚举与规则校验纯函数（不依赖 DB，便于单测）。"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field

# 规则 pattern 最大长度
MAX_PATTERN_LENGTH = 512

# prompt_guidance 最大长度（ADJUST_PROMPT）
MAX_PROMPT_GUIDANCE_LENGTH = 2000

# action_config 允许的白名单 key（回复文案、脱敏替换符、业务动作名、语气指引）
ACTION_CONFIG_ALLOWED_KEYS = frozenset(
    {"reply_text", "replacement", "business_action", "prompt_guidance"}
)

# 零宽字符集合（归一化时剔除）
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"


class Action(str, Enum):
    """敏感内容响应行为枚举（与 sensitive_action 表种子数据一致）。"""

    END_CONVERSATION = "END_CONVERSATION"
    FIXED_REPLY = "FIXED_REPLY"
    BLOCK_REQUEST = "BLOCK_REQUEST"
    REDACT_AND_CONTINUE = "REDACT_AND_CONTINUE"
    LOG_ONLY = "LOG_ONLY"
    BUSINESS_ACTION = "BUSINESS_ACTION"
    CUSTOM_RESPONSE = "CUSTOM_RESPONSE"
    ADJUST_PROMPT = "ADJUST_PROMPT"


class MatchMode(str, Enum):
    TEXT = "text"
    REGEX = "regex"


class ValidationFailure(Exception):
    """规则/类型入参校验失败（携带机器可读错误码）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# 校验纯函数
# ---------------------------------------------------------------------------

def _contains_quantifier(fragment: str) -> bool:
    """判断正则片段内是否含量词（* + {），忽略转义与字符类内部。"""
    i = 0
    in_class = False
    while i < len(fragment):
        ch = fragment[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
        elif ch in "*+{":
            return True
        i += 1
    return False


def has_nested_quantifier(pattern: str) -> bool:
    """启发式检测嵌套量词（如 (a+)+），此类模式易导致灾难性回溯。"""
    stack: list[int] = []
    spans: list[tuple[int, int]] = []
    i = 0
    in_class = False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
        elif ch == "(":
            stack.append(i)
        elif ch == ")":
            if stack:
                spans.append((stack.pop(), i))
        i += 1
    for start, end in spans:
        follower = pattern[end + 1 : end + 2]
        if follower in ("*", "+", "{"):
            if _contains_quantifier(pattern[start + 1 : end]):
                return True
    return False


def validate_pattern(pattern: str, match_mode: str) -> None:
    """校验规则 pattern：非空、长度上限、正则语法与嵌套量词复杂度。"""
    if not pattern or not pattern.strip():
        raise ValidationFailure("empty_pattern", "pattern must not be empty")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValidationFailure(
            "pattern_too_long",
            f"pattern length {len(pattern)} exceeds limit {MAX_PATTERN_LENGTH}",
        )
    if match_mode == MatchMode.REGEX.value:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValidationFailure("invalid_regex", f"invalid regex: {exc}") from exc
        if has_nested_quantifier(pattern):
            raise ValidationFailure(
                "regex_too_complex",
                "nested quantifier detected (catastrophic backtracking risk)",
            )


def validate_action_config(
    action: Action | str, action_config: dict[str, Any]
) -> None:
    """校验 action_config：白名单 key，以及 ADJUST_PROMPT 的 prompt_guidance 语义。

    - ADJUST_PROMPT：必须有非空 prompt_guidance（≤ MAX_PROMPT_GUIDANCE_LENGTH）
    - 其它 action：不得携带 prompt_guidance（不静默过滤，直接 400）
    """
    action_value = action.value if isinstance(action, Action) else str(action)
    extra = set(action_config) - ACTION_CONFIG_ALLOWED_KEYS
    if extra:
        raise ValidationFailure(
            "invalid_action_config",
            f"action_config keys not allowed: {sorted(extra)}",
        )
    if action_value == Action.ADJUST_PROMPT.value:
        guidance = action_config.get("prompt_guidance")
        if not isinstance(guidance, str) or not guidance.strip():
            raise ValidationFailure(
                "invalid_action_config",
                "ADJUST_PROMPT requires non-empty prompt_guidance",
            )
        if len(guidance) > MAX_PROMPT_GUIDANCE_LENGTH:
            raise ValidationFailure(
                "invalid_action_config",
                f"prompt_guidance length {len(guidance)} exceeds "
                f"limit {MAX_PROMPT_GUIDANCE_LENGTH}",
            )
    elif "prompt_guidance" in action_config:
        raise ValidationFailure(
            "invalid_action_config",
            "prompt_guidance is only allowed for ADJUST_PROMPT",
        )


def canonical_pattern(
    pattern: str, match_mode: str, case_sensitive: bool, normalize: bool
) -> str:
    """规范化 pattern，用于同租户重复规则判定与快照去重。

    文本规则在 normalize=True 时执行 NFKC、去零宽字符、空白折叠；
    case_sensitive=False 时统一 casefold。正则 pattern 保持原样
    （大小写语义由引擎 flag 决定，文本层面不做变换）。
    """
    if match_mode == MatchMode.REGEX.value:
        return pattern
    out = pattern
    if normalize:
        out = unicodedata.normalize("NFKC", out)
        out = out.translate({ord(c): None for c in _ZERO_WIDTH})
        out = re.sub(r"\s+", " ", out).strip()
    if not case_sensitive:
        out = out.casefold()
    return out


def dedup_key(rule: dict[str, Any]) -> tuple[str, bool, bool, str]:
    """规则去重键：同 match_mode/case_sensitive/normalize 下规范化后相等即重复。"""
    return (
        str(rule["match_mode"]),
        bool(rule["case_sensitive"]),
        bool(rule["normalize"]),
        canonical_pattern(
            str(rule["pattern"]),
            str(rule["match_mode"]),
            bool(rule["case_sensitive"]),
            bool(rule["normalize"]),
        ),
    )


# ---------------------------------------------------------------------------
# Pydantic 请求/响应模型
# ---------------------------------------------------------------------------

class TypeCreate(BaseModel):
    # 缺省时由 store.create_type 自动生成（如 t_xxxxxxxxxxxx）
    code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    action: Action
    action_config: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    description: str = ""
    enabled: bool = True


class TypeUpdate(BaseModel):
    name: Optional[str] = None
    action: Optional[Action] = None
    action_config: Optional[dict[str, Any]] = None
    priority: Optional[int] = None
    description: Optional[str] = None


class RuleCreate(BaseModel):
    type_id: int
    pattern: str
    match_mode: MatchMode = MatchMode.TEXT
    case_sensitive: bool = False
    normalize: bool = True
    overrides_global_rule_id: Optional[int] = None
    description: str = ""
    priority: int = 0
    remark: str = ""
    enabled: bool = True


class RuleUpdate(BaseModel):
    type_id: Optional[int] = None
    pattern: Optional[str] = None
    match_mode: Optional[MatchMode] = None
    case_sensitive: Optional[bool] = None
    normalize: Optional[bool] = None
    overrides_global_rule_id: Optional[int] = None
    description: Optional[str] = None
    priority: Optional[int] = None
    remark: Optional[str] = None


class HitEventIn(BaseModel):
    event_id: UUID
    rule_id: int
    type_id: int
    rule_action: Action
    final_action: Action
    selected: bool = False
    final_rule_id: int
    tenant_id: str
    request_fingerprint: str
    session_fingerprint: str = ""
    # 明文会话标识（产品决策：供后台跳转查看完整会话；用户原文仍不落库）
    session_id: str = ""
    policy_version: str = ""
    hit_count: int = 1
    hit_at: Optional[datetime] = None


class HitEventBatch(BaseModel):
    events: list[HitEventIn] = Field(default_factory=list)

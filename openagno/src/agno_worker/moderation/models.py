"""敏感内容检测端数据模型：规则 / 类型 / 快照 / 命中 / 决策。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActionType(str, Enum):
    """七种响应行为（与管理服务 sensitive_type.action 枚举一致）。"""

    END_CONVERSATION = "END_CONVERSATION"
    FIXED_REPLY = "FIXED_REPLY"
    BLOCK_REQUEST = "BLOCK_REQUEST"
    REDACT_AND_CONTINUE = "REDACT_AND_CONTINUE"
    LOG_ONLY = "LOG_ONLY"
    BUSINESS_ACTION = "BUSINESS_ACTION"
    CUSTOM_RESPONSE = "CUSTOM_RESPONSE"

    @classmethod
    def parse(cls, value: str) -> "ActionType":
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            # 未知行为按 LOG_ONLY 兜底，避免快照升级引入新枚举时旧端崩溃
            return cls.LOG_ONLY


class DecisionKind(str, Enum):
    """GuardrailDecision 的分派类别（检测与响应行为解耦的中间层）。"""

    CONTINUE = "continue"
    REDACT = "redact"
    RESPOND = "respond"
    REJECT = "reject"
    TERMINATE = "terminate"
    BUSINESS_ACTION = "business_action"


@dataclass(frozen=True)
class SensitiveType:
    """敏感内容类型（行为配置挂在类型上）。"""

    id: int
    code: str = ""
    name: str = ""
    action: ActionType = ActionType.LOG_ONLY
    action_config: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    enabled: bool = True
    tenant_id: str = ""

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "SensitiveType":
        return cls(
            id=int(data.get("id", 0)),
            code=str(data.get("code", "")),
            name=str(data.get("name", "")),
            action=ActionType.parse(str(data.get("action", "LOG_ONLY"))),
            action_config=dict(data.get("action_config") or {}),
            priority=int(data.get("priority", 0)),
            enabled=bool(data.get("enabled", True)),
            tenant_id=str(data.get("tenant_id", "")),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "action": self.action.value,
            "action_config": dict(self.action_config),
            "priority": self.priority,
            "enabled": self.enabled,
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True)
class SensitiveRule:
    """敏感内容规则（文本或正则）。"""

    id: int
    type_id: int
    pattern: str
    match_mode: str = "text"  # text | regex
    case_sensitive: bool = False
    normalize: bool = True
    priority: int = 0
    enabled: bool = True
    tenant_id: str = ""

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "SensitiveRule":
        return cls(
            id=int(data.get("id", 0)),
            type_id=int(data.get("type_id", 0)),
            pattern=str(data.get("pattern", "")),
            match_mode=str(data.get("match_mode", "text")).lower(),
            case_sensitive=bool(data.get("case_sensitive", False)),
            normalize=bool(data.get("normalize", True)),
            priority=int(data.get("priority", 0)),
            enabled=bool(data.get("enabled", True)),
            tenant_id=str(data.get("tenant_id", "")),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type_id": self.type_id,
            "pattern": self.pattern,
            "match_mode": self.match_mode,
            "case_sensitive": self.case_sensitive,
            "normalize": self.normalize,
            "priority": self.priority,
            "enabled": self.enabled,
            "tenant_id": self.tenant_id,
        }


@dataclass
class PolicySnapshot:
    """一个租户的策略快照（含全局+租户合并后的规则集与组合版本）。"""

    tenant_id: str
    version: str = ""  # 组合版本，如 "global-12:tenant-37"
    etag: str = ""
    rules: list[SensitiveRule] = field(default_factory=list)
    types: dict[int, SensitiveType] = field(default_factory=dict)
    fetched_at: float = field(default_factory=time.time)

    def is_stale(self, max_stale: float, *, now: float | None = None) -> bool:
        """快照是否超过过期上限（超限视为无有效快照，按 fail 模式处理）。"""
        current = time.time() if now is None else now
        return (current - self.fetched_at) > max_stale

    @classmethod
    def from_payload(cls, tenant_id: str, data: dict[str, Any]) -> "PolicySnapshot":
        types = {
            t.id: t
            for t in (SensitiveType.from_payload(item) for item in data.get("types") or [])
        }
        rules = [SensitiveRule.from_payload(item) for item in data.get("rules") or []]
        return cls(
            tenant_id=tenant_id,
            version=str(data.get("version", "")),
            etag=str(data.get("etag", "")),
            rules=rules,
            types=types,
            fetched_at=float(data.get("fetched_at") or time.time()),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "etag": self.etag,
            "fetched_at": self.fetched_at,
            "types": [t.to_payload() for t in self.types.values()],
            "rules": [r.to_payload() for r in self.rules],
        }


@dataclass(frozen=True)
class Match:
    """单条规则的命中结果（一条规则可命中多个区间）。"""

    rule_id: int
    type_id: int
    action: ActionType
    spans: tuple[tuple[int, int], ...]  # 原文字符区间 [start, end)
    rule_priority: int = 0
    type_priority: int = 0

    @property
    def hit_count(self) -> int:
        return len(self.spans)

    @property
    def first_pos(self) -> int:
        return self.spans[0][0] if self.spans else -1


@dataclass
class GuardrailDecision:
    """检测结果映射出的最终决策（由排序第一条命中决定行为）。"""

    kind: DecisionKind
    action: ActionType
    rule_id: int = 0
    type_id: int = 0
    action_config: dict[str, Any] = field(default_factory=dict)
    matches: list[Match] = field(default_factory=list)
    message: str = ""  # respond/terminate 场景的回复文案
    redacted_text: str = ""  # redact 场景的脱敏后文本
    policy_version: str = ""

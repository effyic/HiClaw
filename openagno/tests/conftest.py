"""moderation 测试共享夹具与构造辅助。"""
from __future__ import annotations

import time
from typing import Any

import pytest

from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.detector import CompiledPolicy, compile_policy
from agno_worker.moderation.models import (
    ActionType,
    PolicySnapshot,
    SensitiveRule,
    SensitiveType,
)


def make_type(
    type_id: int,
    action: ActionType | str = ActionType.LOG_ONLY,
    *,
    priority: int = 0,
    action_config: dict[str, Any] | None = None,
    enabled: bool = True,
) -> SensitiveType:
    return SensitiveType(
        id=type_id,
        code=f"type-{type_id}",
        name=f"类型{type_id}",
        action=ActionType.parse(str(action.value if isinstance(action, ActionType) else action)),
        action_config=dict(action_config or {}),
        priority=priority,
        enabled=enabled,
    )


def make_rule(
    rule_id: int,
    type_id: int,
    pattern: str,
    *,
    match_mode: str = "text",
    case_sensitive: bool = False,
    normalize: bool = True,
    priority: int = 0,
    enabled: bool = True,
) -> SensitiveRule:
    return SensitiveRule(
        id=rule_id,
        type_id=type_id,
        pattern=pattern,
        match_mode=match_mode,
        case_sensitive=case_sensitive,
        normalize=normalize,
        priority=priority,
        enabled=enabled,
    )


def make_snapshot(
    types: list[SensitiveType],
    rules: list[SensitiveRule],
    *,
    tenant_id: str = "tenant-a",
    agent_id: int = 0,
    binding_rule_ids: list[int] | None = None,
    version: str = "global-1:tenant-1",
    fetched_at: float | None = None,
) -> PolicySnapshot:
    return PolicySnapshot(
        tenant_id=tenant_id,
        agent_id=agent_id,
        binding_rule_ids=list(binding_rule_ids or []),
        version=version,
        etag='"etag-test"',
        rules=list(rules),
        types={t.id: t for t in types},
        fetched_at=fetched_at if fetched_at is not None else time.time(),
    )


def make_policy(
    types: list[SensitiveType],
    rules: list[SensitiveRule],
    **kwargs: Any,
) -> CompiledPolicy:
    return compile_policy(make_snapshot(types, rules, **kwargs))


class CaptureReporter:
    """测试用上报器：仅记录事件，不做网络请求。"""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.started = False

    def enqueue(self, events: list[Any]) -> int:
        self.events.extend(events)
        return len(events)

    def ensure_started(self) -> None:
        self.started = True


@pytest.fixture
def config() -> ModerationConfig:
    """默认 fail-open 配置（不依赖环境变量）。"""
    return ModerationConfig(
        service_url="http://sensitive-content.test",
        fingerprint_key="test-fingerprint-key",
        fail_mode="open",
    )


@pytest.fixture
def closed_config() -> ModerationConfig:
    return ModerationConfig(
        service_url="http://sensitive-content.test",
        fingerprint_key="test-fingerprint-key",
        fail_mode="closed",
    )

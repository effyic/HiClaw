"""敏感内容检测模块（sensitive-content 管理服务的 openagno 检测端）。

对外入口：
- ``maybe_build_guardrail()``：配置 ``SENSITIVE_CONTENT_SERVICE_URL`` 时构建
  :class:`SensitiveContentGuardrail`，未配置时返回 ``None``（保持现有行为不变）。
- ``SensitiveContentDecisionError`` / ``SensitivePolicyUnavailableError``：
  供 runtime/server 做响应映射。
"""
from __future__ import annotations

from agno_worker.moderation.errors import (
    SensitivePolicyUnavailableError,
    SensitiveRegexTimeoutError,
)
from agno_worker.moderation.models import (
    ActionType,
    GuardrailDecision,
    Match,
    PolicySnapshot,
    SensitiveRule,
    SensitiveType,
)

__all__ = [
    "ActionType",
    "GuardrailDecision",
    "Match",
    "PolicySnapshot",
    "SensitiveRule",
    "SensitiveType",
    "SensitivePolicyUnavailableError",
    "SensitiveRegexTimeoutError",
    "maybe_build_guardrail",
    "SensitiveContentDecisionError",
]


def maybe_build_guardrail():  # noqa: ANN201 - 延迟导入避免强依赖 agno
    """按环境变量决定是否构建 Guardrail；未配置服务地址时返回 None。"""
    from agno_worker.moderation.guardrail import maybe_build_guardrail as _impl

    return _impl()


def __getattr__(name: str):  # noqa: ANN202
    # SensitiveContentDecisionError 依赖 agno（InputCheckError 基类），延迟导入。
    if name == "SensitiveContentDecisionError":
        from agno_worker.moderation.guardrail import SensitiveContentDecisionError

        return SensitiveContentDecisionError
    raise AttributeError(name)

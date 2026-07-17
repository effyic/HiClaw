"""请求级上下文（contextvars）：由 engine 入口写入，供 Guardrail 读取。

每个 HTTP 请求生成独立 ``request_id``（uuid4）——``request_fingerprint`` 由它派生，
不得从 ``session_id`` 派生，否则同一会话的多次命中请求会被幂等去重合并，
导致"命中请求数"统计错误；``session_fingerprint`` 才由 ``session_id`` 派生。

另外，agno 在 pre-hook 抛出 ``InputCheckError`` 时会在内部捕获并返回错误
RunOutput（不向调用方重抛），因此 Guardrail 把决策写回本上下文对象
（``pending_decision`` / ``policy_unavailable``），engine 在 ``arun`` 返回后
读取并完成响应映射。
"""
from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RequestContext:
    """一次对话请求的身份信息与 Guardrail 决策回传通道。"""

    tenant_id: str = ""
    agent_id: int = 0
    user_id: str = ""
    session_id: str = ""
    request_id: str = ""
    # Guardrail 写回：阻断/固定回复/终止等决策（GuardrailDecision）
    pending_decision: Any = None
    # Guardrail 写回：fail-closed 且无有效快照（503 语义）
    policy_unavailable: bool = False
    # Guardrail 写回：本轮放行时注入的语气指引（阻断路径保持空）
    prompt_guidances: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


_current: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "sensitive_content_request_context", default=None
)


def new_request_id() -> str:
    """为当前请求生成独立请求标识。"""
    return uuid.uuid4().hex


def set_request_context(
    *,
    tenant_id: str = "",
    agent_id: int = 0,
    user_id: str = "",
    session_id: str = "",
    request_id: str = "",
) -> tuple[RequestContext, contextvars.Token]:
    """写入当前请求上下文，返回 (上下文对象, token)；token 供 finally 复位。"""
    ctx = RequestContext(
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        request_id=request_id or new_request_id(),
    )
    return ctx, _current.set(ctx)


def reset_request_context(token: contextvars.Token) -> None:
    _current.reset(token)


def get_request_context() -> RequestContext:
    """读取当前请求上下文；未设置时返回空上下文（如单测直接调用 Guardrail）。"""
    ctx = _current.get()
    if ctx is None:
        ctx = RequestContext(request_id=new_request_id())
        _current.set(ctx)
    return ctx

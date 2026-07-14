"""Per-request model thinking mode via contextvars."""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_enable_thinking: ContextVar[bool] = ContextVar("agno_enable_thinking", default=False)


def get_enable_thinking() -> bool:
    return bool(_enable_thinking.get())


def set_enable_thinking(enabled: bool) -> Token:
    return _enable_thinking.set(bool(enabled))


def reset_enable_thinking(token: Token) -> None:
    _enable_thinking.reset(token)


def attach_thinking_request_params(model: Any) -> Any:
    """Inject ``enable_thinking`` into OpenAI-compatible ``extra_body`` per request.

    Uses a ContextVar so concurrent requests on a shared Agent/model do not race
    when mutating ``model.extra_body`` directly.
    """
    if model is None or not hasattr(model, "get_request_params"):
        return model
    if getattr(model, "_agno_thinking_patched", False):
        return model

    original = model.get_request_params

    def _patched(*args: Any, **kwargs: Any) -> dict[str, Any]:
        params = dict(original(*args, **kwargs) or {})
        extra = dict(params.get("extra_body") or getattr(model, "extra_body", None) or {})
        extra["enable_thinking"] = get_enable_thinking()
        params["extra_body"] = extra
        return params

    model.get_request_params = _patched  # type: ignore[method-assign]
    model._agno_thinking_patched = True

    existing = getattr(model, "extra_body", None)
    if existing is None:
        model.extra_body = {"enable_thinking": False}
    elif isinstance(existing, dict):
        merged = dict(existing)
        merged.setdefault("enable_thinking", False)
        model.extra_body = merged
    return model

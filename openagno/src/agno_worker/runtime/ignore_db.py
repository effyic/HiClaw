"""Per-request skip of Agno session DB I/O via contextvars."""
from __future__ import annotations

from contextvars import ContextVar, Token

_ignore_db: ContextVar[bool] = ContextVar("agno_ignore_db", default=False)


def get_ignore_db() -> bool:
    return bool(_ignore_db.get())


def set_ignore_db(enabled: bool) -> Token:
    return _ignore_db.set(bool(enabled))


def reset_ignore_db(token: Token) -> None:
    _ignore_db.reset(token)

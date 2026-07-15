"""Per-request Agno session DB I/O policy via contextvars.

When ``x-ignore-db`` is true:
- with ``session_id`` → skip write only (still load history)
- without ``session_id`` → skip both read and write
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IgnoreDbPolicy:
    """Resolved per-request session storage policy."""

    skip_write: bool = False
    skip_read: bool = False

    @property
    def enabled(self) -> bool:
        """True when any ignore-db behavior is active (at least skip write)."""
        return self.skip_write or self.skip_read


_EMPTY = IgnoreDbPolicy()
_policy: ContextVar[IgnoreDbPolicy] = ContextVar("agno_ignore_db_policy", default=_EMPTY)


def resolve_ignore_db_policy(*, ignore_db: bool, session_id: str = "") -> IgnoreDbPolicy:
    """Map header flag + session_id into read/write skip flags."""
    if not ignore_db:
        return _EMPTY
    has_session = bool(str(session_id or "").strip())
    if has_session:
        return IgnoreDbPolicy(skip_write=True, skip_read=False)
    return IgnoreDbPolicy(skip_write=True, skip_read=True)


def get_ignore_db_policy() -> IgnoreDbPolicy:
    return _policy.get()


def get_ignore_db() -> bool:
    """True when session writes must be skipped (``x-ignore-db``)."""
    return get_ignore_db_policy().skip_write


def get_skip_session_write() -> bool:
    return get_ignore_db_policy().skip_write


def get_skip_session_read() -> bool:
    return get_ignore_db_policy().skip_read


def set_ignore_db_policy(policy: IgnoreDbPolicy) -> Token:
    return _policy.set(policy)


def set_ignore_db(enabled: bool, *, session_id: str = "") -> Token:
    """Compatibility helper: set policy from ignore flag + optional session_id."""
    return set_ignore_db_policy(
        resolve_ignore_db_policy(ignore_db=enabled, session_id=session_id)
    )


def reset_ignore_db(token: Token) -> None:
    _policy.reset(token)

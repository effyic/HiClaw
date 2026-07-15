"""Resolve caller identity from HTTP headers, body, and query string."""
from __future__ import annotations

import os
from typing import Mapping

# Align with aip-hub WebFrameworkUtils (tenant-id) and common gateway headers.
USER_ID_HEADERS = ("user-id", "x-user-id")
TENANT_ID_HEADERS = ("tenant-id", "x-tenant-id")
SESSION_ID_HEADERS = ("session-id", "x-session-id")
ROLE_CODE_HEADERS = ("role-code", "x-role-code")
DEBUG_REQUEST_HEADERS = ("x-debug-request", "x-debug-requet")


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _header_value(headers: Mapping[str, str], names: tuple[str, ...]) -> str:
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    for name in names:
        if value := lowered.get(name):
            if value.strip():
                return value.strip()
    return ""


def _truthy_flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _falsy_flag(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "off"}


def _parse_optional_bool(value: str | None) -> bool | None:
    """Parse an explicit bool flag; return None when absent / unrecognized."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _truthy_flag(text):
        return True
    if _falsy_flag(text):
        return False
    return None


def resolve_user_id(
    *,
    body_user_id: str = "",
    headers: Mapping[str, str] | None = None,
    query_user_id: str = "",
) -> str:
    """Prefer gateway-injected headers, then body, then query string."""
    return _first_non_empty(
        _header_value(headers or {}, USER_ID_HEADERS),
        body_user_id,
        query_user_id,
    )


def resolve_tenant_id(
    *,
    headers: Mapping[str, str] | None = None,
    query_tenant_id: str = "",
) -> str:
    """Prefer gateway-injected headers, then query string."""
    return _first_non_empty(
        _header_value(headers or {}, TENANT_ID_HEADERS),
        query_tenant_id,
    )


def resolve_session_id(
    *,
    headers: Mapping[str, str] | None = None,
    query_session_id: str = "",
) -> str:
    """Prefer gateway-injected headers, then query string."""
    return _first_non_empty(
        _header_value(headers or {}, SESSION_ID_HEADERS),
        query_session_id,
    )


def resolve_role_code(
    *,
    headers: Mapping[str, str] | None = None,
    query_role_code: str = "",
) -> str:
    """Prefer gateway-injected headers, then query string."""
    return _first_non_empty(
        _header_value(headers or {}, ROLE_CODE_HEADERS),
        query_role_code,
    )


def default_debug_request() -> bool:
    """Default storage mode when ``x-debug-request`` header is absent.

    Controlled by ``AGNO_DEBUG_REQUEST_DEFAULT`` (Helm ``globalEnv``); defaults to slim storage.
    """
    return os.environ.get("AGNO_DEBUG_REQUEST_DEFAULT", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_debug_request(headers: Mapping[str, str] | None = None) -> bool:
    """Return True for full session persistence; False for slim storage.

    Header ``x-debug-request`` overrides ``AGNO_DEBUG_REQUEST_DEFAULT`` when present.
    """
    value = _header_value(headers or {}, DEBUG_REQUEST_HEADERS)
    if not value:
        return default_debug_request()
    return _truthy_flag(value)


def resolve_ignore_db(headers: Mapping[str, str] | None = None) -> bool:
    """Return True when this request must skip Agno session DB read/write.

    Expects lowercase header name ``x-ignore-db`` (Starlette/FastAPI norm).
    Values: true/1/yes/on → skip; absent or anything else → normal storage.
    """
    if not headers:
        return False
    value = str(headers.get("x-ignore-db") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def resolve_enable_thinking(
    *,
    body_enable_thinking: bool | None = None,
    headers: Mapping[str, str] | None = None,
    query_enable_thinking: str = "",
) -> bool:
    """Resolve whether the model should run with thinking/reasoning enabled.

    Priority (highest → lowest):
    1. Explicit request param: body ``enable_thinking`` → query ``enable_thinking``
    2. Header ``x-debug-request`` (true → on, false → off)
    3. Default ``False`` (thinking off)

    Only ``x-debug-request`` is read from headers (no ``x-enable-thinking``).
    """
    if body_enable_thinking is not None:
        return bool(body_enable_thinking)

    query_flag = _parse_optional_bool(query_enable_thinking)
    if query_flag is not None:
        return query_flag

    debug_flag = _parse_optional_bool(
        _header_value(headers or {}, DEBUG_REQUEST_HEADERS)
    )
    if debug_flag is not None:
        return debug_flag

    return False

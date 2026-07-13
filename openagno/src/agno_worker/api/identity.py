"""Resolve caller identity from HTTP headers, body, and query string."""
from __future__ import annotations

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


def resolve_debug_request(headers: Mapping[str, str] | None = None) -> bool:
    """Return True for full session persistence; False for slim storage.

    ``x-debug-request: false`` enables slim storage. Default is True when absent.
    """
    value = _header_value(headers or {}, DEBUG_REQUEST_HEADERS)
    if not value:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}

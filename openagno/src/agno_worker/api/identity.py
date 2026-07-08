"""Resolve caller identity from HTTP headers, body, and query string."""
from __future__ import annotations

from typing import Mapping

# Align with aip-hub WebFrameworkUtils (tenant-id) and common gateway headers.
USER_ID_HEADERS = ("user-id", "x-user-id")
TENANT_ID_HEADERS = ("tenant-id", "x-tenant-id")


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
    body_tenant_id: str = "",
    headers: Mapping[str, str] | None = None,
    query_tenant_id: str = "",
) -> str:
    """Prefer gateway-injected headers, then body, then query string."""
    return _first_non_empty(
        _header_value(headers or {}, TENANT_ID_HEADERS),
        body_tenant_id,
        query_tenant_id,
    )

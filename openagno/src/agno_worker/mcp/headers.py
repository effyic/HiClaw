"""Forward conversation identity / ``x-*`` headers onto HTTP MCP connections."""
from __future__ import annotations

from typing import Any

from agno_worker.hooks.protocols import MCPServerConfig

# Outbound HTTP header names (aligned with api/identity inbound primary names).
IDENTITY_HEADER_KEYS: tuple[tuple[str, str], ...] = (
    ("user_id", "user-id"),
    ("tenant_id", "tenant-id"),
    ("session_id", "session-id"),
    ("role_code", "role-code"),
)

MCP_FORWARD_HEADER_PREFIX = "x-"
REQUEST_HEADERS_METADATA_KEY = "request_headers"


def _non_empty(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def collect_identity_mcp_headers(run_context: Any) -> dict[str, str]:
    """Build identity headers from run_context attributes and metadata."""
    metadata = getattr(run_context, "metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    headers: dict[str, str] = {}
    for attr, header_name in IDENTITY_HEADER_KEYS:
        value = _non_empty(getattr(run_context, attr, None)) or _non_empty(
            metadata.get(attr)
        )
        if value:
            headers[header_name] = value
    return headers


def collect_x_request_headers(run_context: Any) -> dict[str, str]:
    """Collect inbound headers whose names start with ``x-`` (case-insensitive)."""
    metadata = getattr(run_context, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return {}
    request_headers = metadata.get(REQUEST_HEADERS_METADATA_KEY) or {}
    if not isinstance(request_headers, dict):
        return {}

    out: dict[str, str] = {}
    for raw_key, raw_value in request_headers.items():
        key = str(raw_key)
        if not key.lower().startswith(MCP_FORWARD_HEADER_PREFIX):
            continue
        value = _non_empty(raw_value)
        if value:
            out[key] = value
    return out


def collect_forwarded_mcp_headers(run_context: Any) -> dict[str, str]:
    """Default MCP forward set: identity fields + ``x-*`` request headers."""
    headers = collect_identity_mcp_headers(run_context)
    headers.update(collect_x_request_headers(run_context))
    return headers


def apply_forwarded_mcp_headers(
    run_context: Any,
    servers: list[MCPServerConfig],
) -> list[MCPServerConfig]:
    """Merge default forward headers into HTTP MCP server configs (url present)."""
    forwarded = collect_forwarded_mcp_headers(run_context)
    if not forwarded:
        return servers

    for server in servers:
        if not server.url:
            continue
        merged = dict(server.headers or {})
        merged.update(forwarded)
        server.headers = merged
    return servers

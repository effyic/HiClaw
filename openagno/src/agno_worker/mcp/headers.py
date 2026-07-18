"""Forward conversation identity / ``x-*`` headers onto HTTP MCP connections."""
from __future__ import annotations

from typing import Any, Callable

from agno_worker.hooks.protocols import MCPServerConfig

# Outbound HTTP header names (aligned with api/identity inbound primary names).
IDENTITY_HEADER_KEYS: tuple[tuple[str, str], ...] = (
    ("user_id", "user-id"),
    ("tenant_id", "tenant-id"),
    ("session_id", "session-id"),
    ("role_code", "role-code"),
)

# Must be refreshed per agent run when MCPTools is pooled (long-lived connection).
# Pool key is name+url only, so tenant / session / user / role must not be frozen
# on the shared connection — they are injected via header_provider each run.
PER_RUN_IDENTITY_HEADER_NAMES: frozenset[str] = frozenset(
    {"tenant-id", "user-id", "session-id", "role-code"}
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


def collect_per_run_mcp_headers(run_context: Any) -> dict[str, str]:
    """Headers that must not be frozen on a pooled MCP connection."""
    forwarded = collect_forwarded_mcp_headers(run_context)
    out: dict[str, str] = {}
    for key, value in forwarded.items():
        lower = key.lower()
        if lower in PER_RUN_IDENTITY_HEADER_NAMES or lower.startswith(
            MCP_FORWARD_HEADER_PREFIX
        ):
            out[key] = value
    return out


def split_static_and_per_run_headers(
    headers: dict[str, str] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Split MCP server headers into connection-static vs per-run identity.

    Only conversation identity headers are per-run. Auth headers such as
    ``X-API-Key`` stay static even though they start with ``X-`` / ``x-``.
    """
    static: dict[str, str] = {}
    per_run: dict[str, str] = {}
    for key, raw in (headers or {}).items():
        value = _non_empty(raw)
        if not value:
            continue
        lower = str(key).lower()
        if lower in PER_RUN_IDENTITY_HEADER_NAMES:
            per_run[str(key)] = value
        else:
            static[str(key)] = value
    return static, per_run


def make_mcp_header_provider(
    fallback: dict[str, str] | None = None,
) -> Callable[..., dict[str, str]]:
    """Agno ``header_provider``: prefer run_context identity, else build-time fallback.

    Pooled MCPTools keep one TCP/session for tool discovery, but Agno creates a
    per-run MCP session when ``header_provider`` is set so ``session-id`` /
    ``user-id`` stay correct for tools like ``mec_create_emr_case``.
    """
    fallback_headers = dict(fallback or {})

    def header_provider(
        run_context: Any = None,
        agent: Any = None,
        team: Any = None,
    ) -> dict[str, str]:
        del agent, team
        if run_context is not None:
            dynamic = collect_per_run_mcp_headers(run_context)
            if dynamic:
                return dynamic
        return dict(fallback_headers)

    return header_provider


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

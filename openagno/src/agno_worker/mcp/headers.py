"""Forward conversation identity / ``x-*`` headers onto HTTP MCP connections.

Outbound headers for a pooled MCPTools instance are always:

* **base** — current agent ``mcp_config`` headers (and any pre-merge from
  ``apply_forwarded_mcp_headers`` / ``mcp_headers_hook``)
* **request** — identity (``tenant-id`` / ``user-id`` / ``session-id`` /
  ``role-code``) plus inbound ``x-*``

Pool key stays ``(name, url)``; base headers are rebound on every borrow so
agent-scoped values such as ``campus-id`` are not frozen from the first creator.
"""
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

# Request-side identity names (also stripped from frozen server_params).
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


def normalize_header_map(headers: dict[str, Any] | None) -> dict[str, str]:
    """Copy non-empty header entries as ``str → str``."""
    out: dict[str, str] = {}
    for key, raw in (headers or {}).items():
        value = _non_empty(raw)
        if value:
            out[str(key)] = value
    return out


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
    """Headers taken from the current request (identity + ``x-*``)."""
    return collect_forwarded_mcp_headers(run_context)


def split_static_and_per_run_headers(
    headers: dict[str, str] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Split headers into non-identity vs identity names.

    Kept for callers/tests. Loader no longer freezes the static half on the
    pooled connection — all outbound headers go through ``header_provider``.
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
    base: dict[str, str] | None = None,
) -> Callable[..., dict[str, str]]:
    """Agno ``header_provider``: DB/base headers + request overlay (request wins).

    ``base`` should be the current agent's MCP headers (typically already
    including any finalize-time forward merge). On each call with
    ``run_context``, identity / ``x-*`` from the request override same-named
    base keys. Without ``run_context`` (tool discovery), returns ``base`` only.
    """
    base_headers = normalize_header_map(base)

    def header_provider(
        run_context: Any = None,
        agent: Any = None,
        team: Any = None,
    ) -> dict[str, str]:
        del agent, team
        merged = dict(base_headers)
        if run_context is not None:
            merged.update(collect_forwarded_mcp_headers(run_context))
        return merged

    return header_provider


def bind_mcp_tool_headers(tool: Any, server: MCPServerConfig) -> None:
    """Rebind pooled MCPTools to the current server's headers.

    Clears ``server_params.headers`` so agent-scoped values (e.g. ``campus-id``)
    cannot leak from the first pool creator. All outbound headers are supplied
    by ``header_provider`` = current ``server.headers`` ∪ request forward set.
    """
    if not server.url:
        return

    base = normalize_header_map(server.headers)
    params = getattr(tool, "server_params", None)
    if params is not None and hasattr(params, "headers"):
        # Dataclass instance — replace map so prior campus-id / API keys drop.
        params.headers = {}
    tool.header_provider = make_mcp_header_provider(base)


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

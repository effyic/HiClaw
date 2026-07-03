"""Nacos MCP discovery and mcporter mapping."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from hermes_worker.nacos.client import NacosAPIError, NacosClient
from hermes_worker.nacos.config import NacosConfig

logger = logging.getLogger(__name__)


async def get_mcp_server(
    client: NacosClient,
    name: str,
    *,
    version: Optional[str] = None,
) -> dict[str, Any]:
    params: dict[str, str] = {"mcpName": name}
    if version:
        params["version"] = version
    try:
        data = await client.get_json(
            "/nacos/v3/client/ai/mcp",
            params,
            operation=f"get mcp {name}",
        )
    except NacosAPIError as exc:
        # Nacos 3.2 admin console registers MCP via admin API only.
        if exc.status != 404:
            raise
        data = await client.get_json(
            "/nacos/v3/admin/ai/mcp",
            params,
            operation=f"get mcp {name} (admin)",
        )
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected MCP response for {name}")
    return data


def _resolve_transport(mcp_detail: dict[str, Any], meta: dict[str, Any]) -> str:
    explicit = str(meta.get("transport") or "").strip()
    if explicit:
        return explicit
    proto = str(
        mcp_detail.get("frontProtocol") or mcp_detail.get("protocol") or "http"
    ).lower()
    if "streamable" in proto:
        return "streamable-http"
    if proto in ("sse", "http", "stdio"):
        return proto
    return "http"


async def list_mcp_servers(
    client: NacosClient,
    *,
    name: str = "",
    page_no: int = 1,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    params = {
        "pageNo": str(page_no),
        "pageSize": str(page_size),
        "search": "blur" if name else "accurate",
    }
    if name:
        params["mcpName"] = name
    data = await client.get_json(
        "/nacos/v3/admin/ai/mcp/list",
        params,
        operation="list mcp servers",
    )
    if not data:
        return []
    if isinstance(data, list):
        return data
    return list(data.get("pageItems") or data.get("mcpServers") or [])


def mcp_to_mcporter_entry(
    mcp_detail: dict[str, Any],
    config: NacosConfig,
) -> Optional[tuple[str, dict[str, Any]]]:
    """Map Nacos MCP metadata to one mcporter ``mcpServers`` entry."""
    name = (
        mcp_detail.get("mcpName")
        or mcp_detail.get("name")
        or mcp_detail.get("serverName")
        or ""
    )
    name = str(name).strip()
    if not name:
        return None

    meta = mcp_detail.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    higress_route = meta.get("higressRoute") or meta.get("higress_route") or name
    transport = _resolve_transport(mcp_detail, meta)

    gateway = config.ai_gateway_url
    gateway_key = config.worker_gateway_key
    if not gateway or not gateway_key:
        logger.warning("MCP %s: missing HICLAW_AI_GATEWAY_URL or gateway key", name)
        return None

    url = f"{gateway}/mcp-servers/{higress_route}/mcp"
    entry: dict[str, Any] = {
        "url": url,
        "transport": transport,
        "headers": {"Authorization": f"Bearer {gateway_key}"},
    }
    return name, entry


def build_mcporter_config(
    entries: dict[str, dict[str, Any]],
    platform_entries: Optional[dict[str, dict[str, Any]]] = None,
    *,
    hybrid: bool = True,
) -> dict[str, Any]:
    """Merge platform and self-managed MCP entries (Nacos wins on conflict in hybrid)."""
    merged: dict[str, dict[str, Any]] = {}
    if platform_entries:
        merged.update(platform_entries)
    if hybrid:
        merged.update(entries)
    else:
        merged = entries or merged
    return {"mcpServers": merged}


def read_platform_mcporter(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        servers = data.get("mcpServers") or {}
        return dict(servers) if isinstance(servers, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}

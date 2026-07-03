"""A2A AgentCard registration and heartbeat for Nacos."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from hermes_worker.nacos.client import NacosAPIError, NacosClient
from hermes_worker.nacos.config import NacosConfig

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Register Hermes Worker as an A2A AgentCard and keep it alive."""

    def __init__(
        self,
        client: NacosClient,
        config: NacosConfig,
        control_url: str,
        capabilities_provider: Callable[[], dict[str, Any]],
    ) -> None:
        self._client = client
        self._config = config
        self._control_url = control_url
        self._capabilities_provider = capabilities_provider
        self._registered = False
        self._version = "1.0.0"
        self._task: Optional[asyncio.Task] = None
        self._backoff = 1.0

    @property
    def online(self) -> bool:
        return self._registered

    def build_agent_card(self) -> dict[str, Any]:
        caps = self._capabilities_provider()
        skills = [
            {
                "id": name,
                "name": name,
                "description": info.get("description", f"Skill {name}"),
                "tags": ["hermes", "hiclaw"],
            }
            for name, info in caps.get("skills", {}).items()
        ]
        return {
            "protocolVersion": "0.3.0",
            "name": self._config.agent_card_name,
            "description": (
                f"Hermes Worker {self._config.worker_name} "
                f"(runtime=hermes, team={caps.get('team', '')})"
            ),
            "url": self._control_url,
            "version": self._version,
            "preferredTransport": "HTTP+JSON",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": skills,
            "metadata": {
                "runtime": "hermes",
                "workerName": self._config.worker_name,
                "controlUrl": self._control_url,
                "mcp": list(caps.get("mcp", {}).keys()),
            },
        }

    async def register(self) -> None:
        card = self.build_agent_card()
        card_json = json.dumps(card, ensure_ascii=False)
        await self._release_agent_card(card_json)
        host, port = _parse_control_endpoint(self._control_url)
        await self._register_endpoint_optional(host, port)
        self._registered = True
        self._backoff = 1.0
        logger.info("AgentCard registered: %s", self._config.agent_card_name)

    async def _release_agent_card(self, card_json: str) -> None:
        """Publish AgentCard (URL registration mode).

        Nacos 3.x HTTP API accepts a single ``POST /nacos/v3/admin/ai/a2a`` with
        the full AgentCard JSON (including ``url``).  Re-registration returns
        HTTP 409 when the name already exists — treat that as success for
        heartbeat refreshes.
        """
        try:
            await self._client.post_form(
                "/nacos/v3/admin/ai/a2a",
                {"agentCard": card_json},
                operation="register agent card",
            )
        except NacosAPIError as exc:
            if exc.status == 409:
                logger.debug(
                    "AgentCard already exists, treating as registered: %s",
                    self._config.agent_card_name,
                )
                return
            raise

    async def _register_endpoint_optional(self, host: str, port: int) -> None:
        """Register runtime endpoint when the server exposes the Java SDK API.

        Nacos 3.2+ URL-mode deployments often omit ``/a2a/endpoint``; the
        control URL in AgentCard is sufficient for discovery.
        """
        try:
            await self._client.post_form(
                "/nacos/v3/admin/ai/a2a/endpoint",
                {
                    "agentName": self._config.agent_card_name,
                    "version": self._version,
                    "address": host,
                    "port": str(port),
                    "transport": "HTTP+JSON",
                },
                operation="register agent endpoint",
            )
        except NacosAPIError as exc:
            if exc.status == 404:
                logger.debug(
                    "Nacos A2A endpoint API unavailable; using URL registration "
                    "for %s",
                    self._config.agent_card_name,
                )
                return
            raise

    async def deregister(self) -> None:
        try:
            await self._client.request(
                "DELETE",
                "/nacos/v3/admin/ai/a2a",
                params={
                    "namespaceId": self._config.namespace,
                    "agentName": self._config.agent_card_name,
                    "version": self._version,
                },
                operation="deregister agent card",
            )
        except NacosAPIError as exc:
            logger.debug("Agent card deregister: %s", exc)
        await self._deregister_endpoint_optional()
        self._registered = False

    async def _deregister_endpoint_optional(self) -> None:
        try:
            await self._client.request(
                "DELETE",
                "/nacos/v3/admin/ai/a2a/endpoint",
                params={
                    "namespaceId": self._config.namespace,
                    "agentName": self._config.agent_card_name,
                    "version": self._version,
                },
                operation="deregister agent endpoint",
            )
        except NacosAPIError as exc:
            if exc.status == 404:
                return
            logger.debug("Agent endpoint deregister: %s", exc)

    async def refresh_capabilities(self) -> None:
        if not self._config.agent_register:
            return
        try:
            await self.register()
        except Exception as exc:
            logger.warning("AgentCard refresh failed: %s", exc)

    def start_heartbeat(self) -> None:
        if self._task or not self._config.agent_register:
            return
        self._task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.deregister()

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await self.register()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "AgentCard heartbeat failed (retry in %.0fs): %s",
                    self._backoff,
                    exc,
                )
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 300)
                continue
            await asyncio.sleep(self._config.heartbeat_interval)


def _parse_control_endpoint(control_url: str) -> tuple[str, int]:
    parsed = urlparse(control_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8088
    if host in ("0.0.0.0", ""):
        host = _detect_reachable_host()
    return host, port


def _detect_reachable_host() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"

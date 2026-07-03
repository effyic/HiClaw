"""Poll Nacos and local workspace for capability changes."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional
from hermes_worker.nacos.client import NacosClient
from hermes_worker.nacos.config import NacosConfig
from hermes_worker.nacos import mcp as mcp_mod
from hermes_worker.nacos import skill as skill_mod

logger = logging.getLogger(__name__)


class CapabilityWatcher:
    """F4: detect Nacos skill/MCP version drift and apply updates."""

    def __init__(
        self,
        client: NacosClient,
        config: NacosConfig,
        *,
        get_state: Callable[[], dict],
        on_skill_update: Callable[[str, str, Optional[str]], Awaitable[None]],
        on_mcp_update: Callable[[str], Awaitable[None]],
        on_prompt_change: Callable[[], Awaitable[None]],
        prompt_hash_check: Callable[[], bool],
    ) -> None:
        self._client = client
        self._config = config
        self._get_state = get_state
        self._on_skill_update = on_skill_update
        self._on_mcp_update = on_mcp_update
        self._on_prompt_change = on_prompt_change
        self._prompt_hash_check = prompt_hash_check
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task or not self._config.watch_enabled:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def tick(self) -> None:
        await self._check_skills()
        await self._check_mcp()
        if self._prompt_hash_check():
            await self._on_prompt_change()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("CapabilityWatcher tick error: %s", exc)
            await asyncio.sleep(self._config.watch_interval)

    async def _check_skills(self) -> None:
        state = self._get_state()
        for name, info in state.get("skills", {}).items():
            if info.get("source") != "nacos":
                continue
            meta = await skill_mod.get_skill_meta(self._client, name)
            if not meta:
                continue
            remote_ver = str(meta.get("version") or "")
            local_ver = str(info.get("version") or "")
            if remote_ver and remote_ver != local_ver:
                label = info.get("label")
                await self._on_skill_update(name, remote_ver, label)

    async def _check_mcp(self) -> None:
        state = self._get_state()
        for name, info in state.get("mcp", {}).items():
            if info.get("source") != "nacos":
                continue
            try:
                detail = await mcp_mod.get_mcp_server(self._client, name)
            except Exception as exc:
                logger.debug("MCP watch %s: %s", name, exc)
                continue
            remote_ver = str(detail.get("version") or "")
            local_ver = str(info.get("version") or "")
            if remote_ver and remote_ver != local_ver:
                await self._on_mcp_update(name)

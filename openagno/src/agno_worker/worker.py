"""Agno Worker — reads controller-injected AgentSpec, serves HTTP chat API."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel

from agno_worker.agentspec.loader import directory_fingerprint, load_agentspec_from_dir
from agno_worker.api.server import AgnoAPIServer
from agno_worker.config import WorkerConfig
from agno_worker.runtime.engine import AgnoRuntime

console = Console()
logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self._runtime: Optional[AgnoRuntime] = None
        self._api: Optional[AgnoAPIServer] = None
        self._api_task: Optional[asyncio.Task] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._spec_fingerprint = ""
        self._stopping = False

    async def run(self) -> None:
        if not await self.start():
            return
        try:
            while not self._stopping:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def start(self) -> bool:
        console.print(
            Panel.fit(
                f"[bold green]Agno Worker[/bold green]\n"
                f"Worker: [cyan]{self.config.worker_name}[/cyan]\n"
                f"AgentSpec: [cyan]{self.config.agentspec_dir}[/cyan]\n"
                f"DB: [cyan]{self._mask_db_url()}[/cyan]",
                title="Starting",
            )
        )
        try:
            await self._load_runtime()
        except Exception as exc:
            console.print(f"[red]Failed to load AgentSpec: {exc}[/red]")
            return False

        token = __import__("os").environ.get("AGNO_CONTROL_TOKEN", "")
        self._api = AgnoAPIServer(
            bind=self.config.api_bind,
            port=self.config.api_port,
            token=token,
            chat_handler=self._handle_chat,
            status_handler=self._status,
        )
        self._api_task = asyncio.create_task(self._api.start())
        self._watch_task = asyncio.create_task(self._watch_spec_loop())
        console.print("[green]Agno Worker ready.[/green]")
        return True

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._api:
            await self._api.stop()
        if self._api_task:
            self._api_task.cancel()
            try:
                await self._api_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _load_runtime(self) -> None:
        spec = load_agentspec_from_dir(self.config.agentspec_dir)
        db_url = self.config.db_url or spec.db.url
        runtime = AgnoRuntime(spec, db_url)
        runtime.build()
        self._runtime = runtime
        self._spec_fingerprint = directory_fingerprint(self.config.agentspec_dir)
        logger.info("Runtime loaded: agents=%s fingerprint=%s", list(spec.agents), self._spec_fingerprint)

    async def _watch_spec_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.config.watch_interval)
            try:
                fp = directory_fingerprint(self.config.agentspec_dir)
                if fp and fp != self._spec_fingerprint:
                    logger.info("AgentSpec change detected (%s -> %s)", self._spec_fingerprint, fp)
                    spec = load_agentspec_from_dir(self.config.agentspec_dir)
                    if self._runtime:
                        self._runtime.reload(spec)
                    self._spec_fingerprint = fp
            except Exception as exc:
                logger.warning("AgentSpec watch error: %s", exc)

    def _handle_chat(self, message: str, session_id: str, user_id: str) -> str:
        if not self._runtime:
            raise RuntimeError("runtime not initialized")
        return self._runtime.run(message, session_id=session_id, user_id=user_id)

    def _status(self) -> dict[str, Any]:
        spec = self._runtime.spec if self._runtime else None
        return {
            "worker": self.config.worker_name,
            "runtime": "agno",
            "agentspecDir": str(self.config.agentspec_dir),
            "specFingerprint": self._spec_fingerprint,
            "agents": list(spec.agents.keys()) if spec else [],
            "dbConfigured": bool(self.config.db_url or (spec and spec.db.url)),
        }

    def _mask_db_url(self) -> str:
        url = self.config.db_url
        if "@" in url:
            prefix, rest = url.split("@", 1)
            if "://" in prefix:
                scheme, _creds = prefix.split("://", 1)
                return f"{scheme}://***@{rest}"
        return url

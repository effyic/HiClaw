"""CLI entry point: ``agno-worker``."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

import typer

from agno_worker.config import WorkerConfig
from agno_worker.worker import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main() -> None:
    def _run(
        name: str = typer.Option(
            os.environ.get("HICLAW_WORKER_NAME", ""),
            "--name",
            help="Worker name",
        ),
        api_port: int = typer.Option(
            int(os.environ.get("AGNO_CONTROL_PORT", "8090")),
            "--api-port",
            help="HTTP API port",
        ),
    ) -> None:
        """Start the standalone Agno Worker."""
        if not name:
            raise typer.BadParameter("--name or HICLAW_WORKER_NAME is required")
        config = WorkerConfig.from_env(name)
        config = WorkerConfig(
            worker_name=name,
            agentspec_dir=config.agentspec_dir,
            hooks_dir=config.hooks_dir,
            db_url=config.db_url,
            api_port=api_port,
            api_bind=config.api_bind,
            watch_interval=config.watch_interval,
            enable_agentos=config.enable_agentos,
        )
        worker = Worker(config)

        async def _async_run() -> None:
            loop = asyncio.get_running_loop()

            def _shutdown() -> None:
                asyncio.create_task(worker.stop())

            try:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, _shutdown)
            except NotImplementedError:
                pass
            await worker.run()

        try:
            asyncio.run(_async_run())
        except KeyboardInterrupt:
            pass

    typer.run(_run)

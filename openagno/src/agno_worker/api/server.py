"""HTTP API for chat, health probes, and optional AgentOS console."""
from __future__ import annotations

import asyncio
import logging
import os
from functools import partial
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
import uvicorn

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str
    session_id: str = Field(..., min_length=1)
    user_id: str = ""
    tenant_id: str = ""


class ChatResponse(BaseModel):
    reply: str
    session_id: str


class AgnoAPIServer:
    def __init__(
        self,
        bind: str,
        port: int,
        token: str,
        chat_handler: Callable[..., str],
        status_handler: Callable[[], dict[str, Any]],
        *,
        worker_name: str = "agno-worker",
        enable_agentos: bool = False,
        runtime: Any = None,
    ) -> None:
        self._bind = bind
        self._port = port
        self._token = token
        self._chat_handler = chat_handler
        self._status_handler = status_handler
        self._worker_name = worker_name
        self._enable_agentos = enable_agentos
        self._runtime = runtime
        self._agent_os: Any = None
        self._base_app: FastAPI | None = None
        self._server: Optional[uvicorn.Server] = None
        self._app = self._build_app()

    @property
    def base_url(self) -> str:
        host = self._bind if self._bind not in ("0.0.0.0", "") else "127.0.0.1"
        return f"http://{host}:{self._port}"

    def resync_agentos(self) -> None:
        if self._agent_os is None or self._base_app is None:
            return
        try:
            if self._runtime is not None:
                self._agent_os.agents = list(self._runtime.agents.values())
                self._agent_os.teams = (
                    [self._runtime.team] if self._runtime.team else None
                )
                self._agent_os.workflows = (
                    [self._runtime.workflow] if self._runtime.workflow else None
                )
            # Must resync from base_app so /health /status /v1/chat routes stay mounted.
            self._agent_os.resync(self._base_app)
        except Exception as exc:
            logger.warning("AgentOS resync failed: %s", exc)

    def _build_app(self) -> FastAPI:
        self._base_app = self._build_base_app()
        if not self._enable_agentos or self._runtime is None:
            return self._base_app
        return self._wrap_with_agentos(self._base_app)

    def _build_base_app(self) -> FastAPI:
        app = FastAPI(title="HiClaw Agno Worker", version="0.1.0")

        async def _auth(authorization: Optional[str] = Header(None)) -> None:
            if not self._token:
                return
            expected = f"Bearer {self._token}"
            if authorization != expected:
                raise HTTPException(status_code=401, detail="unauthorized")

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/status")
        async def status(_: None = Depends(_auth)) -> dict[str, Any]:
            return self._status_handler()

        @app.post("/v1/chat", response_model=ChatResponse)
        async def chat(req: ChatRequest, _: None = Depends(_auth)) -> ChatResponse:
            try:
                loop = asyncio.get_running_loop()
                reply = await loop.run_in_executor(
                    None,
                    partial(
                        self._chat_handler,
                        req.message,
                        req.session_id,
                        req.user_id,
                        req.tenant_id,
                    ),
                )
            except Exception as exc:
                from agno_worker.hooks.errors import HookExecutionError, HookLoadError

                if isinstance(exc, (HookLoadError, HookExecutionError)):
                    logger.error("Hook error during chat: %s", exc)
                    raise HTTPException(status_code=500, detail=str(exc)) from exc
                logger.exception("Unhandled error during chat")
                raise HTTPException(status_code=500, detail="Internal server error") from exc
            return ChatResponse(reply=reply, session_id=req.session_id)

        @app.post("/agent/reregister")
        async def reregister(_: None = Depends(_auth)) -> dict[str, str]:
            self.resync_agentos()
            return {"status": "ok"}

        return app

    def _wrap_with_agentos(self, base_app: FastAPI) -> FastAPI:
        from agno.os import AgentOS

        runtime = self._runtime
        agents = list(runtime.agents.values())
        teams = [runtime.team] if runtime.team else None
        workflows = [runtime.workflow] if runtime.workflow else None
        dev_mode = os.environ.get("RUNTIME_ENV", "prd").lower() == "dev"

        self._agent_os = AgentOS(
            name=self._worker_name,
            agents=agents,
            teams=teams,
            workflows=workflows,
            db=runtime.db,
            base_app=base_app,
            on_route_conflict="preserve_base_app",
            authorization=not dev_mode,
        )
        app = self._agent_os.get_app()
        logger.info(
            "AgentOS console enabled (dev_mode=%s). Connect UI at https://os.agno.com",
            dev_mode,
        )
        return app

    async def start(self) -> None:
        config = uvicorn.Config(
            self._app,
            host=self._bind,
            port=self._port,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        await self._server.serve()

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True

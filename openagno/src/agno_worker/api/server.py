"""HTTP API for chat and health probes."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
import uvicorn

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str
    session_id: str = Field(..., min_length=1)
    user_id: str = ""


class ChatResponse(BaseModel):
    reply: str
    session_id: str


class AgnoAPIServer:
    def __init__(
        self,
        bind: str,
        port: int,
        token: str,
        chat_handler: Callable[[str, str, str], str],
        status_handler: Callable[[], dict[str, Any]],
    ) -> None:
        self._bind = bind
        self._port = port
        self._token = token
        self._chat_handler = chat_handler
        self._status_handler = status_handler
        self._server: Optional[uvicorn.Server] = None
        self._app = self._build_app()

    @property
    def base_url(self) -> str:
        host = self._bind if self._bind not in ("0.0.0.0", "") else "127.0.0.1"
        return f"http://{host}:{self._port}"

    def _build_app(self) -> FastAPI:
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
            reply = self._chat_handler(req.message, req.session_id, req.user_id)
            return ChatResponse(reply=reply, session_id=req.session_id)

        @app.post("/agent/reregister")
        async def reregister(_: None = Depends(_auth)) -> dict[str, str]:
            return {"status": "ok"}

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

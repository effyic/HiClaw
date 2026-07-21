"""HTTP API for chat, health probes, and optional AgentOS console."""
from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send
import uvicorn

from agno_worker.api.identity import (
    resolve_enable_thinking,
    resolve_role_code,
    resolve_session_id,
    resolve_tenant_id,
    resolve_user_id,
)
from agno_worker.hooks.filters import RequestRejectedError
from agno_worker.hooks.protocols import UserContext

logger = logging.getLogger(__name__)

CHAT_API_PREFIX = "/effyic"


class SseStreamingResponse(StreamingResponse):
    """SSE response that avoids Starlette's collapsing task-group path.

    Uvicorn HTTP still advertises ASGI http ``spec_version`` ``2.3``, so
    ``StreamingResponse`` runs the body stream and disconnect listener inside
    ``create_collapsing_task_group``. With anyio >= 4.14 (per-task CancelScope
    on TaskHandle), client abort then raises::

        RuntimeError: Attempted to exit a cancel scope that isn't the current
        tasks's current cancel scope

    Forcing ``spec_version`` ``2.4`` selects Starlette's single-task stream
    path (no parallel cancel scopes). Pair with ``anyio<4.14`` in deps.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket":
            patched = dict(scope)
            asgi = dict(patched.get("asgi") or {})
            asgi["spec_version"] = "2.4"
            patched["asgi"] = asgi
            scope = patched
        await super().__call__(scope, receive, send)


def _cors_allow_origins() -> list[str]:
    """Origins for browser-based static test pages calling chat API directly."""
    raw = os.environ.get("CORS_ORIGIN_LIST") or os.environ.get("AGNO_CORS_ORIGINS", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            return [part.strip() for part in raw.split(",") if part.strip()]
    if os.environ.get("RUNTIME_ENV", "prd").lower() == "dev":
        return ["*"]
    return []


class ChatRequest(BaseModel):
    message: str
    user_id: str = ""
    enable_thinking: Optional[bool] = Field(
        default=None,
        description=(
            "Per-request thinking/reasoning toggle (body/query). "
            "Highest priority over x-debug-request. Omitted → follow "
            "x-debug-request, else off."
        ),
    )
    output_schema: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional per-run structured output schema. Pass a plain JSON Schema "
            "object ({type:object, properties:...}) or a provider json_schema "
            "envelope. Converted to Pydantic when possible and passed to "
            "Agent.arun(output_schema=...)."
        ),
    )
    use_json_mode: Optional[bool] = Field(
        default=None,
        description=(
            "When output_schema is set: force JSON mode (schema via prompt + "
            "json_object). Default true if omitted — required for providers "
            "(e.g. DashScope Qwen Responses) that claim native structured "
            "output but do not enforce it. Set false to try native only."
        ),
    )


class ChatResponse(BaseModel):
    """Sync chat payload — field names align with stream ``RunContent`` data."""

    content: str = Field(description="Assistant text (same meaning as stream RunContent.content)")
    session_id: str


ChatStreamHandler = Callable[..., AsyncIterator[dict[str, Any]]]


def _format_sse(payload: dict[str, Any]) -> str:
    """Agno AgentOS SSE: event line + dual-field JSON body (omit nulls)."""
    event_type = str(payload.get("event") or "message")
    body = {k: v for k, v in payload.items() if v is not None}
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
    )


def _truthy_query(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


class AgnoAPIServer:
    def __init__(
        self,
        bind: str,
        port: int,
        token: str,
        chat_handler: Callable[..., tuple[str, str]],
        status_handler: Callable[[], dict[str, Any]],
        *,
        chat_handler_async: Callable[..., Awaitable[tuple[str, str]]] | None = None,
        chat_stream_handler_async: ChatStreamHandler | None = None,
        worker_name: str = "agno-worker",
        enable_agentos: bool = False,
        enable_session_api: bool = True,
        runtime: Any = None,
    ) -> None:
        self._bind = bind
        self._port = port
        self._token = token
        self._chat_handler = chat_handler
        self._chat_handler_async = (
            chat_handler_async
            if chat_handler_async is not None
            else self._default_async_chat_handler(chat_handler)
        )
        self._chat_stream_handler_async = chat_stream_handler_async
        self._status_handler = status_handler
        self._worker_name = worker_name
        self._enable_agentos = enable_agentos
        self._enable_session_api = enable_session_api
        self._runtime = runtime
        self._agent_os: Any = None
        self._base_app: FastAPI | None = None
        self._server: Optional[uvicorn.Server] = None
        self._app = self._build_app()

    @property
    def base_url(self) -> str:
        host = self._bind if self._bind not in ("0.0.0.0", "") else "127.0.0.1"
        return f"http://{host}:{self._port}"

    def _default_async_chat_handler(
        self,
        chat_handler: Callable[..., tuple[str, str]],
    ) -> Callable[..., Awaitable[tuple[str, str]]]:
        import asyncio
        from functools import partial

        async def _handler(
            message: str,
            session_id: str,
            user_id: str,
            tenant_id: str = "",
        ) -> tuple[str, str]:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                partial(chat_handler, message, session_id, user_id, tenant_id),
            )

        return _handler

    def _build_user_context(
        self,
        request: Request,
        *,
        user_id: str,
        tenant_id: str,
        session_id: str,
        role_code: str = "",
        enable_thinking: bool = False,
        output_schema: Optional[dict[str, Any]] = None,
        use_json_mode: Optional[bool] = None,
    ) -> UserContext:
        extra: dict[str, Any] = {"enable_thinking": enable_thinking}
        if output_schema is not None:
            extra["output_schema"] = output_schema
        if use_json_mode is not None:
            extra["use_json_mode"] = use_json_mode
        return UserContext(
            user_id=user_id,
            tenant_id=tenant_id,
            session_id=session_id,
            role_code=role_code,
            headers={str(k): str(v) for k, v in request.headers.items()},
            extra=extra,
        )

    def _handle_api_error(self, exc: Exception) -> HTTPException:
        from agno_worker.hooks.errors import HookExecutionError, HookLoadError
        from agno_worker.moderation.errors import SensitivePolicyUnavailableError

        if isinstance(exc, SensitivePolicyUnavailableError):
            # 无有效策略快照且 fail-closed：不调用 LLM，返回 503
            logger.error("Sensitive content policy unavailable (fail-closed)")
            return HTTPException(
                status_code=503, detail="sensitive_policy_unavailable"
            )
        if self._is_sensitive_decision_error(exc):
            # BLOCK_REQUEST：响应不含敏感词与用户原文
            logger.warning("Request blocked by sensitive content policy")
            return HTTPException(
                status_code=422, detail="sensitive_content_blocked"
            )
        if isinstance(exc, RequestRejectedError):
            logger.warning("Request rejected by pre-filter: %s", exc.reason)
            return HTTPException(status_code=403, detail=exc.reason)
        if isinstance(exc, (HookLoadError, HookExecutionError)):
            logger.error("Hook error during chat: %s", exc)
            return HTTPException(status_code=500, detail=str(exc))
        logger.exception("Unhandled error during chat")
        return HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def _is_sensitive_decision_error(exc: Exception) -> bool:
        """判断是否敏感内容决策异常（延迟导入避免 agno 硬依赖）。"""
        try:
            from agno_worker.moderation.guardrail import SensitiveContentDecisionError
        except Exception:
            return False
        return isinstance(exc, SensitiveContentDecisionError)

    def resync_agentos(self) -> None:
        if self._agent_os is None or self._base_app is None:
            return
        try:
            if self._runtime is not None:
                self._agent_os.agents = list(self._runtime.agents.values())
            # Must resync from base_app so /health /status / chat routes stay mounted.
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
        origins = _cors_allow_origins()
        if origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_credentials=False,
                allow_methods=["*"],
                allow_headers=["*"],
            )

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

        @app.post(f"{CHAT_API_PREFIX}/v1/chat", response_model=ChatResponse)
        async def chat(
            req: ChatRequest,
            request: Request,
            _: None = Depends(_auth),
        ) -> ChatResponse:
            user_id = resolve_user_id(
                body_user_id=req.user_id,
                headers=request.headers,
                query_user_id=request.query_params.get("user_id", ""),
            )
            tenant_id = resolve_tenant_id(
                headers=request.headers,
                query_tenant_id=request.query_params.get("tenant_id", ""),
            )
            session_id = resolve_session_id(
                headers=request.headers,
                query_session_id=request.query_params.get("session_id", ""),
            )
            role_code = resolve_role_code(
                headers=request.headers,
                query_role_code=request.query_params.get("role_code", ""),
            )
            enable_thinking = resolve_enable_thinking(
                body_enable_thinking=req.enable_thinking,
                headers=request.headers,
                query_enable_thinking=request.query_params.get("enable_thinking", ""),
            )
            user_context = self._build_user_context(
                request,
                user_id=user_id,
                tenant_id=tenant_id,
                session_id=session_id,
                role_code=role_code,
                enable_thinking=enable_thinking,
                output_schema=req.output_schema,
                use_json_mode=req.use_json_mode,
            )
            try:
                reply, resolved_session_id = await self._chat_handler_async(
                    req.message,
                    session_id,
                    user_id,
                    tenant_id,
                    user_context=user_context,
                )
            except Exception as exc:
                raise self._handle_api_error(exc) from exc
            return ChatResponse(content=reply, session_id=resolved_session_id)

        @app.post(f"{CHAT_API_PREFIX}/v1/chat/stream")
        async def chat_stream(
            req: ChatRequest,
            request: Request,
            _: None = Depends(_auth),
        ) -> SseStreamingResponse:
            if self._chat_stream_handler_async is None:
                raise HTTPException(status_code=501, detail="streaming not configured")

            user_id = resolve_user_id(
                body_user_id=req.user_id,
                headers=request.headers,
                query_user_id=request.query_params.get("user_id", ""),
            )
            tenant_id = resolve_tenant_id(
                headers=request.headers,
                query_tenant_id=request.query_params.get("tenant_id", ""),
            )
            session_id = resolve_session_id(
                headers=request.headers,
                query_session_id=request.query_params.get("session_id", ""),
            )
            role_code = resolve_role_code(
                headers=request.headers,
                query_role_code=request.query_params.get("role_code", ""),
            )
            enable_thinking = resolve_enable_thinking(
                body_enable_thinking=req.enable_thinking,
                headers=request.headers,
                query_enable_thinking=request.query_params.get("enable_thinking", ""),
            )
            user_context = self._build_user_context(
                request,
                user_id=user_id,
                tenant_id=tenant_id,
                session_id=session_id,
                role_code=role_code,
                enable_thinking=enable_thinking,
                output_schema=req.output_schema,
                use_json_mode=req.use_json_mode,
            )
            stream_events = _truthy_query(
                request.query_params.get("stream_events", "")
            )

            async def event_generator() -> AsyncIterator[str]:
                try:
                    async for chunk in self._chat_stream_handler_async(
                        req.message,
                        session_id,
                        user_id,
                        tenant_id,
                        user_context=user_context,
                        stream_events=stream_events,
                    ):
                        yield _format_sse(chunk)
                except Exception as exc:
                    http_exc = self._handle_api_error(exc)
                    yield _format_sse(
                        {"event": "RunError", "content": http_exc.detail}
                    )
                    return

            return SseStreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.post("/agent/reregister")
        async def reregister(_: None = Depends(_auth)) -> dict[str, str]:
            self.resync_agentos()
            return {"status": "ok"}

        if (
            self._enable_session_api
            and self._runtime is not None
            and getattr(self._runtime, "db", None) is not None
        ):
            from agno_worker.api.sessions import mount_effyic_session_routes

            mount_effyic_session_routes(app, self._runtime.db, _auth)

        return app

    def _wrap_with_agentos(self, base_app: FastAPI) -> FastAPI:
        from agno.os import AgentOS

        runtime = self._runtime
        agents = list(runtime.agents.values())
        dev_mode = os.environ.get("RUNTIME_ENV", "prd").lower() == "dev"

        self._agent_os = AgentOS(
            name=self._worker_name,
            agents=agents,
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

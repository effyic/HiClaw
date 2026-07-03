"""HTTP control plane for platform / orchestrator M2M instructions."""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


class ControlServer:
    """Expose ``/v1`` control APIs on ``HERMES_CONTROL_PORT``."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        handlers: dict[str, Callable],
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._handlers = handlers
        self._app = web.Application(middlewares=[self._auth_middleware])
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._register_routes()

    @property
    def base_url(self) -> str:
        bind_host = "127.0.0.1" if self._host == "127.0.0.1" else self._host
        if bind_host == "0.0.0.0":
            bind_host = "127.0.0.1"
        return f"http://{bind_host}:{self._port}"

    @web.middleware
    async def _auth_middleware(
        self, request: web.Request, handler: Callable
    ) -> web.StreamResponse:
        if request.path == "/health":
            return await handler(request)
        if self._token:
            auth = request.headers.get("Authorization", "")
            expected = f"Bearer {self._token}"
            if auth != expected:
                raise web.HTTPUnauthorized(text="invalid control token")
        elif request.remote not in ("127.0.0.1", "::1"):
            raise web.HTTPUnauthorized(text="control token required for remote access")
        return await handler(request)

    def _register_routes(self) -> None:
        self._app.router.add_get("/health", self._health)
        self._app.router.add_get("/v1/health", self._health)
        self._app.router.add_get("/v1/status", self._wrap("status"))
        self._app.router.add_get("/v1/capabilities", self._wrap("capabilities"))
        self._app.router.add_post(
            "/v1/capabilities/skills/install", self._wrap("install_skill")
        )
        self._app.router.add_delete(
            "/v1/capabilities/skills/{name}", self._wrap("uninstall_skill")
        )
        self._app.router.add_post(
            "/v1/capabilities/mcp/mount", self._wrap("mount_mcp")
        )
        self._app.router.add_delete(
            "/v1/capabilities/mcp/{name}", self._wrap("unmount_mcp")
        )
        self._app.router.add_post("/v1/prompt/reload", self._wrap("reload_prompts"))
        self._app.router.add_post(
            "/v1/agent/reregister", self._wrap("reregister_agent")
        )

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    def _wrap(self, name: str) -> Callable:
        async def _route(request: web.Request) -> web.Response:
            handler = self._handlers.get(name)
            if not handler:
                raise web.HTTPNotFound()
            body: dict[str, Any] = {}
            if request.can_read_body:
                try:
                    body = await request.json()
                except json.JSONDecodeError:
                    body = {}
            params = dict(request.match_info)
            try:
                result = await handler(body, params)
                return web.json_response(result or {"ok": True})
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc
            except Exception as exc:
                logger.exception("Control API %s failed", name)
                raise web.HTTPInternalServerError(text=str(exc)) from exc

        return _route

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info("Control server listening on %s:%s", self._host, self._port)

    async def stop(self) -> None:
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

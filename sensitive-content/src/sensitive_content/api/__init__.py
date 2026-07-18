"""FastAPI 应用工厂与鉴权依赖。"""
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from sensitive_content import config
from sensitive_content.store import GLOBAL_TENANT, StoreError

logger = logging.getLogger(__name__)

# 路径参数中表示全局的租户标识
GLOBAL_TENANT_PATH = "global"


def resolve_tenant(tenant_id: str) -> str:
    """路径中的 tenant_id=global 映射为库内的 ''（全局）。"""
    return GLOBAL_TENANT if tenant_id == GLOBAL_TENANT_PATH else tenant_id


def _check_bearer(authorization: str, expected: str, realm: str) -> None:
    if not expected:
        raise HTTPException(
            status_code=503,
            detail={"code": "token_not_configured", "message": f"{realm} token not configured"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip(), expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": f"invalid {realm} token"},
        )


def require_admin(authorization: str = Header("")) -> None:
    """管理 API：Admin Token Bearer 鉴权（平台管理员凭据）。"""
    _check_bearer(authorization, config.admin_token(), "admin")


def require_runtime(authorization: str = Header("")) -> None:
    """内部 API：Runtime Token Bearer 鉴权。"""
    _check_bearer(authorization, config.runtime_token(), "runtime")


def resolve_operator(x_operator: str = Header("")) -> str:
    """操作人：仅信任认证网关注入的 X-Operator；无网关时回退为 Admin Token 主体标识。"""
    return x_operator.strip() or "admin-token"


async def _cleanup_loop() -> None:
    """定期清理超出保留期的命中事件（默认 90 天，每天一次）。"""
    from sensitive_content.store import purge_expired_hit_events

    while True:
        try:
            deleted = await asyncio.to_thread(
                purge_expired_hit_events, config.retention_days()
            )
            if deleted:
                logger.info("purged %d expired hit events", deleted)
        except Exception:
            logger.exception("hit event cleanup failed")
        await asyncio.sleep(config.cleanup_interval_seconds())


def create_app(*, enable_cleanup: bool = True) -> FastAPI:
    """构建 FastAPI 应用（serve 启用清理任务；测试可关闭）。"""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task: asyncio.Task | None = None
        if enable_cleanup:
            task = asyncio.create_task(_cleanup_loop())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    app = FastAPI(title="sensitive-content", lifespan=lifespan)

    @app.exception_handler(StoreError)
    async def _store_error_handler(_: Request, exc: StoreError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message, **exc.extra}},
        )

    from sensitive_content.api.admin import router as admin_router
    from sensitive_content.api.internal import router as internal_router
    from sensitive_content.api.metrics import router as metrics_router

    app.include_router(admin_router)
    app.include_router(metrics_router)
    app.include_router(internal_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app

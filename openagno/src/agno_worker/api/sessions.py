"""Mount Agno AgentOS session routes under /effyic/v1 with worker token auth."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI

logger = logging.getLogger(__name__)

SESSION_API_PREFIX = "/effyic/v1"


def build_session_dbs(db: Any) -> dict[str, list[Any]]:
    """Map runtime PostgresDb to the structure expected by AgentOS session services."""
    db_id = getattr(db, "id", None) or "default"
    return {str(db_id): [db]}


def mount_effyic_session_routes(
    app: FastAPI,
    db: Any,
    auth: Callable[..., Any],
    *,
    prefix: str = SESSION_API_PREFIX,
) -> None:
    """Expose AgentOS session APIs at ``/effyic/v1/sessions*`` using worker Bearer auth."""
    from agno.os.routers.session.session import attach_routes

    session_router = attach_routes(
        router=APIRouter(
            tags=["Sessions"],
            dependencies=[Depends(auth)],
        ),
        dbs=build_session_dbs(db),
    )
    app.include_router(session_router, prefix=prefix)
    logger.info("Session API mounted at %s/sessions", prefix.rstrip("/"))

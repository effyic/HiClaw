"""Mount Agno AgentOS session routes under /effyic/v1 with worker token auth.

Also exposes ``GET /effyic/v1/sessions/{session_id}/conversation`` which returns
pure user/assistant turns from each run's original ``input`` (not the stored
message content that may include ``<additional context>``).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SESSION_API_PREFIX = "/effyic/v1"


class ConversationMessage(BaseModel):
    role: str = Field(..., description="Message role: user or assistant")
    content: str = Field(..., description="Pure message text without injected context")
    run_id: Optional[str] = Field(None, description="Run that produced this turn")
    created_at: Optional[int] = Field(None, description="Unix timestamp of the run")


class ConversationResponse(BaseModel):
    session_id: str
    user_id: Optional[str] = None
    messages: list[ConversationMessage] = Field(default_factory=list)


def build_session_dbs(db: Any) -> dict[str, list[Any]]:
    """Map runtime PostgresDb to the structure expected by AgentOS session services."""
    db_id = getattr(db, "id", None) or "default"
    return {str(db_id): [db]}


def _stringify_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def extract_run_input(run: Any) -> str:
    """Return the original user input for a run (Agno RunInput.input_content)."""
    inp = getattr(run, "input", None)
    if inp is None:
        return ""
    if hasattr(inp, "input_content"):
        return _stringify_content(getattr(inp, "input_content", None))
    if isinstance(inp, dict) and inp.get("input_content") is not None:
        return _stringify_content(inp.get("input_content"))
    if isinstance(inp, str):
        return inp
    return _stringify_content(inp)


def extract_run_assistant_content(run: Any) -> str:
    getter = getattr(run, "get_content_as_string", None)
    if callable(getter):
        try:
            return _stringify_content(getter())
        except Exception:
            pass
    return _stringify_content(getattr(run, "content", None))


def build_conversation_messages(session: Any) -> list[ConversationMessage]:
    """Build pure user/assistant turns from session runs."""
    messages: list[ConversationMessage] = []
    for run in getattr(session, "runs", None) or []:
        run_id = getattr(run, "run_id", None)
        created_at = getattr(run, "created_at", None)
        created_at_int = created_at if isinstance(created_at, int) else None
        run_id_str = str(run_id) if run_id else None

        user_text = extract_run_input(run)
        if user_text:
            messages.append(
                ConversationMessage(
                    role="user",
                    content=user_text,
                    run_id=run_id_str,
                    created_at=created_at_int,
                )
            )

        assistant_text = extract_run_assistant_content(run)
        if assistant_text:
            messages.append(
                ConversationMessage(
                    role="assistant",
                    content=assistant_text,
                    run_id=run_id_str,
                    created_at=created_at_int,
                )
            )
    return messages


async def load_session(db: Any, session_id: str, user_id: Optional[str]) -> Any:
    """Load an Agent/Team/Workflow session by id (auto-detect type)."""
    from agno.db.base import AsyncBaseDb, SessionType
    from agno.db.utils import deserialize_session_by_type, resolve_session_type

    try:
        session_type, raw = await resolve_session_type(db, session_id, None, user_id)
        if session_type is not None and isinstance(raw, dict):
            session = deserialize_session_by_type(raw)
            if session is not None:
                return session
    except Exception as exc:
        logger.debug("resolve_session_type failed for %s: %s", session_id, exc)

    for st in (SessionType.AGENT, SessionType.TEAM, SessionType.WORKFLOW):
        if isinstance(db, AsyncBaseDb):
            session = await db.get_session(
                session_id=session_id, session_type=st, user_id=user_id
            )
        else:
            session = db.get_session(
                session_id=session_id, session_type=st, user_id=user_id
            )
        if session is not None:
            return session
    return None


def mount_effyic_session_routes(
    app: FastAPI,
    db: Any,
    auth: Callable[..., Any],
    *,
    prefix: str = SESSION_API_PREFIX,
) -> None:
    """Expose AgentOS session APIs plus pure conversation at ``/effyic/v1/sessions*``."""
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

    conversation_router = APIRouter(
        tags=["Sessions"],
        dependencies=[Depends(auth)],
    )

    @conversation_router.get(
        "/sessions/{session_id}/conversation",
        response_model=ConversationResponse,
        summary="Get pure conversation turns",
        description=(
            "Return user/assistant dialogue for a session using each run's original "
            "input (``run_input`` / ``RunInput.input_content``) and assistant content. "
            "Unlike AgentOS ``chat_history``, this excludes injected "
            "``<additional context>`` blocks."
        ),
        response_model_exclude_none=True,
    )
    async def get_session_conversation(
        session_id: str = Path(description="Session ID"),
        user_id: Optional[str] = Query(
            default=None, description="Optional user ID scope filter"
        ),
    ) -> ConversationResponse:
        session = await load_session(db, session_id, user_id)
        if session is None:
            raise HTTPException(
                status_code=404, detail=f"Session with id '{session_id}' not found"
            )
        return ConversationResponse(
            session_id=str(getattr(session, "session_id", session_id)),
            user_id=getattr(session, "user_id", None),
            messages=build_conversation_messages(session),
        )

    app.include_router(conversation_router, prefix=prefix)
    logger.info(
        "Conversation API mounted at %s/sessions/{session_id}/conversation",
        prefix.rstrip("/"),
    )

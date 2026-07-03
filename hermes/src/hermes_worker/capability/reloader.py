"""Apply capability file changes without restarting Matrix."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class ReloadScope(Flag):
    SKILLS = auto()
    MCP = auto()
    PROMPTS = auto()
    ALL = SKILLS | MCP | PROMPTS


@dataclass
class RuntimeReloader:
    """Bridge between CapabilityStore mutations and Worker runtime hooks."""

    sync_skills: Callable[[], None]
    copy_mcporter: Callable[[], None]
    rebridge_prompts: Callable[[], Awaitable[None]]
    on_applied: Optional[Callable[[ReloadScope], Awaitable[None]]] = None
    _pending: ReloadScope = field(default=ReloadScope(0), init=False)
    _gateway_reload_supported: bool = False

    async def reload(self, scope: ReloadScope) -> None:
        if scope & ReloadScope.SKILLS:
            self.sync_skills()
            logger.info("Skills reloaded into HERMES_HOME")
        if scope & ReloadScope.MCP:
            self.copy_mcporter()
            logger.info("mcporter config refreshed")
        if scope & ReloadScope.PROMPTS:
            await self.rebridge_prompts()
            logger.info("Prompts re-bridged (SOUL/AGENTS)")
        if self.on_applied:
            await self.on_applied(scope)
        if not self._gateway_reload_supported and (scope & (ReloadScope.SKILLS | ReloadScope.MCP)):
            logger.info(
                "Gateway hot-reload hook unavailable; changes apply on next agent turn"
            )

    def note_pending(self, scope: ReloadScope) -> None:
        self._pending |= scope

    @property
    def pending(self) -> ReloadScope:
        return self._pending

    def status_extra(self) -> dict[str, str]:
        parts = []
        if self._pending & ReloadScope.SKILLS:
            parts.append("skills_pending")
        if self._pending & ReloadScope.MCP:
            parts.append("mcp_pending")
        if self._pending & ReloadScope.PROMPTS:
            parts.append("prompts_pending")
        return {"reload": ",".join(parts) if parts else "idle"}

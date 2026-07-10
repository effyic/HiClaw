"""Per-run lazy skill tools with catalog/instruction/script caching."""
from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

from agno_worker.hooks.protocols import SkillRef
from agno_worker.skills.normalize import normalize_skill_refs

logger = logging.getLogger(__name__)


class SkillProvider(Protocol):
    def resolve_skill_catalog(
        self, run_context: Any, user_requirements: str = ""
    ) -> list[SkillRef]: ...

    def load_skill_instruction(self, skill_name: str, run_context: Any) -> str: ...

    def load_skill_script(
        self,
        skill_name: str,
        script_name: str,
        run_context: Any,
        *,
        execute: bool = False,
    ) -> Any: ...


@dataclass
class _CacheEntry:
    value: list[SkillRef]
    expires_at: float


class SkillCatalogCache:
    def __init__(self, *, max_entries: int = 256, ttl_seconds: float = 300.0) -> None:
        self._max_entries = max(1, max_entries)
        self._ttl_seconds = max(0.001, ttl_seconds)
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()

    def get(self, key: str) -> list[SkillRef] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.monotonic() >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return list(entry.value)

    def set(self, key: str, catalog: list[SkillRef]) -> None:
        self._entries[key] = _CacheEntry(
            value=list(catalog),
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


class _LRUCache:
    def __init__(self, maxsize: int = 128) -> None:
        self._maxsize = max(1, maxsize)
        self._data: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: str, value: str) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


class DynamicSkillsManager:
    """Resolve skill catalogs per run and expose lazy Agno tools."""

    def __init__(self, skill_provider: SkillProvider) -> None:
        self._provider = skill_provider
        ttl = float(os.environ.get("AGNO_SKILL_CATALOG_TTL", "300"))
        catalog_max = int(os.environ.get("AGNO_SKILL_CATALOG_CACHE_SIZE", "256"))
        instr_max = int(os.environ.get("AGNO_SKILL_INSTRUCTION_CACHE_SIZE", "128"))
        script_max = int(os.environ.get("AGNO_SKILL_SCRIPT_CACHE_SIZE", "64"))
        self._catalog_cache = SkillCatalogCache(max_entries=catalog_max, ttl_seconds=ttl)
        self._instruction_cache = _LRUCache(maxsize=instr_max)
        self._script_cache = _LRUCache(maxsize=script_max)

    def clear_caches(self) -> None:
        self._catalog_cache.clear()
        self._instruction_cache.clear()
        self._script_cache.clear()

    def resolve_catalog(
        self,
        run_context: Any,
        user_requirements: str = "",
    ) -> list[SkillRef]:
        cache_key = self._catalog_cache_key(run_context, user_requirements)
        cached = self._catalog_cache.get(cache_key)
        if cached is not None:
            logger.debug("skill catalog cache hit key=%s count=%d", cache_key, len(cached))
            return cached

        catalog = self._provider.resolve_skill_catalog(run_context, user_requirements or "")
        catalog = normalize_skill_refs(catalog)
        self._catalog_cache.set(cache_key, catalog)
        logger.debug("skill catalog loaded key=%s count=%d", cache_key, len(catalog))
        return catalog

    def build_tools(
        self,
        run_context: Any,
        catalog: list[SkillRef],
    ) -> list[Any]:
        if not catalog:
            return []

        try:
            from agno.tools import tool
        except ImportError:
            logger.warning("agno.tools unavailable; skill tools disabled")
            return []

        allowed = {ref.name for ref in catalog}
        manager = self
        provider = self._provider

        @tool(
            name="list_available_skills",
            description="List skill names and short descriptions available for this run.",
        )
        def list_available_skills(run_context: Any = None) -> str:
            ctx = run_context
            deps = getattr(ctx, "dependencies", None) or {} if ctx is not None else {}
            refs = normalize_skill_refs(deps.get("skill_catalog")) or catalog
            payload = [
                {"name": ref.name, "description": ref.description, "scripts": ref.scripts}
                for ref in refs
            ]
            return json.dumps({"skills": payload}, ensure_ascii=False)

        @tool(
            name="get_skill_instructions",
            description="Load full instructions for a skill on demand.",
        )
        def get_skill_instructions(skill_name: str, run_context: Any = None) -> str:
            ctx = run_context
            if skill_name not in allowed:
                return json.dumps(
                    {
                        "error": f"Skill '{skill_name}' not available for this run",
                        "available_skills": sorted(allowed),
                    },
                    ensure_ascii=False,
                )
            cache_key = manager._content_cache_key(ctx, "instr", skill_name)
            cached = manager._instruction_cache.get(cache_key)
            if cached is not None:
                return cached
            text = provider.load_skill_instruction(skill_name, ctx)
            payload = json.dumps(
                {"skill_name": skill_name, "instructions": str(text or "")},
                ensure_ascii=False,
            )
            manager._instruction_cache.set(cache_key, payload)
            return payload

        @tool(
            name="get_skill_script",
            description="Read a skill script on demand (execute=False by default).",
        )
        def get_skill_script(
            skill_name: str,
            script_name: str,
            execute: bool = False,
            run_context: Any = None,
        ) -> str:
            ctx = run_context
            if skill_name not in allowed:
                return json.dumps(
                    {
                        "error": f"Skill '{skill_name}' not available for this run",
                        "available_skills": sorted(allowed),
                    },
                    ensure_ascii=False,
                )
            cache_key = manager._content_cache_key(ctx, f"script:{execute}", skill_name, script_name)
            if not execute:
                cached = manager._script_cache.get(cache_key)
                if cached is not None:
                    return cached
            raw = provider.load_skill_script(
                skill_name,
                script_name,
                ctx,
                execute=execute,
            )
            if isinstance(raw, dict):
                payload = json.dumps(raw, ensure_ascii=False)
            else:
                payload = json.dumps(
                    {"skill_name": skill_name, "script_name": script_name, "content": str(raw)},
                    ensure_ascii=False,
                )
            if not execute:
                manager._script_cache.set(cache_key, payload)
            return payload

        return [list_available_skills, get_skill_instructions, get_skill_script]

    @staticmethod
    def _catalog_cache_key(run_context: Any, user_requirements: str) -> str:
        metadata = getattr(run_context, "metadata", None) or {}
        tenant_id = str(metadata.get("tenant_id") or "")
        session_state = getattr(run_context, "session_state", None) or {}
        role = str(session_state.get("active_role") or session_state.get("role") or "default")
        req = user_requirements.strip()[:256]
        return f"{tenant_id}|{role}|{req}"

    @staticmethod
    def _content_cache_key(run_context: Any, kind: str, skill_name: str, script_name: str = "") -> str:
        metadata = getattr(run_context, "metadata", None) or {}
        tenant_id = str(metadata.get("tenant_id") or "")
        session_id = str(metadata.get("session_id") or "")
        base = f"{tenant_id}|{session_id}|{kind}|{skill_name}"
        if script_name:
            return f"{base}|{script_name}"
        return base

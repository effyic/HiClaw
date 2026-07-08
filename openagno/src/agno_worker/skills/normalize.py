"""Normalize hook skill payloads into lightweight SkillRef objects."""
from __future__ import annotations

from typing import Any

from agno_worker.hooks.protocols import SkillRef


def normalize_skill_refs(raw: Any) -> list[SkillRef]:
    """Convert hook output to SkillRef list without loading heavy skill bodies."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]

    refs: list[SkillRef] = []
    for item in raw:
        ref = _to_skill_ref(item)
        if ref is not None:
            refs.append(ref)
    return refs


def _to_skill_ref(item: Any) -> SkillRef | None:
    if isinstance(item, SkillRef):
        return item
    if isinstance(item, str):
        name = item.strip()
        if not name:
            return None
        return SkillRef(name=name, description="")
    if isinstance(item, dict):
        name = str(item.get("name") or item.get("skill_name") or "").strip()
        if not name:
            return None
        return SkillRef(
            name=name,
            description=str(item.get("description") or ""),
            source_path=str(item.get("source_path") or item.get("path") or ""),
            scripts=[str(s) for s in (item.get("scripts") or []) if str(s).strip()],
            references=[str(r) for r in (item.get("references") or []) if str(r).strip()],
            metadata=dict(item.get("metadata") or {}),
        )
    # Agno Skill dataclass or compatible object
    name = getattr(item, "name", None)
    if not name:
        return None
    return SkillRef(
        name=str(name),
        description=str(getattr(item, "description", "") or ""),
        source_path=str(getattr(item, "source_path", "") or ""),
        scripts=[str(s) for s in (getattr(item, "scripts", None) or [])],
        references=[str(r) for r in (getattr(item, "references", None) or [])],
        metadata=dict(getattr(item, "metadata", None) or {}),
    )


def skill_catalog_summary(catalog: list[SkillRef]) -> str:
    """Build a lightweight prompt hint; does not include full skill instructions."""
    if not catalog:
        return ""
    lines = ["可用 Skills（按需调用 get_skill_instructions 加载详情）:"]
    for ref in catalog:
        desc = ref.description.strip()
        if desc:
            lines.append(f"- {ref.name}: {desc}")
        else:
            lines.append(f"- {ref.name}")
    return "\n".join(lines)

"""Standard skill catalog resolution from filesystem."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from agno_worker.hooks.protocols import SkillRef
from agno_worker.tenant.context import TenantContextResolver

SKILLS_ROOT = Path(os.environ.get("AGNO_SKILLS_DIR", "/etc/hiclaw/skills")).resolve()
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


class TenantSkillCatalog:
    """Scan skill directories and filter by tenant."""

    def __init__(self, resolver: TenantContextResolver | None = None) -> None:
        self._resolver = resolver or TenantContextResolver()

    def resolve_catalog(
        self,
        run_context: Any,
        user_requirements: str = "",
    ) -> list[SkillRef]:
        tenant_id = self._resolver.resolve_tenant_id(run_context)
        refs: list[SkillRef] = []
        if not SKILLS_ROOT.is_dir():
            return refs

        req_lower = (user_requirements or "").lower()
        for skill_dir in sorted(SKILLS_ROOT.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            meta = _read_skill_frontmatter(str(skill_dir))
            if not meta:
                continue
            tenants = meta.get("tenant_ids") or meta.get("tenants") or []
            if tenants and tenant_id not in [str(t) for t in tenants]:
                continue
            name = str(meta.get("name") or skill_dir.name)
            description = str(meta.get("description") or "")
            if req_lower and req_lower not in f"{name} {description}".lower():
                tags = meta.get("tags") or []
                tag_text = " ".join(str(t) for t in tags).lower()
                if req_lower not in tag_text and req_lower not in name.lower():
                    continue
            scripts_dir = skill_dir / "scripts"
            scripts = (
                sorted(p.name for p in scripts_dir.iterdir() if p.is_file())
                if scripts_dir.is_dir()
                else []
            )
            refs.append(
                SkillRef(
                    name=name,
                    description=description,
                    source_path=str(skill_dir),
                    scripts=scripts,
                    metadata={"tenant_ids": tenants},
                )
            )
        return refs

    def load_instruction(self, skill_name: str, run_context: Any) -> str:
        skill_dir = _resolve_skill_dir(skill_name)
        if skill_dir is None:
            return f"Skill '{skill_name}' not found under {SKILLS_ROOT}"
        body = _read_skill_body(skill_dir)
        tenant_id = self._resolver.resolve_tenant_id(run_context)
        return f"[tenant={tenant_id}]\n{body}"

    def load_script(
        self,
        skill_name: str,
        script_name: str,
        run_context: Any,
        *,
        execute: bool = False,
    ) -> Any:
        del run_context
        skill_dir = _resolve_skill_dir(skill_name)
        if skill_dir is None:
            return {"error": f"Skill '{skill_name}' not found"}
        script_path = skill_dir / "scripts" / script_name
        if not script_path.is_file():
            return {"error": f"Script '{script_name}' not found", "skill_name": skill_name}
        if execute:
            return {
                "error": "execute disabled by default",
                "skill_name": skill_name,
                "script_name": script_name,
            }
        return {"content": script_path.read_text(encoding="utf-8")}


def _resolve_skill_dir(skill_name: str) -> Path | None:
    direct = SKILLS_ROOT / skill_name
    if direct.is_dir() and (direct / "SKILL.md").is_file():
        return direct
    if not SKILLS_ROOT.is_dir():
        return None
    for skill_dir in SKILLS_ROOT.iterdir():
        if not skill_dir.is_dir():
            continue
        meta = _read_skill_frontmatter(str(skill_dir))
        if meta and str(meta.get("name") or skill_dir.name) == skill_name:
            return skill_dir
    return None


@lru_cache(maxsize=64)
def _read_skill_frontmatter(skill_dir: str) -> dict[str, Any]:
    path = Path(skill_dir) / "SKILL.md"
    if not path.is_file():
        return {}
    head = path.read_bytes()[:4096].decode("utf-8", errors="replace")
    match = _FRONTMATTER_RE.match(head)
    if not match:
        return {"name": Path(skill_dir).name, "description": ""}
    block = match.group(1)
    meta: dict[str, Any] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in ("tenant_ids", "tenants", "tags"):
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1]
                meta[key] = [item.strip().strip('"').strip("'") for item in inner.split(",") if item.strip()]
            else:
                meta[key] = [value] if value else []
        else:
            meta[key] = value
    meta.setdefault("name", Path(skill_dir).name)
    return meta


def _read_skill_body(skill_dir: Path) -> str:
    path = skill_dir / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match:
        return text[match.end() :].strip()
    return text.strip()

"""capability-state.json persistence and MinIO sync."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_worker.nacos.config import parse_skill_spec

logger = logging.getLogger(__name__)

STATE_FILENAME = "capability-state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


class CapabilityStore:
    """Track self-managed skills, MCP, and prompt hashes."""

    def __init__(
        self,
        workspace_dir: Path,
        hermes_home: Path,
        *,
        push_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.hermes_home = hermes_home
        self.skills_dir = hermes_home / "skills"
        self._push = push_callback
        self._state_path = workspace_dir / STATE_FILENAME
        self._data: dict[str, Any] = {"skills": {}, "mcp": {}, "prompts": {}}

    @property
    def state_path(self) -> Path:
        return self._state_path

    def load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            self._data = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load %s: %s", self._state_path, exc)

    def save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False)
        )
        if self._push:
            self._push()

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def protected_skill_names(self) -> set[str]:
        return {
            name
            for name, info in self._data.get("skills", {}).items()
            if info.get("source") == "nacos"
        }

    def record_skill(
        self,
        name: str,
        *,
        version: str,
        label: Optional[str] = None,
        source: str = "nacos",
    ) -> None:
        entry: dict[str, Any] = {
            "source": source,
            "version": version,
            "installedAt": _now_iso(),
        }
        if label:
            entry["label"] = label
        self._data.setdefault("skills", {})[name] = entry
        self.save()

    def remove_skill(self, name: str) -> None:
        skills = self._data.get("skills", {})
        if name in skills:
            del skills[name]
            self.save()
        skill_dir = self.skills_dir / name
        ws_skill = self.workspace_dir / "skills" / name
        for d in (skill_dir, ws_skill):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)

    def record_mcp(
        self,
        name: str,
        *,
        higress_route: str,
        source: str = "nacos",
        version: str = "",
    ) -> None:
        self._data.setdefault("mcp", {})[name] = {
            "source": source,
            "higressRoute": higress_route,
            "installedAt": _now_iso(),
        }
        if version:
            self._data["mcp"][name]["version"] = version
        self.save()

    def remove_mcp(self, name: str) -> None:
        mcp = self._data.get("mcp", {})
        if name in mcp:
            del mcp[name]
            self.save()

    def update_prompt_hashes(self) -> None:
        prompts = self._data.setdefault("prompts", {})
        for fname in ("SOUL.md", "AGENTS.md"):
            for base in (self.workspace_dir, self.hermes_home):
                p = base / fname
                if p.exists():
                    prompts[fname] = {
                        "hash": _file_hash(p),
                        "updatedAt": _now_iso(),
                    }
                    break
        self.save()

    def bootstrap_specs(self, specs: list[str]) -> list[tuple[str, Optional[str], Optional[str]]]:
        return [parse_skill_spec(s) for s in specs]

    def sync_skill_to_workspace(self, name: str) -> None:
        """Copy hermes_home skill into workspace MinIO mirror path."""
        src = self.skills_dir / name
        dst = self.workspace_dir / "skills" / name
        if not src.is_dir():
            return
        dst.mkdir(parents=True, exist_ok=True)
        for src_file in src.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(src)
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, out)
            if out.suffix == ".sh":
                out.chmod(out.stat().st_mode | 0o111)

    def write_mcporter(
        self,
        config: dict[str, Any],
        platform_path: Path,
        *,
        hybrid: bool = True,
    ) -> Path:
        from hermes_worker.nacos.mcp import build_mcporter_config, read_platform_mcporter

        nacos_entries = {
            k: v
            for k, v in (config.get("mcpServers") or {}).items()
            if k in self._data.get("mcp", {})
        }
        platform = read_platform_mcporter(platform_path) if hybrid else {}
        merged = build_mcporter_config(nacos_entries, platform, hybrid=hybrid)
        out_ws = self.workspace_dir / "config" / "mcporter.json"
        out_ws.parent.mkdir(parents=True, exist_ok=True)
        out_ws.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
        out_home = self.hermes_home / "config" / "mcporter.json"
        out_home.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_ws, out_home)
        return out_ws

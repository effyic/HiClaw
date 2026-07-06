"""Load AgentSpec YAML from controller-mounted ConfigMap directory."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import yaml

from agno_worker.agentspec.schema import AgentSpec, parse_agentspec_yaml

logger = logging.getLogger(__name__)


def load_agentspec_from_dir(spec_dir: Path) -> AgentSpec:
    for candidate in (
        spec_dir / "agentspec.yaml",
        spec_dir / "spec.yaml",
        spec_dir / "config" / "agentspec.yaml",
    ):
        if candidate.is_file():
            return parse_agentspec_yaml(candidate.read_text(encoding="utf-8"))

    manifest = spec_dir / "manifest.json"
    if manifest.is_file():
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "spec" in raw:
            return parse_agentspec_yaml(yaml.dump(raw["spec"]))
        return parse_agentspec_yaml(yaml.dump(raw))

    raise FileNotFoundError(f"No AgentSpec found under {spec_dir}")


def directory_fingerprint(spec_dir: Path) -> str:
    """Hash file names + mtimes for hot-reload detection."""
    h = hashlib.sha256()
    if not spec_dir.is_dir():
        return ""
    for path in sorted(spec_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(spec_dir).as_posix()
        stat = path.stat()
        h.update(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}\n".encode())
    return h.hexdigest()[:16]

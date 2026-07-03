"""Nacos skill discovery and installation."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

from hermes_worker.nacos.client import NacosClient

logger = logging.getLogger(__name__)


def _clean_zip_entry(name: str) -> str:
    if not name or "\\" in name or name.startswith("/") or ".." in name.split("/"):
        raise ValueError(f"unsafe ZIP entry: {name!r}")
    clean = os.path.normpath(name).replace("\\", "/")
    if clean in (".", "..") or clean.startswith("../"):
        raise ValueError(f"path traversal in ZIP entry: {name!r}")
    return clean


def extract_skill_zip(zip_path: Path, output_dir: Path) -> None:
    """Extract a skill ZIP safely into ``output_dir/{skill-name}/``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_abs = output_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for entry in zf.infolist():
            rel = _clean_zip_entry(entry.filename)
            dest = (output_dir / rel).resolve()
            if not str(dest).startswith(str(output_abs)):
                raise ValueError(f"ZIP entry escapes destination: {entry.filename}")
            if entry.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(entry) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if dest.suffix == ".sh":
                dest.chmod(dest.stat().st_mode | 0o111)


async def download_skill(
    client: NacosClient,
    name: str,
    output_dir: Path,
    *,
    version: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    """Download and extract a skill; return detected version string."""
    params: dict[str, str] = {"name": name}
    if version:
        params["version"] = version
    if label:
        params["label"] = label
    data = await client.download(
        "/nacos/v3/client/ai/skills",
        params,
        operation=f"get skill {name}",
    )
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        extract_skill_zip(tmp_path, output_dir / name)
    finally:
        tmp_path.unlink(missing_ok=True)
    meta = await get_skill_meta(client, name)
    ver = version or (meta or {}).get("version") or label or "latest"
    return str(ver)


async def get_skill_meta(
    client: NacosClient, name: str
) -> Optional[dict[str, Any]]:
    """Fetch skill metadata (version, labels) from list API."""
    items = await list_skills(client, name=name, page_size=1)
    for item in items:
        if item.get("name") == name or item.get("skillName") == name:
            return item
    return items[0] if items else None


async def list_skills(
    client: NacosClient,
    *,
    name: str = "",
    page_no: int = 1,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    params = {
        "pageNo": str(page_no),
        "pageSize": str(page_size),
        "search": "blur" if name else "accurate",
    }
    if name:
        params["skillName"] = name
    data = await client.get_json(
        "/nacos/v3/admin/ai/skills/list",
        params,
        operation="list skills",
    )
    if not data:
        return []
    if isinstance(data, list):
        return data
    return list(data.get("pageItems") or data.get("skills") or [])

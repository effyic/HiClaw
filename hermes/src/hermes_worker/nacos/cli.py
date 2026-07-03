"""``hermes nacos`` CLI — skill/MCP discovery without @nacos-group/cli."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import typer

from hermes_worker.nacos.client import NacosClient, NacosAPIError
from hermes_worker.nacos.config import load_nacos_config
from hermes_worker.nacos import skill as skill_mod

logging.basicConfig(level=logging.INFO)
app = typer.Typer(name="nacos", help="Nacos AI Registry operations for Hermes Worker")

# Nacos skillName filter: lowercase letters, digits, hyphens; no leading/trailing hyphen.
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _worker_name() -> str:
    import os

    return os.environ.get("HICLAW_WORKER_NAME", "hermes-worker")


@app.command("find-skills")
def find_skills(
    query: str = typer.Argument(..., help="Search query"),
    max_results: int = typer.Option(6, "--max", help="Max results"),
) -> None:
    """Search skills in Nacos (skills-style listing)."""

    async def _run() -> None:
        cfg = load_nacos_config(_worker_name())
        if not cfg.server_addr:
            typer.echo("error: SKILLS_API_URL must be a nacos:// URI", err=True)
            raise typer.Exit(1)
        client = NacosClient(cfg)
        try:
            patterns = _build_patterns(query)
            seen: set[str] = set()
            scored: list[tuple[int, str, str]] = []
            for pattern in patterns:
                page = 1
                while True:
                    try:
                        items = await skill_mod.list_skills(
                            client, name=pattern, page_no=page, page_size=100
                        )
                    except NacosAPIError:
                        break
                    if not items:
                        break
                    for item in items:
                        name = str(
                            item.get("skillName") or item.get("name") or ""
                        )
                        if not name or name in seen:
                            continue
                        seen.add(name)
                        desc = str(item.get("description") or "")
                        scored.append((_score(query, name, desc), name, desc))
                    if len(items) < 100:
                        break
                    page += 1
            scored.sort(key=lambda x: (-x[0], x[1]))
            typer.echo(f"Registry: nacos://{cfg.server_addr}/{cfg.namespace}\n")
            for i, (pts, name, desc) in enumerate(scored[:max_results], 1):
                typer.echo(f"{i}. {name}")
                if desc:
                    typer.echo(f"   {desc}")
        finally:
            await client.close()

    asyncio.run(_run())


@app.command("skill-get")
def skill_get(
    name: str = typer.Argument(..., help="Skill name"),
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="Output directory (default: ./skills)"
    ),
    version: Optional[str] = typer.Option(None, "--version"),
    label: Optional[str] = typer.Option(None, "--label"),
) -> None:
    """Download a skill ZIP from Nacos."""

    async def _run() -> None:
        from pathlib import Path

        cfg = load_nacos_config(_worker_name())
        out = Path(output or "skills")
        client = NacosClient(cfg)
        try:
            ver = await skill_mod.download_skill(
                client, name, out, version=version, label=label
            )
            typer.echo(f"Installed {name}@{ver} -> {out / name}")
        except NacosAPIError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        finally:
            await client.close()

    asyncio.run(_run())


def _is_valid_skill_name_pattern(value: str) -> bool:
    return bool(value and _SKILL_NAME_PATTERN.fullmatch(value))


def _build_patterns(query: str) -> list[str]:
    """Build Nacos-safe skillName filters from a free-text query."""
    q = re.sub(r"[^a-zA-Z0-9]+", " ", query.lower()).strip()
    tokens = [t for t in q.split() if len(t) >= 2]
    candidates: list[str] = []
    if q:
        hyphenated = "-".join(tokens) if tokens else q.replace(" ", "-")
        candidates.append(hyphenated)
    candidates.extend(tokens)
    seen: set[str] = set()
    patterns: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if _is_valid_skill_name_pattern(candidate):
            patterns.append(candidate)
    if patterns:
        return patterns
    # No valid filter — paginate unfiltered list and score client-side.
    return [""]


def _score(query: str, name: str, desc: str) -> int:
    q = query.lower()
    n = name.lower()
    d = desc.lower()
    score = 0
    if q in n:
        score += 10
    if q in d:
        score += 5
    for tok in q.split():
        if tok in n:
            score += 3
        if tok in d:
            score += 1
    return score


def main() -> None:
    app()


if __name__ == "__main__":
    main()

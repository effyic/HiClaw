"""Kind registry for collection-style workflow protocols.

Each kind registers a ``extract_config(workflow) -> dict | None`` callable.
Built-in ``collection_dialogue`` lives under ``kinds/dialogue``. Add future
kinds by placing a package under ``kinds/`` and registering its extractor.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

ConfigExtractor = Callable[[dict[str, Any]], dict[str, Any] | None]

_EXTRACTORS: list[ConfigExtractor] = []
_BOOTSTRAPPED = False


def register_config_extractor(extractor: ConfigExtractor) -> ConfigExtractor:
    """Register (or re-register) a kind config extractor. Returns extractor."""
    if extractor not in _EXTRACTORS:
        _EXTRACTORS.append(extractor)
    return extractor


def _bootstrap_builtin_kinds() -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True
    # Leaf import only — must not pull FSM/core (avoids import cycles).
    from agno_worker.tenant.collection.kinds.dialogue.kind import (  # noqa: F401
        extract_config as _dialogue_extract,
    )


def resolve_collection_config(workflow: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return protocol config for a supported collection kind, or None."""
    if not isinstance(workflow, dict) or not workflow:
        return None
    _bootstrap_builtin_kinds()
    for extractor in _EXTRACTORS:
        cfg = extractor(workflow)
        if cfg is not None:
            return cfg
    return None


def is_collection_enabled(workflow: dict[str, Any] | None) -> bool:
    return resolve_collection_config(workflow) is not None


def resolve_kind(workflow: dict[str, Any] | None) -> str | None:
    """Return the resolved protocol ``kind`` string, if any."""
    cfg = resolve_collection_config(workflow)
    if not cfg:
        return None
    kind = str(cfg.get("kind") or "").strip()
    return kind or None

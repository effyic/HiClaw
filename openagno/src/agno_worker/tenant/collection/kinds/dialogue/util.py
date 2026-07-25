"""Small helpers shared inside collection_dialogue."""
from __future__ import annotations

from typing import Any


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# Back-compat private alias used by historical call sites in this package.
_as_bool = as_bool

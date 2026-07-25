"""Kind identity and workflow → config extraction for collection_dialogue."""
from __future__ import annotations

from typing import Any

from agno_worker.tenant.collection.registry import register_config_extractor

KIND = "collection_dialogue"


@register_config_extractor
def extract_config(workflow: dict[str, Any]) -> dict[str, Any] | None:
    """Return collection_dialogue config from agent workflow JSON, or None.

    Accepts either a top-level ``kind: collection_dialogue`` document or a
    nested ``workflow.collection`` object with the same kind / schema shape.
    """
    if not isinstance(workflow, dict) or not workflow:
        return None
    if str(workflow.get("kind") or "").strip() == KIND:
        return workflow
    nested = workflow.get("collection")
    if not isinstance(nested, dict) or not nested:
        return None
    kind = str(nested.get("kind") or "").strip()
    if kind == KIND or nested.get("schema") or nested.get("required_actions"):
        if not kind:
            nested = dict(nested)
            nested.setdefault("kind", KIND)
        return nested
    return None

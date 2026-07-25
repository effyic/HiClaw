"""Config resolvers for collection_dialogue workflow JSON."""
from __future__ import annotations

from typing import Any

from agno_worker.tenant.collection.kinds.dialogue.constants import (
    OPENING_POLICY_FIRST_TURN,
    OPENING_POLICY_OPTIONAL,
)
from agno_worker.tenant.collection.kinds.dialogue.util import as_bool


def confirm_required(config: dict[str, Any] | None) -> bool:
    if not config:
        return True
    return as_bool(config.get("confirm_required"), True)


def resolve_scripts_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Optional ``workflow.scripts`` for opening / guide / closing phase hooks.

    Domain-agnostic: worker only injects text + delivery flags; it does not
    hardcode medical phrasing.
    """
    if not config:
        return None
    raw = config.get("scripts")
    if not isinstance(raw, dict) or not raw:
        return None
    opening = str(raw.get("opening") or "").strip()
    guide = str(raw.get("guide") or "").strip()
    closing = str(raw.get("closing") or "").strip()
    if not opening and not guide and not closing:
        return None
    policy = str(raw.get("opening_policy") or OPENING_POLICY_FIRST_TURN).strip().lower()
    if policy not in {OPENING_POLICY_FIRST_TURN, OPENING_POLICY_OPTIONAL}:
        policy = OPENING_POLICY_FIRST_TURN
    return {
        "opening": opening,
        "guide": guide,
        "closing": closing,
        "opening_policy": policy,
    }


def ask_batch_size(config: dict[str, Any] | None) -> int:
    """Legacy ask window size.

    Hard-cursor collection always focuses a single ``current_field``, so this
    always returns ``1``. ``workflow.ask_batch_size`` is ignored (kept only so
    older published configs remain valid JSON).
    """
    _ = config
    return 1


def resolve_probe_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Optional **post-required enrichment** loop (``phase=probing``).

    Published under ``workflow.probe``::

        {
          "enabled": true,
          "max_rounds": 3,
          "min_rounds": 2,
          "goal": "optional domain purpose string from the tenant agent",
          "hints": [],
          "allow_skip": true
        }

    Semantics (hard boundary):

    - Runs **only after** all required pre-probe slots (and their
      ``schema.fields[].probe``) are done — never during field cursor collect.
    - ``min_rounds`` / ``max_rounds`` bound **this enrichment loop only**
      (``collection_probe_note`` / ``collection_probe_finish`` without ``field=``).
    - They are **not** a global dialogue round budget, and must **not** override
      or stand in for ``schema.fields[].probe.min_rounds/max_rounds``.
    - Product ``collectConfig.maxRounds`` (system_prompt soft hint) is unrelated.

    Platform owns only the FSM (rounds / notes). What to ask and why comes from
    ``goal`` / ``hints`` / agent instructions — never hardcoded here.

    - ``min_rounds``: enrichment ``collection_probe_finish`` blocked until this
      many notes. After min and before max, the model may finish early.
    - ``max_rounds``: auto-complete enrichment when note count reaches this.
    - ``allow_skip``: when false, finish is blocked until max_rounds.
    - Deferred slots use ``schema.fields[].after_probe=true`` (decision slots).
    - Per-field enrichment uses ``schema.fields[].probe``, not this block.
    """
    if not isinstance(config, dict):
        return None
    raw = config.get("probe")
    if not isinstance(raw, dict) or not raw:
        return None
    if not as_bool(raw.get("enabled"), False):
        return None
    try:
        max_rounds = int(raw.get("max_rounds", 3))
    except (TypeError, ValueError):
        max_rounds = 3
    max_rounds = max(0, min(max_rounds, 8))
    try:
        min_rounds = int(raw.get("min_rounds", 0))
    except (TypeError, ValueError):
        min_rounds = 0
    min_rounds = max(0, min(min_rounds, max_rounds if max_rounds > 0 else 0))
    hints_raw = raw.get("hints")
    hints: list[str] = []
    if isinstance(hints_raw, list):
        hints = [str(x).strip() for x in hints_raw if str(x).strip()]
    goal = str(raw.get("goal") or "").strip()
    return {
        "enabled": True,
        "max_rounds": max_rounds,
        "min_rounds": min_rounds,
        "goal": goal,
        "hints": hints,
        "allow_skip": as_bool(raw.get("allow_skip"), True),
    }


def probe_max_rounds(config: dict[str, Any] | None) -> int:
    probe = resolve_probe_config(config)
    if not probe:
        return 0
    return int(probe.get("max_rounds") or 0)


REQUIRED_ACTIONS_MODE_SERIAL = "serial"
REQUIRED_ACTIONS_MODE_CONCURRENT = "concurrent"


def required_actions_mode(config: dict[str, Any] | None) -> str:
    """How ``required_actions`` may execute once the pipeline is ready.

    - ``serial``: only the current incomplete step is actionable; later MCP tools
      stay hard-gated until prior steps complete (may span turns).
    - ``concurrent``: all incomplete steps are actionable in the same turn
      (reply fields + MCP tools visible together). Definition order is preferred
      in prompts but not hard-enforced between steps.
    """
    if not config:
        return REQUIRED_ACTIONS_MODE_SERIAL
    raw = str(
        config.get("required_actions_mode")
        or config.get("required_action_mode")
        or ""
    ).strip().lower()
    if raw in {REQUIRED_ACTIONS_MODE_SERIAL, REQUIRED_ACTIONS_MODE_CONCURRENT}:
        return raw
    return REQUIRED_ACTIONS_MODE_SERIAL


def resolve_required_actions(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize ``required_actions`` (definition-ordered steps).

    Supported ``type`` values:

    - ``mcp``: require a successful MCP tool call (``tool``).
    - ``reply``: require collecting a schema field and producing the patient-facing
      closing / recommendation content for that field (``field``).

    Execution policy is controlled by :func:`required_actions_mode`.
    """
    if not config:
        return []
    raw = config.get("required_actions")
    if not isinstance(raw, list):
        return []
    actions: list[dict[str, Any]] = []
    for item in raw:
        normalized = _normalize_required_action(item)
        if normalized:
            actions.append(normalized)
    return actions


def reply_action_fields(config: dict[str, Any] | None) -> set[str]:
    """Schema field names referenced by ``type=reply`` required actions."""
    names: set[str] = set()
    for action in resolve_required_actions(config):
        if str(action.get("type") or "") != "reply":
            continue
        field = str(action.get("field") or "").strip()
        if field:
            names.add(field)
    return names


def suggested_write_tool(config: dict[str, Any] | None) -> str | None:
    """Return the last MCP tool in ``required_actions`` for prompt hints."""
    actions = resolve_required_actions(config)
    for action in reversed(actions):
        if str(action.get("type") or "") != "mcp":
            continue
        tool = str(action.get("tool") or "").strip()
        if tool:
            return tool
    return None


def _normalize_required_action(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    action_type = str(item.get("type") or "mcp").strip() or "mcp"
    when = str(item.get("when") or "missing_empty").strip() or "missing_empty"
    if when not in {"missing_empty", "before_mark_done"}:
        when = "missing_empty"

    if action_type == "mcp":
        tool = str(item.get("tool") or "").strip()
        if not tool:
            return None
        if "hard_gate" in item:
            hard_gate = as_bool(item.get("hard_gate"), True)
        else:
            hard_gate = when == "missing_empty"
        return {
            "type": "mcp",
            "tool": tool,
            "when": when,
            "hard_gate": hard_gate,
        }

    if action_type == "reply":
        field = str(item.get("field") or item.get("name") or "").strip()
        if not field:
            return None
        return {
            "type": "reply",
            "field": field,
            "when": when,
            "hard_gate": False,
        }

    return None


def required_action_auto_mark_done(config: dict[str, Any] | None) -> bool:
    """Whether satisfying required actions should auto ``completed=true``.

    Default true when any ``required_actions`` is configured; override with
    top-level ``auto_mark_done``.
    """
    if not config or not resolve_required_actions(config):
        return False
    if "auto_mark_done" in config:
        return as_bool(config.get("auto_mark_done"), True)
    return True


def required_action_key(action: dict[str, Any] | None) -> str:
    """Stable key used in ``actions_done`` for a required action step."""
    if not isinstance(action, dict):
        return ""
    action_type = str(action.get("type") or "").strip()
    if action_type == "mcp":
        return str(action.get("tool") or "").strip()
    if action_type == "reply":
        field = str(action.get("field") or "").strip()
        return f"reply:{field}" if field else ""
    return ""

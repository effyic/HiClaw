"""Shared constants for collection_dialogue kind."""
from __future__ import annotations

COLLECTION_STATE_KEY = "collection"
STATUS_MARKER_PREFIX = "<!--COLLECTION_STATUS"
STATUS_MARKER_SUFFIX = "-->"

PHASE_INIT = "init"
PHASE_COLLECTING = "collecting"
PHASE_PROBING = "probing"
PHASE_READY = "ready"
PHASE_CONFIRMED = "confirmed"
PHASE_DONE = "done"

OPENING_POLICY_FIRST_TURN = "first_turn_required"
OPENING_POLICY_OPTIONAL = "optional"

_VALID_PHASES = frozenset(
    {
        PHASE_INIT,
        PHASE_COLLECTING,
        PHASE_PROBING,
        PHASE_READY,
        PHASE_CONFIRMED,
        PHASE_DONE,
    }
)

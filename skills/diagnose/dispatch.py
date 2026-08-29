"""Diagnose-owned dispatch hygiene.

This module is intentionally separate from diagnose-steer dispatch.  It accepts
only the blind diagnose trigger surface and rejects any steer field recursively.
"""

from __future__ import annotations

import re

DIAGNOSE_INPUT_REQUIRED = {"failing_test"}
DIAGNOSE_INPUT_ALLOWED = DIAGNOSE_INPUT_REQUIRED
NODE_ID_RE = re.compile(r"[\w./-]+\.py(::[^:\[\]]+(\[.*\])?)+")


def validate_diagnose_input(payload: dict) -> list[str]:
    reasons: list[str] = []
    if not isinstance(payload, dict):
        return ["invalid-input"]
    missing = DIAGNOSE_INPUT_REQUIRED - set(payload)
    if missing:
        reasons.append("missing-field")
    extra = set(payload) - DIAGNOSE_INPUT_ALLOWED
    if extra:
        reasons.append("unknown-field")
    if _contains_key(payload, "steer"):
        reasons.append("steer-field")
    for key in DIAGNOSE_INPUT_REQUIRED:
        if key in payload and not isinstance(payload[key], str):
            reasons.append("invalid-field")
    failing_test = payload.get("failing_test")
    if isinstance(failing_test, str) and not NODE_ID_RE.fullmatch(failing_test):
        reasons.append("invalid-field")
    return sorted(set(reasons))


def clean_scribe_context() -> dict:
    return {"orchestrator_context_visible": False}


def disjoint_channel_routes() -> list[dict]:
    return []


def _contains_key(value, target: str) -> bool:
    if isinstance(value, dict):
        return any(key == target or _contains_key(item, target) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False

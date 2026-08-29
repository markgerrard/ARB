from __future__ import annotations


def panel_input_lock_reason(payload: dict | None) -> str | None:
    """Return the immutable-input reason for certifying panel tasks."""
    payload = payload or {}
    if payload.get("audit_vote_expected") or payload.get("panel_input_locked") or payload.get("certifying"):
        return "panel_task_input_locked"
    return None

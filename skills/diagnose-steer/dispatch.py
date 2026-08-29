"""Diagnose-steer owned dispatch helpers."""

from __future__ import annotations

from pathlib import Path

from skills._diagnose_common import append_event


def append_steer_event(
    log_path: str | Path,
    event_type: str,
    seat: str,
    channel: str,
    artifact: bytes | str,
    **seat_supplied_fields,
) -> dict:
    if "prev_hash" in seat_supplied_fields:
        raise ValueError("prev_hash is common-owned and may not be supplied by a seat")
    return append_event(log_path, event_type, seat, channel, artifact, **seat_supplied_fields)


def clean_scribe_context() -> dict:
    return {"orchestrator_context_visible": False}


def disjoint_channel_routes() -> list[dict]:
    return []

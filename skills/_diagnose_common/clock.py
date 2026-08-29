"""Tamper-evident append-only dispatch log utilities."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def _artifact_bytes(artifact: bytes | str) -> bytes:
    if isinstance(artifact, bytes):
        return artifact
    return artifact.encode("utf-8")


def artifact_hash(artifact: bytes | str) -> str:
    return hashlib.sha256(_artifact_bytes(artifact)).hexdigest()


def read_dispatch_log(log_path: str | Path) -> list[dict]:
    path = Path(log_path)
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _event_hash(event: dict) -> str:
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def append_event(
    log_path: str | Path,
    event_type: str,
    seat: str,
    channel: str,
    artifact: bytes | str,
    *,
    timestamp: str | None = None,
    **seat_supplied_fields,
) -> dict:
    if "seq" in seat_supplied_fields:
        raise ValueError("seq is common-owned and may not be supplied by a seat")
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    events = read_dispatch_log(path)
    seq = len(events) + 1
    prev_hash = events[-1]["event_hash"] if events else None
    event = {
        "seq": seq,
        "event_type": event_type,
        "seat": seat,
        "channel": channel,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "artifact_hash": artifact_hash(artifact),
        "prev_hash": prev_hash,
        "dispatch_log_ref": str(path),
    }
    event.update(seat_supplied_fields)
    event["event_hash"] = _event_hash(event)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def validate_dispatch_log(events: Iterable[dict]) -> list[str]:
    reasons: list[str] = []
    seen: set[int] = set()
    expected = 1
    previous_hash = None
    for event in events:
        seq = event.get("seq")
        if not isinstance(seq, int):
            reasons.append("missing-seq")
            continue
        if seq in seen:
            reasons.append("duplicate-seq")
        if seq != expected:
            reasons.append("missing-seq")
        seen.add(seq)
        expected += 1
        for field in ("event_type", "seat", "channel", "timestamp", "artifact_hash", "event_hash"):
            if field not in event:
                reasons.append(f"missing-{field}")
        if event.get("prev_hash") != previous_hash:
            reasons.append("hash-chain")
        expected_hash_event = dict(event)
        event_hash = expected_hash_event.pop("event_hash", None)
        if event_hash is not None and _event_hash(expected_hash_event) != event_hash:
            reasons.append("hash-chain")
        previous_hash = event_hash
    return sorted(set(reasons))


def max_seq(events: Iterable[dict], event_type: str) -> int | None:
    values = [
        event["seq"]
        for event in events
        if event.get("event_type") == event_type and isinstance(event.get("seq"), int)
    ]
    return max(values) if values else None

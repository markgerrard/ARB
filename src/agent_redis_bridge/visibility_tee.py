from __future__ import annotations

import json

from .redact import redact
from .transcript_flusher import build_trace_fields

def _redact_live_data(value):
    # The events:live stream is the human-visibility plane behind only the
    # gateway's auth; per the full-fidelity-capture design "raw live is NOT the
    # baseline". Scrub EVERY string value in the payload (nested dicts, lists,
    # and any field name — command/delta but also summary/error/message and
    # anything future engines add) at the XADD boundary, so redaction is a
    # structural server-side boundary, not a key-denylist that drifts as new
    # emitters appear (audit VIS-1 + panel hardening). redact() is a no-op on
    # non-secret text, so applying it to every string is safe.
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_live_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_live_data(v) for v in value]
    return value


def _live_fields(
    *,
    run_id,
    task_id,
    seat_id,
    orchestrator,
    event_type,
    sent_at,
    data,
) -> dict[str, str]:
    return {
        "run_id": run_id,
        "task_id": task_id,
        "seat_id": seat_id or "",
        "orchestrator": orchestrator or "",
        "event_type": event_type,
        "sent_at": sent_at,
        "data": json.dumps(_redact_live_data(data), separators=(",", ":")),
    }


def live_tee(
    redis,
    prefix,
    *,
    run_id,
    task_id,
    seat_id,
    orchestrator,
    event_type,
    sent_at,
    data,
    maxlen,
    ttl,
):
    fields = _live_fields(
        run_id=run_id,
        task_id=task_id,
        seat_id=seat_id,
        orchestrator=orchestrator,
        event_type=event_type,
        sent_at=sent_at,
        data=data,
    )
    key = f"{prefix}events:live"
    entry = redis.xadd(key, fields, maxlen=maxlen)
    if ttl is not None and hasattr(redis, "expire"):
        redis.expire(key, ttl)
    return entry


def trace_tee(
    redis,
    prefix,
    *,
    run_id,
    task_id,
    seat_id,
    orchestrator,
    event,
    turn_index,
    data,
    seq,
    maxlen,
    redactor=redact,
):
    if not isinstance(data, dict):
        data = {}
    delta = data.get("delta")
    content = data.get("content")
    text = delta if isinstance(delta, str) else content if isinstance(content, str) else ""
    kind = str(data.get("kind") or event or "")
    fields = build_trace_fields(
        run_id=run_id,
        task_id=task_id,
        turn_index=turn_index,
        seat_id=seat_id,
        orchestrator=orchestrator,
        item_id=str(data.get("item_id") or ""),
        seq=seq,
        kind=kind,
        tool_name=str(data.get("tool_name") or data.get("command") or ""),
        content=text,
        redactor=redactor,
    )
    if fields is None:
        return None
    return redis.xadd(f"{prefix}arbmem:trace", fields, maxlen=maxlen, approximate=True)

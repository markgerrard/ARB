from __future__ import annotations

from datetime import datetime, timezone
import json


STALE_GRACE_S = 120


def _parse_ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_stale(state, *, now=None):
    if state.get("state") != "running":
        return False
    last_event_ts = _parse_ts(state.get("last_event_ts"))
    if last_event_ts is None:
        return False
    now = now or datetime.now(timezone.utc)
    if last_event_ts.tzinfo is None:
        last_event_ts = last_event_ts.replace(tzinfo=timezone.utc)
    return (now - last_event_ts).total_seconds() > STALE_GRACE_S


def _entry_data(entry):
    raw = entry.get("data", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def reduce_seat(state: dict, entry: dict) -> dict:
    reduced = dict(state)
    if not entry:
        if _is_stale(reduced):
            reduced["state"] = "stale"
        return reduced

    event_type = entry.get("event_type")
    data = _entry_data(entry)
    reduced.update(
        {
            "run_id": entry.get("run_id") or reduced.get("run_id"),
            "task_id": entry.get("task_id") or reduced.get("task_id"),
            "seat_id": entry.get("seat_id") or reduced.get("seat_id"),
            "orchestrator": entry.get("orchestrator") or reduced.get("orchestrator"),
            "last_event_ts": entry.get("sent_at") or reduced.get("last_event_ts"),
            "last_event": event_type or reduced.get("last_event"),
        }
    )
    if event_type in {"task_started", "task_continuing"}:
        reduced["state"] = "running"
    elif event_type == "task_finished":
        reduced["state"] = "failed" if data.get("ok") is False else "done"
    elif event_type == "vote":
        reduced["voted"] = True
        if "stance" in data:
            reduced["stance"] = data["stance"]
        if reduced.get("state") not in {"done", "failed"}:
            reduced["state"] = "voted"
    if _is_stale(reduced):
        reduced["state"] = "stale"
    return reduced


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _timeline_prefix(event: dict) -> str:
    return " ".join(
        part
        for part in (
            event.get("ts") or event.get("sent_at") or "",
            event.get("source") or "event",
            event.get("kind") or event.get("event_type") or "",
        )
        if part
    )


def format_timeline_summary(event: dict) -> str:
    prefix = _timeline_prefix(event)
    if event.get("source") != "transcript":
        return prefix

    kind = event.get("kind") or event.get("event_type") or ""
    meta = event.get("meta") or {}
    tool_name = event.get("tool_name") or ""
    if tool_name == "apply_patch" and meta.get("file"):
        detail = f"edited `{meta.get('file')}` +{_to_int(meta.get('added'))}/-{_to_int(meta.get('removed'))}"
    elif kind == "model_thinking":
        detail = "thinking"
    elif tool_name or kind in {"command_started", "command_finished", "command_output", "tool_call", "tool_output"}:
        detail = tool_name
    else:
        detail = event.get("content") or ""
    return " ".join(part for part in (prefix, detail) if part)


def format_timeline_event(event: dict) -> str:
    prefix = _timeline_prefix(event)
    if event.get("source") != "transcript":
        return prefix

    kind = event.get("kind") or event.get("event_type") or ""
    content = event.get("content") or ""
    meta = event.get("meta") or {}
    tool_name = event.get("tool_name") or ""
    detail = ""
    if tool_name == "apply_patch" and meta.get("file"):
        summary = f"edited `{meta.get('file')}` +{_to_int(meta.get('added'))}/-{_to_int(meta.get('removed'))}"
        detail = summary
        if content:
            detail = f"{summary}\n[dim]diff[/dim]\n{content}"
    elif kind == "model_thinking":
        detail = "[dim][thinking][/dim]"
        if content:
            detail = f"{detail}\n{content}"
    elif tool_name or kind in {"command_started", "command_finished", "command_output", "tool_call", "tool_output"}:
        detail = "\n".join(part for part in (tool_name, content) if part)
    else:
        detail = content
    return " ".join(part for part in (prefix, detail) if part)

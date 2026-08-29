from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any


DEFAULT_REGISTRY_KEY = "claude:registry"
DEFAULT_COLD_DIR = "~/.claude/tasks"
DEFAULT_COLD_AGENT_TYPES = "code-reviewer-report-writer"


def load_hook_payload(args: list[str] | None = None) -> dict[str, Any]:
    if args:
        raw = args[0]
    else:
        raw = sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("hook payload must be a JSON object")
    return data


def registry_path() -> Path | None:
    value = os.environ.get("ARB_CLAUDE_TAIL_REGISTRY_PATH")
    return Path(value).expanduser() if value else None


def read_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        sessions = data.get("sessions")
        if isinstance(sessions, list):
            return [item for item in sessions if isinstance(item, dict)]
        return [dict(value, session_id=key) for key, value in data.items() if isinstance(value, dict)]
    return []


def write_registry(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(records, separators=(",", ":"), sort_keys=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def upsert_registry_record(path: Path, record: dict[str, str]) -> None:
    records = [item for item in read_registry(path) if item.get("session_id") != record["session_id"]]
    records.append(record)
    records.sort(key=lambda item: str(item.get("session_id") or ""))
    write_registry(path, records)


def remove_registry_record(path: Path, session_id: str) -> None:
    records = [item for item in read_registry(path) if item.get("session_id") != session_id]
    write_registry(path, records)


def evict_stale_seat_records(path: Path, seat_id: str, keep_session_id: str) -> list[str]:
    """Drop registry records that share ``seat_id`` with a different session_id
    than ``keep_session_id``. A warm orchestrator seat is one logical thread at a
    time — a new SessionStart for the seat supersedes any prior live record left
    behind by a /clear or resume that wasn't cleanly ended."""
    records = read_registry(path)
    keep, evicted = [], []
    for item in records:
        if item.get("seat_id") == seat_id and item.get("session_id") != keep_session_id:
            evicted.append(str(item.get("session_id")))
        else:
            keep.append(item)
    if evicted:
        write_registry(path, keep)
    return evicted


def registry_redis_key(prefix: str | None = None, key: str | None = None) -> str:
    return f"{prefix if prefix is not None else os.environ.get('AGENT_REDIS_PREFIX', 'agent_scratch:')}{key or os.environ.get('ARB_CLAUDE_TAIL_REGISTRY_KEY', DEFAULT_REGISTRY_KEY)}"


def draining_redis_key(session_id: str, prefix: str | None = None) -> str:
    return f"{prefix if prefix is not None else os.environ.get('AGENT_REDIS_PREFIX', 'agent_scratch:')}claude:draining:{session_id}"


def redis_client():
    url = os.environ.get("AGENT_REDIS_URL")
    if not url:
        return None
    import redis

    return redis.Redis.from_url(url, decode_responses=True)


def read_redis_registry(client) -> list[dict[str, Any]]:
    fields = client.hgetall(registry_redis_key()) or {}
    records: list[dict[str, Any]] = []
    for field, raw in fields.items():
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data.setdefault("session_id", field.decode() if isinstance(field, bytes) else str(field))
            records.append(data)
    return records


def upsert_redis_record(client, record: dict[str, str]) -> None:
    client.hset(registry_redis_key(), record["session_id"], json.dumps(record, separators=(",", ":"), sort_keys=True))


def remove_redis_record(client, session_id: str) -> None:
    client.hdel(registry_redis_key(), session_id)


def copy_redis_record_to_draining(client, session_id: str, *, ttl_secs: int = 604800) -> bool:
    """Copy the registry record to the durable draining key.

    Called by session_end BEFORE remove_redis_record: if the daemon dies in the
    window between registry removal and its next tick, the fresh process
    rediscovers the transcript from this record (CT-1 spec §A, panel r6). A
    crash between the copy and the removal leaves both records — the daemon's
    flap rule (registry supersedes) makes that safe.
    """
    raw = client.hget(registry_redis_key(), session_id)
    if raw is None:
        return False
    if isinstance(raw, bytes):
        raw = raw.decode()
    client.set(draining_redis_key(session_id), raw, ex=ttl_secs)
    return True


def evict_stale_seat_redis_records(client, seat_id: str, keep_session_id: str) -> list[str]:
    """Redis-registry counterpart of ``evict_stale_seat_records``."""
    evicted = []
    for item in read_redis_registry(client):
        if item.get("seat_id") == seat_id and item.get("session_id") != keep_session_id:
            sid = str(item.get("session_id"))
            evicted.append(sid)
            remove_redis_record(client, sid)
    return evicted


def slug(value: str) -> str:
    lowered = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return cleaned or "unknown"


def project_workspace(payload: dict[str, Any]) -> tuple[str, str]:
    project = payload.get("project") or payload.get("project_name") or os.environ.get("ARB_CLAUDE_TAIL_PROJECT")
    workspace = payload.get("workspace") or payload.get("workspace_name") or os.environ.get("ARB_CLAUDE_TAIL_WORKSPACE")
    cwd = payload.get("cwd") or payload.get("project_dir") or payload.get("workspace_dir")
    if not project and isinstance(cwd, str) and cwd:
        project = Path(cwd).name
    if not workspace:
        workspace = Path(str(cwd)).name if cwd else "default"
    return slug(str(project or "claude")), slug(str(workspace))


def required_str(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    raise ValueError(f"missing required field: {'/'.join(names)}")


def mirror_cold_outputs(payload: dict[str, Any]) -> None:
    paths = payload.get("cold_output_paths") or payload.get("cold_outputs") or []
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        return
    cold_dir_path = cold_dir()
    cold_dir_path.mkdir(parents=True, exist_ok=True)
    for item in paths:
        if not isinstance(item, str) or not item:
            continue
        source = Path(item).expanduser()
        if not source.exists() or source.suffix != ".output":
            continue
        target = cold_dir_path / source.name
        source_resolved = source.resolve()
        if target.exists() or target.is_symlink():
            try:
                if target.resolve() == source_resolved:
                    continue
            except FileNotFoundError:
                pass
        if target.is_symlink():
            try:
                if target.resolve() == source_resolved:
                    continue
            except FileNotFoundError:
                pass
            target.unlink()
        elif target.exists():
            continue
        try:
            target.symlink_to(source)
        except OSError:
            shutil.copy2(source, target)


def cold_dir() -> Path:
    return Path(os.environ.get("ARB_CLAUDE_TAIL_COLD_DIR", DEFAULT_COLD_DIR)).expanduser()


def cold_agent_types() -> set[str]:
    raw = os.environ.get("ARB_CLAUDE_TAIL_COLD_AGENT_TYPES", DEFAULT_COLD_AGENT_TYPES)
    return {item.strip() for item in raw.split(",") if item.strip()}


def lookup_registry_record(session_id: str) -> dict[str, Any] | None:
    path = registry_path()
    if path is not None:
        records = read_registry(path)
    else:
        client = redis_client()
        if client is None:
            return None
        records = read_redis_registry(client)
    for item in records:
        if item.get("session_id") == session_id:
            return item
    return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    # Mirrors write_registry's existing temp-file-in-same-dir + os.replace idiom (above) — a
    # reader globbing the directory never observes a partially-written file.
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def fail_soft(label: str, func, *args, **kwargs) -> int:
    try:
        return int(func(*args, **kwargs) or 0)
    except Exception as exc:
        print(f"claude-tail hook error: {label}: {exc}", file=sys.stderr)
        return 0

from __future__ import annotations

from contextlib import suppress
import json
from pathlib import Path
import shutil
from typing import Any

from claude_agent_sdk.types import SessionStore


REDACTED = "[REDACTED]"
_OPTIONAL_SESSION_STORE_METHODS = {"list_sessions", "list_session_summaries", "list_subkeys", "delete"}


def scrub(text: str, secrets: list[str], var_names: list[str]) -> str:
    out = text
    for value in list(secrets) + list(var_names):
        value = (value or "").strip()
        if value:
            out = out.replace(value, REDACTED)
    return out


def _safe_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)


def _key_path(root: Path, agent_id: str, key: dict[str, Any]) -> Path:
    session_id = key["session_id"]
    base = root / _safe_part(agent_id) / _safe_part(str(key["project_key"]))
    subpath = key.get("subpath")
    if subpath:
        parts = [_safe_part(part) for part in str(subpath).split("/") if part]
        return base / _safe_part(str(session_id)) / Path(*parts).with_suffix(".jsonl")
    return base / f"{_safe_part(str(session_id))}.jsonl"


def _session_dir(root: Path, agent_id: str, key: dict[str, Any]) -> Path:
    base = root / _safe_part(agent_id) / _safe_part(str(key["project_key"]))
    return base / _safe_part(str(key["session_id"]))


def _inner_implements(store: Any, method: str) -> bool:
    impl = getattr(store, method, None)
    if impl is None:
        return False
    default = getattr(SessionStore, method, None)
    return getattr(type(store), method, None) is not default


class FileSessionStore:
    def __init__(self, root: Path, agent_id: str) -> None:
        self.root = root
        self.agent_id = agent_id

    async def append(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        path = _key_path(self.root, self.agent_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, separators=(",", ":")))
                handle.write("\n")

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        path = _key_path(self.root, self.agent_id, key)
        try:
            with path.open(encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
        except FileNotFoundError:
            return None

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        project_dir = self.root / _safe_part(self.agent_id) / _safe_part(str(project_key))
        if not project_dir.is_dir():
            return []
        return [
            {"session_id": path.stem, "mtime": int(path.stat().st_mtime * 1000)}
            for path in project_dir.glob("*.jsonl")
            if path.is_file()
        ]

    async def delete(self, key: dict[str, Any]) -> None:
        path = _key_path(self.root, self.agent_id, key)
        with suppress(FileNotFoundError):
            path.unlink()
        if not key.get("subpath"):
            shutil.rmtree(_session_dir(self.root, self.agent_id, key), ignore_errors=True)

    async def list_subkeys(self, key: dict[str, Any]) -> list[str]:
        session_dir = _session_dir(self.root, self.agent_id, key)
        if not session_dir.is_dir():
            return []
        return [
            path.relative_to(session_dir).with_suffix("").as_posix()
            for path in session_dir.rglob("*.jsonl")
            if path.is_file()
        ]


class ScrubbedSessionStore:
    def __init__(self, inner: Any, secrets: list[str], var_names: list[str]) -> None:
        self.inner = inner
        self.secrets = secrets
        self.var_names = var_names

    async def append(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        scrubbed = []
        for entry in entries:
            text = json.dumps(entry, ensure_ascii=False)
            cleaned = scrub(text, self.secrets, self.var_names)
            scrubbed.append(json.loads(cleaned))
        await self.inner.append(key, scrubbed)

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        return await self.inner.load(key)

    def __getattr__(self, name: str) -> Any:
        if name in _OPTIONAL_SESSION_STORE_METHODS and _inner_implements(self.inner, name):
            return getattr(self.inner, name)
        raise AttributeError(name)

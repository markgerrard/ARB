from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from arb_memory.local_read_policy import local_read_dsn


LOCAL_MEMORY_MCP_ENV_KEYS = (
    "ARB_MEMORY_LOCAL_DSN",
    "OPENAI_API_KEY",
    "PATH",
    "PYTHONPATH",
)

# Keys whose values must never appear on a child process command line (ps-visible).
LOCAL_MEMORY_MCP_SECRET_KEYS = (
    "ARB_MEMORY_LOCAL_DSN",
    "OPENAI_API_KEY",
)


READERS_ENV_PATH = "~/.arb-memory-local/readers.env"

_TIER_VALUES = ("dev", "prod")


def _read_readers_env(path: Path) -> dict[str, str]:
    """Parse a readers.env-style file: optional `export ` prefix, KEY=VALUE,
    first-`=` split, optional single/double quotes. Unreadable → empty (the
    feature is optional; absence must never break seat startup)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _server_command() -> str:
    """Prefer the console script sitting next to the running interpreter —
    plist-launched seats have no venv bin on PATH."""
    candidate = Path(sys.executable).parent / "arb-memory-local-mcp"
    if candidate.is_file():
        return str(candidate)
    return "arb-memory-local-mcp"


def local_memory_mcp_config() -> dict[str, Any] | None:
    flag = os.environ.get("ARB_MEMORY_LOCAL_MCP")
    if flag is None:
        return None
    overlay: dict[str, str] = {}
    if flag in _TIER_VALUES:
        readers = _read_readers_env(Path(os.path.expanduser(READERS_ENV_PATH)))
        tier_dsn = readers.get(f"ARB_MEMORY_LOCAL_DSN_{flag.upper()}", "")
        if not tier_dsn:
            return None
        overlay["ARB_MEMORY_LOCAL_DSN"] = tier_dsn
        if readers.get("OPENAI_API_KEY"):
            overlay["OPENAI_API_KEY"] = readers["OPENAI_API_KEY"]
    merged: dict[str, str] = {**os.environ, **overlay}
    dsn = local_read_dsn(merged)
    env = {key: merged[key] for key in LOCAL_MEMORY_MCP_ENV_KEYS if key in merged}
    env["ARB_MEMORY_LOCAL_DSN"] = dsn
    return {
        "command": _server_command(),
        "args": [],
        "env": env,
    }


def local_memory_mcp_env_file_path() -> Path:
    return Path(os.path.expanduser("~/.cache/agent-redis-bridge")) / "arb-memory-local-mcp.env"


def write_local_memory_mcp_env_file(secrets: dict[str, str]) -> Path:
    path = local_memory_mcp_env_file_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    # tmp name must be per-process: daemons bootstrapping concurrently would otherwise
    # os.replace each other's shared tmp away (ENOENT crash at seat standup).
    tmp = path.with_suffix(f".env.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for key, value in secrets.items():
            if "\n" in value:
                raise RuntimeError(f"local-memory MCP secret {key} contains a newline; cannot serialize")
            fh.write(f"{key}={value}\n")
    os.replace(tmp, path)
    return path


def local_memory_mcp_argv_safe_config() -> dict[str, Any] | None:
    """local_memory_mcp_config, reshaped for engines that place env on a command line.

    Secret values go into a mode-600 file (regenerated from the daemon environment at
    every engine spawn — the file is a relay, not a source of truth); the returned env
    carries only a pointer plus non-secret keys. One shared path per host, so only
    host-invariant secrets may be routed through it.
    """
    config = local_memory_mcp_config()
    if config is None:
        return None
    env = config["env"]
    secrets = {key: env[key] for key in LOCAL_MEMORY_MCP_SECRET_KEYS if key in env}
    safe_env: dict[str, str] = {
        "ARB_MEMORY_LOCAL_ENV_FILE": str(write_local_memory_mcp_env_file(secrets))
    }
    for key, value in env.items():
        if key not in LOCAL_MEMORY_MCP_SECRET_KEYS:
            safe_env[key] = value
    return {**config, "env": safe_env}


def local_memory_mcp_servers() -> list[dict[str, Any]]:
    config = local_memory_mcp_config()
    if config is None:
        return []
    return [config]

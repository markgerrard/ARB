from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_redis_bridge.redis_io import RedisCli, RedisConfig


def connect(env_file: Path):
    return RedisCli(RedisConfig.from_env_file(env_file, {})).client


def send(redis_client, prefix: str, recipient: str, envelope: dict[str, Any]) -> None:
    redis_client.lpush(f"{prefix}agent:{recipient}:inbox", json.dumps(envelope, separators=(",", ":")))


def receive(redis_client, prefix: str, agent_id: str, timeout: int = 5) -> dict[str, Any] | None:
    item = redis_client.brpop(f"{prefix}agent:{agent_id}:inbox", timeout=timeout)
    if not item:
        return None
    try:
        value = json.loads(item[1])
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None

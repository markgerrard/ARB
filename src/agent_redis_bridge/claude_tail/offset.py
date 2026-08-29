from __future__ import annotations

import json
import logging
from typing import Any, NamedTuple


logger = logging.getLogger("agent_redis_bridge.claude_tail.offset")
_COMPOSITE_VERSION = 1


def offset_key(path: str, inode: int) -> str:
    return f"{path}|{inode}"


class Position(NamedTuple):
    offset: int
    turn_index: int


class OffsetStore:
    def __init__(self, redis: Any, prefix: str) -> None:
        self.redis = redis
        self.prefix = prefix

    def load(self, key: str) -> Position:
        raw = self.redis.get(self._redis_key(key))
        if raw is None:
            return Position(0, 0)
        if isinstance(raw, bytes):
            try:
                raw = raw.decode()
            except UnicodeDecodeError:
                logger.warning("corrupt claude-tail position; recounting from byte 0", extra={"offset_key": key})
                return Position(0, 0)
        if isinstance(raw, str) and raw.lstrip("-").isdigit():
            return Position(0, 0)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            logger.warning("corrupt claude-tail position; recounting from byte 0", extra={"offset_key": key})
            return Position(0, 0)
        if (
            not isinstance(data, dict)
            or type(data.get("v")) is not int
            or data["v"] != _COMPOSITE_VERSION
            or not isinstance(data.get("offset"), int)
            or not isinstance(data.get("turn_index"), int)
            or isinstance(data.get("offset"), bool)
            or isinstance(data.get("turn_index"), bool)
            or data["offset"] < 0
            or data["turn_index"] < 0
        ):
            logger.warning("corrupt claude-tail position; recounting from byte 0", extra={"offset_key": key})
            return Position(0, 0)
        return Position(offset=data["offset"], turn_index=data["turn_index"])

    def store(self, key: str, offset: int, turn_index: int) -> None:
        offset = int(offset)
        turn_index = int(turn_index)
        if offset < 0 or turn_index < 0:
            raise ValueError("offset and turn_index must be non-negative")
        payload = json.dumps(
            {"v": _COMPOSITE_VERSION, "offset": offset, "turn_index": turn_index},
            separators=(",", ":"),
        )
        self.redis.set(self._redis_key(key), payload)

    def _redis_key(self, key: str) -> str:
        return f"{self.prefix}claude:offset:{key}"

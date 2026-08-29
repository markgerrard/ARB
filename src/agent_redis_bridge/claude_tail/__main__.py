from __future__ import annotations

import os
import logging
import logging.handlers
import socket
import time
from typing import Callable

from redis.exceptions import RedisError

from .service import build_service_from_env
from .watchdog import Watchdog


def configure_logging(label: str) -> str:
    path = os.environ.get("ARB_CLAUDE_TAIL_LOG_FILE") or os.path.expanduser(f"~/Library/Logs/claude-tail/{label}.log")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("agent_redis_bridge")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return path


def build_watchdog(interval: float):
    raw = float(os.environ.get("ARB_CLAUDE_TAIL_WATCHDOG_SECS", "300"))
    if raw <= 0:
        return None
    return Watchdog(raw, interval)


def heartbeat_label() -> str:
    return os.environ.get("ARB_CLAUDE_TAIL_HEARTBEAT_LABEL") or f"claude-tail.{socket.gethostname()}"


def run_loop(service, *, interval: float, sleep_func: Callable[[float], None] = time.sleep, max_ticks: int | None = None, watchdog=None) -> None:
    import logging

    log = logging.getLogger("agent_redis_bridge.claude_tail.service")
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        try:
            service.tick()
        except RedisError:
            # Infra: crash-fast (spec §A). launchd KeepAlive is the retry;
            # normal propagation exits through the interpreter, which flushes
            # logging handlers.
            log.exception("claude tail tick failed on the bus; exiting for KeepAlive respawn")
            raise
        except Exception:
            log.exception("Claude tail service tick failed")
        if watchdog is not None:
            # After every completed tick, success or handled failure — the
            # loop is alive either way (spec §B).
            watchdog.mark_tick()
        ticks += 1
        sleep_func(interval)


def main() -> int:
    label = heartbeat_label()
    configure_logging(label)
    interval = float(os.environ.get("ARB_CLAUDE_TAIL_INTERVAL_SECS", "1.0"))
    watchdog = build_watchdog(interval)
    stale_after = int((watchdog.effective_threshold if watchdog else 300.0) + 30.0)
    service = build_service_from_env(heartbeat_label=label, stale_after_s=stale_after)
    if watchdog is not None:
        watchdog.mark_tick()
        watchdog.start()
    run_loop(service, interval=interval, watchdog=watchdog)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

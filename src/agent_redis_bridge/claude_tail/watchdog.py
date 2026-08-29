from __future__ import annotations

import os
import threading
import time

LAST_GASP = b"[claude-tail watchdog] main loop stalled past threshold; os._exit(86)\n"


class Watchdog:
    """Crash a stalled main loop.

    The check path does time arithmetic, one raw fd write, and exit — no
    Redis, no locks, and NEVER the logging framework: if the main thread is
    hung inside a logging write holding the handler lock (a live candidate
    for the 2026-07-06 incident), a logging call here would deadlock behind
    the same lock and the process would never exit (spec §B).
    """

    def __init__(
        self,
        threshold_secs: float,
        tick_interval_secs: float,
        *,
        wake_secs: float = 15.0,
        time_func=time.monotonic,
        write_func=os.write,
        exit_func=os._exit,
    ) -> None:
        floor = 3.0 * float(tick_interval_secs) + 60.0
        self.effective_threshold = max(float(threshold_secs), floor)
        if self.effective_threshold > float(threshold_secs):
            # Startup log line when the floor raises the threshold (spec §B;
            # plan panel, agy P1). Init runs on the MAIN thread before
            # start() — the no-logging rule applies to check(), not here.
            import logging

            logging.getLogger("agent_redis_bridge.claude_tail.watchdog").warning(
                "watchdog threshold raised to %.0fs (floor 3*interval+60 over configured %.0fs)",
                self.effective_threshold,
                float(threshold_secs),
            )
        self.wake_secs = wake_secs
        self._time = time_func
        self._write = write_func
        self._exit = exit_func
        # Init pin (spec §B): last_tick starts NOW, so a fresh daemon gets a
        # full threshold window before its first completed tick.
        self._last_tick = time_func()

    def mark_tick(self) -> None:
        self._last_tick = self._time()

    def check(self) -> None:
        if self._time() - self._last_tick > self.effective_threshold:
            try:
                self._write(2, LAST_GASP)
            except Exception:
                pass  # a dead stderr must not stop the exit (spec §B)
            self._exit(86)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="claude-tail-watchdog", daemon=True)
        thread.start()
        return thread

    def _loop(self) -> None:  # pragma: no cover - thin sleep loop over check()
        while True:
            time.sleep(self.wake_secs)
            self.check()

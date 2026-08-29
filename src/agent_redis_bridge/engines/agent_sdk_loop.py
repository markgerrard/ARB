from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any


class LoopThread:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._started = threading.Event()

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self._started.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self._started.wait(timeout=5)
        if self.loop is None:
            raise RuntimeError("loop thread did not start")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        self._started.set()
        loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future:
        if self.loop is None:
            raise RuntimeError("loop thread not started")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self, timeout: float = 5) -> None:
        loop = self.loop
        thread = self.thread
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=timeout)
        if not loop.is_closed():
            loop.close()
        self.loop = None
        self.thread = None

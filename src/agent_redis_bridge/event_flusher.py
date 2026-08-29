from __future__ import annotations

import logging
import queue
from typing import Any, Callable

logger = logging.getLogger("agent_redis_bridge.event_flusher")


class EventFlusher:
    def __init__(
        self,
        redis: Any,
        stream: str,
        *,
        maxsize: int = 10000,
        maxlen: int | None = None,
        poll_s: float = 0.25,
        on_drop: Callable[[dict[str, Any], BaseException], None] | None = None,
        on_marker_drop: Callable[[dict[str, Any], BaseException], None] | None = None,
    ) -> None:
        self.redis = redis
        self.stream = stream
        self.maxlen = maxlen
        self.poll_s = poll_s
        self.on_drop = on_drop
        self.on_marker_drop = on_marker_drop
        self.q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        self._stop = False
        self.process_error_count = 0

    def stop(self) -> None:
        self._stop = True

    def enqueue(self, fields: dict[str, Any], *, marker: bool = False) -> bool:
        try:
            self.q.put_nowait({"fields": fields, "marker": marker})
        except queue.Full:
            return False
        return True

    def run(self) -> None:
        logger.warning("event flusher started: stream=%s maxlen=%s", self.stream, self.maxlen)
        while not self._stop:
            try:
                item = self.q.get(timeout=self.poll_s)
            except queue.Empty:
                continue
            self._process_item_failsoft(item)

    def flush_pending(self) -> None:
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            self._process_item_failsoft(item)

    def _process_item_failsoft(self, item: dict[str, Any]) -> None:
        try:
            self._write_failsoft(item)
        except Exception:
            self.process_error_count += 1
            logger.warning("event flusher item processing failed: stream=%s", self.stream, exc_info=True)

    def _write_failsoft(self, item: dict[str, Any]) -> None:
        try:
            kwargs = {}
            if self.maxlen is not None:
                kwargs["maxlen"] = self.maxlen
            self.redis.xadd(self.stream, item["fields"], **kwargs)
        except Exception as exc:
            if item.get("marker"):
                if self.on_marker_drop is not None:
                    self.on_marker_drop(item["fields"], exc)
                else:
                    logger.warning("event marker XADD failed: stream=%s", self.stream, exc_info=True)
                return
            if self.on_drop is not None:
                self.on_drop(item["fields"], exc)
            else:
                logger.warning("event XADD failed: stream=%s", self.stream, exc_info=True)

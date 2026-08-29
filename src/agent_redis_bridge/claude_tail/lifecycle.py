from __future__ import annotations

from .mapper import Event


class Lifecycle:
    def __init__(self) -> None:
        self._started = False

    def started(self) -> Event | None:
        if self._started:
            return None
        self._started = True
        return {"event_type": "task_started", "data": {}}

    def finished(self, *, ok: bool = True) -> Event:
        return {"event_type": "task_finished", "data": {"ok": ok}}

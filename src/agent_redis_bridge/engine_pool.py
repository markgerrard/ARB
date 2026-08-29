from __future__ import annotations

import threading
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class AffinityMissError(RuntimeError):
    def __init__(self, thread_id: str) -> None:
        super().__init__(thread_id)
        self.thread_id = thread_id


class AffinityBusyError(RuntimeError):
    def __init__(self, thread_id: str, owning_task_id: str) -> None:
        super().__init__(thread_id, owning_task_id)
        self.thread_id = thread_id
        self.owning_task_id = owning_task_id


class AffinityAmbiguousError(RuntimeError):
    def __init__(self, thread_id: str) -> None:
        super().__init__(thread_id)
        self.thread_id = thread_id


class EnginePool(Generic[T]):
    """Bounded pool of engine instances, one turn per engine at a time.

    Engines are spawned lazily up to ``max_size``. Each ``acquire(task_id)``
    returns an engine reserved against that task id, or ``None`` when the pool
    is at capacity. The caller must invoke ``release(task_id)`` when the turn
    finishes so the engine becomes available again.
    """

    def __init__(self, factory: Callable[[], T], max_size: int) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._factory = factory
        self._max_size = max_size
        self._lock = threading.Lock()
        self._idle: list[T] = []
        self._busy: dict[str, T | None] = {}
        self._reserved_started: set[str] = set()
        self._started: int = 0
        self._cap_cond = threading.Condition(self._lock)

    @property
    def max_size(self) -> int:
        return self._max_size

    @staticmethod
    def _engine_thread_id(engine: T) -> str | None:
        # Engine-owned ids are read lock-free by design: a race may miss a new
        # assignment and fail loud, but it must not bind to the wrong engine.
        for attr in ("thread_id", "session_id"):
            value = getattr(engine, attr, None)
            if isinstance(value, str) and value:
                return value
        return None

    def acquire(self, task_id: str, thread_id: str | None = None) -> T | None:
        with self._lock:
            if thread_id is not None:
                idle_matches: list[int] = []
                busy_matches: list[str] = []
                for index, engine in enumerate(self._idle):
                    if self._engine_thread_id(engine) == thread_id:
                        idle_matches.append(index)
                for owning_task_id, engine in self._busy.items():
                    if self._engine_thread_id(engine) == thread_id:
                        busy_matches.append(owning_task_id)
                if len(idle_matches) + len(busy_matches) > 1:
                    raise AffinityAmbiguousError(thread_id)
                if busy_matches:
                    raise AffinityBusyError(thread_id, busy_matches[0])
                if idle_matches:
                    engine = self._idle.pop(idle_matches[0])
                    self._busy[task_id] = engine
                    return engine
                raise AffinityMissError(thread_id)

            if self._idle:
                engine = self._idle.pop()
                self._busy[task_id] = engine
                return engine
            if self._started < self._max_size:
                engine = self._factory()
                start = getattr(engine, "start", None)
                if callable(start):
                    try:
                        start()
                    except BaseException:
                        # A failed start leaves the engine unregistered (not in
                        # _busy/_idle, _started unchanged), so stop_all() can
                        # never reach it — without this teardown every failed
                        # acquire leaked the already-spawned child process and
                        # reader thread, one fresh orphan per dispatch.
                        stop = getattr(engine, "stop", None)
                        if callable(stop):
                            try:
                                stop()
                            except Exception:
                                pass
                        raise
                self._started += 1
                self._busy[task_id] = engine
                return engine
            return None

    def reserve(self, task_id: str) -> bool:
        """Reserve a slot without constructing an engine.

        Scored worktree turns bind their cell tool plane only after the worktree exists,
        so the normal pooled factory must not run before that binding.
        """

        retired: T | None = None
        with self._lock:
            if task_id in self._busy:
                return False
            if self._started >= self._max_size and self._idle:
                retired = self._idle.pop()
                self._started -= 1
            if self._started >= self._max_size:
                return False
            self._started += 1
            self._reserved_started.add(task_id)
            self._busy[task_id] = None
        if retired is not None:
            stop = getattr(retired, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        return True

    def _has_capacity_unlocked(self) -> bool:
        return len(self._idle) > 0 or self._started < self._max_size

    def wait_for_capacity(self, timeout: float | None, stop_event: threading.Event | None = None) -> bool:
        def _ready() -> bool:
            return self._has_capacity_unlocked() or (stop_event is not None and stop_event.is_set())

        with self._cap_cond:
            if not _ready():
                self._cap_cond.wait_for(_ready, timeout=timeout)
            return self._has_capacity_unlocked()

    def release(self, task_id: str) -> None:
        stop_engine: T | None = None
        with self._lock:
            if task_id not in self._busy:
                return
            engine = self._busy.pop(task_id)
            if task_id in self._reserved_started:
                self._reserved_started.remove(task_id)
                self._started -= 1
                self._cap_cond.notify()
                return
            if engine is not None:
                is_healthy = getattr(engine, "is_healthy", None)
                if callable(is_healthy) and not is_healthy():
                    self._started -= 1
                    stop_engine = engine
                elif getattr(engine, "retire_after_turn", False):
                    self._started -= 1
                    stop_engine = engine
                else:
                    self._idle.append(engine)
            self._cap_cond.notify()
        if stop_engine is not None:
            stop = getattr(stop_engine, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass

    def get(self, task_id: str) -> T | None:
        """Return the engine currently bound to ``task_id`` (or ``None``).

        Used for routing control messages (steer/cancel) to the engine
        that owns the targeted task.
        """
        with self._lock:
            return self._busy.get(task_id)

    def active_task_ids(self) -> list[str]:
        with self._lock:
            return list(self._busy.keys())

    def stop_all(self) -> None:
        with self._lock:
            engines = [engine for engine in self._busy.values() if engine is not None] + list(self._idle)
            self._busy.clear()
            self._reserved_started.clear()
            self._idle.clear()
            self._cap_cond.notify_all()
        for engine in engines:
            stop = getattr(engine, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass

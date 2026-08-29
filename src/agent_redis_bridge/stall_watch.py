from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Callable


PROGRESS_EVENTS = frozenset(
    {
        "command_started",
        "tool_call",
        "command_output",
        "command_finished",
        "tool_output",
        "model_text",
        "model_thinking",
        # agent-sdk background-task hold: interim result + 60s keepalive while waiting.
        "turn_continued",
    }
)


@dataclass(frozen=True)
class StallEpisode:
    task_id: str
    stalled_for_secs: int


@dataclass(frozen=True)
class BlindEpisode:
    """A blind (unproven progress channel) task crossed the threshold.

    Not a stall claim: the channel has produced no proof of light, so quiet is
    indistinguishable from dark (AGY-2 design v2.1). Surfaced as `stall_unknown`
    + `progress_blind`, never as `stall_detected`/notify/stderr.
    """

    task_id: str
    reason: str
    unproven_for_secs: int


@dataclass(frozen=True)
class StallResume:
    task_id: str


@dataclass
class _TaskStallState:
    last_progress_ts: float
    stalled: bool = False
    blind: bool = False
    blind_reason: str | None = None
    blind_reported: bool = False


class StallWatch:
    def __init__(self, *, after_secs: int) -> None:
        self.after_secs = max(0, int(after_secs))
        self._tasks: dict[str, _TaskStallState] = {}
        self._lock = RLock()

    @property
    def enabled(self) -> bool:
        return self.after_secs > 0

    def start(self, task_id: str, *, now: float, blind: bool = False, blind_reason: str | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._tasks[task_id] = _TaskStallState(
                last_progress_ts=now,
                blind=blind,
                blind_reason=blind_reason if blind else None,
            )

    def progress(self, task_id: str, event: str, *, now: float) -> StallResume | None:
        if not self.enabled or event not in PROGRESS_EVENTS:
            return None
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                self._tasks[task_id] = _TaskStallState(last_progress_ts=now)
                return None
            was_stalled = state.stalled
            was_blind = state.blind
            state.last_progress_ts = now
            state.stalled = False
            state.blind = False
            state.blind_reason = None
            state.blind_reported = False
            return StallResume(task_id=task_id) if (was_stalled or was_blind) else None

    def mark_blind(self, task_id: str, reason: str, *, now: float) -> bool:
        """Mark the task's progress channel unproven (dark).

        Returns True when it is safe for the caller to clear a STALE stall
        marker (no fired episode is active); an active fired `stalled_at` must
        NOT be retracted by going dark (v2.1 — the alarm was earned while the
        channel was proven live). Re-blinding never resets the progress clock.
        """
        if not self.enabled:
            return False
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                state = _TaskStallState(last_progress_ts=now)
                self._tasks[task_id] = state
            already_blind = state.blind
            state.blind = True
            state.blind_reason = reason
            if not already_blind and not state.stalled:
                # new blind episode (was lit): allow one fresh report
                state.blind_reported = False
            return not state.stalled

    def is_blind(self, task_id: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            state = self._tasks.get(task_id)
            return bool(state and state.blind)

    def check(
        self,
        task_id: str,
        *,
        now: float,
        before_mark: Callable[[], None] | None = None,
    ) -> StallEpisode | BlindEpisode | None:
        if not self.enabled:
            return None
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None or state.stalled:
                return None
            if state.blind and state.blind_reported:
                return None
            stalled_for = now - state.last_progress_ts
            if stalled_for <= self.after_secs:
                return None
            if before_mark is not None:
                before_mark()
                state = self._tasks.get(task_id)
                if state is None or state.stalled:
                    return None
                if state.blind and state.blind_reported:
                    return None
                stalled_for = now - state.last_progress_ts
                if stalled_for <= self.after_secs:
                    return None
            if state.blind:
                # Unproven channel: quiet is not evidence of a stall. Report the
                # blindness once per blind episode instead of alarming.
                state.blind_reported = True
                return BlindEpisode(
                    task_id=task_id,
                    reason=state.blind_reason or "unproven",
                    unproven_for_secs=int(stalled_for),
                )
            state.stalled = True
            return StallEpisode(task_id=task_id, stalled_for_secs=int(stalled_for))

    def unmark_blind_report(self, task_id: str) -> None:
        """Revert check()'s blind_reported latch so the next tick re-reports.

        Emission-failure path only (GAP-1 parity): check() latches before the
        caller's side effects, so a failed `stall_unknown` emission must unmark
        or the blind episode is silently lost.
        """
        with self._lock:
            state = self._tasks.get(task_id)
            if state is not None:
                state.blind_reported = False

    def is_blind_reported(self, task_id: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            state = self._tasks.get(task_id)
            return bool(state and state.blind and state.blind_reported)

    def unmark(self, task_id: str) -> None:
        """Revert check()'s stalled mark so the next tick re-detects.

        For the caller's emission-failure path only: check() flips `stalled`
        before the caller performs its side effects, so a failed emission must
        unmark or the episode is silently lost for its whole duration.
        """
        with self._lock:
            state = self._tasks.get(task_id)
            if state is not None:
                state.stalled = False

    def is_stalled(self, task_id: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            state = self._tasks.get(task_id)
            return bool(state and state.stalled)

    def end(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

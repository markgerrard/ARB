"""Single-cell-at-a-time pilot/full-matrix execution machinery."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .phases import AttemptOutcome, PilotError, PilotSeal, _canonical_bytes, _outcome, _schedule, evaluate_stop_rules
from .cell_runtime import attempt_id_for
from .dispatch import ScoredDispatchBinding


class RunnerError(ValueError):
    pass


@dataclass(frozen=True)
class MatrixResult:
    outcomes: tuple[AttemptOutcome, ...]
    stopped_pairs: frozenset[str]
    complete: bool


class MatrixRunner:
    def __init__(
        self,
        manifest: Mapping[str, Any],
        *,
        pilot_seal: PilotSeal,
        execute: Callable[[Any, str], Any],
        append_attempt: Callable[[AttemptOutcome], Any] | None = None,
        stop_observation: Callable[[Any], Mapping[str, Any]] | None = None,
        close_cell: Callable[[AttemptOutcome], Any] | None = None,
        freeze_final: Callable[[tuple[AttemptOutcome, ...]], Any] | None = None,
        config_bytes: bytes | None = None,
        refs: Iterable[tuple[str, str]] | None = None,
        journal_tail: bytes | None = None,
        max_same_cause_failures: int = 3,
    ) -> None:
        self.manifest = manifest
        self.pilot_seal = pilot_seal
        self.execute = execute
        self.append_attempt = append_attempt
        self.stop_observation = stop_observation
        self.close_cell = close_cell
        self.freeze_final = freeze_final
        self.config_bytes = config_bytes
        self.refs = tuple(refs) if refs is not None else None
        self.journal_tail = bytes(journal_tail) if journal_tail is not None else None
        self.max_same_cause_failures = max_same_cause_failures
        if max_same_cause_failures < 1:
            raise RunnerError("max_same_cause_failures must be positive")

    def _validate_pilot(self) -> None:
        try:
            self.pilot_seal.validate()
        except PilotError as exc:
            raise RunnerError(str(exc)) from exc
        expected_manifest = hashlib.sha256(_canonical_bytes(self.manifest)).hexdigest()
        if self.pilot_seal.manifest_identity_digest != expected_manifest:
            raise RunnerError("pilot seal manifest changed")
        if self.config_bytes is not None or self.refs is not None or self.journal_tail is not None:
            if self.config_bytes is None or self.refs is None or self.journal_tail is None:
                raise RunnerError("pilot seal inputs are incomplete")
            from .phases import pilot_seal_digest

            if pilot_seal_digest(self.pilot_seal.manifest_bytes, self.config_bytes, self.refs, self.journal_tail) != self.pilot_seal.digest:
                raise RunnerError("pilot seal config, refs, or journal changed")
        expected = tuple(_schedule(self.manifest)[:32])
        actual: list[int] = []
        for outcome in self.pilot_seal.outcomes:
            if outcome.schedule_index not in actual:
                actual.append(outcome.schedule_index)
        if tuple(actual) != tuple(cell.schedule_index for cell in expected):
            raise RunnerError("pilot seal schedule is not repetition 1 in frozen order")

    def run(self) -> MatrixResult:
        self._validate_pilot()
        cells = _schedule(self.manifest)
        outcomes = list(self.pilot_seal.outcomes)
        stopped: set[str] = set()
        failures: dict[tuple[str, str], int] = {}
        for cell in cells[32:]:
            if cell.pair in stopped:
                continue
            observation = self.stop_observation(cell) if self.stop_observation is not None else {}
            try:
                reasons = evaluate_stop_rules(observation)
            except Exception as exc:  # noqa: BLE001 - stop machinery fails closed
                raise RunnerError(f"stop-rule evaluation failed: {exc}") from exc
            if reasons:
                stopped.add(cell.pair)
                continue
            attempt_id = attempt_id_for(cell.cell_id, 1)
            outcome = _outcome(self.execute(cell, attempt_id), cell, attempt_id)
            if self.append_attempt is not None:
                self.append_attempt(outcome)
            outcomes.append(outcome)
            if self.close_cell is not None:
                self.close_cell(outcome)
            if outcome.status == "UNKNOWN" and outcome.infrastructure:
                key = (cell.pair, outcome.cause or "unknown")
                failures[key] = failures.get(key, 0) + 1
                if failures[key] >= self.max_same_cause_failures:
                    stopped.add(cell.pair)
        complete = not stopped and len({outcome.cell_id for outcome in outcomes}) == 128 and {outcome.schedule_index for outcome in outcomes} == set(range(128))
        if complete:
            if self.freeze_final is not None:
                self.freeze_final(tuple(outcomes))
        return MatrixResult(tuple(outcomes), frozenset(stopped), complete)


def run_matrix(manifest: Mapping[str, Any], *, pilot_seal: PilotSeal, execute: Callable[[Any, str], Any], append_attempt: Callable[[AttemptOutcome], Any] | None = None, stop_observation: Callable[[Any], Mapping[str, Any]] | None = None, close_cell: Callable[[AttemptOutcome], Any] | None = None, freeze_final: Callable[[tuple[AttemptOutcome, ...]], Any] | None = None, config_bytes: bytes | None = None, refs: Iterable[tuple[str, str]] | None = None, journal_tail: bytes | None = None, max_same_cause_failures: int = 3) -> MatrixResult:
    """Production entry point; all model/provider work remains an injected runtime boundary."""

    return MatrixRunner(
        manifest,
        pilot_seal=pilot_seal,
        execute=execute,
        append_attempt=append_attempt,
        stop_observation=stop_observation,
        close_cell=close_cell,
        freeze_final=freeze_final,
        config_bytes=config_bytes,
        refs=refs,
        journal_tail=journal_tail,
        max_same_cause_failures=max_same_cause_failures,
    ).run()


def run_full_matrix(manifest: Mapping[str, Any], *, runtime: Any | None = None) -> MatrixResult:
    """Production CLI body; it refuses to invent a pilot seal or executor."""

    if runtime is None:
        raise RunnerError("production matrix runtime is not bound")
    pilot_seal = getattr(runtime, "pilot_seal", None)
    execute = getattr(runtime, "scored_dispatch", None)
    if not isinstance(pilot_seal, PilotSeal) or not isinstance(execute, ScoredDispatchBinding):
        raise RunnerError("production matrix runtime is not bound to a ScoredDispatchBinding")
    append_attempt = getattr(runtime, "append_attempt", None)
    stop_observation = getattr(runtime, "stop_observation", None)
    close_cell = getattr(runtime, "close_cell", None)
    freeze_final = getattr(runtime, "freeze_final", None)
    return run_matrix(
        manifest,
        pilot_seal=pilot_seal,
        execute=execute,
        append_attempt=append_attempt if callable(append_attempt) else None,
        stop_observation=stop_observation if callable(stop_observation) else None,
        close_cell=close_cell if callable(close_cell) else None,
        freeze_final=freeze_final if callable(freeze_final) else None,
        config_bytes=getattr(runtime, "config_bytes", None),
        refs=getattr(runtime, "refs", None),
        journal_tail=getattr(runtime, "journal_tail", None),
    )


__all__ = ["MatrixResult", "MatrixRunner", "RunnerError", "run_full_matrix", "run_matrix"]

"""Calibration and pilot phase contracts for the isolated bakeoff.

The phase functions own ordering and append-only rules.  Cell creation, provider calls, and
evidence writes are deliberately injected at the boundary so hermetic tests cannot accidentally
reach a live seat.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schedule import ScheduleCell, expand_schedule
from .cell_runtime import attempt_id_for


class CalibrationError(ValueError):
    pass


class PilotError(ValueError):
    pass


class StopRuleError(ValueError):
    pass


CLUSTERS = tuple(f"C{i}" for i in range(1, 8))
CALIBRATION_ARMS = ("glm-pi", "glm-zcode", "kimi-pi", "kimi-cli")
STOP_RULES = (
    "wrong-pin",
    "context-reuse",
    "hidden-key-exposure",
    "fixture-sha-mismatch",
    "write-outside-worktree",
    "source-drift",
    "malformed-evidence",
    "discarded-provider-error",
    "unknown-reasoning",
    "three-infrastructure-failures",
)

_STOP_FIELDS = {
    "wrong_pin",
    "wrong_model",
    "wrong_provider",
    "wrong_harness",
    "wrong_engine_version",
    "context_reuse",
    "hidden_key_exposure",
    "fixture_sha_mismatch",
    "write_outside_worktree",
    "source_drift",
    "malformed_ndjson",
    "discarded_provider_error",
    "unknown_reasoning",
    "reasoning_unknown",
    "reasoning_unequal",
    "reasoning_non_medium",
    "reasoning_echo_only",
    "reasoning_effective",
    "infrastructure_failures",
    "pair",
}


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PilotError(f"phase input is not canonical JSON: {exc}") from exc


def _framed(parts: Iterable[bytes]) -> bytes:
    output = bytearray(b"implbench-phase-v1\x00")
    for part in parts:
        output.extend(len(part).to_bytes(8, "big"))
        output.extend(part)
    return bytes(output)


def _manifest_tasks(manifest: Mapping[str, Any]) -> list[tuple[str, str]]:
    tasks = manifest.get("tasks")
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        raise PilotError("manifest tasks are missing")
    result: list[tuple[str, str]] = []
    for task in tasks:
        if not isinstance(task, Mapping) or not isinstance(task.get("task_id"), str) or not isinstance(task.get("fixture_sha"), str):
            raise PilotError("manifest task pin is invalid")
        result.append((task["task_id"], task["fixture_sha"]))
    try:
        if len(result) != 8:
            raise ValueError
        return result
    except ValueError as exc:
        raise PilotError("pilot requires exactly eight pinned tasks") from exc


def _schedule(manifest: Mapping[str, Any]) -> tuple[ScheduleCell, ...]:
    seed = manifest.get("seed")
    if not isinstance(seed, str):
        raise PilotError("manifest schedule seed is missing")
    try:
        return expand_schedule(seed, _manifest_tasks(manifest))
    except Exception as exc:  # noqa: BLE001 - turn schedule failures into a phase error
        raise PilotError(f"manifest schedule is invalid: {exc}") from exc


def _passed(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, Mapping):
        return value.get("passed") is True or value.get("status") == "PASS"
    return False


def _empty_artifacts(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return True
    for key in ("refs", "results", "scored_refs", "scored_results"):
        raw = value.get(key, ())
        if raw:
            return False
    return True


def _required_method(cell_factory: Callable[[], Any], names: tuple[str, ...]) -> Callable[..., Any]:
    def invoke(*args: Any) -> Any:
        cell = cell_factory()
        method = next((getattr(cell, name, None) for name in names if callable(getattr(cell, name, None))), None)
        if method is None:
            raise CalibrationError("calibration callbacks are not bound to a production cell runtime")
        try:
            return method(*args)
        except TypeError:
            return method()

    return invoke


@dataclass(frozen=True)
class CalibrationResult:
    accepted: bool
    cleared_clusters: frozenset[str]
    scored_refs: tuple[str, ...] = ()
    scored_results: tuple[str, ...] = ()
    unscored_arms: tuple[str, ...] = ()


class CalibrationPhase:
    def __init__(
        self,
        manifest: Mapping[str, Any],
        cell_factory: Callable[[], Any],
        *,
        hermetic_suite: Callable[[Mapping[str, Any]], Any] | None = None,
        adversarial_validation: Callable[[Mapping[str, Any]], Any] | None = None,
        known_good: Callable[[Mapping[str, Any], str, Callable[[], Any]], Any] | None = None,
        unscored: Callable[[Mapping[str, Any], str, str, Callable[[], Any]], Any] | None = None,
    ) -> None:
        self.manifest = manifest
        self.cell_factory = cell_factory
        self.hermetic_suite = hermetic_suite or _required_method(cell_factory, ("hermetic_suite", "run_hermetic_suite"))
        self.adversarial_validation = adversarial_validation or _required_method(cell_factory, ("adversarial_validation", "run_adversarial_validation"))
        self.known_good = known_good or _required_method(cell_factory, ("known_good", "run_known_good"))
        self.unscored = unscored or _required_method(cell_factory, ("unscored", "run_unscored"))

    def run(self) -> CalibrationResult:
        if not _passed(self.hermetic_suite(self.manifest)):
            raise CalibrationError("hermetic calibration suite did not pass")
        if not _passed(self.adversarial_validation(self.manifest)):
            raise CalibrationError("adversarial calibration validation did not pass")

        tasks = self.manifest.get("tasks", ())
        by_cluster: dict[str, list[str]] = {cluster: [] for cluster in CLUSTERS}
        for task in tasks:
            if isinstance(task, Mapping) and task.get("cluster") in by_cluster:
                by_cluster[task["cluster"]].append(str(task["task_id"]))
        cleared: set[str] = set()
        for cluster in CLUSTERS:
            if not by_cluster[cluster]:
                raise CalibrationError(f"no pinned task clears {cluster}")
            result = self.known_good(self.manifest, cluster, self.cell_factory)
            if not _passed(result):
                raise CalibrationError(f"known-good calibration did not clear {cluster}")
            cleared.add(cluster)

        task_id = str(tasks[0]["task_id"]) if tasks and isinstance(tasks[0], Mapping) else ""
        if not task_id:
            raise CalibrationError("unscored calibration task is missing")
        used: list[str] = []
        for arm in CALIBRATION_ARMS:
            result = self.unscored(self.manifest, task_id, arm, self.cell_factory)
            if not _passed(result) and not (isinstance(result, Mapping) and result.get("scored") is False):
                raise CalibrationError(f"unscored calibration failed for {arm}")
            if not _empty_artifacts(result):
                raise CalibrationError("calibration produced scored refs or results")
            used.append(arm)

        evidence = self.manifest.get("evidence")
        if isinstance(evidence, Mapping):
            root = Path(str(evidence.get("root", "")))
            if (root / "git-refs.txt").exists():
                raise CalibrationError("calibration cannot run against a finalized evidence package")
        return CalibrationResult(True, frozenset(cleared), unscored_arms=tuple(used))


def known_good_calibration(manifest: Mapping[str, Any], cell_factory: Callable[[], Any]) -> CalibrationResult:
    """Production callback bound to Gate 14; absent runtime methods fail closed."""

    return CalibrationPhase(manifest, cell_factory).run()


def run_calibration(manifest: Mapping[str, Any], seat: str | None = None, *, runtime: Any | None = None) -> CalibrationResult:
    """Run the production calibration body after a controller-owned runtime is bound."""

    if runtime is None:
        raise CalibrationError("production calibration runtime is not bound")
    cell_factory = getattr(runtime, "cell_factory", None)
    if not callable(cell_factory):
        raise CalibrationError("production calibration runtime has no cell factory")
    callbacks = {name: getattr(runtime, name, None) for name in ("hermetic_suite", "adversarial_validation", "known_good", "unscored")}
    if any(not callable(callback) for callback in callbacks.values()):
        raise CalibrationError("production calibration runtime callbacks are incomplete")
    del seat
    return CalibrationPhase(
        manifest,
        cell_factory,
        hermetic_suite=callbacks["hermetic_suite"],
        adversarial_validation=callbacks["adversarial_validation"],
        known_good=callbacks["known_good"],
        unscored=callbacks["unscored"],
    ).run()


@dataclass(frozen=True)
class AttemptOutcome:
    cell_id: str
    attempt_id: str
    status: str
    infrastructure: bool
    cause: str | None = None
    pair: str = ""
    arm: str = ""
    task_id: str = ""
    repetition: int = 0
    schedule_index: int = -1

    def with_cell(self, cell: ScheduleCell) -> "AttemptOutcome":
        return AttemptOutcome(self.cell_id, self.attempt_id, self.status, self.infrastructure, self.cause, cell.pair, cell.arm, cell.task_id, cell.repetition, cell.schedule_index)


@dataclass(frozen=True)
class PilotSeal:
    digest: str
    final_index_present: bool
    manifest_bytes: bytes = b""
    config_bytes: bytes = b""
    refs: tuple[tuple[str, str], ...] = ()
    journal_tail: bytes = b""
    outcomes: tuple[AttemptOutcome, ...] = ()
    manifest_identity_digest: str = ""

    def validate(self) -> None:
        if self.final_index_present:
            raise PilotError("pilot seal must not contain final refs")
        if len(self.digest) != 64 or any(char not in "0123456789abcdef" for char in self.digest):
            raise PilotError("pilot seal digest is invalid")
        if not self.manifest_bytes and not self.config_bytes and not self.journal_tail and not self.outcomes:
            raise PilotError("pilot seal is not verifiable")
        expected = pilot_seal_digest(self.manifest_bytes, self.config_bytes, self.refs, self.journal_tail)
        if expected != self.digest:
            raise PilotError("pilot seal digest changed")
        indices = [outcome.schedule_index for outcome in self.outcomes]
        first_indices: list[int] = []
        for index in indices:
            if index not in first_indices:
                first_indices.append(index)
        if len(first_indices) != 32 or first_indices != list(range(32)):
            raise PilotError("pilot seal does not contain repetition 1 exactly once")


@dataclass(frozen=True)
class PilotResult:
    outcomes: tuple[AttemptOutcome, ...]
    seal: PilotSeal


def pilot_seal_digest(manifest_bytes: bytes, config_bytes: bytes, refs: Iterable[tuple[str, str]], journal_tail: bytes) -> str:
    rows = sorted((str(ref), str(oid)) for ref, oid in refs)
    return hashlib.sha256(_framed((bytes(manifest_bytes), bytes(config_bytes), _canonical_bytes(rows), bytes(journal_tail)))).hexdigest()


def _outcome(value: Any, cell: ScheduleCell, attempt_id: str) -> AttemptOutcome:
    if isinstance(value, AttemptOutcome):
        return value.with_cell(cell)
    if not isinstance(value, Mapping):
        raise PilotError("cell executor returned an invalid outcome")
    status = value.get("status")
    if not isinstance(status, str) or not status:
        raise PilotError("cell outcome status is missing")
    return AttemptOutcome(cell.cell_id, attempt_id, status, bool(value.get("infrastructure", False)), value.get("cause"), cell.pair, cell.arm, cell.task_id, cell.repetition, cell.schedule_index)


class PilotPhase:
    def __init__(
        self,
        manifest: Mapping[str, Any],
        *,
        execute: Callable[[ScheduleCell, str], Any],
        append_attempt: Callable[[AttemptOutcome], Any],
        manifest_bytes: bytes,
        config_bytes: bytes,
        refs: Iterable[tuple[str, str]],
        journal_tail: bytes,
        final_index_present: bool = False,
        stop_observation: Callable[[ScheduleCell], Mapping[str, Any]] | None = None,
        max_same_cause_failures: int = 3,
    ) -> None:
        self.manifest = manifest
        self.execute = execute
        self.append_attempt = append_attempt
        self.manifest_bytes = bytes(manifest_bytes)
        self.config_bytes = bytes(config_bytes)
        self.refs = tuple(refs)
        self.journal_tail = bytes(journal_tail)
        self.final_index_present = final_index_present
        self.stop_observation = stop_observation
        self.max_same_cause_failures = max_same_cause_failures
        if max_same_cause_failures < 1:
            raise PilotError("max_same_cause_failures must be positive")

    def run(self) -> PilotResult:
        if self.final_index_present:
            raise PilotError("pilot cannot run with final refs")
        cells = _schedule(self.manifest)[:32]
        outcomes: list[AttemptOutcome] = []
        stopped: set[str] = set()
        failures: dict[tuple[str, str], int] = {}
        for cell in cells:
            if cell.pair in stopped:
                continue
            if self.stop_observation is not None:
                try:
                    if evaluate_stop_rules(self.stop_observation(cell)):
                        stopped.add(cell.pair)
                        continue
                except StopRuleError as exc:
                    raise PilotError(f"stop-rule evaluation failed: {exc}") from exc
            attempt_number = 1
            while True:
                attempt_id = attempt_id_for(cell.cell_id, attempt_number)
                outcome = _outcome(self.execute(cell, attempt_id), cell, attempt_id)
                self.append_attempt(outcome)
                outcomes.append(outcome)
                if outcome.status == "UNKNOWN" and outcome.infrastructure:
                    key = (cell.pair, outcome.cause or "unknown")
                    failures[key] = failures.get(key, 0) + 1
                    if failures[key] >= self.max_same_cause_failures:
                        stopped.add(cell.pair)
                if not (outcome.status == "UNKNOWN" and outcome.infrastructure and attempt_number == 1):
                    break
                attempt_number += 1
        seal = PilotSeal(
            pilot_seal_digest(self.manifest_bytes, self.config_bytes, self.refs, self.journal_tail),
            False,
            self.manifest_bytes,
            self.config_bytes,
            tuple(sorted(self.refs)),
            self.journal_tail,
            tuple(outcomes),
            hashlib.sha256(_canonical_bytes(self.manifest)).hexdigest(),
        )
        return PilotResult(tuple(outcomes), seal)


def run_pilot(manifest: Mapping[str, Any], *, runtime: Any | None = None) -> PilotResult:
    """Run the production pilot body with a controller-owned execution runtime."""

    if runtime is None:
        raise PilotError("production pilot runtime is not bound")
    required = ("scored_dispatch", "append_attempt", "manifest_bytes", "config_bytes", "refs", "journal_tail")
    from .dispatch import ScoredDispatchBinding

    if any(not hasattr(runtime, name) for name in required) or not isinstance(getattr(runtime, "scored_dispatch", None), ScoredDispatchBinding) or not callable(getattr(runtime, "append_attempt", None)):
        raise PilotError("production pilot runtime is incomplete")
    return PilotPhase(
        manifest,
        execute=runtime.scored_dispatch,
        append_attempt=runtime.append_attempt,
        manifest_bytes=runtime.manifest_bytes,
        config_bytes=runtime.config_bytes,
        refs=runtime.refs,
        journal_tail=runtime.journal_tail,
        final_index_present=bool(getattr(runtime, "final_index_present", False)),
        stop_observation=getattr(runtime, "stop_observation", None),
        max_same_cause_failures=int(getattr(runtime, "max_same_cause_failures", 3)),
    ).run()


def evaluate_stop_rules(observation: Mapping[str, Any]) -> tuple[str, ...]:
    if not isinstance(observation, Mapping) or set(observation) - _STOP_FIELDS:
        unknown = sorted(set(observation) - _STOP_FIELDS) if isinstance(observation, Mapping) else ["observation"]
        raise StopRuleError(f"unknown stop-rule observation: {unknown}")
    hits: list[str] = []
    if any(observation.get(field) is True for field in ("wrong_pin", "wrong_model", "wrong_provider", "wrong_harness", "wrong_engine_version")):
        hits.append("wrong-pin")
    for field, reason in (
        ("context_reuse", "context-reuse"),
        ("hidden_key_exposure", "hidden-key-exposure"),
        ("fixture_sha_mismatch", "fixture-sha-mismatch"),
        ("write_outside_worktree", "write-outside-worktree"),
        ("source_drift", "source-drift"),
        ("malformed_ndjson", "malformed-evidence"),
        ("discarded_provider_error", "discarded-provider-error"),
        ("unknown_reasoning", "unknown-reasoning"),
    ):
        if observation.get(field) is True:
            hits.append(reason)
    if any(observation.get(field) is True for field in ("reasoning_unknown", "reasoning_unequal", "reasoning_non_medium", "reasoning_echo_only")):
        hits.append("unknown-reasoning")
    if "reasoning_effective" in observation and observation["reasoning_effective"] != "medium":
        hits.append("unknown-reasoning")
    failures = observation.get("infrastructure_failures", ())
    if not isinstance(failures, Sequence) or isinstance(failures, (str, bytes)):
        raise StopRuleError("infrastructure_failures must be a sequence")
    counts: dict[str, int] = {}
    for cause in failures:
        if not isinstance(cause, str) or not cause:
            raise StopRuleError("infrastructure failure causes must be non-empty strings")
        counts[cause] = counts.get(cause, 0) + 1
    if any(count >= 3 for count in counts.values()):
        hits.append("three-infrastructure-failures")
    return tuple(reason for reason in STOP_RULES if reason in hits)

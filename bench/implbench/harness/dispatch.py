"""Thin dispatch boundary for ordinary CLI calls and scored cell attempts."""

from __future__ import annotations

import json
import hashlib
import inspect
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .controller import Controller, merge_runtime_context
from .cell_runtime import attempt_id_for
from .ref_protection import write_bakeoff_ref
from .schedule import ScheduleCell, cell_suffix
from .tasks import Task, static_prefix


@dataclass(frozen=True)
class DispatchResult:
    status: str
    timed_out: bool = False
    structured: dict[str, Any] = field(default_factory=dict)
    completion: dict[str, Any] = field(default_factory=dict)
    text: str = ""


def _merge_completion_projection(target: dict[str, Any], value: Mapping[str, Any]) -> None:
    merge_runtime_context(target, value)


def _attempt_bound(callback: Any, cell: ScheduleCell, attempt_id: str) -> Any:
    if not callable(callback):
        return None
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "attempt_id" in parameters or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return callback(cell, attempt_id=attempt_id)
    return callback(cell)


def glob_to_prefix(glob: str) -> str:
    return static_prefix(glob)


def run_task(
    task: Task,
    seat: str,
    engine: str,
    fixture_sha: str,
    run_id: str,
    repo: Path,
    evidence_log: Any | None = None,
    recorder: Any | None = None,
    *,
    schedule_cell: ScheduleCell | None = None,
    attempt_number: int = 1,
    scored_runtime: Any | None = None,
    scored_runtime_factory: Any | None = None,
    scored_lifecycle: Any | None = None,
    fixture_root_oid: str | None = None,
    tool_gid: int | None = None,
    cell_root: str | Path | None = None,
) -> DispatchResult:
    """Dispatch exactly once; scored close/classification is receipt-driven."""

    scored = run_id.startswith("oi-pi-bakeoff-")
    if scored:
        if not isinstance(schedule_cell, ScheduleCell):
            raise ValueError("scored dispatch requires a schedule-derived cell")
        if schedule_cell.task_id != task.task_id or schedule_cell.fixture_sha != fixture_sha:
            raise ValueError("schedule cell does not bind task fixture")
        cell_id = schedule_cell.cell_id
        attempt_id = attempt_id_for(cell_id, attempt_number)
        if not isinstance(fixture_root_oid, str) or not re.fullmatch(r"[0-9a-f]{40}", fixture_root_oid):
            raise ValueError("scored dispatch requires an explicit canonical fixture root OID")
        if isinstance(tool_gid, bool) or not isinstance(tool_gid, int) or tool_gid <= 0:
            raise ValueError("scored dispatch requires a positive controller tool GID")
        if not callable(scored_runtime_factory):
            raise ValueError("scored dispatch requires a scored runtime factory")
        if scored_runtime is not None:
            raise ValueError("scored dispatch rejects a pre-opened runtime")
        if cell_root is None:
            raise ValueError("scored dispatch requires a controller-provisioned cell root")
        cell_root_path = Path(cell_root)
        if not cell_root_path.is_absolute():
            raise ValueError("scored cell root must be absolute")
        try:
            canonical_cell_root = cell_root_path.resolve(strict=True)
            canonical_repo = repo.resolve(strict=True)
        except OSError as exc:
            raise ValueError("scored cell root is unavailable") from exc
        mac_var_alias = cell_root_path == Path("/var") or (cell_root_path.is_relative_to(Path("/var")) and not cell_root_path.is_symlink())
        if (cell_root_path.is_symlink() and not mac_var_alias) or (canonical_cell_root != cell_root_path and not mac_var_alias) or not cell_root_path.is_dir():
            raise ValueError("scored cell root must be a canonical directory")
        try:
            canonical_cell_root.relative_to(canonical_repo / ".claude" / "worktrees")
        except ValueError:
            pass
        else:
            raise ValueError("scored dispatch rejects ordinary bridge worktrees")
        cell_root = canonical_cell_root
    else:
        cell_id = None
        attempt_id = None

    remote_binding: Mapping[str, str] | None = None
    if scored:
        opener = getattr(scored_lifecycle, "open_attempt_git_service", None)
        if not callable(opener):
            raise ValueError("scored dispatch requires a controller-owned attempt Git RPC service")
        remote_binding = opener(attempt_id, allowed_paths=tuple(task.allowed_paths))
        if not isinstance(remote_binding, Mapping) or set(remote_binding) != {"endpoint", "capability"}:
            raise ValueError("controller attempt Git RPC binding is malformed")

    try:
        if scored:
            starter = getattr(scored_lifecycle, "start_attempt_planes", None)
            dispatcher = getattr(scored_lifecycle, "dispatch_through_control", None)
            if not callable(starter) or not callable(dispatcher) or remote_binding is None:
                raise ValueError("scored dispatch requires the production control/tool plane lifecycle")
            starter(remote_binding)
            value = dispatcher(task, engine, timeout=task.timeout_s)
            if not isinstance(value, Mapping):
                raise ValueError("scored control plane returned a malformed result")
            result = DispatchResult(
                str(value.get("status", "failed")), bool(value.get("timed_out", False)),
                dict(value.get("structured", {})) if isinstance(value.get("structured"), Mapping) else {},
                dict(value.get("completion", {})) if isinstance(value.get("completion"), Mapping) else {},
                str(value.get("text", "")),
            )
        else:
            argv = _argv(task, seat, engine, fixture_sha, run_id)
            result = _dispatch(argv, timeout=task.timeout_s)
    finally:
        # The bridge may still be returning completion, but it cannot retain a usable tool
        # authority after the dispatcher exits.  Controller close owns the durable receipts.
        closer = getattr(scored_lifecycle, "close_attempt_git_service", None)
        if scored and callable(closer):
            closer()
    if not scored:
        return result

    # A scored run has no ordinary task/result refs and no host-Git completion fallback.  The
    # controller owns the cell/attempt ref once the authenticated Git service has produced it.
    completion = dict(result.completion)
    missing = _missing_scored_completion_fields(completion)
    projected = set(completion) & {"imported_oids", "imported_graph_attested", "post_g4_attestation", "scorer_result", "model_limit_proven"}
    if projected:
        missing = tuple(sorted(set(missing) | {"engine-projected-evidence"}))
    if missing:
        completion["infrastructure_failure"] = "incomplete-scored-completion"
    receipts = tuple(completion.get("receipt_oids", ()))
    journal_path = getattr(recorder, "path", None)
    if journal_path is not None:
        journal_path = Path(journal_path).with_suffix(".close.ndjson")
    else:
        # A scored close must remain recoverable after the cell is destroyed.  Keep this
        # compatibility path under the controller-supplied repository rather than using a
        # volatile platform-temp directory; production bindings always provide recorder.path.
        journal_path = repo / ".implbench-close" / f"{cell_id}.{attempt_id}.ndjson"
    context_failure = completion.get("infrastructure_failure")

    def open_runtime() -> Any:
        """Open the descriptor only after FINAL_STATUS and CENSUS_SNAPSHOT commit."""

        if scored_lifecycle is not None:
            projection = getattr(scored_lifecycle, "completion_projection", None)
            if callable(projection):
                value = projection()
                if isinstance(value, dict):
                    _merge_completion_projection(completion, value)
        return scored_runtime_factory(
            cell_id=cell_id,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            fixture_root_oid=fixture_root_oid,
            tool_gid=tool_gid,
            completion=completion,
            repo=repo,
        )

    close = Controller(
        journal_path,
        runtime=scored_lifecycle,
        close_context={
            "dispatch_status": result.status if not missing else "failed",
            "receipts": receipts,
            "imported_oids": (),
            "dirty": bool(completion.get("dirty", False)),
            "seal_complete": completion.get("seal_complete", False),
            "receipts_authenticated": completion.get("receipts_authenticated", False),
            "imported_graph_attested": False,
            "infrastructure_failure": context_failure,
            "dispatch_timed_out": result.timed_out,
        },
        runtime_factory=open_runtime,
        strict_lifecycle=True,
    )
    terminal = "completed" if result.status == "ok" else ("timeout" if result.timed_out or result.status == "timeout" else "dispatch-failed")
    try:
        close_result = close.close(terminal=terminal)
    except RuntimeError as exc:
        # A missing lifecycle/scorer callback is an infrastructure UNKNOWN.  The prepared
        # journal row remains uncommitted so recovery cannot mistake it for a completed phase.
        completion["infrastructure_failure"] = completion.get("infrastructure_failure") or "scored-close"
        completion["close_error"] = str(exc)
        completion["classification"] = {f"G{i}": "UNKNOWN" for i in range(8)}
        completion["cell_id"] = cell_id
        completion["attempt_id"] = attempt_id
        return DispatchResult(result.status, result.timed_out, result.structured, completion, result.text)
    completion["cell_id"] = cell_id
    completion["attempt_id"] = attempt_id
    completion["classification"] = dict(close_result.classification)
    if close.close_context.get("model_limit_proven") is True:
        completion["model_limit_proven"] = True
    else:
        completion.pop("model_limit_proven", None)
    completion.pop("imported_oids", None)
    completion.pop("imported_graph_attested", None)
    if evidence_log is None:
        evidence_log = recorder
    candidate = result.structured.get("record") if isinstance(result.structured, dict) else None
    if evidence_log is not None and isinstance(candidate, dict) and hasattr(evidence_log, "append"):
        evidence_log.append(candidate)
    return DispatchResult(result.status, result.timed_out, result.structured, completion, result.text)


@dataclass(frozen=True)
class ScoredDispatchBinding:
    """Matrix executor that carries controller metadata into :func:`run_task`.

    The controller supplies the per-cell task, seat, fixture-root OID, and tool GID.  Keeping
    this adapter at the production boundary prevents the matrix runner from silently falling
    back to an arbitrary host callback.
    """

    run_id: str
    repo: Path
    task_for_cell: Any
    seat_for_cell: Any
    engine_for_cell: Any
    fixture_root_oid_for_cell: Any
    tool_gid_for_cell: Any
    scored_runtime_factory: Any
    cell_root_for_cell: Any | None = None
    lifecycle_for_cell: Any | None = None
    evidence_log: Any | None = None
    recorder_for_cell: Any | None = None
    dispatch_fn: Any = run_task

    def __post_init__(self) -> None:
        if not self.run_id.startswith("oi-pi-bakeoff-"):
            raise ValueError("scored matrix binding requires a bakeoff run ID")
        if not isinstance(self.repo, Path):
            raise TypeError("scored matrix binding repo must be a Path")
        required = (
            self.task_for_cell,
            self.seat_for_cell,
            self.engine_for_cell,
            self.fixture_root_oid_for_cell,
            self.tool_gid_for_cell,
            self.scored_runtime_factory,
            self.dispatch_fn,
        )
        if any(not callable(value) for value in required):
            raise ValueError("scored matrix dispatch binding is incomplete")

    def __call__(self, cell: ScheduleCell, attempt_id: str) -> Any:
        from .phases import AttemptOutcome

        attempt_number = next(
            (number for number in range(1, 1025) if attempt_id_for(cell.cell_id, number) == attempt_id),
            None,
        )
        if attempt_number is None:
            raise ValueError("scored matrix binding requires an attempt derived from the cell identity")
        task = self.task_for_cell(cell)
        recorder = _attempt_bound(self.recorder_for_cell, cell, attempt_id)
        cell_root = _attempt_bound(self.cell_root_for_cell, cell, attempt_id)
        scored_lifecycle = _attempt_bound(self.lifecycle_for_cell, cell, attempt_id)
        result = self.dispatch_fn(
            task,
            self.seat_for_cell(cell),
            self.engine_for_cell(cell),
            cell.fixture_sha,
            self.run_id,
            self.repo,
            self.evidence_log,
            recorder,
            schedule_cell=cell,
            attempt_number=attempt_number,
            fixture_root_oid=self.fixture_root_oid_for_cell(cell),
            tool_gid=self.tool_gid_for_cell(cell),
            cell_root=cell_root,
            scored_lifecycle=scored_lifecycle,
            scored_runtime_factory=self.scored_runtime_factory,
        )
        classification = result.completion.get("classification", {}) if isinstance(result, DispatchResult) else {}
        if result.timed_out or result.status == "timeout":
            return AttemptOutcome(cell.cell_id, attempt_id, "UNKNOWN", True, cause="timeout")
        if not isinstance(classification, dict) or "G0" not in classification or "G2" not in classification:
            cause = result.completion.get("infrastructure_failure") if isinstance(result.completion, dict) else None
            return AttemptOutcome(cell.cell_id, attempt_id, "UNKNOWN", True, cause=cause or "incomplete-classification")
        # A controller-owned scorer proof is the sole exception to ordinary
        # UNKNOWN precedence: the submitted role actually hit its limit, while
        # later gates were intentionally not run.  Arbitrary mixed verdicts
        # remain infrastructure-unknown and retryable.
        if (result.completion.get("infrastructure_failure") is None
                and result.completion.get("model_limit_proven") is True
                and classification.get("G1") == "FAIL"
                and any(value == "UNKNOWN" for value in classification.values())):
            return AttemptOutcome(cell.cell_id, attempt_id, "FAIL", False, cause="submitted-model-limit")
        if any(value == "UNKNOWN" for value in classification.values()):
            cause = result.completion.get("infrastructure_failure") or "infrastructure-unknown"
            return AttemptOutcome(cell.cell_id, attempt_id, "UNKNOWN", True, cause=cause)
        if any(value == "FAIL" for value in classification.values()) or classification.get("G2") == "not-delivered":
            return AttemptOutcome(cell.cell_id, attempt_id, "FAIL", False)
        if result.status == "ok" and classification.get("G2") == "agent-delivered":
            return AttemptOutcome(cell.cell_id, attempt_id, "PASS", False)
        return AttemptOutcome(cell.cell_id, attempt_id, "UNKNOWN", True, cause="incomplete-classification")


def _argv(
    task: Task,
    seat: str,
    engine: str,
    fixture_sha: str,
    run_id: str,
    *,
    cell_id: str | None = None,
    attempt_id: str | None = None,
    fixture_root_oid: str | None = None,
    tool_gid: int | None = None,
    cell_root: str | Path | None = None,
    tool_endpoint: str | None = None,
    tool_capability: str | None = None,
) -> list[str]:
    suffix = cell_suffix(cell_id) if cell_id is not None else hashlib.sha256(f"{run_id}:{task.task_id}".encode()).hexdigest()[:12]
    scored = run_id.startswith("oi-pi-bakeoff-")
    argv = ["scripts/agent-dispatch", "--engine", engine, "--target-id", seat, "--run-id", run_id]
    if not scored:
        argv.extend((
            "--worktree", f"implbench-{task.task_id}-{suffix}", "--worktree-base", fixture_sha,
            "--worktree-cleanup", "keep",
        ))
    for artifact in task.expected_artifacts:
        argv.extend(("--expected-artifact", artifact))
    for glob in task.allowed_paths:
        argv.extend(("--allowed-path", glob if scored else glob_to_prefix(glob)))
    for flag, value in (
        ("--cell-id", cell_id),
        ("--attempt-id", attempt_id),
        ("--fixture-root-oid", fixture_root_oid),
        ("--tool-gid", str(tool_gid) if tool_gid is not None else None),
    ):
        if value is not None:
            argv.extend((flag, value))
    if scored:
        argv.extend(("--fresh-context", "--effort", "medium"))
        if cell_root is None:
            raise ValueError("scored dispatch requires a controller-provisioned cell root")
        argv.extend(("--cell-root", str(cell_root)))
        if tool_endpoint is not None:
            argv.extend(("--tool-endpoint", tool_endpoint))
        if tool_capability is not None:
            # The capability is deliberately not an argv or environment value.  The controller
            # gives the dispatcher a one-shot inherited descriptor; it alone turns it into the
            # trusted bus-envelope field.
            argv.extend(("--tool-capability-fd", "__IMPLBENCH_CAPABILITY_FD__"))
    argv.extend(("--expect-structured", task.brief))
    return argv


_SCORED_COMPLETION_FIELDS = frozenset({
    "mode", "ref_namespace", "receipt_oids", "dirty", "seal_complete",
    "receipts_authenticated", "infrastructure_failure",
})


def _missing_scored_completion_fields(completion: dict[str, Any]) -> tuple[str, ...]:
    """Validate the controller-owned scored completion projection without guessing defaults."""

    missing: list[str] = []
    if "mode" not in completion:
        missing.append("missing-mode")
    elif completion.get("mode") != "receipt-only":
        missing.append("invalid-mode")
    if "ref_namespace" not in completion:
        missing.append("missing-ref-namespace")
    elif completion.get("ref_namespace") != "cell-attempt":
        missing.append("invalid-ref-namespace")
    missing.extend(sorted(_SCORED_COMPLETION_FIELDS - set(completion) - {"mode", "ref_namespace"}))
    if missing:
        return tuple(missing)
    if not isinstance(completion.get("dirty"), bool) or not isinstance(completion.get("seal_complete"), bool):
        return ("seal-or-dirty-type",)
    if not isinstance(completion.get("receipts_authenticated"), bool):
        return ("auth-type",)
    receipt_oids = completion.get("receipt_oids")
    if not isinstance(receipt_oids, (list, tuple)) or any(
        not isinstance(oid, str) or not re.fullmatch(r"[0-9a-f]{40}", oid) for oid in receipt_oids
    ):
        return ("receipt-oids-type",)
    return ()


def _dispatch(argv: list[str], timeout: int, *, scored: bool = False, tool_capability: str | None = None) -> DispatchResult:
    if scored:
        raise ValueError("scored dispatch must cross the per-cell control plane")
    env = {key: value for key, value in os.environ.items() if key != "IMPLBENCH_BATTERY_KEY"}
    if scored:
        env["AGENT_AUTO_COMMIT"] = "0"
    read_fd: int | None = None
    try:
        command = list(argv)
        if tool_capability is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", tool_capability):
                raise ValueError("scored dispatch capability is malformed")
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, (tool_capability + "\n").encode("ascii"))
            finally:
                os.close(write_fd)
            try:
                index = command.index("__IMPLBENCH_CAPABILITY_FD__")
            except ValueError as exc:
                raise ValueError("scored dispatch lacks a capability descriptor slot") from exc
            command[index] = str(read_fd)
        elif "__IMPLBENCH_CAPABILITY_FD__" in command:
            raise ValueError("scored dispatch lacks controller capability authority")
        kwargs: dict[str, Any] = {"text": True, "capture_output": True, "timeout": timeout, "check": False, "env": env}
        if read_fd is not None:
            kwargs["pass_fds"] = (read_fd,)
        res = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        return DispatchResult("timeout", timed_out=True, text=stdout)
    finally:
        if read_fd is not None:
            os.close(read_fd)
    if res.returncode == 124:
        return DispatchResult("timeout", timed_out=True, text=res.stdout)
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(res.stdout) if res.stdout.strip().startswith("{") else {}
    except json.JSONDecodeError:
        pass
    return DispatchResult(
        "ok" if res.returncode == 0 else "failed",
        structured=payload.get("structured", {}),
        completion=payload.get("completion", {}),
        text=res.stdout,
    )


def scored_ref(repo: Path, *, run_id: str, cell_id: str, attempt_id: str, oid: str, result: bool = False) -> str:
    return write_bakeoff_ref(repo, "results" if result else "runs", run_id, cell_id, attempt_id, oid)

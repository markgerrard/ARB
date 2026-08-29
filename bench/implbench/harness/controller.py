"""Journal-driven close orchestration for scored Implementor Bench cells.

The controller owns the close boundary.  Model dispatch, importer, and scorer are never
re-entered after their journal commit; cleanup and absence probes are the only recovery work.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import inspect
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .classifier import Classification, ClassificationInput, classify, classify_provisional


def production_known_good_calibration(manifest: Mapping[str, Any], cell_factory: Callable[[], Any]) -> Any:
    """The controller-owned Gate 14 callback; no readiness fake is substituted."""

    from .phases import known_good_calibration

    return known_good_calibration(manifest, cell_factory)


class CloseState(str, Enum):
    ALLOCATED = "ALLOCATED"
    CLOSING = "CLOSING"
    CLASSIFYING = "CLASSIFYING"
    UNKNOWN_EVIDENCED = "UNKNOWN_EVIDENCED"
    NOT_DELIVERED = "NOT_DELIVERED"
    IMPORTED = "IMPORTED"
    SCORED = "SCORED"
    EVIDENCED = "EVIDENCED"
    DESTROYED = "DESTROYED"


class CloseCrash(RuntimeError):
    """Test-only injected abrupt close used to prove durable recovery points."""


# Keep the normative close order in one place.  The names are also the journal phase IDs,
# so a restart can identify the first uncommitted action without interpreting prose.
CLOSE_PHASES = (
    "STOP_TOOLS",
    "DRAIN_RPC",
    "KILL_PLANES",
    "CLOSE_ACL",
    "FINAL_STATUS",
    "KILL_GIT",
    "CENSUS_SNAPSHOT",
    "OPEN_DESCRIPTOR",
    "CLASSIFY",
    "IMPORT_SCORE",
    "EVIDENCE_DESTROY",
)

_LEGACY = frozenset({"create_run_ref", "create_result_ref", "evaluate_gate"})
_LOGICAL_PHASES = frozenset({"CLASSIFY"})


def merge_runtime_context(target: dict[str, Any], value: Mapping[str, Any]) -> None:
    """Merge lifecycle/bridge/import/recovery projections without losing evidence."""

    for key, projected in value.items():
        target_key = "receipts" if key in {"receipt_oids", "receipts"} else key
        if target_key == "receipts":
            existing = target.get(target_key)
            if existing and projected and tuple(existing) != tuple(projected):
                target["infrastructure_failure"] = "completion-projection-conflict"
                continue
            if existing:
                continue
            if projected:
                target[target_key] = tuple(projected)
            continue
        if target_key == "infrastructure_failure":
            existing = target.get(target_key)
            # RemoteGitService exposes this exact provisional value while the
            # controller still owns close.  It is not an observed failure.
            # All other infrastructure failures retain their sticky semantics.
            if projected == "awaiting-controller-close":
                continue
            if existing == "awaiting-controller-close" and projected is None:
                # A later controller-owned successful projection resolves the
                # provisional bridge placeholder.  Do not apply this exception
                # to observed infrastructure failures.
                target[target_key] = None
                continue
            if existing == "awaiting-controller-close":
                # A concrete controller observation supersedes the provisional
                # transport placeholder verbatim.
                target[target_key] = projected
                continue
            if existing is not None and projected is None:
                continue
            if existing is not None and projected is not None and projected != existing:
                target[target_key] = "completion-projection-conflict"
                continue
        if projected is not None and projected != () and projected != []:
            target[target_key] = projected


@dataclass(frozen=True)
class CensusHit:
    path: str
    line: int
    symbol: str
    kind: str


@dataclass(frozen=True)
class CloseResult:
    state: CloseState
    classification: Classification
    phases: tuple[str, ...]
    errors: tuple[str, ...] = ()


class ScoredCloseRuntime:
    """Controller-owned CompletionVerifier → importer → attestation → scorer chain."""

    def __init__(
        self,
        *,
        completion_verifier: Any,
        descriptor_importer: Callable[[Any], Any],
        attestation_verifier: Callable[[Any, Any], Any],
        scorer: Callable[[Any, Any], Any],
        receipts: list[Mapping[str, Any]],
        status: Mapping[str, Any],
        worktree: str | Path,
        lifecycle: Any | None = None,
        crash_injector: Callable[[str, int | None], None] | None = None,
    ) -> None:
        self.completion_verifier = completion_verifier
        self.descriptor_importer = descriptor_importer
        self.attestation_verifier = attestation_verifier
        self.scorer = scorer
        self.receipts = receipts
        self.status = status
        self.worktree = worktree
        self.lifecycle = lifecycle
        self.crash_injector = crash_injector
        self._source_fd: int | None = None
        self.requires_real_lifecycle = lifecycle is not None
        self.result_context: dict[str, Any] = {}
        self._delivery_result: Any | None = None

    @classmethod
    def from_descriptor(
        cls,
        *,
        completion_verifier: Any,
        source_fd: int,
        import_destination: str | Path,
        attestation_verifier: Callable[[Any, Any], Any],
        scorer: Callable[[Any, Any], Any],
        receipts: list[Mapping[str, Any]],
        status: Mapping[str, Any],
        worktree: str | Path,
        candidate_ref: str | None = None,
        importer_runner: Callable[[int, str | Path, str | None], Any] | None = None,
        lifecycle: Any | None = None,
        crash_injector: Callable[[str, int | None], None] | None = None,
    ) -> "ScoredCloseRuntime":
        from .importer import import_from_descriptor

        held_fd = os.dup(source_fd)

        def import_once(payload: Any) -> Any:
            del payload
            duplicate = os.dup(held_fd)
            try:
                if importer_runner is not None:
                    return importer_runner(duplicate, import_destination, candidate_ref)
                return import_from_descriptor(duplicate, import_destination, candidate_ref=candidate_ref)
            finally:
                os.close(duplicate)

        try:
            runtime = cls(
                completion_verifier=completion_verifier,
                descriptor_importer=import_once,
                attestation_verifier=attestation_verifier,
                scorer=scorer,
                receipts=receipts,
                status=status,
                worktree=worktree,
                lifecycle=lifecycle,
                crash_injector=crash_injector,
            )
        except Exception:
            os.close(held_fd)
            raise
        runtime._source_fd = held_fd
        return runtime

    def _lifecycle_call(self, name: str, *args: Any) -> Any:
        if self.lifecycle is None:
            raise RuntimeError(f"scored lifecycle callback is not bound: {name}")
        callback = getattr(self.lifecycle, name, None)
        if not callable(callback):
            raise RuntimeError(f"scored lifecycle callback is unavailable: {name}")
        return callback(*args)

    # These callbacks are deliberately real delegates, not no-op placeholders.  The controller
    # journals them before the next close phase and the cell handle owns the idempotent effect.
    def stop_tools(self) -> Any:
        return self._lifecycle_call("stop_tools")

    def drain_rpc(self) -> Any:
        return self._lifecycle_call("drain_rpc")

    def kill_planes(self) -> Any:
        return self._lifecycle_call("kill_planes")

    def close_acl(self) -> Any:
        return self._lifecycle_call("close_acl")

    def final_status(self) -> Any:
        return self._lifecycle_call("final_status")

    def kill_git(self) -> Any:
        return self._lifecycle_call("kill_git")

    def census_snapshot(self) -> Any:
        return self._lifecycle_call("census_snapshot")

    def destroy(self) -> Any:
        try:
            return self._lifecycle_call("destroy")
        finally:
            if self._source_fd is not None:
                os.close(self._source_fd)
                self._source_fd = None

    def import_and_score(self) -> Any:
        completion = self.verify_delivery()
        if getattr(completion, "decision", None) != "agent-delivered":
            raise RuntimeError("completion verifier did not authorize import")
        imported = self.descriptor_importer(completion.payload)
        attestation = self.attestation_verifier(imported, completion)
        if isinstance(attestation, Mapping):
            attestation_value = dict(attestation)
        elif hasattr(attestation, "__dict__"):
            attestation_value = dict(vars(attestation))
        else:
            attestation_value = {}
        if attestation_value.get("attested") is not True:
            raise RuntimeError("import graph attestation was not independently verified")
        materialization = attestation_value.get("materialization")
        digest = attestation_value.get("materialization_digest")
        if not isinstance(materialization, (str, Path)) or not isinstance(digest, str):
            raise RuntimeError("post-import attestation is missing materialization digest")
        from .completion import materialization_digest

        recomputed = materialization_digest(materialization)
        if recomputed != digest:
            raise RuntimeError("post-import materialization digest mismatch")
        post_import = attestation_value.get("post_import_input")
        from .scorer_sandbox import PostImportInput

        if post_import is None:
            post_import = PostImportInput.from_attestation(
                {**attestation_value, "materialization_digest": recomputed}
            )
        elif not isinstance(post_import, PostImportInput) or post_import.digest != recomputed:
            raise RuntimeError("post-import scorer input is not bound to the attestation")
        # Completion is authenticated before the descriptor importer runs.  Supplying its
        # closed payload to the scorer is the only way G4 can bind every result to the
        # imported receipt sequence; it is not model-controlled data.
        scorer_attestation = {**attestation_value, "completion": dict(completion.payload)}
        append_pre_scorer = getattr(self.lifecycle, "append_pre_scorer_attestation", None)
        pre_scorer_attestation_digest: str | None = None
        if callable(append_pre_scorer):
            from .records import canonical_json_bytes

            environment_digest = getattr(self.lifecycle, "environment_manifest_digest", None)
            imported_graph_digest = attestation_value.get("imported_graph_digest")
            if not callable(environment_digest) or not isinstance(imported_graph_digest, str):
                raise RuntimeError("pre-scorer environment attestation is unavailable")
            pre_scorer = {
                "environment_manifest_digest": environment_digest(),
                "completion_digest": hashlib.sha256(canonical_json_bytes(dict(completion.payload))).hexdigest(),
                "imported_graph_digest": imported_graph_digest,
            }
            durable = append_pre_scorer(pre_scorer)
            if (not isinstance(durable, Mapping) or durable.get("record_type") != "pre-scorer-attestation"
                    or not isinstance(durable.get("payload"), Mapping)
                    or canonical_json_bytes(dict(durable["payload"])) != canonical_json_bytes(pre_scorer)):
                raise RuntimeError("pre-scorer authenticated reread is unavailable")
            # Derive the scorer release from the re-read authenticated record,
            # and release only its three digest fields.
            durable_payload = durable["payload"]
            scorer_attestation["pre_scorer_attestation"] = {
                key: durable_payload[key]
                for key in ("environment_manifest_digest", "completion_digest", "imported_graph_digest")
            }
            pre_scorer_attestation_digest = hashlib.sha256(
                canonical_json_bytes(dict(durable))
            ).hexdigest()
        binding_factory = getattr(self.lifecycle, "g4_receipt_bindings", None)
        if callable(binding_factory):
            scorer_attestation["g4_receipt_bindings"] = tuple(
                binding_factory(dict(completion.payload), scorer_attestation)
            )
        score = self.scorer(post_import, scorer_attestation)
        scorer_context = self._project_scorer_result(score)
        imported_oids = attestation_value.get("object_ids")
        if not isinstance(imported_oids, (tuple, list)) or not imported_oids:
            raise RuntimeError("post-import attestation is missing independent object inventory")
        imported_oids = tuple(imported_oids)
        if any(not isinstance(oid, str) or len(oid) != 40 or any(char not in "0123456789abcdef" for char in oid) for oid in imported_oids):
            raise RuntimeError("post-import attestation contains an invalid object inventory")
        append_g4_receipt = getattr(self.lifecycle, "append_g4_receipt", None)
        if callable(append_g4_receipt):
            for index, receipt in enumerate(scorer_context["g4_receipts"], start=1):
                append_g4_receipt(receipt)
                self._crash("after_g4_receipt", index)
        append_attestation = getattr(self.lifecycle, "append_post_g4_attestation", None)
        if pre_scorer_attestation_digest is not None:
            from .records import canonical_json_bytes

            if not callable(append_attestation):
                raise RuntimeError("post-G4 authenticated attestation is unavailable")
            receipts_digest = hashlib.sha256(canonical_json_bytes({"g4_receipts": list(scorer_context["g4_receipts"])})).hexdigest()
            post_g4 = {
                "pre_scorer_attestation_digest": pre_scorer_attestation_digest,
                "g4_receipts_digest": receipts_digest,
            }
            self._crash("before_post_g4_attestation")
            durable_post_g4 = append_attestation(post_g4)
            if (not isinstance(durable_post_g4, Mapping)
                    or durable_post_g4.get("record_type") != "post-g4-attestation"
                    or not isinstance(durable_post_g4.get("payload"), Mapping)
                    or canonical_json_bytes(dict(durable_post_g4["payload"])) != canonical_json_bytes(post_g4)):
                raise RuntimeError("post-G4 authenticated reread is unavailable")
            self._crash("after_post_g4_attestation")
        self.result_context = {
            "imported_oids": imported_oids,
            "imported_graph_attested": True,
            "scorer_failure": None,
            "materialization_digest": recomputed,
            "scorer_result": score,
            **scorer_context,
        }
        return score

    def _crash(self, point: str, index: int | None = None) -> None:
        """One-shot deterministic crash injection for close-recovery testing."""
        injector = getattr(self, "crash_injector", None)
        if callable(injector):
            injector(point, index)

    def verify_delivery(self) -> Any:
        """Return the authenticated completion decision before import or scoring."""

        if self._delivery_result is None:
            self._delivery_result = self.completion_verifier.verify(self.receipts, self.status, self.worktree)
        return self._delivery_result

    @staticmethod
    def _project_scorer_result(score: Any) -> dict[str, Any]:
        """Validate the complete closed scorer result before it reaches classification."""

        if not isinstance(score, Mapping):
            raise RuntimeError("scorer result is not a mapping")
        required = ("g1", "g3", "g4", "g5", "g6", "g7", "g4_receipts")
        missing = tuple(name for name in required if name not in score)
        if missing:
            raise RuntimeError(f"scorer result is missing closed gates: {', '.join(missing)}")
        projected: dict[str, Any] = {}
        for name in required[:-1]:
            value = score[name]
            if not isinstance(value, str) or value not in {"PASS", "FAIL", "UNKNOWN"}:
                raise RuntimeError(f"scorer result has an invalid {name} verdict")
            projected[name] = value
        model_limit_proven = score.get("model_limit_proven", False)
        if not isinstance(model_limit_proven, bool):
            raise RuntimeError("scorer result has invalid model-limit attribution")
        if model_limit_proven:
            projected["model_limit_proven"] = True
        receipts = score["g4_receipts"]
        if not isinstance(receipts, (list, tuple)):
            raise RuntimeError("scorer result has invalid g4_receipts")
        normalized_receipts: list[Any] = []
        for receipt in receipts:
            if not isinstance(receipt, Mapping) or set(receipt) != {
                "cell_id", "attempt_id", "commit_oid", "public_suite_oid", "public_suite_digest",
                "public_suite_digest_version", "outcome_enum", "controller_sequence", "nonce",
            }:
                raise RuntimeError("scorer result has malformed G4 receipt evidence")
            outcome = receipt.get("outcome_enum")
            if outcome not in {"FAIL", "PASS", "NOT_SCORED", "UNKNOWN"}:
                raise RuntimeError("scorer result has an invalid G4 receipt outcome")
            normalized_receipts.append(dict(receipt))
        projected["g4_receipts"] = tuple(normalized_receipts)
        return projected


class CloseJournal:
    """Small fsynced write-ahead journal kept outside the disposable cell root."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def append(self, phase: str, status: str, **details: Any) -> None:
        row = {"phase": phase, "status": status, "details": details}
        encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]


def census_legacy_adapters(root: str | Path) -> tuple[CensusHit, ...]:
    """AST census of the complete harness/tests tree before adapter deletion.

    Strings in comments and documentation do not count.  Definitions, imports, and calls do:
    this catches both production consumers and tests that would otherwise keep a deleted symbol
    reachable through a stale import.
    """

    root_path = Path(root)
    files = sorted(root_path.rglob("*.py"), key=lambda path: path.as_posix())
    hits: list[CensusHit] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            symbol: str | None = None
            kind = "reference"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _LEGACY:
                symbol, kind = node.name, "definition"
            elif isinstance(node, ast.Name) and node.id in _LEGACY:
                symbol = node.id
                kind = "name"
            elif isinstance(node, ast.alias) and node.name.rsplit(".", 1)[-1] in _LEGACY:
                symbol = node.name.rsplit(".", 1)[-1]
                kind = "import"
            elif isinstance(node, ast.Attribute) and node.attr in _LEGACY:
                symbol = node.attr
                kind = "attribute"
            if symbol is not None:
                hits.append(CensusHit(str(path), node.lineno, symbol, kind))
    return tuple(hits)


def classify_close(
    *,
    dispatch_status: str,
    dispatch_timed_out: bool = False,
    receipts: tuple[str, ...],
    imported_oids: tuple[str, ...] = (),
    dirty: bool,
    seal_complete: bool,
    receipts_authenticated: bool = True,
    imported_graph_attested: bool = True,
    budget_authenticated: bool = False,
    budget_fsynced: bool = True,
    budget_operation: str | None = None,
    infrastructure_failure: str | None = None,
    model_non_delivery: bool = False,
    scorer_failure: str | None = None,
    g1: str = "PASS",
    g3: str = "PASS",
    g4: str = "PASS",
    g5: str = "PASS",
    g6: str = "PASS",
    g7: str = "PASS",
    g4_receipts: tuple[object, ...] = (),
) -> Classification:
    """Apply the frozen precedence table to the close evidence projection."""

    return classify(
        ClassificationInput(
            dispatch_status=dispatch_status,
            dispatch_timed_out=dispatch_timed_out,
            receipts=receipts,
            imported_oids=imported_oids,
            dirty=dirty,
            seal_complete=seal_complete,
            receipts_authenticated=receipts_authenticated,
            imported_graph_attested=imported_graph_attested,
            infrastructure_failure=infrastructure_failure,
            model_non_delivery=model_non_delivery,
            budget_authenticated=budget_authenticated,
            budget_fsynced=budget_fsynced,
            budget_operation=budget_operation,
            scorer_failure=scorer_failure,
            g1=g1,
            g3=g3,
            g4=g4,
            g5=g5,
            g6=g6,
            g7=g7,
            g4_receipts=g4_receipts,
        )
    )


def classify_provisional_close(
    *,
    dispatch_status: str,
    receipts: tuple[str, ...],
    dirty: bool,
    seal_complete: bool,
    receipts_authenticated: bool = True,
    budget_authenticated: bool = False,
    budget_fsynced: bool = True,
    budget_operation: str | None = None,
    infrastructure_failure: str | None = None,
    model_non_delivery: bool = False,
    dispatch_timed_out: bool = False,
) -> Classification:
    return classify_provisional(
        ClassificationInput(
            dispatch_status=dispatch_status,
            dispatch_timed_out=dispatch_timed_out,
            receipts=receipts,
            dirty=dirty,
            seal_complete=seal_complete,
            receipts_authenticated=receipts_authenticated,
            budget_authenticated=budget_authenticated,
            budget_fsynced=budget_fsynced,
            budget_operation=budget_operation,
            infrastructure_failure=infrastructure_failure,
            model_non_delivery=model_non_delivery,
        )
    )


def _normalise_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {
            "dispatch_status": "ok",
            "receipts": (),
            "imported_oids": (),
            "dirty": False,
            "seal_complete": True,
            "receipts_authenticated": True,
            "imported_graph_attested": True,
            "infrastructure_failure": None,
            "model_non_delivery": False,
        }
    context = dict(value or {})
    required = {"dispatch_status", "receipts", "imported_oids", "dirty", "seal_complete", "receipts_authenticated", "imported_graph_attested", "infrastructure_failure"}
    if not required <= set(context):
        context["infrastructure_failure"] = context.get("infrastructure_failure") or "incomplete-close-context"
    context.setdefault("dispatch_status", "failed")
    context.setdefault("receipts", ())
    context.setdefault("imported_oids", ())
    context.setdefault("dirty", False)
    context.setdefault("seal_complete", False)
    context.setdefault("receipts_authenticated", False)
    context.setdefault("imported_graph_attested", False)
    context.setdefault("infrastructure_failure", None)
    context.setdefault("model_non_delivery", False)
    return context


class Controller:
    """Single close dispatcher with durable phase commits and restart recovery."""

    def __init__(
        self,
        journal: str | Path,
        *,
        runtime: Any | None = None,
        actions: Mapping[str, Callable[[], Any]] | None = None,
        recovery_actions: Mapping[str, Callable[[], Any]] | None = None,
        close_context: Mapping[str, Any] | None = None,
        crash_before: Iterable[str] = (),
        runtime_factory: Callable[[], Any] | None = None,
        strict_lifecycle: bool = False,
    ) -> None:
        self.journal = CloseJournal(journal)
        self.runtime = runtime
        self._lifecycle_runtime = runtime
        self.runtime_factory = runtime_factory
        self.strict_lifecycle = strict_lifecycle
        self.actions = dict(actions or {})
        lifecycle_methods_enabled = runtime is not None and not (
            isinstance(runtime, ScoredCloseRuntime) and not runtime.requires_real_lifecycle
        )
        if lifecycle_methods_enabled:
            runtime_actions = {
                "STOP_TOOLS": ("stop_tools", "stop_new_tools"),
                "DRAIN_RPC": ("drain_rpc", "drain"),
                "KILL_PLANES": ("kill_planes", "stop_processes"),
                "CLOSE_ACL": ("close_acl", "close_acl_lifecycle"),
                "FINAL_STATUS": ("final_status",),
                "KILL_GIT": ("kill_git", "stop_git"),
                "CENSUS_SNAPSHOT": ("census_snapshot", "snapshot"),
                "IMPORT_SCORE": ("import_and_score", "import_score"),
                "EVIDENCE_DESTROY": ("destroy",),
            }
        elif isinstance(runtime, ScoredCloseRuntime):
            runtime_actions = {"IMPORT_SCORE": ("import_and_score",)}
        else:
            runtime_actions = {}
        if lifecycle_methods_enabled:
            for phase, names in runtime_actions.items():
                if phase in self.actions:
                    continue
                method = next((getattr(runtime, name, None) for name in names if callable(getattr(runtime, name, None))), None)
                if method is not None:
                    self.actions[phase] = method
        elif isinstance(runtime, ScoredCloseRuntime) and "IMPORT_SCORE" not in self.actions:
            method = runtime.import_and_score
            self.actions["IMPORT_SCORE"] = method
        if runtime_factory is not None and "OPEN_DESCRIPTOR" not in self.actions:
            self.actions["OPEN_DESCRIPTOR"] = runtime_factory
        self.recovery_actions = dict(recovery_actions or {})
        self._has_context = close_context is not None
        self.close_context = _normalise_context(close_context)
        self.crash_before = set(crash_before)
        self.state = CloseState.ALLOCATED
        self._classification: Classification | None = None
        self._errors: list[str] = []

    def _committed(self) -> set[str]:
        return {row["phase"] for row in self.journal.read() if row.get("status") == "committed"}

    def _prepared(self) -> set[str]:
        rows = self.journal.read()
        committed = {row["phase"] for row in rows if row.get("status") == "committed"}
        return {row["phase"] for row in rows if row.get("status") == "prepared" and row["phase"] not in committed}

    def _run(self, phase: str, *, recovery: bool = False) -> Any:
        callback = (self.recovery_actions if recovery else self.actions).get(phase)
        if callback is None:
            callback = self.actions.get(phase)
        if callback is None and self.strict_lifecycle and phase not in _LOGICAL_PHASES:
            self.journal.append(phase, "prepared", recovery=recovery)
            raise RuntimeError(f"scored lifecycle callback is not bound: {phase}")
        self.journal.append(phase, "prepared", recovery=recovery)
        if not recovery and phase in self.crash_before:
            self.crash_before.remove(phase)
            return
        result = callback() if callback is not None else None
        if phase == "OPEN_DESCRIPTOR" and result is not None:
            self._bind_open_runtime(result)
        details: dict[str, Any] = {"recovery": recovery}
        if phase == "IMPORT_SCORE":
            details["result_context"] = self._durable_result_context(result)
        self.journal.append(phase, "committed", **details)
        return result

    def _bind_open_runtime(self, runtime: Any) -> None:
        """Bind descriptor-backed cleanup and import callbacks for the remaining phases."""

        self.runtime = runtime
        lifecycle = getattr(runtime, "lifecycle", None)
        if lifecycle is not None:
            self._lifecycle_runtime = lifecycle
        callbacks = {
            "STOP_TOOLS": "stop_tools",
            "DRAIN_RPC": "drain_rpc",
            "KILL_PLANES": "kill_planes",
            "CLOSE_ACL": "close_acl",
            "FINAL_STATUS": "final_status",
            "KILL_GIT": "kill_git",
            "CENSUS_SNAPSHOT": "census_snapshot",
            "IMPORT_SCORE": "import_and_score",
            "EVIDENCE_DESTROY": "destroy",
        }
        committed = self._committed()
        for phase, name in callbacks.items():
            callback = getattr(runtime, name, None)
            if callable(callback) and phase not in committed and (lifecycle is not None or phase == "IMPORT_SCORE"):
                self.actions[phase] = callback

    def _reconstruct_open_runtime(self) -> None:
        """Recreate only the local descriptor runtime after a committed OPEN_DESCRIPTOR."""

        if self.runtime_factory is None or "OPEN_DESCRIPTOR" not in self._committed() or "IMPORT_SCORE" in self._committed():
            return
        try:
            parameters = inspect.signature(self.runtime_factory).parameters
            if "recovery" in parameters or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                runtime = self.runtime_factory(recovery=True)
            else:
                runtime = self.runtime_factory()
        except Exception as exc:  # noqa: BLE001 - recovery must not invent an import runtime
            raise RuntimeError(f"scored lifecycle runtime reconstruction failed: {exc}") from exc
        if runtime is None:
            raise RuntimeError("scored lifecycle runtime reconstruction returned no runtime")
        self._bind_open_runtime(runtime)

    def _sync_lifecycle_context(self) -> None:
        runtime = self._lifecycle_runtime
        projection = None
        if runtime is not None:
            instance_value = vars(runtime).get("completion_projection") if hasattr(runtime, "__dict__") else None
            class_value = getattr(runtime, "completion_projection", None) if "completion_projection" in vars(type(runtime)) else None
            projection = instance_value if callable(instance_value) else class_value
        if not callable(projection):
            return
        try:
            value = projection()
        except Exception as exc:  # noqa: BLE001 - lifecycle evidence is authoritative
            self.close_context["infrastructure_failure"] = "completion-projection"
            self._errors.append(f"completion-projection: {exc}")
            return
        if not isinstance(value, Mapping):
            self.close_context["infrastructure_failure"] = "completion-projection"
            return
        merge_runtime_context(self.close_context, value)

    def _durable_result_context(self, action_result: Any = None) -> dict[str, Any]:
        value: dict[str, Any] = {}
        runtime_context = getattr(self.runtime, "result_context", None)
        if isinstance(runtime_context, Mapping):
            value.update(runtime_context)
        if isinstance(action_result, Mapping):
            value.update(action_result)
        allowed = {
            "imported_oids", "imported_graph_attested", "scorer_failure", "materialization_digest", "scorer_result",
            "g1", "g3", "g4", "g5", "g6", "g7", "g4_receipts",
            "model_limit_proven",
        }
        value = {key: item for key, item in value.items() if key in allowed}
        try:
            return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("IMPORT_SCORE result context is not durably serializable") from exc

    def _rehydrate_import_context(self) -> None:
        for row in reversed(self.journal.read()):
            if row.get("phase") != "IMPORT_SCORE" or row.get("status") != "committed":
                continue
            details = row.get("details")
            if not isinstance(details, Mapping):
                self.close_context["infrastructure_failure"] = "missing-durable-import-score-context"
                return
            context = details.get("result_context")
            required = {"imported_oids", "imported_graph_attested", "scorer_failure", "g1", "g3", "g4", "g5", "g6", "g7", "g4_receipts"}
            if not isinstance(context, Mapping) or not required <= set(context):
                self.close_context["infrastructure_failure"] = "missing-durable-import-score-context"
                return
            self.close_context.update(context)
            if details.get("failure") == "import-score":
                self.close_context["infrastructure_failure"] = "import-score"
                self.close_context["imported_graph_attested"] = False
            return

    def _verify_delivery(self) -> None:
        """Verify the sealed completion before opening the importer boundary.

        A verifier's authenticated ``not-delivered`` result is a model outcome.  Verifier
        exceptions are controller/infrastructure failures and therefore remain UNKNOWN.
        """

        if self.close_context.get("delivery_verified"):
            return
        verifier = None
        if self.runtime is not None:
            instance_value = vars(self.runtime).get("verify_delivery") if hasattr(self.runtime, "__dict__") else None
            class_value = getattr(self.runtime, "verify_delivery", None) if "verify_delivery" in vars(type(self.runtime)) else None
            verifier = instance_value if callable(instance_value) else class_value
        if not callable(verifier):
            if self.strict_lifecycle:
                if self.close_context.get("receipts"):
                    self.close_context["infrastructure_failure"] = "missing-completion-verifier"
                else:
                    self.close_context["model_non_delivery"] = True
            self.close_context["delivery_verified"] = True
            return
        try:
            decision = verifier()
        except Exception as exc:  # noqa: BLE001 - verifier failure is authoritative infrastructure
            self.close_context["infrastructure_failure"] = "completion-verification"
            self._errors.append(f"completion-verification: {exc}")
        else:
            if getattr(decision, "decision", decision) == "not-delivered":
                self.close_context["model_non_delivery"] = True
            elif getattr(decision, "decision", decision) != "agent-delivered":
                self.close_context["infrastructure_failure"] = "completion-verification"
        self.close_context["delivery_verified"] = True

    def close(self, *, terminal: str = "completed") -> CloseResult:
        if self.state is CloseState.DESTROYED:
            return self.result()
        if self.state is CloseState.CLOSING:
            raise RuntimeError("close dispatcher already active")
        self._rehydrate_import_context()
        self.state = CloseState.CLOSING
        for phase in CLOSE_PHASES:
            if phase in self._committed():
                continue
            if phase == "OPEN_DESCRIPTOR":
                self._sync_lifecycle_context()
                if not self._has_context:
                    self._run(phase)
                    if phase in self._prepared() and phase not in self._committed():
                        raise RuntimeError(f"close interrupted before {phase} side effect")
                    continue
                self._classify(terminal, provisional=True)
                if not self._import_allowed():
                    continue
                open_failed = False
                try:
                    self._run(phase)
                except Exception as exc:
                    if "callback is not bound" in str(exc):
                        raise
                    if not self.strict_lifecycle:
                        raise
                    self._errors.append(f"open-descriptor: {exc}")
                    self.close_context["infrastructure_failure"] = "open-descriptor"
                    self.close_context["imported_graph_attested"] = False
                    open_failed = True
                if not open_failed and phase in self._prepared() and phase not in self._committed():
                    raise RuntimeError(f"close interrupted before {phase} side effect")
                continue
            if phase == "IMPORT_SCORE":
                self._verify_delivery()
                self._classify(terminal, provisional=True)
                if not self._import_allowed():
                    continue
                try:
                    action_result = self._run(phase)
                except Exception as exc:
                    if isinstance(exc, CloseCrash):
                        raise
                    if self.runtime is None or "callback is not bound" in str(exc):
                        raise
                    self._errors.append(f"import-score: {exc}")
                    self.close_context["infrastructure_failure"] = "import-score"
                    self.close_context["imported_graph_attested"] = False
                    if not self.strict_lifecycle:
                        self.journal.append(phase, "committed", failure="import-score")
                    action_result = None
                if not self.strict_lifecycle and phase in self._prepared() and phase not in self._committed():
                    raise RuntimeError(f"close interrupted before {phase} side effect")
                runtime_context = getattr(self.runtime, "result_context", None)
                if isinstance(runtime_context, Mapping):
                    self._merge_runtime_context(runtime_context)
                if isinstance(action_result, Mapping):
                    self._merge_runtime_context(action_result)
                self._classify(terminal, provisional=False)
                continue

            self._run(phase)
            if phase in self._prepared() and phase not in self._committed():
                raise RuntimeError(f"close interrupted before {phase} side effect")
            if phase == "CLASSIFY":
                self._classify(terminal, provisional=True)
        if "IMPORT_SCORE" in self._committed():
            self._classify(terminal, provisional=False)
        return self._finish(terminal)

    def recover(self) -> CloseResult:
        """Resume the first prepared/uncommitted phase, then only later phases."""

        if self.state is CloseState.DESTROYED:
            return self.result()
        self._rehydrate_import_context()
        self._reconstruct_open_runtime()
        self.state = CloseState.CLOSING
        prepared = self._prepared()
        committed = self._committed()
        # A crash between two phase calls leaves no prepared row.  Recovery must still resume
        # at the first uncommitted phase; starting at len(CLOSE_PHASES) would silently skip the
        # remainder of close and falsely report destruction.
        start = next(
            (
                index
                for index, phase in enumerate(CLOSE_PHASES)
                if phase in prepared
            ),
            next(
                (
                    index
                    for index, phase in enumerate(CLOSE_PHASES)
                    if phase not in committed
                ),
                len(CLOSE_PHASES),
            ),
        )
        for phase in CLOSE_PHASES[start:]:
            if phase in committed:
                continue
            if phase == "IMPORT_SCORE":
                self._verify_delivery()
                self._classify(self.close_context.get("terminal", "completed"), provisional=True)
                if not self._import_allowed():
                    continue
            if phase == "IMPORT_SCORE":
                try:
                    action_result = self._run(phase, recovery=phase in prepared)
                except Exception as exc:
                    if isinstance(exc, CloseCrash):
                        raise
                    if self.runtime is None or "callback is not bound" in str(exc):
                        raise
                    self._errors.append(f"import-score: {exc}")
                    self.close_context["infrastructure_failure"] = "import-score"
                    self.close_context["imported_graph_attested"] = False
                    if not self.strict_lifecycle:
                        self.journal.append(phase, "committed", failure="import-score")
                    action_result = None
                runtime_context = getattr(self.runtime, "result_context", None)
                if isinstance(runtime_context, Mapping):
                    self._merge_runtime_context(runtime_context)
                if isinstance(action_result, Mapping):
                    self._merge_runtime_context(action_result)
                self._classify(self.close_context.get("terminal", "completed"), provisional=False)
            elif phase == "OPEN_DESCRIPTOR":
                self._sync_lifecycle_context()
                if not self._has_context:
                    self._run(phase, recovery=phase in prepared)
                    continue
                self._classify(self.close_context.get("terminal", "completed"), provisional=True)
                if not self._import_allowed():
                    continue
                try:
                    self._run(phase, recovery=phase in prepared)
                except Exception as exc:
                    if "callback is not bound" in str(exc):
                        raise
                    self._errors.append(f"open-descriptor: {exc}")
                    self.close_context["infrastructure_failure"] = "open-descriptor"
                    self.close_context["imported_graph_attested"] = False
            else:
                self._run(phase, recovery=phase in prepared)
                if phase == "CLASSIFY":
                    self._classify(self.close_context.get("terminal", "completed"), provisional=True)
        if "IMPORT_SCORE" in self._committed():
            self._classify(self.close_context.get("terminal", "completed"), provisional=False)
        return self._finish(self.close_context.get("terminal", "completed"))

    def _merge_runtime_context(self, value: Mapping[str, Any]) -> None:
        merge_runtime_context(self.close_context, value)

    def _classify(self, terminal: str, *, provisional: bool = False) -> None:
        context = dict(self.close_context)
        context["dispatch_status"] = {"timeout": "timeout", "dispatch-failed": "failed"}.get(
            terminal, context.get("dispatch_status", "ok")
        )
        allowed = {"dispatch_status", "dispatch_timed_out", "receipts", "imported_oids", "dirty", "seal_complete", "receipts_authenticated", "imported_graph_attested", "infrastructure_failure", "model_non_delivery", "budget_authenticated", "budget_fsynced", "budget_operation", "scorer_failure", "g1", "g3", "g4", "g5", "g6", "g7", "g4_receipts"}
        try:
            if provisional:
                self._classification = classify_provisional_close(**{key: value for key, value in context.items() if key in {
                    "dispatch_status", "receipts", "dirty", "seal_complete", "receipts_authenticated",
                    "budget_authenticated", "budget_fsynced", "budget_operation", "infrastructure_failure", "model_non_delivery", "dispatch_timed_out",
                }})
            else:
                self._classification = classify_close(**{key: value for key, value in context.items() if key in allowed})
        except (TypeError, ValueError) as exc:
            self._errors.append(str(exc))
            self._classification = Classification({f"G{i}": "UNKNOWN" for i in range(8)}, reason="infrastructure")

    def _import_allowed(self) -> bool:
        """Only the authoritative classification may open the importer/scorer boundary."""

        # Context-free Controller instances are the ordinary non-scored close adapter.  The
        # scored path always supplies close_context and is therefore gated by classification.
        return self._has_context and self._classification is not None and self._classification.get("G2") == "agent-delivered"

    def _finish(self, terminal: str) -> CloseResult:
        self.state = CloseState.CLASSIFYING
        if self._classification is None:
            self._classify(terminal)
        if any(value == "UNKNOWN" for value in self._classification.values()):
            self.state = CloseState.UNKNOWN_EVIDENCED
        elif self._classification.get("G2") == "not-delivered":
            self.state = CloseState.NOT_DELIVERED
        else:
            self.state = CloseState.IMPORTED
        if "IMPORT_SCORE" in self._committed() and self.state is CloseState.IMPORTED:
            self.state = CloseState.SCORED
        self.state = CloseState.DESTROYED
        return self.result()

    def result(self) -> CloseResult:
        classification = self._classification or Classification({f"G{i}": "UNKNOWN" for i in range(8)}, reason="not-closed")
        committed = self._committed()
        return CloseResult(self.state, classification, tuple(phase for phase in CLOSE_PHASES if phase in committed), tuple(self._errors))


ProductionController = Controller


def close_cell(runtime: Any, *, journal: str | Path | None = None, terminal: str = "completed", context: Mapping[str, Any] | None = None) -> CloseResult:
    """Run the production close dispatcher against a cell-runtime implementation."""

    runtime_journal = getattr(getattr(runtime, "journal", None), "path", None)
    controller = Controller(journal or runtime_journal or Path("cell-close.ndjson"), runtime=runtime, close_context=context)
    return controller.close(terminal=terminal)

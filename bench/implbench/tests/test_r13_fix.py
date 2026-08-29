from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from implbench.harness.controller import CLOSE_PHASES, Controller, ScoredCloseRuntime
from implbench.harness.completion import materialization_digest
from implbench.harness.dispatch import _argv, run_task
from implbench.harness.runtime import ProductionRuntimeUnavailable, _ProductionCell, build_production_scorer
from implbench.harness.importer import ImportGraphAttestation
from implbench.harness.scorer_sandbox import G4ReceiptBinding, ScorerModelExecutionLimit, ScorerRunResult
from implbench.harness.schedule import expand_schedule
from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.engines.base import EngineError
from attempt_service_fixture import lifecycle as attempt_lifecycle


def _context(*, receipts: tuple[str, ...]) -> dict[str, object]:
    return {
        "dispatch_status": "ok",
        "receipts": receipts,
        "imported_oids": (),
        "dirty": False,
        "seal_complete": True,
        "receipts_authenticated": True,
        "imported_graph_attested": False,
        "infrastructure_failure": None,
    }


def _lifecycle(events: list[str], *, receipts: tuple[str, ...]) -> object:
    class Lifecycle:
        def completion_projection(self) -> dict[str, object]:
            return {
                "receipt_oids": list(receipts),
                "dirty": False,
                "seal_complete": True,
                "receipts_authenticated": True,
                "infrastructure_failure": None,
            }

        def __getattr__(self, name: str):
            if name in {"stop_tools", "drain_rpc", "kill_planes", "close_acl", "final_status", "kill_git", "census_snapshot", "destroy"}:
                return lambda name=name: events.append(name)
            raise AttributeError(name)

    return Lifecycle()


def test_scored_descriptor_open_is_lazy_and_after_final_status(tmp_path: Path) -> None:
    events: list[str] = []
    receipt = "a" * 40
    lifecycle = _lifecycle(events, receipts=(receipt,))

    def open_descriptor() -> object:
        events.append("OPEN_DESCRIPTOR")
        raise RuntimeError("descriptor unavailable")

    result = Controller(
        tmp_path / "close.ndjson",
        runtime=lifecycle,
        runtime_factory=open_descriptor,
        strict_lifecycle=True,
        close_context=_context(receipts=(receipt,)),
    ).close()

    assert events.index("final_status") < events.index("census_snapshot") < events.index("OPEN_DESCRIPTOR")
    assert "IMPORT_SCORE" not in events
    assert events[-1] == "destroy"
    assert result.classification["G2"] == "UNKNOWN"
    rows = [row for row in (tmp_path / "close.ndjson").read_text().splitlines() if row]
    assert not any('"phase":"OPEN_DESCRIPTOR","status":"committed"' in row for row in rows)


def test_empty_receipts_skip_factory_and_are_model_non_delivery(tmp_path: Path) -> None:
    events: list[str] = []
    called = False

    def factory() -> object:
        nonlocal called
        called = True
        raise AssertionError("empty receipt close must not open scorer dependencies")

    result = Controller(
        tmp_path / "close.ndjson",
        runtime=_lifecycle(events, receipts=()),
        runtime_factory=factory,
        strict_lifecycle=True,
        close_context=_context(receipts=()),
    ).close()

    assert called is False
    assert result.classification["G2"] == "not-delivered"
    assert result.classification["G1"] == "NOT_SCORED"


def test_missing_scored_lifecycle_has_no_committed_fake_close(tmp_path: Path) -> None:
    controller = Controller(
        tmp_path / "close.ndjson",
        strict_lifecycle=True,
        close_context=_context(receipts=("a" * 40,)),
    )

    with pytest.raises(RuntimeError, match="callback is not bound"):
        controller.close()
    rows = controller.journal.read()
    assert rows and rows[0]["phase"] == "STOP_TOOLS"
    assert not any(row["status"] == "committed" for row in rows)


def test_scored_argv_has_no_bridge_worktree_and_requires_cell_root(tmp_path: Path) -> None:
    task = SimpleNamespace(task_id="task-1", expected_artifacts=(), allowed_paths=(), brief="", timeout_s=1)
    with pytest.raises(ValueError, match="cell root"):
        _argv(task, "seat", "pi-sdk", "a" * 40, "oi-pi-bakeoff-r13", cell_id="cell-x")
    argv = _argv(task, "seat", "pi-sdk", "a" * 40, "oi-pi-bakeoff-r13", cell_id="cell-x", cell_root=tmp_path)
    assert "--worktree" not in argv
    assert argv[argv.index("--cell-root") + 1] == str(tmp_path)


def test_scored_run_rejects_a_bridge_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".claude" / "worktrees" / "ordinary"
    worktree.mkdir(parents=True)
    task = SimpleNamespace(task_id="task-1", expected_artifacts=(), allowed_paths=(), brief="", timeout_s=1)
    cell = next(cell for cell in expand_schedule("00" * 32, [("task-1", "a" * 64)] + [(f"task-{i}", "a" * 64) for i in range(2, 9)]) if cell.task_id == "task-1")
    with pytest.raises(ValueError, match="ordinary bridge worktrees"):
        run_task(
            task, "seat", "pi-sdk", "a" * 64, "oi-pi-bakeoff-r13", repo,
            schedule_cell=cell, fixture_root_oid="b" * 40, tool_gid=123,
            cell_root=worktree, scored_runtime_factory=lambda **_: None,
            scored_lifecycle=attempt_lifecycle(tmp_path),
        )


def test_parse_cell_root_accepts_macos_var_alias(tmp_path: Path) -> None:
    bridge = Bridge.__new__(Bridge)
    bridge.args = SimpleNamespace(scored_cell_root_base="/private/var")
    canonical = Path("/private/var/tmp")
    alias = Path("/var/tmp")
    envelope = SimpleNamespace(payload={"cell_root": str(alias)})
    assert bridge.parse_cell_root(envelope) == canonical.resolve()


def test_production_scorer_is_bound_but_unavailable_fails_closed() -> None:
    scorer = build_production_scorer({"pins": {"scorer": {"version": "v1", "digest": "sha256:" + "0" * 64}}})
    assert callable(scorer)
    with pytest.raises(ProductionRuntimeUnavailable, match="scorer is unavailable"):
        scorer(None, None)


def test_production_scorer_reaches_closed_runtime_projection_without_pass_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "scorer"
    binary.write_text("#!/bin/sh\n[ \"$1\" = --version ] && { echo scorer-v1; exit 0; }\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    public_digest = "b" * 64
    manifest = {
        "pins": {
            "scorer": {"version": "scorer-v1", "digest": "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()},
            "public_suite": {"digest": "sha256:" + public_digest, "digest_version": "public-suite-v1"},
        }, "budgets": {"scorer_max_output_bytes": 1024},
    }
    monkeypatch.setenv("IMPLBENCH_SCORER_BIN", str(binary))
    monkeypatch.setenv("IMPLBENCH_PUBLIC_SUITE_OID", "c" * 40)
    monkeypatch.setenv("IMPLBENCH_BATTERY_KEY", "controller-only-test-key")
    for index, name in enumerate((
        "IMPLBENCH_SCORER_KEYED_RUNNER_UID", "IMPLBENCH_SCORER_BROKER_UID",
        "IMPLBENCH_SCORER_SUBMITTED_PROGRAM_UID", "IMPLBENCH_SCORER_COORDINATOR_UID",
        "IMPLBENCH_SCORER_SUITE_RUNNER_BROKER_UID", "IMPLBENCH_SCORER_SUBMITTED_CODE_UID",
    ), start=101):
        monkeypatch.setenv(name, str(index))

    class GraphSandbox:
        def __init__(self, _root, _materialization, topology, **_kwargs):
            self.gate = topology.gate
            self.last_graph_result = None
        def run_topology(self, _commands, **kwargs):
            if self.gate == "G1":
                self.last_graph_result = {"g1": "FAIL", "g3": "PASS", "g5": "FAIL", "g6": "UNKNOWN", "g7": "PASS"}
            else:
                bindings = kwargs["g4_receipt_bindings"]
                self.last_graph_result = {"g4": "PASS", "g4_receipts": tuple({
                    "cell_id": item.cell_id, "attempt_id": item.attempt_id, "commit_oid": item.commit_oid,
                    "public_suite_oid": item.public_suite_oid, "public_suite_digest": item.public_suite_digest,
                    "public_suite_digest_version": item.public_suite_digest_version, "outcome_enum": "PASS",
                    "controller_sequence": item.controller_sequence, "nonce": item.nonce,
                } for item in bindings)}
            return tuple(ScorerRunResult(role.value, 0, "", "", 0 if role.value in {"broker", "suite-runner/broker"} else None)
                         for role in _commands if role.value not in {"submitted-program", "submitted-code"})

    monkeypatch.setattr("implbench.harness.runtime.ScorerSandbox", GraphSandbox)
    materialization = tmp_path / "materialization"; materialization.mkdir()
    (materialization / "result.txt").write_text("trusted\n", encoding="utf-8")
    digest = materialization_digest(materialization)
    completion = SimpleNamespace(
        decision="agent-delivered",
        payload={"cell_id": "cell-" + "d" * 64, "attempt_id": "attempt-" + "e" * 32,
                 "receipts": [{"commit_oid": "f" * 40, "controller_sequence": 1}]},
    )
    lifecycle = SimpleNamespace(g4_receipt_bindings=lambda completion, _attestation: (
        G4ReceiptBinding(
            cell_id=completion["cell_id"], attempt_id=completion["attempt_id"], commit_oid="f" * 40,
            public_suite_oid="c" * 40, public_suite_digest=public_digest,
            public_suite_digest_version="public-suite-v1", controller_sequence=1, nonce="d" * 64,
        ),
    ))
    runtime = ScoredCloseRuntime(
        completion_verifier=SimpleNamespace(verify=lambda *_: completion),
        descriptor_importer=lambda _payload: object(),
        attestation_verifier=lambda _imported, _completion: ImportGraphAttestation(
            True, "a" * 64, ("f" * 40,), materialization, digest),
        scorer=build_production_scorer(manifest), receipts=[], status={}, worktree=materialization, lifecycle=lifecycle,
    )
    runtime.import_and_score()
    assert runtime.result_context["g1"] == "FAIL"
    assert runtime.result_context["g5"] == "FAIL"
    assert runtime.result_context["g6"] == "UNKNOWN"
    assert runtime.result_context["g4_receipts"][0]["commit_oid"] == "f" * 40


@pytest.mark.parametrize("limit", ["timeout", "output-limit"])
def test_production_scorer_model_limit_reaches_durable_close_and_recovery(
    limit: str,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "scorer"
    binary.write_text("#!/bin/sh\n[ \"$1\" = --version ] && { echo scorer-v1; exit 0; }\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    public_digest = "b" * 64
    manifest = {
        "pins": {
            "scorer": {"version": "scorer-v1", "digest": "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()},
            "public_suite": {"digest": "sha256:" + public_digest, "digest_version": "public-suite-v1"},
        }, "budgets": {"scorer_max_output_bytes": 1024},
    }
    monkeypatch.setenv("IMPLBENCH_SCORER_BIN", str(binary))
    monkeypatch.setenv("IMPLBENCH_PUBLIC_SUITE_OID", "c" * 40)
    monkeypatch.setenv("IMPLBENCH_BATTERY_KEY", "controller-only-test-key")
    for index, name in enumerate((
        "IMPLBENCH_SCORER_KEYED_RUNNER_UID", "IMPLBENCH_SCORER_BROKER_UID",
        "IMPLBENCH_SCORER_SUBMITTED_PROGRAM_UID", "IMPLBENCH_SCORER_COORDINATOR_UID",
        "IMPLBENCH_SCORER_SUITE_RUNNER_BROKER_UID", "IMPLBENCH_SCORER_SUBMITTED_CODE_UID",
    ), start=101):
        monkeypatch.setenv(name, str(index))

    class GraphSandbox:
        def __init__(self, _root, _materialization, topology, **_kwargs):
            self.gate = topology.gate
            self.last_graph_result = None
        def run_topology(self, _commands, **kwargs):
            if self.gate == "G1":
                raise ScorerModelExecutionLimit(f"submitted scorer execution {limit}")
            else:
                bindings = kwargs["g4_receipt_bindings"]
                self.last_graph_result = {"g4": "PASS", "g4_receipts": tuple({
                    "cell_id": item.cell_id, "attempt_id": item.attempt_id, "commit_oid": item.commit_oid,
                    "public_suite_oid": item.public_suite_oid, "public_suite_digest": item.public_suite_digest,
                    "public_suite_digest_version": item.public_suite_digest_version, "outcome_enum": "PASS",
                    "controller_sequence": item.controller_sequence, "nonce": item.nonce,
                } for item in bindings)}
            return tuple(ScorerRunResult(role.value, 0, "", "", 0 if role.value in {"broker", "suite-runner/broker"} else None)
                         for role in _commands if role.value not in {"submitted-program", "submitted-code"})

    monkeypatch.setattr("implbench.harness.runtime.ScorerSandbox", GraphSandbox)
    materialization = tmp_path / "materialization"; materialization.mkdir()
    (materialization / "result.txt").write_text("trusted\n", encoding="utf-8")
    digest = materialization_digest(materialization)
    completion = SimpleNamespace(
        decision="agent-delivered",
        payload={"cell_id": "cell-" + "d" * 64, "attempt_id": "attempt-" + "e" * 32,
                 "receipts": [{"commit_oid": "f" * 40, "controller_sequence": 1}]},
    )
    lifecycle = _lifecycle([], receipts=("f" * 40,))
    lifecycle.g4_receipt_bindings = lambda completion, _attestation: (
        G4ReceiptBinding(
            cell_id=completion["cell_id"], attempt_id=completion["attempt_id"], commit_oid="f" * 40,
            public_suite_oid="c" * 40, public_suite_digest=public_digest,
            public_suite_digest_version="public-suite-v1", controller_sequence=1, nonce="d" * 64,
        ),
    )
    runtime = ScoredCloseRuntime(
        completion_verifier=SimpleNamespace(verify=lambda *_: completion),
        descriptor_importer=lambda _payload: object(),
        attestation_verifier=lambda _imported, _completion: ImportGraphAttestation(
            True, "a" * 64, ("f" * 40,), materialization, digest),
        scorer=build_production_scorer(manifest), receipts=[], status={}, worktree=materialization, lifecycle=lifecycle,
    )
    context = _context(receipts=("f" * 40,))
    journal = tmp_path / "close.ndjson"
    result = Controller(
        journal, runtime=runtime, runtime_factory=lambda: runtime,
        strict_lifecycle=True, close_context=context,
    ).close()

    assert result.classification["G0"] == "PASS"
    assert result.classification["G2"] == "agent-delivered"
    assert result.classification["G1"] == "FAIL"
    rows = [json.loads(row) for row in journal.read_text(encoding="utf-8").splitlines()]
    import_row = next(row for row in rows if row["phase"] == "IMPORT_SCORE" and row["status"] == "committed")
    assert import_row["details"]["result_context"]["g1"] == "FAIL"

    recovered = Controller(
        journal,
        runtime=SimpleNamespace(
            result_context={},
            import_and_score=lambda: (_ for _ in ()).throw(AssertionError("recovery must not rescore")),
        ),
        strict_lifecycle=True,
        close_context=context,
    ).recover()
    assert recovered.phases == CLOSE_PHASES
    assert recovered.classification["G1"] == "FAIL"
    assert {row["phase"] for row in rows if row["status"] == "committed"} >= set(CLOSE_PHASES)


def test_production_scorer_launcher_failure_is_infrastructure_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "scorer"
    binary.write_text("#!/bin/sh\n[ \"$1\" = --version ] && { echo scorer-v1; exit 0; }\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    public_digest = "b" * 64
    manifest = {
        "pins": {
            "scorer": {"version": "scorer-v1", "digest": "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()},
            "public_suite": {"digest": "sha256:" + public_digest, "digest_version": "public-suite-v1"},
        }, "budgets": {"scorer_max_output_bytes": 1024},
    }
    monkeypatch.setenv("IMPLBENCH_SCORER_BIN", str(binary))
    monkeypatch.setenv("IMPLBENCH_PUBLIC_SUITE_OID", "c" * 40)
    monkeypatch.setenv("IMPLBENCH_BATTERY_KEY", "controller-only-test-key")
    for index, name in enumerate((
        "IMPLBENCH_SCORER_KEYED_RUNNER_UID", "IMPLBENCH_SCORER_BROKER_UID",
        "IMPLBENCH_SCORER_SUBMITTED_PROGRAM_UID", "IMPLBENCH_SCORER_COORDINATOR_UID",
        "IMPLBENCH_SCORER_SUITE_RUNNER_BROKER_UID", "IMPLBENCH_SCORER_SUBMITTED_CODE_UID",
    ), start=101):
        monkeypatch.setenv(name, str(index))

    class GraphSandbox:
        def __init__(self, *_args, **_kwargs):
            self.last_graph_result = None
        def run_topology(self, *_args, **_kwargs):
            raise RuntimeError("launcher helper status failure")

    monkeypatch.setattr("implbench.harness.runtime.ScorerSandbox", GraphSandbox)
    materialization = tmp_path / "materialization"; materialization.mkdir()
    (materialization / "result.txt").write_text("trusted\n", encoding="utf-8")
    digest = materialization_digest(materialization)
    completion = SimpleNamespace(
        decision="agent-delivered",
        payload={"cell_id": "cell-" + "d" * 64, "attempt_id": "attempt-" + "e" * 32,
                 "receipts": [{"commit_oid": "f" * 40, "controller_sequence": 1}]},
    )
    lifecycle = _lifecycle([], receipts=("f" * 40,))
    lifecycle.g4_receipt_bindings = lambda completion, _attestation: (
        G4ReceiptBinding(
            cell_id=completion["cell_id"], attempt_id=completion["attempt_id"], commit_oid="f" * 40,
            public_suite_oid="c" * 40, public_suite_digest=public_digest,
            public_suite_digest_version="public-suite-v1", controller_sequence=1, nonce="d" * 64,
        ),
    )
    runtime = ScoredCloseRuntime(
        completion_verifier=SimpleNamespace(verify=lambda *_: completion),
        descriptor_importer=lambda _payload: object(),
        attestation_verifier=lambda _imported, _completion: ImportGraphAttestation(
            True, "a" * 64, ("f" * 40,), materialization, digest),
        scorer=build_production_scorer(manifest), receipts=[], status={}, worktree=materialization, lifecycle=lifecycle,
    )
    with pytest.raises(ProductionRuntimeUnavailable, match="production scorer execution failed"):
        runtime.import_and_score()

    result = Controller(
        tmp_path / "close.ndjson", runtime=runtime, runtime_factory=lambda: runtime, strict_lifecycle=True,
        close_context=_context(receipts=("f" * 40,)),
    ).close()
    assert all(value == "UNKNOWN" for value in result.classification.values())


def test_descriptor_snapshot_retries_after_interrupted_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / ".git" / "objects" / "aa").mkdir(parents=True)
    (repo / ".git" / "objects" / "aa" / "object").write_bytes(b"object")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cell = _ProductionCell.__new__(_ProductionCell)
    cell.repo = repo
    cell.paths = SimpleNamespace(runtime=runtime)
    head = "b" * 40
    runtime_module = __import__("implbench.harness.runtime", fromlist=["_copy_descriptor_tree"])
    original = runtime_module._copy_descriptor_tree
    calls = {"count": 0}

    def flaky_copytree(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("interrupted snapshot")
        return original(*args, **kwargs)

    monkeypatch.setattr("implbench.harness.runtime._copy_descriptor_tree", flaky_copytree)
    descriptor = cell.descriptor_root({"head": head})
    assert calls["count"] >= 2
    assert (descriptor / "refs" / "implbench" / "candidate").read_text() == head + "\n"
    assert not list(runtime.glob(".descriptor-stage-*"))


def test_scored_controls_refuse_unsupported_engine_before_turn() -> None:
    bridge = Bridge.__new__(Bridge)
    bridge.engine_name = "pi-rpc"
    request = SimpleNamespace(id="r13", payload={"fresh_context": True, "reasoning_effort": "medium"})
    with pytest.raises(EngineError, match="unsupported"):
        bridge.reset_context_if_requested(request, object(), required=True)

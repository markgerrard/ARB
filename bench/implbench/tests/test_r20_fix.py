from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_redis_bridge.bridge import Bridge
from implbench.harness import dispatch
from implbench.harness.controller import merge_runtime_context
from implbench.harness.git_service import RemoteGitService
from implbench.harness.importer import ImportGraphAttestation
from implbench.harness import runtime as runtime_module
from implbench.harness.runtime import ProductionRuntimeUnavailable, _ProductionController, build_production_scorer
from implbench.harness.schedule import expand_schedule
from implbench.harness.scorer_launcher import (
    SUPERVISOR_REGISTRATION_MAX_BYTES,
    _write_supervisor_registration,
)
from implbench.harness.scorer_sandbox import ScorerUidLauncher
from implbench.harness.scorer_sandbox import G4ReceiptBinding, PostImportInput, ScorerRole, ScorerRunResult


def test_controller_merges_remote_close_placeholder_truth_table() -> None:
    completion = {"infrastructure_failure": "awaiting-controller-close"}
    merge_runtime_context(completion, {"infrastructure_failure": None})
    assert completion["infrastructure_failure"] is None

    completion = {"infrastructure_failure": "awaiting-controller-close"}
    merge_runtime_context(completion, {"infrastructure_failure": "real-rpc-failure"})
    assert completion["infrastructure_failure"] == "real-rpc-failure"

    merge_runtime_context(completion, {"infrastructure_failure": "different-real-failure"})
    assert completion["infrastructure_failure"] == "completion-projection-conflict"

    completion = {"infrastructure_failure": "real-rpc-failure"}
    merge_runtime_context(completion, {"infrastructure_failure": None})
    assert completion["infrastructure_failure"] == "real-rpc-failure"

    completion = {"infrastructure_failure": None}
    merge_runtime_context(completion, {"infrastructure_failure": "awaiting-controller-close"})
    assert completion["infrastructure_failure"] is None

    merge_runtime_context(completion, {"infrastructure_failure": "real-rpc-failure"})
    merge_runtime_context(completion, {"infrastructure_failure": "awaiting-controller-close"})
    assert completion["infrastructure_failure"] == "real-rpc-failure"


def test_scored_dispatch_clears_remote_close_placeholder_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Exercise bridge projection, dispatch seeding, close projection, and classification."""
    task = SimpleNamespace(task_id="c1-parser", allowed_paths=("src/*.py",), expected_artifacts=(), brief="", timeout_s=1)
    cell = next(item for item in expand_schedule("00" * 32, [("c1-parser", "a" * 64)] + [(f"c{i}", "a" * 64) for i in range(2, 9)])
                if item.task_id == "c1-parser")
    receipt = "b" * 40
    service = RemoteGitService(endpoint="/tmp/implbench-r20.sock", capability="c" * 64, tool_gid=49123)
    projected = Bridge.project_scored_completion({}, service)
    assert projected["infrastructure_failure"] == "awaiting-controller-close"

    class Lifecycle:
        def open_attempt_git_service(self, *_args, **_kwargs):
            return {"endpoint": "/tmp/implbench-r20.sock", "capability": "c" * 64}

        def close_attempt_git_service(self):
            return None

        def start_attempt_planes(self, _binding):
            return None

        def dispatch_through_control(self, _task, _engine, *, timeout):
            return {"status": "ok", "completion": projected}

        def completion_projection(self):
            return {"receipt_oids": [receipt], "dirty": False, "seal_complete": True,
                    "receipts_authenticated": True, "infrastructure_failure": None}

        def __getattr__(self, name):
            if name in {"stop_tools", "drain_rpc", "kill_planes", "close_acl", "final_status", "kill_git", "census_snapshot", "destroy"}:
                return lambda: None
            raise AttributeError(name)

    lifecycle = Lifecycle()

    class Runtime:
        def verify_delivery(self):
            return SimpleNamespace(decision="agent-delivered")

        def import_and_score(self):
            return {"imported_oids": (receipt,), "imported_graph_attested": True,
                    "g1": "PASS", "g3": "PASS", "g4": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS", "g4_receipts": ()}

    Runtime.lifecycle = lifecycle

    monkeypatch.setattr(dispatch, "_dispatch", lambda *args, **kwargs: dispatch.DispatchResult("ok", completion=projected))
    result = dispatch.run_task(
        task, "seat", "pi-rpc", "a" * 64, "oi-pi-bakeoff-r20b", tmp_path,
        schedule_cell=cell, fixture_root_oid="d" * 40, tool_gid=49123, cell_root=tmp_path,
        scored_runtime_factory=lambda **_: Runtime(), scored_lifecycle=lifecycle,
        recorder=SimpleNamespace(path=tmp_path / "close.ndjson"),
    )
    assert result.completion["infrastructure_failure"] is None
    assert any(value != "UNKNOWN" for value in result.completion["classification"].values())


def test_scored_close_carries_model_limit_proof_to_binding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from implbench.harness.dispatch import DispatchResult, ScoredDispatchBinding
    from implbench.harness.cell_runtime import attempt_id_for

    task = SimpleNamespace(task_id="c1-parser", allowed_paths=("src/*.py",), expected_artifacts=(), brief="", timeout_s=1)
    cell = next(item for item in expand_schedule("00" * 32, [("c1-parser", "a" * 64)] + [(f"c{i}", "a" * 64) for i in range(2, 9)]) if item.task_id == "c1-parser")
    receipt = "b" * 40

    class Lifecycle:
        def open_attempt_git_service(self, *_args, **_kwargs):
            return {"endpoint": "/tmp/implbench-r21.sock", "capability": "c" * 64}
        def close_attempt_git_service(self):
            return None
        def start_attempt_planes(self, _binding):
            return None
        def dispatch_through_control(self, _task, _engine, *, timeout):
            return {"status": "ok", "completion": projected}
        def completion_projection(self):
            return {"receipt_oids": [receipt], "dirty": False, "seal_complete": True,
                    "receipts_authenticated": True, "infrastructure_failure": None}
        def __getattr__(self, name):
            if name in {"stop_tools", "drain_rpc", "kill_planes", "close_acl", "final_status", "kill_git", "census_snapshot", "destroy"}:
                return lambda: None
            raise AttributeError(name)

    class Runtime:
        def verify_delivery(self):
            return SimpleNamespace(decision="agent-delivered")
        def import_and_score(self):
            return {"imported_oids": (receipt,), "imported_graph_attested": True, "model_limit_proven": True,
                    "g1": "FAIL", "g3": "UNKNOWN", "g4": "UNKNOWN", "g5": "UNKNOWN", "g6": "UNKNOWN", "g7": "UNKNOWN", "g4_receipts": ()}
    projected = Bridge.project_scored_completion(
        {}, RemoteGitService(endpoint="/tmp/implbench-r21.sock", capability="c" * 64, tool_gid=49123),
    )
    lifecycle = Lifecycle()
    Runtime.lifecycle = lifecycle
    monkeypatch.setattr(dispatch, "_dispatch", lambda *args, **kwargs: DispatchResult("ok", completion=projected))
    result = dispatch.run_task(task, "seat", "pi-rpc", "a" * 64, "oi-pi-bakeoff-r21", tmp_path,
                               schedule_cell=cell, fixture_root_oid="d" * 40, tool_gid=49123, cell_root=tmp_path,
                               scored_runtime_factory=lambda **_: Runtime(), scored_lifecycle=lifecycle,
                               recorder=SimpleNamespace(path=tmp_path / "limit.ndjson"))
    assert result.completion["model_limit_proven"] is True
    assert result.completion["classification"]["G1"] == "FAIL"

    binding = ScoredDispatchBinding(
        run_id="oi-pi-bakeoff-r21", repo=tmp_path,
        task_for_cell=lambda _cell: task, seat_for_cell=lambda _cell: "seat", engine_for_cell=lambda _cell: "pi-rpc",
        fixture_root_oid_for_cell=lambda _cell: "d" * 40, tool_gid_for_cell=lambda _cell: 49123,
        scored_runtime_factory=lambda **_: Runtime(), dispatch_fn=lambda *_args, **_kwargs: result,
    )
    outcome = binding(cell, attempt_id_for(cell.cell_id, 1))
    assert outcome.status == "FAIL" and not outcome.infrastructure and outcome.cause == "submitted-model-limit"


@pytest.mark.parametrize("shape", [
    "success",
    "six-flat", "empty", "incomplete", "duplicate", "wrong-role", "missing-exit", "invalid-exit",
    "nonzero-role", "wrong-child-placement", "missing-child", "nonzero-child",
])
def test_production_scorer_rejects_malformed_role_results(
    shape: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    binary = tmp_path / "scorer"
    binary.write_text("#!/bin/sh\n[ \"$1\" = --version ] && { echo scorer-v1; exit 0; }\n", encoding="utf-8")
    binary.chmod(0o700)
    public_digest = "b" * 64
    manifest = {"pins": {"scorer": {"version": "scorer-v1", "digest": "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()},
                         "public_suite": {"digest": "sha256:" + public_digest, "digest_version": "v1"}},
                "budgets": {"scorer_max_output_bytes": 1024}}
    monkeypatch.setenv("IMPLBENCH_SCORER_BIN", str(binary))
    monkeypatch.setenv("IMPLBENCH_PUBLIC_SUITE_OID", "c" * 40)
    monkeypatch.setenv("IMPLBENCH_BATTERY_KEY", "test-key")
    for index, name in enumerate(("IMPLBENCH_SCORER_KEYED_RUNNER_UID", "IMPLBENCH_SCORER_BROKER_UID",
                                  "IMPLBENCH_SCORER_SUBMITTED_PROGRAM_UID", "IMPLBENCH_SCORER_COORDINATOR_UID",
                                  "IMPLBENCH_SCORER_SUITE_RUNNER_BROKER_UID", "IMPLBENCH_SCORER_SUBMITTED_CODE_UID"), 101):
        monkeypatch.setenv(name, str(index))
    roles = [role.value for role in (ScorerRole.KEYED_RUNNER, ScorerRole.BROKER,
                                     ScorerRole.COORDINATOR, ScorerRole.SUITE_RUNNER_BROKER)]
    rows = [ScorerRunResult(role, 0, "", "", 0 if role in {"broker", "suite-runner/broker"} else None) for role in roles]
    if shape == "six-flat":
        rows = [ScorerRunResult(role.value, 0, "", "") for role in ScorerRole]
    elif shape == "empty": rows = []
    elif shape == "incomplete": rows.pop()
    elif shape == "duplicate": rows[-1] = ScorerRunResult(rows[0].role, 0, "", "")
    elif shape == "wrong-role": rows[-1] = ScorerRunResult("other", 0, "", "")
    elif shape == "missing-exit": rows[-1] = SimpleNamespace(role=rows[-1].role, stdout="", stderr="", submitted_child_exit_code=None)
    elif shape == "invalid-exit": rows[-1] = ScorerRunResult(rows[-1].role, True, "", "")
    elif shape == "nonzero-role": rows[-1] = ScorerRunResult(rows[-1].role, 7, "", "")
    elif shape == "wrong-child-placement": rows[0] = ScorerRunResult(rows[0].role, 0, "", "", 0)
    elif shape == "missing-child": rows[1] = ScorerRunResult(rows[1].role, 0, "", "", None)
    elif shape == "nonzero-child": rows[1] = ScorerRunResult(rows[1].role, 0, "", "", 7)

    class Sandbox:
        def __init__(self, _root, _input, topology, **_kwargs):
            self.gate = topology.gate
            self.last_graph_result = {"g1": "PASS", "g3": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS"} if self.gate == "G1" else {"g4": "PASS", "g4_receipts": ()}
        def run_topology(self, commands, **kwargs):
            if self.gate == "G4" and shape == "success":
                self.last_graph_result = {"g4": "PASS", "g4_receipts": tuple({
                    "cell_id": item.cell_id, "attempt_id": item.attempt_id, "commit_oid": item.commit_oid,
                    "public_suite_oid": item.public_suite_oid, "public_suite_digest": item.public_suite_digest,
                    "public_suite_digest_version": item.public_suite_digest_version, "outcome_enum": "PASS",
                    "controller_sequence": item.controller_sequence, "nonce": item.nonce,
                } for item in kwargs["g4_receipt_bindings"])}
            return tuple(row for row in rows if row.role in {role.value for role in commands})

    monkeypatch.setattr(runtime_module, "ScorerSandbox", Sandbox)
    materialization = tmp_path / "materialization"; materialization.mkdir()
    cell_id, attempt_id = "cell-" + "a" * 64, "attempt-" + "b" * 32
    binding = G4ReceiptBinding(cell_id, attempt_id, "f" * 40, "c" * 40, public_digest, "v1", 1, "d" * 64)
    attestation = {"completion": {"cell_id": cell_id, "attempt_id": attempt_id, "receipts": [{"commit_oid": "f" * 40, "controller_sequence": 1}]}, "g4_receipt_bindings": (binding,)}
    score = build_production_scorer(manifest)
    if shape == "success":
        assert score(PostImportInput(materialization, "a" * 64), attestation)["g4"] == "PASS"
    else:
        with pytest.raises(ProductionRuntimeUnavailable, match="production scorer execution failed"):
            score(PostImportInput(materialization, "a" * 64), attestation)


def test_production_controller_attestation_callback_converts_real_attestation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    controller = object.__new__(_ProductionController)
    cell_id, attempt_id = "cell-x", "attempt-x"
    cell = SimpleNamespace(attempt_id=attempt_id, cell=object(), receipt_key=b"k" * 32, repo=tmp_path,
                           bind_receipts=lambda *_args, **_kwargs: None, descriptor_root=lambda _status: tmp_path)
    controller._cells = {cell_id: cell}
    controller.task_for_cell = lambda _cell: SimpleNamespace(allowed_paths=("src/*.py",))
    controller.fixture_root_oid_for_cell = lambda _cell: "a" * 40
    controller.manifest = {"pins": {"public_suite": {"digest": "sha256:" + "b" * 64, "digest_version": "v1"}}}
    controller.run_id, controller.runtime_root, controller.scored_scorer_factory = "oi-pi-bakeoff-r20b", tmp_path, lambda *_: {}
    captured = {}
    monkeypatch.setenv("IMPLBENCH_PUBLIC_SUITE_OID", "c" * 40)
    monkeypatch.setattr(runtime_module, "attest_imported_graph", lambda *_args, **_kwargs: ImportGraphAttestation(True, "d" * 64, ("e" * 40,), tmp_path, "f" * 64))
    monkeypatch.setattr(runtime_module.ScoredCloseRuntime, "from_descriptor", classmethod(lambda _cls, **kwargs: captured.update(kwargs) or object()))
    controller.scored_runtime_factory(completion={"receipt_records": [], "status": {}}, cell_id=cell_id, attempt_id=attempt_id)
    converted = captured["attestation_verifier"](object(), SimpleNamespace(payload={"receipts": []}))
    assert converted == {"attested": True, "imported_graph_digest": "d" * 64, "object_ids": ("e" * 40,), "materialization": tmp_path,
                         "materialization_digest": "f" * 64, "public_suite_oid": "c" * 40, "public_suite_digest": "b" * 64, "public_suite_digest_version": "v1"}
    monkeypatch.setattr(runtime_module, "attest_imported_graph", lambda *_args, **_kwargs: object())
    with pytest.raises(ProductionRuntimeUnavailable, match="public suite binding"):
        captured["attestation_verifier"](object(), SimpleNamespace(payload={"receipts": []}))


@pytest.mark.parametrize("force_enumeration_failure", [False, True], ids=["dev-fd", "fallback"])
def test_fd_cleanup_closes_high_fd_and_keeps_explicit_allowset(force_enumeration_failure: bool) -> None:
    read_fd, write_fd = os.pipe()
    code = """
import os, resource, sys
from implbench.harness import scorer_launcher
allowed, force_failure = map(int, sys.argv[1:])
original_limits = resource.getrlimit(resource.RLIMIT_NOFILE)
if original_limits[1] != resource.RLIM_INFINITY and original_limits[1] < 4096:
    raise RuntimeError('test host cannot create FD 2048')
resource.setrlimit(resource.RLIMIT_NOFILE, (4096, original_limits[1]))
high = 2048
os.dup2(allowed, high)
# Deliberately irreversible in this subprocess: its exit confines the lowered
# hard limit while preserving the exact adversarial condition.
resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
if force_failure:
    scorer_launcher.os.listdir = lambda _: (_ for _ in ()).throw(OSError('forced'))
scorer_launcher._close_fds_except({1, 2, allowed})
try:
    os.fstat(high)
except OSError:
    high_state = 'CLOSED'
else:
    high_state = 'LEAK'
os.write(allowed, f'{high_state} ALLOWED\\n'.encode())
"""
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(write_fd), str(int(force_enumeration_failure))],
            pass_fds=(write_fd,), close_fds=True,
        )
        os.close(write_fd)
        write_fd = -1
        result = os.read(read_fd, 4096)
        assert child.wait(timeout=5) == 0
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
    assert result == b"CLOSED ALLOWED\n"


def test_scored_binding_model_limit_is_fail_only_with_authoritative_proof() -> None:
    from implbench.harness.dispatch import DispatchResult, ScoredDispatchBinding
    from implbench.harness.cell_runtime import attempt_id_for
    from implbench.harness.phases import PilotPhase

    cell = next(iter(expand_schedule("00" * 32, [(f"task-{index}", "a" * 40) for index in range(8)])))
    base = {"G0": "PASS", "G2": "agent-delivered", "G1": "FAIL", "G3": "UNKNOWN", "G4": "UNKNOWN",
            "G5": "UNKNOWN", "G6": "UNKNOWN", "G7": "UNKNOWN"}
    def binding(proven: bool):
        return ScoredDispatchBinding(
            run_id="oi-pi-bakeoff-r21-test", repo=Path("/tmp/implbench-r21-repo"),
            task_for_cell=lambda value: SimpleNamespace(task_id=value.task_id, expected_artifacts=(), allowed_paths=(), brief="", timeout_s=1),
            seat_for_cell=lambda _value: "seat", engine_for_cell=lambda _value: "pi-rpc",
            fixture_root_oid_for_cell=lambda _value: "b" * 40, tool_gid_for_cell=lambda _value: 1,
            scored_runtime_factory=lambda **_kwargs: None,
            dispatch_fn=lambda *_args, **_kwargs: DispatchResult("ok", completion={"classification": base, "model_limit_proven": proven}),
        )
    attempt_id = attempt_id_for(cell.cell_id, 1)
    assert binding(True)(cell, attempt_id).status == "FAIL"
    unproven = binding(False)(cell, attempt_id)
    assert unproven.status == "UNKNOWN" and unproven.infrastructure and unproven.cause == "infrastructure-unknown"

    calls: list[str] = []
    pilot = PilotPhase(
        {"seed": "00" * 32, "tasks": [{"task_id": f"task-{index}", "fixture_sha": "a" * 40} for index in range(8)]},
        execute=lambda scheduled, submitted_attempt: calls.append(submitted_attempt) or binding(True)(scheduled, submitted_attempt),
        append_attempt=lambda _outcome: None, manifest_bytes=b"manifest", config_bytes=b"config",
        refs=(), journal_tail=b"journal", max_same_cause_failures=1,
    ).run()
    assert len(calls) == len(pilot.outcomes) == 32
    assert all(outcome.status == "FAIL" and not outcome.infrastructure for outcome in pilot.outcomes)


def test_supervisor_registration_accepts_exact_512_bytes_and_rejects_oversize() -> None:
    read_fd, write_fd = os.pipe()
    value = {"boundary": 1, "helper": 2, "broker": 3, "child": 4, "padding": ""}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    value["padding"] = "x" * (SUPERVISOR_REGISTRATION_MAX_BYTES - len(encoded))
    try:
        _write_supervisor_registration(write_fd, value)
        raw = os.read(read_fd, SUPERVISOR_REGISTRATION_MAX_BYTES)
        assert len(raw) == SUPERVISOR_REGISTRATION_MAX_BYTES
        assert ScorerUidLauncher._supervisor_registration_from_bytes(raw) == (1, 2, 3, 4)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(RuntimeError, match="oversized"):
            _write_supervisor_registration(write_fd, {**value, "padding": value["padding"] + "x"})
    finally:
        os.close(read_fd)
        os.close(write_fd)

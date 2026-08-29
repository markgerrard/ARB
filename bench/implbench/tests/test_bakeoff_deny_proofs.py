from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from implbench.harness import dispatch, validate
from implbench.harness.dispatch import ScoredDispatchBinding
from implbench.harness.evidence import EvidencePackage, EvidencePackageError
from implbench.harness.phases import AttemptOutcome, PilotPhase
from implbench.harness.runner import MatrixRunner
from implbench.harness.schedule import expand_schedule
from implbench.harness.cell_runtime import attempt_id_for
from implbench.harness.scorer_sandbox import ScorerInputError, ScorerRole, ScorerSandbox, build_g1_topology, post_import_input
from agent_redis_bridge.bridge import Bridge
from attempt_service_fixture import lifecycle as attempt_lifecycle


def _manifest() -> dict[str, object]:
    return {
        "run_id": "oi-pi-bakeoff-deny-20260714T000000Z",
        "source": {"realpath": "/private/tmp/controller-source", "commit": "a" * 40, "tree": "b" * 40, "dirty": False},
    }


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        task_id="c1-parser",
        expected_artifacts=("src/result.py",),
        allowed_paths=("src/*.py",),
        brief="return the structured result",
        timeout_s=1,
    )


def _schedule_cell() -> object:
    tasks = [("c1-parser", "a" * 64)] + [(f"c{i}", "a" * 64) for i in range(2, 9)]
    return next(cell for cell in expand_schedule("00" * 32, tasks) if cell.task_id == "c1-parser")


def _assert_scored_dispatch_contract(events: list[dict[str, object]]) -> None:
    dispatch_event = next(event for event in events if event["op"] == "dispatch")
    assert set(dispatch_event) == {"op", "task", "engine", "timeout"}
    assert "controller-secret" not in json.dumps(dispatch_event, sort_keys=True)
    assert all("capability" not in event for event in events)


def test_scored_dispatch_scrubs_secret_and_legacy_host_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IMPLBENCH_BATTERY_KEY", "controller-secret")
    service_lifecycle = attempt_lifecycle(tmp_path)
    dispatch.run_task(
        _task(), "seat-a", "pi-sdk", "a" * 64, "oi-pi-bakeoff-deny-20260714T000000Z", tmp_path,
        schedule_cell=_schedule_cell(), fixture_root_oid="c" * 40, tool_gid=49123, cell_root=tmp_path,
        scored_runtime_factory=lambda **_: None,
        scored_lifecycle=service_lifecycle,
    )

    _assert_scored_dispatch_contract(service_lifecycle.events)
    broken = [
        ({**event, "task": "controller-secret"} if event["op"] == "dispatch" else dict(event))
        for event in service_lifecycle.events
    ]
    with pytest.raises(AssertionError):
        _assert_scored_dispatch_contract(broken)


def test_scored_run_tripwire_rejects_host_gated_completion_when_guard_is_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    good_lifecycle = attempt_lifecycle(tmp_path / "good", dispatch_result={
        "status": "ok", "completion": {"mode": "receipt-only"},
    })
    result = dispatch.run_task(
        _task(),
        "seat-a",
        "pi-sdk",
        "a" * 64,
        "oi-pi-bakeoff-deny-20260714T000000Z",
        tmp_path,
        schedule_cell=_schedule_cell(),
        fixture_root_oid="c" * 40,
        tool_gid=49123,
        cell_root=tmp_path,
        scored_runtime_factory=lambda **_: SimpleNamespace(import_and_score=lambda: None),
        scored_lifecycle=good_lifecycle,
    )
    assert result.completion["mode"] == "receipt-only"

    broken_lifecycle = attempt_lifecycle(tmp_path / "broken", dispatch_result={
        "status": "ok", "completion": {"mode": "host-gated"},
    })
    broken = dispatch.run_task(
        _task(),
        "seat-a",
        "pi-sdk",
        "a" * 64,
        "oi-pi-bakeoff-deny-20260714T000000Z",
        tmp_path,
        schedule_cell=_schedule_cell(),
        fixture_root_oid="c" * 40,
        tool_gid=49123,
        cell_root=tmp_path,
        scored_runtime_factory=lambda **_: SimpleNamespace(import_and_score=lambda: None),
        scored_lifecycle=broken_lifecycle,
    )
    with pytest.raises(AssertionError):
        assert broken.completion["mode"] == "receipt-only"


@pytest.mark.parametrize("missing", ["fixture_root_oid", "tool_gid", "scored_runtime_factory"])
def test_scored_run_requires_explicit_controller_metadata_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(dispatch, "_dispatch", lambda *args, **kwargs: calls.append(args) or pytest.fail("dispatch must not run"))
    kwargs: dict[str, object] = {
        "schedule_cell": _schedule_cell(),
        "fixture_root_oid": "c" * 40,
        "tool_gid": 49123,
        "scored_runtime_factory": lambda **_: SimpleNamespace(import_and_score=lambda: None),
        "scored_lifecycle": attempt_lifecycle(tmp_path),
    }
    kwargs.pop(missing)

    with pytest.raises(ValueError, match="fixture root|tool GID|runtime factory"):
        dispatch.run_task(
            _task(),
            "seat-a",
            "pi-sdk",
            "a" * 64,
            "oi-pi-bakeoff-deny-20260714T000000Z",
            tmp_path,
            **kwargs,
        )
    assert calls == []


def test_scored_run_capture_uses_real_dispatch_metadata_and_attempt_number(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cell = _schedule_cell()
    seen: dict[str, object] = {}

    def runtime_factory(**kwargs):
        seen["factory"] = kwargs
        return SimpleNamespace(import_and_score=lambda: None)

    service_lifecycle = attempt_lifecycle(tmp_path)
    result = dispatch.run_task(
        _task(),
        "seat-a",
        "pi-sdk",
        "a" * 64,
        "oi-pi-bakeoff-deny-20260714T000000Z",
        tmp_path,
        schedule_cell=cell,
        attempt_number=2,
        fixture_root_oid="c" * 40,
        tool_gid=49123,
        cell_root=tmp_path,
        scored_runtime_factory=runtime_factory,
        scored_lifecycle=service_lifecycle,
    )

    opened = next(event for event in service_lifecycle.events if event["op"] == "open")
    assert opened["attempt_id"] == attempt_id_for(cell.cell_id, 2)
    assert opened["allowed_paths"] == ("src/*.py",)
    assert "factory" not in seen
    assert result.completion["attempt_id"] == attempt_id_for(cell.cell_id, 2)


def test_matrix_binding_captures_the_real_scored_dispatch_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cell = _schedule_cell()
    captured: dict[str, object] = {}

    def wrapped_run_task(*args, **kwargs):
        captured["run_task_args"] = args
        captured["run_task_kwargs"] = kwargs
        return dispatch.run_task(*args, **kwargs)

    binding = ScoredDispatchBinding(
        run_id="oi-pi-bakeoff-deny-20260714T000000Z",
        repo=tmp_path,
        task_for_cell=lambda cell: _task(),
        seat_for_cell=lambda cell: "seat-a",
        engine_for_cell=lambda cell: "pi-sdk",
        fixture_root_oid_for_cell=lambda cell: "c" * 40,
        tool_gid_for_cell=lambda cell: 49123,
        scored_runtime_factory=lambda **_: SimpleNamespace(import_and_score=lambda: None),
        lifecycle_for_cell=lambda cell, attempt_id=None: attempt_lifecycle(tmp_path),
        cell_root_for_cell=lambda cell: tmp_path,
        dispatch_fn=wrapped_run_task,
    )

    outcome = binding(cell, attempt_id_for(cell.cell_id, 1))

    assert captured["run_task_kwargs"]["schedule_cell"] is cell
    assert captured["run_task_kwargs"]["attempt_number"] == 1
    assert outcome.status == "UNKNOWN"


def test_scored_bridge_mode_tripwire_cannot_become_host_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    scored = SimpleNamespace(run_id="oi-pi-bakeoff-deny-20260714T000000Z", payload={})
    assert Bridge.is_scored_request(scored)
    assert Bridge.completion_mode(scored) == "receipt-only"

    monkeypatch.setattr(Bridge, "is_scored_request", staticmethod(lambda request: False))
    with pytest.raises(AssertionError):
        assert Bridge.completion_mode(scored) == "receipt-only"


def test_classifier_deny_proofs_go_red_if_classifier_is_stubbed_green(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validate, "classify", lambda value: {f"G{i}": "PASS" for i in range(8)})
    report = validate.run_validate()
    assert report.ok is False
    assert set(report.misses) == {"null-implementor", "fabricator", "scope-escaper", "test-weakener", "rail-breaker", "discipline-skipper"}
    assert report.stubbed_green is True


def test_evidence_secret_and_sealed_mutation_denials_are_mechanical(tmp_path: Path) -> None:
    package = EvidencePackage.create(tmp_path / "evidence", _manifest())
    with pytest.raises(EvidencePackageError):
        package.append_public({"secret": "controller-secret"})
    package.append_private_digest("controller-secret diagnostic")
    package.seal([])

    for mutation in (
        lambda: package.append_public({"status": "late"}),
        lambda: package.append_private_digest("late diagnostic"),
        lambda: package.seal([]),
    ):
        with pytest.raises(EvidencePackageError):
            mutation()
    package.validate(require_sealed=True)
    assert b"controller-secret diagnostic" not in (package.root / "preflight" / "private-digests.ndjson").read_bytes()


def test_extension_and_active_checkout_inputs_are_not_injected_into_scored_launch() -> None:
    argv = dispatch._argv(_task(), "seat-a", "pi-sdk", "a" * 64, "oi-pi-bakeoff-deny-20260714T000000Z", cell_root=Path("/Users/Shared/arb-implbench/cell"))
    assert "--worktree" not in argv
    assert "--cell-root" in argv
    assert not any(value.startswith(("--extension", "--plugin", "--skill", "--cwd")) for value in argv)
    assert str(Path.cwd()) not in argv


def test_scored_dispatch_without_controller_gid_does_not_self_attest_service_gid(tmp_path: Path) -> None:
    from agent_redis_bridge.engines.base import EngineError

    class Service:
        receipt_chain = object()
        tool_gid = 20

        def handle(self, request, *, actor):
            return {"request": request, "actor": actor}

        def completion_projection(self):
            return {}

    bridge = Bridge.__new__(Bridge)
    bridge.args = SimpleNamespace(scored_git_service_factory=lambda envelope, worktree: Service())
    envelope = SimpleNamespace(
        run_id="oi-pi-bakeoff-deny-20260714T000000Z",
        payload={
            "cell_id": "cell-" + "a" * 64,
            "attempt_id": "attempt-" + "b" * 64,
            "fixture_root_oid": "c" * 40,
            "allowed_paths": ["src/**"],
        },
    )

    with pytest.raises(EngineError, match="provenance|GID"):
        bridge._bind_scored_tool_plane(envelope, tmp_path)


def test_matrix_execution_never_reaches_a_live_subprocess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest = {
        "run_id": "oi-pi-bakeoff-deny-20260714T000000Z",
        "seed": "00" * 32,
        "evidence": {"root": str(tmp_path / "evidence")},
        "tasks": [{"task_id": f"task-{index}", "fixture_sha": "a" * 64} for index in range(8)],
    }
    pilot = PilotPhase(
        manifest,
        execute=lambda cell, attempt_id: AttemptOutcome(cell.cell_id, attempt_id, "PASS", False),
        append_attempt=lambda outcome: None,
        manifest_bytes=json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        config_bytes=b"config",
        refs=(),
        journal_tail=b"journal",
    ).run()

    def forbidden(*args, **kwargs):
        raise AssertionError("scored matrix execution reached a live subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = MatrixRunner(
        manifest,
        pilot_seal=pilot.seal,
        execute=lambda cell, attempt_id: AttemptOutcome(cell.cell_id, attempt_id, "PASS", False),
    ).run()
    assert result.complete is True


def test_scorer_rejects_secret_output_and_non_post_import_materialization(tmp_path: Path) -> None:
    materialized = tmp_path / "imported"
    materialized.mkdir()
    input_value = post_import_input(materialized, digest="c" * 64)
    topology = build_g1_topology(keyed_runner_uid=101, broker_uid=102, submitted_program_uid=103, battery_key="controller-secret")
    class Launcher:
        def run(self, argv, *, uid, cwd, env, timeout):
            del argv, uid, cwd, env, timeout
            return type("Completed", (), {"returncode": 0, "stdout": "controller-secret\n", "stderr": ""})()

    sandbox = ScorerSandbox(tmp_path, input_value, topology, launcher=Launcher())
    with pytest.raises(ScorerInputError, match="exfiltrated"):
        sandbox.run(ScorerRole.SUBMITTED_PROGRAM, [sys.executable, "-c", "print('controller-secret')"])
    with pytest.raises(ScorerInputError):
        post_import_input(materialized, digest="c" * 64, provenance="live-cell")

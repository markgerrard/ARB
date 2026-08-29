from __future__ import annotations

import os
from pathlib import Path

import pytest

from implbench.harness.scorer_sandbox import (
    BatteryBoundaryError,
    ScorerInputError,
    ScorerRole,
    ScorerSandbox,
    build_g1_topology,
    build_g4_topology,
    post_import_input,
)


def test_g1_topology_has_three_distinct_roles_and_only_runner_gets_key() -> None:
    topology = build_g1_topology(
        keyed_runner_uid=101,
        broker_uid=102,
        submitted_program_uid=103,
        battery_key="controller-secret",
    )

    assert [process.role for process in topology.processes] == [
        ScorerRole.KEYED_RUNNER,
        ScorerRole.BROKER,
        ScorerRole.SUBMITTED_PROGRAM,
    ]
    assert len({process.uid for process in topology.processes}) == 3
    assert topology.processes[0].environment["IMPLBENCH_BATTERY_KEY"] == "controller-secret"
    assert all(
        "IMPLBENCH_BATTERY_KEY" not in process.environment
        for process in topology.processes[1:]
    )
    assert all("controller-secret" not in repr(process) for process in topology.processes[1:])


def test_g4_topology_is_keyless_and_pins_public_suite() -> None:
    topology = build_g4_topology(
        coordinator_uid=201,
        broker_uid=202,
        submitted_code_uid=203,
        public_suite_oid="a" * 40,
        public_suite_digest="b" * 64,
    )

    assert [process.role for process in topology.processes] == [
        ScorerRole.COORDINATOR,
        ScorerRole.SUITE_RUNNER_BROKER,
        ScorerRole.SUBMITTED_CODE,
    ]
    assert all("IMPLBENCH_BATTERY_KEY" not in process.environment for process in topology.processes)
    assert topology.public_suite_oid == "a" * 40
    assert topology.public_suite_digest == "b" * 64


def test_post_import_input_rejects_live_cell_preimport_and_fixture_surrogates(tmp_path: Path) -> None:
    worktree = tmp_path / "imported"
    worktree.mkdir()
    for provenance in ("live-cell", "pre-import", "fixture-tip", "controller-working-tree"):
        with pytest.raises(ScorerInputError):
            post_import_input(worktree, digest="c" * 64, provenance=provenance)


def test_post_import_input_requires_descriptor_safe_digest(tmp_path: Path) -> None:
    worktree = tmp_path / "imported"
    worktree.mkdir()
    (worktree / "module.py").write_text("VALUE = 1\n")
    with pytest.raises(ScorerInputError):
        post_import_input(worktree, digest="not-a-digest", provenance="post-import")
    link = tmp_path / "link"
    link.symlink_to(worktree, target_is_directory=True)
    with pytest.raises(ScorerInputError):
        post_import_input(link, digest="c" * 64, provenance="post-import")


def test_legacy_host_battery_execution_is_a_hard_boundary(monkeypatch, tmp_path: Path) -> None:
    from implbench.harness import battery

    monkeypatch.setattr(battery, "decrypt", lambda *_args, **_kwargs: os.urandom(8))
    with pytest.raises(BatteryBoundaryError):
        battery.run_battery(object(), tmp_path)  # type: ignore[arg-type]


def test_scorer_launcher_receives_declared_uid_instead_of_topology_only(tmp_path: Path) -> None:
    materialization = tmp_path / "imported"
    materialization.mkdir()
    input_value = post_import_input(materialization, digest="c" * 64)
    topology = build_g1_topology(keyed_runner_uid=101, broker_uid=102, submitted_program_uid=103, battery_key="secret")
    seen: list[int] = []

    class Launcher:
        def run(self, argv, *, uid, cwd, env, timeout):
            del argv, cwd, env, timeout
            seen.append(uid)
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    sandbox = ScorerSandbox(tmp_path, input_value, topology, launcher=Launcher())
    sandbox.run(ScorerRole.SUBMITTED_PROGRAM, ["/bin/true"])

    assert seen == [103]

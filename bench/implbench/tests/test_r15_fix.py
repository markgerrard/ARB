from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from implbench.harness.controller import merge_runtime_context
from implbench.harness.runtime import ProductionRuntimeUnavailable, _ProductionCell, build_production_controller
from implbench.harness.scorer_sandbox import ScorerModelExecutionLimit, ScorerRole, ScorerSandbox, build_g1_topology, post_import_input, reap_and_prove_empty


def _manifest(repo: Path, evidence: Path) -> dict:
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    tasks = ["c1-permissive-boundary", "c1-token-bucket", "c2-parser", "c3-refactor", "c4-rail", "c5-artifact", "c6-scope", "c7-provenance"]
    return {
        "run_id": "oi-pi-bakeoff-r15-" + evidence.name,
        "source": {"realpath": str(repo)}, "base_sha": base, "seed": "00" * 32,
        "tasks": [{"task_id": task, "fixture_sha": base} for task in tasks],
        "arms": [
            {"arm": "glm-pi", "engine": "pi-sdk", "agent_prefix": "pi-glm"},
            {"arm": "glm-zcode", "engine": "openinterpreter", "agent_prefix": "oi-glm"},
            {"arm": "kimi-pi", "engine": "pi-sdk", "agent_prefix": "pi-kimi"},
            {"arm": "kimi-cli", "engine": "openinterpreter", "agent_prefix": "oi-kimi"},
        ],
        "evidence": {"root": str(evidence)},
    }


def test_unpatched_controller_fails_closed_without_real_host_authority(tmp_path: Path) -> None:
    with pytest.raises(ProductionRuntimeUnavailable, match="ARB_MEMORY_REDIS_URL"):
        build_production_controller(_manifest(Path(__file__).parents[3], tmp_path / "evidence"))


def test_context_merge_rejects_receipt_and_failure_conflicts() -> None:
    context = {"receipts": ("a" * 40,), "infrastructure_failure": "first"}
    merge_runtime_context(context, {"receipt_oids": ["b" * 40], "infrastructure_failure": "second"})
    assert context["receipts"] == ("a" * 40,)
    assert context["infrastructure_failure"] == "completion-projection-conflict"
    merge_runtime_context(context, {"receipts": (), "infrastructure_failure": None})
    assert context["receipts"] == ("a" * 40,)
    assert context["infrastructure_failure"] == "completion-projection-conflict"


def test_descriptor_policy_failure_removes_stage_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / ".git" / "objects").mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    def reject(*args, **kwargs):
        raise ProductionRuntimeUnavailable("policy rejection")

    monkeypatch.setattr("implbench.harness.runtime._copy_descriptor_tree", reject)
    cell = _ProductionCell.__new__(_ProductionCell)
    cell.repo = source
    cell.paths = SimpleNamespace(runtime=runtime)
    with pytest.raises(ProductionRuntimeUnavailable, match="policy rejection"):
        cell.descriptor_root({"head": "a" * 40})
    assert not list(runtime.glob(".descriptor-stage-*"))


def test_scorer_output_cap_is_a_failure_not_truncation(tmp_path: Path) -> None:
    materialization = tmp_path / "imported"
    materialization.mkdir()
    input_value = post_import_input(materialization, digest="c" * 64)
    topology = build_g1_topology(keyed_runner_uid=101, broker_uid=102, submitted_program_uid=103, battery_key="secret")

    class Launcher:
        def run(self, argv, *, uid, cwd, env, timeout):
            del argv, uid, cwd, env, timeout
            return type("Completed", (), {"returncode": 0, "stdout": "x" * 65_537, "stderr": ""})()

    sandbox = ScorerSandbox(tmp_path, input_value, topology, launcher=Launcher(), max_output_bytes=65_536)
    with pytest.raises(ScorerModelExecutionLimit):
        sandbox.run(ScorerRole.SUBMITTED_PROGRAM, ["/bin/true"])


def test_cleanup_escalates_term_to_kill_and_requires_empty_census() -> None:
    remaining = {11, 12}
    signals: list[tuple[int, int]] = []

    def census(uid):
        del uid
        return set(remaining)

    def kill(pid, sig):
        signals.append((pid, sig))
        if sig == 9:
            remaining.clear()

    reap_and_prove_empty(123, list_processes=census, kill_process=kill, grace_s=0)
    assert signals == [(11, 15), (12, 15), (11, 9), (12, 9)]

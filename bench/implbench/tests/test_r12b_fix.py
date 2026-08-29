from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest

from implbench.harness import dispatch
from implbench.harness.dispatch import DispatchResult
from implbench.harness.runtime import _PersistentIdentityStore, ProductionRuntimeUnavailable
from implbench.harness.cell_runtime import PlaneIdentities
from implbench.harness.runtime import build_production_controller
from implbench.harness.schedule import expand_schedule
from attempt_service_fixture import lifecycle as attempt_lifecycle


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        task_id="task-1",
        expected_artifacts=(),
        allowed_paths=("src/*.py",),
        brief="brief",
        timeout_s=1,
    )


def _cell():
    tasks = [(f"task-{index}", "a" * 40) for index in range(8)]
    return expand_schedule("00" * 32, tasks)[0]


def test_production_identity_store_is_persistent_and_collision_free(tmp_path: Path) -> None:
    tasks = [(f"task-{index}", "a" * 40) for index in range(8)]
    cell_a, cell_b = _cell().cell_id, expand_schedule("11" * 32, tasks)[0].cell_id
    store = _PersistentIdentityStore(tmp_path)
    with pytest.raises(ProductionRuntimeUnavailable, match="synthetic identity"):
        store.get(cell_a)
    first = PlaneIdentities(41001, 41002, 41003, 41004)
    second = PlaneIdentities(41011, 41012, 41013, 41014)
    store.put(cell_a, first)
    store.put(cell_b, second)
    first = _PersistentIdentityStore(tmp_path).get(cell_a)
    second = _PersistentIdentityStore(tmp_path).get(cell_b)
    recovered = _PersistentIdentityStore(tmp_path).get(cell_a)

    assert first == recovered
    assert set((*first, first.tool_gid)).isdisjoint(set((*second, second.tool_gid)))
    assert (tmp_path / "preflight" / "cell-identities.json").stat().st_mode & 0o777 == 0o600


def test_scored_argv_requires_fresh_medium_and_controller_cell_root(tmp_path: Path) -> None:
    argv = dispatch._argv(
        _task(), "seat", "pi-sdk", "a" * 40, "oi-pi-bakeoff-r12b-test",
        cell_id=_cell().cell_id, attempt_id="attempt-" + "b" * 64,
        fixture_root_oid="c" * 40, tool_gid=1234, cell_root=tmp_path / "cell",
    )

    assert "--fresh-context" in argv
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--cell-root") + 1] == str(tmp_path / "cell")


def test_missing_scored_projection_distinguishes_mode_and_namespace() -> None:
    assert dispatch._missing_scored_completion_fields({})[:2] == ("missing-mode", "missing-ref-namespace")
    assert dispatch._missing_scored_completion_fields({
        "mode": "host-gated", "ref_namespace": "host", "receipt_oids": [],
        "dirty": False, "seal_complete": False, "receipts_authenticated": False,
        "infrastructure_failure": None,
    })[:2] == ("invalid-mode", "invalid-ref-namespace")


def test_setup_failure_still_runs_real_lifecycle_callbacks(tmp_path: Path, monkeypatch) -> None:
    cell = _cell()
    events: list[str] = []

    class Lifecycle:
        def __getattribute__(self, name):
            if name in {"stop_tools", "drain_rpc", "kill_planes", "close_acl", "final_status", "kill_git", "census_snapshot", "destroy"}:
                return lambda: events.append(name)
            return object.__getattribute__(self, name)

    monkeypatch.setattr(dispatch, "_dispatch", lambda *args, **kwargs: DispatchResult("ok", completion={}))
    cell_root = tmp_path / "cell-root"
    cell_root.mkdir()
    result = dispatch.run_task(
        _task(), "seat", "pi-sdk", "a" * 40, "oi-pi-bakeoff-r12b-test", tmp_path,
        schedule_cell=cell, fixture_root_oid="c" * 40, tool_gid=1234,
        cell_root=cell_root,
        scored_runtime_factory=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("missing")),
        scored_lifecycle=attempt_lifecycle(
            tmp_path,
            Lifecycle(),
            dispatch_result={"status": "ok", "completion": {}},
        ),
    )

    assert result.completion["infrastructure_failure"] == "incomplete-scored-completion"
    assert events == ["stop_tools", "drain_rpc", "kill_planes", "close_acl", "final_status", "kill_git", "census_snapshot", "destroy"]


def test_unpatched_production_factory_recovers_a_provisioned_cell(tmp_path: Path) -> None:
    repo = Path(__file__).parents[3]
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    task_ids = (
        "c1-permissive-boundary", "c1-token-bucket", "c2-parser", "c3-refactor",
        "c4-rail", "c5-artifact", "c6-scope", "c7-provenance",
    )
    manifest = {
        "run_id": "oi-pi-bakeoff-r12b-provision-20260714T000000Z",
        "source": {"realpath": str(repo)}, "base_sha": base, "seed": "00" * 32,
        "tasks": [{"task_id": task_id, "fixture_sha": base} for task_id in task_ids],
        "arms": [
            {"arm": "glm-pi", "engine": "pi-sdk", "agent_prefix": "pi-glm"},
            {"arm": "glm-zcode", "engine": "openinterpreter", "agent_prefix": "oi-glm"},
            {"arm": "kimi-pi", "engine": "pi-sdk", "agent_prefix": "pi-kimi"},
            {"arm": "kimi-cli", "engine": "openinterpreter", "agent_prefix": "oi-kimi"},
        ],
        "evidence": {"root": str(tmp_path / "evidence")},
    }
    with pytest.raises(ProductionRuntimeUnavailable, match="ARB_MEMORY_REDIS_URL"):
        build_production_controller(manifest)

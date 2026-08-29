from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from implbench.harness.scorer_sandbox import ScorerRole, ScorerSandbox, build_g1_topology, post_import_input
from implbench.harness.runtime import ProductionRuntimeUnavailable, _SystemPlaneProvisioner


def test_production_runtime_has_no_r15_placeholder_or_ambient_sleep() -> None:
    source = Path(__file__).parents[1] / "harness" / "runtime.py"
    text = source.read_text(encoding="utf-8")
    assert "_LocalPlaneProvisioner" not in text
    assert "_LocalACLBackend" not in text
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run":
            args = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            assert args != ["sleep", "3600"]


def test_production_plane_authority_is_a_serialized_required_boundary() -> None:
    with pytest.raises(ProductionRuntimeUnavailable, match="non-empty scored run ID"):
        _SystemPlaneProvisioner(helper=None)


def test_configured_budget_reaches_real_pipe_drainer(tmp_path: Path) -> None:
    materialization = tmp_path / "imported"
    materialization.mkdir()
    input_value = post_import_input(materialization, digest="c" * 64)
    topology = build_g1_topology(keyed_runner_uid=101, broker_uid=102, submitted_program_uid=103, battery_key="secret")
    seen: list[int] = []

    class Launcher:
        def run(self, argv, *, uid, cwd, env, timeout, max_output_bytes):
            del argv, uid, cwd, env, timeout
            seen.append(max_output_bytes)
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    sandbox = ScorerSandbox(tmp_path, input_value, topology, launcher=Launcher(), max_output_bytes=1234)
    sandbox.run(ScorerRole.SUBMITTED_PROGRAM, ["/bin/true"])
    assert seen == [1234]


def test_topology_starts_roles_concurrently(tmp_path: Path) -> None:
    active = 0
    peak = 0
    lock = threading.Lock()
    materialization = tmp_path / "imported"
    materialization.mkdir()
    topology = build_g1_topology(keyed_runner_uid=101, broker_uid=102, submitted_program_uid=103, battery_key="secret")
    input_value = post_import_input(materialization, digest="c" * 64)

    class Launcher:
        def run(self, argv, *, uid, cwd, env, timeout, max_output_bytes):
            nonlocal active, peak
            del argv, uid, cwd, env, timeout, max_output_bytes
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    sandbox = ScorerSandbox(tmp_path, input_value, topology, launcher=Launcher())
    sandbox.run_topology({role: ["/bin/true"] for role in (ScorerRole.KEYED_RUNNER, ScorerRole.BROKER, ScorerRole.SUBMITTED_PROGRAM)})
    assert peak == 3

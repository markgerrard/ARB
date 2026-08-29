import json
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = json.loads((ROOT / "tests/e2e/h2_guard_registry.json").read_text(encoding="utf-8"))


class _MutationBatch:
    def __init__(self, tmp_path: Path, guard_ids: list[str]):
        self._tmp_path = tmp_path
        self._guard_ids = guard_ids
        self._results: dict[str, dict] | None = None

    def result(self, guard_id: str) -> dict:
        if self._results is None:
            self._results = _run_mutation_batch_driver(self._guard_ids, self._tmp_path)
        return self._results[guard_id]


@pytest.fixture(scope="session")
def mutation_batch(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest):
    guard_ids = request.config._h2_mutation_guard_ids
    batch_tmp = tmp_path_factory.mktemp("h2-mutation-batch")
    batch = _MutationBatch(batch_tmp, guard_ids)
    yield batch


@pytest.mark.parametrize("guard_id", sorted(REGISTRY))
def test_guard_mutation_reds_and_restore_greens(guard_id, allow_e2e_subprocess, mutation_batch):
    result = mutation_batch.result(guard_id)
    baseline = result["baseline"]
    assert baseline["ok"] is True, baseline
    mutated = result["mutated"]
    assert mutated is not None, result
    assert mutated["ok"] is False, mutated
    assert mutated["failure"].startswith(f"guard {guard_id} failed open"), mutated
    assert result["restored"]["ok"] is True, result


def _run_mutation_batch_driver(guard_ids: list[str], tmp_path: Path) -> dict[str, dict]:
    cmd = [
        sys.executable,
        "-m",
        "tests.e2e._mutate",
        "--batch",
        "--tmp",
        str(tmp_path),
    ]
    for guard_id in guard_ids:
        cmd.extend(["--guard", guard_id])
    proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["guards"]

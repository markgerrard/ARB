from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = json.loads((ROOT / "tests/e2e/h2_guard_registry.json").read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guard", action="append", required=True)
    parser.add_argument("--tmp", required=True)
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--both", action="store_true")
    parser.add_argument("--mutate", action="store_true")
    args = parser.parse_args()
    guard_ids = args.guard

    worktree = Path(args.tmp) / "repo"
    if worktree.exists():
        shutil.rmtree(worktree)
    shutil.copytree(
        ROOT,
        worktree,
        ignore=_mutation_copy_ignore,
    )

    _use_worktree(worktree)
    _configure_tmp(Path(args.tmp))

    if args.batch or len(guard_ids) > 1:
        if args.both or args.mutate or (len(guard_ids) == 1 and not args.batch):
            parser.error("--batch cannot be combined with --both/--mutate")
        results = _run_guard_batch(guard_ids, worktree, Path(args.tmp))
        print(json.dumps({"guards": results}, sort_keys=True))
        return 0

    guard_id = guard_ids[0]
    if args.both:
        baseline = _run_guard_check(guard_id, worktree, Path(args.tmp))
        if not baseline["ok"]:
            print(json.dumps({"baseline": baseline, "mutated": None}, sort_keys=True))
            return 0
        _apply_mutation(worktree, guard_id)
        _use_worktree(worktree)
        mutated = _run_guard_check(guard_id, worktree, Path(args.tmp))
        print(json.dumps({"baseline": baseline, "mutated": mutated}, sort_keys=True))
        return 0

    if args.mutate:
        _apply_mutation(worktree, guard_id)
    print(json.dumps(_run_guard_check(guard_id, worktree, Path(args.tmp)), sort_keys=True))
    return 0


def _mutation_copy_ignore(path: str, names: list[str]) -> set[str]:
    relative = Path(path).resolve().relative_to(ROOT)
    if not relative.parts:
        return set(names) - {"skills", "tests"}
    if relative == Path("tests"):
        return set(names) - {"e2e"}
    return set(shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache")(path, names))


def _run_guard_check(guard_id: str, worktree: Path, tmp: Path) -> dict:
    try:
        _assert_guard_enforced(guard_id, worktree, tmp)
    except AssertionError as exc:
        return {"ok": False, "failure": str(exc), "worktree": str(worktree)}
    return {"ok": True, "failure": None, "worktree": str(worktree)}


def _configure_tmp(tmp: Path) -> None:
    os.environ["ARB_H2_SHADOW_LOG"] = str(tmp / "state" / "h2-shadow-log.jsonl")
    os.environ["XDG_STATE_HOME"] = str(tmp / "state")
    os.environ["HOME"] = str(tmp / "home")
    Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)


def _run_guard_batch(guard_ids: list[str], worktree: Path, tmp: Path) -> dict[str, dict]:
    results = {}
    for guard_id in guard_ids:
        guard_tmp = tmp / guard_id
        _configure_tmp(guard_tmp)
        target = worktree / REGISTRY[guard_id]["file"]
        original = target.read_bytes()
        baseline = _run_guard_check(guard_id, worktree, guard_tmp)
        if baseline["ok"]:
            _apply_mutation(worktree, guard_id)
            _use_worktree(worktree)
            mutated = _run_guard_check(guard_id, worktree, guard_tmp)
        else:
            mutated = None
        target.write_bytes(original)
        _use_worktree(worktree)
        restored = _run_guard_check(guard_id, worktree, guard_tmp)
        results[guard_id] = {"baseline": baseline, "mutated": mutated, "restored": restored}
    return results


def _apply_mutation(worktree: Path, guard_id: str) -> None:
    entry = REGISTRY[guard_id]
    path = worktree / entry["file"]
    source = path.read_text(encoding="utf-8")
    locator = entry["locator"]
    assert source.count(locator) == 1, guard_id
    if guard_id == "every_derived_has_row":
        source = source.replace(
            "        and all(\n"
            "            any(isinstance(row, Mapping) and row.get(\"candidate_id\") == candidate_id for row in rows)\n"
            "            for candidate_id in derived\n"
            "        )",
            "        and True",
        )
    elif entry["recipe"] == "delete-line":
        lines = [line for line in source.splitlines() if locator not in line]
        source = "\n".join(lines) + "\n"
    elif entry["recipe"] == "replace":
        source = source.replace(locator, entry["replacement"])
    else:
        raise AssertionError(f"unknown mutation recipe for {guard_id}")
    path.write_text(source, encoding="utf-8")


def _use_worktree(worktree: Path) -> None:
    sys.path.insert(0, str(worktree))
    # Evict skills.* AND tests.* so later imports load the MUTATED worktree copies, not the
    # already-imported ROOT modules. Without the tests.* eviction, pinning the e2e's OWN guards
    # (runner.py / spine.py) would silently import the unmutated module → a hollow self-pin (the
    # exact trap this slice warns about). Belt-and-suspenders: the two e2e-own asserts also load by
    # explicit path and assert module.__file__ is the worktree copy.
    for name in list(sys.modules):
        if name in {"skills", "tests"} or name.startswith(("skills.", "tests.")):
            del sys.modules[name]


def _load_worktree_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    # Register BEFORE exec_module: @dataclass (and other introspection) resolves cls.__module__ via
    # sys.modules[name], which is None for an unregistered spec-loaded module (Python 3.14).
    sys.modules[name] = module
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == path.resolve(), f"loaded {module.__file__}, not {path}"
    return module


def _assert_guard_enforced(guard_id: str, worktree: Path, tmp: Path) -> None:
    if guard_id.startswith(("min_", "fp_", "discrimination", "complete_only")):
        _assert_graduation_guard(guard_id)
        return
    if guard_id == "validity":
        _assert_validity_guard(worktree)
        return
    if guard_id == "spine_zero_case":
        _assert_spine_zero_case_guard(worktree)
        return
    if guard_id == "runner_canary":
        _assert_runner_canary_guard(worktree, tmp)
        return
    if guard_id == "runner_block_fail":
        _assert_runner_block_fail_guard(worktree, tmp)
        return
    out = _run_level_b(_case_for_guard(guard_id), tmp)
    if out["record_payload"]["complete"] is not False:
        raise AssertionError(f"guard {guard_id} failed open: complete={out['record_payload']['complete']}")


def _assert_spine_zero_case_guard(worktree: Path) -> None:
    # Deny-proof #2 guard: a zero-case run must be BLOCK_UNRUN (never a vacuous pass). The mutation
    # drops the `case_count == 0` clause; a zero-case run then falls through to PASS → failed open.
    spine = _load_worktree_module("mutated_spine", worktree / "tests/e2e/spine.py")
    result = spine.E2EResult.from_counts(case_count=0, passed=0, block_fail=0, block_unrun=0)
    if result.status is not spine.E2EStatus.BLOCK_UNRUN:
        raise AssertionError(f"guard spine_zero_case failed open: zero-case -> {result.status}")


def _assert_runner_canary_guard(worktree: Path, tmp: Path) -> None:
    # Deny-proof #1 guard: run_suite must invoke the boundary-honesty canary so a mocked boundary is
    # classified BLOCK_UNRUN (miscategorised). The mutation deletes the canary call; the mocked
    # collector then runs faithfully (its side_effect delegates to the real append) → PASS → failed
    # open. Mirrors test_denyproofs.test_mocked_boundary_maps_to_block_unrun.
    import unittest.mock as mock
    from types import SimpleNamespace

    runner = _load_worktree_module("mutated_runner", worktree / "tests/e2e/runner.py")
    h2_harness = runner.h2_harness
    real_load = h2_harness._load
    real_coll = real_load("h2_collector", "skills/bridge-protocol/gate/h2_collector.py")

    def fake_load(modname, relpath):
        if relpath.endswith("h2_collector.py"):
            return SimpleNamespace(
                append_record=mock.Mock(side_effect=real_coll.append_record),
                shadow_log_path=mock.Mock(side_effect=real_coll.shadow_log_path),
            )
        return real_load(modname, relpath)

    h2_harness._load = fake_load
    try:
        result = runner.run_suite(
            case_ids=["enumerated/redis-from-url"],
            tmp=tmp / "canary",
            status_path=tmp / "canary_status.json",
        )
    finally:
        h2_harness._load = real_load
    if result.status is not runner.E2EStatus.BLOCK_UNRUN:
        raise AssertionError(f"guard runner_canary failed open: mocked boundary -> {result.status}")


def _assert_runner_block_fail_guard(worktree: Path, tmp: Path) -> None:
    # Deny-proof #3 guard: a DETECTED surface breakage (assert_case_expected raises AssertionError)
    # must classify BLOCK_FAIL, not BLOCK_UNRUN. The mutation flips the except-AssertionError arm to
    # block_unrun; the tampered record then misroutes -> failed open. (Detection itself is redundantly
    # guarded by 3 sibling assertions — verified empirically — so the load-bearing, pinnable guard is
    # the CLASSIFICATION arm, not any single detection line.) Mirrors
    # test_denyproofs.test_expected_surface_breakage_maps_to_block_fail.
    # NB: this relies on the duplicate-id case being complete=False with dispositions, so forcing
    # complete=True yields a detectable mismatch (panel P2). If that case's expectations change,
    # re-confirm detection still fires here.
    runner = _load_worktree_module("mutated_runner_bf", worktree / "tests/e2e/runner.py")
    h2_harness = runner.h2_harness
    original_run_case = h2_harness.run_case

    def broken_run_case(case, case_tmp):
        out = original_run_case(case, case_tmp)
        out["record_payload"] = dict(out["record_payload"])
        out["record_payload"]["complete"] = True
        out["log_records"][-1] = out["record_payload"]
        return out

    h2_harness.run_case = broken_run_case
    try:
        result = runner.run_suite(
            case_ids=["discovered/duplicate-id"],
            tmp=tmp / "blockfail",
            status_path=tmp / "blockfail_status.json",
        )
    finally:
        h2_harness.run_case = original_run_case
    if result.status is not runner.E2EStatus.BLOCK_FAIL:
        raise AssertionError(f"guard runner_block_fail failed open: surface breakage -> {result.status}")


def _assert_validity_guard(worktree: Path) -> None:
    from skills.defect_hunts.h2_assumptions import is_complete

    section = _section(
        [
            {
                "candidate_id": "redis:pkg/a.py:redis.from_url#1",
                "disposition": "answered",
                "violating_run": "r",
                "evidence": "",
            }
        ]
    )
    if is_complete(["redis:pkg/a.py:redis.from_url#1"], section, repo_root=worktree) is not False:
        raise AssertionError("guard validity failed open: invalid section completed")


def _assert_graduation_guard(guard_id: str) -> None:
    from skills.defect_hunts.h2_graduation import is_graduation_ready

    records = {
        "min_runs": _records(9, answered=20, nlb=1),
        "min_disposed": _records(10, answered=18, nlb=1),
        "discrimination": _records(10, answered=20, nlb=0),
        "fp_threshold": _records(10, answered=18, nlb=2),
        "complete_only": [_record(True, ["answered", "answered"]) for _ in range(9)]
        + [_record(False, ["answered", "not_load_bearing"])],
    }[guard_id]
    if is_graduation_ready(records) is not False:
        raise AssertionError(f"guard {guard_id} failed open: graduation ready")


def _records(count: int, *, answered: int, nlb: int) -> list[dict]:
    dispositions = ["answered"] * answered + ["not_load_bearing"] * nlb
    records = [_record(True, []) for _ in range(count)]
    for index, disposition in enumerate(dispositions):
        records[index % count]["dispositions"].append({"disposition": disposition})
    return records


def _record(complete: bool, dispositions: list[str]) -> dict:
    return {
        "complete": complete,
        "dispositions": [{"disposition": disposition} for disposition in dispositions],
    }


def _run_level_b(case: dict, tmp: Path) -> dict:
    harness_path = Path(sys.path[0]) / "tests/e2e/h2_harness.py"
    spec = importlib.util.spec_from_file_location("mutated_h2_harness", harness_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module.run_case(case, tmp)


def _case_for_guard(guard_id: str) -> dict:
    if guard_id == "coverage_ack":
        return _redis_case(_section([_row()], acknowledged=False))
    if guard_id == "derived_nonempty":
        return {
            "files": {"pkg/noop.py": "VALUE = 1\n"},
            "diff": "diff --git a/pkg/noop.py b/pkg/noop.py\n--- a/pkg/noop.py\n+++ b/pkg/noop.py\n@@ -0,0 +1 @@\n+VALUE = 1\n",
            "changed_paths": ["pkg/noop.py"],
            "phase_input": {"h2_section": _section([])},
        }
    if guard_id == "rows_subset":
        rows = [_row(), {**_row(), "candidate_id": "redis:pkg/a.py:redis.Redis#1"}]
        return _redis_case(_section(rows))
    if guard_id == "uniqueness":
        return _redis_case(_section([_row(), _row()]))
    if guard_id == "every_derived_has_row":
        row = {
            "candidate_id": "redis:pkg/a.py:redis.from_url#1",
            "disposition": "answered",
            "violating_run": "r",
            "evidence": "pkg/a.py",
        }
        return {
            "files": {
                "pkg/a.py": "import redis\none = redis.from_url('redis://one')\ntwo = redis.Redis(host='localhost')\n"
            },
            "diff": "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -0,0 +1,3 @@\n+import redis\n+one = redis.from_url('redis://one')\n+two = redis.Redis(host='localhost')\n",
            "changed_paths": ["pkg/a.py"],
            "phase_input": {"h2_section": _section([row])},
        }
    raise AssertionError(f"no case for {guard_id}")


def _redis_case(section: dict) -> dict:
    return {
        "files": {"pkg/a.py": "import redis\nclient = redis.from_url('redis://localhost:6379/0')\n"},
        "diff": "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -0,0 +1,2 @@\n+import redis\n+client = redis.from_url('redis://localhost:6379/0')\n",
        "changed_paths": ["pkg/a.py"],
        "phase_input": {"h2_section": section},
    }


def _section(rows: list[dict], *, acknowledged: bool = True) -> dict:
    return {"coverage_acknowledgment": {"acknowledged": acknowledged, "additional_assumptions": []}, "rows": rows}


def _row() -> dict:
    return {
        "candidate_id": "redis:pkg/a.py:redis.from_url#1",
        "disposition": "answered",
        "violating_run": "r",
        "evidence": "pkg/a.py",
    }


if __name__ == "__main__":
    raise SystemExit(main())

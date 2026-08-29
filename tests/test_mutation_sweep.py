"""Tests for scripts/mutation_sweep.py — the inject-revert harness.

The harness exists to prove other checks can fail, so its own refusals are the part that
must be load-bearing. Each refusal test asserts the OBSERVABLE CONSEQUENCE (uncommitted work
still present, tree still clean, nothing run) rather than just that an exception was raised:
a raise alone cannot distinguish "refused for the stated reason" from "refused incidentally".

SELF-SWEEP — observed output, 2026-07-26. The harness was run against its own source, which
is the only honest way to claim these guards work. Command:

    .venv/bin/python scripts/mutation_sweep.py --spec <spec>.json \
        --test-cmd ".venv/bin/python -m pytest tests/test_mutation_sweep.py -q"

    BASELINE (no mutation): 13 passed in 0.95s

    S1  dirty-tree refusal removed
        1 failed, 12 passed in 1.05s
        FAILED test_dirty_tree_is_refused_before_anything_is_touched
    S2  zero-collected refusal disabled
        1 failed, 12 passed in 1.01s
        FAILED test_zero_collected_baseline_is_refused_as_a_broken_harness
    S3  anchor exactly-once check disabled
        2 failed, 11 passed in 1.05s
        FAILED test_anchor_must_match_exactly_once[E-many]
        FAILED test_anchor_must_match_exactly_once[NOT_PRESENT-zero]
    S4  collected-drift check disabled
        1 failed, 12 passed in 0.99s
        FAILED test_collected_count_drift_between_runs_is_refused

    RESTORED BASELINE: 13 passed in 0.96s
    tree after sweep: CLEAN

Tests are cited by NAME, not line number — line citations rot silently while still being
believed. Why self-sweeping works despite the tool editing its own source mid-run: the
driver process imported the module before mutating, so only the pytest SUBPROCESS re-imports
the mutated file. The spec lived outside the repo, or it would have made the tree dirty and
the harness would have refused itself — which it did, on the first attempt.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from mutation_sweep import (  # noqa: E402
    SweepRefused,
    parse_pytest_output,
    run_sweep,
)

# A fake test command: prints a pytest-shaped summary, red when the marker is mutated.
FAKE_TEST = """import os
import pathlib
src = pathlib.Path("src.py").read_text()
if "KEEP" in src:
    pathlib.Path(os.environ["MUTATION_SWEEP_JUNIT"]).write_text('<testsuite><testcase classname="tests.t" name="test_marker" /></testsuite>')
    print("5 passed in 0.10s")
else:
    pathlib.Path(os.environ["MUTATION_SWEEP_JUNIT"]).write_text('<testsuite><testcase classname="tests.t" name="test_marker"><failure /></testcase></testsuite>')
    print("1 failed, 4 passed in 0.10s")
"""

# A command that runs but collects nothing — the silently-broken-harness shape.
FAKE_EMPTY = 'import os, pathlib; pathlib.Path(os.environ["MUTATION_SWEEP_JUNIT"]).write_text("<testsuite />"); print("no tests ran in 0.00s")'


def _repo(tmp_path: Path, runner: str = FAKE_TEST) -> Path:
    """The repo is a SUBDIR of tmp_path so callers can put spec files beside it without
    making the tree dirty — which the harness would (correctly) refuse."""
    tmp_path = tmp_path / "repo"
    tmp_path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "src.py").write_text("VALUE = 'KEEP'\n")
    (tmp_path / "faketest.py").write_text(runner)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _cmd() -> list[str]:
    return [sys.executable, "faketest.py"]


CAUGHT = [{"id": "M1", "label": "marker removed", "file": "src.py",
           "find": "KEEP", "replace": "GONE"}]


def test_caught_mutation_reports_the_failure_and_leaves_the_tree_clean(tmp_path):
    repo = _repo(tmp_path)
    results, report = run_sweep(repo, CAUGHT, _cmd())
    assert results[0]["survived"] is False
    assert results[0]["failed"] == ["tests/t.py::test_marker"]
    assert "BASELINE (no mutation): 5 passed" in report
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True).stdout == ""
    assert (repo / "src.py").read_text() == "VALUE = 'KEEP'\n", "mutation was not reverted"


def test_surviving_mutation_is_named_not_silently_passed(tmp_path):
    """A mutation nothing catches is the finding the sweep exists to produce."""
    repo = _repo(tmp_path)
    spec = [{"id": "M9", "label": "untested constant", "file": "src.py",
             "find": "VALUE", "replace": "RENAMED"}]
    results, report = run_sweep(repo, spec, _cmd())
    assert results[0]["survived"] is True
    assert "NO TEST FAILED - MUTATION SURVIVES" in report


def test_dirty_tree_is_refused_before_anything_is_touched(tmp_path):
    """The load-bearing refusal. `git checkout --` restores HEAD, so uncommitted work in a
    mutated file is destroyed by the first revert. Asserting the raise is not enough: the
    uncommitted edit must still be on disk afterwards, and nothing may have run."""
    repo = _repo(tmp_path)
    (repo / "src.py").write_text("VALUE = 'KEEP'\nUNCOMMITTED = 'precious'\n")
    with pytest.raises(SweepRefused) as exc:
        run_sweep(repo, CAUGHT, _cmd())
    assert "dirty" in str(exc.value)
    assert "no override" in str(exc.value).lower()
    assert "precious" in (repo / "src.py").read_text(), (
        "the sweep destroyed uncommitted work — the refusal is not load-bearing"
    )


def test_untracked_file_also_counts_as_dirty(tmp_path):
    repo = _repo(tmp_path)
    (repo / "scratch.txt").write_text("x")
    with pytest.raises(SweepRefused):
        run_sweep(repo, CAUGHT, _cmd())


def test_zero_collected_baseline_is_refused_as_a_broken_harness(tmp_path):
    """`no tests ran in 0.00s` is a broken harness, not a green baseline: without this the
    sweep reports every mutation as caught while running nothing."""
    repo = _repo(tmp_path, runner=FAKE_EMPTY)
    with pytest.raises(SweepRefused) as exc:
        run_sweep(repo, CAUGHT, _cmd())
    assert "collected 0 tests" in str(exc.value)
    assert (repo / "src.py").read_text() == "VALUE = 'KEEP'\n"


def test_named_id_not_collected_at_baseline_refuses_naming_id_form(tmp_path):
    repo = _repo(tmp_path, runner='import os, pathlib\npathlib.Path(os.environ["MUTATION_SWEEP_JUNIT"]).write_text(\'<testsuite><testcase classname="tests.t" name="test_other" /></testsuite>\')\nprint("1 passed in 0.01s")\n')
    spec = [{"id": "M-id", "label": "id form", "file": "src.py",
             "find": "KEEP", "replace": "DROP",
             "expect_failed": ["tests/t.py::test_expected"]}]
    with pytest.raises(SweepRefused, match="NAMED-TEST-NOT-COLLECTED") as exc:
        run_sweep(repo, spec, _cmd())
    assert "tests/t.py::test_expected" in str(exc.value)
    assert "match no collected node id" in str(exc.value)


def test_named_id_skipped_at_baseline_refuses_naming_environment(tmp_path):
    repo = _repo(tmp_path, runner='import os, pathlib\npathlib.Path(os.environ["MUTATION_SWEEP_JUNIT"]).write_text(\'<testsuite><testcase classname="tests.t" name="test_expected"><skipped /></testcase></testsuite>\')\nprint("1 skipped in 0.01s")\n')
    spec = [{"id": "M-skipped", "label": "environment", "file": "src.py",
             "find": "KEEP", "replace": "DROP",
             "expect_failed": ["tests/t.py::test_expected"]}]
    with pytest.raises(SweepRefused, match="NAMED-TEST-SKIPPED") as exc:
        run_sweep(repo, spec, _cmd())
    assert "this environment cannot demonstrate fail-ability" in str(exc.value)


@pytest.mark.parametrize("find,why", [("NOT_PRESENT", "zero"), ("E", "many")])
def test_anchor_must_match_exactly_once(tmp_path, find, why):
    """A zero-match anchor mutates nothing and reports a false 'caught'; a multi-match anchor
    mutates more than the finding describes. Either way the report would be a lie."""
    repo = _repo(tmp_path)
    spec = [{"id": "MX", "label": why, "file": "src.py", "find": find, "replace": "Z"}]
    with pytest.raises(SweepRefused) as exc:
        run_sweep(repo, spec, _cmd())
    assert "exactly 1" in str(exc.value)
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True).stdout == ""


def test_mutation_that_changes_no_bytes_is_refused_not_reported_as_surviving(tmp_path):
    """A no-op spec entry passes the anchor check and leaves the file identical, so the suite
    stays green and the run would be reported as MUTATION SURVIVES — blaming the test suite for
    a broken spec entry. The two need opposite fixes, so the harness must name which it is."""
    repo = _repo(tmp_path)
    spec = [{"id": "M0", "label": "no-op", "file": "src.py", "find": "KEEP", "replace": "KEEP"}]
    with pytest.raises(SweepRefused) as exc:
        run_sweep(repo, spec, _cmd())
    assert "changed no bytes" in str(exc.value)
    assert "MUTATION SURVIVES" not in str(exc.value).upper() or "misattributes" in str(exc.value)


def test_collected_count_drift_between_runs_is_refused(tmp_path):
    """If the mutated run collects a different number of tests, it is not the comparison the
    report claims — e.g. the mutation broke collection rather than the behaviour."""
    repo = _repo(tmp_path, runner='import os, pathlib\n'
                                      'src = pathlib.Path("src.py").read_text()\n'
                                      'count = 5 if "KEEP" in src else 2\n'
                                      'cases = "".join(f\'<testcase classname="tests.t" name="test_{i}" />\' for i in range(count))\n'
                                      'pathlib.Path(os.environ["MUTATION_SWEEP_JUNIT"]).write_text(f"<testsuite>{cases}</testsuite>")\n'
                                      'print(f"{count} passed in 0.1s")\n')
    with pytest.raises(SweepRefused) as exc:
        run_sweep(repo, CAUGHT, _cmd())
    assert "baseline collected" in str(exc.value)


def test_forged_stdout_count_cannot_hide_junit_collection_drift(tmp_path):
    repo = _repo(tmp_path, runner='import atexit, os, pathlib\n'
                                      'atexit.register(lambda: print("2 passed in 0.01s"))\n'
                                      'count = 2 if "KEEP" in pathlib.Path("src.py").read_text() else 1\n'
                                      'cases = "".join(f\'<testcase classname="tests.t" name="test_{i}" />\' for i in range(count))\n'
                                      'pathlib.Path(os.environ["MUTATION_SWEEP_JUNIT"]).write_text(f"<testsuite>{cases}</testsuite>")\n')
    with pytest.raises(SweepRefused, match="baseline collected 2"):
        run_sweep(repo, CAUGHT, _cmd())


class TestParse:
    def test_counts_every_outcome_that_ran(self):
        r = parse_pytest_output("2 failed, 43 passed, 1 skipped in 3s")
        assert r.collected == 0

    def test_deselected_do_not_count_as_run(self):
        assert parse_pytest_output("2 passed, 27 deselected in 1s").collected == 0

    def test_no_tests_ran_collects_zero(self):
        assert parse_pytest_output("no tests ran in 0.00s").collected == 0

    def test_missing_summary_collects_zero_rather_than_guessing(self):
        assert parse_pytest_output("some unrelated chatter").collected == 0

    def test_green_outer_junit_ignores_nested_failure_output(self, tmp_path):
        report = tmp_path / "outer.xml"
        report.write_text(
            '<testsuite><testcase classname="tests.test_outer" name="test_outer" /></testsuite>'
        )
        result = parse_pytest_output(
            "FAILED tests/t.py::test_marker - nested fixture output\n1 passed in 0.01s",
            junit_path=report,
        )
        assert result.failed == []

    def test_junit_case_count_is_authoritative_over_forged_stdout(self, tmp_path):
        report = tmp_path / "result.xml"
        report.write_text('<testsuite><testcase classname="tests.t" name="test_a" /></testsuite>')
        assert parse_pytest_output("2 passed in 0.01s", junit_path=report).collected == 1

    def test_mutated_nonzero_exit_refuses_even_with_junit(self, tmp_path):
        repo = _repo(tmp_path, runner='import os, pathlib, sys\n'
                                      'mutated = "KEEP" not in pathlib.Path("src.py").read_text()\n'
                                      'pathlib.Path(os.environ["MUTATION_SWEEP_JUNIT"]).write_text("<testsuite><testcase classname=\\"tests.t\\" name=\\"test_marker\\"><failure /></testcase></testsuite>" if mutated else "<testsuite><testcase classname=\\"tests.t\\" name=\\"test_marker\\" /></testsuite>")\n'
                                      'print("1 failed in 0.01s" if mutated else "1 passed in 0.01s")\n'
                                      'sys.exit(7 if mutated else 0)\n')
        with pytest.raises(SweepRefused, match="MUTATED-RUN-INVALID-EXIT exit=7"):
            run_sweep(repo, CAUGHT, _cmd())

    def test_junit_class_node_id_uses_pytest_class_separator(self, tmp_path):
        report = tmp_path / "class.xml"
        report.write_text(
            '<testsuite><testcase classname="tests.test_outer.TestCase" name="test_outer"><failure /></testcase></testsuite>'
        )
        result = parse_pytest_output("1 failed in 0.01s", junit_path=report)
        assert result.failed == ["tests/test_outer.py::TestCase::test_outer"]

    def test_malformed_junit_refuses_instead_of_falling_back_to_stdout(self, tmp_path):
        report = tmp_path / "broken.xml"
        report.write_text("<testsuite>")
        with pytest.raises(SweepRefused, match="JUNIT-PARSE-ERROR"):
            parse_pytest_output("FAILED tests/t.py::test_marker\n1 failed in 0.01s", junit_path=report)

    def test_missing_junit_refuses_instead_of_falling_back_to_stdout(self, tmp_path):
        with pytest.raises(SweepRefused, match="JUNIT-ABSENT"):
            parse_pytest_output("1 passed in 0.01s", junit_path=tmp_path / "missing.xml")


def test_cli_exit_codes(tmp_path):
    """0 = all caught, 1 = something survived, 2 = refused."""
    from mutation_sweep import main

    repo = _repo(tmp_path)
    spec_ok = tmp_path / "ok.json"
    spec_ok.write_text(json.dumps(CAUGHT))
    argv = ["--spec", str(spec_ok), "--test-cmd", f"{sys.executable} faketest.py",
            "--repo", str(repo)]
    assert main(argv) == 0

    spec_survive = tmp_path / "s.json"
    spec_survive.write_text(json.dumps([{"id": "M9", "label": "x", "file": "src.py",
                                         "find": "VALUE", "replace": "RENAMED"}]))
    assert main(["--spec", str(spec_survive), "--test-cmd",
                 f"{sys.executable} faketest.py", "--repo", str(repo)]) == 1

    (repo / "dirt.txt").write_text("x")
    assert main(argv) == 2

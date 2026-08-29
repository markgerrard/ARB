from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SWEEP = ROOT / "scripts" / "mutation_sweep.py"


def _fixture(tmp_path, body="def test_a():\n    assert True\n\ndef test_b():\n    assert True\n"):
    (tmp_path / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n")
    (tmp_path / "app.py").write_text("VALUE = True\n")
    (tmp_path / "test_app.py").write_text(body)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "init"], cwd=tmp_path, check=True)


def test_missing_named_failure_is_not_a_kill(tmp_path):
    _fixture(tmp_path, "def test_a():\n    assert True\n")
    spec = [{"id": "M1", "label": "noop", "file": "app.py", "find": "True", "replace": "False", "expect_failed": ["test_app.py::test_a"]}]
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    results, _ = mutation_sweep.run_sweep(tmp_path, spec, [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"])
    assert results[0]["wrong_mechanism"] is True
    assert results[0]["missing_expected"] == ["test_app.py::test_a"]


def test_correct_binding_is_kill(tmp_path):
    _fixture(tmp_path, "def test_a():\n    assert VALUE\n")
    (tmp_path / "test_app.py").write_text("from app import VALUE\n\ndef test_a():\n    assert VALUE\n")
    subprocess.run(["git", "add", "test_app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "test"], cwd=tmp_path, check=True)
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    spec = [{"id": "M1", "label": "break value", "file": "app.py", "find": "True", "replace": "False", "expect_failed": ["test_app.py::test_a"]}]
    results, _ = mutation_sweep.run_sweep(tmp_path, spec, [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"])
    assert results[0]["wrong_mechanism"] is False
    assert results[0]["survived"] is False


def test_real_mutated_run_uses_junit_not_fake_failed_stdout(tmp_path):
    _fixture(tmp_path, "def test_a():\n    print('FAILED test_app.py::test_a')\n    assert True\n")
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    spec = [{"id": "M", "file": "app.py", "find": "True", "replace": "False"}]
    results, _ = mutation_sweep.run_sweep(
        tmp_path, spec,
        [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"],
    )
    assert results[0]["survived"] is True


def test_real_mutated_run_uses_junit_not_fake_error_stdout(tmp_path):
    """Candidate stdout must not fabricate a kill through pytest's ERROR-looking text."""
    _fixture(tmp_path, "def test_a():\n    print('ERROR test_app.py::test_a - forged')\n    assert True\n")
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    spec = [{"id": "M", "file": "app.py", "find": "True", "replace": "False"}]
    results, _ = mutation_sweep.run_sweep(
        tmp_path, spec,
        [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"],
    )
    assert results[0]["survived"] is True


def test_all_skipped_baseline_refuses(tmp_path):
    _fixture(tmp_path, "import pytest\n@pytest.mark.skip\ndef test_a(): pass\n")
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    with pytest.raises(mutation_sweep.SweepRefused, match="baseline passed 0 tests") as refused:
        mutation_sweep.run_sweep(tmp_path, [{"id":"M", "label":"x", "file":"app.py", "find":"True", "replace":"False"}], [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"])
    assert "all skipped or empty" in str(refused.value)


def test_pre_red_baseline_refuses_with_specific_reason(tmp_path):
    _fixture(tmp_path, "def test_a():\n    assert False\n")
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    with pytest.raises(mutation_sweep.SweepRefused, match="BASELINE-NOT-GREEN") as refused:
        mutation_sweep.run_sweep(tmp_path, [{"id": "M", "file": "app.py", "find": "True", "replace": "False"}], [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"])
    assert "BASELINE-NOT-GREEN" in str(refused.value)


def test_restore_hash_mismatch_refuses(tmp_path, monkeypatch):
    _fixture(tmp_path)
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    original = (tmp_path / "app.py").read_text()
    real_git = mutation_sweep.git
    def fake_git(repo, *args):
        if args[:2] == ("checkout", "--"):
            return ""
        return real_git(repo, *args)
    monkeypatch.setattr(mutation_sweep, "git", fake_git)
    with pytest.raises(mutation_sweep.SweepRefused, match="RESTORE FAILED"):
        mutation_sweep.run_sweep(tmp_path, [{"id": "M", "file": "app.py", "find": "True", "replace": "False"}],
                                 [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"])
    assert (tmp_path / "app.py").read_text() == "VALUE = False\n"
    (tmp_path / "app.py").write_text(original)


def test_green_parse_nonzero_exit_baseline_refuses(tmp_path, monkeypatch):
    _fixture(tmp_path)
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    real_run = mutation_sweep.subprocess.run

    def fake_run(command, *args, **kwargs):
        if "pytest" in command:
            junit = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml="))
            Path(junit).write_text('<testsuite><testcase classname="test_app" name="test_a" /></testsuite>')
            return subprocess.CompletedProcess(command, 3, "1 passed\n", "")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(mutation_sweep.subprocess, "run", fake_run)
    with pytest.raises(mutation_sweep.SweepRefused, match="BASELINE-NOT-GREEN exit=3"):
        mutation_sweep.run_sweep(tmp_path, [{"id": "M", "file": "app.py", "find": "True", "replace": "False"}],
                                 [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"])


def test_named_id_skipped_at_baseline_refuses_naming_environment(tmp_path):
    _fixture(tmp_path, "import pytest\n@pytest.fixture\ndef blocked(): pytest.skip('needs env')\ndef test_a(blocked): pass\ndef test_b(): pass\n")
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    with pytest.raises(mutation_sweep.SweepRefused, match="NAMED-TEST-SKIPPED") as refused:
        mutation_sweep.run_sweep(tmp_path, [{"id": "M", "file": "app.py", "find": "True", "replace": "False",
                                             "expect_failed": ["test_app.py::test_a"]}],
                                 [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"])
    assert "this environment cannot demonstrate fail-ability" in str(refused.value)


def test_named_id_not_collected_at_baseline_refuses_naming_id_form(tmp_path):
    _fixture(tmp_path, "import pytest\n@pytest.mark.parametrize('value', [1])\ndef test_a(value): pass\n")
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    with pytest.raises(mutation_sweep.SweepRefused, match="NAMED-TEST-NOT-COLLECTED"):
        mutation_sweep.run_sweep(tmp_path, [{"id": "M", "file": "app.py", "find": "True", "replace": "False",
                                             "expect_failed": ["test_app.py::test_a"]}],
                                 [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"])


def test_masked_import_resolution_refuses(tmp_path, monkeypatch):
    _fixture(tmp_path)
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("VALUE=True\n")
    (tmp_path / "decoy").mkdir()
    (tmp_path / "decoy" / "pkg.py").write_text("VALUE=False\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "src"], cwd=tmp_path, check=True)
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    real_run = mutation_sweep.subprocess.run
    def fake_run(command, *args, **kwargs):
        if "-c" in command:
            return subprocess.CompletedProcess(command, 0, str(tmp_path / "decoy" / "pkg.py") + "\n", "")
        return real_run(command, *args, **kwargs)
    monkeypatch.setattr(mutation_sweep.subprocess, "run", fake_run)
    with pytest.raises(mutation_sweep.SweepRefused, match="IMPORT-RESOLUTION-MISMATCH"):
        mutation_sweep._check_import_resolution(tmp_path, [{"file": "src/pkg/mod.py"}])


def test_sigterm_mid_sweep_restores_tree(tmp_path):
    _fixture(tmp_path, "from app import VALUE\nimport time\n\ndef test_a():\n    time.sleep(1)\n    assert VALUE\n")
    spec = tmp_path / "mutations.json"
    spec.write_text(json.dumps([{
        "id": "M", "file": "app.py", "find": "True", "replace": "False",
        "expect_failed": ["test_app.py::test_a"],
    }]))
    subprocess.run(["git", "add", "mutations.json"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "spec"], cwd=tmp_path, check=True)
    process = subprocess.Popen([
        sys.executable, str(SWEEP), "--repo", str(tmp_path), "--spec", str(spec),
        "--test-cmd", f"{sys.executable} -m pytest test_app.py -q -rA --no-header",
    ], cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 10
    while (tmp_path / "app.py").read_text() == "VALUE = True\n" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "app.py").read_text() == "VALUE = False\n"
    os.kill(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 2, (stdout, stderr)
    assert "mutation-sweep INTERRUPTED: received signal 15" in stderr
    assert (tmp_path / "app.py").read_text() == "VALUE = True\n"
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=tmp_path, text=True) == ""


def test_old_format_spec_is_flagged_binding_none(tmp_path):
    _fixture(tmp_path)
    sys.path.insert(0, str(ROOT / "scripts"))
    import mutation_sweep
    results, _ = mutation_sweep.run_sweep(tmp_path, [{"id": "M", "file": "app.py", "find": "True", "replace": "False"}],
                                          [sys.executable, "-m", "pytest", "test_app.py", "-q", "-rA", "--no-header"])
    assert results[0]["binding"] == "none"

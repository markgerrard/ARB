import ast
import hashlib
import json
import os
import signal
import shutil
import subprocess
from pathlib import Path

import importlib.util
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("changed_gate", ROOT / "scripts/changed_test_mutation_gate.py")
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)


def test_pattern_set_matches_ci_suite_trees():
    trees = set(gate.SCOPED_TEST_TREES)
    assert "tests" in trees
    assert "tools/authorbench/tests" in trees


def test_closure_refuses_paired_file(tmp_path):
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests" / "test_x.py"
    test.write_text("import pathlib\n")
    closure = gate.pair_closure(test, tmp_path)
    assert "tests/test_x.py" in closure.refused_surface


def test_closure_refuses_relative_imported_scoped_module(tmp_path):
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "tests" / "pkg" / "support.py").write_text("VALUE = True\n")
    test = tmp_path / "tests" / "pkg" / "test_x.py"
    test.write_text("from . import support\n\ndef test_x(): assert support.VALUE\n")
    closure = gate.pair_closure(test, tmp_path)
    assert "tests/pkg/support.py" in closure.refused_surface


def test_closure_recurses_through_a_scoped_helper_to_another_scoped_helper(tmp_path):
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "tests" / "pkg" / "helper.py").write_text("from . import support\n")
    (tmp_path / "tests" / "pkg" / "support.py").write_text("VALUE = True\n")
    test = tmp_path / "tests" / "pkg" / "test_x.py"
    test.write_text("from . import helper\n\ndef test_x(): assert helper\n")
    assert "tests/pkg/support.py" in gate.pair_closure(test, tmp_path).refused_surface


def test_invalid_kind_is_named(tmp_path):
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests" / "test_x.py"
    test.write_text("def test_x(): pass\n")
    test.with_name("test_x.mutations.json").write_text(json.dumps({"schema":1,"mutations":[{"kind":"bad","file":"src/x.py","expect_failed":["tests/test_x.py::test_x"]}]}))
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(), {})
    assert isinstance(result, gate.GateFail)
    assert "kind must be defect or reinstate" in result.reason


def test_probe_marker_shape_is_named(tmp_path):
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests" / "test_x.py"
    test.write_text("PROBE_PROVENANCE = {'claim_id': ''}\ndef test_x(): pass\n")
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(probe={"claim_id":""}), {})
    assert isinstance(result, gate.GateFail)
    assert "marker shape invalid" in result.reason


def test_missing_sidecar_is_decl_missing(tmp_path):
    test = tmp_path / "test_x.py"
    test.write_text("def test_x(): pass\n")
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(), {})
    assert isinstance(result, gate.GateFail)
    assert result.reason.startswith("DECL-MISSING")


def test_candidate_authored_exemption_is_decl_invalid(tmp_path):
    test = tmp_path / "test_x.py"
    test.write_text("def test_x(): pass\n")
    test.with_name("test_x.mutations.json").write_text(json.dumps({"schema": 1, "no_legal_target": True}))
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(), {})
    assert isinstance(result, gate.GateFail)
    assert result.reason.startswith("DECL-INVALID")
    assert "candidate-authored" in result.reason


def test_tree_residue_codes_are_distinct(tmp_path):
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("x")
    untracked = tmp_path / "new.txt"
    untracked.write_text("x")
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    tracked.write_text("changed")
    assert gate._tree_status(tmp_path) == (["tracked.txt"], ["new.txt"])
    assert "DIRTY-AFTER-SWEEP" in "FAIL[tree]: DIRTY-AFTER-SWEEP"
    assert "UNTRACKED-RESIDUE" in "FAIL[tree]: UNTRACKED-RESIDUE"


def _named_test(tmp_path, name="test_x"):
    test = tmp_path / "tests" / "test_x.py"
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text(f"def {name}():\n    assert True\n")
    return test


def _fixture_repo(tmp_path, files):
    """Create a committed, clean fixture repository for gate scope tests."""
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (tmp_path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (tmp_path / "scripts").mkdir(exist_ok=True)
    for name in ("changed_test_mutation_gate.py", "mutation_sweep.py"):
        shutil.copy2(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()


def _commit(repo, message="change"):
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", message], cwd=repo, check=True)


def test_ast_exempt_pair_emits_visible_pass(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    (tmp_path / "tests/test_x.py").write_text("# comment\ndef test_x(): pass\n")
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 0
    assert "PASS[pair:tests/test_x.py]: EXEMPT-AST" in capsys.readouterr().out


def test_authorbench_production_target_is_legal_while_its_paired_surface_is_refused(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = True\n")
    test = _named_test(tmp_path)
    test.write_text("from app import VALUE\n\ndef test_x(): assert VALUE\n")
    closure = gate.pair_closure(test, tmp_path)
    assert "tests/test_x.py" in closure.refused_surface
    assert closure.legal_candidates == {"app.py"}
    gate._sidecar(test).write_text(json.dumps({"schema": 1, "mutations": [{
        "kind": "defect", "file": "app.py", "expect_failed": ["tests/test_x.py::test_x"],
    }]}))
    result = gate.load_declaration(test, tmp_path, closure, {
        "tests/test_x.py": {"reason": "harness-self-test", "subject": "fixture"},
    })
    assert isinstance(result, gate.GateFail)
    assert "legal candidates exist: app.py" in result.reason


def test_base_resolving_to_head_is_named_scope_fail(tmp_path):
    _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    _, failure = gate.changed_pairs(tmp_path, "HEAD")
    assert failure.reason == "FAIL[scope]: base resolves to HEAD — wrong invocation or nothing to gate"


def test_behavioural_edit_is_not_ast_exempt(tmp_path):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    (tmp_path / "tests/test_x.py").write_text("def test_x(): assert True\n")
    _commit(tmp_path)
    assert not gate._ast_equal_to_base(tmp_path, "tests/test_x.py", base)


def test_changed_test_without_declaration_fails_by_name(tmp_path):
    result = gate.load_declaration(_named_test(tmp_path), tmp_path, gate.PairClosure(), {})
    assert isinstance(result, gate.GateFail) and result.reason.startswith("DECL-MISSING")


def test_deleted_test_file_is_exempt_and_visible(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    (tmp_path / "tests/test_x.py").unlink()
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 0
    assert "PASS[pair:tests/test_x.py]: EXEMPT-DELETED" in capsys.readouterr().out


def test_deleting_sidecar_alone_enters_validation_and_sweep(tmp_path):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n", "tests/test_x.mutations.json": '{"schema":1,"mutations":[]}'})
    (tmp_path / "tests/test_x.mutations.json").unlink()
    _commit(tmp_path)
    _, _, _, pairs = gate.changed_pairs(tmp_path, base)[0]
    assert pairs == ["tests/test_x.py"]
    result = gate.load_declaration(tmp_path / "tests/test_x.py", tmp_path, gate.pair_closure(tmp_path / "tests/test_x.py", tmp_path), {})
    assert result.reason == "DECL-MISSING — no mutation sidecar"


def test_empty_or_foreign_expect_failed_is_decl_invalid(tmp_path):
    test = _named_test(tmp_path)
    sidecar = gate._sidecar(test)
    sidecar.write_text(json.dumps({"schema": 1, "mutations": [{"kind": "defect", "file": "app.py", "expect_failed": []}]}))
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(), {})
    assert isinstance(result, gate.GateFail) and "expect_failed" in result.reason


def test_exit_code_contract(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    (tmp_path / "tests/test_x.py").write_text("# comment\ndef test_x(): pass\n")
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 0
    capsys.readouterr()
    (tmp_path / "tests/test_x.py").write_text("def test_x(): assert True\n")
    _commit(tmp_path, "behavioural change")
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert "FAIL[pair:tests/test_x.py]: DECL-MISSING" in capsys.readouterr().out
    assert gate.main(["--repo", str(tmp_path / "tests"), "--base", base]) == 2


def test_failed_restore_yields_distinct_tree_fail_and_aborts_remaining_pairs(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tracked.txt": "clean\n",
        "tests/test_x.py": "from pathlib import Path\n\ndef test_x():\n    Path('tracked.txt').write_text('changed\\n')\n",
        "tests/test_y.py": "def test_y(): pass\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "break-value", "kind": "defect", "file": "app.py", "find": "True", "replace": "False",
            "expect_failed": ["tests/test_x.py::test_x"],
        }]}),
        "tests/test_y.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "break-value", "kind": "defect", "file": "app.py", "find": "True", "replace": "False",
            "expect_failed": ["tests/test_y.py::test_y"],
        }]}),
    })
    for name in ("x", "y"):
        test = tmp_path / f"tests/test_{name}.py"
        test.write_text("MARKER = 1\n" + test.read_text())
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    output = capsys.readouterr().out
    assert "FAIL[tree]: DIRTY-AFTER-SWEEP — ['tracked.txt']" in output
    assert "FAIL[pair:tests/test_y.py]" not in output
    subprocess.run(["git", "checkout", "--", "tracked.txt"], cwd=tmp_path, check=True)


def test_gate_sees_a_changed_test_file_in_fixture_repo(tmp_path):
    test = _named_test(tmp_path)
    assert gate._scoped(test, tmp_path)


def test_changed_non_test_scoped_python_file_does_not_make_a_pair(tmp_path):
    base = _fixture_repo(tmp_path, {"tests/conftest.py": "VALUE = True\n", "tests/test_x.py": "def test_x(): pass\n"})
    (tmp_path / "tests/conftest.py").write_text("VALUE = False\n")
    _commit(tmp_path)
    assert gate.changed_pairs(tmp_path, base)[0][3] == []


def test_changed_test_pattern_file_makes_a_pair(tmp_path):
    base = _fixture_repo(tmp_path, {"tests/spine.py": "VALUE = True\n", "tests/test_x.py": "def test_x(): pass\n"})
    (tmp_path / "tests/test_x.py").write_text("def test_x(): assert True\n")
    _commit(tmp_path)
    assert gate.changed_pairs(tmp_path, base)[0][3] == ["tests/test_x.py"]


def test_collection_vital_forged_no_tests_stdout_stays_vital(tmp_path):
    test = _named_test(tmp_path)
    test.write_text("import atexit\natexit.register(lambda: print('no tests collected'))\ndef test_x(): assert True\n")
    assert gate.collection_vital(tmp_path, "tests/test_x.py") is True


def test_gate_sigterm_restores_tree_and_exits_nonzero(tmp_path):
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tests/test_x.py": "from app import VALUE\nimport time\n\ndef test_x():\n    time.sleep(1)\n    assert VALUE\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "M", "kind": "defect", "file": "app.py", "find": "True", "replace": "False",
            "expect_failed": ["tests/test_x.py::test_x"],
        }]}),
    })
    test = tmp_path / "tests/test_x.py"
    test.write_text(test.read_text().replace("assert VALUE", "assert VALUE is True"))
    _commit(tmp_path)
    process = subprocess.Popen([
        sys.executable, "scripts/changed_test_mutation_gate.py", "--repo", str(tmp_path), "--base", base,
    ], cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 10
    while (tmp_path / "app.py").read_text() == "VALUE = True\n" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "app.py").read_text() == "VALUE = False\n"
    os.kill(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 1, (stdout, stderr)
    assert "FAIL[pair:tests/test_x.py]: SWEEP-REFUSED — received signal 15" in stdout
    assert (tmp_path / "app.py").read_text() == "VALUE = True\n"
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=tmp_path, text=True) == ""


def test_json_summary_fields_and_deterministic_pair_order(tmp_path, capsys, monkeypatch):
    # The summary reports whether ARB_MEMORY_DSN is set; this test asserts the
    # reported value, so it must own that precondition rather than inherit the
    # operator's. Setting the DSN is correct practice for a meaningful suite run
    # (ENVIRONMENT-TRAPS.md §7), and without this delenv that correct practice
    # turned this test red.
    monkeypatch.delenv("ARB_MEMORY_DSN", raising=False)
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tests/test_a.py": "from app import VALUE\ndef test_a(): assert VALUE\n",
        "tests/test_a.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "a", "kind": "defect", "file": "app.py", "find": "True", "replace": "False",
            "expect_failed": ["tests/test_a.py::test_a"],
        }]}),
        "tests/test_b.py": "from app import VALUE\ndef test_b(): assert VALUE\n",
        "tests/test_b.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "b", "kind": "defect", "file": "app.py", "find": "True", "replace": "False",
            "expect_failed": ["tests/test_b.py::test_b"],
        }]}),
    })
    for name in ("a", "b"):
        (tmp_path / f"tests/test_{name}.py").write_text(
            f"from app import VALUE\ndef test_{name}(): assert VALUE is True\n"
        )
    _commit(tmp_path)
    summary = tmp_path / "summary.json"
    assert gate.main(["--repo", str(tmp_path), "--base", base, "--json", str(summary)]) == 0
    data = json.loads(summary.read_text())
    assert [item["path"] for item in data["pairs"]] == ["tests/test_a.py", "tests/test_b.py"]
    assert all(item["refused_surface"] for item in data["pairs"])
    assert all(item["legal_candidates"] == ["app.py"] for item in data["pairs"])
    assert all(isinstance(item["duration"], float) for item in data["pairs"])
    assert data["environment"] == {"interpreter": sys.executable, "ARB_MEMORY_DSN": False}
    emitted = capsys.readouterr().out.splitlines()
    assert [item["line"] for item in data["checks"]] == emitted
    assert emitted == sorted(emitted, key=lambda line: (not line.startswith("PASS[scope]"), line))


def test_json_summary_matches_emitted_checks(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    (tmp_path / "tests/test_x.py").write_text("# comment\ndef test_x(): pass\n")
    _commit(tmp_path)
    summary = tmp_path / "summary.json"
    assert gate.main(["--repo", str(tmp_path), "--base", base, "--json", str(summary)]) == 0
    emitted = capsys.readouterr().out.splitlines()
    assert [item["line"] for item in json.loads(summary.read_text())["checks"]] == emitted


def test_missing_or_invalid_kind_is_decl_invalid(tmp_path):
    test = _named_test(tmp_path)
    gate._sidecar(test).write_text(json.dumps({"schema": 1, "mutations": [{"file": "app.py", "expect_failed": ["tests/test_x.py::test_x"]}]}))
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(), {})
    assert isinstance(result, gate.GateFail) and "kind" in result.reason


def test_missing_or_unresolvable_base_is_named_fail(tmp_path):
    result = gate.changed_pairs(tmp_path, "does-not-exist")[1]
    assert result is not None and result.reason.startswith("FAIL[scope]")


def test_modifying_sidecar_alone_enters_validation_and_sweep(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"app.py": "VALUE = True\n", "tests/test_x.py": "from app import VALUE\ndef test_x(): assert VALUE\n", "tests/test_x.mutations.json": '{"schema":1,"mutations":[]}'})
    (tmp_path / "tests/test_x.mutations.json").write_text('{"schema":1,"mutations":[{"id":"M","kind":"defect","file":"app.py","find":"True","replace":"False","expect_failed":["tests/test_x.py::test_x"]}]}')
    _commit(tmp_path)
    _, _, changed, pairs = gate.changed_pairs(tmp_path, base)[0]
    assert "tests/test_x.mutations.json" in changed
    assert pairs == ["tests/test_x.py"]
    assert gate._ast_equal_to_base(tmp_path, "tests/test_x.py", base)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 0
    assert "PASS[pair:tests/test_x.py]: 1 mutations, all RED via declared tests" in capsys.readouterr().out


def test_mutation_targeting_conftest_chain_or_imported_scoped_module_is_decl_invalid(tmp_path):
    (tmp_path / "tests" / "support.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "conftest.py").write_text("pytest_plugins = ['tests.plugin']\n")
    (tmp_path / "tests" / "plugin.py").write_text("from tests import support\n")
    (tmp_path / "tests" / "support.py").write_text("VALUE = True\n")
    test = _named_test(tmp_path)
    closure = gate.pair_closure(test, tmp_path)
    assert {"tests/conftest.py", "tests/plugin.py", "tests/support.py"} <= closure.refused_surface
    for target in ("tests/conftest.py", "tests/support.py"):
        gate._sidecar(test).write_text(json.dumps({"schema": 1, "mutations": [{
            "kind": "defect", "file": target, "expect_failed": ["tests/test_x.py::test_x"],
        }]}))
        result = gate.load_declaration(test, tmp_path, closure, {})
        assert isinstance(result, gate.GateFail)
        assert result.reason == f"DECL-INVALID — mutation target is on refused import surface: {target}"


def test_mutation_targeting_paired_file_is_decl_invalid(tmp_path):
    test = _named_test(tmp_path)
    gate._sidecar(test).write_text(json.dumps({"schema": 1, "mutations": [{"kind": "defect", "file": "tests/test_x.py", "expect_failed": ["tests/test_x.py::test_x"]}]}))
    result = gate.load_declaration(test, tmp_path, gate.pair_closure(test, tmp_path), {})
    assert isinstance(result, gate.GateFail) and "refused import surface" in result.reason


def test_no_legal_target_allowlist_verified_against_recomputed_closure(tmp_path):
    test = _named_test(tmp_path)
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(), {"tests/test_x.py": {"reason": "harness-self-test", "subject": "fixture"}})
    assert result["reason"] == "harness-self-test"
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(legal_candidates={"app.py"}), {"tests/test_x.py": {"reason": "harness-self-test", "subject": "fixture"}})
    assert isinstance(result, gate.GateFail) and "legal candidates exist: app.py" in result.reason
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(probe={"claim_id": "c", "probe_artefact_id": "a", "probe_artefact_version": 1}), {"tests/test_x.py": {"reason": "harness-self-test", "subject": "fixture"}})
    assert isinstance(result, gate.GateFail) and "cannot use no-legal-target exemption" in result.reason
    gate._sidecar(test).write_text(json.dumps({"schema": 1, "no_legal_target": True, "mutations": [{"kind": "defect", "file": "app.py", "expect_failed": ["tests/test_x.py::test_x"]}]}))
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(), {})
    assert isinstance(result, gate.GateFail) and "candidate-authored" in result.reason


def test_candidate_authored_no_legal_target_beats_owner_allowlist(tmp_path):
    test = _named_test(tmp_path)
    gate._sidecar(test).write_text(json.dumps({"schema": 1, "no_legal_target": True}))
    result = gate.load_declaration(
        test, tmp_path, gate.PairClosure(),
        {"tests/test_x.py": {"reason": "harness-self-test", "subject": "fixture"}},
    )
    assert isinstance(result, gate.GateFail)
    assert "candidate-authored" in result.reason


def test_load_allowlist_reads_only_base_ref(tmp_path):
    base = _fixture_repo(tmp_path, {
        "scripts/mutation_gate_exemptions.json": json.dumps({"exemptions": [{
            "path": "tests/test_x.py", "reason": "harness-self-test", "subject": "base",
        }]}),
    })
    assert gate.load_allowlist(tmp_path, base)["tests/test_x.py"]["subject"] == "base"
    (tmp_path / "scripts/mutation_gate_exemptions.json").write_text(json.dumps({"exemptions": [{
        "path": "tests/test_y.py", "reason": "harness-self-test", "subject": "worktree",
    }]}))
    assert "tests/test_y.py" not in gate.load_allowlist(tmp_path, base)


def test_base_allowlist_governs_after_candidate_tip_revokes_entry(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "tests/test_x.py": "def test_x(): pass\n",
        "scripts/mutation_gate_exemptions.json": json.dumps({"exemptions": [{
            "path": "tests/test_x.py", "reason": "harness-self-test", "subject": "base authority",
        }]}),
    })
    (tmp_path / "tests/test_x.py").write_text("def test_x(): assert True\n")
    (tmp_path / "scripts/mutation_gate_exemptions.json").write_text('{"schema": 1, "exemptions": []}\n')
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 0
    assert "EXEMPT-NO-LEGAL-TARGET (harness-self-test) — base authority" in capsys.readouterr().out


def test_zero_collection_pair_has_no_legal_candidates(tmp_path, monkeypatch):
    test = _named_test(tmp_path)
    (tmp_path / "app.py").write_text("VALUE = True\n")
    test.write_text("from app import VALUE\n\ndef helper(): return VALUE\n")
    closure = gate.pair_closure(test, tmp_path)
    assert closure.legal_candidates == {"app.py"}
    monkeypatch.setattr(gate, "collection_vital", lambda repo, rel: False)
    closure.collection_vital = gate.collection_vital(tmp_path, "tests/test_x.py")
    if not closure.collection_vital:
        closure.legal_candidates.clear()
    assert closure.legal_candidates == set()


def test_zero_collection_pair_falls_out_of_legal_target_at_gate(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tests/test_x.py": "from app import VALUE\n\ndef helper(): return VALUE\n",
        "scripts/mutation_gate_exemptions.json": json.dumps({"exemptions": [{
            "path": "tests/test_x.py", "reason": "deselected-placeholder", "subject": "placeholder",
        }]}),
    })
    (tmp_path / "tests/test_x.py").write_text("from app import VALUE\n\ndef helper(): return VALUE is True\n")
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 0
    assert "EXEMPT-NO-LEGAL-TARGET (deselected-placeholder) — placeholder" in capsys.readouterr().out


def test_load_allowlist_absent_at_base_is_empty(tmp_path):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    assert gate.load_allowlist(tmp_path, base) == {}


def _over_budget_fixture(tmp_path):
    mutations = [{
        "id": f"over-budget-{index}", "kind": "defect", "file": "app.py",
        "find": "NOT_PRESENT", "replace": "False",
        "expect_failed": ["tests/test_x.py::test_x"],
    } for index in range(gate.MAX_MUTATIONS_PER_RUN + 1)]
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tests/test_x.py": "from app import VALUE\ndef test_x(): assert VALUE\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": mutations}),
    })
    (tmp_path / "tests/test_x.py").write_text("from app import VALUE\n\ndef test_x(): assert VALUE is True\n")
    _commit(tmp_path)
    return base


def test_over_ceiling_is_fail_budget(tmp_path, capsys):
    base = _over_budget_fixture(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert f"FAIL[budget]: declared={gate.MAX_MUTATIONS_PER_RUN + 1} ceiling={gate.MAX_MUTATIONS_PER_RUN}" in capsys.readouterr().out


def test_over_ceiling_runs_no_sweep_at_all(tmp_path, capsys):
    base = _over_budget_fixture(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    output = capsys.readouterr().out
    assert "FAIL[budget]" in output
    assert "FAIL[pair:tests/test_x.py]: SWEEP-REFUSED" not in output


def test_prered_baseline_refuses(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tests/test_x.py": "from app import VALUE\ndef test_x(): assert not VALUE\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "M", "kind": "defect", "file": "app.py", "find": "True", "replace": "False",
            "expect_failed": ["tests/test_x.py::test_x"],
        }]}),
    })
    (tmp_path / "tests/test_x.py").write_text("from app import VALUE\ndef test_x(): assert not VALUE is True\n")
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert "SWEEP-REFUSED — BASELINE-NOT-GREEN exit=1" in capsys.readouterr().out


def test_probe_marker_shape_invalid_is_named_fail(tmp_path):
    test = _named_test(tmp_path)
    test.write_text("PROBE_PROVENANCE = {'claim_id': ''}\ndef test_x(): pass\n")
    result = gate.load_declaration(test, tmp_path, gate.pair_closure(test, tmp_path), {})
    assert isinstance(result, gate.GateFail) and "marker shape invalid" in result.reason


def test_probe_provenance_file_requires_declaration(tmp_path):
    test = _named_test(tmp_path)
    test.write_text("PROBE_PROVENANCE = {'claim_id':'c','probe_artefact_id':'a','probe_artefact_version':1}\ndef test_x(): pass\n")
    result = gate.load_declaration(test, tmp_path, gate.pair_closure(test, tmp_path), {})
    assert isinstance(result, gate.GateFail) and "requires" in result.reason


def test_probe_sidecar_without_reinstate_is_decl_invalid(tmp_path):
    test = _named_test(tmp_path)
    test.write_text("PROBE_PROVENANCE = {'claim_id':'c','probe_artefact_id':'a','probe_artefact_version':1}\ndef test_x(): pass\n")
    gate._sidecar(test).write_text(json.dumps({"schema": 1, "mutations": [{"kind": "defect", "file": "app.py", "expect_failed": ["tests/test_x.py::test_x"]}]}))
    result = gate.load_declaration(test, tmp_path, gate.pair_closure(test, tmp_path), {})
    assert isinstance(result, gate.GateFail) and "reinstate" in result.reason


def test_red_via_unnamed_test_is_wrong_mechanism_fail(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "app.py": "EXPECTED = True\nUNEXPECTED = True\n",
        "tests/test_x.py": "from app import EXPECTED, UNEXPECTED\ndef test_expected(): assert EXPECTED\ndef test_unexpected(): assert UNEXPECTED\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "wrong-target", "kind": "defect", "file": "app.py", "find": "UNEXPECTED = True", "replace": "UNEXPECTED = False",
            "expect_failed": ["tests/test_x.py::test_expected"],
        }]}),
    })
    (tmp_path / "tests/test_x.py").write_text("from app import EXPECTED, UNEXPECTED\ndef test_expected(): assert EXPECTED is True\ndef test_unexpected(): assert UNEXPECTED\n")
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert "FAIL[pair:tests/test_x.py]: WRONG-MECHANISM" in capsys.readouterr().out


def test_rename_leaving_sidecar_at_old_stem_is_decl_missing():
    assert gate._sidecar(Path("tests/test_new.py")).name == "test_new.mutations.json"


def test_renamed_test_file_stays_in_scope(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"tests/test_old.py": "def test_old(): pass\n"})
    subprocess.run(["git", "mv", "tests/test_old.py", "tests/test_new.py"], cwd=tmp_path, check=True)
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert "FAIL[pair:tests/test_new.py]: DECL-MISSING" in capsys.readouterr().out


def test_repin_appends_record_in_pin_format(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    pin_path = tmp_path / "scripts/changed_test_mutation_gate.py.sha256"
    pin_path.write_text("existing-record\n")
    assert gate.main(["--repo", str(tmp_path), "--base", base, "--repin"]) == 0
    records = pin_path.read_text().splitlines()
    timestamp, digest = records[-1].split()
    assert records[:-1] == ["existing-record"]
    assert timestamp.endswith("+00:00")
    assert digest == hashlib.sha256((tmp_path / "scripts/changed_test_mutation_gate.py").read_bytes()).hexdigest()
    assert f"repinned {digest}" in capsys.readouterr().out


def test_repo_not_git_toplevel_is_usage_refusal(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    assert gate.main(["--repo", str(tmp_path / "tests"), "--base", base]) == 2
    assert capsys.readouterr().err.strip() == "FAIL[scope]: --repo must be the git toplevel"


def test_scope_facts_present_in_pass_line_and_json(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    (tmp_path / "tests/test_x.py").write_text("# comment\ndef test_x(): pass\n")
    _commit(tmp_path)
    summary = tmp_path / "summary.json"
    assert gate.main(["--repo", str(tmp_path), "--base", base, "--json", str(summary)]) == 0
    data = json.loads(summary.read_text())
    scope = next(check["line"] for check in data["checks"] if check["id"] == "scope")
    assert scope.startswith("PASS[scope]: ")
    assert all(f"{key}=" in scope for key in ("base", "head", "changed_files", "test_pairs", "exempt"))
    assert scope in capsys.readouterr().out


def test_survivor_is_named_fail_and_exit_red(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\nUNUSED = True\n",
        "tests/test_x.py": "from app import VALUE\ndef test_x(): assert VALUE\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "survives", "kind": "defect", "file": "app.py", "find": "UNUSED = True", "replace": "UNUSED = False",
            "expect_failed": ["tests/test_x.py::test_x"],
        }]}),
    })
    (tmp_path / "tests/test_x.py").write_text("from app import VALUE\ndef test_x(): assert VALUE is True\n")
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert "FAIL[pair:tests/test_x.py]: SURVIVOR" in capsys.readouterr().out


def test_sweep_source_drift_is_named_refusal(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    (tmp_path / "tests/test_x.py").write_text("# comment\ndef test_x(): pass\n")
    _commit(tmp_path)
    with (tmp_path / "scripts/mutation_sweep.py").open("a") as stream:
        stream.write("\n# drift\n")
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert "FAIL[sweep-pin]: mutation_sweep.py digest mismatch" in capsys.readouterr().out


def test_unbound_declaration_is_decl_invalid(tmp_path):
    test = _named_test(tmp_path)
    gate._sidecar(test).write_text(json.dumps({"schema": 1, "mutations": [{"kind": "defect", "file": "app.py", "expect_failed": ["other.py::test_x"]}]}))
    result = gate.load_declaration(test, tmp_path, gate.PairClosure(), {})
    assert isinstance(result, gate.GateFail) and "inside paired file" in result.reason


def test_unimported_scoped_tree_file_is_legal_target_for_meta_test_pair(tmp_path):
    test = _named_test(tmp_path)
    helper = tmp_path / "tests" / "test_helper.py"
    helper.write_text("HELPER = True\n")
    closure = gate.pair_closure(test, tmp_path)
    assert not closure.legal_candidates
    assert "tests/test_helper.py" not in closure.refused_surface
    gate._sidecar(test).write_text(json.dumps({"schema": 1, "mutations": [{
        "kind": "defect", "file": "tests/test_helper.py", "expect_failed": ["tests/test_x.py::test_x"],
    }]}))
    result = gate.load_declaration(test, tmp_path, closure, {})
    assert result["mutations"][0]["file"] == "tests/test_helper.py"


def test_unparseable_version_is_not_exempt(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tests/test_x.py": "from app import VALUE\ndef test_x(): assert VALUE\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "break-value", "kind": "defect", "file": "app.py", "find": "True", "replace": "False",
            "expect_failed": ["tests/test_x.py::test_x"],
        }]}),
    })
    (tmp_path / "tests/test_x.py").write_text("def test_x(:\n")
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    output = capsys.readouterr().out
    assert "EXEMPT-AST" not in output
    assert "COLLECTION-ERROR exit=2" in output


def test_untracked_residue_is_distinct_tree_fail_with_clean_guidance(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tests/test_x.py": "from pathlib import Path\n\ndef test_x():\n    Path('leftover.txt').write_text('residue\\n')\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "break-value", "kind": "defect", "file": "app.py", "find": "True", "replace": "False",
            "expect_failed": ["tests/test_x.py::test_x"],
        }]}),
    })
    test = tmp_path / "tests/test_x.py"
    test.write_text("MARKER = 1\n" + test.read_text())
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    output = capsys.readouterr().out
    assert "FAIL[tree]: UNTRACKED-RESIDUE — ['leftover.txt'] — preview with: git clean -n" in output
    subprocess.run(["git", "clean", "-f", "leftover.txt"], cwd=tmp_path, check=True)


def test_zero_collection_refusal_is_named_fail_with_reason(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tests/test_x.py": "import pytest\nfrom app import VALUE\n@pytest.mark.skip\ndef test_x(): assert VALUE\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "M", "kind": "defect", "file": "app.py", "find": "True", "replace": "False",
            "expect_failed": ["tests/test_x.py::test_x"],
        }]}),
    })
    (tmp_path / "tests/test_x.py").write_text("import pytest\nfrom app import VALUE\n@pytest.mark.skip(reason='all skipped')\ndef test_x(): assert VALUE\n")
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert "FAIL[pair:tests/test_x.py]: SWEEP-REFUSED — NAMED-TEST-SKIPPED" in capsys.readouterr().out


def test_deleting_sidecar_alone_is_decl_missing_fail(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n", "tests/test_x.mutations.json": '{"schema":1,"mutations":[]}'})
    (tmp_path / "tests/test_x.mutations.json").unlink()
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert "FAIL[pair:tests/test_x.py]: DECL-MISSING — no mutation sidecar" in capsys.readouterr().out


def test_missing_or_unresolvable_base_is_named_fail_not_empty_diff(tmp_path, capsys):
    _fixture_repo(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    assert gate.main(["--repo", str(tmp_path), "--base", "does-not-exist"]) == 1
    assert capsys.readouterr().out.strip() == "FAIL[scope]: base cannot be resolved: does-not-exist"


def test_unbound_declaration_is_decl_invalid_at_gate(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tests/test_x.py": "from app import VALUE\ndef test_x(): assert VALUE\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "old-format", "kind": "defect", "file": "app.py", "find": "True", "replace": "False"
        }]}),
    })
    (tmp_path / "tests/test_x.py").write_text("from app import VALUE\ndef test_x(): assert VALUE is True\n")
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert "FAIL[pair:tests/test_x.py]: DECL-INVALID" in capsys.readouterr().out


def test_skipped_named_test_refuses_with_environment_reason(tmp_path, capsys):
    base = _fixture_repo(tmp_path, {
        "app.py": "VALUE = True\n",
        "tests/test_x.py": "from app import VALUE\ndef test_x(): assert VALUE\n",
        "tests/test_x.mutations.json": json.dumps({"schema": 1, "mutations": [{
            "id": "break-value", "kind": "defect", "file": "app.py", "find": "True", "replace": "False",
            "expect_failed": ["tests/test_x.py::test_x"],
        }]}),
    })
    (tmp_path / "tests/test_x.py").write_text("import pytest\nfrom app import VALUE\n@pytest.mark.skip\ndef test_x(): assert VALUE\n")
    _commit(tmp_path)
    assert gate.main(["--repo", str(tmp_path), "--base", base]) == 1
    assert "SWEEP-REFUSED — NAMED-TEST-SKIPPED" in capsys.readouterr().out

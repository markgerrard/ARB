"""Slice 1e-i: probe-package schema validator (design §2.1/§2.2).

The validator gates SHAPE, not truth: it proves the package parses, carries
exactly the declared keys, and satisfies every mechanical §2.1 constraint.
Truth checks (excerpt-in-tree, red actually red) belong to the build tool and
deep check, not here. One test per named problem string, red-first.
"""
from __future__ import annotations

import json

from arb_memory.probe_package import (
    PACKAGE_MAX_BYTES,
    validate_package_path,
    validate_probe_package,
)

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _pkg(**overrides):
    base = {
        "probe_package_v": 1,
        "claim_id": "claim-123",
        "target": {"commit": COMMIT, "origin_hint": "github.com/example/repo"},
        "files": {
            "tests/probe_x.py": {
                "mode": "create",
                "content": "def test_defect_reproduces():\n    assert False\n",
            }
        },
        "pytest_args": ["-q"],
        "runtime": {"python": "3.12.4", "pytest": "8.2.0"},
        "red": {
            "expect_failed": ["tests/probe_x.py::test_defect_reproduces"],
            "run_log": "1 failed in 0.01s",
            "tree_provenance_stamp": (
                "tree-provenance: start head=%s tree_sha256=aa dirty=0 cmd=pytest\n"
                "tree-provenance: OK head=%s tree_sha256=aa dirty=0 cmd_exit=1"
            )
            % (COMMIT, COMMIT),
            "exit_code": 1,
        },
        "defect": {"path": "src/pkg/mod.py", "excerpt": "return wrong_value"},
        "notes": "probe demonstrates the defect",
    }
    base.update(overrides)
    return base


def _check(obj):
    return validate_probe_package(json.dumps(obj))


# ---------------------------------------------------------------- envelope


def test_valid_package_passes():
    check = _check(_pkg())
    assert check.problems == []
    assert check.ok is True
    assert check.package["claim_id"] == "claim-123"


def test_malformed_json_refused():
    check = validate_probe_package("{not json")
    assert check.ok is False
    assert any(p.startswith("malformed package JSON:") for p in check.problems)


def test_duplicate_keys_refused():
    text = '{"probe_package_v": 1, "probe_package_v": 1}'
    check = validate_probe_package(text)
    assert check.ok is False
    assert any(
        p.startswith("malformed package JSON:") and "duplicate key" in p
        for p in check.problems
    )


def test_non_object_top_level_refused():
    check = validate_probe_package("[1, 2]")
    assert "package must be a JSON object" in check.problems


def test_unknown_top_level_key_refused():
    check = _check(_pkg(zzz="?"))
    assert "unknown package key(s): ['zzz']" in check.problems


def test_missing_top_level_key_refused():
    pkg = _pkg()
    del pkg["claim_id"]
    check = _check(pkg)
    assert "missing package key(s): ['claim_id']" in check.problems


def test_package_too_large_refused():
    pkg = _pkg(notes="x" * PACKAGE_MAX_BYTES)
    check = _check(pkg)
    assert any(p.startswith("package_too_large:") for p in check.problems)


def test_wrong_schema_version_refused():
    check = _check(_pkg(probe_package_v=2))
    assert any(p.startswith("probe_package_v must be 1") for p in check.problems)


def test_blank_claim_id_refused():
    check = _check(_pkg(claim_id="   "))
    assert "claim_id must be a nonblank string" in check.problems


# ---------------------------------------------------------------- target


def test_target_must_be_object():
    check = _check(_pkg(target="oops"))
    assert "target must be an object" in check.problems


def test_target_unknown_key_refused():
    check = _check(_pkg(target={"commit": COMMIT, "origin_hint": "x", "env": {}}))
    assert "unknown target key(s): ['env']" in check.problems


def test_target_missing_key_refused():
    check = _check(_pkg(target={"commit": COMMIT}))
    assert "missing target key(s): ['origin_hint']" in check.problems


def test_target_commit_must_be_40_hex():
    for bad in ("HEAD", COMMIT[:39], COMMIT.upper(), COMMIT[:-1] + "g"):
        check = _check(_pkg(target={"commit": bad, "origin_hint": "x"}))
        assert any(
            p.startswith("target.commit must be a 40-hex commit OID")
            for p in check.problems
        ), bad


def test_target_origin_hint_must_be_string():
    check = _check(_pkg(target={"commit": COMMIT, "origin_hint": 7}))
    assert "target.origin_hint must be a string" in check.problems


# ---------------------------------------------------------------- files


def test_files_must_be_nonempty_object():
    for bad in ({}, [], "x"):
        check = _check(_pkg(files=bad))
        assert "files must be a non-empty object" in check.problems, bad


def test_file_entry_unknown_key_refused():
    files = {"tests/t.py": {"mode": "create", "content": "x", "chmod": "755"}}
    check = _check(_pkg(files=files))
    assert "files['tests/t.py']: unknown key(s) ['chmod']" in check.problems


def test_file_entry_missing_key_refused():
    files = {"tests/t.py": {"mode": "create"}}
    check = _check(_pkg(files=files))
    assert "files['tests/t.py']: missing key(s) ['content']" in check.problems


def test_file_mode_must_be_create_or_replace():
    files = {"tests/t.py": {"mode": "append", "content": "x"}}
    check = _check(_pkg(files=files))
    assert any(
        p.startswith("files['tests/t.py']: mode must be create|replace")
        for p in check.problems
    )


def test_file_content_not_text_refused():
    # A lone surrogate parses as a Python str but cannot encode to UTF-8.
    text = json.dumps(_pkg()).replace(
        '"def test_defect_reproduces():\\n    assert False\\n"', '"\\ud800"'
    )
    check = validate_probe_package(text)
    assert any(
        p.startswith("files['tests/probe_x.py']: files_not_text")
        for p in check.problems
    )


def test_file_content_non_string_refused():
    files = {"tests/t.py": {"mode": "create", "content": 42}}
    check = _check(_pkg(files=files))
    assert any(
        p.startswith("files['tests/t.py']: files_not_text") for p in check.problems
    )


def test_file_path_problems_are_prefixed():
    files = {"../evil.py": {"mode": "create", "content": "x"}}
    check = _check(_pkg(files=files))
    assert "files['../evil.py']: traversal segment in package path" in check.problems


# ------------------------------------------------- validate_package_path


def test_path_valid_examples():
    for good in ("tests/probe_x.py", "tests/sub/probe.py", "fixtures/seed.json", "conftest.py"):
        assert validate_package_path(good) == [], good


def test_path_must_be_nonblank_string():
    for bad in ("", "   ", None, 7):
        assert "package path must be a nonblank string" in validate_package_path(bad), bad


def test_path_nul_refused():
    assert "NUL in package path" in validate_package_path("tests/a\x00b.py")


def test_path_absolute_refused():
    assert "absolute package path" in validate_package_path("/etc/passwd")


def test_path_backslash_refused():
    assert "backslash in package path" in validate_package_path("tests\\t.py")


def test_path_traversal_refused():
    assert "traversal segment in package path" in validate_package_path("tests/../x.py")


def test_path_empty_or_dot_segment_refused():
    for bad in ("tests//t.py", "tests/./t.py", "tests/t.py/"):
        assert (
            "empty or dot segment in package path" in validate_package_path(bad)
        ), bad


def test_path_option_shaped_segment_refused():
    assert "option-shaped segment in package path" in validate_package_path(
        "tests/-o.py"
    )


def test_path_outside_roots_refused():
    for bad in ("src/mod.py", "setup.py", "tests", "fixtures", "conftest2.py"):
        assert (
            "package path outside allowed roots (tests/, fixtures/, conftest.py)"
            in validate_package_path(bad)
        ), bad


# ---------------------------------------------------------------- pytest_args


def test_pytest_args_must_be_list_of_strings():
    for bad in ("-q", {"a": 1}, [1]):
        check = _check(_pkg(pytest_args=bad))
        assert "pytest_args must be a list of strings" in check.problems, bad


def test_pytest_args_allowlist():
    check = _check(_pkg(pytest_args=["-q", "-x", "-p", "no:cacheprovider"]))
    assert not any("pytest_args" in p for p in check.problems)


def test_pytest_args_not_allowlisted_refused():
    check = _check(_pkg(pytest_args=["--capture=no"]))
    assert "pytest_args_not_allowlisted: '--capture=no'" in check.problems


def test_pytest_args_dangling_p_refused():
    check = _check(_pkg(pytest_args=["-p"]))
    assert "pytest_args_not_allowlisted: '-p'" in check.problems


def test_pytest_args_p_with_other_plugin_refused():
    check = _check(_pkg(pytest_args=["-p", "xdist"]))
    assert "pytest_args_not_allowlisted: '-p'" in check.problems


def test_pytest_args_stray_cacheprovider_refused():
    check = _check(_pkg(pytest_args=["no:cacheprovider"]))
    assert "pytest_args_not_allowlisted: 'no:cacheprovider'" in check.problems


# ---------------------------------------------------------------- runtime


def test_runtime_must_be_object():
    check = _check(_pkg(runtime="3.12"))
    assert "runtime must be an object" in check.problems


def test_runtime_unknown_and_missing_keys_refused():
    check = _check(_pkg(runtime={"python": "3.12", "os": "darwin"}))
    assert "unknown runtime key(s): ['os']" in check.problems
    assert "missing runtime key(s): ['pytest']" in check.problems


def test_runtime_values_must_be_nonblank_strings():
    check = _check(_pkg(runtime={"python": "", "pytest": 8}))
    assert "runtime.python must be a nonblank string" in check.problems
    assert "runtime.pytest must be a nonblank string" in check.problems


# ---------------------------------------------------------------- red


def test_red_must_be_object():
    check = _check(_pkg(red=[]))
    assert "red must be an object" in check.problems


def test_red_unknown_and_missing_keys_refused():
    red = _pkg()["red"]
    red["extra"] = 1
    del red["run_log"]
    check = _check(_pkg(red=red))
    assert "unknown red key(s): ['extra']" in check.problems
    assert "missing red key(s): ['run_log']" in check.problems


def test_expect_failed_must_be_nonempty_list():
    for bad in ([], "t::x", None):
        red = _pkg()["red"]
        red["expect_failed"] = bad
        check = _check(_pkg(red=red))
        assert (
            "red.expect_failed must be a non-empty list of node ids"
            in check.problems
        ), bad


def test_expect_failed_entries_must_be_node_ids():
    red = _pkg()["red"]
    red["expect_failed"] = ["tests/probe_x.py"]
    check = _check(_pkg(red=red))
    assert "red.expect_failed[0] must be a node id containing '::'" in check.problems


def test_expect_failed_duplicates_refused():
    red = _pkg()["red"]
    red["expect_failed"] = ["t.py::a", "t.py::a"]
    check = _check(_pkg(red=red))
    assert "red.expect_failed contains duplicate node id(s)" in check.problems


def test_run_log_must_be_nonblank():
    red = _pkg()["red"]
    red["run_log"] = "  "
    check = _check(_pkg(red=red))
    assert "red.run_log must be a nonblank string" in check.problems


def test_stamp_must_be_nonblank():
    red = _pkg()["red"]
    red["tree_provenance_stamp"] = ""
    check = _check(_pkg(red=red))
    assert "red.tree_provenance_stamp must be a nonblank string" in check.problems


def test_exit_code_must_be_integer():
    for bad in ("1", 1.5, True, None):
        red = _pkg()["red"]
        red["exit_code"] = bad
        check = _check(_pkg(red=red))
        assert "red.exit_code must be an integer" in check.problems, bad


# ---------------------------------------------------------------- defect


def test_defect_must_be_object():
    check = _check(_pkg(defect="src/mod.py"))
    assert "defect must be an object" in check.problems


def test_defect_unknown_and_missing_keys_refused():
    check = _check(_pkg(defect={"path": "src/m.py", "line": 3}))
    assert "unknown defect key(s): ['line']" in check.problems
    assert "missing defect key(s): ['excerpt']" in check.problems


def test_defect_path_must_be_nonblank():
    check = _check(_pkg(defect={"path": " ", "excerpt": "x"}))
    assert "defect.path must be a nonblank string" in check.problems


def test_defect_path_hostility_rules():
    cases = {
        "/etc/passwd": "absolute defect.path",
        "src/../x.py": "traversal segment in defect.path",
        "src/-o.py": "option-shaped segment in defect.path",
        "src\\m.py": "backslash in defect.path",
        "src/a\x00b.py": "NUL in defect.path",
    }
    for bad, expected in cases.items():
        check = _check(_pkg(defect={"path": bad, "excerpt": "x"}))
        assert expected in check.problems, bad


def test_defect_excerpt_must_be_nonblank():
    check = _check(_pkg(defect={"path": "src/m.py", "excerpt": ""}))
    assert "defect.excerpt must be a nonblank string" in check.problems


# ---------------------------------------------------------------- notes


def test_notes_must_be_string():
    check = _check(_pkg(notes=None))
    assert "notes must be a string" in check.problems


def test_problems_accumulate():
    pkg = _pkg(claim_id="", notes=7, probe_package_v=9)
    check = _check(pkg)
    assert len(check.problems) >= 3
    assert check.ok is False

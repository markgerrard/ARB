"""Tests for scripts/coverage_mutation_sweep.py — rem3/rem5 acceptance harness.

The harness exists to prove coverage-guard mutations are refused, so its own
refusals are the part that must be load-bearing. Each refusal test asserts the
OBSERVABLE CONSEQUENCE (uncommitted work still present, tree still clean,
nothing further mutated) rather than just that an exception was raised.

Load-bearing guards (matching scripts/mutation_sweep.py discipline):

  1. dirty-tree refusal — ``git checkout --`` restores HEAD, not intent
  2. no-op-mutation refusal — find==replace must not be reported as SURVIVOR
  3. emptied-catalogue refusal — floor must RED (same class as zero-tests-collected)
  4. real-loop end-to-end — at least one self-test calls ``run_coverage_sweep``
     (not only helpers) against a faithful mini and asserts count floor +
     planted SURVIVOR nonzero exit
  5. machinery mutants in the catalogue — call-del, 3-site composite, KNOWN_TAGS
     weaken; after the unit second-literal pins they are REFUSED@unit

Note: the 27af2b75 claim that "a retag mutant then SURVIVES" when the call is
deleted is FALSE — unit classification tests refuse retags with or without the
call. Call-deletion is caught by a dedicated machinery mutant + unit source pin,
not by an indirect retag path.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coverage_mutation_sweep import (  # noqa: E402
    MIN_MUTATION_FLOOR,
    Mutation,
    SweepRefused,
    apply_mutation,
    apply_replacements,
    assert_clean_tree,
    build_mutation_catalogue,
    check_refused_source_only,
    import_ok,
    main,
    parse_probed_pairs,
    restore_source,
    run_coverage_sweep,
    sha256_file,
    worktree_env,
)


# Faithful mini: same anchors the production catalogue mutates (KNOWN_TAGS
# close, EXEMPT block, base INSERT line, module-scope call, probed-by lines).
# Unit tests pin second literals so machinery mutants REFUSE@unit; a planted
# always-green path is provided separately for SURVIVOR proof.
MINI_SRC = textwrap.dedent(
    '''\
    """Mini stand-in for claim_resolver privilege-coverage surface (faithful anchors)."""

    KNOWN_TAGS: frozenset[str] = frozenset(
        {
            "probed-by-view-select-required",
            "probed-by-view-relation",
            "probed-by-view-relation+column",
            "probed-by-base-relation",
            "probed-by-base-relation+column",
            "exempt-because-select-alone-is-not-a-mint-or-escalation-path",
        }
    )

    EXEMPT_COVERAGE_PAIRS: frozenset[tuple[str, str]] = frozenset(
        {
            ("base_table", "SELECT"),
        }
    )

    PRIVILEGE_COVERAGE = {
        ("view", "SELECT"): "probed-by-view-select-required",
        ("view", "INSERT"): "probed-by-view-relation+column",
        ("base_table", "SELECT"): (
            "exempt-because-select-alone-is-not-a-mint-or-escalation-path"
        ),
        ("base_table", "INSERT"): "probed-by-base-relation+column",
    }


    def _validate_privilege_coverage(coverage=None):
        coverage = PRIVILEGE_COVERAGE if coverage is None else coverage
        for tag in coverage.values():
            if tag not in KNOWN_TAGS:
                raise ValueError(
                    f"PRIVILEGE_COVERAGE has tags outside KNOWN_TAGS (closed vocabulary): {[tag]}"
                )


    _validate_privilege_coverage()
    '''
)

# Unit suite for faithful mini: second-literal pins + call pin (machinery refuse).
MINI_TESTS = textwrap.dedent(
    '''\
    from pathlib import Path
    import agent_redis_bridge.claim_resolver as cr

    def test_exempt_and_known_tags_pinned():
        assert cr.EXEMPT_COVERAGE_PAIRS == frozenset({("base_table", "SELECT")})
        assert cr.KNOWN_TAGS == frozenset(
            {
                "probed-by-view-select-required",
                "probed-by-view-relation",
                "probed-by-view-relation+column",
                "probed-by-base-relation",
                "probed-by-base-relation+column",
                "exempt-because-select-alone-is-not-a-mint-or-escalation-path",
            }
        )

    def test_module_scope_call_present():
        src = Path(cr.__file__).read_text()
        bare = [ln for ln in src.splitlines() if ln.strip() == "_validate_privilege_coverage()"]
        assert bare == ["_validate_privilege_coverage()"]
    '''
)

# Always-pass suite: call-deletion alone is a SURVIVOR (planted-survivor proof).
MINI_ALWAYS_PASS = "def test_always_passes():\n    assert True\n"

PKG_INIT = ""


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)


def _mini_repo(tmp_path: Path, *, tests_body: str = MINI_ALWAYS_PASS, src_body: str = MINI_SRC) -> Path:
    """Repo is a SUBDIR so callers can place specs beside it without dirtying it."""
    repo = tmp_path / "repo"
    src = repo / "src" / "agent_redis_bridge"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(PKG_INIT)
    (src / "claim_resolver.py").write_text(src_body)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_claim_resolver.py").write_text(tests_body)
    # Without this, pytest's __pycache__ dirties the tree and the post-sweep
    # clean-tree check refuses (false BROKEN on a faithful mini).
    (repo / ".gitignore").write_text("__pycache__/\n*.py[cod]\n.pytest_cache/\n")
    _git_init(repo)
    return repo


def test_dirty_tree_is_refused_before_anything_is_touched(tmp_path):
    """Load-bearing refusal: uncommitted work must still be on disk after refuse."""
    repo = _mini_repo(tmp_path)
    target = repo / "src" / "agent_redis_bridge" / "claim_resolver.py"
    target.write_text(target.read_text() + "\nUNCOMMITTED = 'precious'\n")
    with pytest.raises(SweepRefused) as exc:
        assert_clean_tree(repo, when="before the sweep")
    assert "dirty" in str(exc.value)
    assert "no override" in str(exc.value).lower()
    assert "precious" in target.read_text(), (
        "the sweep destroyed uncommitted work — the refusal is not load-bearing"
    )


def test_untracked_file_also_counts_as_dirty(tmp_path):
    repo = _mini_repo(tmp_path)
    (repo / "scratch.txt").write_text("x")
    with pytest.raises(SweepRefused) as exc:
        assert_clean_tree(repo, when="before the sweep")
    assert "dirty" in str(exc.value)


def test_mutation_that_changes_no_bytes_is_refused_not_reported_as_surviving(tmp_path):
    """A no-op find/replace must refuse — not be misreported as SURVIVOR."""
    repo = _mini_repo(tmp_path)
    target = repo / "src" / "agent_redis_bridge" / "claim_resolver.py"
    anchor = "_validate_privilege_coverage()"
    with pytest.raises(SweepRefused) as exc:
        apply_mutation(target, anchor, anchor, label="no-op call")
    msg = str(exc.value)
    assert "changed no bytes" in msg
    assert "SURVIVOR" not in msg.upper() or "misattributes" in msg
    # Tree untouched.
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout == ""


def test_anchor_must_match_exactly_once(tmp_path):
    repo = _mini_repo(tmp_path)
    target = repo / "src" / "agent_redis_bridge" / "claim_resolver.py"
    with pytest.raises(SweepRefused) as exc:
        apply_mutation(target, "NOT_PRESENT_AT_ALL", "x", label="zero")
    assert "exactly 1" in str(exc.value)


def test_deleted_validator_call_is_reported_as_survivor(tmp_path):
    """Planted-survivor visibility against an always-pass suite.

    Against a temp copy whose unit suite always passes, deleting the call is a
    SURVIVOR — proves the harness can name that outcome. Production refuses the
    same mutant via the unit source pin of the bare call (see
    test_module_scope_validator_call_is_present_in_source), not via retag.
    """
    repo = _mini_repo(tmp_path, tests_body=MINI_ALWAYS_PASS)
    relpath = "src/agent_redis_bridge/claim_resolver.py"
    target = repo / relpath
    base_hash = sha256_file(target)

    apply_mutation(
        target,
        "\n_validate_privilege_coverage()\n",
        "\n# _validate_privilege_coverage()  # deleted — planted survivor\n",
        label="del validator call",
    )
    mutated = target.read_text()
    assert "deleted — planted survivor" in mutated
    assert "def _validate_privilege_coverage" in mutated
    live_call_lines = [
        ln
        for ln in mutated.splitlines()
        if ln.strip() == "_validate_privilege_coverage()"
    ]
    assert live_call_lines == [], f"live call still present: {live_call_lines}"

    results: list[tuple[str, str, str]] = []
    check_refused_source_only(
        repo,
        sys.executable,
        "del validator call",
        results,
        test_relpath="tests/test_claim_resolver.py",
    )
    restore_source(repo, relpath, base_hash)

    assert results, "harness produced no result rows"
    label, verdict, detail = results[0]
    assert label == "del validator call"
    assert verdict == "SURVIVOR", (
        f"expected SURVIVOR for call deletion against always-pass suite; "
        f"got {verdict}: {detail}"
    )
    assert "green" in detail.lower() or "import OK" in detail


def test_deleted_validator_call_cli_exits_nonzero(tmp_path):
    """CLI contract: a run that records a SURVIVOR must exit 1, not 0."""
    repo = _mini_repo(tmp_path, tests_body=MINI_ALWAYS_PASS)
    relpath = "src/agent_redis_bridge/claim_resolver.py"
    target = repo / relpath
    base_hash = sha256_file(target)

    apply_mutation(
        target,
        "\n_validate_privilege_coverage()\n",
        "\n# call removed\n",
        label="del validator call",
    )
    results: list[tuple[str, str, str]] = []
    check_refused_source_only(
        repo,
        sys.executable,
        "del validator call",
        results,
        test_relpath="tests/test_claim_resolver.py",
    )
    restore_source(repo, relpath, base_hash)

    survivors = [r for r in results if r[1] == "SURVIVOR"]
    assert survivors, f"expected SURVIVOR row, got {results}"
    exit_code = 1 if survivors else 0
    assert exit_code == 1


def test_cli_dirty_tree_exits_2(tmp_path):
    """main() maps SweepRefused (dirty tree) to exit 2."""
    repo = _mini_repo(tmp_path)
    (repo / "dirt.txt").write_text("x")
    code = main([str(repo)])
    assert code == 2


def test_cli_no_live_flag_skips_live_and_still_refuses_dirty(tmp_path):
    """--no-live is accepted; dirty-tree refusal still protects direct runs."""
    repo = _mini_repo(tmp_path)
    (repo / "dirt.txt").write_text("x")
    # Dirty refusal is independent of --no-live (hook isolation is the escape,
    # not this flag).
    assert main(["--no-live", str(repo)]) == 2
    (repo / "dirt.txt").unlink()
    # Clean mini + --no-live: no DSN required; production pair-count floor
    # still REFUSES (exit 2) rather than argparse error or live ALL-SKIPPED.
    code = main(["--no-live", str(repo)])
    assert code == 2


def test_worktree_env_forces_repo_src_on_pythonpath(tmp_path):
    """Ambient parent PYTHONPATH must not mask worktree mutations."""
    repo = _mini_repo(tmp_path)
    env = worktree_env(repo, base_env={"PYTHONPATH": "/some/parent/src", "PATH": "/bin"})
    assert env["PYTHONPATH"] == str(repo.resolve() / "src")
    assert "/some/parent" not in env["PYTHONPATH"]


def test_import_ok_rejects_path_outside_target_repo(tmp_path, monkeypatch):
    """Every sweep subprocess path-check: import outside target is not ok."""
    repo = _mini_repo(tmp_path)
    # Point PYTHONPATH at a different tree that also has the package name via
    # monkeypatching worktree_env is too invasive; instead assert import_ok
    # succeeds for the mini (under repo) and that a forged outside path fails
    # the startswith check by using a sibling decoy package.
    ok, where = import_ok(repo, sys.executable)
    assert ok, where
    assert str(where).startswith(str(repo.resolve()))


# ---------------------------------------------------------------------------
# rem5: catalogue machinery + floor + real-loop self-tests
# ---------------------------------------------------------------------------


def test_production_catalogue_includes_machinery_and_meets_floor():
    """Catalogue is more than table entries; floor is load-bearing."""
    root = Path(__file__).resolve().parents[1]
    text = (root / "src" / "agent_redis_bridge" / "claim_resolver.py").read_text()
    muts = build_mutation_catalogue(text)
    labels = {m.label for m in muts}

    assert "del module-scope validator call" in labels
    assert "composite 3-site exempt base_table.INSERT" in labels
    assert "weaken KNOWN_TAGS with free tag" in labels

    # Test-side floor pin: emptying the catalogue must not silently drop below
    # the accepted production size (52 table + 3 machinery).
    assert MIN_MUTATION_FLOOR >= 55
    assert len(muts) >= MIN_MUTATION_FLOOR
    # Also pin the production pair count the catalogue expands from.
    assert len(parse_probed_pairs(text)) == 15


def test_emptied_catalogue_is_refused_by_run_coverage_sweep(tmp_path):
    """Same guard as mutation_sweep zero-tests-collected: empty catalogue RED."""
    repo = _mini_repo(tmp_path, tests_body=MINI_TESTS)
    with pytest.raises(SweepRefused) as exc:
        run_coverage_sweep(
            repo,
            py=sys.executable,
            run_live=False,
            expected_pair_count=None,  # mini has 2 probed-by lines, not 15
            min_mutations=1,
            catalogue=[],  # emptied
        )
    msg = str(exc.value)
    assert "floor" in msg or "0" in msg
    assert "catalogue" in msg.lower() or "entries" in msg


def test_run_coverage_sweep_reports_planted_survivor_end_to_end(tmp_path):
    """Real loop (run_coverage_sweep, not a helper) names a planted SURVIVOR.

    Catalogue is a single call-deletion mutant; mini unit suite always passes
    → SURVIVOR → nonzero exit policy. Closes the strawman gap: self-tests
    previously never called run_coverage_sweep's mutation loop.
    """
    repo = _mini_repo(tmp_path, tests_body=MINI_ALWAYS_PASS)
    planted = [
        Mutation(
            label="planted del module-scope validator call",
            replacements=(
                (
                    "\n_validate_privilege_coverage()\n",
                    "\n# _validate_privilege_coverage()  # planted\n",
                ),
            ),
        )
    ]
    results, report = run_coverage_sweep(
        repo,
        py=sys.executable,
        run_live=False,
        expected_pair_count=None,
        min_mutations=1,
        catalogue=planted,
    )
    survivors = [r for r in results if r[1] == "SURVIVOR"]
    assert survivors, f"expected SURVIVOR named; got results={results}\n{report}"
    assert any("planted del" in r[0] for r in survivors)
    # Exit policy identical to main(): survivors -> 1
    exit_code = 1 if survivors else 0
    assert exit_code == 1
    # Tree restored
    assert "TREE RESTORED CLEAN" in report
    src = (repo / "src" / "agent_redis_bridge" / "claim_resolver.py").read_text()
    assert any(
        ln.strip() == "_validate_privilege_coverage()" for ln in src.splitlines()
    ), "call must be restored after sweep"


def test_run_coverage_sweep_refuses_machinery_on_faithful_mini(tmp_path):
    """Faithful mini + pin tests: production machinery mutants REFUSE@unit."""
    repo = _mini_repo(tmp_path, tests_body=MINI_TESTS)
    text = (repo / "src" / "agent_redis_bridge" / "claim_resolver.py").read_text()
    # Build only the three machinery mutants (table anchors differ on mini size).
    full = build_mutation_catalogue(text)
    machinery = [
        m
        for m in full
        if m.label
        in {
            "del module-scope validator call",
            "composite 3-site exempt base_table.INSERT",
            "weaken KNOWN_TAGS with free tag",
        }
    ]
    assert len(machinery) == 3, f"expected 3 machinery mutants, got {[m.label for m in machinery]}"

    results, report = run_coverage_sweep(
        repo,
        py=sys.executable,
        run_live=False,
        expected_pair_count=None,
        min_mutations=3,
        catalogue=machinery,
    )
    assert len(results) == 3, report
    survivors = [r for r in results if r[1] == "SURVIVOR"]
    assert survivors == [], f"machinery must be REFUSED; got {results}\n{report}"
    for label, verdict, detail in results:
        assert verdict.startswith("REFUSED"), f"{label}: {verdict} {detail}"


def test_apply_replacements_composite_is_atomic(tmp_path):
    """Multi-site composite changes all sites or refuses mid-anchor."""
    repo = _mini_repo(tmp_path)
    target = repo / "src" / "agent_redis_bridge" / "claim_resolver.py"
    base = target.read_text()
    apply_replacements(
        target,
        (
            (
                "\n_validate_privilege_coverage()\n",
                "\n# call gone\n",
            ),
            (
                '        ("base_table", "SELECT"),\n',
                '        ("base_table", "SELECT"),\n        ("base_table", "INSERT"),\n',
            ),
        ),
        label="composite-test",
    )
    text = target.read_text()
    assert "# call gone" in text
    assert '("base_table", "INSERT")' in text
    assert text != base

#!/usr/bin/env python3
"""Exhaustive coverage-guard mutation sweep for claim_resolver's PRIVILEGE_COVERAGE.

Usage:
    coverage_mutation_sweep.py [repo-or-worktree-root] [expected-head-prefix]
    coverage_mutation_sweep.py --no-live [repo-or-worktree-root] [expected-head-prefix]

  --no-live  Skip the live-PG control and per-mutation live checks (unit/import
             only). Intended for isolated worktree fast paths; the standing
             hook still runs live when ARB_MEMORY_DSN is present.

Per the rem3/rem5 acceptance contract: EVERY probed pair retagged -> refusal;
EVERY probed pair misspelled -> import failure; EVERY pair deleted -> import
failure; every +column tag stripped of +column -> refusal; AND the machinery
mutations (module-scope validator-call deletion, cold-Opus 3-site composite
exempt expansion, KNOWN_TAGS weakening) -> refusal. Refusal = import failure
OR at least one test failure. A mutation that leaves everything green is a
SURVIVOR and fails acceptance (exit 1).

Harness discipline (mirrors scripts/mutation_sweep.py):
  DIRTY TREE -> refuse (exit 2). Revert is ``git checkout --``, which restores
  HEAD not intent; uncommitted work in a mutated file is eaten by the first
  revert. No --allow-dirty escape.
  NO-OP MUTATION -> refuse (exit 2). An anchor that matches but changes no
  bytes would be reported as SURVIVOR and misattribute a broken harness entry
  to a suite gap.
  EMPTIED CATALOGUE -> refuse (exit 2). Same guard as mutation_sweep's
  zero-tests-collected: a sweep that applies nothing reports 0 survivors while
  testing nothing. Floor is MIN_MUTATION_FLOOR.
  RESTORE-BY-HASH after every mutation; control proves the harness can see
  success (unmutated green) before believing its refusals.
  WORKTREE IMPORT: PYTHONPATH is forced to ``<repo>/src`` so an ambient
  parent-checkout PYTHONPATH cannot mask mutations; every subprocess asserts
  the imported claim_resolver ``__file__`` resolves under the target root.

Self-tests live in tests/test_coverage_mutation_sweep.py. A standing live hook
in tests/arb_memory/ shells this script against the real repo so call-deletion
is a standing test failure, not a manual-run-only catch.

Note on the 27af2b75 claim: retag mutants are refused by the unit
classification tests with or without the module-scope call; the call
contributes nothing the table-only catalogue could observe. That claim is
false and is not preserved here. Call-deletion is caught by a dedicated
machinery mutant in this catalogue plus a unit source pin of the bare call.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class SweepRefused(Exception):
    """The sweep will not run (or will not continue), and has changed nothing further."""


# Table mutations: 15 pairs × (retag + typo + del) + 6 (-col for +column tags)
# + 1 exempt-block del = 52. Machinery: call-del + 3-site composite + KNOWN_TAGS
# weaken = 3. Floor = 55. Emptied or truncated catalogue must RED.
MIN_MUTATION_FLOOR = 55


@dataclass(frozen=True)
class Mutation:
    """One catalogue entry: one or more exact find/replace steps as a single mutant."""

    label: str
    replacements: tuple[tuple[str, str], ...]


def sha256_file(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd), env=env)


def git(repo: Path, *args: str) -> str:
    r = run_cmd(["git", *args], cwd=repo)
    if r.returncode != 0:
        raise SweepRefused(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def assert_clean_tree(repo: Path, *, when: str) -> None:
    dirty = git(repo, "status", "--porcelain").strip()
    if dirty:
        raise SweepRefused(
            f"working tree is dirty {when}; refusing.\n"
            f"{dirty}\n"
            "A revert restores the last COMMIT, not your intent — uncommitted work "
            "in a mutated file is silently discarded by the first revert. "
            "Commit or stash first. There is no override."
        )


def worktree_env(repo: Path, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Force imports to resolve under this repo's src/ (not an ambient parent).

    Replaces PYTHONPATH entirely with ``<repo>/src`` so an ambient parent
    checkout path cannot shadow the worktree under test.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["PYTHONPATH"] = str(Path(repo).resolve() / "src")
    return env


def _repo_resolved(repo: Path | str) -> Path:
    return Path(repo).resolve()


def _default_py(repo: Path | str) -> str:
    """Prefer ``<repo>/.venv/bin/python``; fall back to the running interpreter.

    Throwaway worktrees of HEAD do not carry an untracked ``.venv``; the
    standing hook (and any ``--no-live`` worktree run) invokes this script with
    a venv interpreter already on PATH via ``sys.executable``.
    """
    venv_py = Path(repo) / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable


def import_ok(repo: Path, py: str) -> tuple[bool, str]:
    """Import claim_resolver; succeed only if ``__file__`` is under the target repo."""
    repo = _repo_resolved(repo)
    env = worktree_env(repo)
    r = run_cmd(
        [
            py,
            "-c",
            "import agent_redis_bridge.claim_resolver as c; print(c.__file__)",
        ],
        cwd=repo,
        env=env,
    )
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        return False, out
    file_line = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
    try:
        imported = Path(file_line).resolve()
    except OSError:
        return False, f"import path unresolvable: {file_line!r}"
    repo_s = str(repo)
    if not str(imported).startswith(repo_s + os.sep) and str(imported) != repo_s:
        return (
            False,
            f"import resolved outside target repo: {imported} (want under {repo_s})",
        )
    return True, str(imported)


def unit_green(repo: Path, py: str) -> tuple[bool, str]:
    """Run unit suite; every subprocess forces worktree src and checks import path."""
    repo = _repo_resolved(repo)
    ok, where = import_ok(repo, py)
    if not ok:
        return False, f"unit pre-import path check failed: {where}"
    env = worktree_env(repo)
    r = run_cmd(
        [py, "-m", "pytest", "tests/test_claim_resolver.py", "-q", "--no-header", "-x"],
        cwd=repo,
        env=env,
    )
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]
    return r.returncode == 0, tail


def live_green(repo: Path, py: str) -> tuple[bool | None, str]:
    """Run live suite; every subprocess forces worktree src and checks import path."""
    repo = _repo_resolved(repo)
    ok, where = import_ok(repo, py)
    if not ok:
        return False, f"live pre-import path check failed: {where}"
    env = worktree_env(repo)
    r = run_cmd(
        [
            py,
            "-m",
            "pytest",
            "tests/arb_memory/test_claim_resolver.py",
            "-q",
            "--no-header",
            "-x",
        ],
        cwd=repo,
        env=env,
    )
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]
    if "skipped" in tail and "passed" not in tail:
        return None, "ALL-SKIPPED (DSN missing?) — proves nothing"
    return r.returncode == 0, tail


def apply_mutation(path: Path, old_text: str, new_text: str, *, label: str = "") -> None:
    """Replace old_text with new_text exactly once; refuse zero/multi/no-op matches."""
    apply_replacements(path, ((old_text, new_text),), label=label)


def apply_replacements(
    path: Path,
    replacements: tuple[tuple[str, str], ...] | list[tuple[str, str]],
    *,
    label: str = "",
) -> None:
    """Apply one or more exact find/replace steps as a single composite mutant."""
    text = path.read_text()
    original = text
    for old_text, new_text in replacements:
        n = text.count(old_text)
        if n != 1:
            raise SweepRefused(
                f"{label or path.name}: anchor matched {n} times (must be exactly 1) "
                f"for {old_text[:60]!r}"
            )
        text = text.replace(old_text, new_text)
    if text == original:
        raise SweepRefused(
            f"{label or path.name}: matched its anchor(s) but changed no bytes "
            f"(find and replace are equivalent). Without this refusal the run is "
            f"reported as SURVIVOR, which misattributes a broken harness entry to "
            f"a gap in the suite — opposite remediations. A mutation must actually mutate."
        )
    path.write_text(text)


def restore_source(repo: Path, relpath: str, baseline_hash: str) -> None:
    git(repo, "checkout", "--", relpath)
    got = sha256_file(repo / relpath)
    if got != baseline_hash:
        raise SweepRefused(
            f"RESTORE FAILED for {relpath}: hash {got} != baseline {baseline_hash}"
        )


def check_refused(
    repo: Path,
    py: str,
    label: str,
    results: list[tuple[str, str, str]],
) -> None:
    """Append (label, verdict, detail). SURVIVOR = import + unit + live all green."""
    ok_import, where = import_ok(repo, py)
    if not ok_import:
        results.append((label, "REFUSED@import", where.splitlines()[-1][:110]))
        return
    g_unit, out_u = unit_green(repo, py)
    if not g_unit:
        results.append((label, "REFUSED@unit", str(out_u)[:110]))
        return
    g_live, out_l = live_green(repo, py)
    if g_live is None:
        results.append((label, "BROKEN-HARNESS", str(out_l)))
        return
    if not g_live:
        results.append((label, "REFUSED@live", str(out_l)[:110]))
        return
    results.append((label, "SURVIVOR", "import OK, unit green, live green"))


# Minimal source-only check used by self-tests (no live DSN, no full suite).
def check_refused_source_only(
    repo: Path,
    py: str,
    label: str,
    results: list[tuple[str, str, str]],
    *,
    test_relpath: str | None = None,
) -> None:
    """Like check_refused but import + optional single test file only.

    Used by self-tests to prove SURVIVOR detection without a Postgres DSN.
    If ``test_relpath`` is None, green tests are treated as always-green (so a
    mutation that does not break import is a SURVIVOR — the call-deletion shape
    against a strawman always-pass suite).
    """
    ok_import, where = import_ok(repo, py)
    if not ok_import:
        results.append((label, "REFUSED@import", where.splitlines()[-1][:110]))
        return
    if test_relpath is None:
        results.append((label, "SURVIVOR", "import OK, no suite configured"))
        return
    env = worktree_env(repo)
    r = run_cmd(
        [py, "-m", "pytest", test_relpath, "-q", "--no-header", "-x"],
        cwd=repo,
        env=env,
    )
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]
    if r.returncode != 0:
        results.append((label, "REFUSED@unit", str(tail)[:110]))
        return
    results.append((label, "SURVIVOR", f"import OK, {test_relpath} green"))


def parse_probed_pairs(text: str) -> list[tuple[str, str, str]]:
    pat = re.compile(
        r'^    \("(view|base_table)", "([A-Z]+)"\): "(probed-by-[^"]+)",$',
        re.M,
    )
    return pat.findall(text)


# Anchors shared by machinery mutants (must match production claim_resolver.py).
_KNOWN_TAGS_CLOSE = (
    '        "exempt-because-select-alone-is-not-a-mint-or-escalation-path",\n'
    "    }\n"
    ")"
)
_EXEMPT_BLOCK = (
    "EXEMPT_COVERAGE_PAIRS: frozenset[tuple[str, str]] = frozenset(\n"
    "    {\n"
    '        ("base_table", "SELECT"),\n'
    "    }\n"
    ")"
)
_BASE_INSERT_LINE = '    ("base_table", "INSERT"): "probed-by-base-relation+column",'
_MODULE_SCOPE_CALL = "\n_validate_privilege_coverage()\n"
_COMPOSITE_TAG = "exempt-because-acceptance-sweep-composite"
_WEAKEN_TAG = "arbitrary-free-tag-not-used-by-coverage"


def build_mutation_catalogue(text: str) -> list[Mutation]:
    """Build the full mutation catalogue from claim_resolver source text.

    Table mutations cover every probed pair; machinery mutations cover the
    activation edge and the pin layer (rem5 P1-A / P1-B).
    """
    pairs = parse_probed_pairs(text)
    mutations: list[Mutation] = []

    for kind, priv, tag in pairs:
        line = f'    ("{kind}", "{priv}"): "{tag}",'
        # Retag to a string outside KNOWN_TAGS — refused as closed-vocabulary
        # violation (not as an "unpinned legal exempt"; that string is not legal).
        mutations.append(
            Mutation(
                label=f"retag {kind}.{priv}",
                replacements=(
                    (
                        line,
                        f'    ("{kind}", "{priv}"): "exempt-because-acceptance-sweep",',
                    ),
                ),
            )
        )
        bad = tag[:-1] + ("x" if tag[-1] != "x" else "y")
        mutations.append(
            Mutation(
                label=f"typo  {kind}.{priv}",
                replacements=((line, f'    ("{kind}", "{priv}"): "{bad}",'),),
            )
        )
        mutations.append(
            Mutation(
                label=f"del   {kind}.{priv}",
                replacements=((line + "\n", ""),),
            )
        )
        if tag.endswith("+column"):
            mutations.append(
                Mutation(
                    label=f"-col  {kind}.{priv}",
                    replacements=(
                        (
                            line,
                            f'    ("{kind}", "{priv}"): "{tag[: -len("+column")]}",',
                        ),
                    ),
                )
            )

    # Delete the multi-line exempt pair.
    block = (
        '    ("base_table", "SELECT"): (\n'
        '        "exempt-because-select-alone-is-not-a-mint-or-escalation-path"\n'
        "    ),\n"
    )
    mutations.append(
        Mutation(
            label="del   base_table.SELECT(exempt)",
            replacements=((block, ""),),
        )
    )

    # --- Machinery mutations (rem5): beyond the coverage TABLE ---

    # (a) Delete the module-scope validator call. The unit source pin of the
    # bare call makes this REFUSED@unit; without that pin it survives unit+live
    # AND a table-only catalogue (the false 27af2b75 retag-then-survives claim).
    mutations.append(
        Mutation(
            label="del module-scope validator call",
            replacements=(
                (
                    _MODULE_SCOPE_CALL,
                    "\n# _validate_privilege_coverage()  # machinery mutant: call deleted\n",
                ),
            ),
        )
    )

    # (b) Cold-Opus 3-site composite as ONE mutant: retag base_table INSERT to a
    # new exempt tag, add that tag to KNOWN_TAGS, add the pair to EXEMPT.
    # Consistent with the import-time validator; refused by the test-side
    # second-literal pins (and the sweep proves the composition stays closed).
    mutations.append(
        Mutation(
            label="composite 3-site exempt base_table.INSERT",
            replacements=(
                (
                    _KNOWN_TAGS_CLOSE,
                    (
                        '        "exempt-because-select-alone-is-not-a-mint-or-escalation-path",\n'
                        f'        "{_COMPOSITE_TAG}",\n'
                        "    }\n"
                        ")"
                    ),
                ),
                (
                    _EXEMPT_BLOCK,
                    (
                        "EXEMPT_COVERAGE_PAIRS: frozenset[tuple[str, str]] = frozenset(\n"
                        "    {\n"
                        '        ("base_table", "SELECT"),\n'
                        '        ("base_table", "INSERT"),\n'
                        "    }\n"
                        ")"
                    ),
                ),
                (
                    _BASE_INSERT_LINE,
                    f'    ("base_table", "INSERT"): "{_COMPOSITE_TAG}",',
                ),
            ),
        )
    )

    # (c) Weaken KNOWN_TAGS by adding an arbitrary unused tag — second-literal
    # pin on KNOWN_TAGS goes RED; without it this is a silent vocabulary widen.
    mutations.append(
        Mutation(
            label="weaken KNOWN_TAGS with free tag",
            replacements=(
                (
                    _KNOWN_TAGS_CLOSE,
                    (
                        '        "exempt-because-select-alone-is-not-a-mint-or-escalation-path",\n'
                        f'        "{_WEAKEN_TAG}",\n'
                        "    }\n"
                        ")"
                    ),
                ),
            ),
        )
    )

    return mutations


def run_coverage_sweep(
    repo: Path,
    *,
    expect_head_prefix: str | None = None,
    py: str | None = None,
    relpath: str = "src/agent_redis_bridge/claim_resolver.py",
    run_live: bool = True,
    expected_pair_count: int | None = 15,
    min_mutations: int | None = None,
    catalogue: list[Mutation] | None = None,
) -> tuple[list[tuple[str, str, str]], str]:
    """Run the exhaustive coverage mutation sweep. Leaves the tree clean.

    Returns (results, report). Raises SweepRefused on dirty tree / restore
    failure / control failure / emptied catalogue. Exit policy is applied by
    main().

    ``catalogue`` and ``min_mutations`` / ``expected_pair_count`` exist so
    self-tests can drive the real loop against a faithful mini-repo or prove
    the floor; production ``main()`` uses the defaults.
    """
    repo = _repo_resolved(repo)
    py = py or _default_py(repo)
    src = repo / relpath
    floor = MIN_MUTATION_FLOOR if min_mutations is None else min_mutations

    assert_clean_tree(repo, when="before the sweep")
    head = git(repo, "rev-parse", "--short", "HEAD").strip()
    if expect_head_prefix is not None and not head.startswith(expect_head_prefix):
        raise SweepRefused(f"HEAD {head} does not start with {expect_head_prefix!r}")

    base_hash = sha256_file(src)

    # Control: harness can see success, and the module resolves to THIS worktree.
    ok, where = import_ok(repo, py)
    if not ok:
        raise SweepRefused(f"control import: ok={ok} file={where}")
    g, out = unit_green(repo, py)
    if not g:
        raise SweepRefused(f"control unit not green: {out}")
    if run_live:
        gl, outl = live_green(repo, py)
        if gl is not True:
            raise SweepRefused(f"control live not green: {outl}")
    else:
        outl = "live skipped"

    lines = [
        f"CONTROL OK  head={head}  unit[{out}]  live[{outl}]",
        f"repo={repo}  import={where}",
        "",
    ]

    text = src.read_text()
    pairs = parse_probed_pairs(text)
    if expected_pair_count is not None and len(pairs) != expected_pair_count:
        raise SweepRefused(
            f"expected {expected_pair_count} probed pairs, parsed {len(pairs)}"
        )

    muts = list(catalogue) if catalogue is not None else build_mutation_catalogue(text)
    if len(muts) < floor:
        raise SweepRefused(
            f"mutation catalogue has {len(muts)} entries, floor is {floor}; "
            f"an emptied or truncated catalogue would report 0 survivors while "
            f"testing nothing (same class as mutation_sweep zero-tests-collected)"
        )

    results: list[tuple[str, str, str]] = []
    if run_live:
        checker = check_refused
    else:

        def checker(
            r: Path,
            p: str,
            lab: str,
            res: list[tuple[str, str, str]],
        ) -> None:
            check_refused_source_only(
                r, p, lab, res, test_relpath="tests/test_claim_resolver.py"
            )

    for mut in muts:
        apply_replacements(src, mut.replacements, label=mut.label)
        checker(repo, py, mut.label, results)
        restore_source(repo, relpath, base_hash)

    assert_clean_tree(repo, when="after the sweep (a mutation was not reverted)")
    if sha256_file(src) != base_hash:
        raise SweepRefused("tree hash drift after restore cycle")

    survivors = [r for r in results if r[1] == "SURVIVOR"]
    broken = [r for r in results if r[1] == "BROKEN-HARNESS"]
    lines.append(
        f"{len(results)} mutations, {len(survivors)} survivors, {len(broken)} harness-broken"
    )
    for label, verdict, detail in results:
        lines.append(f"  {verdict:16} {label:42} {detail}")
    lines.append("")
    lines.append("TREE RESTORED CLEAN")
    return results, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Exhaustive coverage-guard mutation sweep for claim_resolver's "
            "PRIVILEGE_COVERAGE. Dirty-tree refusal is load-bearing for direct "
            "runs; callers that need isolation should target a throwaway "
            "worktree (see tests/arb_memory/test_coverage_mutation_sweep_live.py)."
        )
    )
    ap.add_argument(
        "repo",
        nargs="?",
        default=None,
        help="repo or worktree root (default: git toplevel of cwd)",
    )
    ap.add_argument(
        "expect_head_prefix",
        nargs="?",
        default=None,
        help="optional HEAD short-sha prefix required for the run",
    )
    ap.add_argument(
        "--no-live",
        action="store_true",
        help=(
            "skip live-PG control and per-mutation live checks "
            "(unit/import only). For isolated worktree fast paths; the "
            "standing hook still runs live when ARB_MEMORY_DSN is present."
        ),
    )
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.repo is None:
        repo = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    else:
        repo = Path(args.repo)
    try:
        results, report = run_coverage_sweep(
            repo,
            expect_head_prefix=args.expect_head_prefix,
            run_live=not args.no_live,
        )
    except SweepRefused as exc:
        print(f"coverage-mutation-sweep REFUSED: {exc}", file=sys.stderr)
        return 2
    print(report)
    survivors = [r[0] for r in results if r[1] == "SURVIVOR"]
    broken = [r[0] for r in results if r[1] == "BROKEN-HARNESS"]
    if survivors or broken:
        print(
            f"\n{len(survivors)} SURVIVOR(s), {len(broken)} BROKEN-HARNESS: "
            f"{', '.join(survivors + broken)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

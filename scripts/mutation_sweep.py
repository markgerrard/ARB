#!/usr/bin/env python3
"""Inject/revert mutation sweep used by the changed-test gate.

Honest limit (r5 panel P1, unanimous, left open by owner ruling 2026-07-30;
residual accepted with this docs fix 2026-08-01): the sweep reads pass/fail
through a junit XML channel that code under test can itself write to. The
gate therefore demonstrates mutation behaviour for NON-ADVERSARIAL candidates
only — it is not evidence against a candidate that targets the result channel.
The structural fix (a result channel the candidate cannot write) was not
built; do not cite this gate's green as if it were that stronger claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


class SweepRefused(Exception):
    """The sweep cannot safely demonstrate a mutation."""


@dataclass
class RunResult:
    summary: str
    collected: int
    failed: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    returncode: int = 0


_COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped|deselected)")
_NO_TESTS_RE = re.compile(r"no tests ran", re.I)


def parse_pytest_output(out: str, returncode: int = 0, junit_path: Path | None = None) -> RunResult:
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    summary = next((line for line in reversed(lines)
                    if _COUNT_RE.search(line) or _NO_TESTS_RE.search(line)), "NO SUMMARY LINE")
    if junit_path is None:
        return RunResult(summary, 0, returncode=returncode)
    if not junit_path.exists():
        raise SweepRefused(f"JUNIT-ABSENT {junit_path}")
    try:
        root = ET.parse(junit_path).getroot()
    except ET.ParseError as exc:
        raise SweepRefused(f"JUNIT-PARSE-ERROR {junit_path}: {exc}") from exc
    groups = {key: set() for key in ("passed", "failed", "skipped")}
    for case in root.iter("testcase"):
        nodeid = _junit_nodeid(case.get("classname", ""), case.get("name", ""))
        child_tags = {child.tag for child in case}
        if child_tags & {"failure", "error"}:
            groups["failed"].add(nodeid)
        elif "skipped" in child_tags:
            groups["skipped"].add(nodeid)
        else:
            groups["passed"].add(nodeid)
    return RunResult(summary, sum(len(group) for group in groups.values()), sorted(groups["failed"]),
                     sorted(groups["passed"]), sorted(groups["skipped"]), returncode)


def _junit_nodeid(classname: str, name: str) -> str:
    """Reconstruct pytest's module::class::test node ID from junit attributes."""
    parts = classname.split(".") if classname else []
    class_index = next((i for i, part in enumerate(parts) if part[:1].isupper()), len(parts))
    module = "/".join(parts[:class_index])
    classes = "::".join(parts[class_index:])
    prefix = f"{module}.py" if module else ""
    return "::".join(part for part in (prefix, classes, name) if part)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode:
        raise SweepRefused(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def assert_clean_tree(repo: Path, *, when: str) -> None:
    dirty = git(repo, "status", "--porcelain").strip()
    if dirty:
        raise SweepRefused(f"working tree is dirty {when}; refusing.\n{dirty}\nCommit or stash first. There is no override.")


def apply_mutation(repo: Path, mut: dict) -> None:
    path = repo / mut["file"]
    original = path.read_text()
    if mut.get("regex"):
        replacement, count = re.subn(mut["find"], mut["replace"], original)
    else:
        count = original.count(mut["find"])
        replacement = original.replace(mut["find"], mut["replace"])
    if count != 1:
        raise SweepRefused(f"{mut['id']}: anchor matched {count} times in {mut['file']} (must be exactly 1)")
    if replacement == original:
        raise SweepRefused(f"{mut['id']}: matched its anchor but changed no bytes in {mut['file']}")
    path.write_text(replacement)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _restore(repo: Path, relative: str, expected: str) -> None:
    git(repo, "checkout", "--", relative)
    actual = _sha(repo / relative)
    if actual != expected:
        raise SweepRefused(f"RESTORE FAILED {relative}: expected {expected}, got {actual}")


def _check_import_resolution(repo: Path, spec: list[dict]) -> None:
    src = (repo / "src").resolve()
    if not src.exists():
        return
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src)
    for mutation in spec:
        target = (repo / mutation["file"]).resolve()
        if src not in target.parents:
            continue
        package = target.relative_to(src).parts[0]
        code = f"import importlib; m=importlib.import_module({package!r}); print(getattr(m, '__file__', ''))"
        result = subprocess.run([sys.executable, "-c", code], cwd=repo, env=env,
                                capture_output=True, text=True)
        imported = Path(result.stdout.strip()).resolve() if result.returncode == 0 and result.stdout.strip() else None
        if imported is None or src not in imported.parents:
            raise SweepRefused(f"IMPORT-RESOLUTION-MISMATCH {package}: {imported}")


def run_sweep(repo: Path, spec: list[dict], test_cmd: list[str]) -> tuple[list[dict], str]:
    repo = repo.resolve()
    assert_clean_tree(repo, when="before the sweep")
    _check_import_resolution(repo, spec)

    def run(*, baseline: bool = False) -> RunResult:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo / "src")
        descriptor, filename = tempfile.mkstemp(prefix="mutation-sweep-", suffix=".xml")
        os.close(descriptor)
        junit_path = Path(filename)
        command = list(test_cmd)
        if "pytest" in command:
            command.append(f"--junitxml={junit_path}")
        try:
            env["MUTATION_SWEEP_JUNIT"] = str(junit_path)
            result = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
            return parse_pytest_output(result.stdout + result.stderr, result.returncode, junit_path)
        finally:
            junit_path.unlink(missing_ok=True)

    baseline = run(baseline=True)
    if baseline.returncode != 0 or baseline.failed:
        raise SweepRefused(f"BASELINE-NOT-GREEN exit={baseline.returncode} failures={baseline.failed}")
    if baseline.collected == 0:
        raise SweepRefused(f"baseline collected 0 tests ({baseline.summary!r}); refusing")
    named = sorted({node for mutation in spec for node in mutation.get("expect_failed", [])})
    collected_ids = set(baseline.passed) | set(baseline.failed) | set(baseline.skipped)
    skipped = [node for node in named
               if node in baseline.skipped or node.split("::", 1)[0] in baseline.skipped]
    missing = [node for node in named if node not in collected_ids and node not in skipped]
    if missing:
        near = sorted(node for node in collected_ids
                      if any(node.rsplit("::", 1)[-1].split("[")[0] in wanted for wanted in missing))
        raise SweepRefused(f"NAMED-TEST-NOT-COLLECTED — {missing} match no collected node id; collected near-misses: {near}")
    if skipped:
        raise SweepRefused(f"NAMED-TEST-SKIPPED — {skipped} skipped at baseline; this environment cannot demonstrate fail-ability")
    if not baseline.passed:
        raise SweepRefused(
            "baseline passed 0 tests — all skipped or empty; "
            "this environment cannot demonstrate fail-ability"
        )
    not_passed = [node for node in named if node not in baseline.passed]
    if not_passed:
        raise SweepRefused(f"NAMED-TEST-NOT-COLLECTED — {not_passed} were not baseline-passed")

    results: list[dict] = []
    for mutation in spec:
        expected_hash = _sha(repo / mutation["file"])
        apply_mutation(repo, mutation)
        try:
            result = run()
        finally:
            _restore(repo, mutation["file"], expected_hash)
        if result.returncode not in (0, 1):
            raise SweepRefused(f"MUTATED-RUN-INVALID-EXIT exit={result.returncode} failures={result.failed}")
        if result.collected != baseline.collected:
            raise SweepRefused(f"{mutation['id']}: collected {result.collected} tests, baseline collected {baseline.collected}")
        expected = set(mutation.get("expect_failed", []))
        new_failed = set(result.failed) - set(baseline.failed)
        wrong = bool(expected) and not expected.issubset(new_failed)
        results.append({"id": mutation["id"], "label": mutation.get("label", mutation["id"]),
                        "summary": result.summary, "failed": result.failed,
                        "passed": result.passed, "skipped": result.skipped,
                        "returncode": result.returncode,
                        "binding": "expect_failed" if expected else "none",
                        "missing_expected": sorted(expected - new_failed),
                        "wrong_mechanism": wrong,
                        "survived": not result.failed})

    assert_clean_tree(repo, when="after the sweep")
    restored = run()
    lines = [f"BASELINE (no mutation): {baseline.summary}", ""]
    for result in results:
        lines.extend([f"{result['id']}  {result['label']}", f"    {result['summary']}"])
        lines.extend(f"    FAILED {node}" for node in result["failed"])
        if result["wrong_mechanism"]:
            lines.append(f"    WRONG-MECHANISM missing expected: {result['missing_expected']}")
        if result["survived"]:
            lines.append("    *** NO TEST FAILED - MUTATION SURVIVES ***")
        lines.append("")
    lines.extend([f"RESTORED BASELINE: {restored.summary}", "tree after sweep: CLEAN"])
    return results, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inject-revert mutation sweep.")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--test-cmd", required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    def unwind(signum, frame):
        raise KeyboardInterrupt(f"received signal {signum}")
    signal.signal(signal.SIGINT, unwind)
    signal.signal(signal.SIGTERM, unwind)
    try:
        spec = json.loads(args.spec.read_text())
        results, report = run_sweep(args.repo, spec, args.test_cmd.split())
    except SweepRefused as exc:
        print(f"mutation-sweep REFUSED: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt as exc:
        print(f"mutation-sweep INTERRUPTED: {exc}", file=sys.stderr)
        return 2
    print(report)
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n")
    survivors = [result["id"] for result in results if result["survived"]]
    if survivors:
        print(f"{len(survivors)} mutation(s) SURVIVED: {', '.join(survivors)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

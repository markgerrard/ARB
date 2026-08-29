#!/usr/bin/env python3
"""Validate declarations and sweep mutations for changed pytest files."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

SCOPED_TEST_TREES = ("tests", "tools/eval/tests", "bench/implbench/tests", "tools/authorbench/tests", "tools/faba/tests")
MAX_MUTATIONS_PER_RUN = 64
MUTATION_SWEEP_SHA256 = "6968a6ed4a9618f6a47de6454def3d0e7b33afae152218d5fda5bacbb3ab2b45"


@dataclass
class PairClosure:
    refused_surface: set[str] = field(default_factory=set)
    legal_candidates: set[str] = field(default_factory=set)
    collection_vital: bool = True
    probe: dict | None = None


@dataclass
class GateFail:
    reason: str


class CollectionError(Exception):
    pass


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _rel(path: Path, repo: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _scoped(path: Path, repo: Path) -> bool:
    return any(_under(path, repo / tree) for tree in SCOPED_TEST_TREES)


def _module_names(tree: ast.AST, source: Path, repo: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            package: list[str] = []
            if node.level:
                package = list(source.relative_to(repo).parent.parts)
                package = package[:1 - node.level] if node.level > 1 else package
            base = package + (node.module.split(".") if node.module else [])
            if node.module:
                names.append(".".join(base))
            names.extend(".".join(base + [alias.name]) for alias in node.names)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytest_plugins":
                    values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
                    names.extend(value.value for value in values if isinstance(value, ast.Constant) and isinstance(value.value, str))
    return names


def _resolve_module(name: str, repo: Path) -> Path | None:
    roots = [repo, repo / "src"] + [p for p in (repo / "tools").glob("*/") if p.is_dir()]
    parts = name.split(".")
    for root in roots:
        candidate = root.joinpath(*parts)
        if candidate.with_suffix(".py").is_file():
            return candidate.with_suffix(".py")
        if candidate.is_dir() and (candidate / "__init__.py").is_file():
            return candidate / "__init__.py"
    return None


def _conftests(test: Path, repo: Path) -> list[Path]:
    chain = []
    current = test.parent
    dirs = []
    while _under(current, repo):
        dirs.append(current)
        if current == repo:
            break
        current = current.parent
    for directory in reversed(dirs):
        candidate = directory / "conftest.py"
        if candidate.is_file():
            chain.append(candidate)
    return chain


def pair_closure(test_file: Path, repo: Path) -> PairClosure:
    test_file = test_file.resolve(); repo = repo.resolve()
    closure = PairClosure(refused_surface={_rel(test_file, repo)})
    queue = [test_file] + _conftests(test_file, repo)
    closure.refused_surface.update(_rel(path, repo) for path in queue)
    current = test_file.parent
    while _under(current, repo):
        if _scoped(current / "__init__.py", repo) and (current / "__init__.py").is_file():
            closure.refused_surface.add(_rel(current / "__init__.py", repo))
        if current == repo: break
        current = current.parent
    seen = set()
    while queue:
        source = queue.pop(0)
        if source in seen: continue
        seen.add(source)
        try: names = _module_names(ast.parse(source.read_text(), filename=str(source)), source, repo)
        except (OSError, SyntaxError): continue
        for name in names:
            target = _resolve_module(name, repo)
            if not target: continue
            relative = _rel(target, repo)
            if _scoped(target, repo):
                closure.refused_surface.add(relative)
                if target not in seen: queue.append(target)
            else:
                closure.legal_candidates.add(relative)
    closure.legal_candidates.difference_update(closure.refused_surface)
    closure.legal_candidates = {p for p in closure.legal_candidates
                                if not (_scoped(repo / p, repo) and (Path(p).name == "conftest.py" or Path(p).name == "__init__.py" or Path(p).name.startswith("test_")))}
    try:
        tree = ast.parse(test_file.read_text(), filename=str(test_file))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "PROBE_PROVENANCE":
                        closure.probe = ast.literal_eval(node.value)
    except (OSError, SyntaxError, ValueError):
        pass
    return closure


def _sidecar(path: Path) -> Path:
    return path.with_name(path.stem + ".mutations.json")


def load_allowlist(repo: Path, base: str) -> dict[str, dict]:
    result = subprocess.run(["git", "show", f"{base}:scripts/mutation_gate_exemptions.json"], cwd=repo,
                            capture_output=True, text=True)
    if result.returncode:
        return {}
    data = json.loads(result.stdout)
    return {entry["path"]: entry for entry in data.get("exemptions", [])}


def collection_vital(repo: Path, rel: str) -> bool:
    """A pair without a collected test cannot be a legal mutation target."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", rel, "-q", "--collect-only", "--no-header"],
        cwd=repo, env={**os.environ, "PYTHONPATH": str(repo / "src")}, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 5:
        return False
    raise CollectionError(f"COLLECTION-ERROR exit={result.returncode}")


def load_declaration(test_file: Path, repo: Path, closure: PairClosure, exemptions: dict[str, dict]):
    rel = _rel(test_file, repo)
    sidecar = _sidecar(test_file)
    if closure.probe is not None:
        marker = closure.probe
        if not isinstance(marker, dict) or not isinstance(marker.get("claim_id"), str) or not marker.get("claim_id") or not isinstance(marker.get("probe_artefact_id"), str) or not marker.get("probe_artefact_id") or not isinstance(marker.get("probe_artefact_version"), int) or marker["probe_artefact_version"] <= 0:
            return GateFail("DECL-INVALID — PROBE_PROVENANCE marker shape invalid")
    data = json.loads(sidecar.read_text()) if sidecar.exists() else None
    if data is not None and "no_legal_target" in data:
        return GateFail("DECL-INVALID — no_legal_target is candidate-authored; use the base-ref owner allowlist")
    entry = exemptions.get(rel)
    if entry:
        if closure.probe is not None:
            return GateFail("DECL-INVALID — PROBE_PROVENANCE pairs cannot use no-legal-target exemption")
        if closure.legal_candidates:
            return GateFail("DECL-INVALID — allowlisted no-legal-target, but legal candidates exist: " + ", ".join(sorted(closure.legal_candidates)))
        return entry
    if data is None:
        if closure.probe is not None:
            return GateFail("DECL-MISSING — PROBE_PROVENANCE requires the relanding convention sidecar (docs/probe-relanding-convention.md)")
        return GateFail("DECL-MISSING — no mutation sidecar")
    mutations = data.get("mutations")
    if data.get("schema") != 1 or not isinstance(mutations, list) or not mutations:
        return GateFail("DECL-INVALID — mutations must be a non-empty list")
    if data.get("schema") != 1:
        return GateFail("DECL-INVALID — schema must be 1")
    if any(m.get("kind") not in ("defect", "reinstate") for m in mutations):
        return GateFail("DECL-INVALID — kind must be defect or reinstate")
    if closure.probe is not None and not any(m.get("kind") == "reinstate" for m in mutations):
        return GateFail("DECL-INVALID — PROBE_PROVENANCE requires at least one reinstate mutation")
    for mutation in mutations:
        ids = mutation.get("expect_failed")
        if not isinstance(ids, list) or not ids:
            return GateFail(f"DECL-INVALID — {mutation.get('id', '?')} expect_failed must be non-empty")
        if any(node.split("::", 1)[0] != rel for node in ids):
            return GateFail(f"DECL-INVALID — expect_failed ids must be inside paired file {rel}")
        target = _rel((repo / mutation.get("file", "")).resolve(), repo)
        if target in closure.refused_surface:
            return GateFail(f"DECL-INVALID — mutation target is on refused import surface: {target}")
    return data


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def changed_pairs(repo: Path, base: str):
    mb = _git(repo, "merge-base", "HEAD", base)
    if mb.returncode:
        return None, GateFail(f"FAIL[scope]: base cannot be resolved: {base}")
    merge_base = mb.stdout.strip(); head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if merge_base == head:
        return None, GateFail("FAIL[scope]: base resolves to HEAD — wrong invocation or nothing to gate")
    diff = _git(repo, "diff", "--name-status", "-M", f"{merge_base}...HEAD")
    changed = {}
    for line in diff.stdout.splitlines():
        parts = line.split("\t")
        if not parts: continue
        status = parts[0][0]
        path = parts[-1]
        changed[path] = (status, parts[1] if status == "R" and len(parts) > 2 else None)
    pairs = set()
    for path, (status, old) in changed.items():
        candidate = repo / path
        if candidate.suffix == ".py" and candidate.name.startswith("test_") and any(_under(candidate, repo / tree) for tree in SCOPED_TEST_TREES):
            pairs.add(path)
        if path.endswith(".mutations.json"):
            pairs.add(path.removesuffix(".mutations.json") + ".py")
    return (merge_base, head, changed, sorted(pairs)), None


def _ast_equal_to_base(repo: Path, path: str, merge_base: str, old_path: str | None = None) -> bool:
    current = repo / path
    if not current.exists():
        return False
    source = _git(repo, "show", f"{merge_base}:{old_path or path}")
    if source.returncode:
        return False
    try:
        return ast.dump(ast.parse(source.stdout)) == ast.dump(ast.parse(current.read_text()))
    except SyntaxError:
        return False


def _tree_status(repo: Path):
    result = _git(repo, "status", "--porcelain")
    tracked, untracked = [], []
    for line in result.stdout.splitlines():
        if line.startswith("??"): untracked.append(line[3:])
        elif line.strip(): tracked.append(line[3:] if len(line) > 3 else line)
    return tracked, untracked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--json", type=Path)
    parser.add_argument("--repin", action="store_true")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top.returncode or Path(top.stdout.strip()).resolve() != repo:
        print("FAIL[scope]: --repo must be the git toplevel", file=sys.stderr); return 2
    def unwind(signum, frame): raise KeyboardInterrupt(f"received signal {signum}")
    signal.signal(signal.SIGINT, unwind); signal.signal(signal.SIGTERM, unwind)
    if args.repin:
        digest = hashlib.sha256((repo / "scripts/changed_test_mutation_gate.py").read_bytes()).hexdigest()
        pin_path = repo / "scripts/changed_test_mutation_gate.py.sha256"
        with pin_path.open("a") as stream:
            stream.write(f"{datetime.now(timezone.utc).isoformat()} {digest}\n")
        print(f"repinned {digest}")
        return 0
    pair_data, failure = changed_pairs(repo, args.base)
    checks = []
    summary = {"checks": checks, "environment": {"interpreter": sys.executable, "ARB_MEMORY_DSN": bool(os.environ.get("ARB_MEMORY_DSN"))}, "pairs": []}
    if failure:
        checks.append({"id": "scope", "line": failure.reason}); print(failure.reason); return 1
    merge_base, head, changed, pairs = pair_data
    actual_pin = hashlib.sha256((repo / "scripts/mutation_sweep.py").read_bytes()).hexdigest()
    if MUTATION_SWEEP_SHA256 != actual_pin:
        line = f"FAIL[sweep-pin]: mutation_sweep.py digest mismatch expected={MUTATION_SWEEP_SHA256} actual={actual_pin}"
        checks.append({"id": "sweep-pin", "line": line}); print(line); return 1
    checks.append({"id": "scope", "line": f"PASS[scope]: base={merge_base} head={head} changed_files={len(changed)} test_pairs={len(pairs)} exempt=0"})
    allow = load_allowlist(repo, merge_base)
    total = 0
    work = importlib.util.spec_from_file_location("mutation_sweep", repo / "scripts/mutation_sweep.py")
    sweep = importlib.util.module_from_spec(work)
    sys.modules[work.name] = sweep
    work.loader.exec_module(sweep)
    for rel in pairs:
        path = repo / rel
        status, old_path = changed.get(rel, (None, None))
        sidecar_path = _sidecar(path).relative_to(repo).as_posix()
        sidecar_changed = sidecar_path in changed
        if not path.exists():
            line = f"PASS[pair:{rel}]: EXEMPT-DELETED"; checks.append({"id": f"pair:{rel}", "line": line}); continue
        if status == "M" and not sidecar_changed and _ast_equal_to_base(repo, rel, merge_base, old_path):
            line = f"PASS[pair:{rel}]: EXEMPT-AST — comment/whitespace-only change"
            checks.append({"id": f"pair:{rel}", "line": line}); continue
        closure = pair_closure(path, repo)
        try:
            closure.collection_vital = collection_vital(repo, rel)
        except CollectionError as exc:
            line = f"FAIL[pair:{rel}]: {exc}"; checks.append({"id": f"pair:{rel}", "line": line}); continue
        if not closure.collection_vital:
            closure.legal_candidates.clear()
        declaration = load_declaration(path, repo, closure, allow)
        if isinstance(declaration, GateFail):
            line = f"FAIL[pair:{rel}]: {declaration.reason}"; checks.append({"id": f"pair:{rel}", "line": line}); continue
        if "reason" in declaration and "mutations" not in declaration:
            line = f"PASS[pair:{rel}]: EXEMPT-NO-LEGAL-TARGET ({declaration['reason']}) — {declaration['subject']}"; checks.append({"id": f"pair:{rel}", "line": line}); continue
        mutations = declaration["mutations"]; total += len(mutations)
        summary["pairs"].append({"path": rel, "refused_surface": sorted(closure.refused_surface), "legal_candidates": sorted(closure.legal_candidates), "collection_vital": closure.collection_vital, "duration": 0.0})
    if total > MAX_MUTATIONS_PER_RUN:
        line = f"FAIL[budget]: declared={total} ceiling={MAX_MUTATIONS_PER_RUN} — split the change"; checks.append({"id": "budget", "line": line})
    else:
        for item in summary["pairs"]:
            rel = item["path"]; path = repo / rel; data = load_declaration(path, repo, pair_closure(path, repo), allow)
            start = time.monotonic()
            try:
                results, _ = sweep.run_sweep(repo, data["mutations"], [sys.executable, "-m", "pytest", rel, "-q", "-rA", "--no-header"])
                if any(r["survived"] for r in results):
                    line = f"FAIL[pair:{rel}]: SURVIVOR"
                elif any(r["wrong_mechanism"] for r in results):
                    line = f"FAIL[pair:{rel}]: WRONG-MECHANISM"
                else:
                    line = f"PASS[pair:{rel}]: {len(results)} mutations, all RED via declared tests"
            except (sweep.SweepRefused, KeyboardInterrupt) as exc:
                line = f"FAIL[pair:{rel}]: SWEEP-REFUSED — {exc}"
            item["duration"] = round(time.monotonic() - start, 4); checks.append({"id": f"pair:{rel}", "line": line})
            tracked, untracked = _tree_status(repo)
            if tracked or untracked:
                if tracked: checks.append({"id": "tree", "line": f"FAIL[tree]: DIRTY-AFTER-SWEEP — {tracked}"})
                if untracked: checks.append({"id": "tree-untracked", "line": f"FAIL[tree]: UNTRACKED-RESIDUE — {untracked} — preview with: git clean -n"})
                break
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2) + "\n")
    for check in checks: print(check["line"])
    return 1 if any(check["line"].startswith("FAIL") for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path

from tests.e2e import spine


ROOT = Path(__file__).resolve().parents[2]


def _load(modname: str, relpath: str):
    spec = importlib.util.spec_from_file_location(modname, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def run_case(case: dict, tmp: Path) -> dict:
    # Fail-closed hermeticity (review P2): run_case writes via shadow_log_path(), which falls back to
    # the REAL ~/.local/state/arb/h2-shadow-log.jsonl when ARB_H2_SHADOW_LOG is unset. Every legit
    # caller (the _hermetic fixture, runner.main, _mutate) redirects it; refuse rather than silently
    # pollute production if a future caller forgets. Don't rely on the caller remembering.
    if not os.environ.get("ARB_H2_SHADOW_LOG"):
        raise RuntimeError(
            "run_case requires ARB_H2_SHADOW_LOG redirected to a hermetic path "
            "(refusing to write the default production shadow log); use the e2e _hermetic fixture, "
            "runner.main(), or tests.e2e._mutate"
        )
    gate = _load("bridge_protocol_gate", "skills/bridge-protocol/gate/gate.py")
    coll = _load("h2_collector", "skills/bridge-protocol/gate/h2_collector.py")

    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for rel, content in case["files"].items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    result = gate.h2_standing_check(case["phase_input"], repo, case["changed_paths"], case["diff"])
    log_path = coll.shadow_log_path()
    coll.append_record(result["record"], log_path=log_path)
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    record_payload = records[-1] if records else None
    read_evidence = _read_evidence(repo, case["changed_paths"], record_payload.get("derived", []) if record_payload else [])
    return {
        "status": result["status"],
        "reason": result["reason"],
        "notice": result["notice"],
        "record_payload": record_payload,
        "log_records": records,
        "log_path": str(log_path),
        "read_evidence": read_evidence,
    }


def assert_real_boundary_symbols() -> None:
    gate = _load("bridge_protocol_gate", "skills/bridge-protocol/gate/gate.py")
    coll = _load("h2_collector", "skills/bridge-protocol/gate/h2_collector.py")
    grad = importlib.import_module("skills.defect_hunts.h2_graduation")
    checks = [
        (gate.h2_standing_check, "bridge_protocol_gate", ROOT / "skills/bridge-protocol/gate/gate.py"),
        (gate._h2_candidate_files, "bridge_protocol_gate", ROOT / "skills/bridge-protocol/gate/gate.py"),
        (coll.append_record, "h2_collector", ROOT / "skills/bridge-protocol/gate/h2_collector.py"),
        (coll.shadow_log_path, "h2_collector", ROOT / "skills/bridge-protocol/gate/h2_collector.py"),
        (
            grad.is_graduation_ready,
            "skills.defect_hunts.h2_graduation",
            ROOT / "skills/defect_hunts/h2_graduation.py",
        ),
    ]
    for func, module_name, real_path in checks:
        spine.assert_real_symbol(func, module_name=module_name, real_path=real_path)


def assert_case_expected(case: dict, out: dict) -> None:
    expected = case["expected"]
    record = out["record_payload"]
    assert out["status"] == expected["status"]
    assert record["derived"] == expected["derived"]
    assert record["dispositions"] == expected["record"]["dispositions"]
    assert record["complete"] is expected["record"]["complete"]
    # Per-case graduation counts are THIS record's contribution (spec §6.2), not the whole
    # accumulated shadow log. The log is append-only and shared across a run_suite() call, so
    # counting out["log_records"] would sum foreign records once a second complete case exists
    # (GLM/codex review P1). Count the single current record.
    counts = _disposition_counts([record], complete_only=True)
    assert counts == expected["graduation"]["disposition_counts"]
    assert (record["complete"] is True) is expected["graduation"]["counted"]
    for derived_id in expected["derived"]:
        rel = _relpath_from_candidate_id(derived_id)
        assert rel in out["read_evidence"]
        assert derived_id in out["read_evidence"][rel]["explains"]


def _disposition_counts(records: list[dict], *, complete_only: bool) -> dict[str, int]:
    counts = {"answered": 0, "not_load_bearing": 0, "flag": 0}
    for record in records:
        if complete_only and record.get("complete") is not True:
            continue
        for row in record.get("dispositions", []):
            disposition = row.get("disposition")
            if disposition in counts:
                counts[disposition] += 1
    return counts


def _read_evidence(repo: Path, changed_paths: list[str], derived: list[str]) -> dict[str, dict]:
    evidence: dict[str, dict] = {}
    for rel in changed_paths:
        path = repo / rel
        if not path.exists() or path.suffix != ".py":
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        explained = [candidate_id for candidate_id in derived if _candidate_explained(candidate_id, rel, text)]
        evidence[rel] = {"sha256": hashlib.sha256(raw).hexdigest(), "explains": explained}
    return evidence


def _candidate_explained(candidate_id: str, rel: str, source: str) -> bool:
    candidate_rel = _relpath_from_candidate_id(candidate_id)
    if candidate_rel != rel:
        return False
    callee = _callee_from_candidate_id(candidate_id)
    if callee is None:
        return False
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError:
        return False
    return any(_dotted_callee(node.func) == callee for node in ast.walk(tree) if isinstance(node, ast.Call))


def _relpath_from_candidate_id(candidate_id: str) -> str:
    try:
        _kind, candidate_rel, callee_occurrence = candidate_id.split(":", 2)
    except ValueError:
        return ""
    return candidate_rel


def _callee_from_candidate_id(candidate_id: str) -> str | None:
    try:
        _kind, _candidate_rel, callee_occurrence = candidate_id.split(":", 2)
        callee, _occurrence = callee_occurrence.rsplit("#", 1)
    except ValueError:
        return None
    return callee


def _dotted_callee(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_callee(node.value)
        if prefix is None:
            return None
        return f"{prefix}.{node.attr}"
    return None

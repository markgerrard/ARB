"""Executable gate for the bridge-protocol skill.

The schema validator is intentionally small and stdlib-only.  It validates the
bounded paths the evaluator reads, then the evaluator stays fail-closed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


BLOCK_OPEN_FINDING = "open-finding"
BLOCK_HARD_SIGNAL = "hard-signal"
BLOCK_CHEAP_FAKE = "cheap-fake"
BLOCK_MISSING_DIMENSION = "missing-required-dimension"
BLOCK_UNCLASSIFIED = "unclassified"
BLOCK_DECLARATIVE_VERIFIED = "declarative-verified"
BLOCK_CIRCULAR_VALIDATOR = "circular-validator-dependency"
BLOCK_STALE_BASIS = "stale-correctness-basis"
BLOCK_SELF_CERT = "self-certification"
BLOCK_BUILDER_DECISION = "builder-supplied-decision"
BLOCK_ESCAPED_DEFECT = "escaped-defect-open"
BLOCK_STALE_ROOT = "stale-trust-root"
BLOCK_SETUP = "setup-error"
BLOCK_H1_STANDING_CHECK = "h1-standing-check"
BLOCK_H2_STANDING_CHECK = "h2-standing-check"
H1_STANDING_CHECK_SCOPE = "same-directory .py siblings only; cross-package same-named literals are not scanned"
H2_MODE = "shadow"
H2_UNANSWERED_NOTICE = "H2 derived candidates were not disposed in h2_section"
H2_UNACKNOWLEDGED_NOTICE = "H2 coverage acknowledgment is missing or false"

SCHEMA_NAMES = (
    "phase_input",
    "gate_result",
    "trust_root",
    "trust_root_rotation",
    "layer_registry",
    "load_bearing_components",
    "validator_readiness",
    "stage_modes",
)

PHASE_INPUT_REQUIRED = {
    "phase",
    "phase_class",
    "artifact_sha",
    "correctness_basis",
    "reviewer_reports",
    "manifest_ref",
    "escaped_defect",
}
PHASE_INPUT_FORBIDDEN = {"gate_decision", "block_reasons", "verified", "judged", "hard_signal"}

ENUMS = {
    "phase_class": {"executable", "declarative"},
    "correctness_basis": {
        "manual-panel",
        "diagnose",
        "diagnose-steer",
        "external-base-case",
        "hard-signal",
    },
}


class GateError(Exception):
    """Raised for invalid gate inputs or setup errors."""


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def schema_dir(repo: str | Path) -> Path:
    return Path(repo) / "skills" / "bridge-protocol" / "gate" / "schemas"


def load_schema(repo: str | Path, name: str) -> dict[str, Any]:
    if name not in SCHEMA_NAMES:
        raise GateError(f"unknown schema: {name}")
    path = schema_dir(repo) / f"{name}.json"
    if not path.exists():
        path = Path(__file__).resolve().parent / "schemas" / f"{name}.json"
    doc = read_json(path)
    validate_schema_doc(doc, name)
    return doc


def validate_schema_doc(doc: dict[str, Any], name: str) -> None:
    required = {"name", "required", "type"}
    missing = required - set(doc)
    if missing:
        raise GateError(f"schema {name} missing {sorted(missing)}")
    if doc["name"] != name:
        raise GateError(f"schema {name} has mismatched name {doc['name']!r}")


def validate_doc(doc: dict[str, Any], schema: dict[str, Any]) -> None:
    name = schema["name"]
    if name == "phase_input":
        _validate_phase_input(doc)
    elif name == "gate_result":
        _require(doc, {"gate_decision", "block_reasons", "derived_head", "verified", "judged"}, name)
    elif name == "layer_registry":
        _require(doc, {"excluded_roots", "rules"}, name)
    elif name == "load_bearing_components":
        if not isinstance(doc, list):
            raise GateError("load_bearing_components must be a list")
    elif name == "validator_readiness":
        _require(doc, {"validators"}, name)
    elif name == "stage_modes":
        _require(doc, {"stages"}, name)
    elif name == "trust_root":
        _require(doc, {"certified_object_sha", "certifying_seats", "human_approver", "judged_not_verified"}, name)
    elif name == "trust_root_rotation":
        _require(
            doc,
            {
                "old_sha",
                "new_sha",
                "reason",
                "certifying_seats",
                "human_approver",
                "change_author",
                "invalidated_basis_records",
            },
            name,
        )


def _validate_phase_input(doc: dict[str, Any]) -> None:
    _require(doc, PHASE_INPUT_REQUIRED, "phase_input")
    forbidden = PHASE_INPUT_FORBIDDEN & set(doc)
    if forbidden:
        raise GateError(f"phase_input has gate-produced/forbidden fields: {sorted(forbidden)}")
    for key, allowed in ENUMS.items():
        if doc.get(key) not in allowed:
            raise GateError(f"{key} must be one of {sorted(allowed)}")
    if not isinstance(doc["reviewer_reports"], list):
        raise GateError("reviewer_reports must be a list")
    for report in doc["reviewer_reports"]:
        _require(report, {"seat", "verdict", "findings"}, "reviewer_report")
        if not isinstance(report["findings"], list):
            raise GateError("findings must be a list")
        for finding in report["findings"]:
            _require(finding, {"id", "severity", "file_line", "fix", "status"}, "finding")
    escaped = doc["escaped_defect"]
    _require(escaped, {"triggered", "changelog", "corpus_row", "standing_rule", "state"}, "escaped_defect")
    if escaped["state"] not in {"open", "fixed", "invalidated"}:
        raise GateError("escaped_defect.state invalid")
    evidence = doc.get("hard_signal_evidence")
    if evidence is not None:
        _require(
            evidence,
            {
                "command",
                "cwd",
                "test_count",
                "check_id",
                "commit_sha",
                "captured_output_path",
                "tree_hash",
                "captured_output_sha256",
                "exit_code",
                "start_time",
                "end_time",
                "runner_id",
            },
            "hard_signal_evidence",
        )


def _require(doc: dict[str, Any], required: set[str], name: str) -> None:
    missing = required - set(doc)
    if missing:
        raise GateError(f"{name} missing {sorted(missing)}")


def load_phase_input(repo: str | Path, path: str | Path) -> dict[str, Any]:
    doc = read_json(path)
    validate_doc(doc, load_schema(repo, "phase_input"))
    return doc


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git(repo: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_head(repo: str | Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def git_tree_hash(repo: str | Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", f"{ref}^{{tree}}").stdout.strip()


def is_clean_tree(repo: str | Path) -> bool:
    return git(repo, "status", "--porcelain").stdout == ""


def derive_ground_truth(repo: str | Path) -> dict[str, Any]:
    return {
        "head": git_head(repo),
        "tree_hash": git_tree_hash(repo),
        "clean_tree": is_clean_tree(repo),
    }


def hard_signal_blocks(repo: str | Path, evidence: dict[str, Any] | None) -> list[str]:
    if not evidence:
        return [BLOCK_HARD_SIGNAL]
    truth = derive_ground_truth(repo)
    blocks: list[str] = []
    if evidence.get("commit_sha") != truth["head"]:
        blocks.append(BLOCK_HARD_SIGNAL)
    if evidence.get("tree_hash") != truth["tree_hash"]:
        blocks.append(BLOCK_HARD_SIGNAL)
    path = Path(evidence.get("captured_output_path", ""))
    if not path.is_absolute():
        path = Path(repo) / path
    if not path.exists() or sha256_file(path) != evidence.get("captured_output_sha256"):
        blocks.append(BLOCK_HARD_SIGNAL)
    if evidence.get("exit_code") != 0 or int(evidence.get("test_count", 0)) <= 0:
        blocks.append(BLOCK_HARD_SIGNAL)
    if not truth["clean_tree"]:
        blocks.append(BLOCK_HARD_SIGNAL)
    return sorted(set(blocks))


def posix_rel(path: str | Path) -> str:
    return PurePosixPath(str(path).replace(os.sep, "/")).as_posix().lstrip("./")


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def validate_layer_registry(registry: dict[str, Any]) -> None:
    _require(registry, {"excluded_roots", "rules"}, "layer_registry")
    if not isinstance(registry["rules"], list) or not registry["rules"]:
        raise GateError("layer_registry has no rules")
    if not any(rule.get("pattern") == "**" for rule in registry["rules"]):
        raise GateError("layer_registry missing catch-all")
    for rule in registry["rules"]:
        _require(rule, {"pattern", "layer", "required_dimensions", "owner"}, "layer_rule")


def classify_path(path: str | Path, registry: dict[str, Any]) -> dict[str, Any]:
    validate_layer_registry(registry)
    rel = posix_rel(path)
    if matches_any(rel, registry.get("excluded_roots", [])):
        return {"path": rel, "production": False, "layer": None, "required_dimensions": []}
    matches = [rule for rule in registry["rules"] if fnmatch.fnmatch(rel, rule["pattern"])]
    if not matches:
        raise GateError(f"{BLOCK_UNCLASSIFIED}: {rel}")
    # Most specific = longest non-wildcard prefix; catch-all loses naturally.
    matches.sort(key=lambda rule: len(rule["pattern"].replace("*", "")), reverse=True)
    rule = matches[0]
    if rule["layer"] == "unclassified":
        raise GateError(f"{BLOCK_UNCLASSIFIED}: {rel}")
    return {
        "path": rel,
        "production": True,
        "layer": rule["layer"],
        "required_dimensions": list(rule.get("required_dimensions", [])),
        "owner": rule["owner"],
    }


def classify_changes(paths: list[str], registry: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    classified: list[dict[str, Any]] = []
    blocks: list[str] = []
    for path in paths:
        try:
            classified.append(classify_path(path, registry))
        except GateError:
            blocks.append(BLOCK_UNCLASSIFIED)
    return classified, sorted(set(blocks))


def changed_files(repo: str | Path, target_branch: str) -> list[str]:
    base = git(repo, "merge-base", target_branch, "HEAD", check=False)
    if base.returncode != 0 or not base.stdout.strip():
        raise GateError(f"cannot resolve target branch {target_branch!r}")
    base_ref = base.stdout.strip()
    diff = git(repo, "diff", "--name-only", "--diff-filter=ACMR", f"{base_ref}...HEAD", check=False)
    if diff.returncode != 0:
        raise GateError(f"cannot diff against target branch {target_branch!r}")
    return [line for line in diff.stdout.splitlines() if line]


def scope_changed_paths_for_phase(paths: list[str], phase: str) -> list[str]:
    if phase == "bridge-protocol-root":
        return [path for path in paths if path.startswith("skills/bridge-protocol/")]
    return paths


def git_review_diff(repo: str | Path, target_branch: str) -> str:
    base = git(repo, "merge-base", target_branch, "HEAD", check=False)
    if base.returncode != 0 or not base.stdout.strip():
        raise GateError(f"cannot resolve target branch {target_branch!r}")
    base_ref = base.stdout.strip()
    diff = git(repo, "diff", f"{base_ref}...HEAD", check=False)
    if diff.returncode != 0:
        raise GateError(f"cannot diff against target branch {target_branch!r}")
    return diff.stdout


def h1_standing_check(repo: str | Path, changed_paths: list[str], diff: str) -> list[dict[str, Any]]:
    from skills.defect_hunts.h1_config_drift import hunt
    from skills.defect_hunts.types import Scenario

    scenario = Scenario(
        scenario_id="bridge-protocol-gate-h1",
        files=_h1_scenario_files(Path(repo), changed_paths),
        diff=diff,
    )
    verdict = hunt(scenario)
    findings: list[dict[str, Any]] = []
    for finding in verdict.findings:
        if finding.kind != "H1" or finding.decision != "FLAG":
            continue
        findings.append(
            {
                "id": "H1-config-drift",
                "severity": "P1",
                "file_line": finding.subject,
                "fix": f"Run H1 config-drift follow-up for {finding.subject}",
                "status": "open",
                "kind": finding.kind,
                "evidence": finding.evidence,
                "scope": H1_STANDING_CHECK_SCOPE,
            }
        )
    return findings


def _h1_scenario_files(repo: Path, changed_paths: list[str]) -> dict[str, str]:
    paths: set[Path] = set()
    for changed in changed_paths:
        path = repo / changed
        if path.suffix != ".py":
            continue
        paths.add(path)
        if path.parent.exists():
            paths.update(candidate for candidate in path.parent.glob("*.py") if candidate.is_file())

    files: dict[str, str] = {}
    for path in sorted(paths):
        try:
            files[path.relative_to(repo).as_posix()] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return files


def h2_standing_check(
    phase_input: dict[str, Any],
    repo: str | Path,
    changed_paths: list[str],
    diff: str,
) -> dict[str, Any]:
    from skills.defect_hunts.h2_assumptions import H2Record, is_complete, validate_h2_section
    from skills.defect_hunts.h2_derive import derive

    repo_path = Path(repo)
    candidates = derive(_h2_candidate_files(repo_path, changed_paths), diff)
    derived_ids = [candidate.id for candidate in candidates]
    section = _h2_section(phase_input)
    rows = _h2_rows(section)
    coverage_acknowledged = _h2_coverage_acknowledged(section)
    dispositions = _h2_record_dispositions(rows, valid=False)
    complete = False
    reason = None

    if section is _UNSET:
        status = "shadow" if derived_ids else "enforced"
        notice = H2_UNANSWERED_NOTICE if derived_ids and H2_MODE == "shadow" else None
    else:
        valid, validation_reason = validate_h2_section(section, repo_root=repo_path)
        reason = None if valid else validation_reason
        dispositions = _h2_record_dispositions(rows, valid=valid)
        complete = is_complete(derived_ids, section, repo_root=repo_path) if valid else False
        has_flag = valid and _h2_has_flag(section)
        rows_match_derived = valid and _h2_rows_reference_only_derived(rows, derived_ids)

        if has_flag:
            status = "flagged"
            notice = None
        elif not valid or not rows_match_derived:
            status = "shadow"
            reason = reason or "H2 section references unknown derived candidate"
            notice = H2_UNANSWERED_NOTICE if H2_MODE == "shadow" else None
        elif not coverage_acknowledged:
            status = "static-only-unacknowledged"
            notice = H2_UNACKNOWLEDGED_NOTICE if H2_MODE == "shadow" else None
        elif complete:
            status = "enforced"
            notice = None
        else:
            status = "shadow"
            notice = H2_UNANSWERED_NOTICE if H2_MODE == "shadow" else None

    record = H2Record(
        run_id=str(phase_input.get("artifact_sha") or phase_input.get("phase") or ""),
        h2_mode=H2_MODE,
        derived=derived_ids,
        dispositions=dispositions,
        coverage_acknowledged=coverage_acknowledged,
        complete=complete,
    )
    return {"status": status, "reason": reason, "notice": notice, "record": record}


def _h2_candidate_files(repo: Path, changed_paths: list[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for changed in changed_paths:
        path = repo / changed
        if path.suffix != ".py":
            continue
        try:
            files[path.relative_to(repo).as_posix()] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return files


def _h2_rows(section: Any) -> list[dict[str, Any]]:
    if not isinstance(section, dict):
        return []
    rows = section.get("rows")
    return [dict(row) for row in rows] if isinstance(rows, list) and all(isinstance(row, dict) for row in rows) else []


def _h2_coverage_acknowledged(section: Any) -> bool:
    if not isinstance(section, dict):
        return False
    coverage = section.get("coverage_acknowledgment")
    return isinstance(coverage, dict) and coverage.get("acknowledged") is True


def _h2_record_dispositions(rows: list[dict[str, Any]], *, valid: bool) -> list[dict[str, Any]]:
    dispositions: list[dict[str, Any]] = []
    for row in rows:
        dispositions.append(
            {
                "candidate_id": row.get("candidate_id"),
                "disposition": row.get("disposition"),
                "valid": valid,
            }
        )
    return dispositions


def _h2_has_flag(section: Any) -> bool:
    if not isinstance(section, dict):
        return False
    rows = list(section.get("rows", []))
    coverage = section.get("coverage_acknowledgment")
    if isinstance(coverage, dict):
        rows.extend(coverage.get("additional_assumptions", []))
    return any(isinstance(row, dict) and row.get("disposition") == "flag" for row in rows)


def _h2_rows_reference_only_derived(rows: list[dict[str, Any]], derived_ids: list[str]) -> bool:
    derived = set(derived_ids)
    return all(row.get("candidate_id") in derived for row in rows)


def _h2_section(phase_input: dict[str, Any]) -> Any:
    if "h2_section" in phase_input:
        return phase_input["h2_section"]
    review_brief = phase_input.get("review_brief")
    if isinstance(review_brief, dict) and "h2_section" in review_brief:
        return review_brief["h2_section"]
    return _UNSET


def _component_matches(entry: dict[str, Any], classified: dict[str, Any]) -> bool:
    component = entry.get("component", "")
    path = classified["path"]
    return component == path or fnmatch.fnmatch(path, component)


def _finding_resolves(reviewer_reports: list[dict[str, Any]], reviewer: str, finding_id: str) -> bool:
    for report in reviewer_reports:
        if report.get("seat") != reviewer:
            continue
        for finding in report.get("findings", []):
            if finding.get("id") == finding_id and finding.get("status") == "resolved":
                return True
    return False


def manifest_blocks(
    classified: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
    reviewer_reports: list[dict[str, Any]],
) -> list[str]:
    blocks: list[str] = []
    for item in classified:
        if not item.get("production") or item.get("layer") == "other":
            continue
        entries = [entry for entry in manifest if _component_matches(entry, item)]
        if not entries:
            blocks.append(BLOCK_CHEAP_FAKE)
            continue
        entry = entries[0]
        costly = set(entry.get("costly_dimensions", []))
        tests = entry.get("dimension_preserving_tests", {})
        evidence = entry.get("dimension_evidence", {})
        for dimension in costly:
            if dimension not in tests or dimension not in evidence:
                blocks.append(BLOCK_CHEAP_FAKE)
        exclusions = entry.get("dimensions_considered_and_excluded", [])
        approved_excluded: set[str] = set()
        for exclusion in exclusions:
            if exclusion.get("waiver_status") == "approved":
                if _finding_resolves(
                    reviewer_reports,
                    exclusion.get("waiver_reviewer", ""),
                    exclusion.get("waiver_finding_id", ""),
                ):
                    approved_excluded.add(exclusion.get("dimension"))
                else:
                    blocks.append(BLOCK_MISSING_DIMENSION)
        for dimension in item.get("required_dimensions", []):
            if dimension not in costly and dimension not in approved_excluded:
                blocks.append(BLOCK_MISSING_DIMENSION)
    return sorted(set(blocks))


def brief_authorship_blocks(run_record: dict[str, Any], bus_records: list[dict[str, Any]]) -> list[str]:
    """Check brief recompute and submission/bus-record consistency.

    The submission check is not a forgery guarantee: bus_records are supplied
    by the caller, so this only catches non-adversarial mismatches between a
    submission's recorded reply hash and the supplied bus record list.
    """
    from skills._diagnose_common import seal
    from skills.diagnose.briefs import author_briefs, author_post_briefs

    blocks: list[str] = []
    for brief in run_record.get("sealed_briefs", []) + run_record.get("post_briefs", []):
        if brief.get("seal") != seal(brief.get("brief", {})):
            blocks.append("brief-tampered")

    constants = read_json(Path(__file__).resolve().parents[3] / "skills" / "diagnose" / "panel_constants.json")
    constants["_repo_root"] = run_record.get("repo_root", run_record.get("repo_sha", ""))
    recorded = run_record.get("recorded_traceback", {})
    recorded_traceback = {
        "reproduced": recorded.get("reproduced", False),
        "window": recorded.get("window", {"start": 0, "end": 0}),
        "traceback": "",
        "blocking": None,
    }
    try:
        expected_pre = author_briefs(
            run_record.get("trigger", {}).get("failing_test", ""),
            run_record.get("repo_sha", ""),
            recorded_traceback,
            constants,
        )
        expected_post = author_post_briefs(constants, run_record.get("submissions", []))
    except Exception:
        blocks.append("brief-not-skill-authored")
        expected_pre = []
        expected_post = []

    if expected_pre and _brief_bodies_by_role(expected_pre) != _brief_bodies_by_role(run_record.get("sealed_briefs", [])):
        blocks.append("brief-not-skill-authored")
    if expected_post and _brief_bodies_by_role(expected_post) != _brief_bodies_by_role(run_record.get("post_briefs", [])):
        blocks.append("brief-not-skill-authored")

    supplied_bus_hashes = set()
    for record in bus_records:
        if "sha256" in record:
            supplied_bus_hashes.add(record["sha256"])
        if "reply" in record:
            supplied_bus_hashes.add(hashlib.sha256(str(record["reply"]).encode("utf-8")).hexdigest())
    for submission in run_record.get("submissions", []):
        if submission.get("bus_reply_sha256") not in supplied_bus_hashes:
            blocks.append("submission-inconsistent")
    return sorted(set(blocks))


def _brief_bodies_by_role(briefs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {brief["role"]: brief.get("brief", {}) for brief in briefs}


BASIS_STRENGTH = {
    "manual-panel": 1,
    "diagnose": 2,
    "diagnose-steer": 2,
    "external-base-case": 3,
    "hard-signal": 4,
}


def validator_status(readiness: dict[str, Any], validator_id: str) -> dict[str, Any]:
    for validator in readiness.get("validators", []):
        if validator.get("validator_id") == validator_id:
            return validator
    return {"validator_id": validator_id, "status": "absent"}


def basis_available(readiness: dict[str, Any], stage_modes: dict[str, Any], head: str, stage: str) -> str:
    stage_cfg = stage_modes.get("stages", {}).get(stage, {"mode": "blind", "eligible_validator": "diagnose"})
    mode = stage_cfg.get("mode", "blind")
    if mode == "external":
        return "external-base-case"
    validator_id = "diagnose-steer" if mode == "steered" else "diagnose"
    validator_id = stage_cfg.get("eligible_validator", validator_id)
    status = validator_status(readiness, validator_id).get("status", "absent")
    if status == "verified":
        return validator_id
    return "manual-panel"


def basis_blocks(phase_input: dict[str, Any], readiness: dict[str, Any], stage_modes: dict[str, Any], head: str) -> list[str]:
    stage = phase_input.get("phase", "")
    current = phase_input.get("correctness_basis")
    available = basis_available(readiness, stage_modes, head, stage)
    blocks: list[str] = []
    if current in {"diagnose", "diagnose-steer"}:
        status = validator_status(readiness, current).get("status", "absent")
        if status == "invalidated":
            blocks.append(BLOCK_STALE_BASIS)
        elif status != "verified":
            blocks.append(BLOCK_CIRCULAR_VALIDATOR)
    if BASIS_STRENGTH.get(current, 0) < BASIS_STRENGTH.get(available, 0):
        blocks.append(BLOCK_STALE_BASIS)
    return sorted(set(blocks))


def logic_set_paths(repo: str | Path) -> list[Path]:
    base = Path(repo) / "skills" / "bridge-protocol"
    defect_hunts = Path(repo) / "skills" / "defect_hunts"
    # SKILL.md is deliberately NOT certified. It was until 2026-08-08, and the
    # consequence was that 7ee22909 -- five lines of YAML frontmatter fixing a
    # real silent capability loss -- invalidated the trust root and left this
    # repo's own dogfood test red for five days. Worse, it made the docs
    # unfixable without a rotation: correcting a sentence in SKILL.md moved the
    # hash, so honesty edits needed the full ceremony.
    #
    # gate/root_rules.md carries the rules that must stay certified (what the
    # certified object covers, who may author a root). Leaving THOSE uncertified
    # would be strictly worse than never splitting -- a worker could edit the
    # rule forbidding workers from editing the root. Raised by cold-opus in
    # panel-gaterotate-20260808T045453Z-4acada, verified in both directions by
    # all three seats of panel-gaterotate-r2-20260808T111742Z-fdbcb3.
    paths = [base / "gate" / "gate.py", base / "gate" / "root_rules.md"]
    paths.extend(sorted((base / "gate" / "schemas").glob("*")))
    paths.extend(
        [
            defect_hunts / "h2_assumptions.py",
            defect_hunts / "h2_derive.py",
            defect_hunts / "h2_graduation.py",
        ]
    )
    return sorted(paths, key=lambda p: p.relative_to(repo).as_posix())


def certified_object_sha(repo: str | Path) -> str:
    repo_path = Path(repo)
    h = hashlib.sha256()
    for path in logic_set_paths(repo_path):
        if not path.is_file():
            raise GateError(f"certified object missing {path}")
        rel = path.relative_to(repo_path).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def trust_root_blocks(repo: str | Path, trust_root: dict[str, Any] | None) -> list[str]:
    if not trust_root:
        return [BLOCK_STALE_ROOT]
    try:
        running = certified_object_sha(repo)
    except GateError:
        return [BLOCK_STALE_ROOT]
    if trust_root.get("certified_object_sha") != running:
        return [BLOCK_STALE_ROOT]
    return []


def self_cert_blocks(phase_input: dict[str, Any]) -> list[str]:
    skill = phase_input.get("skill_under_review") or phase_input.get("skill-under-review")
    if not skill:
        return []
    for report in phase_input.get("reviewer_reports", []):
        if report.get("seat") == skill:
            return [BLOCK_SELF_CERT]
    return []


def _string_list(value: Any) -> tuple[set[str], bool]:
    """Coerce a JSON field to a set of strings, reporting whether it was WELL
    FORMED. Returns (values, ok); callers must fail closed on ok=False.

    Exists because `set(value)` silently does the wrong thing on the wrong type
    and the wrong thing looks like success:

        set("fabricated-seat")  -> {'f','a','b','r','i','c','t','e','d','-','s'}

    a set of characters that cannot intersect any real seat id, so a
    disjointness check against it passes VACUOUSLY. And `set(None)` raises
    TypeError, which in a human-facing checklist is worse than a block -- it
    tells the reader nothing and looks like a tooling fault rather than a
    rejected record.

    Found by the muse seat on 2026-08-08, after five panel seats and the
    orchestrator had all read this function. The gate-path version of the same
    defect was sol's P0 in panel-gaterotate-r2-20260808T111742Z-fdbcb3; removing
    the wiring closed the gate path and left this one open, because
    rotation_blocks is still what a HUMAN runs while deciding whether to move a
    trust root.
    """
    if not isinstance(value, list):
        return set(), False
    if not all(isinstance(item, str) for item in value):
        return set(), False
    return set(value), True


_APPROVER_SENTINELS = frozenset(
    {"pending", "todo", "tbd", "tba", "none", "nobody", "n/a", "na",
     "placeholder", "unknown", "-", "--", "?"}
)

# Matched only when followed by a separator, never as a bare prefix: a rule that
# rejected anything starting with "todo" would reject an approver named Todor.
_APPROVER_SENTINEL_PREFIXES = ("pending", "todo", "tbd", "tba", "placeholder")
_APPROVER_SEPARATORS = frozenset(" -:_.,;/")


def _approver_is_placeholder(value: Any) -> bool:
    """True when `value` is EMPTY, NOT A STRING, or one of a fixed list of
    sentinels — nothing more.

    Read that scope literally. This rejects the obvious mistake: a draft record
    promoted with its approver field never filled in. It does NOT establish that
    a human approved anything, and no function in this repository can — the
    field is a string in a file writable by whoever is making the change, which
    is the same structural limit `rotation_blocks` documents at length.

    So the honest description is "rejects unfilled approver fields", not
    "verifies human approval". If this function's presence ever makes the
    checklist feel stronger than that, it is doing harm.

    History: the hole was found by round 1 of the rotation panel
    (`panel-gaterotate-20260808T045453Z-4acada`), which observed that
    `if not rotation.get("human_approver")` accepts the literal string
    "PENDING-MARK-COSIGN". A fix drafted in round 2 was blocked for unrelated
    reasons and never landed, so the finding stayed open across three rounds.
    Round 3's grok seat added that a whitespace-only value passes too, " "
    being truthy. Both are covered here.

    DELIBERATE NON-GOAL, decided 2026-08-14 after rotation rounds 4 and 5.
    This does NOT detect invisible or blank-rendering characters. A value of
    only U+200B, a combining mark, U+3164 HANGUL FILLER or U+2800 BRAILLE
    PATTERN BLANK passes, and that is a scope decision rather than an oversight.

    Why: the accident this exists to catch is a draft record promoted with the
    field never filled in, and that accident produces "" or a sentinel, not a
    Braille blank. Against a deliberate actor it is worthless either way — the
    field is a string in a file writable by whoever is making the change, so
    anyone faking approval types a name, which is easier than any invisible
    trick. An earlier revision filtered Unicode categories Cf and Cc and called
    the rule "visibility"; four panel seats then showed that Mn, Me, Lo and So
    all render blank too, so the prose was claiming more than the code did. The
    claim is narrowed here rather than the enumeration extended, because
    chasing categories has no end and buys nothing against either threat.
    """
    if not isinstance(value, str):
        return True
    normalised = value.strip().lower()
    if not normalised:
        return True
    if normalised in _APPROVER_SENTINELS:
        return True
    for prefix in _APPROVER_SENTINEL_PREFIXES:
        if (
            normalised.startswith(prefix)
            and len(normalised) > len(prefix)
            and normalised[len(prefix)] in _APPROVER_SEPARATORS
        ):
            return True
    return False


def rotation_blocks(
    current_root: dict[str, Any],
    rotation: dict[str, Any],
    new_sha: str,
    existing_basis_records: set[str] | None = None,
) -> list[str]:
    """Checklist for a HUMAN running a rotation. NOT called by evaluate().

    Read that twice, because the shape invites the opposite assumption and it
    already cost a round: on 2026-08-08 an orchestrator wired this into
    evaluate() believing that would give the rotation contract teeth, and
    panel-gaterotate-r2-20260808T111742Z-fdbcb3 blocked it with two P0s. The
    wiring did not make the contract enforceable; it added a second way to pass
    while leaving the first one open, and it consumed unvalidated JSON.

    The reason is structural, not a bug to be fixed by validating harder: EVERY
    input this gate reads -- trust_root.json, any rotation record, gate.py
    itself -- lives in the repository the change is being made to, and is
    therefore writable by whoever is making the change. A gate cannot
    authenticate changes to the tree it lives in. Validating the record more
    strictly only moves the forgery one field along.

    So this function is a checklist, and the authorisation it describes lives
    outside the repo: in the panel audit trail (append-only, reconcile-gated,
    in Postgres) and in a human. If you are tempted to call this from
    evaluate(), read test_rotation_blocks_is_deliberately_not_on_the_gate_path
    first -- it exists to make that temptation visible rather than silent.
    """
    existing_basis_records = existing_basis_records or set()
    blocks: list[str] = []
    if rotation.get("old_sha") != current_root.get("certified_object_sha"):
        blocks.append(BLOCK_STALE_ROOT)
    if rotation.get("new_sha") != new_sha:
        blocks.append(BLOCK_STALE_ROOT)

    # ABSENT is not MALFORMED. A bootstrap root with no recorded seats has
    # nothing to be disjoint from, so absence is tolerated; an explicit null or
    # a wrong type means the disjointness check cannot be evaluated at all, and
    # a check that cannot be evaluated must not report success.
    if "certifying_seats" in current_root:
        prior, prior_ok = _string_list(current_root.get("certifying_seats"))
    else:
        prior, prior_ok = set(), True
    seats, seats_ok = _string_list(rotation.get("certifying_seats"))
    if not (prior_ok and seats_ok):
        blocks.append(BLOCK_STALE_ROOT)
    if not seats or seats & prior:
        blocks.append(BLOCK_STALE_ROOT)
    if rotation.get("change_author") in seats:
        blocks.append(BLOCK_STALE_ROOT)
    if _approver_is_placeholder(rotation.get("human_approver")):
        blocks.append(BLOCK_STALE_ROOT)
    invalidated, invalidated_ok = _string_list(rotation.get("invalidated_basis_records", []))
    if not invalidated_ok:
        # A string here would iterate CHARACTERS; None would raise. Same class.
        blocks.append(BLOCK_STALE_ROOT)
    for record in invalidated:
        if record not in existing_basis_records:
            blocks.append(BLOCK_STALE_ROOT)
    return sorted(set(blocks))


_UNSET = object()


def _read_optional_json(path: str | Path | None, default: Any) -> Any:
    if not path:
        return default
    p = Path(path)
    if not p.exists():
        return default
    return read_json(p)


def _artifact_path(repo: Path, relative: str) -> Path:
    return repo / "skills" / "bridge-protocol" / "gate" / relative


def _read_required_artifact(repo: Path, relative: str) -> tuple[Any | None, str | None]:
    path = _artifact_path(repo, relative)
    try:
        return read_json(path), None
    except (OSError, json.JSONDecodeError):
        return None, BLOCK_SETUP


def _read_trust_root(repo: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = _artifact_path(repo, "trust_root.json")
    try:
        return read_json(path), None
    except (OSError, json.JSONDecodeError):
        return None, BLOCK_STALE_ROOT


def _read_manifest(repo: Path, manifest_ref: str | None) -> list[dict[str, Any]]:
    if not manifest_ref:
        return []
    path = Path(manifest_ref)
    if not path.is_absolute():
        path = repo / path
    try:
        doc = read_json(path)
    except (OSError, json.JSONDecodeError):
        return []
    return doc if isinstance(doc, list) else []


def evaluate(
    phase_input: dict[str, Any],
    repo: str | Path,
    *,
    registry: dict[str, Any] | None | object = _UNSET,
    manifest: list[dict[str, Any]] | None | object = _UNSET,
    readiness: dict[str, Any] | None | object = _UNSET,
    stage_modes: dict[str, Any] | None | object = _UNSET,
    trust_root: dict[str, Any] | None | object = _UNSET,
    changed_paths: list[str] | None | object = _UNSET,
    review_diff: str | None | object = _UNSET,
) -> dict[str, Any]:
    repo_path = Path(repo)
    blocks: list[str] = []
    standing_check_findings: list[dict[str, Any]] = []
    paths: list[str] | None = None if changed_paths is _UNSET else changed_paths or []
    diff_text: str | None = None if review_diff is _UNSET else review_diff or ""

    if "gate_decision" in phase_input or "block_reasons" in phase_input:
        blocks.append(BLOCK_BUILDER_DECISION)
    if phase_input.get("phase_class") == "declarative" and phase_input.get("verified") is True:
        blocks.append(BLOCK_DECLARATIVE_VERIFIED)

    try:
        validate_doc(phase_input, load_schema(repo_path, "phase_input"))
    except GateError as exc:
        if "forbidden" in str(exc) or "gate-produced" in str(exc):
            if BLOCK_BUILDER_DECISION not in blocks:
                blocks.append(BLOCK_BUILDER_DECISION)
        elif BLOCK_DECLARATIVE_VERIFIED not in blocks:
            blocks.append(BLOCK_SETUP)

    for report in phase_input.get("reviewer_reports", []):
        for finding in report.get("findings", []):
            if finding.get("severity") in {"P0", "P1"} and finding.get("status") == "open":
                blocks.append(BLOCK_OPEN_FINDING)

    if phase_input.get("phase_class") == "executable":
        blocks.extend(hard_signal_blocks(repo_path, phase_input.get("hard_signal_evidence")))

    if phase_input.get("run_record"):
        blocks.extend(brief_authorship_blocks(phase_input["run_record"], phase_input.get("bus_records", [])))

    if registry is _UNSET:
        registry, registry_error = _read_required_artifact(repo_path, "layer_registry.json")
        if registry_error:
            blocks.append(registry_error)
    if manifest is _UNSET:
        manifest = _read_manifest(repo_path, phase_input.get("manifest_ref"))
    if registry is not None and registry is not _UNSET:
        if changed_paths is _UNSET:
            try:
                target_branch = phase_input.get("target_branch") or "main"
                paths = changed_files(repo_path, target_branch)
                paths = scope_changed_paths_for_phase(paths, phase_input.get("phase", ""))
                if diff_text is None:
                    diff_text = git_review_diff(repo_path, target_branch)
            except (OSError, subprocess.SubprocessError, GateError):
                paths = []
                blocks.append(BLOCK_SETUP)
        else:
            paths = changed_paths or []
        classified, class_blocks = classify_changes(paths, registry)
        blocks.extend(class_blocks)
        blocks.extend(manifest_blocks(classified, manifest or [], phase_input.get("reviewer_reports", [])))

    h2_result = h2_standing_check(phase_input, repo_path, paths or [], diff_text or "")
    if (
        h2_result["status"] == "flagged"
        or h2_result["reason"] is not None
        or (h2_result["status"] == "shadow" and H2_MODE == "block")
        or (h2_result["status"] == "static-only-unacknowledged" and H2_MODE == "block")
    ):
        blocks.append(BLOCK_H2_STANDING_CHECK)

    if phase_input.get("phase") != "bridge-protocol-root" and paths and diff_text:
        standing_check_findings.extend(h1_standing_check(repo_path, paths, diff_text))
    if standing_check_findings:
        blocks.append(BLOCK_H1_STANDING_CHECK)

    if readiness is _UNSET:
        readiness, readiness_error = _read_required_artifact(repo_path, "validator_readiness.json")
        if readiness_error:
            blocks.append(readiness_error)
            readiness = {"validators": []}
    elif readiness is None:
        readiness = {"validators": []}
    if stage_modes is _UNSET:
        stage_modes, stage_modes_error = _read_required_artifact(repo_path, "stage_modes.json")
        if stage_modes_error:
            blocks.append(stage_modes_error)
            stage_modes = {"stages": {}}
    elif stage_modes is None:
        stage_modes = {"stages": {}}

    try:
        head_for_basis = git_head(repo_path) if (repo_path / ".git").exists() else "HEAD"
    except (OSError, subprocess.SubprocessError):
        head_for_basis = "HEAD"
        blocks.append(BLOCK_SETUP)
    blocks.extend(basis_blocks(phase_input, readiness, stage_modes, head_for_basis))

    if phase_input.get("references_validator"):
        status = validator_status(readiness, phase_input["references_validator"]).get("status", "absent")
        if status != "verified":
            blocks.append(BLOCK_CIRCULAR_VALIDATOR)

    blocks.extend(self_cert_blocks(phase_input))

    escaped = phase_input.get("escaped_defect", {})
    if escaped.get("triggered") and escaped.get("state") not in {"fixed", "invalidated"}:
        blocks.append(BLOCK_ESCAPED_DEFECT)

    if trust_root is _UNSET:
        trust_root, trust_error = _read_trust_root(repo_path)
        if trust_error:
            blocks.append(trust_error)
    if trust_root is not None and trust_root is not _UNSET:
        blocks.extend(trust_root_blocks(repo_path, trust_root))

    block_reasons = sorted(set(blocks))
    try:
        head = git_head(repo_path) if (repo_path / ".git").exists() else None
    except (OSError, subprocess.SubprocessError):
        head = None
    return {
        "gate_decision": "block" if block_reasons else "pass",
        "block_reasons": block_reasons,
        "derived_head": head,
        "derived_basis_available": basis_available(readiness, stage_modes, head or "HEAD", phase_input.get("phase", "")),
        "run_after_final_diff": not bool(hard_signal_blocks(repo_path, phase_input.get("hard_signal_evidence")))
        if phase_input.get("phase_class") == "executable"
        else None,
        "schema_valid": BLOCK_SETUP not in block_reasons,
        "verified": phase_input.get("phase_class") == "executable" and not block_reasons,
        "judged": phase_input.get("phase_class") == "declarative",
        "standing_check_findings": standing_check_findings,
        "h2_status": h2_result["status"],
        "h2_notice": h2_result["notice"],
        "h2_record": h2_result["record"],
    }

"""Neutral-only run record validators."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import validate

from .canonical import seal


RUN_RECORD_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "run_record.json"
PANEL_CONSTANTS_PATH = Path(__file__).resolve().parents[1] / "diagnose" / "panel_constants.json"


NEUTRAL_BLOCK_REASONS = {
    "scope-override",
    "blind-assignment-violation",
    "non-exclusive-predicate",
    "reciprocal-certification",
    "scribe-contamination",
    "cross-channel-routing",
    "evidence-category-mismatch",
    "unverified-without-panel",
    "certifier-starved",
}


def validate_run_record(run_record: dict) -> list[str]:
    reasons: list[str] = []
    reasons.extend(_scope_blocks(run_record))
    reasons.extend(_assignment_blocks(run_record))
    reasons.extend(_predicate_blocks(run_record))
    reasons.extend(_scribe_blocks(run_record))
    reasons.extend(_channel_blocks(run_record))
    reasons.extend(_panel_blocks(run_record))
    return sorted(set(reasons))


def _scope_blocks(run_record: dict) -> list[str]:
    expected = _normalised_list(run_record.get("extraction_scope", {}).get("paths", []))
    observed = _normalised_list(item.get("path") for item in run_record.get("observables", []))
    if not expected:
        return ["scope-override"]
    if expected != observed:
        return ["scope-override"]
    return []


def _assignment_blocks(run_record: dict) -> list[str]:
    assignment = run_record.get("assignment", {})
    if assignment.get("orchestrator_chosen") or "orchestrator_hypothesis" in assignment:
        return ["blind-assignment-violation"]
    return []


def _predicate_blocks(run_record: dict) -> list[str]:
    reasons: list[str] = []
    edges: set[tuple[str, str]] = set()
    for predicate in run_record.get("predicates", []):
        under_b = predicate.get("under_b", {})
        certifier = predicate.get("certifier", {})
        if "not_value" not in under_b or predicate.get("observable") is None:
            reasons.append("non-exclusive-predicate")
        if certifier.get("seat") == predicate.get("author_seat"):
            reasons.append("reciprocal-certification")
        if certifier.get("model") == predicate.get("author_model"):
            reasons.append("reciprocal-certification")
        evidence_ref = certifier.get("evidence_ref", "")
        if resolve_evidence_category(evidence_ref) != certifier.get("evidence_category"):
            reasons.append("evidence-category-mismatch")
        author = predicate.get("author_seat")
        certifier_seat = certifier.get("seat")
        if author and certifier_seat:
            edges.add((author, certifier_seat))
    if _has_cycle(edges):
        reasons.append("reciprocal-certification")
    return reasons


def _scribe_blocks(run_record: dict) -> list[str]:
    scribe_context = run_record.get("scribe_context", {})
    if scribe_context.get("orchestrator_context_visible"):
        return ["scribe-contamination"]
    return []


def _channel_blocks(run_record: dict) -> list[str]:
    for route in run_record.get("channel_routes", []):
        if route.get("phase") == "pre-submit" and route.get("from") != route.get("to"):
            return ["cross-channel-routing"]
    return []


def _panel_blocks(run_record: dict) -> list[str]:
    if run_record.get("verified") is not True:
        return []
    if run_record.get("panel_executed") is not True:
        return ["unverified-without-panel"]

    constants = _panel_constants()
    roster = {seat["role"]: seat for seat in constants["roster"]}
    required_roles = {"scribe", *(seat["role"] for seat in constants["roster"])}
    sealed_briefs = run_record.get("sealed_briefs", [])
    submissions = run_record.get("submissions", [])
    post_briefs = run_record.get("post_briefs", [])

    sealed_by_role = {brief.get("role"): brief for brief in sealed_briefs}
    submissions_by_role = {submission.get("role"): submission for submission in submissions}
    if set(sealed_by_role) != required_roles or set(submissions_by_role) != required_roles:
        return ["unverified-without-panel"]
    if any(brief.get("seal") != seal(brief.get("brief", {})) for brief in sealed_by_role.values()):
        return ["unverified-without-panel"]
    for role in required_roles:
        submission = submissions_by_role[role]
        expected_seat = roster[role]["target_id"] if role in roster else role
        if submission.get("seat") != expected_seat or submission.get("seal") != sealed_by_role[role].get("seal"):
            return ["unverified-without-panel"]
        if not submission.get("bus_reply_ref") or not _looks_sha256(submission.get("bus_reply_sha256", "")):
            return ["unverified-without-panel"]

    scribe_brief = sealed_by_role["scribe"].get("brief", {})
    expected_scribe = {
        "role": "scribe",
        "observables": scribe_brief.get("observables"),
        "task": "describe-observables-only",
        "system_prompt": constants["scribe"]["system_prompt"],
        "window": dict(run_record.get("recorded_traceback", {}).get("window", {})),
    }
    if scribe_brief != expected_scribe:
        return ["unverified-without-panel"]

    post_by_role = {brief.get("role"): brief for brief in post_briefs}
    if set(post_by_role) != {"certifier", "collation"}:
        return ["unverified-without-panel"]
    expected_submissions = _canonical_submissions([s for s in submissions if s.get("role") != "scribe"])
    expected_post = {
        "certifier": {
            "role": "certifier",
            "task": constants["certifier"]["rule"],
            "sealed_submissions": expected_submissions,
        },
        "collation": {
            "role": "collation",
            "task": constants["collation_order"],
            "sealed_submissions": expected_submissions,
        },
    }
    for role, body in expected_post.items():
        post = post_by_role[role]
        if post.get("brief") != body or post.get("seal") != seal(body):
            return ["unverified-without-panel"]
    return []


def _panel_constants() -> dict:
    return json.loads(PANEL_CONSTANTS_PATH.read_text(encoding="utf-8"))


def _looks_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _canonical_submissions(submissions: list[dict]) -> list[dict]:
    return sorted(
        [
            {
                "role": item["role"],
                "seat": item["seat"],
                "model": item.get("model", ""),
                "seal": item["seal"],
                "bus_reply_ref": item["bus_reply_ref"],
                "bus_reply_sha256": item["bus_reply_sha256"],
            }
            for item in submissions
        ],
        key=lambda item: (item["seat"], item["role"]),
    )


def assert_run_record_conformant(run_record: dict) -> None:
    schema = json.loads(RUN_RECORD_SCHEMA_PATH.read_text(encoding="utf-8"))
    validate(instance=run_record, schema=schema)


def _normalised_list(values) -> list[str]:
    return [str(value).replace("\\", "/").lstrip("./") for value in values if value]


def _has_cycle(edges: set[tuple[str, str]]) -> bool:
    graph: dict[str, set[str]] = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for target in graph.get(node, set()):
            if visit(target):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def resolve_evidence_category(evidence_ref: str) -> str:
    if evidence_ref.startswith("live:"):
        return "live_data"
    path, sep, line = evidence_ref.rpartition(":")
    if sep and path and line.isdigit():
        return "file_line"
    return "none"

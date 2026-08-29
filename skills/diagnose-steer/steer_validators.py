"""Steer-specific validators for diagnose-steer only."""

from __future__ import annotations

import json
from pathlib import Path

from skills._diagnose_common import (
    max_seq,
    read_dispatch_log,
    resolve_evidence_category,
    validate_dispatch_log,
    validate_run_record,
)


STEER_BLOCK_REASONS = {
    "early-steer-reveal",
    "steer-via-observation",
    "unresolved-citation",
    "no-discriminating-experiment",
    "inverted-confidence",
    "pmax-as-input",
}

CONSTANTS_PATH = Path(__file__).resolve().parent / "confidence_constants.json"


def validate_steer_run_record(run_record: dict) -> list[str]:
    reasons = set(validate_run_record(run_record))
    reasons.update(_dispatch_blocks(run_record))
    reasons.update(_channel_blocks(run_record))
    reasons.update(_reason_blocks(run_record))
    reasons.update(_experiment_blocks(run_record))
    reasons.update(_phase_input_blocks(run_record))
    reasons.update(_confidence_blocks(run_record))
    return sorted(reasons)


def compute_confidence(predicates: list[dict], constants: dict | None = None) -> dict:
    constants = constants or load_confidence_constants()
    p_max = constants["P_max"]
    strength_by_category = constants["strength_by_category"]
    cap = float(constants["alt_contribution_cap"])
    by_alt: dict[str, float] = {}
    for predicate in predicates:
        if not _is_certified(predicate):
            continue
        alt = str(predicate["hypothesis_b"])
        category = resolve_evidence_category(predicate["certifier"]["evidence_ref"])
        strength = float(strength_by_category[category])
        by_alt[alt] = max(by_alt.get(alt, 0.0), _predicate_exclusivity(predicate) * strength)
    product = 1.0
    for best in by_alt.values():
        product *= 1.0 - min(cap, best)
    q = 1.0 - product
    return {"P_max": p_max, "Q": q, "confidence": p_max * q}


def load_confidence_constants() -> dict:
    return json.loads(CONSTANTS_PATH.read_text(encoding="utf-8"))


def _dispatch_blocks(run_record: dict) -> list[str]:
    events = read_dispatch_log(run_record.get("dispatch_log_ref", ""))
    if not events or validate_dispatch_log(events):
        return ["dispatch-log-invalid"]
    steer_seq = min(
        (event["seq"] for event in events if event.get("event_type") == "steer_sent"),
        default=None,
    )
    blind_seq = max_seq(events, "blind_submission_received")
    if steer_seq is None or blind_seq is None:
        return ["early-steer-reveal"]
    if steer_seq <= blind_seq:
        return ["early-steer-reveal"]
    return []


def _channel_blocks(run_record: dict) -> list[str]:
    for observable in run_record.get("observables", []):
        if observable.get("channel") == "steer" or "steer" in observable:
            return ["steer-via-observation"]
    return []


def _reason_blocks(run_record: dict) -> list[str]:
    reason = run_record.get("steer", {}).get("reason", {})
    reason_type = reason.get("type")
    if reason_type == "unreconstructable_context":
        cited = reason.get("cited_fact")
        resolved = set(run_record.get("resolved_citations", []))
        if not cited or cited not in resolved:
            return ["unresolved-citation"]
    elif reason_type == "search_space_scale":
        if reason.get("space_size", 0) <= 0 or reason.get("budget", 0) <= 0:
            return ["unresolved-citation"]
    else:
        return ["unresolved-citation"]
    return []


def _experiment_blocks(run_record: dict) -> list[str]:
    if run_record.get("steered") and run_record.get("converged"):
        if not any(
            predicate.get("certifier", {}).get("evidence_category") == "live_data"
            for predicate in run_record.get("predicates", [])
        ):
            return ["no-discriminating-experiment"]
    return []


def _phase_input_blocks(run_record: dict) -> list[str]:
    phase_input = run_record.get("phase_input", {})
    forbidden = {"P_max", "floor", "strength_by_category", "alt_contribution_cap"} & set(phase_input)
    if forbidden:
        return ["pmax-as-input"]
    return []


def _confidence_blocks(run_record: dict) -> list[str]:
    expected = compute_confidence(run_record.get("predicates", []))
    actual = run_record.get("confidence", {})
    for key in ("P_max", "Q", "confidence"):
        if abs(float(actual.get(key, -1.0)) - float(expected[key])) > 1e-9:
            return ["inverted-confidence"]
    return []


def _is_certified(predicate: dict) -> bool:
    certifier = predicate.get("certifier", {})
    if certifier.get("seat") == predicate.get("author_seat"):
        return False
    if certifier.get("model") == predicate.get("author_model"):
        return False
    return certifier.get("evidence_category") == resolve_evidence_category(certifier.get("evidence_ref", ""))


def _predicate_exclusivity(predicate: dict) -> float:
    return 1.0 if predicate.get("under_b", {}).get("not_value") is not None else 0.0

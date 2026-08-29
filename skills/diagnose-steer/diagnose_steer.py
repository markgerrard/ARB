"""Read-only diagnose-steer harness over live panel dispatch."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
from typing import Callable

from skills._diagnose_common import (
    assert_run_record_conformant,
    collate_artifacts,
    read_dispatch_log,
    resolve_evidence_category,
    seal,
)
from skills.diagnose.briefs import author_post_briefs
from skills.diagnose.panel import run_panel
from skills.diagnose import derive_scope, extract

try:
    from .dispatch import append_steer_event, clean_scribe_context, disjoint_channel_routes
    from .steer_validators import compute_confidence, validate_steer_run_record
except ImportError:  # support direct file loading from the hyphenated skill directory
    _skill_dir = Path(__file__).resolve().parent

    def _load_sibling(name: str):
        spec = importlib.util.spec_from_file_location(name, _skill_dir / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        return module

    _dispatch = _load_sibling("dispatch")
    _validators = _load_sibling("steer_validators")
    append_steer_event = _dispatch.append_steer_event
    clean_scribe_context = _dispatch.clean_scribe_context
    disjoint_channel_routes = _dispatch.disjoint_channel_routes
    compute_confidence = _validators.compute_confidence
    validate_steer_run_record = _validators.validate_steer_run_record


SEATS = [
    {"seat": "alpha", "model": "model-alpha", "role": "blind"},
    {"seat": "beta", "model": "model-beta", "role": "alternative"},
    {"seat": "steelman", "model": "model-steelman", "role": "steelman"},
    {"seat": "scribe", "model": "model-scribe", "role": "scribe"},
]


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run_diagnose_steer(
    repo_root: str | Path,
    trigger: dict,
    steer: dict,
    work_dir: str | Path,
    *,
    dispatch: Callable[[dict], dict | None] | None = None,
) -> tuple[dict, list[str]]:
    root = Path(repo_root)
    work = Path(work_dir)
    log_path = work / "dispatch_log.jsonl"

    append_steer_event(log_path, "trigger_received", "orchestrator", "trigger", json.dumps(trigger, sort_keys=True))
    constants = _load_panel_constants(root)
    recorded_traceback = trigger.get(
        "recorded_traceback",
        {"reproduced": True, "window": {"start": 1, "end": 1}, "traceback": "", "blocking": None},
    )
    scope = derive_scope(trigger["failing_test"], recorded_traceback, root)
    append_steer_event(log_path, "scope_derived", "diagnose-steer", "extraction", json.dumps(scope, sort_keys=True))
    observations = extract(root, scope)
    append_steer_event(log_path, "artifact_visible", "scribe", "observation", json.dumps(observations, sort_keys=True))

    _append_blind_submissions(log_path, observations)
    steer_brief = author_steer_brief(steer, trigger["failing_test"], constants, _blind_max_seq(log_path))
    append_steer_event(log_path, "steer_sent", "orchestrator", "steer", json.dumps(steer_brief, sort_keys=True))
    panel_result = run_panel([steer_brief], _required_dispatch(dispatch), work / "panel", repo_root=root)

    predicates = build_predicates(observations)
    confidence = compute_confidence(predicates)
    confidence["attested_residual"] = "motive-validity-panel-judged"

    if panel_result["blocking"]:
        record = _base_record(
            trigger,
            scope,
            observations,
            log_path,
            predicates,
            confidence,
            steer,
            root,
            panel_executed=False,
            sealed_briefs=[steer_brief],
            submissions=panel_result.get("submissions", []),
            post_briefs=[],
        )
        record["harness_only"] = True
        record["blocking_real_use"] = panel_result["blocking"]
        assert_run_record_conformant(record)
        return record, [panel_result["blocking"]]

    post_briefs = author_post_briefs(constants, panel_result["submissions"])
    record = _base_record(
        trigger,
        scope,
        observations,
        log_path,
        predicates,
        confidence,
        steer,
        root,
        panel_executed=True,
        sealed_briefs=[steer_brief],
        submissions=panel_result["submissions"],
        post_briefs=post_briefs,
    )
    record["harness_only"] = True
    record["blocking_real_use"] = "steer-verification-pending"
    assert_run_record_conformant(record)
    return record, validate_steer_run_record(record)


def _base_record(
    trigger: dict,
    scope: dict,
    observations: list[dict],
    log_path: Path,
    predicates: list[dict],
    confidence: dict,
    steer: dict,
    repo_root: Path,
    *,
    panel_executed: bool,
    sealed_briefs: list[dict],
    submissions: list[dict],
    post_briefs: list[dict],
) -> dict:
    return {
        "trigger": dict(trigger),
        "repo_root": str(repo_root),
        "extraction_scope": scope,
        "observables": observations,
        "seats": [dict(seat) for seat in SEATS],
        "predicates": predicates,
        "dispatch_log_ref": str(log_path),
        "confidence": confidence,
        "assignment": {"orchestrator_chosen": False, "steered_seat": "steelman"},
        "scribe_context": clean_scribe_context(),
        "channel_routes": disjoint_channel_routes(),
        "collation": collate_artifacts(observations),
        "steered": True,
        "steer": {
            "declared_steer": dict(steer),
            "reason": dict(steer["reason"]),
            "channel": "steer",
            "pre_steer_state": {"blind_submission_max_seq": _blind_max_seq(log_path)},
            "post_steer_state": {"steer_seen_by": "steelman"},
        },
        "resolved_citations": list(steer.get("resolved_citations", [])),
        "converged": True,
        "sealed_briefs": sealed_briefs,
        "submissions": submissions,
        "post_briefs": post_briefs,
        "panel_executed": panel_executed,
        "verified": False,
    }


def _required_dispatch(dispatch: Callable[[dict], dict | None] | None) -> Callable[[dict], dict | None]:
    if dispatch is not None:
        return dispatch

    def missing_dispatch(_sealed_brief: dict) -> dict | None:
        return None

    return missing_dispatch


def author_steer_brief(declared_steer: dict, failing_test: str, constants: dict, blind_submission_max_seq: int | None) -> dict:
    brief = {
        "role": "steer",
        "task": "inspect declared steer after blind barrier",
        "failing_test": failing_test,
        "declared_steer": dict(declared_steer),
        "blind_submission_max_seq": blind_submission_max_seq,
    }
    return {"role": "steer", "model": constants["scribe"]["model"], "brief": brief, "seal": seal(brief)}


def steer_brief_authorship_blocks(run_record: dict) -> list[str]:
    constants = _load_panel_constants(Path("."))
    expected = author_steer_brief(
        run_record.get("steer", {}).get("declared_steer", {}),
        run_record.get("trigger", {}).get("failing_test", ""),
        constants,
        run_record.get("steer", {}).get("pre_steer_state", {}).get("blind_submission_max_seq"),
    )
    actual = next((brief for brief in run_record.get("sealed_briefs", []) if brief.get("role") == "steer"), None)
    if not actual:
        return ["brief-not-skill-authored"]
    if actual.get("seal") != seal(actual.get("brief", {})):
        return ["brief-tampered"]
    if actual.get("brief") != expected["brief"]:
        return ["brief-not-skill-authored"]
    return []


def build_predicates(observations: list[dict]) -> list[dict]:
    predicates = []
    for index in range(2):
        observable = observations[index % len(observations)]["id"] if observations else "none"
        predicates.append(
            {
                "predicate_id": f"p{index + 1}",
                "author_seat": "alpha",
                "author_model": "model-alpha",
                "hypothesis_a": "steered fault",
                "hypothesis_b": f"alternative {index + 1}",
                "observable": observable,
                "under_a": {"op": "eq", "value": "fails-here"},
                "under_b": {"op": "eq", "not_value": "fails-here"},
                "certifier": {
                    "seat": "beta",
                    "model": "model-beta",
                    "evidence_ref": "live:experiment-1",
                    "derivation_ref": f"derived-from:{observable}",
                    "evidence_category": resolve_evidence_category("live:experiment-1"),
                },
            }
        )
    return predicates


def _append_blind_submissions(log_path: Path, observations: list[dict]) -> None:
    payload = json.dumps({"observations": [item["id"] for item in observations]}, sort_keys=True)
    append_steer_event(log_path, "blind_submission_received", "alpha", "blind", payload)
    append_steer_event(log_path, "blind_submission_received", "beta", "blind", payload)


def _blind_max_seq(log_path: Path) -> int | None:
    from skills._diagnose_common import max_seq

    return max_seq(read_dispatch_log(log_path), "blind_submission_received")


def _load_panel_constants(repo_root: Path) -> dict:
    constants = json.loads(Path("skills/diagnose/panel_constants.json").read_text(encoding="utf-8"))
    constants["_repo_root"] = str(repo_root)
    return constants

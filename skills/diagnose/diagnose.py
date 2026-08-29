"""Read-only diagnose orchestration over live panel dispatch."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from skills._diagnose_common import (
    NEUTRAL_BLOCK_REASONS,
    RUN_RECORD_SCHEMA_PATH,
    append_event,
    collate_artifacts,
    max_seq,
    read_dispatch_log,
    validate_dispatch_log,
    validate_run_record,
)

from .briefs import CertifierStarved, author_briefs, author_post_briefs
from .dispatch import clean_scribe_context, disjoint_channel_routes, validate_diagnose_input
from .extraction import derive_scope, extract
from .containment import run_contained
from .panel import run_panel


TRACEBACK_LINE_RE = re.compile(r'File "([^"]+)", line (\d+)')


def run_diagnose(
    repo_root: str | Path,
    trigger: dict,
    work_dir: str | Path,
    *,
    dispatch: Callable[[dict], dict | None] | None = None,
) -> tuple[dict, list[str]]:
    """Build and evaluate a diagnose run_record without applying code changes."""
    input_reasons = validate_diagnose_input(trigger)
    if input_reasons:
        raise ValueError(f"invalid diagnose input: {input_reasons}")

    root = Path(repo_root)
    work = Path(work_dir)
    log_path = work / "dispatch_log.jsonl"
    append_event(log_path, "trigger_received", "orchestrator", "trigger", json.dumps(trigger, sort_keys=True))
    constants = _load_panel_constants(root)
    repo_sha = _repo_sha(root)

    recorded_traceback = record_traceback(str(_checkout_at(root, repo_sha, work)), trigger["failing_test"])
    if recorded_traceback["blocking"]:
        return {
            "trigger": dict(trigger),
            "repo_root": str(root),
            "extraction_scope": {"paths": [], "window": recorded_traceback["window"]},
            "observables": [],
            "seats": _seats_from_constants(constants),
            "predicates": [],
            "dispatch_log_ref": str(log_path),
            "confidence": {},
            "panel_executed": False,
            "verified": False,
            "harness_only": True,
            "blocking_real_use": recorded_traceback["blocking"],
            "assignment": {"orchestrator_chosen": False},
            "scribe_context": clean_scribe_context(),
            "channel_routes": disjoint_channel_routes(),
        }, [recorded_traceback["blocking"]]

    scope = derive_scope(trigger["failing_test"], recorded_traceback, root, repo_sha)
    append_event(log_path, "scope_derived", "diagnose", "extraction", json.dumps(scope, sort_keys=True))

    observations = extract(root, scope, repo_sha)
    append_event(log_path, "artifact_visible", "scribe", "observation", json.dumps(observations, sort_keys=True))

    sealed_briefs = author_briefs(trigger["failing_test"], repo_sha, recorded_traceback, constants)
    panel_result = run_panel(sealed_briefs, _required_dispatch(dispatch), work / "panel", repo_root=root)
    if panel_result["blocking"]:
        return _blocked_panel_record(
            trigger,
            scope,
            observations,
            log_path,
            repo_sha,
            recorded_traceback,
            sealed_briefs,
            panel_result,
            constants,
        ), [panel_result["blocking"]]
    try:
        post_briefs = author_post_briefs(constants, panel_result["submissions"], [])
    except CertifierStarved:
        return _blocked_panel_record(
            trigger,
            scope,
            observations,
            log_path,
            repo_sha,
            recorded_traceback,
            sealed_briefs,
            {**panel_result, "blocking": "certifier-starved"},
            constants,
        ), ["certifier-starved"]

    record = {
        "trigger": dict(trigger),
        "repo_root": str(root),
        "repo_sha": repo_sha,
        "recorded_traceback": _anchored_traceback(recorded_traceback),
        "extraction_scope": scope,
        "observables": observations,
        "seats": _seats_from_constants(constants),
        "predicates": [],
        "dispatch_log_ref": str(log_path),
        "confidence": {},
        "sealed_briefs": sealed_briefs,
        "submissions": panel_result["submissions"],
        "post_briefs": post_briefs,
        "panel_executed": True,
        "verified": True,
        "assignment": {"orchestrator_chosen": False},
        "scribe_context": clean_scribe_context(),
        "channel_routes": disjoint_channel_routes(),
        "collation": collate_artifacts(observations),
    }

    reasons = evaluate_run_record(record)
    if reasons:
        record["verified"] = False
        record["harness_only"] = True
        record["blocking_real_use"] = reasons[0]
    return record, reasons


def evaluate_run_record(run_record: dict) -> list[str]:
    """Apply diagnose's neutral gate plus dispatch-log fail-closed checks."""
    reasons = set(validate_run_record(run_record))
    events = read_dispatch_log(run_record.get("dispatch_log_ref", ""))
    if not events or validate_dispatch_log(events):
        reasons.add("dispatch-log-invalid")
    return sorted(reasons)


def _required_dispatch(dispatch: Callable[[dict], dict | None] | None) -> Callable[[dict], dict | None]:
    if dispatch is not None:
        return dispatch

    def missing_dispatch(_sealed_brief: dict) -> dict | None:
        return None

    return missing_dispatch


def _blocked_panel_record(
    trigger: dict,
    scope: dict,
    observations: list[dict],
    log_path: Path,
    repo_sha: str,
    recorded_traceback: dict,
    sealed_briefs: list[dict],
    panel_result: dict,
    constants: dict,
) -> dict:
    return {
        "trigger": dict(trigger),
        "repo_root": str(Path(constants.get("_repo_root", ""))),
        "repo_sha": repo_sha,
        "recorded_traceback": _anchored_traceback(recorded_traceback),
        "extraction_scope": scope,
        "observables": observations,
        "seats": _seats_from_constants(constants),
        "predicates": [],
        "dispatch_log_ref": str(log_path),
        "confidence": {},
        "sealed_briefs": sealed_briefs,
        "submissions": panel_result.get("submissions", []),
        "post_briefs": [],
        "panel_executed": False,
        "verified": False,
        "harness_only": True,
        "blocking_real_use": panel_result["blocking"],
        "assignment": {"orchestrator_chosen": False},
        "scribe_context": clean_scribe_context(),
        "channel_routes": disjoint_channel_routes(),
        "collation": collate_artifacts(observations),
    }


def record_traceback(repo_sha_checkout: str, failing_test: str) -> dict:
    result = run_contained(
        [sys.executable, "-m", "pytest", failing_test, "-q", "--no-header"],
        cwd=repo_sha_checkout,
        timeout_s=120,
    )
    if not result["contained"]:
        return _blocked_traceback("test-containment-unavailable")
    if result["timed_out"]:
        return _blocked_traceback("test-execution-timeout")
    if not result["reproduced"]:
        return _blocked_traceback("test-nonreproduction")
    traceback = result["stdout"] + result["stderr"]
    return {
        "reproduced": True,
        "window": _window_from_traceback(traceback),
        "traceback": traceback,
        "blocking": None,
    }


def _blocked_traceback(reason: str) -> dict:
    return {"reproduced": False, "window": {"start": 0, "end": 0}, "traceback": "", "blocking": reason}


def _window_from_traceback(traceback: str) -> dict:
    lines = [int(match.group(2)) for match in TRACEBACK_LINE_RE.finditer(traceback)]
    if not lines:
        return {"start": 1, "end": 1}
    return {"start": min(lines), "end": max(lines)}


def _load_panel_constants(repo_root: Path) -> dict:
    constants = json.loads(Path("skills/diagnose/panel_constants.json").read_text(encoding="utf-8"))
    constants["_repo_root"] = str(repo_root)
    return constants


def _repo_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def _checkout_at(repo_root: Path, repo_sha: str, work_dir: Path) -> Path:
    checkout = work_dir / "repo-sha-checkouts" / repo_sha
    if checkout.exists():
        return checkout
    checkout.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--no-checkout", str(repo_root), str(checkout)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "checkout", "--quiet", "--detach", repo_sha],
        cwd=checkout,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return checkout


def _anchored_traceback(recorded_traceback: dict) -> dict:
    traceback = recorded_traceback.get("traceback", "")
    return {
        "reproduced": bool(recorded_traceback.get("reproduced")),
        "window": dict(recorded_traceback["window"]),
        "traceback_sha256": hashlib.sha256(traceback.encode("utf-8")).hexdigest(),
    }


def _seats_from_constants(constants: dict) -> list[dict]:
    seats = [
        {"seat": item["role"], "model": item["model"], "role": _schema_role(item["role"])}
        for item in constants["roster"]
    ]
    seats.append({"seat": "scribe", "model": constants["scribe"]["model"], "role": "scribe"})
    return seats


def _schema_role(role: str) -> str:
    return "steelman" if role == "open" else role


def run_record_schema() -> dict:
    return json.loads(RUN_RECORD_SCHEMA_PATH.read_text(encoding="utf-8"))


def neutral_block_reasons() -> set[str]:
    return set(NEUTRAL_BLOCK_REASONS)


def last_event_seq(run_record: dict, event_type: str) -> int | None:
    return max_seq(read_dispatch_log(run_record["dispatch_log_ref"]), event_type)

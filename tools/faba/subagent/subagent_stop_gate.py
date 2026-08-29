"""SubagentStop record gate — the FABA integrity invariant in hook form.

Design note art-0f9fa949a90ae634 §4, revised by the PF1 remediation (contract
v2): the round agent no longer publishes and holds no bus credential — the
DRIVER publishes the record after the child session ends and gates on its own
receipt round-trip. This hook enforces the deterministic CONTENT half at stop
time: decision-record.md must exist, parse against the schema, and cover the
prior record's open findings. Exit 2 blocks the subagent's stop and feeds the
named problems back to it; the agent cannot end a round without an acceptable
record on disk.

Wiring: registered under hooks.SubagentStop in .claude/settings.local.json;
fires on EVERY subagent stop, so it no-ops fast unless the parent armed a
round via the pointer file .claude/faba-current-round.json:

    {"request_id": ..., "record_artefact_id": ..., "workspace": ...,
     "prior_open_ids": [...], "attempts": 0}

The pointer is parent-authored (like the harness's minted ids); the gate
mutates it in place with attempts / gate outcome as the round's audit
residue. The gate needs no bus access at all — content validation is pure
disk+schema; ingestion is the driver's own publish/receipt round-trip.

Loop protection: after MAX_ATTEMPTS blocked stops the gate stops blocking and
records gate=failed (crash equivalence — the parent reads the pointer, sees a
failed round, re-dispatches). Never blocks forever on a round that cannot
publish.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FABA = HERE.parent
REPO = FABA.parents[1]
for p in (str(FABA), str(REPO / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

MAX_ATTEMPTS = 3
# FABA_POINTER_FILE override exists for the unit tests; production wiring uses
# the repo-fixed path so the parent and the hook agree without shared env.
POINTER = Path(os.environ.get("FABA_POINTER_FILE", str(REPO / ".claude" / "faba-current-round.json")))


def _manifest_covers(manifest_path: Path, name: str) -> bool:
    """True if the staged-input manifest exists and hashes a file named `name`.

    The manifest is forensics-grade only (the child can write anything in its
    workspace) — this proves a revision round was ARMED through a path that
    staged its inputs, not that the prior is untampered."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and any(Path(key).name == name for key in manifest)


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        hook_input = {}

    if not POINTER.exists():
        return 0  # no armed round — not a FABA subagent stop

    pointer = json.loads(POINTER.read_text(encoding="utf-8"))
    record_artefact_id = pointer["record_artefact_id"]

    # Observability residue: append every gated-session stop event to the round
    # workspace so probe rounds can pin the real payload shape. Never fatal.
    workspace = pointer.get("workspace")
    if workspace:
        try:
            with open(Path(workspace) / "gate-log.jsonl", "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "hook_input": {
                                k: hook_input.get(k)
                                for k in (
                                    "hook_event_name",
                                    "agent_id",
                                    "agent_type",
                                    "agent_transcript_path",
                                    "transcript_path",
                                    "stop_hook_active",
                                )
                            },
                            "pointer_gate": pointer.get("gate"),
                            "attempts": pointer.get("attempts", 0),
                        }
                    )
                    + "\n"
                )
        except OSError:
            pass

    if pointer.get("gate") == "passed":
        return 0  # already verified this round; never re-gate a passed round

    # Only gate the FABA subagent. SubagentStop input carries the subagent's OWN
    # transcript as agent_transcript_path; the top-level transcript_path is the
    # PARENT session's transcript, which contains the dispatch brief — grepping
    # it would match EVERY sibling stop (panel-faba-sa-...-3037b6 P1, 3-seat
    # convergent). The scoping TOKEN is the record artefact id: it is
    # parent-minted AND present in the v2 brief's round variables, unlike the
    # request_id, which no longer reaches the subagent at all now that the
    # publish command is gone (panel-faba-v2-...-c56303 grok F1 — scoping on
    # request_id made the gate treat our own round agent as foreign). An
    # unscopeable stop (field absent) is treated as foreign and never gated:
    # integrity is preserved by the driver, which publishes nothing for a round
    # whose gate never fired — the gate can false-fail, never false-accept.
    transcript = hook_input.get("agent_transcript_path")
    if not transcript or not Path(transcript).exists():
        return 0
    if record_artefact_id not in Path(transcript).read_text(encoding="utf-8", errors="replace"):
        return 0

    # Content gate (PF1 remediation, v2 contract): the round agent no longer
    # publishes and holds no bus credential — the DRIVER publishes after the
    # child session ends. This hook's job is the deterministic content half.
    # Two round kinds share the machinery (Workflow C, owner-directed
    # 2026-07-19): kind="review" (default — decision-record.md against the
    # schema + binding + prior-open coverage) and kind="author" (artefact.md
    # against the light authored-artefact check; quality is the panel's job).
    if not workspace:
        pointer["gate"] = "failed"
        pointer["gate_reason"] = "pointer carries no workspace — gate cannot locate the record"
        POINTER.write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"systemMessage": "FABA gate: no workspace in pointer; round recorded as FAILED"}))
        return 0  # fail the round loudly rather than trap the subagent

    kind = pointer.get("kind", "review")
    if kind == "author":
        from faba_schema import validate_authored_artefact, validate_revision_fold  # noqa: E402

        gated_file = "artefact.md"
        artefact_path = Path(workspace) / gated_file

        # F14 route (c) HYGIENE tier (panel panel-f14c-design-20260720T033218Z-5ec74f,
        # owner-scoped 2026-07-20): a revision round must look like one — prior body
        # staged and manifest-covered. These are ARMING failures the child cannot fix
        # (it has no Memory access), so they fail the round immediately, same shape as
        # the no-workspace branch above — never the bounce loop (cold-opus P2-3).
        # Hygiene, not integrity: prior-record.md is child-writable, so this catches
        # honest mis-arming, not a self-rewriting child; the blind-overwrite closure
        # is the publish-time store read-back (fetch-by-id follow-on).
        prior_text: str | None = None
        if pointer.get("revision"):
            prior_path = Path(workspace) / "prior-record.md"
            arming_problem = None
            if not prior_path.exists():
                arming_problem = "revision round has no prior-record.md in the workspace"
            else:
                prior_text = prior_path.read_text(encoding="utf-8")
                prior_check = validate_authored_artefact(prior_text, allow_trailing_markup=True)
                if not prior_check.ok:
                    arming_problem = (
                        "prior-record.md is not a publishable artefact body: "
                        + "; ".join(prior_check.problems)
                    )
            if arming_problem is None and not _manifest_covers(
                Path(workspace) / "staged-input-manifest.json", "prior-record.md"
            ):
                arming_problem = "staged-input-manifest.json missing or does not cover prior-record.md"
            if arming_problem:
                pointer["gate"] = "failed"
                pointer["gate_reason"] = f"revision arming invalid — {arming_problem}"
                POINTER.write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")
                print(json.dumps({"systemMessage": f"FABA gate: revision arming invalid; round recorded as FAILED — {arming_problem}"}))
                return 0

        if artefact_path.exists():
            artefact_text = artefact_path.read_text(encoding="utf-8")
            check = validate_authored_artefact(artefact_text)
            passed, reason = check.ok, ("authored-artefact check ok" if check.ok else "; ".join(check.problems))
            if passed and prior_text is not None:
                # Child-fixable fold failure (nothing folded) — bounce-eligible.
                fold_check = validate_revision_fold(artefact_text, prior_text)
                passed, reason = fold_check.ok, (
                    "authored-artefact + revision-fold checks ok" if fold_check.ok else "; ".join(fold_check.problems)
                )
        else:
            passed, reason = False, f"no {gated_file} in workspace"
    else:
        from faba_schema import validate_decision_record  # noqa: E402

        gated_file = "decision-record.md"
        record_path = Path(workspace) / gated_file
        if record_path.exists():
            check = validate_decision_record(
                record_path.read_text(encoding="utf-8"),
                pointer.get("prior_open_ids") or [],
                expected_round=pointer.get("round"),
                expected_subject=pointer.get("subject_artefact_id"),
            )
            # Status: failed is stoppable here — a failed round must still end with
            # a publishable record (crash equivalence); the DRIVER fails the round.
            passed, reason = check.ok, ("schema+binding+coverage ok" if check.ok else "; ".join(check.problems))
        else:
            passed, reason = False, f"no {gated_file} in workspace"

    if passed:
        pointer["gate"] = "passed"
        pointer["gate_reason"] = reason
        POINTER.write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"systemMessage": f"FABA record gate passed: {reason}"}))
        return 0

    attempts = int(pointer.get("attempts", 0)) + 1
    pointer["attempts"] = attempts
    if attempts >= MAX_ATTEMPTS:
        pointer["gate"] = "failed"
        pointer["gate_reason"] = f"{reason} (after {attempts} blocked stops)"
        POINTER.write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"systemMessage": f"FABA gate: round FAILED — {reason} (gave up after {attempts} attempts)"}))
        return 0

    POINTER.write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")
    print(
        f"FABA {kind} gate: your output is not acceptable ({reason}). "
        f"Fix exactly these problems in {gated_file} in your workspace, "
        f"then finish with the FABA_EXIT line. (gate attempt {attempts}/{MAX_ATTEMPTS})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

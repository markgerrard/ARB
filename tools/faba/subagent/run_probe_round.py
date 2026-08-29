"""Drive one subagent-FABA probe round (Workflow A, design note art-0f9fa949a90ae634 §4).

Why a child session: hooks and agent definitions snapshot at session start, so
the SubagentStop record gate cannot be exercised from the session that just
wrote it. This driver launches a fresh headless `claude -p` session that loads
`.claude/agents/faba-round.md` and the settings.local.json hook at ITS start,
plays the warm-orchestrator role (dispatches the faba-round agent via Task),
and is gated by the hook when the subagent stops.

Parent-minted integrity, same as the SDK harness: ids are generated HERE,
armed in the pointer file the gate reads, and embedded in the brief — the
round agent never publishes at all (contract v2: THIS driver validates the
record and publishes it after the child session ends) and cannot satisfy the
gate by prose. The shared contract (round-contract.md) is embedded verbatim
in the brief: one instruction surface for both FABA forms.

    .venv/bin/python tools/faba/subagent/run_probe_round.py \
        --artefact-id art-81438f2f5a5c4955 --round 1 --task "..." [--block-test]

--block-test adds the deliberate no-publish-first twist so the round proves
the gate's exit-2 block path end-to-end (first stop blocked, agent resumes,
publishes, second stop passes).

Exit 3 means the round passed its own gate but the post-hoc subject version
spot-diff detected drift. Exit 0/1/2 retain their existing meanings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import redis

HERE = Path(__file__).resolve().parent
FABA = HERE.parent
REPO = FABA.parents[1]
if str(FABA) not in sys.path:
    sys.path.insert(0, str(FABA))

from arb_memory.fetch import memory_fetch_by_id  # noqa: E402
from faba_launch import (  # noqa: E402
    emit_subject_drift,
    gate_hook_wired,
    observe_subject_version,
    parse_env_file,
    publish_and_gate,
    subject_spot_diff,
)
from faba_schema import validate_decision_record  # noqa: E402

POINTER = REPO / ".claude" / "faba-current-round.json"
SUBJECT_FETCH_TIMEOUT = 10.0

BLOCK_TEST_TWIST = """
GATE TEST TWIST (deliberate, part of the probe): on your FIRST attempt to
finish, deliberately leave decision-record.md ABSENT or schema-incomplete (for
example, omit the '## Recommendation' section) and end your turn. If your stop
is then blocked by a record-gate message naming schema problems, follow it: fix
decision-record.md exactly as instructed, and finish with the FABA_EXIT line.
Record in your decision record that the twist was active.
"""

ORCHESTRATOR_PROMPT = """You are the warm-orchestrator half of a FABA Workflow-A probe. Do EXACTLY this and nothing else:

1. Dispatch the `faba-round` agent via the Task tool. The Task prompt must be
   the FABA ROUND BRIEF below, verbatim and complete (from "# FABA round
   brief" to the end).
2. When the subagent returns, output its final message verbatim between the
   markers SUBAGENT_FINAL_BEGIN and SUBAGENT_FINAL_END, then stop.
3. Do not re-dispatch, do not perform any round work yourself, do not touch
   the workspace or the bus.

{brief}
"""


def materialise_workspace(
    workspace: Path,
    *,
    round_number: int,
    artefact_id: str,
    subject_summary: str,
    prior_record_id: str,
    record_artefact_id: str,
    task: str,
    prior_record_file: Path | None = None,
    prior_basis: str | None = None,
) -> list[str]:
    """Write round-input.json (+ the materialised prior record, LISTED in
    round-input — the contract tells the agent to work from copies listed
    there; an unlisted prior record bakes in a false-fail against the coverage
    gate or invites invented rows, panel-faba-v2-r2 codex F1). Returns the
    must-carry finding ids (the coverage predicate): the prior record's open
    findings, plus — when prior_basis is given — any closed finding reopened by
    a matching change since then (reopen consumer, ADR open item #12)."""
    prior_open_ids: list[str] = []
    round_input = {
        "round": round_number,
        "artefact_id": artefact_id,
        "subject_summary": subject_summary,
        "prior_record_id": prior_record_id,
        "record_artefact_id": record_artefact_id,
        "task": task,
    }
    if prior_record_file is not None:
        from faba_schema import basis_ref, must_carry_ids

        prior_text = prior_record_file.read_text(encoding="utf-8")
        (workspace / "prior-record.md").write_text(prior_text, encoding="utf-8")
        round_input["prior_record_file"] = "prior-record.md"
        # Auto-basis (open item #13): fall back to the commit the prior record
        # recorded (its Basis line) when no --prior-basis was passed.
        effective_basis = prior_basis or basis_ref(prior_text)
        changed_paths: list[str] = []
        if effective_basis is not None:
            from faba_git import changed_paths_since

            changed_paths = changed_paths_since(effective_basis)
        prior_open_ids = must_carry_ids(prior_text, changed_paths)
        round_input["must_carry_ids"] = prior_open_ids

    (workspace / "round-input.json").write_text(
        json.dumps(round_input, indent=2) + "\n", encoding="utf-8"
    )
    return prior_open_ids


def build_brief(
    contract: str,
    *,
    workspace,
    round_number: int,
    artefact_id: str,
    subject_summary: str,
    prior_record_id: str,
    record_artefact_id: str,
    task: str,
    block_test: bool,
) -> str:
    """The dispatch brief: shared contract verbatim + round variables.

    INVARIANT (panel-faba-v2 grok F1): the round variables MUST carry the
    record artefact id — it is the gate's scoping token, the only parent-minted
    string guaranteed to land in the round agent's transcript. Pinned by test.
    """
    return f"""# FABA round brief (subagent form)

{contract}

## Round variables

- workspace: {workspace}
- round: {round_number}
- subject artefact: {artefact_id}
- subject summary: {subject_summary}
- prior decision record: {prior_record_id}
- record artefact id the parent will publish under: {record_artefact_id}
- round task: {task}
{BLOCK_TEST_TWIST if block_test else ""}"""


def main(argv=None, *, fetch_by_id=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="native", help="native | bridge:<target-id>")
    parser.add_argument("--run-id", default=None, help="orchestrator-visible round id")
    parser.add_argument("--lease-ttl", type=int, default=None)
    parser.add_argument("--turn-timeout", type=int, default=900)
    parser.add_argument("--artefact-id", required=True)
    parser.add_argument(
        "--subject-summary",
        required=True,
        help="one line: WHAT the subject artefact is (lands in round-input.json)",
    )
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--prior-record-id", default="none")
    parser.add_argument(
        "--prior-record-file",
        type=Path,
        default=None,
        help="materialised prior decision record; its open findings become the coverage predicate",
    )
    parser.add_argument(
        "--prior-basis",
        default=None,
        help="git ref the prior round verified against; enables the reopen consumer "
        "(closed findings whose reopen-if scope changed since then are re-carried)",
    )
    parser.add_argument("--task", required=True)
    parser.add_argument("--block-test", action="store_true")
    parser.add_argument("--env-file", type=Path, default=REPO / ".env.oi-r26")
    parser.add_argument("--contract", type=Path, default=FABA / "round-contract.md")
    parser.add_argument("--child-model", default="sonnet")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument(
        "--child-timeout",
        type=int,
        default=900,
        help="child-session wall-clock ceiling in seconds; the 900s default fits "
        "probe/synth rounds but a fused panel round (dispatch + wait + synth) "
        "needs the seat timeout plus synthesis headroom (incident: r4 child "
        "killed at 900s while 7/8 seats were still mid-review, 2026-07-19)",
    )
    args = parser.parse_args(argv)

    if args.engine == "native":
        gate_problem = gate_hook_wired(REPO)
        if gate_problem is not None:
            print(f"[probe] {gate_problem}; refusing", file=sys.stderr)
            return 2
    elif not args.engine.startswith("bridge:") or not args.engine.removeprefix("bridge:"):
        parser.error("--engine must be native or bridge:<target-id>")

    # PF1 containment, driver side: the credential never enters os.environ (the
    # child session inherits it) — parse locally, publish from THIS process.
    # pop_publish_env strips BOTH publish creds; we consume only the memory URL.
    from agent_redis_bridge.dispatch_authority import pop_publish_env

    bus_env = parse_env_file(args.env_file) if args.env_file.exists() else {}
    redis_url = pop_publish_env(os.environ).get("ARB_MEMORY_REDIS_URL") or bus_env.get("ARB_MEMORY_REDIS_URL")
    if not redis_url:
        print("[probe] ARB_MEMORY_REDIS_URL not available — no bus, no gate; refusing", file=sys.stderr)
        return 2

    fetch_by_id = fetch_by_id or memory_fetch_by_id
    client = redis.from_url(redis_url, decode_responses=True)
    try:
        subject_fetch_timeout = float(
            os.environ.get("FABA_SUBJECT_FETCH_TIMEOUT", str(SUBJECT_FETCH_TIMEOUT))
        )
    except ValueError:
        subject_fetch_timeout = SUBJECT_FETCH_TIMEOUT
    subject_start = observe_subject_version(
        fetch_by_id, client, args.artefact_id, subject_fetch_timeout
    )

    if args.engine == "native" and POINTER.exists():
        print(f"[probe] pointer {POINTER} already exists — another round armed; refusing", file=sys.stderr)
        return 2

    run_id = args.run_id or uuid.uuid4().hex[:8]
    request_id = f"faba-sa-r{args.round}-{run_id}"
    record_artefact_id = f"art-faba-sa-{run_id}"

    if args.engine.startswith("bridge:"):
        from bridge_round import BridgeRoundError, lease_ttl_lower_bound, run_bridge_round

        target_id = args.engine.removeprefix("bridge:")
        try:
            subject_fetched = fetch_by_id(client, args.artefact_id, timeout=subject_fetch_timeout)
        except Exception:
            subject_fetched = None
        if not isinstance(subject_fetched, dict) or subject_fetched.get("outcome") != "ok" or not isinstance(subject_fetched.get("content"), str):
            print("[probe] bridge mode requires an arm-time materialisable subject; refusing", file=sys.stderr)
            return 2
        subject_bytes = subject_fetched["content"].encode("utf-8")
        prior_file = args.prior_record_file
        fetched_prior_bytes = None
        if prior_file is None and args.prior_record_id != "none":
            try:
                prior_fetched = fetch_by_id(client, args.prior_record_id, timeout=subject_fetch_timeout)
            except Exception:
                prior_fetched = None
            if not isinstance(prior_fetched, dict) or prior_fetched.get("outcome") != "ok" or not isinstance(prior_fetched.get("content"), str):
                print("[probe] bridge mode could not materialise the prior record; refusing", file=sys.stderr)
                return 2
            fetched_prior_bytes = prior_fetched["content"].encode("utf-8")

        with tempfile.TemporaryDirectory(prefix="faba-sa-stage-") as stage_dir:
            staged = Path(stage_dir)
            if fetched_prior_bytes is not None:
                (staged / "fetched-prior.md").write_bytes(fetched_prior_bytes)
                prior_file = staged / "fetched-prior.md"
            prior_open_ids = materialise_workspace(
                staged, round_number=args.round, artefact_id=args.artefact_id,
                subject_summary=args.subject_summary, prior_record_id=args.prior_record_id,
                record_artefact_id=record_artefact_id, task=args.task,
                prior_record_file=prior_file, prior_basis=args.prior_basis,
            )
            (staged / "subject.md").write_bytes(subject_bytes)
            round_input = json.loads((staged / "round-input.json").read_text(encoding="utf-8"))
            round_input["subject_file"] = "subject.md"
            round_input["subject_sha256"] = hashlib.sha256(subject_bytes).hexdigest()
            (staged / "round-input.json").write_text(json.dumps(round_input, indent=2) + "\n", encoding="utf-8")
            staged_files = {
                path.name: path.read_bytes() for path in staged.iterdir()
                if path.is_file() and path.name != "fetched-prior.md"
            }

        contract = args.contract.read_text(encoding="utf-8")
        # Seat-resolved isolation baseline: the subject is fully STAGED, so the
        # worktree base is only an isolation snapshot. Resolving a driver-repo
        # OID here breaks any seat anchored to a different repo (Chain B r2a
        # worktree-lease-base-ref-invalid, 2026-07-22); the seat pins "HEAD"
        # to an OID at arm time and the pinned value is recorded from the
        # bridge result as provenance.
        base_oid = "HEAD"
        ttl = args.lease_ttl or lease_ttl_lower_bound(args.turn_timeout)

        def make_bridge_brief(workspace):
            return build_brief(
                contract, workspace=workspace, round_number=args.round,
                artefact_id=args.artefact_id, subject_summary=args.subject_summary,
                prior_record_id=args.prior_record_id,
                record_artefact_id=record_artefact_id, task=args.task,
                block_test=args.block_test,
            ) + "\nRead the materialised subject_file listed in round-input.json. Return-channel rule: reply ONLY with FABA_EXIT.\n"

        def validate_bridge(text):
            return validate_decision_record(
                text, prior_open_ids, expected_round=args.round,
                expected_subject=args.artefact_id,
            ).problems

        def publish_bridge(workspace):
            return publish_and_gate(
                redis_url, workspace=workspace, record_artefact_id=record_artefact_id,
                request_id=request_id, author=f"faba-sa-r{args.round}",
                prior_open_ids=prior_open_ids, receipt_timeout=60.0,
                expected_round=args.round, expected_subject=args.artefact_id,
            )

        try:
            bridge_result, publish_result = run_bridge_round(
                target_id=target_id, env_file=args.env_file, run_id=run_id,
                artefact_lock_id=args.artefact_id, base_oid=base_oid,
                lease_ttl=ttl, turn_timeout=args.turn_timeout,
                staged_files=staged_files, output_name="decision-record.md",
                expected_exit_key="record_artefact_id", expected_exit_id=record_artefact_id,
                make_brief=make_bridge_brief, validate=validate_bridge,
                publish=publish_bridge,
            )
        except BridgeRoundError as exc:
            print(f"[probe] bridge round refused: {exc}", file=sys.stderr)
            return 2
        passed = bridge_result.passed
        receipt = publish_result[2] if publish_result else None
        check = publish_result[3] if publish_result else None
        phase = getattr(publish_result, "phase", "not_enqueued") if publish_result else "not_enqueued"
        subject_end = observe_subject_version(fetch_by_id, client, args.artefact_id, subject_fetch_timeout)
        spot_diff = subject_spot_diff(
            subject_start, subject_end, phase=phase, receipt=receipt, probe=True,
            refusal_cause=getattr(publish_result, "refusal_cause", None) if publish_result else None,
        )
        final = {
            "run_id": run_id, "request_id": request_id,
            "record_artefact_id": record_artefact_id,
            "workspace": str(bridge_result.workspace) if bridge_result.workspace else None,
            "engine_family": bridge_result.engine_family, "bounce_mode": bridge_result.bounce_mode,
            "lease_id": bridge_result.lease_id, "thread_id": bridge_result.thread_id,
            "base_oid": bridge_result.base_oid,
            "attempts": bridge_result.attempts,
            "content_gate": "passed" if publish_result else "failed",
            "content_gate_reason": bridge_result.reason,
            "gate": "passed" if passed else "failed", "gate_reason": bridge_result.reason,
            "content_check": None if check is None else {"ok": check.ok, "problems": check.problems},
            "receipt": receipt, "publish_phase": phase, "subject_spot_diff": spot_diff,
            "forensics_loss": bridge_result.forensics_loss,
        }
        print(json.dumps(final, indent=2))
        if passed and spot_diff["verdict"] == "drift":
            return 3
        return 0 if passed else 1

    workspace = Path(tempfile.mkdtemp(prefix=f"faba-sa-r{args.round}-"))
    from bridge_round import BridgeRoundError, acquire_local_lock, release_local_lock
    try:
        native_lock = acquire_local_lock(args.artefact_id)
    except BridgeRoundError as exc:
        print(f"[probe] {exc}; refusing", file=sys.stderr)
        return 2

    prior_open_ids = materialise_workspace(
        workspace,
        round_number=args.round,
        artefact_id=args.artefact_id,
        subject_summary=args.subject_summary,
        prior_record_id=args.prior_record_id,
        record_artefact_id=record_artefact_id,
        task=args.task,
        prior_record_file=args.prior_record_file,
        prior_basis=args.prior_basis,
    )

    brief = build_brief(
        args.contract.read_text(encoding="utf-8"),
        workspace=workspace,
        round_number=args.round,
        artefact_id=args.artefact_id,
        subject_summary=args.subject_summary,
        prior_record_id=args.prior_record_id,
        record_artefact_id=record_artefact_id,
        task=args.task,
        block_test=args.block_test,
    )

    # Arm the gate BEFORE the child session exists — parent-authored, like the
    # SDK harness's minted ids. record_artefact_id doubles as the gate's scoping
    # token (it is in the brief, so it lands in the round agent's transcript —
    # the request_id does NOT reach the subagent under contract v2); round +
    # subject give the content gate its binding predicate, prior_open_ids its
    # coverage predicate.
    POINTER.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "record_artefact_id": record_artefact_id,
                "workspace": str(workspace),
                "round": args.round,
                "subject_artefact_id": args.artefact_id,
                "prior_open_ids": prior_open_ids,
                "attempts": 0,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"[probe] run={run_id} request_id={request_id} record={record_artefact_id}\n"
        f"[probe] workspace={workspace} block_test={args.block_test}",
        file=sys.stderr,
        flush=True,
    )

    # No PYTHONPATH extension and no bus credential: the child neither imports
    # arb_memory nor publishes anything (contract v2 — the driver publishes).
    child_env = dict(os.environ)

    proc = None
    timed_out = False
    try:
        proc = subprocess.run(
            [
                "claude",
                "-p",
                ORCHESTRATOR_PROMPT.format(brief=brief),
                "--model",
                args.child_model,
                "--allowedTools",
                "Task,Read,Grep,Glob,Write,Edit,Bash",
                "--output-format",
                "json",
                "--max-turns",
                str(args.max_turns),
            ],
            cwd=REPO,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=args.child_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        (workspace / "child-stdout.json").write_text(
            (exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout) or "", encoding="utf-8"
        )
        (workspace / "child-stderr.txt").write_text(
            (exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr) or "", encoding="utf-8"
        )
    finally:
        pointer_state = json.loads(POINTER.read_text(encoding="utf-8")) if POINTER.exists() else None
        if pointer_state is not None:
            # Disarm ONLY the round we armed (another driver's pointer is not
            # ours to remove); keep the audit residue in the workspace, never
            # leave a stale armed round to gate unrelated future subagents.
            if pointer_state.get("request_id") == request_id:
                (workspace / "gate-pointer-final.json").write_text(
                    json.dumps(pointer_state, indent=2) + "\n", encoding="utf-8"
                )
                POINTER.unlink()
            else:
                print(
                    f"[probe] pointer holds foreign round {pointer_state.get('request_id')!r}"
                    " — leaving it armed, not ours to disarm",
                    file=sys.stderr,
                )
                pointer_state = None

    if proc is not None:
        (workspace / "child-stdout.json").write_text(proc.stdout or "", encoding="utf-8")
        (workspace / "child-stderr.txt").write_text(proc.stderr or "", encoding="utf-8")

    content_gate = (pointer_state or {}).get("gate")

    # Publish half (contract v2): only a content-gate-passed round is published,
    # by THIS process, with its own credential and receipt round-trip. The
    # publish gate re-validates content independently — the hook's verdict is
    # residue, not authority.
    if content_gate == "passed":
        publish_result = publish_and_gate(
            redis_url,
            workspace=workspace,
            record_artefact_id=record_artefact_id,
            request_id=request_id,
            author=f"faba-sa-r{args.round}",
            prior_open_ids=prior_open_ids,
            receipt_timeout=60.0,
            expected_round=args.round,
            expected_subject=args.artefact_id,
        )
        passed, reason, receipt, check = publish_result
    else:
        passed, reason, receipt, check = (
            False,
            f"content gate {content_gate or 'never-fired'} — nothing published",
            None,
            None,
        )

    if content_gate != "passed":
        phase = "not_enqueued"
    else:
        phase = getattr(
            publish_result,
            "phase",
            "receipt_confirmed" if receipt is not None else "receipt_unknown",
        )
    subject_end = observe_subject_version(
        fetch_by_id, client, args.artefact_id, subject_fetch_timeout
    )
    spot_diff = subject_spot_diff(
        subject_start,
        subject_end,
        phase=phase,
        receipt=receipt,
        probe=True,
        refusal_cause=getattr(publish_result, "refusal_cause", None)
        if content_gate == "passed"
        else None,
    )
    if spot_diff["verdict"] == "drift":
        print(
            "[probe] SUBJECT DRIFT: "
            f"artefact={args.artefact_id} start={spot_diff['start']} "
            f"end={spot_diff['end']} expected={spot_diff['expected']}",
            file=sys.stderr,
        )
        audit_payload = {
            "subject_id": args.artefact_id,
            "start": spot_diff["start"],
            "end": spot_diff["end"],
            "expected": spot_diff["expected"],
            "receipt_outcome": receipt.get("artefact_outcome") if isinstance(receipt, dict) else None,
            "phase": phase,
            "round_gate_result": "passed" if passed else "failed",
            "run_id": run_id,
            "request_id": request_id,
        }
        try:
            spot_diff["audit_emitted"] = emit_subject_drift(
                client, request_id, audit_payload, source="faba-probe"
            )
        except Exception as exc:
            print(f"[probe] WARNING: subject_drift audit emit failed: {exc}", file=sys.stderr)

    final = {
        "run_id": run_id,
        "request_id": request_id,
        "record_artefact_id": record_artefact_id,
        "workspace": str(workspace),
        "child_exit": "timeout" if timed_out else (proc.returncode if proc is not None else None),
        "content_gate": content_gate or "never-fired",
        "content_gate_reason": (pointer_state or {}).get("gate_reason"),
        "attempts": (pointer_state or {}).get("attempts"),
        "gate": "passed" if passed else "failed",
        "gate_reason": reason,
        "content_check": None if check is None else {"ok": check.ok, "problems": check.problems},
        "receipt": receipt,
        "publish_phase": phase,
        "subject_spot_diff": spot_diff,
    }
    print(json.dumps(final, indent=2))
    release_local_lock(native_lock)
    if passed and spot_diff["verdict"] == "drift":
        return 3
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

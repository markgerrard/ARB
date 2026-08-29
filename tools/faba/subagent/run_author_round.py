"""Drive one subagent-FABA AUTHOR round (Workflow C, owner-directed 2026-07-19).

The author twin of run_probe_round.py: same parent-minted integrity, same
pointer-armed SubagentStop gate (kind="author"), same harness-publish +
receipt discipline — different content. A fresh `faba-author` subagent writes
ONE stage artefact (design/spec/plan/...) to its workspace from POINTERS (the
approved prior-stage artefact + the latest decision record), the gate blocks
its stop until artefact.md passes the light authored-artefact check, and THIS
driver publishes the artefact to ARB Memory under the pre-minted id and gates
on its own receipt. The child session's model is the per-stage author-tier
lever (the faba-author agent def deliberately carries no model pin).

    .venv/bin/python tools/faba/subagent/run_author_round.py \
        --stage design --subject-summary "..." --task "..." \
        [--artefact-id art-...] [--prior-artefact-id art-...] \
        [--prior-record-id art-...] [--prior-record-file path] \
        [--child-model sonnet|opus|fable]

--artefact-id revises an EXISTING artefact (publishes the next version under
that id); omitted, a fresh art-faba-au-<run> id is minted.

Exit 3 means the round passed its own gate but the post-hoc subject version
spot-diff detected drift. Exit 0/1/2 retain their existing meanings.
"""

from __future__ import annotations

import argparse
import contextvars
import functools
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

from faba_launch import (  # noqa: E402
    emit_subject_drift,
    gate_hook_wired,
    observe_subject_version,
    parse_env_file,
    publish_artefact_and_gate,
    subject_spot_diff,
)
from faba_schema import validate_authored_artefact  # noqa: E402
from faba_schema import validate_dispatch_brief  # noqa: E402
from faba_schema import validate_revision_fold  # noqa: E402
from arb_memory.fetch import memory_fetch_by_id  # noqa: E402

POINTER = REPO / ".claude" / "faba-current-round.json"


def content_validator_for_stage(stage: str, *, target_vantage: str | None = None):
    """Select the content gate for an author stage.

    Stage ``brief`` (Slice 1d-iv) uses ``validate_dispatch_brief`` bound to the
    selected target's registry-advertised vantage. All other authored stages
    keep ``validate_authored_artefact``. The vantage is never taken from
    caller-supplied brief prose — it is the frozen target record's
    ``worker_vantage`` (or an arm-time stand-in when the target is already
    known).
    """
    if stage == "brief":
        if not isinstance(target_vantage, str) or not target_vantage.strip():
            raise ValueError(
                "stage 'brief' requires a nonblank target_vantage "
                "(registry-advertised worker_vantage)"
            )
        vantage = target_vantage.strip()

        def _validate(text: str):
            return validate_dispatch_brief(text, target_vantage=vantage)

        return _validate
    return validate_authored_artefact
SUBJECT_FETCH_TIMEOUT = 10.0
_NATIVE_LOCK = contextvars.ContextVar("faba_native_lock", default=None)

ORCHESTRATOR_PROMPT = """You are the warm-orchestrator half of a FABA Workflow-C author round. Do EXACTLY this and nothing else:

1. Dispatch the `faba-author` agent via the Task tool. The Task prompt must be
   the FABA AUTHOR BRIEF below, verbatim and complete (from "# FABA author
   brief" to the end).
2. When the subagent returns, output its final message verbatim between the
   markers SUBAGENT_FINAL_BEGIN and SUBAGENT_FINAL_END, then stop.
3. Do not re-dispatch, do not author anything yourself, do not touch the
   workspace or the bus.

{brief}
"""


def materialise_author_workspace(
    workspace: Path,
    *,
    stage: str,
    subject_summary: str,
    artefact_id: str,
    prior_artefact_id: str,
    prior_record_id: str,
    task: str,
    prior_record_file: Path | None = None,
    prior_record_bytes: bytes | None = None,
    prior_provenance: dict | None = None,
) -> None:
    """Write author-input.json (+ the materialised prior record, LISTED there —
    same rail as the review driver: an unlisted materialised input invites the
    agent to miss or invent it)."""
    author_input = {
        "stage": stage,
        "subject_summary": subject_summary,
        "artefact_id": artefact_id,
        "prior_artefact_id": prior_artefact_id,
        "prior_record_id": prior_record_id,
        "task": task,
    }
    if prior_record_bytes is None and prior_record_file is not None:
        prior_record_bytes = prior_record_file.read_bytes()
    if prior_record_bytes is not None:
        (workspace / "prior-record.md").write_bytes(prior_record_bytes)
        author_input["prior_record_file"] = "prior-record.md"
        author_input.update(prior_provenance or {})
    (workspace / "author-input.json").write_text(
        json.dumps(author_input, indent=2) + "\n", encoding="utf-8"
    )


def hash_staged_inputs(workspace: Path, source: Path | None) -> dict[str, str]:
    """sha256 the round's input channel right after materialisation.

    Incident 2026-07-19 (v18 fold): the author child overwrote its staged
    source file with its own output; the publish was unaffected but the
    baseline had to be refetched from the store. The in-memory manifest is
    the authoritative copy — the workspace manifest file is forensics only,
    since the child can write anything in its workspace.
    """
    manifest: dict[str, str] = {}
    for name in ("author-input.json", "prior-record.md"):
        path = workspace / name
        if path.exists():
            manifest[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    if source is not None:
        manifest[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
    return manifest


def verify_staged_inputs(manifest: dict[str, str], workspace: Path) -> list[dict]:
    """Re-hash every staged input after the round; report mutations.

    scope="workspace" (the round's actual input channel — mutation breaks the
    published artefact's provenance) vs scope="source" (the operator's staging
    copy outside the workspace — publish provenance intact, baseline poisoned).
    """
    mutations: list[dict] = []
    for path_str, digest in manifest.items():
        try:
            now: str | None = hashlib.sha256(Path(path_str).read_bytes()).hexdigest()
        except OSError:
            now = None
        if now != digest:
            mutations.append(
                {
                    "path": path_str,
                    "scope": "workspace" if path_str.startswith(str(workspace)) else "source",
                    "staged_sha256": digest,
                    "post_round_sha256": now,
                }
            )
    return mutations


AUTHOR_AGENT_TYPE = "faba-author"


def author_dispatch_observed(workspace) -> bool:
    """Did the child orchestrator actually dispatch the `faba-author` agent?

    FABA-1: a round where the orchestrator never delegated fails identically to
    one where the author ran and produced nothing — both end
    ``content_gate: never-fired``. The gate log already carries the
    discriminator, so read it rather than making the operator guess.

    Note the signal is the PRESENCE of a ``faba-author`` entry, not the absence
    of untyped ones: a healthy round can log an untyped SubagentStop alongside
    the author's (observed on the passing run of 2026-07-28). Missing or
    malformed log lines are treated as "not observed" — a partially written log
    must degrade the diagnosis, never crash it.

    ``artefact.md`` counts as proof on its own. An author killed by the turn
    timeout mid-write never emits a SubagentStop, so the gate log alone would
    call it undispatched — which is exactly backwards, and was a real shape:
    the 2026-07-28 fold that hit the then-hardcoded 900s ceiling had written a
    complete 98KB draft. Claiming "no author ever ran" there would send the next
    reader hunting the orchestrator instead of the timeout.
    """
    if (Path(workspace) / "artefact.md").exists():
        return True
    log = Path(workspace) / "gate-log.jsonl"
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        hook_input = entry.get("hook_input")
        if isinstance(hook_input, dict) and hook_input.get("agent_type") == AUTHOR_AGENT_TYPE:
            return True
    return False


def never_fired_reason(content_gate, workspace) -> str:
    """The failure line for a round that published nothing.

    Names WHICH of the two failures happened. The undispatched-author case gets
    its trigger named too: the ``--task`` string is interpolated into the brief
    the child ORCHESTRATOR reads, so a task carrying instructions rather than a
    pointer makes the orchestrator follow them instead of delegating.
    """
    generic = f"content gate {content_gate or 'never-fired'} — nothing published"
    if author_dispatch_observed(workspace):
        return generic
    return (
        f"{generic}; author-never-dispatched — no {AUTHOR_AGENT_TYPE} SubagentStop was "
        "observed, so the child orchestrator did not delegate and no author ever ran. "
        "This is NOT an author that wrote nothing. Most likely cause: the --task string "
        "carries instructions rather than a pointer, and the orchestrator followed them "
        "itself. Pass a short --task naming an instructions file instead."
    )


def build_author_brief(
    contract: str,
    *,
    workspace,
    stage: str,
    subject_summary: str,
    artefact_id: str,
    prior_artefact_id: str,
    prior_record_id: str,
    task: str,
) -> str:
    """The dispatch brief: author contract verbatim + round variables.

    INVARIANT (same as the review brief, panel-faba-v2 grok F1): the variables
    MUST carry the artefact id the parent will publish under — it is the gate's
    scoping token, the only parent-minted string guaranteed to land in the
    author agent's transcript. Pinned by test.
    """
    return f"""# FABA author brief (subagent form)

{contract}

## Round variables

- workspace: {workspace}
- stage: {stage}
- subject summary: {subject_summary}
- prior-stage artefact: {prior_artefact_id}
- prior decision record: {prior_record_id}
- artefact id the parent will publish under: {artefact_id}
- round task: {task}
"""


def _release_native_lock_on_exit(func):
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        entry_lock = _NATIVE_LOCK.get()
        try:
            return func(*args, **kwargs)
        finally:
            native_lock = _NATIVE_LOCK.get()
            if native_lock is not None and native_lock is not entry_lock:
                from bridge_round import release_local_lock
                _NATIVE_LOCK.set(entry_lock)
                release_local_lock(native_lock)
    return wrapped


@_release_native_lock_on_exit
def main(argv=None, *, fetch_by_id=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="native", help="native | bridge:<target-id>")
    parser.add_argument("--run-id", default=None, help="orchestrator-visible round id")
    parser.add_argument("--lease-ttl", type=int, default=None)
    parser.add_argument("--turn-timeout", type=int, default=900)
    parser.add_argument("--stage", required=True, help="design | spec | plan | <free label>")
    parser.add_argument(
        "--subject-summary",
        required=True,
        help="one line: WHAT is being authored (lands in author-input.json)",
    )
    parser.add_argument("--task", required=True)
    parser.add_argument(
        "--artefact-id",
        default=None,
        help="existing artefact id to revise (next version); omitted = mint a fresh id",
    )
    parser.add_argument("--prior-artefact-id", default="none")
    parser.add_argument("--prior-record-id", default="none")
    parser.add_argument(
        "--prior-record-file",
        type=Path,
        default=None,
        help=(
            "arm-time HEAD snapshot or forensic-staging override. Fresh artefact: "
            "the decision record whose findings the draft must address. Revision "
            "(--artefact-id): an override of the automatic HEAD fetch; a divergent "
            "override arms with a warning but cannot publish as a revision"
        ),
    )
    parser.add_argument("--env-file", type=Path, default=REPO / ".env.oi-r26")
    parser.add_argument("--contract", type=Path, default=FABA / "author-contract.md")
    parser.add_argument("--child-model", default="sonnet", help="the per-stage author-tier lever")
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument(
        "--target-vantage",
        default=None,
        help=(
            "registry-advertised worker_vantage of the dispatch target; required "
            "when --stage brief so validate_dispatch_brief binds demonstrations "
            "to the selected seat (never caller prose)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        stage_validator = content_validator_for_stage(
            args.stage, target_vantage=args.target_vantage
        )
    except ValueError as exc:
        print(f"[author] {exc}; refusing", file=sys.stderr)
        return 2

    if args.engine == "native":
        gate_problem = gate_hook_wired(REPO)
        if gate_problem is not None:
            print(f"[author] {gate_problem}; refusing", file=sys.stderr)
            return 2
    elif not args.engine.startswith("bridge:") or not args.engine.removeprefix("bridge:"):
        parser.error("--engine must be native or bridge:<target-id>")

    # Revision guard (incident: art-81438f2f5a5c4955 store v16, 2026-07-19): a
    # revision fold requires a reachable store or an override file — the
    # tool-bounded author cannot read ARB Memory itself.
    # --prior-artefact-id is a pointer for provenance, not an input channel.
    # F14 (r5 record art-faba-sa-d176adf6): presence alone is not enough — the
    # staged body is checked too, so /dev/null, an empty file, or a stray
    # document is refused before any workspace, pointer, or bus state exists.
    prior_bytes = None
    prior_provenance = None
    if args.prior_record_file is not None:
        try:
            prior_bytes = args.prior_record_file.read_bytes()
            prior_body = prior_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            parser.error(f"--prior-record-file {args.prior_record_file}: unreadable ({exc})")
        if not prior_body.strip():
            parser.error(
                f"--prior-record-file {args.prior_record_file} is empty — an empty "
                "prior body cannot ground a draft (v16 incident class, F14)"
            )
        if args.artefact_id:
            # allow_trailing_markup: the prior is the store as it IS, fetched
            # verbatim — a historical tail blemish (v17/v19 class) must not
            # block the revision that exists to remove it. Output gates keep
            # the tail check on.
            prior_check = validate_authored_artefact(prior_body, allow_trailing_markup=True)
            if not prior_check.ok:
                parser.error(
                    "--prior-record-file does not hold a publishable artefact body — "
                    "a revision must be grounded in the CURRENT body as published "
                    "(F14, v16 incident class): " + "; ".join(prior_check.problems)
                )
        prior_provenance = {
            "prior_source": "operator-file",
            "prior_sha256": hashlib.sha256(prior_bytes).hexdigest(),
        }

    # PF1 containment, driver side — identical to the review driver: parse the
    # credential locally, never into the child's environment. pop_publish_env
    # strips BOTH publish creds from os.environ; we consume only the memory URL.
    from agent_redis_bridge.dispatch_authority import pop_publish_env

    bus_env = parse_env_file(args.env_file) if args.env_file.exists() else {}
    redis_url = pop_publish_env(os.environ).get("ARB_MEMORY_REDIS_URL") or bus_env.get("ARB_MEMORY_REDIS_URL")
    if not redis_url:
        print("[author] ARB_MEMORY_REDIS_URL not available — no bus, no gate; refusing", file=sys.stderr)
        return 2

    fetch_by_id = fetch_by_id or memory_fetch_by_id
    client = redis.from_url(redis_url, decode_responses=True)
    try:
        subject_fetch_timeout = float(
            os.environ.get("FABA_SUBJECT_FETCH_TIMEOUT", str(SUBJECT_FETCH_TIMEOUT))
        )
    except ValueError:
        subject_fetch_timeout = SUBJECT_FETCH_TIMEOUT
    subject_start = {"value": "absent", "outcome": "fresh"}

    # Fetch and validate HEAD before any workspace, pointer, child, or
    # write-intent state exists. Re-running the driver is the retry policy.
    if args.artefact_id and args.prior_record_file is None:
        try:
            fetched = fetch_by_id(client, args.artefact_id, timeout=subject_fetch_timeout)
        except Exception:
            # Anything memory_fetch_by_id did not itself classify (it handles redis.RedisError
            # and names the leg). We do not know which leg failed, or whether the request went
            # out — so this is a driver fault, NOT the store reporting exhaustion.
            fetched = {"outcome": "driver_error"}
        outcome = fetched.get("outcome") if isinstance(fetched, dict) else None
        if outcome == "ok":
            content = fetched.get("content")
            version = fetched.get("version")
            if (
                not isinstance(content, str)
                or not isinstance(version, int)
                or isinstance(version, bool)
                or version < 1
                or fetched.get("artefact_id") != args.artefact_id
            ):
                outcome = "malformed"
            else:
                prior_bytes = content.encode("utf-8")
                prior_body = content
                prior_provenance = {
                    "prior_source": "store-fetch",
                    "store_version": version,
                    "fetch_request_id": fetched.get("request_id"),
                    "prior_sha256": hashlib.sha256(prior_bytes).hexdigest(),
                }
                subject_start = {"value": version, "outcome": "ok"}
        if outcome != "ok":
            failure_class = "timeout" if fetched is None else outcome or "malformed"
            print(
                "[author] revision requires a reachable store or --prior-record-file; "
                f"arm-time fetch refused: {failure_class}",
                file=sys.stderr,
            )
            return 2

        prior_check = validate_authored_artefact(prior_body, allow_trailing_markup=True)
        if not prior_check.ok:
            print(
                "[author] arm-time fetch refused: malformed (HEAD does not hold a "
                "publishable artefact body): " + "; ".join(prior_check.problems),
                file=sys.stderr,
            )
            return 2
    elif args.artefact_id and args.prior_record_file is not None:
        # The override wins for staging. Reachable HEAD is advisory only.
        try:
            fetched = fetch_by_id(client, args.artefact_id, timeout=subject_fetch_timeout)
        except Exception:
            fetched = None
        if isinstance(fetched, dict) and fetched.get("outcome") == "ok":
            version = fetched.get("version")
            if isinstance(version, int) and not isinstance(version, bool) and version >= 1:
                subject_start = {"value": version, "outcome": "ok"}
            else:
                subject_start = {"value": "unobserved", "outcome": "malformed"}
            store_content = fetched.get("content")
            if isinstance(store_content, str):
                store_sha = hashlib.sha256(store_content.encode("utf-8")).hexdigest()
                prior_sha = prior_provenance["prior_sha256"]
                if store_sha != prior_sha:
                    print(
                        "[author] WARNING: override differs from store HEAD "
                        f"(prior_sha256={prior_sha}, store_sha256={store_sha}, "
                        f"store_version={fetched.get('version')})",
                        file=sys.stderr,
                    )
        else:
            failure_class = "timeout" if fetched is None else (
                fetched.get("outcome") if isinstance(fetched, dict) else "malformed"
            )
            subject_start = {"value": "unobserved", "outcome": failure_class or "malformed"}

    if args.engine == "native" and POINTER.exists():
        print(f"[author] pointer {POINTER} already exists — another round armed; refusing", file=sys.stderr)
        return 2

    run_id = args.run_id or uuid.uuid4().hex[:8]
    request_id = f"faba-au-{args.stage}-{run_id}"
    artefact_id = args.artefact_id or f"art-faba-au-{run_id}"

    if args.engine.startswith("bridge:"):
        from bridge_round import BridgeRoundError, lease_ttl_lower_bound, run_bridge_round

        target_id = args.engine.removeprefix("bridge:")
        with tempfile.TemporaryDirectory(prefix="faba-au-stage-") as stage_dir:
            staged = Path(stage_dir)
            materialise_author_workspace(
                staged, stage=args.stage, subject_summary=args.subject_summary,
                artefact_id=artefact_id, prior_artefact_id=args.prior_artefact_id,
                prior_record_id=args.prior_record_id, task=args.task,
                prior_record_file=args.prior_record_file, prior_record_bytes=prior_bytes,
                prior_provenance=prior_provenance,
            )
            staged_files = {
                path.name: path.read_bytes() for path in staged.iterdir() if path.is_file()
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
            return build_author_brief(
                contract, workspace=workspace, stage=args.stage,
                subject_summary=args.subject_summary, artefact_id=artefact_id,
                prior_artefact_id=args.prior_artefact_id,
                prior_record_id=args.prior_record_id, task=args.task,
            ) + "\nReturn-channel rule: your reply must contain ONLY the single FABA_EXIT line.\n"

        def validate_bridge(text):
            check = stage_validator(text)
            problems = list(check.problems)
            if not problems and args.artefact_id and prior_bytes is not None:
                problems.extend(validate_revision_fold(text, prior_bytes.decode("utf-8")).problems)
            return problems

        def publish_bridge(workspace):
            return publish_artefact_and_gate(
                redis_url, workspace=workspace, artefact_id=artefact_id,
                request_id=request_id, author=f"faba-au-{args.stage}",
                receipt_timeout=60.0, revision=bool(args.artefact_id),
                fetch_by_id=fetch_by_id,
                validate=stage_validator,
            )

        try:
            bridge_result, publish_result = run_bridge_round(
                target_id=target_id, env_file=args.env_file, run_id=run_id,
                artefact_lock_id=artefact_id, base_oid=base_oid, lease_ttl=ttl,
                turn_timeout=args.turn_timeout, staged_files=staged_files,
                output_name="artefact.md", expected_exit_key="artefact_id",
                expected_exit_id=artefact_id, make_brief=make_bridge_brief,
                validate=validate_bridge, publish=publish_bridge,
            )
        except BridgeRoundError as exc:
            print(f"[author] bridge round refused: {exc}", file=sys.stderr)
            return 2
        passed = bridge_result.passed
        receipt = publish_result[2] if publish_result else None
        check = publish_result[3] if publish_result else None
        phase = getattr(publish_result, "phase", "not_enqueued") if publish_result else "not_enqueued"
        subject_end = observe_subject_version(fetch_by_id, client, artefact_id, subject_fetch_timeout)
        spot_diff = subject_spot_diff(
            subject_start, subject_end, phase=phase, receipt=receipt, probe=False,
            refusal_cause=getattr(publish_result, "refusal_cause", None) if publish_result else None,
        )
        final = {
            "run_id": run_id, "request_id": request_id, "artefact_id": artefact_id,
            "workspace": str(bridge_result.workspace) if bridge_result.workspace else None,
            "stage": args.stage, "engine_family": bridge_result.engine_family,
            "bounce_mode": bridge_result.bounce_mode, "lease_id": bridge_result.lease_id,
            "base_oid": bridge_result.base_oid,
            "thread_id": bridge_result.thread_id, "attempts": bridge_result.attempts,
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

    workspace = Path(tempfile.mkdtemp(prefix=f"faba-au-{args.stage}-"))
    from bridge_round import BridgeRoundError, acquire_local_lock
    try:
        _NATIVE_LOCK.set(acquire_local_lock(artefact_id))
    except BridgeRoundError as exc:
        print(f"[author] {exc}; refusing", file=sys.stderr)
        return 2

    materialise_author_workspace(
        workspace,
        stage=args.stage,
        subject_summary=args.subject_summary,
        artefact_id=artefact_id,
        prior_artefact_id=args.prior_artefact_id,
        prior_record_id=args.prior_record_id,
        task=args.task,
        prior_record_file=args.prior_record_file,
        prior_record_bytes=prior_bytes,
        prior_provenance=prior_provenance,
    )

    staged_manifest = hash_staged_inputs(workspace, args.prior_record_file)
    (workspace / "staged-input-manifest.json").write_text(
        json.dumps(staged_manifest, indent=2) + "\n", encoding="utf-8"
    )

    brief = build_author_brief(
        args.contract.read_text(encoding="utf-8"),
        workspace=workspace,
        stage=args.stage,
        subject_summary=args.subject_summary,
        artefact_id=artefact_id,
        prior_artefact_id=args.prior_artefact_id,
        prior_record_id=args.prior_record_id,
        task=args.task,
    )

    # Arm the gate BEFORE the child exists. kind="author" switches the content
    # gate to artefact.md + the light check; record_artefact_id stays the field
    # name for the publish id — it is the scoping token for BOTH kinds.
    POINTER.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "record_artefact_id": artefact_id,
                "kind": "author",
                "revision": bool(args.artefact_id),
                "workspace": str(workspace),
                "stage": args.stage,
                "attempts": 0,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"[author] run={run_id} request_id={request_id} artefact={artefact_id}\n"
        f"[author] workspace={workspace} stage={args.stage} child_model={args.child_model}",
        file=sys.stderr,
        flush=True,
    )

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
            timeout=args.turn_timeout,
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
            if pointer_state.get("request_id") == request_id:
                (workspace / "gate-pointer-final.json").write_text(
                    json.dumps(pointer_state, indent=2) + "\n", encoding="utf-8"
                )
                POINTER.unlink()
            else:
                print(
                    f"[author] pointer holds foreign round {pointer_state.get('request_id')!r}"
                    " — leaving it armed, not ours to disarm",
                    file=sys.stderr,
                )
                pointer_state = None

    if proc is not None:
        (workspace / "child-stdout.json").write_text(proc.stdout or "", encoding="utf-8")
        (workspace / "child-stderr.txt").write_text(proc.stderr or "", encoding="utf-8")

    content_gate = (pointer_state or {}).get("gate")

    # Input-mutation incident (2026-07-19, v18 fold): verify the staged inputs
    # survived the round untouched. A mutated WORKSPACE input breaks the
    # published artefact's provenance — refuse to publish. A mutated SOURCE
    # file leaves the publish sound but poisons the operator's staging copy —
    # warn loudly and record it.
    input_mutations = verify_staged_inputs(staged_manifest, workspace)
    workspace_mutations = [m for m in input_mutations if m["scope"] == "workspace"]
    for m in input_mutations:
        print(
            f"[author] staged input MUTATED during round ({m['scope']}): {m['path']}",
            file=sys.stderr,
        )

    if content_gate == "passed" and workspace_mutations:
        passed, reason, receipt, check = (
            False,
            "staged workspace input mutated by the child — provenance broken, "
            "nothing published: " + ", ".join(m["path"] for m in workspace_mutations),
            None,
            None,
        )
    elif content_gate == "passed":
        publish_result = publish_artefact_and_gate(
            redis_url,
            workspace=workspace,
            artefact_id=artefact_id,
            request_id=request_id,
            author=f"faba-au-{args.stage}",
            receipt_timeout=60.0,
            revision=bool(args.artefact_id),
            fetch_by_id=fetch_by_id,
            validate=stage_validator,
        )
        passed, reason, receipt, check = publish_result
    else:
        passed, reason, receipt, check = (
            False,
            never_fired_reason(content_gate, workspace),
            None,
            None,
        )

    if content_gate != "passed" or workspace_mutations:
        phase = "not_enqueued"
    else:
        phase = getattr(
            publish_result,
            "phase",
            "receipt_confirmed" if receipt is not None else "receipt_unknown",
        )
    subject_end = observe_subject_version(
        fetch_by_id, client, artefact_id, subject_fetch_timeout
    )
    spot_diff = subject_spot_diff(
        subject_start,
        subject_end,
        phase=phase,
        receipt=receipt,
        probe=False,
        refusal_cause=getattr(publish_result, "refusal_cause", None)
        if content_gate == "passed" and not workspace_mutations
        else None,
    )
    if spot_diff["verdict"] == "drift":
        print(
            "[author] SUBJECT DRIFT: "
            f"artefact={artefact_id} start={spot_diff['start']} end={spot_diff['end']} "
            f"expected={spot_diff['expected']} phase={phase}",
            file=sys.stderr,
        )
        audit_payload = {
            "subject_id": artefact_id,
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
                client, request_id, audit_payload, source="faba-author"
            )
        except Exception as exc:
            print(f"[author] WARNING: subject_drift audit emit failed: {exc}", file=sys.stderr)

    final = {
        "run_id": run_id,
        "request_id": request_id,
        "artefact_id": artefact_id,
        "workspace": str(workspace),
        "stage": args.stage,
        "child_exit": "timeout" if timed_out else (proc.returncode if proc is not None else None),
        "content_gate": content_gate or "never-fired",
        "content_gate_reason": (pointer_state or {}).get("gate_reason"),
        "attempts": (pointer_state or {}).get("attempts"),
        "gate": "passed" if passed else "failed",
        "gate_reason": reason,
        "content_check": None if check is None else {"ok": check.ok, "problems": check.problems},
        "input_integrity": {"ok": not input_mutations, "mutations": input_mutations},
        "receipt": receipt,
        "publish_phase": phase,
        "subject_spot_diff": spot_diff,
    }
    print(json.dumps(final, indent=2))
    if passed and spot_diff["verdict"] == "drift":
        return 3
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

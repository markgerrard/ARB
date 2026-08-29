"""FABA per-round launch harness (prototype, contract v2).

The parent shells this script; the process IS the round. Lifecycle:

    bootstrap from pointers -> root Agent SDK session runs the round
    -> agent leaves decision-record.md in the workspace (it never publishes)
    -> harness validates the record (schema + binding + prior-open coverage),
       publishes it ITSELF over the ARB Memory bus, and gates on its own
       receipt round-trip -> prints exit line -> dies

Process death is the automatable /clear: context lifetime is bound to round
lifetime by construction. Exit 0 only when a Status: ok record is verified
ingested (integrity invariant); a Status: failed record is published for the
audit trail but the round fails. A missing/invalid record is a failed round,
indistinguishable from a crash — the parent re-dispatches (crash equivalence).

The gate never trusts the agent: ids are minted HERE, the bus credential is
parsed-not-exported (the child env never carries it), and content is validated
from disk — a perfect FABA_EXIT line cannot mask a missing or garbage record.

Run with the repo venv so the SDK and redis are importable:

    .venv/bin/python tools/faba/faba_launch.py \
        --artefact-id art-81438f2f5a5c4955 --round 1 \
        --task "Summarise the artefact's open items as findings."
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

VARIABLES_MARKER = "<!-- ROUND VARIABLES BELOW"
CONTRACT_MARKER = "<!-- FABA ROUND CONTRACT -->"
GATE_SETTINGS_PATHS = (
    ".claude/settings.json",
    ".claude/settings.local.json",
)


def read_text_exact(path: Path) -> str:
    """Read text without universal-newline translation (CRLF stays visible).

    open(newline="") rather than Path.read_text(newline=""): the read_text
    kwarg needs Python >= 3.13 and FABA drivers run on 3.12 venvs."""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def gate_hook_wired(repo: Path) -> str | None:
    """Return a refusal reason unless this checkout wires the FABA stop gate."""
    inspected = [repo / rel for rel in GATE_SETTINGS_PATHS]
    inspected_text = ", ".join(str(path) for path in inspected)
    prefix = (
        f"FABA gate wiring check failed; inspected {inspected_text}; "
        "user-level settings were not checked"
    )
    found_file = False
    wired = False
    file_problems: list[str] = []
    for path in inspected:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as exc:
            found_file = True
            file_problems.append(f"{path} is unreadable ({exc})")
            continue
        found_file = True
        try:
            settings = json.loads(raw)
        except json.JSONDecodeError as exc:
            file_problems.append(f"{path} contains invalid JSON ({exc})")
            continue
        hooks_config = settings.get("hooks") if isinstance(settings, dict) else None
        subagent_stop = (
            hooks_config.get("SubagentStop", []) if isinstance(hooks_config, dict) else []
        )
        if not isinstance(subagent_stop, list):
            continue
        for matcher in subagent_stop:
            hooks = matcher.get("hooks", []) if isinstance(matcher, dict) else []
            if not isinstance(hooks, list):
                continue
            for hook in hooks:
                command = hook.get("command") if isinstance(hook, dict) else None
                if isinstance(command, str) and "subagent_stop_gate.py" in command:
                    wired = True
    if wired:
        for problem in file_problems:
            print(f"[faba-warning] {problem}", file=sys.stderr)
        return None
    problem = (
        "both settings files are missing"
        if not found_file
        else "no qualifying SubagentStop hook was found"
    )
    details = "; ".join([*file_problems, problem])
    return f"{prefix}; {details}. See tools/faba/subagent/README.md#Wiring for the wiring recipe."


@dataclass(frozen=True)
class PublishGateResult:
    """Legacy four-value gate result plus an explicit publish phase.

    Iteration and indexing deliberately expose only the historical
    ``(passed, reason, receipt, check)`` shape; callers that need spot-diff
    semantics read ``.phase``.
    """

    passed: bool
    reason: str
    receipt: dict | None
    check: object
    phase: str
    refusal_cause: Literal["fresh_id_already_exists"] | None = None

    def __iter__(self):
        return iter((self.passed, self.reason, self.receipt, self.check))

    def __len__(self):
        return 4

    def __getitem__(self, index):
        return (self.passed, self.reason, self.receipt, self.check)[index]


def observe_subject_version(fetch_by_id, client, artefact_id: str, timeout: float) -> dict:
    """Fetch one typed HEAD-version observation without raising into a round."""
    try:
        fetched = fetch_by_id(client, artefact_id, timeout=timeout)
    except Exception as exc:
        return {"value": "unobserved", "outcome": type(exc).__name__}
    if not isinstance(fetched, dict):
        return {"value": "unobserved", "outcome": "timeout" if fetched is None else "malformed"}
    outcome = fetched.get("outcome")
    if outcome == "not_found":
        return {"value": "absent", "outcome": "not_found"}
    version = fetched.get("version")
    if (
        outcome == "ok"
        and fetched.get("artefact_id") == artefact_id
        and isinstance(version, int)
        and not isinstance(version, bool)
        and version >= 1
    ):
        return {"value": version, "outcome": "ok"}
    return {"value": "unobserved", "outcome": outcome or "malformed"}


def subject_spot_diff(
    start: dict,
    end: dict,
    *,
    phase: str,
    receipt: dict | None,
    probe: bool,
    refusal_cause: Literal["fresh_id_already_exists"] | None = None,
) -> dict:
    """Compute a typed post-hoc subject-version verdict for either driver."""
    result = {
        "start": start["value"],
        "end": end["value"],
        "expected": "unobserved",
        "verdict": "unobserved",
        "start_outcome": start["outcome"],
        "end_outcome": end["outcome"],
        "audit_emitted": False,
    }
    if phase not in {"not_enqueued", "receipt_confirmed", "receipt_unknown"}:
        raise ValueError(f"unknown publish phase: {phase!r}")
    if not probe and phase == "receipt_unknown":
        result["expected"] = "indeterminate"
        result["verdict"] = "indeterminate"
        return result
    if start["value"] == "unobserved" or end["value"] == "unobserved":
        return result

    start_value = start["value"]
    end_value = end["value"]
    if probe:
        # The receipt_unknown -> indeterminate rule is author-only: probe rounds
        # publish the decision RECORD, never the subject being spot-diffed.
        expected = start_value
    elif phase == "not_enqueued":
        expected = start_value
        if (
            refusal_cause == "fresh_id_already_exists"
            and start_value == "absent"
            and end_value != "absent"
        ):
            result["expected"] = "absent"
            result["verdict"] = "pre-existing-id"
            return result
    elif phase == "receipt_confirmed":
        delta = 1 if isinstance(receipt, dict) and receipt.get("artefact_outcome") == "stored" else 0
        expected = delta if start_value == "absent" else start_value + delta
    else:
        raise ValueError(f"unknown author publish phase: {phase!r}")

    result["expected"] = expected
    result["verdict"] = "clean" if end_value == expected else "drift"
    return result


def emit_subject_drift(client, request_id: str, payload: dict, *, source: str) -> bool:
    """Persist a drift signal on the audit stream; callers handle failure softly."""
    from arb_memory.audit import AuditRun

    AuditRun(client, request_id, prefix=getattr(client, "prefix", "")).emit(
        source, "subject_drift", payload
    )
    return True


def compose_contract(template: str, contract: str) -> str:
    """Insert the shared round contract (round-contract.md) at the template's
    contract marker. One instruction surface for both FABA forms: the SDK
    template composes it here; the subagent driver embeds the same file in its
    dispatch brief. The marker must sit in the invariant head so the rendered
    sha covers the composed contract.
    """
    head, sep, _ = template.partition(VARIABLES_MARKER)
    if CONTRACT_MARKER not in head:
        raise ValueError(f"template missing contract marker {CONTRACT_MARKER!r} in invariant head")
    if not sep:
        raise ValueError(f"template missing variables marker {VARIABLES_MARKER!r}")
    return template.replace(CONTRACT_MARKER, contract.strip(), 1)


# Only Memory-bus credentials may enter the process env from the harness env file.
# The file often also carries AGENT_REDIS_* bridge-bus selection vars, and those poison
# every child dispatch: agent-dispatch/go-client give process env precedence over their
# --env-file, so a leaked AGENT_REDIS_HOST silently reroutes seat envelopes to a bus no
# daemon is watching (r3 incident, 2026-07-18 — five envelopes stranded on the oi-r26
# Valkey while the bridge-dev seats listened on localhost).
ENV_FILE_ALLOWED_PREFIXES = ("ARB_MEMORY_",)


def parse_env_file(path: Path) -> dict[str, str]:
    """Read only ENV_FILE_ALLOWED_PREFIXES keys — parse, never export.

    PF1 containment: the SDK child inherits this process's os.environ, so the
    bus credential must never enter it. The harness holds the credential in a
    local dict and does its own publishing (the agent writes the record; the
    parent publishes and gates)."""
    parsed: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not key.startswith(ENV_FILE_ALLOWED_PREFIXES):
            continue
        parsed[key] = value
    return parsed


def load_env_file(path: Path) -> dict[str, str]:
    """Apply only ENV_FILE_ALLOWED_PREFIXES keys to os.environ; explicit env still wins.

    For harness-side processes only (the subagent gate hook, drivers) — the SDK
    launch path uses parse_env_file so the child never inherits the credential."""
    applied: dict[str, str] = {}
    for key, value in parse_env_file(path).items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def render_bootstrap(template: str, variables: dict[str, str]) -> tuple[str, str]:
    """Render {{placeholders}} in the variables tail; the invariant prefix is untouched.

    Returns (prompt, invariant_sha). The sha covers everything above the marker —
    the cache-stable block; a changed sha means the template version moved.

    PF5: a placeholder in the invariant head is a template bug — raise, never
    emit it silently. PF6: unresolved-variable detection runs on the ORIGINAL
    tail's placeholder set, so `{{...}}` text inside a substituted VALUE (e.g. a
    task quoting a placeholder) no longer aborts the launch.
    """
    import re

    head, sep, tail = template.partition(VARIABLES_MARKER)
    if not sep:
        raise ValueError(f"template missing variables marker {VARIABLES_MARKER!r}")
    head_placeholders = re.findall(r"\{\{(\w+)\}\}", head)
    if head_placeholders:
        raise ValueError(f"placeholders in invariant head (never rendered): {head_placeholders}")
    invariant_sha = hashlib.sha256(head.encode("utf-8")).hexdigest()
    needed = set(re.findall(r"\{\{(\w+)\}\}", tail))
    missing = sorted(needed - set(variables))
    if missing:
        raise ValueError(f"unresolved template variables: {missing}")
    for key, value in variables.items():
        tail = tail.replace("{{" + key + "}}", value)
    return head + sep + tail, invariant_sha


def parse_exit_line(text: str) -> dict | None:
    """Extract the agent's FABA_EXIT json from its final message. None if absent/mangled."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("FABA_EXIT "):
            try:
                return json.loads(line[len("FABA_EXIT ") :])
            except json.JSONDecodeError:
                return None
    return None


def gate_decision(receipt: dict | None, record_artefact_id: str) -> tuple[bool, str]:
    """The ingestion half of the invariant: record verifiably ingested under the
    expected id. The content half is faba_schema.validate_decision_record — the
    harness runs BOTH, on a record it publishes itself (the agent never holds
    the bus credential, so this receipt is the harness's own round-trip, not an
    agent claim — PF1)."""
    if receipt is None:
        return False, "no receipt before timeout (record not ingested — failed round)"
    outcome = receipt.get("artefact_outcome")
    if outcome not in {"stored", "deduped"}:
        return False, f"receipt outcome {outcome!r}"
    if receipt.get("artefact_id") != record_artefact_id:
        return False, f"receipt artefact_id {receipt.get('artefact_id')!r} != expected {record_artefact_id!r}"
    return True, f"record {record_artefact_id} v{receipt.get('version')} {outcome}"


def publish_and_gate(
    redis_url: str,
    *,
    workspace: Path,
    record_artefact_id: str,
    request_id: str,
    author: str,
    prior_open_ids: list[str] | None,
    receipt_timeout: float,
    expected_round: int | None = None,
    expected_subject: str | None = None,
) -> PublishGateResult:
    """Validate the round's record, then publish it AS THE HARNESS and gate on
    the harness's own receipt round-trip (PF1 content-half: the child neither
    publishes nor holds the credential in any env we hand it; PF2: the poll
    starts immediately after our own XADD, inside the receipt TTL by
    construction).

    Gate matrix (panel-faba-v2 codex F1):
      - no record / schema / binding / coverage problems -> fail, publish NOTHING
      - record valid, Status: failed -> PUBLISH (crash-equivalence audit trail),
        but the round FAILS — a failed record is never a successful round
      - record valid, Status: ok -> publish, pass iff the receipt verifies

    Returns a four-value-compatible PublishGateResult with ``phase`` exposed."""
    import redis as redis_lib

    from arb_memory import bus
    from faba_record import poll_receipt
    from faba_schema import validate_decision_record

    record_path = workspace / "decision-record.md"
    if not record_path.exists():
        return PublishGateResult(False, "no decision-record.md in workspace (agent produced nothing — failed round)", None, None, "not_enqueued")
    content = read_text_exact(record_path)
    check = validate_decision_record(
        content, prior_open_ids, expected_round=expected_round, expected_subject=expected_subject
    )
    if not check.ok:
        return PublishGateResult(False, "record failed schema/binding/coverage: " + "; ".join(check.problems), None, check, "not_enqueued")

    client = redis_lib.from_url(redis_url, decode_responses=True)
    # Clean slate on the deterministic receipt key. This NARROWS the forgery
    # window (a receipt pre-seeded mid-session dies here); it does not CLOSE it:
    # session end is not process-group death, so a detached credential-harvesting
    # descendant could re-seed after this DEL (panel-faba-v2 cold-opus P2-1 /
    # codex F3). Full closure is PF3-class containment — deferred by owner
    # 2026-07-18 — or a store read-back once a fetch-by-id path exists.
    try:
        client.delete(bus.write_result_key(request_id))
    except Exception as exc:
        return PublishGateResult(
            False, f"record publish refused before enqueue: {type(exc).__name__}",
            None, check, "not_enqueued"
        )
    try:
        bus.memory_write(
            client,
            artefact={
                "artefact_id": record_artefact_id,
                "content": content,
                "mime": "text/markdown",
                "source": "faba",
                "author": author,
            },
            hints=[],
            request_id=request_id,
        )
    except Exception as exc:
        return PublishGateResult(
            False, f"record publish refused before enqueue: {type(exc).__name__}",
            None, check, "not_enqueued"
        )
    try:
        receipt = poll_receipt(request_id, receipt_timeout, client=client)
    except Exception as exc:
        return PublishGateResult(
            False, f"record receipt unknown after enqueue attempt: {type(exc).__name__}",
            None, check, "receipt_unknown"
        )
    ingested, reason = gate_decision(receipt, record_artefact_id)
    phase = "receipt_confirmed" if receipt is not None else "receipt_unknown"
    if ingested and check.status == "failed":
        return PublishGateResult(False, f"record published for audit ({reason}) but carries Status: failed — failed round", receipt, check, phase)
    return PublishGateResult(ingested, reason, receipt, check, phase)


def publish_artefact_and_gate(
    redis_url: str,
    *,
    workspace: Path,
    artefact_id: str,
    request_id: str,
    author: str,
    receipt_timeout: float,
    revision: bool = False,
    fetch_by_id=None,
    validate=None,
) -> PublishGateResult:
    """Author-round twin of publish_and_gate (Workflow C, owner-directed
    2026-07-19): validate workspace artefact.md with the light authored-artefact
    check, then publish it AS THE HARNESS under the parent-minted artefact id
    and gate on the harness's own receipt round-trip. Same PF1/PF2 posture as
    the record path: the child never publishes and never holds the credential;
    the poll starts immediately after our own XADD.

    Validation runs BEFORE any bus access — an invalid artefact publishes
    nothing and needs no redis to say so.

    `validate` (Slice 1d-iv): optional ``Callable[[str], RecordCheck]``. Defaults
    to ``validate_authored_artefact`` so existing author stages are unchanged.
    Dispatch-brief publish selects ``validate_dispatch_brief`` (bound to the
    frozen target vantage) instead of duplicating the publish/receipt path.

    `revision=True` (F14 route (c) hygiene tier — panel
    panel-f14c-design-20260720T033218Z-5ec74f, owner-scoped 2026-07-20)
    additionally requires the workspace prior-record.md to be a publishable
    body and the artefact to differ from it. The unconditional store read-back
    below then refuses a fresh publish over an existing id and requires an
    exact staged-prior match for revisions. This closes blind overwrite absent
    a concurrent writer; atomic compare-and-set remains PF3-class.

    Returns a four-value-compatible PublishGateResult with ``phase`` exposed."""
    from faba_schema import validate_authored_artefact, validate_revision_fold

    artefact_path = workspace / "artefact.md"
    if not artefact_path.exists():
        return PublishGateResult(False, "no artefact.md in workspace (author produced nothing — failed round)", None, None, "not_enqueued")
    content = read_text_exact(artefact_path)
    validate_fn = validate or validate_authored_artefact
    check = validate_fn(content)
    if not check.ok:
        return PublishGateResult(False, "artefact failed the authored-artefact check: " + "; ".join(check.problems), None, check, "not_enqueued")
    if revision:
        prior_path = workspace / "prior-record.md"
        if not prior_path.exists():
            return PublishGateResult(
                False,
                "revision publish without prior-record.md in the workspace "
                "(F14(c) hygiene: a revision must be grounded in a materialised prior)",
                None,
                None,
                "not_enqueued",
            )
        prior_text = read_text_exact(prior_path)
        prior_check = validate_authored_artefact(prior_text, allow_trailing_markup=True)
        if not prior_check.ok:
            return PublishGateResult(
                False,
                "revision prior-record.md is not a publishable artefact body: "
                + "; ".join(prior_check.problems),
                None,
                prior_check,
                "not_enqueued",
            )
        fold_check = validate_revision_fold(content, prior_text)
        if not fold_check.ok:
            return PublishGateResult(
                False,
                "artefact failed the revision-fold check: " + "; ".join(fold_check.problems),
                None,
                fold_check,
                "not_enqueued",
            )

    import redis as redis_lib

    from arb_memory import bus
    from arb_memory.fetch import memory_fetch_by_id
    from faba_record import poll_receipt

    client = redis_lib.from_url(redis_url, decode_responses=True)
    fetch_by_id = fetch_by_id or memory_fetch_by_id
    fetched = fetch_by_id(client, artefact_id)
    outcome = fetched.get("outcome") if isinstance(fetched, dict) else None
    if outcome not in {"ok", "not_found"}:
        failure_class = "timeout" if fetched is None else outcome or "malformed"
        return PublishGateResult(False, f"artefact store read-back refused: {failure_class}", None, check, "not_enqueued")
    if outcome == "not_found":
        if revision:
            return PublishGateResult(False, "artefact store read-back refused: revision target not_found", None, check, "not_enqueued")
    elif not revision:
        return PublishGateResult(
            False,
            "artefact store read-back refused: fresh artefact id already exists",
            None,
            check,
            "not_enqueued",
            "fresh_id_already_exists",
        )
    else:
        stored_content = fetched.get("content")
        if stored_content != prior_text:
            stored_sha = hashlib.sha256(
                stored_content.encode("utf-8") if isinstance(stored_content, str) else b""
            ).hexdigest()
            prior_sha = hashlib.sha256(prior_text.encode("utf-8")).hexdigest()
            return PublishGateResult(
                False,
                "artefact store read-back refused: stale prior "
                f"(store_version={fetched.get('version')}, store_sha256={stored_sha}, "
                f"prior_sha256={prior_sha})",
                None,
                check,
                "not_enqueued",
            )
    # Same clean-slate DEL as the record path: narrows the pre-seeded-receipt
    # window; atomic closure remains PF3-class compare-and-set.
    try:
        client.delete(bus.write_result_key(request_id))
    except Exception as exc:
        return PublishGateResult(
            False, f"artefact publish refused before enqueue: {type(exc).__name__}",
            None, check, "not_enqueued"
        )
    try:
        bus.memory_write(
            client,
            artefact={
                "artefact_id": artefact_id,
                "content": content,
                "mime": "text/markdown",
                "source": "faba-author",
                "author": author,
            },
            hints=[],
            request_id=request_id,
        )
    except Exception as exc:
        return PublishGateResult(
            False, f"artefact publish refused before enqueue: {type(exc).__name__}",
            None, check, "not_enqueued"
        )
    try:
        receipt = poll_receipt(request_id, receipt_timeout, client=client)
    except Exception as exc:
        return PublishGateResult(
            False, f"artefact receipt unknown after enqueue attempt: {type(exc).__name__}",
            None, check, "receipt_unknown"
        )
    ingested, reason = gate_decision(receipt, artefact_id)
    phase = "receipt_confirmed" if receipt is not None else "receipt_unknown"
    return PublishGateResult(ingested, reason, receipt, check, phase)


async def run_session(prompt: str, manifest: dict, cwd: Path, model: str | None) -> dict:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )

    mcp_servers: dict = {}
    try:
        from agent_redis_bridge.engines.agent_sdk import local_memory_mcp_agent_sdk_servers
        from agent_redis_bridge.local_memory_mcp import local_memory_mcp_config

        mcp_servers = local_memory_mcp_agent_sdk_servers(local_memory_mcp_config())
    except Exception:
        mcp_servers = {}

    options = ClaudeAgentOptions(
        cwd=str(cwd),
        allowed_tools=list(manifest["allowed_tools"]),
        disallowed_tools=list(manifest.get("disallowed_tools", [])),
        permission_mode=manifest["permission_mode"],
        model=model,
        mcp_servers=mcp_servers,
    )

    last_text = ""
    result: dict = {"num_turns": None, "total_cost_usd": None, "session_id": None, "is_error": False}
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    print(f"[faba] tool: {block.name}", file=sys.stderr, flush=True)
                elif isinstance(block, TextBlock):
                    last_text = block.text
        elif isinstance(message, ResultMessage):
            if message.result:
                last_text = message.result
            result["num_turns"] = getattr(message, "num_turns", None)
            result["total_cost_usd"] = getattr(message, "total_cost_usd", None)
            result["session_id"] = getattr(message, "session_id", None)
            result["is_error"] = bool(getattr(message, "is_error", False))
    result["text"] = last_text
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artefact-id", required=True, help="subject artefact the round works on")
    parser.add_argument(
        "--subject-summary",
        required=True,
        help="one line: WHAT the subject artefact is — lands in round-input.json"
        " so a round (and its record's successor) is self-contained without a"
        " materialising brief",
    )
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--prior-record-id", default="none")
    parser.add_argument(
        "--prior-record-file",
        type=Path,
        default=None,
        help="materialised prior decision record; the parent pre-fetches it because the"
        " headless round has no Memory read path unless the local read MCP env is present",
    )
    parser.add_argument("--task", required=True, help="the round task, one paragraph")
    parser.add_argument("--model", default=None, help="SDK model override (default: CLI default)")
    parser.add_argument("--env-file", type=Path, default=None, help="bus credentials env file")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--template", type=Path, default=HERE / "bootstrap_template.md")
    parser.add_argument("--contract", type=Path, default=HERE / "round-contract.md")
    parser.add_argument("--receipt-timeout", type=float, default=120.0)
    parser.add_argument(
        "--prior-basis",
        default=None,
        help="git ref the prior round verified against; enables the reopen consumer "
        "(closed findings whose reopen-if scope changed since then are re-carried)",
    )
    args = parser.parse_args(argv)

    env_file = args.env_file
    if env_file is None and (REPO / ".env.oi-r26").exists():
        env_file = REPO / ".env.oi-r26"
    bus_env = parse_env_file(env_file) if env_file is not None else {}
    # Explicit process env wins, but is also DELIBERATELY not propagated to the
    # child: the credential lives in this local variable only (PF1 containment).
    # pop_publish_env removes BOTH publish creds from os.environ so neither the
    # memory nor the audit URL leaks into the child; we consume only the memory URL.
    from agent_redis_bridge.dispatch_authority import pop_publish_env

    redis_url = pop_publish_env(os.environ).get("ARB_MEMORY_REDIS_URL") or bus_env.get("ARB_MEMORY_REDIS_URL")
    if not redis_url:
        print("[faba] ARB_MEMORY_REDIS_URL not available — no bus, no gate; refusing to launch", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex[:8]
    request_id = f"faba-r{args.round}-{run_id}"
    record_artefact_id = f"art-faba-{run_id}"

    workspace = args.workspace or Path(tempfile.mkdtemp(prefix=f"faba-r{args.round}-"))
    workspace.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest_sha = hashlib.sha256(args.manifest.read_bytes()).hexdigest()

    round_input = {
        "round": args.round,
        "artefact_id": args.artefact_id,
        "subject_summary": args.subject_summary,
        "prior_record_id": args.prior_record_id,
        "record_artefact_id": record_artefact_id,
        "task": args.task,
    }
    prior_open_ids: list[str] = []
    if args.prior_record_file is not None:
        prior_text = read_text_exact(args.prior_record_file)
        (workspace / "prior-record.md").write_bytes(prior_text.encode("utf-8"))
        round_input["prior_record_file"] = "prior-record.md"
        from faba_schema import basis_ref, must_carry_ids

        # Reopen consumer (open item #12): a closed finding whose reopen-if scope
        # matches a path changed since the prior round's basis must be carried
        # again, alongside the still-open ones. The basis is --prior-basis, or —
        # when absent — the commit the prior record recorded (auto-basis, open
        # item #13). No basis at all => the must-carry set is exactly the open
        # findings (unchanged).
        effective_basis = args.prior_basis or basis_ref(prior_text)
        changed_paths: list[str] = []
        if effective_basis is not None:
            from faba_git import changed_paths_since

            changed_paths = changed_paths_since(effective_basis)
        prior_open_ids = must_carry_ids(prior_text, changed_paths)
        round_input["must_carry_ids"] = prior_open_ids
    (workspace / "round-input.json").write_text(
        json.dumps(round_input, indent=2) + "\n", encoding="utf-8"
    )

    prompt, invariant_sha = render_bootstrap(
        compose_contract(
            args.template.read_text(encoding="utf-8"),
            args.contract.read_text(encoding="utf-8"),
        ),
        {
            "workspace": str(workspace),
            "round": str(args.round),
            "artefact_id": args.artefact_id,
            "subject_summary": args.subject_summary,
            "prior_record_id": args.prior_record_id,
            "record_artefact_id": record_artefact_id,
            "task": args.task,
        },
    )

    print(
        f"[faba] run={run_id} request_id={request_id} record={record_artefact_id}\n"
        f"[faba] template_invariant_sha={invariant_sha[:16]} manifest_sha={manifest_sha[:16]}\n"
        f"[faba] workspace={workspace}",
        file=sys.stderr,
        flush=True,
    )

    session = asyncio.run(run_session(prompt, manifest, workspace, args.model))
    # Persist the final message for post-mortem — on a failed round it is the only
    # trace of why the agent stopped (lost in the r3 incident, never again).
    (workspace / "session-final.txt").write_text(session.get("text", "") or "", encoding="utf-8")
    agent_exit = parse_exit_line(session.get("text", ""))

    passed, reason, receipt, check = publish_and_gate(
        redis_url,
        workspace=workspace,
        record_artefact_id=record_artefact_id,
        request_id=request_id,
        author=f"faba-r{args.round}",
        prior_open_ids=prior_open_ids,
        receipt_timeout=args.receipt_timeout,
        expected_round=args.round,
        expected_subject=args.artefact_id,
    )

    final = {
        "run_id": run_id,
        "request_id": request_id,
        "record_artefact_id": record_artefact_id,
        "gate": "passed" if passed else "failed",
        "gate_reason": reason,
        "content_check": None if check is None else {"ok": check.ok, "problems": check.problems},
        "prior_open_ids": prior_open_ids,
        "receipt": receipt,
        "agent_exit": agent_exit,
        "session": {k: session.get(k) for k in ("num_turns", "total_cost_usd", "session_id", "is_error")},
        "workspace": str(workspace),
    }
    print(json.dumps(final, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

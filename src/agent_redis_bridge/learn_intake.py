"""ARB /learn intake CLI.

The module keeps DB and dispatch effects behind injectable callables so the contract can be
tested without a live memory database or bridge seats.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from arb_memory.stance import StanceError, parse_stance


TERMINAL_STATUSES = {"rejected", "promoted"}
READ_SENTINEL_BEGIN = "ARB_JSON_BEGIN"
READ_SENTINEL_END = "ARB_JSON_END"
INDEX_HINT_CHARS = 2000


@dataclasses.dataclass(frozen=True)
class LearnSeat:
    name: str
    engine: str
    target_id: str
    effort: str | None = None
    isolated_worktree: bool = False


SEAT_REGISTRY = {
    "agy": LearnSeat("agy", "agy-print", "agy-bridge-dev"),
    "codex-sol": LearnSeat(
        "codex-sol", "codex", "codex-bridge-dev-example", effort="high"
    ),
    "fable": LearnSeat(
        "fable", "agent-sdk", "asdk-agentredisbridge-dev-example",
        isolated_worktree=True,
    ),
    "glm": LearnSeat("glm", "pi-sdk", "pi-sdk-bridge-dev-example"),
    "grok": LearnSeat("grok", "grok-acp", "grok-agentredisbridge-dev-example"),
}
PANEL_SEATS = {
    "core": ("codex-sol", "agy", "glm"),
    "full": ("codex-sol", "agy", "glm", "fable", "grok"),
}


class LearnError(RuntimeError):
    pass


@dataclasses.dataclass
class SeatVerdict:
    seat: str
    verdict: str
    reason: str
    stance: dict | None


@dataclasses.dataclass
class Deps:
    list_proposals: Callable[[], list[dict]] | None = None
    get_status: Callable[[str], dict | None] | None = None
    store_fn: Callable[[list[dict]], None] | None = None
    dispatch_fn: Callable[[LearnSeat, str, str], str] | None = None
    memory_search_fn: Callable[[str, int], list[dict]] | None = None
    sleep_fn: Callable[[int], None] | None = None


def _deps(deps: Deps | None) -> Deps:
    deps = deps or Deps()
    return Deps(
        list_proposals=deps.list_proposals or list_proposals,
        get_status=deps.get_status or get_status,
        store_fn=deps.store_fn or _run_store,
        dispatch_fn=deps.dispatch_fn or dispatch,
        memory_search_fn=deps.memory_search_fn or memory_search,
        sleep_fn=deps.sleep_fn or time.sleep,
    )


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "proposal")[:40].strip("-") or "proposal"


def mint_id(source_bytes: bytes, first_line: str) -> str:
    return f"learn-{_slug(first_line)}-{hashlib.sha256(source_bytes).hexdigest()[:8]}"


def next_force_id(base_id: str, rows: list[dict]) -> str:
    highest = 0
    pat = re.compile(rf"^{re.escape(base_id)}-r(\d+)$")
    for row in rows:
        m = pat.match(row.get("artefact_id", ""))
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{base_id}-r{highest + 1}"


def parse_header(content: str) -> tuple[dict, str]:
    first, sep, body = content.partition("\n")
    if not sep:
        raise LearnError("learn proposal header missing body separator")
    try:
        header = json.loads(first)
    except json.JSONDecodeError as exc:
        raise LearnError(f"malformed learn proposal header: {exc}") from exc
    if not isinstance(header, dict):
        raise LearnError("malformed learn proposal header: first line is not an object")
    for key in ("status", "target", "proposer", "supersedes", "referent"):
        if key not in header:
            raise LearnError(f"malformed learn proposal header: missing {key}")
    return header, body


def render_header(header: dict) -> str:
    return json.dumps(header, sort_keys=True, separators=(",", ":")) + "\n"


def dedupe_key(content: str) -> str:
    try:
        _header, body = parse_header(content)
    except LearnError:
        body = content
    lines = body.splitlines()
    title = lines[0] if lines else ""
    sample = body[:500]
    return normalize(f"{title}\n{sample}")


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def parse_sentinel_json(stdout: str):
    start = stdout.find(READ_SENTINEL_BEGIN)
    end = stdout.find(READ_SENTINEL_END)
    if start == -1 or end == -1 or end < start:
        raise LearnError("sentinel-framed JSON payload not found")
    payload = stdout[start + len(READ_SENTINEL_BEGIN):end].strip()
    return json.loads(payload)


def _read_script(sql: str) -> str:
    return f"""\
import json, os
import psycopg
conn = psycopg.connect(os.environ["ARB_MEMORY_DSN"])
cur = conn.execute({sql!r})
rows = cur.fetchall()
cols = [d.name for d in cur.description]
print({READ_SENTINEL_BEGIN!r})
print(json.dumps([dict(zip(cols, row)) for row in rows], default=str))
print({READ_SENTINEL_END!r})
"""


def _run_read(sql: str, *, run_fn=None):
    runner = run_fn or subprocess.run
    res = runner(
        [
            "ssh", "arb-prod",
            "cd /home/claude/AgentRedisBridge && "
            "docker compose -f deploy/docker-compose.yml exec -T memory python3 -",
        ],
        input=_read_script(sql),
        text=True,
        check=True,
        capture_output=True,
    )
    return parse_sentinel_json(res.stdout)


def _base_sql(where: str = "") -> str:
    return (
        "SELECT DISTINCT ON (artefact_id) artefact_id, version, content, created_at "
        "FROM artefacts WHERE artefact_id LIKE 'learn-%' "
        f"{where} "
        "ORDER BY artefact_id, version DESC"
    )


def list_proposals(*, run_fn=None) -> list[dict]:
    rows = _run_read(_base_sql(), run_fn=run_fn)
    return [_with_header(r) for r in rows]


def get_status(learn_id: str, *, run_fn=None) -> dict | None:
    safe = learn_id.replace("'", "''")
    rows = _run_read(_base_sql(f"AND artefact_id = '{safe}'"), run_fn=run_fn)
    return _with_header(rows[0]) if rows else None


def _with_header(row: dict) -> dict:
    header, body = parse_header(row["content"])
    out = dict(row)
    out["header"] = header
    out["body"] = body
    out["status"] = header["status"]
    return out


def _ensure_row(row: dict) -> dict:
    return row if "header" in row and "body" in row else _with_header(row)


STORE_SCRIPT_TEMPLATE = '''\
import base64, json, os
from arb_memory import redis_conn
from arb_memory.bus import memory_write

intents = json.loads(base64.b64decode("{payload_b64}"))
client = redis_conn.from_url(os.environ["ARB_MEMORY_REDIS_URL"])
for intent in intents:
    memory_write(
        client,
        artefact=intent["artefact"],
        hints=intent["hints"],
        source=intent["artefact"]["source"],
        author=intent["artefact"]["author"],
        ulid=intent["ulid"],
    )
print(f"enqueued {{len(intents)}} intents")
'''


def build_store_script(intents: list[dict]) -> str:
    payload_b64 = base64.b64encode(json.dumps(intents).encode()).decode("ascii")
    return STORE_SCRIPT_TEMPLATE.format(payload_b64=payload_b64)


def _run_store(intents: list[dict], *, run_fn=None) -> None:
    runner = run_fn or subprocess.run
    runner(
        [
            "ssh", "arb-prod",
            "cd /home/claude/AgentRedisBridge && "
            "docker compose -f deploy/docker-compose.yml exec -T memory python3 -",
        ],
        input=build_store_script(intents),
        text=True,
        check=True,
    )


def _intent_ulid(learn_id: str, version: int) -> str:
    return hashlib.sha256(f"{learn_id}:{version}".encode()).hexdigest()[:32]


def hint_summary(learn_id: str, version: int, header: dict, body: str) -> str:
    """Metadata-only index-hint text for a learn proposal.

    The raw proposal body is EXTERNAL, UNTRUSTED text stored at propose time — before any
    eval — and index hints are embedded and served by memory_search to every connected
    client, persisting across rejection (GLM, injection-screening eval, 2026-07-07). So the
    searchable surface carries only bounded metadata + the title line; the verbatim body
    stays in the artefact, reachable only by an explicit memory_get.
    """
    title = next((line.strip() for line in body.splitlines() if line.strip()), learn_id)[:160]
    parts = [
        f"learn proposal: {title}",
        f"status={header.get('status', '?')} target={header.get('target', '?')}",
    ]
    if header.get("supersedes"):
        parts.append(f"supersedes={header['supersedes']}")
    if header.get("referent"):
        parts.append("referent=workflow-verified")
    parts.append(
        f"External/body text withheld from the search index by design; "
        f"full content via memory_get({learn_id!r}, {version})."
    )
    return "\n".join(parts)


def store_version(
    learn_id: str,
    body: str,
    header: dict,
    *,
    current: dict | None,
    store_fn: Callable[[list[dict]], None],
    get_status_fn: Callable[[str], dict | None],
    sleep_fn: Callable[[int], None],
) -> int:
    version = int(current["version"]) + 1 if current else 1
    content = render_header(header) + body
    intent = {
        "ulid": _intent_ulid(learn_id, version),
        "artefact": {
            "artefact_id": learn_id,
            "content": content,
            "mime": "text/markdown",
            "source": "learn",
            "author": header.get("proposer") or "arb-learn",
        },
        "hints": [{
            "text": hint_summary(learn_id, version, header, body),
            "metadata": {
                "kind": "artefact_index",
                "artefact_id": learn_id,
                "learn_proposal": True,
                "status": header["status"],
                "target": header["target"],
            },
        }],
    }
    store_fn([intent])
    wait_visible(learn_id, version, sleep_fn=sleep_fn, get_status_fn=get_status_fn)
    return version


def wait_visible(
    learn_id: str,
    version: int,
    *,
    sleep_fn: Callable[[int], None],
    get_status_fn: Callable[[str], dict | None],
) -> None:
    for _ in range(10):
        row = get_status_fn(learn_id)
        if row and int(row.get("version", 0)) >= version:
            return
        sleep_fn(2)
    raise LearnError(f"WriteLoop did not make {learn_id} version {version} visible")


def memory_search(query: str, k: int = 5, *, run_fn=None) -> list[dict]:
    # arb_memory has no db.connect_from_env, and retrieve() REQUIRES embed= — caught by the
    # live gate: the unit pin covered the argv, not the inline script's viability
    script = f"""\
import json, os, psycopg
from arb_memory.store import retrieve
from arb_memory.embed import embed
conn = psycopg.connect(os.environ["ARB_MEMORY_DSN"])
rows = retrieve(conn, {query!r}, k={k}, embed=embed)
out = [{{"artefact_id": r["hint"].get("artefact_id"), "text": r["hint"].get("text", "")[:200]}}
       for r in rows]
print({READ_SENTINEL_BEGIN!r})
print(json.dumps(out, default=str))
print({READ_SENTINEL_END!r})
"""
    runner = run_fn or subprocess.run
    res = runner(
        [
            "ssh", "arb-prod",
            "cd /home/claude/AgentRedisBridge && "
            "docker compose -f deploy/docker-compose.yml exec -T memory python3 -",
        ],
        input=script,
        text=True,
        check=True,
        capture_output=True,
    )
    return parse_sentinel_json(res.stdout)


def resolve_seats(panel: str, seat_names: list[str]) -> list[LearnSeat]:
    names = seat_names or list(PANEL_SEATS[panel])
    return [SEAT_REGISTRY[name] for name in dict.fromkeys(names)]


def dispatch(seat: LearnSeat, task: str, run_id: str) -> str:
    """Ordinary learn-intake dispatch via harness publish + authority (Slice 1d-iv)."""
    import json
    import tempfile
    from pathlib import Path

    timeout = 5400
    redis_url = os.environ.get("ARB_MEMORY_REDIS_URL", "").strip()
    if not redis_url:
        raise LearnError(
            "learn_intake ordinary dispatch requires ARB_MEMORY_REDIS_URL "
            "for the short-lived FABA harness publish step (Slice 1d-iv)"
        )
    try:
        from agent_redis_bridge.dispatch_cli import wrap_instructions_as_brief
    except ImportError as exc:  # pragma: no cover
        raise LearnError(f"dispatch_cli unavailable: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="learn-dispatch-") as tmp:
        tmp_p = Path(tmp)
        brief_file = tmp_p / "brief.md"
        brief_file.write_text(
            wrap_instructions_as_brief(task, vantage="learn-intake"),
            encoding="utf-8",
        )
        pub = subprocess.run(
            [
                "scripts/arb-memory-harness-publish",
                "--target-agent-id", seat.target_id,
                "--brief", str(brief_file),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "FROM_AGENT_ID": "claude-bridge-dev", "BRANCH": "dev"},
        )
        receipt = json.loads(pub.stdout)
        receipt_file = tmp_p / "receipt.json"
        receipt_file.write_text(json.dumps(receipt), encoding="utf-8")
        command = [
            "scripts/dispatch-dev",
            "--engine", seat.engine,
            "--target-id", seat.target_id,
            "--timeout", str(timeout),
            "--run-id", run_id,
            "--audit-panel",
            "--artefact-id", str(receipt["artefact_id"]),
            "--version", str(receipt["version"]),
            "--receipt", str(receipt_file),
            "--brief", str(brief_file),
        ]
        if seat.effort:
            command.extend(["--effort", seat.effort])
        if seat.isolated_worktree:
            suffix = hashlib.sha256(f"{run_id}:{seat.name}".encode()).hexdigest()[:12]
            command.extend([
                "--worktree", f"learn-{seat.name}-{suffix}",
                "--worktree-cleanup", "auto",
            ])
        from .dispatch_authority import filter_publish_env

        enq_env = filter_publish_env({
            **os.environ,
            "FROM_AGENT_ID": "claude-bridge-dev",
            "BRANCH": "dev",
        })
        res = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout + 60,
            env=enq_env,
        )
        return res.stdout


def dispatch_reply_text(dispatch_stdout: str) -> str:
    try:
        payload = json.loads(dispatch_stdout)
    except json.JSONDecodeError as exc:
        raise LearnError(f"malformed dispatch reply envelope: {exc}") from exc
    if not isinstance(payload, dict) or "result" not in payload:
        raise LearnError("dispatch reply envelope missing result")
    return str(payload["result"])


TOKEN_PATTERNS = [
    ("reject", re.compile(r"\bREJECT\b")),
    ("needs-mark", re.compile(r"\bNEEDS-MARK\b")),
    ("worth-building", re.compile(r"\bWORTH-BUILDING\b")),
]
STANCE_FOR_TOKEN = {"reject": "block", "worth-building": "approve", "needs-mark": "abstain"}


def parse_seat_verdict(stdout: str, seat: str = "seat") -> SeatVerdict:
    try:
        text = dispatch_reply_text(stdout)
    except LearnError as exc:
        return SeatVerdict(seat, "eval-error", str(exc), None)
    found = [name for name, pat in TOKEN_PATTERNS if pat.search(text)]
    if not found:
        return SeatVerdict(seat, "eval-error", "no uppercase domain token", None)
    for name in ("reject", "needs-mark", "worth-building"):
        if name in found:
            token = name
            break
    try:
        stance = parse_stance(text, require_fence=True)
    except StanceError as exc:
        return SeatVerdict(seat, "eval-error", str(exc), None)
    if stance["stance"] != STANCE_FOR_TOKEN[token]:
        return SeatVerdict(seat, "eval-error", "domain token disagrees with fenced stance", stance)
    return SeatVerdict(seat, token, text, stance)


def decide_outcome(verdicts: list[SeatVerdict]) -> str:
    names = [v.verdict for v in verdicts]
    if "reject" in names:
        return "rejected"
    if "eval-error" in names:
        return "eval-error"
    if "needs-mark" in names:
        return "needs-mark"
    return "eval-approved"


def _status(row: dict | None) -> str | None:
    if not row:
        return None
    if "header" in row:
        return row["header"]["status"]
    return parse_header(row["content"])[0]["status"]


def _body(row: dict) -> str:
    if "body" in row:
        return row["body"]
    return parse_header(row["content"])[1]


def _guard_not_terminal(row: dict | None) -> None:
    status = _status(row)
    if status in TERMINAL_STATUSES:
        raise LearnError(f"{status} learn proposal cannot be modified")


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text.splitlines() else "proposal"


def _find_dedupe(source_body: str, rows: list[dict]) -> dict | None:
    key = dedupe_key(source_body)
    best = None
    best_ratio = 0.0
    for row in rows:
        ratio = similarity(key, dedupe_key(row["content"]))
        if ratio > best_ratio:
            best = row
            best_ratio = ratio
    return best if best and best_ratio >= 0.6 else None


def validate_referent(workflow: list[str] | None, repo: str | None, *, run_fn=subprocess.run) -> dict | None:
    if not workflow:
        return None
    if len(workflow) != 2 or not repo:
        raise LearnError("--from-workflow requires HANDOFF SHA --repo PATH")
    handoff, sha = workflow
    if not Path(handoff).exists():
        raise LearnError(f"handoff does not exist: {handoff}")
    try:
        run_fn(["git", "-C", repo, "cat-file", "-e", sha], check=True)
    except subprocess.CalledProcessError as exc:
        raise LearnError(f"workflow sha {sha} not found in repo {repo}") from exc
    return {"workflow_handoff": handoff, "workflow_sha": sha, "workflow_repo": repo}


def _propose(args, deps: Deps) -> int:
    source_path = Path(args.source_file)
    source = source_path.read_bytes()
    body = source.decode()
    base_id = mint_id(source, _first_line(body))
    existing = deps.get_status(base_id)
    # propose never writes over an EXISTING id in any status: statuses are forward-only and
    # owned by evaluate/promote/resolve, and a re-propose over eval-approved would clobber
    # an approval with a fresh 'proposed' (caught by the hint-summary pin test, 2026-07-07;
    # TERMINAL_STATUSES alone missed the non-terminal clobber). --force supersedes via -rN.
    if existing is not None and not args.force:
        raise LearnError(
            f"{base_id} already exists (status {_status(existing)}); "
            "use --force to supersede, or evaluate/promote/resolve it")
    rows = deps.list_proposals()

    for hit in deps.memory_search_fn(_first_line(body), 5)[:5]:
        print(f"prior-art {hit.get('artefact_id') or hit.get('id')}")
    dupe = _find_dedupe(body, rows)
    if dupe and not args.force:
        print(f"{dupe['artefact_id']} {_status(dupe)}")
        raise LearnError("duplicate proposal; use --force to supersede")
    supersedes = None
    if args.force:
        supersedes = existing["artefact_id"] if existing else (dupe["artefact_id"] if dupe else None)
    learn_id = next_force_id(base_id, rows) if args.force and supersedes else base_id
    header = {
        "status": "proposed",
        "target": args.target,
        "proposer": "mark",
        "supersedes": supersedes,
        "referent": validate_referent(args.from_workflow, args.repo),
    }
    store_version(
        learn_id, body, header, current=deps.get_status(learn_id), store_fn=deps.store_fn,
        get_status_fn=deps.get_status, sleep_fn=deps.sleep_fn,
    )
    print(f"{learn_id} proposed")
    return 0


def _eval_brief(learn_id: str, body: str, citations: list[dict], digest: str) -> str:
    wiki = [r.get("artefact_id") or r.get("id") for r in citations if str(r.get("artefact_id") or r.get("id")).startswith("wiki-")][:3]
    return (
        f"Evaluate ARB learn proposal {learn_id}.\n\n"
        "Emit exactly one uppercase domain token: REJECT, NEEDS-MARK, or WORTH-BUILDING.\n"
        "End with a fenced vote block containing stance and severity. The stance field\n"
        "takes ONLY the canonical mapping of your domain token — REJECT -> \"block\",\n"
        "WORTH-BUILDING -> \"approve\", NEEDS-MARK -> \"abstain\" — never the domain token\n"
        "itself (a stance like \"needs-mark\" is invalid and voids your vote). The severity\n"
        "field takes ONLY \"P0\", \"P1\", \"P2\", or \"none\" (your highest surviving finding;\n"
        "words like \"medium\" are invalid and void your vote), e.g.\n"
        "```vote\n{\"stance\":\"approve\",\"severity\":\"none\",\"refs\":[],\"note\":\"...\"}\n```\n\n"
        f"Top wiki citations: {', '.join(wiki) or '(none)'}\n"
        f"Current proposals digest:\n{digest}\n\nProposal:\n{body}"
    )


def _substantive_rows(rows: list[dict]) -> list[dict]:
    keep = []
    for row in rows:
        status = _status(row)
        if status in {"rejected", "eval-approved", "promoted", "needs-mark"}:
            keep.append(row)
    return sorted(keep, key=lambda r: r.get("created_at", ""))[-10:]


def drift_rate(rows: list[dict]) -> tuple[int, int, float | None]:
    sample = _substantive_rows(rows)
    if not sample:
        return 0, 0, None
    approvals = sum(1 for r in sample if _status(r) in {"eval-approved", "promoted"})
    return approvals, len(sample), approvals / len(sample)


def _evaluate(args, deps: Deps) -> int:
    row = deps.get_status(args.learn_id)
    if not row:
        raise LearnError(f"unknown learn proposal {args.learn_id}")
    row = _ensure_row(row)
    _guard_not_terminal(row)
    status = _status(row)
    if status != "proposed" and not (status == "eval-error" and args.retry):
        raise LearnError(f"cannot evaluate status {status}")
    citations = deps.memory_search_fn(_first_line(_body(row)), 3)
    rows = deps.list_proposals()
    digest = "\n".join(f"- {r['artefact_id']} {_status(r)}" for r in rows[:20])
    run_id = f"learn-{args.learn_id}-{hashlib.sha256(os.urandom(8)).hexdigest()[:8]}"
    verdicts = []
    brief = _eval_brief(args.learn_id, _body(row), citations, digest)
    for seat in resolve_seats(args.panel, args.seat):
        try:
            stdout = deps.dispatch_fn(seat, brief, run_id)
            verdicts.append(parse_seat_verdict(stdout, seat=seat.target_id))
        except subprocess.TimeoutExpired:
            verdicts.append(SeatVerdict(seat.target_id, "eval-error", "timeout", None))
        except subprocess.CalledProcessError as exc:
            verdicts.append(SeatVerdict(
                seat.target_id, "eval-error", f"dispatch failed: {exc}", None
            ))
    outcome = decide_outcome(verdicts)
    header = dict(row["header"] if "header" in row else parse_header(row["content"])[0])
    header["status"] = outcome
    reason_body = _body(row) + "\n\n## Evaluation\n" + "\n".join(
        f"- {v.seat}: {v.verdict}: {v.reason}" for v in verdicts
    )
    store_version(args.learn_id, reason_body, header, current=row, store_fn=deps.store_fn,
                  get_status_fn=deps.get_status, sleep_fn=deps.sleep_fn)
    # re-list AFTER the write (barrier guarantees visibility): the printed rate must include
    # the outcome this run just recorded
    approvals, total, rate = drift_rate(deps.list_proposals())
    if rate is None:
        print("approval-rate 0/0")
    else:
        print(f"approval-rate {approvals}/{total} ({rate:.0%})")
    print(f"{args.learn_id} {outcome}")
    return 0


def _promote(args, deps: Deps) -> int:
    row = deps.get_status(args.learn_id)
    if not row:
        raise LearnError(f"unknown learn proposal {args.learn_id}")
    row = _ensure_row(row)
    _guard_not_terminal(row)
    if _status(row) != "eval-approved":
        raise LearnError(f"cannot promote status {_status(row)}")
    if row["header"].get("target") == "project" and not args.i_am_mark:
        raise LearnError("--target project promotion requires --i-am-mark")
    approvals, total, rate = drift_rate(deps.list_proposals())
    if rate is not None and total >= 5:
        if rate >= 0.6 and not args.override:
            raise LearnError(f"approval-rate refusal {approvals}/{total}; use --override")
        if rate >= 0.4:
            print(f"warning: approval-rate {approvals}/{total}", file=sys.stderr)
    outdir = Path(tempfile.gettempdir()) / "arb-learn-briefs"
    outdir.mkdir(exist_ok=True)
    brief = outdir / f"{args.learn_id}-build-brief.md"
    brief.write_text(f"# Build brief for {args.learn_id}\n\n{_body(row)}")
    header = dict(row["header"])
    header["status"] = "promoted"
    store_version(args.learn_id, _body(row) + f"\n\nPromoted brief: {brief}\n", header,
                  current=row, store_fn=deps.store_fn, get_status_fn=deps.get_status,
                  sleep_fn=deps.sleep_fn)
    print(str(brief))
    return 0


def _resolve(args, deps: Deps) -> int:
    row = deps.get_status(args.learn_id)
    if not row:
        raise LearnError(f"unknown learn proposal {args.learn_id}")
    row = _ensure_row(row)
    _guard_not_terminal(row)
    if _status(row) != "needs-mark":
        raise LearnError(f"cannot resolve status {_status(row)}")
    header = dict(row["header"])
    header["status"] = "eval-approved" if args.decision == "approve" else "rejected"
    body = _body(row) + f"\n\n## Mark resolution\n{args.decision}: {args.reason}\n"
    store_version(args.learn_id, body, header, current=row, store_fn=deps.store_fn,
                  get_status_fn=deps.get_status, sleep_fn=deps.sleep_fn)
    print(f"{args.learn_id} {header['status']}")
    return 0


class LearnArgumentParser(argparse.ArgumentParser):
    def format_help(self):
        text = super().format_help()
        panels = ",".join(PANEL_SEATS)
        panel_usage = "|".join(PANEL_SEATS)
        seats = ",".join(SEAT_REGISTRY)
        return text + (
            "\nSurface:\n"
            "  propose <source-file> [--target skill|arb|project] [--force] "
            "[--from-workflow HANDOFF SHA --repo PATH]\n"
            "  --target {skill,arb,project}\n"
            f"  evaluate <learn-id> [--retry] [--panel {panel_usage}] "
            "[--seat NAME ...]\n"
            f"  --panel {{{panels}}}\n"
            f"  --seat {{{seats}}}\n"
            "  promote <learn-id> [--override] [--i-am-mark]\n"
            "  resolve <learn-id> {approve,reject} --reason TEXT\n"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = LearnArgumentParser(prog="arb-learn")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose", usage="arb-learn propose <source-file> [options]")
    p.add_argument("source_file")
    p.add_argument("--target", choices=["skill", "arb", "project"], default="arb")
    p.add_argument("--force", action="store_true")
    p.add_argument("--from-workflow", nargs=2, metavar=("HANDOFF", "SHA"))
    p.add_argument("--repo")
    panel_usage = "|".join(PANEL_SEATS)
    e = sub.add_parser(
        "evaluate",
        usage=f"arb-learn evaluate <learn-id> [--retry] [--panel {panel_usage}] "
        "[--seat NAME ...]",
    )
    e.add_argument("learn_id")
    e.add_argument("--retry", action="store_true")
    selection = e.add_mutually_exclusive_group()
    selection.add_argument(
        "--panel", choices=tuple(PANEL_SEATS), default=argparse.SUPPRESS
    )
    selection.add_argument(
        "--seat", choices=tuple(SEAT_REGISTRY), action="append",
        default=argparse.SUPPRESS,
    )
    pr = sub.add_parser("promote", usage="arb-learn promote <learn-id> [--override] [--i-am-mark]")
    pr.add_argument("learn_id")
    pr.add_argument("--override", action="store_true")
    pr.add_argument("--i-am-mark", action="store_true", dest="i_am_mark")
    r = sub.add_parser("resolve", usage="arb-learn resolve <learn-id> {approve,reject} --reason TEXT")
    r.add_argument("learn_id")
    r.add_argument("decision", choices=["approve", "reject"])
    r.add_argument("--reason", required=True)
    return parser


def main(argv=None, *, deps: Deps | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    deps = _deps(deps)
    try:
        if args.cmd == "propose":
            return _propose(args, deps)
        if args.cmd == "evaluate":
            args.panel = getattr(args, "panel", "core")
            args.seat = getattr(args, "seat", [])
            return _evaluate(args, deps)
        if args.cmd == "promote":
            return _promote(args, deps)
        if args.cmd == "resolve":
            return _resolve(args, deps)
        raise LearnError(f"unknown command {args.cmd}")
    except LearnError as exc:
        print(f"arb-learn: ERROR {exc}", file=sys.stderr)
        return 2

"""Instrument 1 dispatch pipeline (schema v0.2 §3-§6).

The deterministic core already lives in power/viability/report/schema. This module wires the
run path around it without weakening the parked decisions:
  - real dispatch is behind a Dispatcher seam;
  - off-quorum normalization defaults to an unresolved sentinel and refuses live runs;
  - gold validation is present as an explicit unadjudicated flag, never fabricated;
  - NDJSON is authoritative; the report is rendered through the allowlist grid.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import boundary, gold, power, provenance, report
from .schema import ControlLocus, Scenario, Seed, cluster_key, TAXONOMY as TAXONOMY_NAMES
from .viability import UNKNOWN, ViabilityResult, classify

UNRESOLVED_OFFQUORUM = "UNRESOLVED_OFFQUORUM"
GOLD_UNADJUDICATED = "GOLD_UNADJUDICATED"


class Parked(RuntimeError):
    """Raised when a parked safety/human-labor decision blocks a trustworthy live run."""


class Dispatcher(Protocol):
    def dispatch(self, task: dict[str, Any]) -> str:
        """Return a raw seat reply for a review/normalization task."""


class Normalizer(Protocol):
    def normalize(self, finding: str, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return zero or more normalized findings traced to the raw finding."""


# seat -> (bridge engine, target-id). Live floor panel is bridge-dispatchable seats only
# (cold-opus is an in-process subagent a standalone CLI can't spawn — decision (i); the
# AnthropicDispatcher generalization (iii) is the fast-follow for direct-API seats like Claude).
DEFAULT_SEAT_MAP = {
    "codex": ("codex", "codex-agentredisbridge-dev"),
    "agy": ("agy-print", "agy-agentredisbridge-dev"),
}


class DispatchError(RuntimeError):
    """A seat dispatch failed (non-zero exit / ok:false / timeout). Caller routes to detection-miss."""

    def __init__(self, message: str, *, kind: str = "review"):
        super().__init__(message)
        self.kind = kind


def _dispatch_timeout() -> int:
    return int(os.environ.get("ARB_EVAL_DISPATCH_TIMEOUT", "900"))


class BridgeDispatcher:
    """Live bridge dispatcher: builds the seat's review brief FROM the subject diff, invokes
    `agent-dispatch` with the correct engine/target/env, and parses the reply envelope's `result`.

    The brief asks for findings in the exact line format `segment_reply` + the M3 normalizer consume
    (`<class> | <file>:<line> | <statement>`, or `no issues`). On failure it raises DispatchError so
    the run loop records it and routes to detection-miss — never a silent empty reply.
    """

    def __init__(self, script: str = "scripts/agent-dispatch", *, seat_map: dict | None = None,
                 from_agent_id: str | None = None, branch: str | None = None,
                 env_file: str | None = None, timeout: int = 900, run_id: str | None = None):
        import os
        self.script = script
        self.seat_map = seat_map or DEFAULT_SEAT_MAP
        self.from_agent_id = from_agent_id or os.environ.get("ARB_FROM_AGENT_ID", "claude-agentredisbridge-dev")
        self.branch = branch or os.environ.get("ARB_BRANCH", "main")
        self.env_file = env_file or os.environ.get("AGENT_ENV_FILE", "")
        self.timeout = timeout
        # Threaded from run_floor's own per-run id (scenario.id-<hex>) so agent-dispatch's
        # required --run-id/--adhoc gate gets the SAME label already used for events.ndjson,
        # instead of a second, disconnected identifier.
        self.run_id = run_id

    def assert_read_confined(self, repo: str) -> None:
        """Fail-CLOSED integrity gate. The bridge path CANNOT read-confine a seat (codex/agy sandboxes
        restrict writes, not reads), so a bridge dispatch is NEVER confined — it always refuses. The
        only confined path is the ContainerDispatcher (absence-jail proven per-dispatch by its canary;
        run_floor accepts it via its CONFINED marker, not via this method). There is deliberately NO
        env-var that grants 'confined' status here: a settable string is spoofable (it would let a
        caller claim confinement the bridge doesn't provide). The single escape is an EXPLICIT,
        integrity-UNGUARANTEED diagnostic flag."""
        import os
        if os.environ.get("ARB_ALLOW_UNCONFINED_FLOOR") == "1":
            return  # explicit, integrity-UNGUARANTEED diagnostic escape — verdicts are NOT trustworthy
        raise Parked(
            "live bridge dispatch cannot read-confine a seat (codex/agy sandboxes restrict writes, not "
            "reads), so it can read the scenario/answer-key and game the floor. Use the confined "
            "ContainerDispatcher (arb-eval run --confined), or set ARB_ALLOW_UNCONFINED_FLOOR=1 to "
            "accept an integrity-UNGUARANTEED diagnostic run."
        )

    def _review_prompt(self, task: dict[str, Any]) -> str:
        # MEASUREMENT-CORRECTNESS: the seat must INVESTIGATE a real checkout with its tools (Read/Grep/
        # Bash) — verify against config/call-sites, not reason from a diff blob. Handing diff text would
        # measure crippled snippet-review, not the tool-equipped review ARB actually uses (and the floor
        # is supposed to predict), and would understate every seat. So we point at the repo + the change,
        # and instruct investigation. The normalizer (M3) is deliberately the opposite — bare API, no tools.
        repo = task["repo"]            # path to the fixture checkout, relative to the seat's working dir
        base, head = task["base"], task["head"]
        return (
            f"You are a code reviewer with shell + file tools. The subject is a real git checkout at "
            f"`{repo}` in your working directory; the change under review is `git -C {repo} diff {base}..{head}`.\n"
            "INVESTIGATE with your tools — open the changed files, trace call sites, check config/usage "
            "against the rest of the repo (do NOT reason from the diff alone; verify against ground truth). "
            "Then report each genuine defect on its OWN line in EXACTLY this format:\n"
            "  <class> | <file>:<line> | <one-line description>\n"
            "where <class> is one of: " + ", ".join(sorted(TAXONOMY_NAMES)) + ".\n"
            "If there are no issues, reply with exactly: no issues\n"
            "No prose, no preamble, no markdown — only finding lines or `no issues`.\n"
        )

    def dispatch(self, task: dict[str, Any]) -> str:
        import os
        import sys
        import tempfile
        seat = task["seat"]
        if seat not in self.seat_map:
            raise DispatchError(f"seat {seat!r} is not bridge-dispatchable (live floor panel = "
                                f"{sorted(self.seat_map)}); cold-opus/Claude need the AnthropicDispatcher (iii)")
        engine, target_id = self.seat_map[seat]
        prompt = self._review_prompt(task)
        env = dict(os.environ, FROM_AGENT_ID=self.from_agent_id, BRANCH=self.branch)
        if self.env_file:
            env["AGENT_ENV_FILE"] = self.env_file
        run_id_flags = ["--run-id", self.run_id] if self.run_id else ["--adhoc"]
        # Slice 1d-iv: pre-mint via harness publish, enqueue via authority quartet
        # (no free-form positional task — agent-dispatch refuses that path).
        redis_url = os.environ.get("ARB_MEMORY_REDIS_URL", "").strip()
        if not redis_url:
            raise DispatchError(
                "ARB_MEMORY_REDIS_URL required for harness publish "
                "(Slice 1d-iv authority path)"
            )
        _bridge_src = str(Path(__file__).resolve().parents[3] / "src")
        if _bridge_src not in sys.path:
            sys.path.insert(0, _bridge_src)
        from agent_redis_bridge.dispatch_cli import wrap_instructions_as_brief

        with tempfile.TemporaryDirectory(prefix="eval-dispatch-") as tmp:
            tmp_p = Path(tmp)
            brief_file = tmp_p / "brief.md"
            brief_file.write_text(
                wrap_instructions_as_brief(prompt, vantage="arb-eval"),
                encoding="utf-8",
            )
            # Publisher consumes --env-file (not AGENT_ENV_FILE); pass when configured.
            pub_cmd = [
                "scripts/arb-memory-harness-publish",
                "--target-agent-id", target_id,
                "--brief", str(brief_file),
            ]
            if self.env_file:
                pub_cmd.extend(["--env-file", self.env_file])
            pub = subprocess.run(
                pub_cmd,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, env=env,
            )
            if pub.returncode != 0:
                raise DispatchError(
                    f"harness-publish {task.get('task_id')} exit={pub.returncode}: "
                    f"{pub.stderr.strip()[:200]}"
                )
            receipt = json.loads(pub.stdout)
            receipt_file = tmp_p / "receipt.json"
            receipt_file.write_text(json.dumps(receipt), encoding="utf-8")
            # Enqueue without publish credentials (non-FABA identity).
            from agent_redis_bridge.dispatch_authority import filter_publish_env

            enq_env = filter_publish_env(env)
            proc = subprocess.run(
                [self.script, "--engine", engine, "--target-id", target_id,
                 "--timeout", str(self.timeout), *run_id_flags,
                 "--artefact-id", str(receipt["artefact_id"]),
                 "--version", str(receipt["version"]),
                 "--receipt", str(receipt_file),
                 "--brief", str(brief_file)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, env=enq_env,
            )
        if proc.returncode != 0:
            raise DispatchError(f"dispatch {task.get('task_id')} exit={proc.returncode}: {proc.stderr.strip()[:200]}")
        try:
            env_reply = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return proc.stdout  # some engines print raw; treat stdout as the reply
        if not env_reply.get("ok", True):
            raise DispatchError(f"dispatch {task.get('task_id')} ok=false: {str(env_reply.get('error'))[:200]}")
        return str(env_reply.get("result", ""))


class ContainerDispatcher:
    """Confined dispatcher (decision panel Option C — absence-based confinement). Each reviewing seat
    runs in a container jailed to the fixture; the scenario/answer-key is absent from the namespace by
    construction. Confinement is PROVEN PER DISPATCH by the canary (run inside confined-review.sh),
    not by a settable env var — so a catch here is earned-by-construction, and run_floor accepts this
    dispatcher as confined because the canary, not a string, is the proof. codex and agy are the
    configured confined seats; live gates verify each engine against the current image/auth state."""

    CONFINED = True  # marker: confinement proven per-dispatch by the canary, not a spoofable env var

    def __init__(self, scenario_path: str, confinement_dir: str | None = None,
                 seats: tuple[str, ...] = ("codex", "agy")):
        self.scenario_path = str(Path(scenario_path).resolve())
        self.dir = Path(confinement_dir) if confinement_dir else Path(__file__).resolve().parents[1] / "confinement"
        self.seats = set(seats)
        self.last_engine_versions: dict[str, str] = {}
        self.last_confined_command = ""
        self.last_model_reported: str | None = None
        self.last_model_source = "cli-version-only"

    def dispatch(self, task: dict[str, Any]) -> str:
        seat = task["seat"]
        if seat not in self.seats:
            raise DispatchError(f"seat {seat!r} is not confined-dispatchable (verified: {sorted(self.seats)})")
        review = self.dir / "confined-review.sh"
        timeout = _dispatch_timeout()
        env = dict(os.environ)
        model = task.get("model")
        if model and model != "?":
            env["ARB_EVAL_MODEL"] = str(model)
            self.last_model_source = "pinned-flag"
        else:
            env.pop("ARB_EVAL_MODEL", None)
            self.last_model_source = "cli-version-only"
        nonce = task.get("provenance_nonce")
        if nonce:
            env["ARB_PROV_NONCE"] = str(nonce)
        argv = [str(review), seat, task["repo"], task["base"], task["head"], self.scenario_path]
        self.last_confined_command = " ".join(argv + ([f"model={model}"] if model and model != "?" else []))
        try:
            proc = subprocess.run(
                argv,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            raise DispatchError(
                f"confined dispatch {task.get('task_id')} timed out after {timeout}s",
                kind="timeout",
            )
        if proc.returncode != 0:
            if proc.returncode == 43:
                raise DispatchError(
                    f"confined dispatch {task.get('task_id')} failed canary: "
                    f"{(proc.stderr or proc.stdout).strip()[:300]}",
                    kind="canary",
                )
            # canary failure (boundary breach) or review failure -> fail loud, never a silent reply
            raise DispatchError(f"confined dispatch {task.get('task_id')} failed (canary or review): "
                                f"{(proc.stderr or proc.stdout).strip()[:300]}",
                                kind="review")
        stdout, versions = provenance.strip_prov_fence(proc.stdout, str(nonce or ""))
        self.last_engine_versions = versions
        self.last_model_reported = None
        return stdout


class DispatcherNormalizer:
    """Normalizer implemented via a configured off-quorum dispatcher seat."""

    def __init__(self, dispatcher: Dispatcher, seat: str):
        self.dispatcher = dispatcher
        self.seat = seat

    def normalize(self, finding: str, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        task = {
            "kind": "normalize",
            "seat": self.seat,
            "finding": finding,
            "context": context or {},
            "output": "json-list",
        }
        raw = self.dispatcher.dispatch(task)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"normalizer returned non-JSON for finding {finding!r}") from e
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise RuntimeError("normalizer must return a JSON object or list")
        return [dict(x) for x in data]


class MockDispatcher:
    """Deterministic dispatcher for tests."""

    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.tasks: list[dict[str, Any]] = []

    def dispatch(self, task: dict[str, Any]) -> str:
        self.tasks.append(task)
        return self.responses.get(task["task_id"], "no issues")


class MockNormalizer:
    """Deterministic normalizer for tests."""

    def __init__(self, mapping: dict[str, list[dict[str, Any]]]):
        self.mapping = mapping

    def normalize(self, finding: str, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [dict(x) for x in self.mapping.get(finding, [])]


# Quorum model/seat names — defense-in-depth: the off-quorum normalizer must not be one of these
# (M3 doesn't trigger it, but a future config change might). Cheap collision guard, per P-1.
QUORUM_MODELS = frozenset({"gpt-5.5", "codex", "gemini", "agy", "agy-print", "opus", "cold-opus"})


def _load_minimax_key() -> str:
    """Operator-supplied only. ARB does not read other tools' credential stores
    (the former fallback to pi's ~/.pi/agent/auth.json was removed 2026-08-30)."""
    import os
    key = os.environ.get("ARB_MINIMAX_KEY", "")
    if not key:
        raise RuntimeError("ARB_MINIMAX_KEY is not set (export the MiniMax API key; no dotfile fallback)")
    return key


def _extract_json(text: str) -> dict:
    """Robust, fence-tolerant extraction — M3 occasionally wraps JSON in ```json fences (1/6 observed)."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S) or re.search(r"(\{.*\})", t, re.S)
    return json.loads(m.group(1) if m else t)


class AnthropicNormalizer:
    """Off-quorum normalizer via the Anthropic-compatible API (resolves P-1).

    pi cannot forward `temperature`; this client sets temp=0 + thinking-disabled as first-class
    request params on MiniMax-M3's `/anthropic` endpoint. Off-quorum by construction (M3 not in the
    voting quorum); its residual MoE variance lands only in confidence/wording, which the matcher
    ignores (verified: match is class+location only). On unparseable output it FAILS LOUD — emits
    `class:"unknown"` (routes to matcher-ambiguous) with the error attached — never silently drops.
    """

    def __init__(self, model: str = "MiniMax-M3", base_url: str = "https://api.minimax.io/anthropic",
                 api_key: str | None = None, temperature: float = 0.0, max_tokens: int = 512,
                 taxonomy: list[str] | None = None):
        if model in QUORUM_MODELS:
            raise Parked(f"normalizer model {model!r} is a quorum model — collision guard")
        import anthropic
        from .schema import TAXONOMY
        self.client = anthropic.Anthropic(base_url=base_url, api_key=api_key or _load_minimax_key())
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.taxonomy = list(taxonomy or TAXONOMY)
        self._sys = (
            "You normalize one code-review finding into JSON. Output ONLY a JSON object, no prose, no "
            "markdown. Schema: {\"class\": one of " + str(self.taxonomy) + ", \"location\": "
            "{\"file\": str, \"line\": int|null, \"symbol\": str|null}, \"severity\": \"P0\"|\"P1\"|\"P2\", "
            "\"confidence\": 0..1, \"statement\": str}. If the finding fits no class, use \"unknown\" — "
            "do NOT force a class."
        )

    def _fail(self, finding: str, reason: str, raw: str | None = None) -> dict[str, Any]:
        d = {"class": "unknown", "location": {}, "severity": "P2", "confidence": 0.0,
             "statement": finding, "normalize_error": reason}
        if raw is not None:
            d["raw_response"] = raw[:200]
        return d

    def normalize(self, finding: str, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        try:
            r = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens, temperature=self.temperature,
                thinking={"type": "disabled"}, system=self._sys,
                messages=[{"role": "user", "content": finding}])
            text = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text")
        except Exception as e:  # network/API failure → fail loud, never drop
            return [self._fail(finding, f"dispatch-error: {type(e).__name__}: {e}")]
        try:
            j = _extract_json(text)
        except Exception as e:
            return [self._fail(finding, f"parse-error: {e}", raw=text)]
        cls = j.get("class")
        if cls not in self.taxonomy:   # never force; route unknown/garbage to matcher-ambiguous
            cls = "unknown"
        return [{
            "class": cls,
            "location": dict(j.get("location") or {}),
            "severity": j.get("severity", "P2"),
            "confidence": j.get("confidence", 0.0),
            "statement": j.get("statement", finding),
        }]


@dataclass(frozen=True)
class MatchDecision:
    outcome: str              # detected / detection-miss / matcher-ambiguous
    basis: str | None = None  # symbol / function / window
    boundary_oracle: str | None = None


@dataclass
class RunResult:
    run_id: str
    events_path: str
    report_path: str
    detail_path: str
    report_text: str
    verdicts: dict[str, dict[str, str]]
    claim_levels: dict[str, str]
    gold_unadjudicated: bool


def segment_reply(reply: str) -> list[str]:
    """Split one seat reply into atomic findings, preserving no-op as zero findings."""
    text = (reply or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x.get("statement", x)).strip() if isinstance(x, dict) else str(x).strip()
                    for x in data if str(x).strip()]
        if isinstance(data, dict) and "findings" in data and isinstance(data["findings"], list):
            return [str(x.get("statement", x)).strip() if isinstance(x, dict) else str(x).strip()
                    for x in data["findings"] if str(x).strip()]
    except json.JSONDecodeError:
        if text.startswith(("{", "[")):
            return []
    lines = [ln.strip(" -*\t") for ln in text.splitlines() if ln.strip()]
    candidates = [
        ln for ln in lines
        if not re.match(r"(?i)^(no issues|no findings|nothing to report|looks good)\b", ln)
    ]
    return candidates


def format_conformance(candidates: list[str]) -> tuple[float, int, int]:
    if not candidates:
        return (1.0, 0, 0)
    pattern = re.compile(r"^\s*([^|]+?)\s*\|\s*([^:|]+):(\d+)\s*\|\s*(.+?)\s*$")
    conforming = 0
    for line in candidates:
        m = pattern.match(line)
        if m and m.group(1).strip() in TAXONOMY_NAMES:
            conforming += 1
    return (conforming / len(candidates), conforming, len(candidates))


def _location(d: dict[str, Any]) -> dict[str, Any]:
    loc = d.get("location") or {}
    if loc is None:
        return {}
    return dict(loc)


def _safe_int(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _enclosing_function(repo: Path | None, file: str | None, line: int | None) -> str | None:
    """Compatibility wrapper for tests and older callers."""
    return boundary.enclosing_symbol(repo, file, line).symbol


def _weaker_oracle(a: str, b: str) -> str:
    order = {"heuristic": 0, "ctags": 1, "tree-sitter": 2}
    return a if order[a] <= order[b] else b


def match_finding(finding: dict[str, Any], seed: Seed, repo: Path | None = None,
                  window: int = 10) -> MatchDecision:
    """Match normalized finding to a seed via class + symbol/function/window overlap."""
    cls = finding.get("class", "unknown")
    if cls == "unknown" or cls != seed.cls:
        return MatchDecision("matcher-ambiguous" if cls == "unknown" else "detection-miss")
    loc = _location(finding)
    seed_loc = seed.location or {}
    if loc.get("file") != seed_loc.get("file"):
        return MatchDecision("detection-miss")
    if loc.get("symbol") and seed_loc.get("symbol") and loc["symbol"] == seed_loc["symbol"]:
        return MatchDecision("detected", "symbol")
    seed_line = _safe_int(seed_loc.get("line"))
    found_line = _safe_int(loc.get("line"))
    seed_func = boundary.enclosing_symbol(repo, seed_loc.get("file"), seed_line)
    found_func = boundary.enclosing_symbol(repo, loc.get("file"), found_line)
    if seed_func.symbol and found_func.symbol and seed_func.symbol == found_func.symbol:
        return MatchDecision("detected", "function", _weaker_oracle(seed_func.tier, found_func.tier))
    if seed_func.symbol and found_func.symbol and seed_func.symbol != found_func.symbol:
        # Both the finding and the seed resolve to functions, and they DIFFER. A line-window match
        # across a function boundary would conflate distinct loci — e.g. a seat false-positiving on a
        # clean control within 10 lines of a seed would be scored as detecting the seed (inflating
        # caught, hiding the noise). The finding belongs to its own function's locus, not this seed.
        return MatchDecision("detection-miss")
    if seed_line is not None and found_line is not None and abs(seed_line - found_line) <= window:
        return MatchDecision("detected", "window")
    return MatchDecision("matcher-ambiguous")


class _Recorder:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a")

    def write(self, event: str, **payload: Any) -> None:
        report.guard(payload)
        row = {"ts": time.time(), "run_id": self.run_id, "event": event, **payload}
        self._fh.write(json.dumps(row, sort_keys=True) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _target_from(sc: Scenario) -> power.PowerTarget:
    p = sc.power or {}
    d = power.PowerTarget()
    return power.PowerTarget(
        V=p.get("V", d.V),
        nu=p.get("nu", d.nu),
        alpha=p.get("alpha", d.alpha),
        I_min=p.get("I_min", d.I_min),
        R_min=p.get("R_min", d.R_min),
        matcher_gate=p.get("matcher_gate", d.matcher_gate),
        matcher_true_recall=p.get("matcher_true_recall", d.matcher_true_recall),
    )


def _repo_for(sc: Scenario) -> Path | None:
    repo = sc.subject.get("repo")
    if not repo:
        return None
    return Path(repo)


def _control_as_seed(locus: ControlLocus) -> Seed:
    return Seed(id=locus.id, cls=locus.cls, location=locus.location,
                description=locus.description, legible=True)


def _normalize_all(normalizer: Normalizer, atomics: list[str], context: dict[str, Any],
                   rec: _Recorder) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in atomics:
        rows = normalizer.normalize(raw, context)
        for row in rows:
            report.guard(row)
            normalized.append({
                "class": row.get("class", "unknown"),
                "location": dict(row.get("location") or {}),
                "severity": row.get("severity", "P2"),
                "statement": row.get("statement", raw),
                "confidence": row.get("confidence", 0.0),
                "raw": raw,
            })
    rec.write("normalized", context=context, count=len(normalized), findings=normalized)
    return normalized


def _counts_from_events(path: Path, seats: list[str], classes: list[str]) -> tuple[
    dict[tuple[str, str], int],
    dict[tuple[str, str], int],
    dict[tuple[str, str], int],
    dict[tuple[str, str], int],
]:
    # Both axes aggregate to the CLUSTER, and REPEATS ARE POOLED (not multiplied) — the trial unit is
    # (seat, cls, cluster), n = number of distinct clusters. Repeats of a cluster are correlated
    # (a deterministic seat repeats its outcome), so counting clusters x repeats would re-inflate the
    # Wilson n and over-claim; the true independent sample count is the cluster count. Repeats still
    # run and still push the POOLED outcome toward conservatism (decision panel: B, unanimous):
    #   - a NOISE cluster is "flagged" if ANY member control is flagged in ANY repeat (worst-case);
    #   - a CAUGHT cluster is "detected" only if EVERY member seed is detected in EVERY repeat.
    # Small n -> wide Wilson CI -> the CI self-limits; no hard gate is needed (P1 instance 5).
    seed_any_miss: dict[tuple[str, str, str], bool] = {}     # cluster -> any (member,repeat) not detected
    ctrl_any_flag: dict[tuple[str, str, str], bool] = {}     # cluster -> any (member,repeat) flagged
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("event") != "matcher_decision":
                continue
            seat = row.get("seat")
            cls = row.get("cls")
            if seat not in seats or cls not in classes:
                continue
            locus = row.get("locus")
            if locus == "seed":
                cluster = row.get("cluster") or f"__id__:{row.get('seed_id')}"
                key = (seat, cls, cluster)
                seed_any_miss[key] = seed_any_miss.get(key, False) or (row.get("outcome") != "detected")
            elif locus == "control":
                cluster = row.get("cluster") or f"__id__:{row.get('control_id')}"
                key = (seat, cls, cluster)
                ctrl_any_flag[key] = ctrl_any_flag.get(key, False) or (row.get("outcome") == "flagged")
    detected = {(s, c): 0 for s in seats for c in classes}
    totals = {(s, c): 0 for s in seats for c in classes}
    for (seat, cls, _cluster), any_miss in seed_any_miss.items():
        totals[(seat, cls)] += 1
        if not any_miss:  # every member seed detected in every repeat
            detected[(seat, cls)] += 1
    noise = {(s, c): 0 for s in seats for c in classes}
    noise_n = {(s, c): 0 for s in seats for c in classes}
    for (seat, cls, _cluster), flagged in ctrl_any_flag.items():
        noise_n[(seat, cls)] += 1
        if flagged:
            noise[(seat, cls)] += 1
    return detected, totals, noise, noise_n


def _incomplete_seats(path: Path) -> dict[str, list[tuple[str, int]]]:
    incomplete: dict[str, list[tuple[str, int]]] = {}
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("event") != "incomplete_repeat":
                continue
            seat = row.get("seat")
            kind = row.get("kind")
            repeat = row.get("repeat")
            if seat and kind is not None and repeat is not None:
                incomplete.setdefault(str(seat), []).append((str(kind), int(repeat)))
    return incomplete


def _matcher_ambiguous_counts(path: Path) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("event") == "matcher_decision" and row.get("outcome") == "matcher-ambiguous":
                key = (row.get("seat"), row.get("cls"))
                counts[key] = counts.get(key, 0) + 1
    return counts


def _format_conformance_means(path: Path) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("event") != "segmented":
                continue
            seat = (row.get("task") or {}).get("seat")
            if seat:
                values.setdefault(seat, []).append(float(row.get("format_conformance", 1.0)))
    return {seat: sum(rows) / len(rows) for seat, rows in values.items() if rows}


def _gold_suppressed(summary: dict[str, Any] | None) -> bool:
    if not summary:
        return False
    gate = float(summary.get("matcher_gate", 0.85))
    recall = summary.get("recall") or {}
    return float(recall.get("lo", 0.0)) < gate


def _assert_no_answer_key_in_fixture(scenario: Scenario, repo: str) -> None:
    """In-band-leak pre-flight (jail-INDEPENDENT). The seat reads the fixture fully — including
    untracked files and ALL git objects — so a seed/control ID anywhere in that surface is the
    answer-key inside what the seat may read; OS read-confinement can't stop an in-band leak.
    Covers the FULL read-surface: every file in the working tree (tracked AND untracked — a jailed seat
    sees both via the bind mount), plus EVERY git object via `git cat-file --batch-all-objects`
    (loose + packed, reachable or not — so dangling/reflog/stash/packed-unreachable bodies a seat could
    recover with `git cat-file`/`git fsck` are covered; `git log --all -p` walks refs only and misses
    those — the residual the pre-merge re-review caught). Greps for the unique seed/control IDs (NOT
    descriptive cluster tags, which coincide with code — e.g. a 'clamp' cluster vs a clamp() function).
    Bound: cat-file dumps object BODIES, not tree path-names, so a token living ONLY as a historical
    filename (never in any blob/message) isn't caught here — but the working-tree scan catches live
    filenames, and an answer-key id is a content token, not a filename-only artifact. Park on any hit."""
    repo_p = Path(repo)
    if not (repo_p / ".git").exists():
        return  # not an isolated fixture checkout (mock / '.') — nothing to vet
    ids = [s.id for s in scenario.seeds] + [c.id for c in scenario.control_loci]
    ids = [i for i in ids if i]
    if not ids:
        return
    hits: list[str] = []
    # working tree: ALL files (tracked AND untracked), excluding .git internals (history covered below)
    for f in repo_p.rglob("*"):
        if not f.is_file() or ".git" in f.parts:
            continue
        try:
            txt = f.read_text(errors="ignore")
        except OSError:
            continue
        hits += [f"{f.relative_to(repo_p)}:{i}" for i in ids if i in txt]
    # full git history and all objects (tracked, untracked, reflog, stash, dangling, packed)
    hist = subprocess.run(["git", "-C", str(repo_p), "cat-file", "--batch-all-objects", "--batch"],
                          capture_output=True, text=True, errors="ignore").stdout
    hits += [f"<git-history>:{i}" for i in ids if i in hist]
    if hits:
        raise Parked(
            "fixture leaks seed/control identity — the answer-key is inside what the seat reads: "
            + ", ".join(sorted(set(hits))[:8])
            + ". Scrub the fixture source/history to be marker-free before a scored run."
        )


def run_floor(scenario: Scenario, dispatcher: Dispatcher | None = None,
              normalizer: Normalizer | None = None,
              normalizer_seat: str = UNRESOLVED_OFFQUORUM,
              output_root: str | Path | None = None,
              repeats: int | None = None,
              seats: tuple[str, ...] | None = None,
              matcher_window: int = 10) -> RunResult:
    """Run one floor scenario end-to-end.

    Tests inject MockDispatcher/MockNormalizer. Live runs must configure an off-quorum normalizer
    seat; the sentinel parks instead of silently using a quorum seat.
    """
    dispatcher = dispatcher or BridgeDispatcher()
    panel_seats = {p.seat for p in scenario.panel}
    if normalizer is None:
        if normalizer_seat == UNRESOLVED_OFFQUORUM:
            raise Parked("off-quorum normalizer seat unresolved — parked")
        if normalizer_seat in panel_seats:
            raise Parked("off-quorum normalizer seat is a panel seat — parked")
        normalizer = DispatcherNormalizer(dispatcher, normalizer_seat)

    target = _target_from(scenario)
    budget = power.compute(target)
    run_repeats = repeats if repeats is not None else budget.repeats
    control_loci = list(scenario.control_loci)
    # Power is met by EFFECTIVE controls (distinct why-clean clusters), not nominal loci: correlated
    # controls don't buy independent confidence (measurement-principles P1 #5).
    control_counts = scenario.control_cluster_count_per_class()
    control_nominal = scenario.control_count_per_class()
    # Effective seed count = distinct MECHANISMS (not locations). Used for class-level ELIGIBILITY
    # (>= I_min mechanisms; one-mechanism/many-location -> INSTANCE-LEVEL) and the SMALL-N report line.
    # Correlated seeds don't over-claim because caught is computed on these clusters with repeats POOLED
    # (no inflation) — there is no hard under-T rail (measurement-principles P1 #5; decision panel B).
    seed_cluster_counts = scenario.seed_cluster_count_per_class()

    run_id = f"{scenario.id}-{uuid.uuid4().hex[:8]}"
    provenance_nonce = uuid.uuid4().hex
    if isinstance(dispatcher, BridgeDispatcher) and dispatcher.run_id is None:
        dispatcher.run_id = run_id
    root = Path(output_root) if output_root is not None else Path("floor")
    run_dir = root / "runs" / run_id
    events_path = run_dir / "events.ndjson"
    report_path = run_dir / "report.txt"
    detail_path = run_dir / "detail.json"
    rec = _Recorder(events_path, run_id)
    repo = _repo_for(scenario)
    subject_repo = scenario.subject.get("repo", ".")
    subject_base = scenario.subject.get("base", "")
    subject_head = scenario.subject.get("head", "")

    # Integrity gates (fail-closed), live dispatch only — mock dispatchers don't touch a real seat:
    #  (1) read-confinement: the seat must be jailed to the fixture (else it can read the answer-key);
    #  (2) in-band leak: even jailed, the fixture the seat reads must not NAME its own seeds (OS
    #      confinement can't stop a leak that lives inside what the seat is allowed to read).
    if isinstance(dispatcher, BridgeDispatcher):
        dispatcher.assert_read_confined(subject_repo)        # un-confined: Park unless explicit override
        _assert_no_answer_key_in_fixture(scenario, subject_repo)
    elif getattr(dispatcher, "CONFINED", False):
        # confined-by-construction: the canary inside each confined dispatch is the per-run proof of
        # absence (not a settable env var), so this dispatcher satisfies the confinement requirement.
        _assert_no_answer_key_in_fixture(scenario, subject_repo)

    seats = [p.seat for p in scenario.panel if seats is None or p.seat in seats]
    for seat in seats:
        report.guard({seat: "seat-id"})
    classes = scenario.classes()
    claim_levels = {
        cls: ("CLASS-LEVEL" if ok else "INSTANCE-LEVEL")
        for cls, ok in scenario.class_level_ok(target.I_min).items()
    }
    details: dict[str, Any] = {
        "run_id": run_id,
        "gold_status": GOLD_UNADJUDICATED,
        "claim_levels": claim_levels,
        "classes": classes,
        "budget": {
            "trials": budget.trials,
            "instances": budget.instances,
            "repeats": budget.repeats,
            "control_loci_required": budget.control_loci_required,
            "actual_repeats": run_repeats,
            "actual_control_loci": len(control_loci),
            "actual_control_clusters_per_class": control_counts,
            "actual_control_loci_per_class": control_nominal,
        },
        "oracle": {},
    }
    oracle_by_language = {
        language: boundary.best_oracle_for_language(language)
        for language in scenario.subject.get("languages", [])
    }
    gold_versions = {p.seat: GOLD_UNADJUDICATED for p in scenario.panel}
    prov = provenance.collect(
        scenario,
        dispatcher=dispatcher,
        normalizer=normalizer,
        oracle_by_language=oracle_by_language,
        gold_versions=gold_versions,
        image=None,
        harness_root=Path(__file__).resolve().parents[1],
        run_id=run_id,
    )
    details["provenance"] = prov

    try:
        rec.write("scenario_loaded", scenario_id=scenario.id, classes=classes, seats=seats,
                  gold_status=GOLD_UNADJUDICATED)
        rec.write("provenance", provenance=prov)
        provenance_seen: set[str] = set()
        for seat in seats:
            seat_model = next((p.model for p in scenario.panel if p.seat == seat), "?")
            for repeat in range(run_repeats):
                task = {
                    "kind": "review",
                    "seat": seat,
                    "repeat": repeat,
                    "model": seat_model,
                    "provenance_nonce": provenance_nonce,
                    "repo": subject_repo,
                    "base": subject_base,
                    "head": subject_head,
                    "task_id": f"review:{seat}:{repeat}",
                }
                rec.write("dispatch_start", task=task)
                dispatch_failed = False
                try:
                    reply = dispatcher.dispatch(task)
                    rec.write("dispatch_end", task=task, ok=True)
                    if seat not in provenance_seen and hasattr(dispatcher, "last_engine_versions"):
                        engine_versions = dict(getattr(dispatcher, "last_engine_versions", {}) or {})
                        model_reported = getattr(dispatcher, "last_model_reported", None)
                        model_source = getattr(dispatcher, "last_model_source", "cli-version-only")
                        rec.write("provenance_engine", seat=seat, engine_versions=engine_versions,
                                  model_reported=model_reported, model_source=model_source)
                        prov["engine_versions"][seat] = engine_versions
                        if seat in prov["model"]:
                            prov["model"][seat]["model_reported"] = model_reported
                            prov["model"][seat]["model_source"] = model_source
                            command = getattr(dispatcher, "last_confined_command", "")
                            if command:
                                prov["model"][seat]["confined_command"] = command
                        provenance_seen.add(seat)
                    rec.write("finding_emitted", task=task, raw=reply)
                    atomics = segment_reply(reply)
                    frac, conforming, candidate_count = format_conformance(atomics)
                    rec.write("segmented", task=task, findings=atomics,
                              format_conformance=frac, conforming_lines=conforming,
                              candidate_lines=candidate_count)
                    normalized = _normalize_all(normalizer, atomics, task, rec)
                except DispatchError as e:
                    if e.kind == "canary":
                        raise Parked(f"confined canary failed for {seat} repeat={repeat}: {e}") from e
                    rec.write("dispatch_error", task=task, seat=seat, repeat=repeat,
                              kind=e.kind, error=str(e))
                    rec.write("incomplete_repeat", seat=seat, repeat=repeat, kind=e.kind,
                              classes=classes)
                    continue

                # A normalized finding is classified ONCE: a detection (seed) takes precedence over
                # a control flag, and a finding flags at most one control. Without this, a finding
                # co-located with both a seed and a nearby control double-counts (detection AND
                # noise), and a finding near several controls over-counts noise. (D3 gate.)
                consumed: set[int] = set()  # finding indices already counted as a seed detection
                for seed in scenario.seeds:
                    decision = MatchDecision("detection-miss", "dispatch-error" if dispatch_failed else None)
                    for idx, row in enumerate(normalized):
                        m = match_finding(row, seed, repo=repo, window=matcher_window)
                        if m.outcome == "detected":
                            decision = m
                            consumed.add(idx)
                            break
                        if m.outcome == "matcher-ambiguous":
                            decision = m
                    rec.write("matcher_decision", task=task, locus="seed", seed_id=seed.id,
                              cluster=cluster_key(seed), seat=seat,
                              cls=seed.cls, outcome=decision.outcome, basis=decision.basis,
                              **({"boundary_oracle": decision.boundary_oracle}
                                 if decision.basis == "function" else {}))

                flagged: set[int] = set()  # finding indices already assigned to a control flag
                for locus in control_loci:
                    decision = MatchDecision("detection-miss", "dispatch-error" if dispatch_failed else None)
                    control_seed = _control_as_seed(locus)
                    for idx, row in enumerate(normalized):
                        if idx in consumed or idx in flagged:
                            continue  # seed precedence + one-control-per-finding
                        m = match_finding(row, control_seed, repo=repo, window=matcher_window)
                        if m.outcome == "detected":
                            decision = m
                            flagged.add(idx)
                            break
                        if m.outcome == "matcher-ambiguous":
                            decision = m
                    outcome = "flagged" if decision.outcome == "detected" else "clean"
                    rec.write("matcher_decision", task=task, locus="control", control_id=locus.id,
                              cluster=cluster_key(locus),
                              seat=seat, cls=locus.cls, outcome=outcome, basis=decision.basis,
                              **({"boundary_oracle": decision.boundary_oracle}
                                 if decision.basis == "function" else {}))

        verdicts: dict[str, dict[str, str]] = {s: {} for s in seats}
        class_level_classes = [c for c in classes if claim_levels[c] == "CLASS-LEVEL"]
        rec.close()
        detected, totals, noise, noise_n = _counts_from_events(events_path, seats, classes)
        incomplete_by_seat = _incomplete_seats(events_path)
        matcher_ambiguous = _matcher_ambiguous_counts(events_path)
        format_means = _format_conformance_means(events_path)
        rec = _Recorder(events_path, run_id)
        gold_summaries = {seat: gold.load_summary(seat) for seat in seats}
        for seat, summary in gold_summaries.items():
            if summary and summary.get("gold_version"):
                gold_versions[seat] = str(summary["gold_version"])
        prov["gold_versions"] = dict(gold_versions)
        suppressed_lines: list[str] = []
        suppressed_seats: list[str] = []
        for seat in seats:
            details["oracle"][seat] = {}
            for cls in classes:
                # No hard under-T gate (decision panel: B, unanimous). Repeats are POOLED in
                # _counts_from_events, so n is the true independent cluster count and the Wilson CI is
                # genuinely wide at small n — it self-limits. A clean seat can PASS at <T clusters when
                # the CIs really separate (valid inference); a one-mechanism scenario stays UNKNOWN via
                # the wide 1-cluster CI, NOT via a gate. effective cluster counts are recorded for the
                # reader (read verdicts alongside caught_n/noise_n, never the bare grid).
                result: ViabilityResult = classify(
                    detected[(seat, cls)], totals[(seat, cls)], noise[(seat, cls)], noise_n[(seat, cls)],
                    target.alpha,
                )
                verdict = result.verdict
                if seat in incomplete_by_seat:
                    verdict = UNKNOWN
                details["oracle"][seat][cls] = {
                    "claim_level": claim_levels[cls],
                    "verdict": verdict,
                    "effective_control_clusters": control_counts.get(cls, 0),
                    "effective_seed_clusters": seed_cluster_counts.get(cls, 0),
                    "clusters_for_full_power": budget.control_loci_required,
                    "caught_k": result.caught_k,
                    "caught_n": result.caught_n,
                    "noise_k": result.noise_k,
                    "noise_n": result.noise_n,
                    "caught_lo": result.caught_lo,
                    "caught_hi": result.caught_hi,
                    "noise_lo": result.noise_lo,
                    "noise_hi": result.noise_hi,
                    "matcher_ambiguous_n": matcher_ambiguous.get((seat, cls), 0),
                    "matcher_band": (
                        {
                            "recall_lo": gold_summaries[seat]["recall"]["lo"],
                            "recall_hi": gold_summaries[seat]["recall"]["hi"],
                        }
                        if gold_summaries.get(seat) else None
                    ),
                    "format_conformance_mean": format_means.get(seat, 1.0),
                }
                rec.write("oracle_result", seat=seat, cls=cls, claim_level=claim_levels[cls],
                          verdict=verdict, caught_k=result.caught_k, caught_n=result.caught_n,
                          noise_k=result.noise_k, noise_n=result.noise_n)
                if claim_levels[cls] == "CLASS-LEVEL":
                    verdicts[seat][cls] = verdict
            summary = gold_summaries.get(seat)
            if _gold_suppressed(summary):
                gate = float(summary.get("matcher_gate", 0.85))
                lo = float((summary.get("recall") or {}).get("lo", 0.0))
                verdicts.pop(seat, None)
                suppressed_seats.append(seat)
                suppressed_lines.append(
                    f"SUPPRESSED: {seat} matcher recall lo={lo:.3f} < gate={gate:.2f} — rows withheld"
                )

        grid = report.render_grid(verdicts, class_level_classes)
        instance_lines = [f"INSTANCE-LEVEL only: {c} ({seed_cluster_counts.get(c, 0)} distinct "
                          f"mechanism(s) [{scenario.instances_per_class()[c]} seed location(s)] < "
                          f"I_min={target.I_min}); no class-level PASS/FAIL emitted"
                          for c in classes if claim_levels[c] == "INSTANCE-LEVEL"]
        # Small-n is informational, NOT a gate (decision panel B): below full-power T the Wilson CI is
        # wide, so only genuinely-separated data PASSes; read verdicts alongside caught_n/noise_n.
        control_lines = [f"SMALL-N controls: {c} ({control_counts.get(c, 0)} effective control clusters "
                         f"[{control_nominal.get(c, 0)} nominal], full power at {budget.control_loci_required}); "
                         f"noise CI is wide — verdict rests on real Wilson separation, not a gate"
                         for c in classes if control_counts.get(c, 0) < budget.control_loci_required]
        seed_lines = [f"SMALL-N seeds: {c} ({seed_cluster_counts.get(c, 0)} effective seed mechanisms "
                      f"[{scenario.instances_per_class().get(c, 0)} nominal locations], full power at "
                      f"{budget.control_loci_required}); caught CI is wide — only a clean, well-separated "
                      f"seat PASSes here"
                      for c in classes if seed_cluster_counts.get(c, 0) < budget.control_loci_required]
        # Control loci in a class with no seed have no viability to score: they are written to the
        # log then dropped from the grid. Surface them, never swallow them silently (D3 gate).
        orphan_lines = [f"ORPHAN control class: {c} ({control_nominal[c]} control locus/loci, no seed "
                        f"of this class); these loci are not scored"
                        for c in sorted(set(control_counts) - set(classes))]
        infra_lines = [
            f"infra_incomplete({seat}, kind={kind}, repeat={repeat})"
            for seat, rows in incomplete_by_seat.items()
            for kind, repeat in rows
        ]
        details["report_lines"] = {
            "small_n": [*control_lines, *seed_lines],
            "orphan": orphan_lines,
            "infra_incomplete": infra_lines,
        }
        details["suppressed_seats"] = suppressed_seats
        warning = f"[gold] {GOLD_UNADJUDICATED}: placeholder gold set; floor verdicts are not trusted"
        report_text = "\n".join([warning, *instance_lines, *control_lines, *seed_lines,
                                 *orphan_lines, *infra_lines, *suppressed_lines, "", grid])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text)
        report.guard(details)
        detail_path.write_text(json.dumps(details, indent=2, sort_keys=True))
        rec.write("run_end", ok=True, report=str(report_path), detail=str(detail_path))
    finally:
        rec.close()

    return RunResult(
        run_id=run_id,
        events_path=str(events_path),
        report_path=str(report_path),
        detail_path=str(detail_path),
        report_text=report_text,
        verdicts=verdicts,
        claim_levels=claim_levels,
        gold_unadjudicated=True,
    )

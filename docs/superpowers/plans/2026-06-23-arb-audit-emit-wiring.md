# ARB audit-emit panel wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire real ARB review/design panels to record an audit trail (roster manifest + per-seat votes + verdict), correlation-keyed by one `run_id`, such that doneness-laundering fails loud.

**Architecture:** One shared stance parser extracts a machine-readable vote block from raw reply text for both transports (bridge via `agent-dispatch --audit-panel`, in-session via the orchestrator). An orchestrator-emitted roster **manifest at seq 1** is the un-trimmable ground truth. The verdict emit is gated by a fail-loud reconciler that reads the committed `audit_events` rows and refuses unless the roster reconciles. Spec: `docs/superpowers/specs/2026-06-23-arb-audit-emit-wiring-design.md` (v2).

**Tech Stack:** Python 3.12+ (`arb_memory` package, `src/` layout), `psycopg` (Postgres), `redis-py` (DO Valkey db3 audit bus), bash (`agent-dispatch`), pytest.

## Global Constraints

- Audit bus is **DO Valkey db3** via `ARB_MEMORY_REDIS_URL`; Postgres via `ARB_MEMORY_DSN`. Prefix-isolatable via `ARB_MEMORY_PREFIX` (`PREFIX` in `audit.py`).
- Audit payload hard cap **16384 bytes** (`AUDIT_MAX_PAYLOAD_BYTES`, `audit.py`); `audit_emit` raises above it. Verdict payload = stances + short rationale + pointers, never full reply text.
- Stance enum: `approve | needs-changes | block | abstain | timed-out`. Severity enum: `none | P2 | P1 | P0`.
- Audit event payload shapes (canonical):
  - dispatch (manifest): `{"kind":"dispatch","roster":["seat:x",...],"task":"...","branch":"..."}`
  - vote: `{"kind":"vote","actor":"seat:x","stance":"approve","severity":"none","refs":[...],"note":"..."}`
  - verdict: `{"kind":"verdict","decision":"...","roster":["seat:x",...],"stances":{"seat:x":"approve",...},"rationale":"...","refs":{...}}`
- `--run-id` ALONE must never trigger audit emit; the explicit `--audit-panel` flag gates it (eval-trace also threads `run_id`).
- Existing helpers (do not reimplement): `AuditRun(redis, run_id, prefix).emit(source, kind, payload)` (`audit.py`), `audit_lag(redis, prefix=...) -> {"pending","lag",...}` (`audit.py:214`), `AuditConsumer(redis, conn_factory, prefix=...)` with `.start()/.drain_pending()/.step()` (`audit.py:236`).
- `scratch` pytest fixture (`tests/arb_memory/conftest.py:77`) yields an autocommit `psycopg` conn on a temp schema with `schema.sql` applied; skips when `ARB_MEMORY_DSN` unset.
- TDD always: failing test first, watch it fail, minimal impl, watch it pass, commit. Each task ends green.

---

### Task 1: `stance.py` — the shared stance-block parser

**Files:**
- Create: `src/arb_memory/stance.py`
- Test: `tests/arb_memory/test_stance.py`

**Interfaces:**
- Produces: `parse_stance(text: str) -> dict` returning `{"stance":str,"severity":str,"refs":list,"note":str}`; raises `StanceError` (subclass of `ValueError`) on absent/malformed/wrong-enum. Module constants `STANCES: set[str]`, `SEVERITIES: set[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_stance.py
import pytest
from arb_memory.stance import parse_stance, StanceError, STANCES, SEVERITIES

VALID = '''Here is my review.
```vote
{"stance":"block","severity":"P0","refs":["protocol.py:9"],"note":"bad"}
```'''

def test_parses_vote_fenced_block():
    out = parse_stance(VALID)
    assert out == {"stance":"block","severity":"P0","refs":["protocol.py:9"],"note":"bad"}

def test_accepts_json_tagged_fence():
    out = parse_stance('```json\n{"stance":"approve","severity":"none","refs":[],"note":""}\n```')
    assert out["stance"] == "approve"

def test_accepts_bare_trailing_object():
    out = parse_stance('prose...\n{"stance":"approve","severity":"none","refs":[],"note":"ok"}')
    assert out["stance"] == "approve"

def test_uses_last_block_when_multiple():
    txt = '```vote\n{"stance":"approve","severity":"none","refs":[],"note":"a"}\n```\n' \
          '```vote\n{"stance":"block","severity":"P1","refs":[],"note":"b"}\n```'
    assert parse_stance(txt)["stance"] == "block"

def test_timed_out_is_valid_stance():
    assert "timed-out" in STANCES
    assert parse_stance('```vote\n{"stance":"timed-out","severity":"none","refs":[],"note":""}\n```')["stance"] == "timed-out"

def test_absent_block_raises():
    with pytest.raises(StanceError):
        parse_stance("no vote here at all")

def test_malformed_json_raises():
    with pytest.raises(StanceError):
        parse_stance('```vote\n{"stance":"approve", oops}\n```')

def test_wrong_enum_raises():
    with pytest.raises(StanceError):
        parse_stance('```vote\n{"stance":"yolo","severity":"none","refs":[],"note":""}\n```')

def test_missing_severity_raises():
    with pytest.raises(StanceError):
        parse_stance('```vote\n{"stance":"approve","refs":[],"note":""}\n```')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_stance.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'arb_memory.stance'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/arb_memory/stance.py
"""Parse a panel seat's structured stance block out of raw reply text.

One parser for BOTH transports (bridge reply text + in-session final message). NOT the bridge's
--expect-structured field (a fixed status schema that cannot carry a stance block; see spec v2/P0).
"""
import json
import re

STANCES = {"approve", "needs-changes", "block", "abstain", "timed-out"}
SEVERITIES = {"none", "P2", "P1", "P0"}

_FENCE = re.compile(r"```(?:vote|json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


class StanceError(ValueError):
    """Raised when a reply has no parseable / valid stance block. Fail-loud; never invent a vote."""


def _candidates(text):
    # Fenced blocks first (last wins), then a bare trailing {...} object.
    for m in reversed(list(_FENCE.finditer(text))):
        yield m.group(1)
    start = text.rfind("{")
    if start != -1:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
            yield json.dumps(value)
        except json.JSONDecodeError:
            return


def parse_stance(text: str) -> dict:
    raw = next(_candidates(text), None)
    if raw is None:
        raise StanceError("no stance block found in reply")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StanceError(f"stance block is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise StanceError("stance block is not an object")
    stance = value.get("stance")
    severity = value.get("severity")
    if stance not in STANCES:
        raise StanceError(f"invalid stance {stance!r}; must be one of {sorted(STANCES)}")
    if severity not in SEVERITIES:
        raise StanceError(f"invalid severity {severity!r}; must be one of {sorted(SEVERITIES)}")
    return {
        "stance": stance,
        "severity": severity,
        "refs": list(value.get("refs", [])),
        "note": str(value.get("note", "")),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_stance.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/stance.py tests/arb_memory/test_stance.py
git commit -m "feat(arb): stance.py — shared stance-block parser (vote/json/bare fence, fail-loud)"
```

---

### Task 2: `panel_audit.reconcile` — the 5 roster assertions

**Files:**
- Create: `src/arb_memory/panel_audit.py`
- Test: `tests/arb_memory/test_panel_audit.py`

**Interfaces:**
- Produces: `reconcile(conn, run_id, verdict_payload, *, redis=None, poll_timeout_s=30.0, poll_interval_s=0.25) -> dict` returning `{"ok":bool, "incomplete":bool, "gaps":list[str]}`. This task implements the assertions with `redis=None` (no poll); Task 3 adds the bounded poll.
- Consumes: `audit_events` rows `(run_id, seq, source, kind, payload jsonb)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_panel_audit.py
import json
import pytest
from psycopg.types.json import Jsonb
from arb_memory.panel_audit import reconcile

ROSTER = ["seat:codex", "seat:cold-opus"]

def _row(conn, run_id, seq, kind, payload):
    conn.execute(
        "INSERT INTO audit_events (run_id, seq, source, kind, payload) VALUES (%s,%s,%s,%s,%s)",
        (run_id, seq, "orchestrator", kind, Jsonb(payload)),
    )

def _good_panel(conn, run_id):
    _row(conn, run_id, 1, "dispatch", {"kind":"dispatch","roster":ROSTER,"task":"t","branch":"dev"})
    _row(conn, run_id, 2, "vote", {"kind":"vote","actor":"seat:codex","stance":"approve"})
    _row(conn, run_id, 3, "vote", {"kind":"vote","actor":"seat:cold-opus","stance":"needs-changes"})

VERDICT = {"kind":"verdict","roster":ROSTER,
           "stances":{"seat:codex":"approve","seat:cold-opus":"needs-changes"}}

def test_clean_panel_reconciles(scratch):
    rid = "panel-clean"
    _good_panel(scratch, rid)
    out = reconcile(scratch, rid, VERDICT)
    assert out == {"ok": True, "incomplete": False, "gaps": []}

def test_missing_manifest_fails(scratch):
    rid = "panel-nomanifest"
    _row(scratch, rid, 1, "vote", {"kind":"vote","actor":"seat:codex","stance":"approve"})
    out = reconcile(scratch, rid, VERDICT)
    assert out["ok"] is False and any("manifest" in g for g in out["gaps"])

def test_manifest_not_seq1_fails(scratch):
    rid = "panel-lateman"
    _row(scratch, rid, 1, "vote", {"kind":"vote","actor":"seat:codex","stance":"approve"})
    _row(scratch, rid, 2, "vote", {"kind":"vote","actor":"seat:cold-opus","stance":"needs-changes"})
    _row(scratch, rid, 3, "dispatch", {"kind":"dispatch","roster":ROSTER,"task":"t","branch":"dev"})
    out = reconcile(scratch, rid, VERDICT)
    assert out["ok"] is False and any("seq" in g or "precede" in g for g in out["gaps"])

def test_missing_vote_fails(scratch):
    rid = "panel-missvote"
    _row(scratch, rid, 1, "dispatch", {"kind":"dispatch","roster":ROSTER,"task":"t","branch":"dev"})
    _row(scratch, rid, 2, "vote", {"kind":"vote","actor":"seat:codex","stance":"approve"})
    out = reconcile(scratch, rid, VERDICT)
    assert out["ok"] is False and any("cold-opus" in g for g in out["gaps"])

def test_unrostered_vote_fails(scratch):
    rid = "panel-extra"
    _good_panel(scratch, rid)
    _row(scratch, rid, 4, "vote", {"kind":"vote","actor":"seat:ghost","stance":"approve"})
    out = reconcile(scratch, rid, VERDICT)
    assert out["ok"] is False and any("ghost" in g for g in out["gaps"])

def test_verdict_stance_mismatch_fails(scratch):
    rid = "panel-mismatch"
    _good_panel(scratch, rid)
    bad = {"kind":"verdict","roster":ROSTER,
           "stances":{"seat:codex":"approve","seat:cold-opus":"approve"}}  # voted needs-changes
    out = reconcile(scratch, rid, bad)
    assert out["ok"] is False and any("cold-opus" in g for g in out["gaps"])

def test_timed_out_vote_satisfies_roster(scratch):
    rid = "panel-timeout"
    _row(scratch, rid, 1, "dispatch", {"kind":"dispatch","roster":ROSTER,"task":"t","branch":"dev"})
    _row(scratch, rid, 2, "vote", {"kind":"vote","actor":"seat:codex","stance":"approve"})
    _row(scratch, rid, 3, "vote", {"kind":"vote","actor":"seat:cold-opus","stance":"timed-out"})
    v = {"kind":"verdict","roster":ROSTER,
         "stances":{"seat:codex":"approve","seat:cold-opus":"timed-out"}}
    out = reconcile(scratch, rid, v)
    assert out == {"ok": True, "incomplete": False, "gaps": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_panel_audit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'arb_memory.panel_audit'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/arb_memory/panel_audit.py
"""Reconcile a panel's committed audit rows against its verdict — the fail-loud roster guard (spec v2).

reconcile() is the only thing standing between a verdict and the audit log. It refuses (ok=False) unless
the manifest is seq-1, precedes every vote, every rostered seat has exactly one terminal vote, no
unrostered votes exist, and the verdict's per-seat stances match the vote rows. Ground truth is the
committed rows, never the orchestrator's recollection.
"""


def _load_rows(conn, run_id):
    rows = conn.execute(
        "SELECT seq, kind, payload FROM audit_events WHERE run_id = %s ORDER BY seq",
        (run_id,),
    ).fetchall()
    return [{"seq": int(s), "kind": k, "payload": p} for (s, k, p) in rows]


def _assert(rows, verdict_payload):
    gaps = []
    manifests = [r for r in rows if r["kind"] == "dispatch"]
    votes = [r for r in rows if r["kind"] == "vote"]

    if len(manifests) != 1:
        gaps.append(f"expected exactly 1 dispatch manifest, found {len(manifests)}; run un-auditable")
        return gaps
    manifest = manifests[0]
    if manifest["seq"] != 1:
        gaps.append(f"manifest seq is {manifest['seq']}, must be 1 (manifest must be committed first)")
    if votes and min(v["seq"] for v in votes) <= manifest["seq"]:
        gaps.append("manifest does not precede all votes; roster not trustworthy")

    roster = set(manifest["payload"].get("roster", []))
    if not roster:
        gaps.append("manifest declares an empty roster")

    vote_by_actor = {}
    for v in votes:
        actor = v["payload"].get("actor")
        if actor not in roster:
            gaps.append(f"unrostered vote from {actor!r}")
            continue
        if actor in vote_by_actor:
            gaps.append(f"duplicate vote for {actor!r}")
        vote_by_actor[actor] = v["payload"].get("stance")

    for seat in roster:
        if seat not in vote_by_actor:
            gaps.append(f"seat {seat!r} declared in roster but never voted")

    stances = verdict_payload.get("stances", {})
    if set(stances) != roster:
        gaps.append(f"verdict stances keys {sorted(stances)} != roster {sorted(roster)}")
    for seat, claimed in stances.items():
        actual = vote_by_actor.get(seat)
        if actual is not None and claimed != actual:
            gaps.append(f"verdict claims {seat}={claimed!r} but vote row says {actual!r}")
    return gaps


def reconcile(conn, run_id, verdict_payload, *, redis=None, poll_timeout_s=30.0, poll_interval_s=0.25):
    rows = _load_rows(conn, run_id)
    gaps = _assert(rows, verdict_payload)
    return {"ok": not gaps, "incomplete": False, "gaps": gaps}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_panel_audit.py -v`
Expected: PASS (7 passed) — skips if `ARB_MEMORY_DSN` unset.

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/panel_audit.py tests/arb_memory/test_panel_audit.py
git commit -m "feat(arb): panel_audit.reconcile — 5 roster assertions (seq-1, precedence, votes, stance-match)"
```

---

### Task 3: bounded-poll drain — `incomplete → refuse`

**Files:**
- Modify: `src/arb_memory/panel_audit.py`
- Test: `tests/arb_memory/test_panel_audit_poll.py`

**Interfaces:**
- Produces: `_poll_until_stable(count_fn, lag_fn, *, timeout_s, interval_s, sleep, now) -> bool` (pure; injectable clock). `reconcile(..., redis=<client>)` now polls before asserting and returns `incomplete=True` (ok=False) if rows never stabilize.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_panel_audit_poll.py
from arb_memory.panel_audit import _poll_until_stable

class FakeClock:
    def __init__(self): self.t = 0.0
    def now(self): return self.t
    def sleep(self, s): self.t += s

def test_stabilizes_when_count_steady_and_lag_zero():
    clk = FakeClock()
    counts = iter([1, 2, 3, 3])           # stable on the 3==3 read
    lags = iter([{"pending":0,"lag":0}] * 4)
    assert _poll_until_stable(lambda: next(counts), lambda: next(lags),
                              timeout_s=30, interval_s=0.25, sleep=clk.sleep, now=clk.now) is True

def test_incomplete_when_lag_never_clears():
    clk = FakeClock()
    assert _poll_until_stable(lambda: 5, lambda: {"pending":3,"lag":1},
                              timeout_s=1, interval_s=0.25, sleep=clk.sleep, now=clk.now) is False

def test_incomplete_when_count_keeps_growing():
    clk = FakeClock()
    n = iter(range(1, 100))
    assert _poll_until_stable(lambda: next(n), lambda: {"pending":0,"lag":0},
                              timeout_s=1, interval_s=0.25, sleep=clk.sleep, now=clk.now) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_panel_audit_poll.py -v`
Expected: FAIL with `ImportError: cannot import name '_poll_until_stable'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/arb_memory/panel_audit.py` (top, after the docstring) the import and helper, and wire it into `reconcile`:

```python
import time

from .audit import audit_lag


def _poll_until_stable(count_fn, lag_fn, *, timeout_s, interval_s, sleep, now):
    deadline = now() + timeout_s
    prev = None
    while now() <= deadline:
        count = count_fn()
        lag = lag_fn()
        if count == prev and lag.get("pending", 0) == 0 and lag.get("lag", 0) == 0:
            return True
        prev = count
        sleep(interval_s)
    return False
```

Replace the body of `reconcile` with:

```python
def reconcile(conn, run_id, verdict_payload, *, redis=None, poll_timeout_s=30.0, poll_interval_s=0.25):
    if redis is not None:
        def _count():
            return conn.execute(
                "SELECT count(*) FROM audit_events WHERE run_id = %s", (run_id,)
            ).fetchone()[0]
        stable = _poll_until_stable(
            _count, lambda: audit_lag(redis),
            timeout_s=poll_timeout_s, interval_s=poll_interval_s, sleep=time.sleep, now=time.monotonic,
        )
        if not stable:
            return {"ok": False, "incomplete": True,
                    "gaps": ["audit-consumer-incomplete: rows did not stabilize within poll window"]}
    rows = _load_rows(conn, run_id)
    gaps = _assert(rows, verdict_payload)
    return {"ok": not gaps, "incomplete": False, "gaps": gaps}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_panel_audit_poll.py tests/arb_memory/test_panel_audit.py -v`
Expected: PASS (3 + 7 = 10 passed; the redis=None path keeps Task 2 green)

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/panel_audit.py tests/arb_memory/test_panel_audit_poll.py
git commit -m "feat(arb): panel_audit bounded-poll drain (30s/250ms, incomplete->refuse, injectable clock)"
```

---

### Task 4: `arb-panel-vote` — reply text → stance → vote emit (fail-loud)

**Files:**
- Create: `scripts/arb-panel-vote`
- Test: `tests/arb_memory/test_arb_panel_vote.py`

**Interfaces:**
- Produces: CLI `arb-panel-vote --run-id R --actor seat:x [--timed-out]`. Reads reply text on **stdin**; parses the stance via `stance.parse_stance`; emits a `vote` event via `AuditRun`. `--timed-out` emits `stance:"timed-out"` without reading stdin. Exits **nonzero with no emit** on `StanceError` (fail-loud). Importable `main(argv, stdin, redis_factory) -> int` for tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_arb_panel_vote.py
import importlib.util, io, json, os
from pathlib import Path
import pytest

_spec = importlib.util.spec_from_file_location(
    "arb_panel_vote", Path(__file__).parents[2] / "scripts" / "arb-panel-vote")
apv = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(apv)

class FakeRedis:
    def __init__(self): self.adds = []
    def xadd(self, stream, fields, **kw): self.adds.append((stream, fields)); return b"1-0"
    def incr(self, k): return 1
    def expire(self, k, s): return True

VALID = 'review...\n```vote\n{"stance":"block","severity":"P0","refs":[],"note":"x"}\n```'

def test_valid_reply_emits_vote():
    r = FakeRedis()
    rc = apv.main(["--run-id","R","--actor","seat:codex"], stdin=io.StringIO(VALID),
                  redis_factory=lambda: r)
    assert rc == 0
    stream, fields = r.adds[0]
    assert fields["kind"] == "vote"
    p = json.loads(fields["payload"])
    assert p["actor"] == "seat:codex" and p["stance"] == "block"

def test_timed_out_emits_without_stdin():
    r = FakeRedis()
    rc = apv.main(["--run-id","R","--actor","seat:m3","--timed-out"], stdin=io.StringIO(""),
                  redis_factory=lambda: r)
    assert rc == 0
    assert json.loads(r.adds[0][1]["payload"])["stance"] == "timed-out"

def test_malformed_reply_fails_loud_no_emit():
    r = FakeRedis()
    rc = apv.main(["--run-id","R","--actor","seat:codex"], stdin=io.StringIO("no vote block"),
                  redis_factory=lambda: r)
    assert rc != 0
    assert r.adds == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_arb_panel_vote.py -v`
Expected: FAIL (file `scripts/arb-panel-vote` does not exist)

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
# scripts/arb-panel-vote
"""Emit one panel `vote` audit event, derived from a seat's raw reply text (fail-loud on no stance)."""
import argparse, json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from arb_memory.audit import AuditRun       # noqa: E402
from arb_memory.stance import parse_stance, StanceError  # noqa: E402


def main(argv=None, *, stdin=None, redis_factory=None):
    p = argparse.ArgumentParser(prog="arb-panel-vote")
    p.add_argument("--run-id", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--timed-out", action="store_true")
    a = p.parse_args(argv)
    stdin = stdin if stdin is not None else sys.stdin

    if a.timed_out:
        stance = {"stance": "timed-out", "severity": "none", "refs": [], "note": "no reply"}
    else:
        try:
            stance = parse_stance(stdin.read())
        except StanceError as exc:
            print(f"arb-panel-vote: no valid stance for {a.actor}: {exc}", file=sys.stderr)
            return 3

    payload = {"kind": "vote", "actor": a.actor, **stance}
    if redis_factory is not None:
        r = redis_factory()
    else:
        import redis as _redis
        url = os.environ.get("ARB_MEMORY_REDIS_URL")
        if not url:
            print("ARB_MEMORY_REDIS_URL required", file=sys.stderr); return 2
        r = _redis.from_url(url, decode_responses=True)
    AuditRun(r, a.run_id, prefix=os.environ.get("ARB_MEMORY_PREFIX", "")).emit("orchestrator", "vote", payload)
    print(f"voted {a.actor} stance={payload['stance']} run_id={a.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test + make executable**

Run: `chmod +x scripts/arb-panel-vote && .venv/bin/python -m pytest tests/arb_memory/test_arb_panel_vote.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/arb-panel-vote tests/arb_memory/test_arb_panel_vote.py
git commit -m "feat(arb): arb-panel-vote — reply-text -> stance -> vote emit (fail-loud, --timed-out)"
```

---

### Task 5: gate `arb-audit-emit --kind verdict` behind `reconcile`

**Files:**
- Modify: `scripts/arb-audit-emit`
- Test: `tests/arb_memory/test_arb_audit_emit_verdict_gate.py`

**Interfaces:**
- Consumes: `panel_audit.reconcile(conn, run_id, verdict_payload, redis=...)`.
- Produces: `arb-audit-emit --kind verdict` connects to `ARB_MEMORY_DSN`, runs `reconcile`; on `ok=False` (gap OR incomplete) prints gaps to stderr, returns nonzero, **no XADD**. `dispatch`/`vote` kinds unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_arb_audit_emit_verdict_gate.py
import importlib.util, io, json
from pathlib import Path
import pytest

_spec = importlib.util.spec_from_file_location(
    "arb_audit_emit", Path(__file__).parents[2] / "scripts" / "arb-audit-emit")
aae = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(aae)

class FakeRedis:
    def __init__(self): self.adds = []
    def xadd(self, stream, fields, **kw): self.adds.append((stream, fields)); return b"1-0"
    def incr(self, k): return 1
    def expire(self, k, s): return True

def test_verdict_refused_on_gap_no_xadd(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(aae, "reconcile", lambda *a, **k: {"ok": False, "incomplete": False, "gaps": ["seat X never voted"]})
    rc = aae.main(["--run-id","R","--kind","verdict","--payload",'{"kind":"verdict","roster":[],"stances":{}}'],
                  redis_factory=lambda: r, conn_factory=lambda: object())
    assert rc != 0 and r.adds == []

def test_verdict_emitted_when_reconcile_ok(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(aae, "reconcile", lambda *a, **k: {"ok": True, "incomplete": False, "gaps": []})
    rc = aae.main(["--run-id","R","--kind","verdict","--payload",'{"kind":"verdict","roster":[],"stances":{}}'],
                  redis_factory=lambda: r, conn_factory=lambda: object())
    assert rc == 0 and r.adds and r.adds[0][1]["kind"] == "verdict"

def test_dispatch_kind_still_emits_without_reconcile(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(aae, "reconcile", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reconcile dispatch")))
    rc = aae.main(["--run-id","R","--kind","dispatch","--payload",'{"roster":["seat:x"]}'],
                  redis_factory=lambda: r, conn_factory=lambda: object())
    assert rc == 0 and r.adds[0][1]["kind"] == "dispatch"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_arb_audit_emit_verdict_gate.py -v`
Expected: FAIL — `main()` has no `redis_factory`/`conn_factory` params and does not import `reconcile`.

- [ ] **Step 3: Write minimal implementation**

Replace `scripts/arb-audit-emit` with (keeps the existing emit, adds the verdict gate + injectable factories for tests):

```python
#!/usr/bin/env python3
"""Emit one ARB audit event (dispatch/vote/verdict). verdict is gated by panel_audit.reconcile."""
import argparse, json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from arb_memory.audit import AuditRun           # noqa: E402
from arb_memory.panel_audit import reconcile    # noqa: E402


def main(argv=None, *, redis_factory=None, conn_factory=None):
    p = argparse.ArgumentParser(prog="arb-audit-emit")
    p.add_argument("--run-id", required=True)
    p.add_argument("--kind", required=True, choices=("dispatch", "vote", "verdict"))
    p.add_argument("--source", default="orchestrator")
    p.add_argument("--actor")
    p.add_argument("--payload", default="{}")
    a = p.parse_args(argv)
    payload = json.loads(a.payload)
    payload.setdefault("kind", a.kind)
    if a.actor:
        payload["actor"] = a.actor

    if redis_factory is not None:
        r = redis_factory()
    else:
        import redis as _redis
        url = os.environ.get("ARB_MEMORY_REDIS_URL")
        if not url:
            print("ARB_MEMORY_REDIS_URL required", file=sys.stderr); return 2
        r = _redis.from_url(url, decode_responses=True)

    if a.kind == "verdict":
        if conn_factory is not None:
            conn = conn_factory()
        else:
            import psycopg
            dsn = os.environ.get("ARB_MEMORY_DSN")
            if not dsn:
                print("ARB_MEMORY_DSN required for verdict reconcile", file=sys.stderr); return 2
            conn = psycopg.connect(dsn, autocommit=True)
        result = reconcile(conn, a.run_id, payload, redis=r)
        if not result["ok"]:
            print("arb-audit-emit: verdict REFUSED — roster did not reconcile:", file=sys.stderr)
            for gap in result["gaps"]:
                print(f"  - {gap}", file=sys.stderr)
            return 4

    AuditRun(r, a.run_id, prefix=os.environ.get("ARB_MEMORY_PREFIX", "")).emit(a.source, a.kind, payload)
    print(f"emitted {a.kind} run_id={a.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test + the existing CLI test**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_arb_audit_emit_verdict_gate.py tests/arb_memory/test_arb_audit_emit_cli.py -v`
Expected: PASS (3 new + existing CLI test stays green)

- [ ] **Step 5: Commit**

```bash
git add scripts/arb-audit-emit tests/arb_memory/test_arb_audit_emit_verdict_gate.py
git commit -m "feat(arb): arb-audit-emit verdict gated by reconcile (refuse + no XADD on gap/incomplete)"
```

---

### Task 6: `agent-dispatch --audit-panel` — gated, fail-soft vote emit

**Files:**
- Modify: `scripts/agent-dispatch`
- Test: `tests/test_agent_dispatch_audit_panel.py`

**Interfaces:**
- Consumes: `scripts/arb-panel-vote`.
- Produces: new flag `--audit-panel`. When set with `--run-id`: after a matching reply, pipe `payload.result` into `arb-panel-vote --run-id $RUN_ID --actor seat:$TO` (wrapped so a failure warns but never breaks the dispatch); on timeout (exit 124) call `arb-panel-vote --timed-out`. When `ARB_MEMORY_REDIS_URL` unset → loud warning. Without `--audit-panel` the LPUSH envelope is byte-identical to today.

- [ ] **Step 1: Write the failing test** (envelope byte-identical when flag off; flag parsed when on)

```python
# tests/test_agent_dispatch_audit_panel.py
import subprocess, json, os
from pathlib import Path

DISPATCH = str(Path(__file__).parents[1] / "scripts" / "agent-dispatch")
ENV = {**os.environ, "BRANCH": "dev", "FROM_AGENT_ID": "claude-bridge-dev",
       "AGENT_REDIS_HOST": "127.0.0.1", "AGENT_REDIS_PORT": "6399"}

def _envelope(extra_args):
    out = subprocess.run([DISPATCH, *extra_args, "--dry-run-envelope", "hello"],
                         capture_output=True, text=True, env=ENV)
    return out.stdout.strip()

def test_audit_panel_flag_does_not_change_envelope():
    base = json.loads(_envelope(["--run-id", "panel-x"]))
    withp = json.loads(_envelope(["--run-id", "panel-x", "--audit-panel"]))
    assert base == withp                      # the flag must NOT alter the request envelope

def test_audit_panel_flag_is_accepted():
    out = subprocess.run([DISPATCH, "--audit-panel", "--run-id", "p", "--dry-run-envelope", "hi"],
                         capture_output=True, text=True, env=ENV)
    assert out.returncode == 0 and "unknown" not in out.stderr.lower()

def test_audit_panel_requires_nothing_when_off():
    out = subprocess.run([DISPATCH, "--dry-run-envelope", "hi"], capture_output=True, text=True, env=ENV)
    assert out.returncode == 0 and '"run_id"' not in out.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_agent_dispatch_audit_panel.py -v`
Expected: FAIL — `--audit-panel` is an unknown flag (`agent-dispatch` `break`s on it, treating it as the task).

- [ ] **Step 3: Write minimal implementation**

In `scripts/agent-dispatch`, add the flag variable near the other defaults (after `RUN_ID=`):

```bash
AUDIT_PANEL=
```

Add a case in the arg-parse `while` loop (next to `--run-id`):

```bash
    --audit-panel)
      AUDIT_PANEL=1
      shift
      ;;
```

Add the flag to the `usage()` string. Then, in the reply loop where a matching reply is found (the block `if [ "$kind" = "reply" ] && [ "$in_reply_to" = "$ID" ]; then ... jq '.payload' <<<"$raw"`), insert the gated vote emit BEFORE the `exit`:

```bash
  if [ "$kind" = "reply" ] && [ "$in_reply_to" = "$ID" ]; then
    jq '.payload' <<<"$raw"
    if [ -n "$AUDIT_PANEL" ] && [ -n "$RUN_ID" ]; then
      if [ -z "${ARB_MEMORY_REDIS_URL:-}" ]; then
        echo "warn: --audit-panel set but ARB_MEMORY_REDIS_URL unset; vote for seat:$TO NOT recorded (run un-auditable)" >&2
      else
        # fail-soft-but-loud: a down audit bus must never break the dispatch.
        set +e
        jq -r '.payload.result // ""' <<<"$raw" \
          | "$(dirname "$0")/arb-panel-vote" --run-id "$RUN_ID" --actor "seat:$TO"
        rc=$?
        set -e
        [ "$rc" -ne 0 ] && echo "warn: panel vote emit for seat:$TO failed (rc=$rc); missing row will block the verdict verifier" >&2
      fi
    fi
    ok=$(jq -r '.payload.ok // false' <<<"$raw")
    [ "$ok" = "true" ] && exit 0 || exit 1
  fi
```

And at the timeout path (replace the final `echo "timed out..."; exit 124` block):

```bash
if [ -n "$AUDIT_PANEL" ] && [ -n "$RUN_ID" ] && [ -n "${ARB_MEMORY_REDIS_URL:-}" ]; then
  set +e
  "$(dirname "$0")/arb-panel-vote" --run-id "$RUN_ID" --actor "seat:$TO" --timed-out
  set -e
fi
echo "timed out waiting for reply to $ID" >&2
exit 124
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_agent_dispatch_audit_panel.py -v`
Expected: PASS (3 passed). Also run the existing `tests/test_agent_dispatch_run_id.py` to confirm no regression.

- [ ] **Step 5: Commit**

```bash
git add scripts/agent-dispatch tests/test_agent_dispatch_audit_panel.py
git commit -m "feat(dispatch): --audit-panel — fail-soft vote emit from reply text + timeout-vote; envelope byte-identical when off"
```

---

### Task 7: reconcile `roles/reviewer.md` vocabulary + require the stance fence

**Files:**
- Modify: `roles/reviewer.md`
- Test: `tests/test_reviewer_role_stance_contract.py`

**Interfaces:** none (role-profile text). Test asserts the file documents the stance enum + a trailing fence and no longer mandates a conflicting verdict vocabulary.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reviewer_role_stance_contract.py
from pathlib import Path
TXT = (Path(__file__).parents[1] / "roles" / "reviewer.md").read_text()

def test_documents_stance_enum():
    for s in ("approve", "needs-changes", "block", "abstain"):
        assert s in TXT

def test_requires_trailing_vote_fence():
    assert "```vote" in TXT

def test_reconciles_legacy_vocabulary():
    # the old labels must be mapped/superseded, not left as a second contradicting contract
    if "FIX_BEFORE_MERGE" in TXT or "SHIP_WITH_NITS" in TXT:
        assert "approve" in TXT and "```vote" in TXT  # mapping must be present alongside
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_reviewer_role_stance_contract.py -v`
Expected: FAIL (`test_requires_trailing_vote_fence`: no ```` ```vote ```` block in the role file yet).

- [ ] **Step 3: Append the stance contract to `roles/reviewer.md`**

Add this section at the end of `roles/reviewer.md`:

````markdown
## Panel stance block (REQUIRED when dispatched for an audited panel)

End every review with a single machine-readable stance block. This SUPERSEDES the prose verdict
labels above for audited panels — emit exactly one stance; do not also emit a contradicting prose
label. Map your judgment as: SHIP→`approve`, SHIP_WITH_NITS→`approve` (+severity `P2`),
FIX_BEFORE_MERGE→`needs-changes`, BLOCK_MERGE→`block`. Use `abstain` if you cannot judge.

```vote
{"stance":"approve|needs-changes|block|abstain","severity":"none|P2|P1|P0","refs":["file:line",...],"note":"<=200 chars"}
```
````

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_reviewer_role_stance_contract.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add roles/reviewer.md tests/test_reviewer_role_stance_contract.py
git commit -m "feat(arb): reviewer role emits the panel stance block (supersedes legacy verdict labels)"
```

---

### Task 8: document the panel-emit discipline in the bridge skill

**Files:**
- Modify: `skills/using-agent-bridge/SKILL.md`

**Interfaces:** none (operator documentation). No test (doc-only); verified by the e2e in Task 9 exercising the documented commands.

- [ ] **Step 1: Add the discipline section**

Append a section to `skills/using-agent-bridge/SKILL.md` titled "Auditing a review/design panel" containing the exact sequence:

````markdown
## Auditing a review/design panel (arb-audit-emit wiring)

When running a panel you want on the audit trail (`ARB_MEMORY_REDIS_URL` = audit bus db3 must be set):

1. **Mint one `run_id` per panel, before any dispatch:** `RID=panel-<slug>-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 3)`. Reuse it verbatim everywhere; a re-panel mints a NEW id (reference the prior via `supersedes:` in the verdict payload).
2. **Emit the roster manifest FIRST (it must be seq 1):**
   `arb-audit-emit --run-id "$RID" --kind dispatch --payload "{\"kind\":\"dispatch\",\"roster\":[\"seat:codex\",\"seat:cold-opus-a\",\"seat:cold-opus-b\"],\"task\":\"...\",\"branch\":\"dev\"}"`
3. **Dispatch bridge seats with `--audit-panel --run-id "$RID"`** — the vote is auto-emitted from the seat's stance fence (fail-soft). Instruct every seat (bridge and in-session) to END its reply with the ```` ```vote ```` block.
4. **For in-session (cold-Opus) seats, emit each vote yourself** from the subagent's final message:
   `printf '%s' "<reply text>" | scripts/arb-panel-vote --run-id "$RID" --actor seat:cold-opus-a`
5. **Close with the verdict — it self-verifies (`ARB_MEMORY_DSN` must be set):**
   `arb-audit-emit --run-id "$RID" --kind verdict --payload "{\"kind\":\"verdict\",\"roster\":[...],\"stances\":{\"seat:codex\":\"approve\",...},\"rationale\":\"...\"}"`
   If it exits nonzero it has REFUSED — a seat is missing/unrostered or a stance is laundered. Fix the gap; do NOT announce the verdict.
6. **Residual (named):** the verifier only guards verdicts that go through the CLI. Before announcing any verdict in prose, run the done-criterion query and cite the `run_id` + row counts — a verdict announced without a passing audit-close is un-audited.
````

- [ ] **Step 2: Commit**

```bash
git add skills/using-agent-bridge/SKILL.md
git commit -m "docs(bridge): panel-emit discipline (mint run_id, manifest-first, --audit-panel, self-verifying verdict)"
```

---

### Task 9: end-to-end — `arb-memory-panel-audit-e2e` (the done-criterion + deny-proofs)

**Files:**
- Create: `scripts/arb-memory-panel-audit-e2e`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `AuditRun`, `AuditConsumer` (`audit.py`), `reconcile` (`panel_audit.py`), real `ARB_MEMORY_DSN` + `ARB_MEMORY_REDIS_URL`. Mirrors `scripts/arb-memory-eval-e2e` (run-tag isolation, drain barrier, run-scoped cleanup).

- [ ] **Step 1: Write the e2e script**

```python
#!/usr/bin/env python3
# scripts/arb-memory-panel-audit-e2e
"""Live close-condition for the panel-audit wiring. Drives one synthetic panel on a run-isolated prefix:
emit manifest(seq1)+votes+verdict, run the AuditConsumer, then assert reconcile() PASSES — and prove the
guard bites by deleting/mutating rows and asserting it REFUSES (deny-proofs, both directions)."""
import os, sys, time, uuid
import psycopg
import redis as _redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from arb_memory.audit import AuditConsumer, AuditRun   # noqa: E402
from arb_memory.panel_audit import reconcile           # noqa: E402


def _connect(dsn): return psycopg.connect(dsn, autocommit=True)

def wait_until(fn, timeout_s, what):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if fn(): return
        time.sleep(0.1)
    raise RuntimeError(f"timeout waiting for {what}")


def main():
    dsn = os.environ["ARB_MEMORY_DSN"]
    audit_url = os.environ["ARB_MEMORY_REDIS_URL"]
    run_tag = f"arb-panel-e2e-{uuid.uuid4().hex}"
    prefix = f"{run_tag}:"
    roster = [f"seat:{run_tag}-codex", f"seat:{run_tag}-coldopus"]
    verdict = {"kind": "verdict", "roster": roster,
               "stances": {roster[0]: "approve", roster[1]: "needs-changes"}, "rationale": "ok"}

    ar = _redis.from_url(audit_url, decode_responses=True)
    conn = _connect(dsn)
    audit = AuditConsumer(ar, lambda: _connect(dsn), prefix=prefix)

    def n_rows():
        return conn.execute("SELECT count(*) FROM audit_events WHERE run_id=%s", (run_tag,)).fetchone()[0]
    def cleanup():
        conn.execute("DELETE FROM audit_events WHERE run_id=%s", (run_tag,))

    try:
        cleanup()
        audit.start()
        run = AuditRun(ar, run_tag, prefix=prefix)
        run.emit("orchestrator", "dispatch", {"kind": "dispatch", "roster": roster, "task": "t", "branch": "dev"})
        run.emit("orchestrator", "vote", {"kind": "vote", "actor": roster[0], "stance": "approve", "severity": "none"})
        run.emit("orchestrator", "vote", {"kind": "vote", "actor": roster[1], "stance": "needs-changes", "severity": "P2"})
        wait_until(lambda: n_rows() == 3, 30, "3 audit rows drained to PG")

        ok = reconcile(conn, run_tag, verdict, redis=ar)
        assert ok["ok"] is True, f"clean panel should reconcile: {ok}"

        # deny-proof A: delete a vote -> refuse
        conn.execute("DELETE FROM audit_events WHERE run_id=%s AND kind='vote' AND payload->>'actor'=%s",
                     (run_tag, roster[1]))
        bad = reconcile(conn, run_tag, verdict, redis=ar)
        assert bad["ok"] is False and any("never voted" in g for g in bad["gaps"]), bad

        # deny-proof B: re-add but with the WRONG seq so the manifest no longer precedes -> refuse
        # (re-insert vote at seq 0 < manifest seq 1)
        conn.execute("INSERT INTO audit_events (run_id, seq, source, kind, payload) "
                     "VALUES (%s, 0, 'orchestrator', 'vote', %s)",
                     (run_tag, psycopg.types.json.Jsonb({"kind":"vote","actor":roster[1],"stance":"needs-changes"})))
        bad2 = reconcile(conn, run_tag, verdict, redis=ar)
        assert bad2["ok"] is False and any("precede" in g for g in bad2["gaps"]), bad2

        print(f"PANEL-AUDIT E2E GREEN run_tag={run_tag}: clean reconcile + 2 deny-proofs bit")
    finally:
        audit.stop() if hasattr(audit, "stop") else None
        cleanup()
        conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make executable and run against DO dev**

```bash
chmod +x scripts/arb-memory-panel-audit-e2e
set -a; . envs/arb-memory-do-dev.env; set +a
.venv/bin/python scripts/arb-memory-panel-audit-e2e
```
Expected: `PANEL-AUDIT E2E GREEN run_tag=... : clean reconcile + 2 deny-proofs bit`

- [ ] **Step 3: Add the CHANGELOG entry**

Add to `CHANGELOG.md` (top of the unreleased section):

```markdown
- **arb-audit-emit panel wiring** — real review/design panels now record an audit trail (roster
  manifest + per-seat votes + verdict) keyed by one `run_id`. Votes are parsed from a uniform stance
  block by `stance.py`; the verdict emit is gated by `panel_audit.reconcile` (seq-1 manifest +
  precedence + roster + stance-match, `incomplete→refuse`) so doneness-laundering fails loud.
  `agent-dispatch --audit-panel` auto-emits bridge votes fail-soft. **Why:** the audit path was built
  but dormant (zero real emitters); this makes the audit log record live panel decisions.
```

- [ ] **Step 4: Commit**

```bash
git add scripts/arb-memory-panel-audit-e2e CHANGELOG.md
git commit -m "feat(arb): panel-audit e2e (clean reconcile + deny-proofs) + CHANGELOG; closes audit-emit wiring"
```

---

## Self-review (completed by plan author)

- **Spec coverage:** stance contract → T1; reconcile 5 assertions incl. seq-1 + precedence → T2; bounded-poll `incomplete→refuse` → T3; vote-from-reply-text fail-loud → T4; verdict gate (refuse + no XADD) → T5; `--audit-panel` gated fail-soft + timeout-vote + byte-identical-off → T6; reviewer.md vocabulary reconciliation → T7; skill discipline + named prose-bypass residual → T8; done-criterion + deny-proofs both directions + CHANGELOG → T9. All spec sections mapped.
- **Placeholders:** none — every code/test step carries complete content.
- **Type consistency:** `parse_stance` return shape (T1) consumed verbatim by T4 (`{**stance}`) and asserted in T2/T9 payloads; `reconcile(...)->{"ok","incomplete","gaps"}` (T2/T3) consumed identically in T5/T9; payload shapes match the Global Constraints block throughout.

## Notes for the executor
- The bridge tee / eval half is OUT of scope (separate slice). This plan touches only the audit path.
- `ARB_MEMORY_DSN` becomes a new runtime dep of `arb-audit-emit` **for the verdict kind only**; dispatch/vote keep needing just `ARB_MEMORY_REDIS_URL`.
- Prod `arbmemory` audit schema must exist before the verdict reconcile runs there; DO-dev is ready.

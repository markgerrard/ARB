# ARB Observability — Slice 3 Implementation Plan (manifest + votes + reconcile-gated verdict)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `audit_events` + `reconcile()` LIVE — wire the bridge to transcribe each panel seat's explicit stance into a `vote` audit row, so a verdict reconciles against the orchestrator's declared roster.

**Architecture:** The audit spine (AuditRun/next_seq/reconcile/AuditConsumer/parse_stance/arb-audit-emit) already exists with zero emitters. This slice adds **bridge-daemon vote extraction (option A)**: on task-finish, if the request carries `payload.audit_vote_expected` + `run_id`, the bridge parses the seat's reply with a STRICT fenced-only stance parser and emits `AuditRun.emit("seat:<agent_id>", "vote", {...})` to the audit bus — fail-soft. Turn-timeout emits a synthesized `timed-out` vote; never-popped leaves a fail-loud reconcile gap. The old `agent-dispatch --audit-panel → arb-panel-vote` wrapper path is removed (single emitter, no double-votes). The manifest is an orchestrator preflight (`arb-audit-emit --kind dispatch`, seq=1); the verdict is the existing reconcile-gated `arb-audit-emit --kind verdict`.

**Tech Stack:** Python 3.12, redis-py (streams/INCR), psycopg3, pytest, bash (`agent-dispatch`), Postgres.

## Global Constraints

- **Vote emission is fail-soft** (try/except + short socket timeout): a down audit bus must NEVER crash the worker turn. A dropped vote becomes a fail-loud `reconcile` gap at verdict time — never a silent pass.
- **The bridge is the SINGLE vote emitter.** `agent-dispatch` emits NO votes after this slice. (Two emitters → `duplicate vote` reconcile failure.)
- **Guard (a) declared-panel only:** emit a vote ONLY when `request.payload.get("audit_vote_expected")` is truthy AND `request.run_id` is non-empty. Non-panel tasks never produce votes.
- **Guard (b) explicit fenced block only:** the bridge MUST parse with `require_fence=True`. A bare trailing `{...}` must NOT become a vote.
- **Guard (c) fail-loud-no-fabricate:** unparseable/missing stance ⇒ NO vote row (log it); never invent a stance.
- **Actor name = `"seat:" + self.agent_id`** (frozen contract). Roster entries (orchestrator preflight) must be the bridges' registered agent-ids.
- **`timed-out`** is the synthesized stance on engine turn-timeout (`_is_engine_timeout`, bridge.py:1086). It is a valid `STANCE` (stance.py:9).
- **Audit bus** = `arbmem:audit` via `ARB_MEMORY_REDIS_URL` (prod db-5 / dev db-3) + `ARB_MEMORY_PREFIX`. Resolve from `.env` (exported wins, `.env` fallback) — the Slice-1 lesson.

### Test harness (every task's pytest steps assume this)
```bash
cd /Users/<user>/<workspace>
export PYTHONPATH="$(pwd):$(pwd)/src"
set -a; . envs/arb-memory-dev.env; set +a          # ARB_MEMORY_DSN -> local pgvector
export ARB_MEMORY_REDIS_URL=redis://127.0.0.1:6379/15
PYTEST=/Users/<user>/<workspace>/.venv/bin/pytest
```

---

### Task 1: Strict fenced-only stance parse (`require_fence`)

**Files:**
- Modify: `src/arb_memory/stance.py` (`_candidates`, `parse_stance`)
- Test: `tests/arb_memory/test_stance.py`

**Interfaces:**
- Produces: `parse_stance(text, *, require_fence=False) -> dict`. With `require_fence=True`, only fenced ```vote/```json blocks are considered; the bare-trailing-`{` fallback is skipped. Default `False` = existing behavior.

- [ ] **Step 1: Write the failing test**
```python
import pytest
from arb_memory.stance import parse_stance, StanceError

_BARE = 'blah blah\n{"stance":"approve","severity":"none"}'

def test_require_fence_rejects_bare_trailing_json():
    # default (loose) still parses the bare object — unchanged behavior
    assert parse_stance(_BARE)["stance"] == "approve"
    # strict bridge path rejects it (guard b)
    with pytest.raises(StanceError):
        parse_stance(_BARE, require_fence=True)

def test_require_fence_accepts_fenced():
    fenced = 'text\n```vote\n{"stance":"block","severity":"P0"}\n```'
    assert parse_stance(fenced, require_fence=True)["stance"] == "block"
```

- [ ] **Step 2: Run it — expect FAIL** (`parse_stance() got an unexpected keyword argument 'require_fence'`)
Run: `$PYTEST tests/arb_memory/test_stance.py::test_require_fence_rejects_bare_trailing_json -v`

- [ ] **Step 3: Implement** — thread `require_fence` through `_candidates`:
```python
def _candidates(text, *, require_fence=False):
    # Fenced blocks first (last wins), then (unless require_fence) a bare trailing {...} object.
    for m in reversed(list(_FENCE.finditer(text))):
        yield m.group(1)
    if require_fence:
        return
    start = text.rfind("{")
    if start != -1:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
            yield json.dumps(value)
        except json.JSONDecodeError:
            return
```
And in `parse_stance`:
```python
def parse_stance(text: str, *, require_fence: bool = False) -> dict:
    last_error = None
    found = False
    for raw in _candidates(text, require_fence=require_fence):
        found = True
        try:
            value = json.loads(raw)
            return _validate(value)
        except (json.JSONDecodeError, StanceError) as exc:
            last_error = exc
    if not found:
        raise StanceError("no stance block found in reply")
    if isinstance(last_error, json.JSONDecodeError):
        raise StanceError(f"stance block is not valid JSON: {last_error}") from last_error
    raise last_error
```

- [ ] **Step 4: Run tests — expect PASS** (incl. the existing `test_stance.py` cases)
Run: `$PYTEST tests/arb_memory/test_stance.py -v`

- [ ] **Step 5: Commit**
```bash
git add src/arb_memory/stance.py tests/arb_memory/test_stance.py
git commit -m "feat(stance): add require_fence=True strict fenced-only parse for the bridge vote path"
```

---

### Task 2: Bridge audit-Redis config (`resolve_audit_redis` + `self.audit_redis`)

**Files:**
- Modify: `src/agent_redis_bridge/bridge.py` (add `resolve_audit_redis`; init `self.audit_redis` + `self._audit_prefix` in `__init__`, after the eval block ~line 233)
- Test: `tests/test_resolve_audit_redis.py` (create)

**Interfaces:**
- Produces: `agent_redis_bridge.bridge.resolve_audit_redis(env) -> tuple[str|None, str]` returning `(url, prefix)`. `url` falsy when unset. `self.audit_redis` = a redis client (or `None`); `self._audit_prefix` = str.

- [ ] **Step 1: Write the failing test**
```python
import importlib
bridge = importlib.import_module("agent_redis_bridge.bridge")

def test_audit_env_file_arms_when_process_env_absent(monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_MEMORY_PREFIX", raising=False)
    url, prefix = bridge.resolve_audit_redis({"ARB_MEMORY_REDIS_URL": "redis://prod:6379/5", "ARB_MEMORY_PREFIX": "p:"})
    assert url == "redis://prod:6379/5" and prefix == "p:"

def test_audit_process_env_wins(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://exported:6379/5")
    url, prefix = bridge.resolve_audit_redis({"ARB_MEMORY_REDIS_URL": "redis://file:6379/3"})
    assert url == "redis://exported:6379/5"

def test_audit_unset_is_none(monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_MEMORY_PREFIX", raising=False)
    url, prefix = bridge.resolve_audit_redis({})
    assert not url and prefix == ""
```

- [ ] **Step 2: Run — expect FAIL** (`module ... has no attribute 'resolve_audit_redis'`)
Run: `$PYTEST tests/test_resolve_audit_redis.py -v`

- [ ] **Step 3: Implement** — add next to `resolve_eval_redis` (~bridge.py:123):
```python
def resolve_audit_redis(env):
    """Resolve audit-bus Redis config: exported process env wins, parsed .env file is fallback.

    The audit bus URL (ARB_MEMORY_REDIS_URL) carries its db (prod /5, dev /3). A URL present only in the
    bridge's .env must still arm vote emission (Slice-1 mistake-prevention lesson)."""
    url = os.environ.get("ARB_MEMORY_REDIS_URL") or env.get("ARB_MEMORY_REDIS_URL")
    prefix = os.environ.get("ARB_MEMORY_PREFIX") or env.get("ARB_MEMORY_PREFIX") or ""
    return url, prefix
```
And in `__init__`, after the eval-redis block (~line 233), add:
```python
        audit_url, audit_prefix = resolve_audit_redis(env)
        if audit_url:
            import redis as _redis
            self.audit_redis = _redis.from_url(
                audit_url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5,
            )
            self._audit_prefix = audit_prefix
        else:
            self.audit_redis = None
            self._audit_prefix = ""
```

- [ ] **Step 4: Run — expect PASS**
Run: `$PYTEST tests/test_resolve_audit_redis.py -v`

- [ ] **Step 5: Commit**
```bash
git add src/agent_redis_bridge/bridge.py tests/test_resolve_audit_redis.py
git commit -m "feat(audit): bridge resolves audit-bus Redis from .env (mirrors resolve_eval_redis)"
```

---

### Task 3: Bridge vote emission (`_emit_vote`) + wire into `process_request`

**Files:**
- Modify: `src/agent_redis_bridge/bridge.py` (add `_emit_vote(self, envelope, result)`; call it after `send_reply` in `process_request` ~line 830)
- Test: `tests/test_bridge_emit_vote.py` (create)

**Interfaces:**
- Consumes: `self.audit_redis`, `self._audit_prefix`, `self.agent_id` (Task 2); `parse_stance(require_fence=True)` (Task 1); `Bridge._is_engine_timeout(result)` (bridge.py:1086); `arb_memory.audit.AuditRun` (lazy import).
- Produces: `Bridge._emit_vote(envelope, result) -> None` — emits one `vote` audit row when the run is a declared panel; guards (a)-(d). Actor = `"seat:" + self.agent_id`.

- [ ] **Step 1: Write the failing tests** (drive `_emit_vote` directly with a recording audit redis)
```python
import json
from types import SimpleNamespace
from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.redis_io import RedisConfig

class RecordingRedis:
    def __init__(self): self.xadds = []; self.kv = {}
    def incr(self, k): self.kv[k] = self.kv.get(k, 0) + 1; return self.kv[k]
    def expire(self, k, s): pass
    def xadd(self, key, fields, **kw): self.xadds.append((key, fields)); return "1-0"

def _bridge(audit_redis):
    b = Bridge.__new__(Bridge)
    b.agent_id = "codex-bridge-dev"
    b.audit_redis = audit_redis
    b._audit_prefix = ""
    return b

def _req(payload, run_id="run-1"):
    return Envelope(id="t1", sender="claude", branch="b", recipient="codex-bridge-dev",
                    kind="request", sent_at="x", payload=payload, run_id=run_id)

def _ok(text): return SimpleNamespace(ok=True, result=text, error=None)

FENCED = 'reviewed\n```vote\n{"stance":"approve","severity":"none"}\n```'

def test_emits_vote_for_declared_panel():
    r = RecordingRedis()
    _bridge(r)._emit_vote(_req({"audit_vote_expected": True}), _ok(FENCED))
    assert len(r.xadds) == 1
    fields = r.xadds[0][1]
    assert fields["kind"] == "vote"
    assert json.loads(fields["payload"])["actor"] == "seat:codex-bridge-dev"
    assert json.loads(fields["payload"])["stance"] == "approve"

def test_no_vote_without_marker():
    r = RecordingRedis()
    _bridge(r)._emit_vote(_req({}), _ok(FENCED))
    assert r.xadds == []

def test_no_vote_without_run_id():
    r = RecordingRedis()
    _bridge(r)._emit_vote(_req({"audit_vote_expected": True}, run_id=None), _ok(FENCED))
    assert r.xadds == []

def test_bare_json_is_not_a_vote_guard_b():
    r = RecordingRedis()
    _bridge(r)._emit_vote(_req({"audit_vote_expected": True}), _ok('x\n{"stance":"approve","severity":"none"}'))
    assert r.xadds == []   # require_fence=True rejects bare JSON -> StanceError -> no emit (guard c)

def test_timeout_emits_synthesized_timed_out():
    r = RecordingRedis()
    res = SimpleNamespace(ok=False, result="", error="turn timed out after 60s")
    _bridge(r)._emit_vote(_req({"audit_vote_expected": True}), res)
    assert json.loads(r.xadds[0][1]["payload"])["stance"] == "timed-out"

def test_down_bus_does_not_raise_guard_d():
    class Boom:
        def incr(self, k): raise TimeoutError("audit bus wedged")
    _bridge(Boom())._emit_vote(_req({"audit_vote_expected": True}), _ok(FENCED))  # must not raise

def test_no_audit_redis_is_noop():
    b = _bridge(None)
    b._emit_vote(_req({"audit_vote_expected": True}), _ok(FENCED))  # must not raise
```

- [ ] **Step 2: Run — expect FAIL** (`Bridge has no attribute '_emit_vote'`)
Run: `$PYTEST tests/test_bridge_emit_vote.py -v`

- [ ] **Step 3: Implement `_emit_vote`** (add as a method on `Bridge`, near `push_task_event`):
```python
    def _emit_vote(self, envelope, result) -> None:
        """Transcribe a panel seat's terminal stance into a `vote` audit row (option A). Fail-soft.
        Guard (a) declared-panel only; (b) strict fenced parse; (c) no fabrication; (d) never crash."""
        if self.audit_redis is None:
            return
        if not getattr(envelope, "run_id", None):
            return
        payload = getattr(envelope, "payload", None) or {}
        if not payload.get("audit_vote_expected"):
            return
        try:
            if self._is_engine_timeout(result):
                stance = {"stance": "timed-out", "severity": "none", "refs": [], "note": "engine turn timeout"}
            else:
                from arb_memory.stance import parse_stance, StanceError
                try:
                    stance = parse_stance(result.result or "", require_fence=True)
                except StanceError as exc:
                    logger.warning("panel vote: no valid stance for %s run %s: %s",
                                   self.agent_id, envelope.run_id, exc)
                    return  # guard (c): fail-loud-no-fabricate (missing row -> reconcile gap)
            actor = "seat:" + self.agent_id
            from arb_memory.audit import AuditRun
            AuditRun(self.audit_redis, envelope.run_id, prefix=self._audit_prefix).emit(
                "seat:" + self.agent_id, "vote", {"actor": actor, **stance})
        except Exception:  # guard (d): a down/failed audit bus must never crash the worker turn
            logger.exception("panel vote emit failed for %s run %s", self.agent_id,
                             getattr(envelope, "run_id", "?"))
```
Wire it in `process_request` **immediately BEFORE** `self.send_reply(...)` (~bridge.py:830). ORDERING IS
LOAD-BEARING (plan-panel P1): `send_reply` LPUSHes the reply (bridge.py:1510); `agent-dispatch` exits the
moment it sees the reply and the orchestrator can then emit the verdict. If the vote XADD happens *after*
the reply is visible, `reconcile` can race a not-yet-written vote and fail with a spurious "seat never
voted" gap. Emitting before `send_reply` guarantees the vote is on the audit stream before the reply is
visible. `_emit_vote` is fail-soft + 0.5s socket timeout, so a down bus cannot block the reply beyond that.
```python
            self._emit_vote(envelope, result)  # BEFORE send_reply (see ordering note above)
            self.send_reply(envelope, result, structured)
```

- [ ] **Step 3b: Add a wiring + ordering test** (proves `process_request` actually calls `_emit_vote`, and
  before `send_reply` — guards against a future edit silently dropping/reordering the call). In
  `tests/test_bridge_emit_vote.py`, patch the two methods to record call order and drive `process_request`
  with the heavy collaborators stubbed:
```python
from unittest import mock

def test_process_request_emits_vote_before_reply(monkeypatch):
    calls = []
    b = Bridge.__new__(Bridge)
    # stub every side-effect process_request calls between turn-end and return, recording order for the two we care about
    monkeypatch.setattr(Bridge, "_emit_vote", lambda self, e, r: calls.append("vote"))
    monkeypatch.setattr(Bridge, "send_reply", lambda self, e, r, s: calls.append("reply"))
    # ... set the minimum attrs + stub run_turn/update_task_status/push_task_event/write_task_result/
    #     send_milestone/parse_structured_for_request/record_turn_seconds so process_request reaches the
    #     reply path with a TurnResult(ok=True, result=FENCED). If wiring the full process_request proves
    #     disproportionate, report DONE_WITH_CONCERNS — the before-send_reply placement is also covered by
    #     code review + the Task-5 live E2E.
    # assert the vote is emitted, and strictly before the reply:
    assert calls == ["vote", "reply"]
```

- [ ] **Step 4: Run — expect PASS** (all 7)
Run: `$PYTEST tests/test_bridge_emit_vote.py -v`

- [ ] **Step 5: Commit**
```bash
git add src/agent_redis_bridge/bridge.py tests/test_bridge_emit_vote.py
git commit -m "feat(audit): bridge-daemon vote extraction (option A) with guards a-d + actor contract"
```

---

### Task 4: Remove wrapper vote emission from `agent-dispatch` + update stale tests

**Files:**
- Modify: `scripts/agent-dispatch` (remove the reply-vote block ~378-389 and the timeout-vote block ~410-418; KEEP the `--audit-panel` flag, the `run_id` + `audit_vote_expected` stamping, and the fail-loud `--audit-panel requires --run-id` gate)
- Modify: `tests/test_agent_dispatch_audit_panel.py` (remove/replace any assertion that the wrapper emits a vote; keep marker + gate tests)

**Interfaces:**
- Produces: `agent-dispatch --audit-panel` stamps `payload.audit_vote_expected` + `run_id` and emits NO vote (the bridge does). `arb-panel-vote` remains a standalone manual CLI (unchanged).

- [ ] **Step 1: Identify the wrapper-vote assertions**
Run: `grep -nE "arb-panel-vote|panel.vote|will record vote|--timed-out" scripts/agent-dispatch tests/test_agent_dispatch_audit_panel.py`
Expected: the reply-vote call (~382), the timeout-vote call (~414), the "panel: will record vote" notice (~357). Confirm no test asserts a vote row is produced by the wrapper (the design panel confirmed none do; verify).

- [ ] **Step 2: Remove the two wrapper-vote blocks** in `scripts/agent-dispatch`:
  - Delete the reply branch's `--audit-panel` vote emission (the block that pipes `.payload.result` into `arb-panel-vote --run-id "$RUN_ID" --actor "seat:$TO"`, ~lines 375-389), leaving the plain `jq '.payload' <<<"$raw"` reply output + `exit 0`.
  - Delete the timeout block's `arb-panel-vote ... --timed-out` emission (~lines 410-418), leaving the `echo "timed out..."; exit 124`.
  - Change the "panel: will record vote as seat:$TO" notice (~357) to reflect that the BRIDGE now records the vote (not the wrapper) — e.g. `echo "panel: bridge will record vote as seat:$TO for run $RUN_ID" >&2`. (plan-panel P2: don't leave a stale UX string implying the wrapper votes.)

- [ ] **Step 3: Run the dispatch tests — expect PASS** (marker + gate behavior unchanged)
Run: `$PYTEST tests/test_agent_dispatch_audit_panel.py -v`
If a test asserted the wrapper emits a vote, replace it with an assertion that `--audit-panel --run-id X --dry-run-envelope` stamps `payload.audit_vote_expected==true` and that no `arb-panel-vote` invocation occurs (the bridge owns it).

- [ ] **Step 4: Commit**
```bash
git add scripts/agent-dispatch tests/test_agent_dispatch_audit_panel.py
git commit -m "refactor(audit): remove wrapper vote emission (bridge is the single vote emitter)"
```

---

### Task 5: Live E2E audit round-trip + manifest-preflight runbook

**Files:**
- Create: `tests/e2e_audit_roundtrip.py` (standalone, live infra — not auto-collected)
- Modify: `docs/orchestrator-patterns.md` (add a short "panel audit runbook" section: preflight manifest → dispatch → bridge votes → reconcile-gated verdict)

**Interfaces:**
- Consumes (plan-panel P2 — exact import paths): `from arb_memory.audit import AuditRun, AuditConsumer` (BOTH live in `arb_memory.audit`, NOT a separate `audit_consumer` module); `from arb_memory.panel_audit import reconcile`; the real `Bridge._emit_vote` path (Task 3). `AuditRun`'s `prefix` is keyword-only: `AuditRun(redis, run_id, prefix=PREFIX)`. `AuditConsumer(redis, conn_factory, *, prefix=...)` with `.step()` returning `None` when drained (mirrors `EvalConsumer`).

- [ ] **Step 1: Write the E2E** (mirror `tests/e2e_eval_roundtrip.py` structure: unique run_id + prefix, live local Redis + Postgres, cleanup own rows). The flow:
```python
# 1) preflight manifest (seq=1): AuditRun(redis, run_id, prefix).emit("orchestrator","dispatch",{"roster":["seat:A","seat:B"]})
# 2) drive Bridge._emit_vote for two fake seats A,B with fenced approve stances + audit_vote_expected → vote rows seq 2,3
# 3) run AuditConsumer to drain stream -> audit_events
# 4) emit a matching verdict {roster, stances} and call reconcile(conn, run_id, verdict) -> assert ok is True
# 5) NEGATIVE: a third run where seat B's reply is bare-JSON (no fenced) -> _emit_vote produces NO vote ->
#    reconcile(verdict) -> assert ok is False with a "never voted" gap (proves guard b+c + fail-loud)
# checks printed like e2e_eval_roundtrip; DELETE rows WHERE run_id LIKE 'e2e-audit-%' + del stream keys in finally
```
Use `Bridge.__new__(Bridge)` with `audit_redis` = a real redis client on a throwaway db (db-14), `agent_id` set per fake seat, `_audit_prefix` = the unique prefix.

- [ ] **Step 2: Run it (3 isolated runs) — expect PASS + 0 residue**
```bash
for i in 1 2 3; do /Users/<user>/<workspace>/.venv/bin/python tests/e2e_audit_roundtrip.py || echo FAIL; done
```
Expected: positive reconcile ok=True, negative reconcile ok=False ("seat never voted"), cleanup leaves 0 `e2e-audit-%` rows.

- [ ] **Step 3: Write the manifest-preflight runbook** in `docs/orchestrator-patterns.md` — the orchestrator MUST, before dispatching a panel: `arb-audit-emit --kind dispatch --payload '{"roster":["seat:<id1>",...]}'` (seq=1); roster entries are the registered bridge agent-ids; after replies land, `arb-audit-emit --kind verdict --payload '{"roster":[...],"stances":{...}}'` (reconcile-gated). Note `--role` panels must use the role-suffixed ids.

- [ ] **Step 4: Commit**
```bash
git add tests/e2e_audit_roundtrip.py docs/orchestrator-patterns.md
git commit -m "test(audit): live E2E audit round-trip (manifest+bridge votes+reconcile) + panel runbook"
```

---

## Self-Review

**Spec coverage:** §1 bridge vote extraction → Task 3 (+ Task 1 strict parse, Task 2 audit_redis); §2 timeout/never-popped → Task 3 (timeout synth) + Task 5 negative E2E (fail-loud gap); §3 manifest preflight → Task 5 runbook + E2E; §4 actor contract → Task 3 tests; §5 audit_redis → Task 2; wrapper removal → Task 4. All covered.

**Placeholder scan:** every code step carries real code grounded in the cited source. The only soft spot is Task 4's exact line numbers (the wrapper-vote blocks) — mitigated by Step 1's grep to locate them before editing.

**Type consistency:** `parse_stance(text, *, require_fence=False)` matches its Task-3 call site (`require_fence=True`). `resolve_audit_redis(env) -> (url, prefix)` matches its `__init__` use. `_emit_vote(envelope, result)` matches the `process_request` call. Actor `"seat:" + self.agent_id` consistent across `_emit_vote` and the E2E roster. `AuditRun(redis, run_id, prefix=).emit(source, kind, payload)` matches audit.py:75-91.

**Coherence:** depends on the merged Slice-1 marker living in `payload` (Task 3 reads `payload.audit_vote_expected`) — confirmed shipped (`9e6d423`). `arb_memory` must be importable at bridge runtime (lazy import inside `_emit_vote`; an ImportError is caught by guard d → fail-soft). The executor should confirm `python -c "import arb_memory.audit"` works on the bridge's PYTHONPATH before relying on live emission.

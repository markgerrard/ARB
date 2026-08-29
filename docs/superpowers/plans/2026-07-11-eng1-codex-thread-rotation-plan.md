# ENG-1 implementation plan — codex warm-engine thread rotation (rev 2.2)

**Design (CLOSED v1.2):** `docs/superpowers/specs/2026-07-11-eng1-codex-thread-rotation-design.md`
**Implementor:** codex-bridge-dev-luna, `--effort high`, one task per dispatch, fresh
cold-Opus gate per task before the next is dispatched.
**Workspace:** `/Users/<user>/<workspace>/.claude/worktrees/eng1` (branch `feat/eng1-thread-rotation`
off `dev`; pre-created by the orchestrator). ALL work happens inside this worktree.

> **rev 2.1** (world_at: plan b6f79ed, worktree afd428b3 — the tree the BLOCKED ran
> against; pre-image = parent of the rev-2.1 commit d075fad) (luna BLOCKED report, Task 2: the prescribed fakes have `process=None`,
> and `is_healthy()` ANDs the health flag with subprocess liveness — codex.py:200-207,
> CDX-3 — so `is_healthy()` assertions can never pass on a fake. All fake-based
> assertions now check the `healthy` flag directly; D7 is a flag gate, liveness is
> separately pinned by the CDX-3 tests. No design change.)
>
> **rev 2** (plan panel `panel-eng1plan-20260711T102509Z-016eaf`: terra block/P1,
> agy needs-changes/P1, cold-Opus needs-changes/P0; all convergent): `logger.*` →
> `LOGGER.*` (codex.py binds `LOGGER`, line 39 — the lowercase calls were a grok-template
> transcription error that would NameError even existing io tests through the status-less
> else branch); Task 2/3 test SPECS replaced with full transcribable BODIES on a
> self-contained fake (incl. the `send_request_no_wait`-stubbed interrupt-latch fake
> cold-Opus required); fence expanded to the two named `test_codex_approvals.py`
> assertions D7 deliberately flips; Task 1 pre-edit claim corrected (3 fail / 3
> regression-pin pass); pytest prefix sanitized with `env -u ARB_MEMORY_LOCAL_MCP`
> (ambient MCP override polluted `command_args()` in 2 io tests).

## Standing contract (every task)

- TDD strictly: write the task's tests EXACTLY as given, run them, SHOW the failure,
  then apply the implementation edit EXACTLY as given, run again, SHOW the pass.
- Test command prefix, always (worktree-shadowing guard + sanitized env):
  `cd /Users/<user>/<workspace>/.claude/worktrees/eng1 && env -u ARB_MEMORY_LOCAL_MCP PYTHONPATH=$PWD/src /Users/<user>/<workspace>/.venv/bin/python -m pytest -q <files>`
  Before the first run of each task, verify the import target:
  `env -u ARB_MEMORY_LOCAL_MCP PYTHONPATH=$PWD/src /Users/<user>/<workspace>/.venv/bin/python -c "import agent_redis_bridge.engines.codex as m; print(m.__file__)"`
  must print the WORKTREE path. If it prints the main checkout, STOP and report BLOCKED.
- Files you may touch, whole plan: `src/agent_redis_bridge/engines/codex.py`,
  `tests/test_codex_rotation.py` (new), `tests/test_codex_approvals.py` (ONLY the two
  assertion flips named in Task 2), `tests/test_codex_io.py` (ONLY fixture alignments
  named in Task 2, if any prove needed), `CHANGELOG.md` (Task 4 only). Out of scope,
  hard fence: `bridge.py`, `engine_pool.py`, every other engine, every other test file,
  all docs.
- Each task ends with ONE commit using the exact message given. Reply contract, inline,
  every task: `STATUS: DONE|BLOCKED` / `TASK: <n>` / `SHA: <full sha>` / `TESTS: <file>:
  N passed, M failed` (before AND after) / `NOTES: <surprises, or "none">`. Write the
  full run transcript to
  `/private/tmp/claude-501/-Users-mark-<workspace>/4e5d93a9-c987-4a22-b917-56a914d74fb6/scratchpad/eng1-impl/task-<n>-report.md`.
- If ANY instruction conflicts with what you find in the code, do NOT improvise: report
  BLOCKED with the exact conflict.

## Task 1 — config skeleton: property, counters, declarations (design D4/D9/D10, F2/F3)

**Tests first** — create `tests/test_codex_rotation.py` with EXACTLY:

```python
"""ENG-1 rotation unit tests (design v1.2, G1)."""
import logging
import os
import queue
from typing import Any

import pytest

from agent_redis_bridge.engines.codex import AppServerError, CodexEngine


def _with_env(env, factory):
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        return factory()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def make_engine(retire=None, cap=None):
    return _with_env(
        {"BRIDGE_CODEX_RETIRE_AFTER_TURN": retire, "BRIDGE_CODEX_MAX_PROCESS_TURNS": cap},
        lambda: CodexEngine(cwd="/tmp", model=None, approval_policy="never", sandbox="read-only"),
    )


# --- Task 1: property / counters / declarations -------------------------------
# NOTE: the first two tests and test_cap_zero_means_unlimited PASS before the
# Task-1 edit (regression pins on existing env-flag semantics); the other three
# FAIL before it. Report the observed split.


def test_construction_retire_default_on():
    eng = make_engine(retire=None)
    assert eng.retire_after_turn is True


def test_construction_retire_opt_out():
    eng = make_engine(retire="0")
    assert eng.retire_after_turn is False


def test_cap_flips_retire_property():
    eng = make_engine(retire="0", cap="3")
    assert eng.retire_after_turn is False
    eng._process_turns = 3
    assert eng.retire_after_turn is True   # D9: capped engine retires itself at release


def test_cap_zero_means_unlimited():
    eng = make_engine(retire="0", cap="0")
    eng._process_turns = 10_000
    assert eng.retire_after_turn is False


def test_supports_continuation_tripwire():
    # D10: enabling continuation without resetting _thread_turns per
    # drive_to_completion attempt would rotate mid-dispatch and destroy the
    # dispatch's own context. Do NOT flip this without the D10 enable-path work.
    assert CodexEngine.supports_continuation is False


def test_counters_initialized():
    eng = make_engine()
    assert eng._thread_turns == 0 and eng._process_turns == 0 and eng._interrupted is False
```

Run (expect: `test_cap_flips_retire_property`, `test_supports_continuation_tripwire`,
`test_counters_initialized` FAIL — AttributeError shapes; the other three pass as
regression pins).

**Implementation** — in `src/agent_redis_bridge/engines/codex.py`:

1. Add to the class body, directly under `class CodexEngine:` (before `__init__`):
```python
    # ENG-1 D10 tripwire: the bridge's drive_to_completion re-prompts the SAME
    # engine with no resume/fork between attempts; per-dispatch thread rotation
    # (D2) would destroy the dispatch's own context on attempt 2. Enabling
    # continuation requires resetting _thread_turns per continuation attempt.
    supports_continuation = False
```
2. In `__init__`, REPLACE the two lines
```python
        raw_retire = os.environ.get("BRIDGE_CODEX_RETIRE_AFTER_TURN")
        self.retire_after_turn = str(raw_retire).lower() not in {"0", "false"}
```
   with
```python
        raw_retire = os.environ.get("BRIDGE_CODEX_RETIRE_AFTER_TURN")
        self._retire_after_turn_env = str(raw_retire).lower() not in {"0", "false"}
        # ENG-1 D9: process-lifetime bound for warm (retire=0) seats. 0 = unlimited.
        self._max_process_turns = int(os.environ.get("BRIDGE_CODEX_MAX_PROCESS_TURNS", "20"))
        # ENG-1 D2/D7 state: per-thread turn count (reset on any thread install),
        # per-process turn count (never reset), interrupt latch for D7.
        self._thread_turns = 0
        self._process_turns = 0
        self._interrupted = False
```
   (Keep the retire-rationale comment block above it unchanged.)
3. Add the property immediately after `__init__`:
```python
    @property
    def retire_after_turn(self) -> bool:
        """ENG-1 D9: env flag, or the process-turns cap on warm seats. Read-only
        by design — the pool reads this dynamically at release, so a capped
        engine retires itself with zero pool changes."""
        if self._retire_after_turn_env:
            return True
        cap = self._max_process_turns
        return cap > 0 and self._process_turns >= cap
```

Run all of `tests/test_codex_rotation.py` + `tests/test_codex_retire.py` +
`tests/test_engine_pool.py` — all green.

**Commit:** `feat(codex): ENG-1 T1 — retire_after_turn property + process-turns cap + rotation counters + supports_continuation tripwire`

## Task 2 — D7 affirmative health (allowlist clean terminal) + start_thread validation (F1/R4)

**Tests first** — append to `tests/test_codex_rotation.py` EXACTLY:

```python
# --- Task 2: D7 affirmative health --------------------------------------------


class RotationEngine(CodexEngine):
    """Self-contained fake: scripted request() + message queue, no subprocess.
    Mirrors tests/test_codex_io.py's FakeCodexEngine, plus: turn/start enqueues
    its own terminal (configurable status), send_request_no_wait is stubbed so
    interrupt() works without a process, and thread/turn failures are scriptable."""

    def __init__(self) -> None:
        super().__init__(
            cwd="/tmp", model="gpt-5.5", approval_policy="never", sandbox="workspace-write"
        )
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.thread_counter = 0
        self.turn_counter = 0
        self.thread_start_calls = 0
        self.sent_turn_threads: list[str] = []
        self.sent_no_wait: list[tuple[str, dict[str, Any]]] = []
        self.fail_next_thread_start = False
        self.fail_next_turn_start = False
        self.terminal_status: str | None = "completed"

    def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        if method == "thread/start":
            if self.fail_next_thread_start:
                self.fail_next_thread_start = False
                raise AppServerError("scripted thread/start failure")
            self.thread_start_calls += 1
            self.thread_counter += 1
            return {"thread": {"id": f"thread-{self.thread_counter}"}}
        if method == "thread/resume":
            return {}
        if method == "thread/fork":
            return {"thread": {"id": "thread-child"}}
        if method == "turn/start":
            if self.fail_next_turn_start:
                self.fail_next_turn_start = False
                raise AppServerError("turn/start timed out after 30s")
            self.sent_turn_threads.append(params["threadId"])
            self.turn_counter += 1
            turn_id = f"turn-{self.turn_counter}"
            turn: dict[str, Any] = {"id": turn_id}
            if self.terminal_status is not None:
                turn["status"] = self.terminal_status
            self.messages.put(
                {"method": "turn/completed", "params": {"turnId": turn_id, "turn": turn}}
            )
            return {"turn": {"id": turn_id}}
        return {}

    def send_request_no_wait(self, method: str, params: dict[str, Any]) -> int:
        self.sent_no_wait.append((method, params))
        return 999

    def _get_message(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None


def make_rotation_engine(retire=None, cap=None):
    return _with_env(
        {"BRIDGE_CODEX_RETIRE_AFTER_TURN": retire, "BRIDGE_CODEX_MAX_PROCESS_TURNS": cap},
        RotationEngine,
    )


def _run(eng, on_event=None):
    return eng.run_turn_with_progress("hi", timeout=5, policy="trusted", on_event=on_event)


def test_clean_completed_status_reaffirms_healthy():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    result = _run(eng)
    assert result.ok is True
    assert eng.healthy is True   # D7 flag; is_healthy() also ANDs process liveness (CDX-3), which these process-less fakes cannot satisfy


def test_missing_status_quarantines(caplog):
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.terminal_status = None
    with caplog.at_level(logging.WARNING, logger="agent_redis_bridge.engines.codex"):
        result = _run(eng)
    assert result.ok is True            # ok semantics UNCHANGED (blocklist)
    assert eng.healthy is False    # reuse semantics: allowlist
    assert "non-clean terminal status" in caplog.text


def test_unknown_status_quarantines():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.terminal_status = "cancelled"
    result = _run(eng)
    assert eng.healthy is False


def test_interrupted_status_quarantines_but_ok_unchanged():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.terminal_status = "interrupted"
    result = _run(eng)
    assert result.ok is True            # today's ok computation, pinned
    assert eng.healthy is False    # D7: the process is never reused


def test_interrupt_sets_latch():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.active_turn_id = "turn-x"
    eng.interrupt()
    assert eng._interrupted is True
    assert eng.sent_no_wait and eng.sent_no_wait[-1][0] == "turn/interrupt"


def test_interrupt_latch_quarantines_even_on_clean_status():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"

    def on_event(name, data):
        if name == "turn_started":
            eng.interrupt()

    result = _run(eng, on_event=on_event)
    assert eng.healthy is False    # latch wins over the clean status


def test_start_thread_rejects_empty_id():
    eng = make_rotation_engine()
    eng.thread_id = "thread-keep"

    def bad_request(method, params, *, timeout):
        return {"thread": {"id": ""}}

    eng.request = bad_request  # type: ignore[method-assign]
    with pytest.raises(AppServerError):
        eng.start_thread()
    assert eng.thread_id == "thread-keep"
```

Run: all Task-2 tests FAIL before the edit EXCEPT `test_clean_completed_status_reaffirms_healthy`
(passes vacuously against optimistic health — report it as such) and
`test_start_thread_rejects_empty_id`'s failure shape is the missing-raise. Show shapes.

**Implementation** — in `codex.py`:

1. `run_turn_with_progress` — at the very top of the method body (BEFORE the existing
   `if self.thread_id is None:` guard), insert:
```python
        # ENG-1 D7: affirmative health — a warm process must EARN reuse each
        # turn. True again only on a clean, uninterrupted "completed" terminal.
        self.healthy = False
        self._interrupted = False
```
2. `interrupt()` — after the `send_request_no_wait(...)` call, add:
```python
        self._interrupted = True
```
3. The `turn/completed` terminal branch — directly after the existing line
   `ok = status not in {"errored", "failed"} if status is not None else True`, add:
```python
                if status == "completed" and not self._interrupted:
                    # ENG-1 D7 allowlist: the ONLY place healthy flips back True.
                    self.healthy = True
                else:
                    LOGGER.warning(
                        f"[codex] non-clean terminal status={status!r} interrupted={self._interrupted} — quarantining warm process"
                    )
```
   (`TurnResult.ok` computation stays byte-identical. Note: `LOGGER`, uppercase —
   codex.py:39.)
4. `start_thread()` — change the validation line to mirror `fork_thread`:
```python
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or not thread["id"]:
```
5. **Named assertion flips in `tests/test_codex_approvals.py`** (D7 makes deny-exhaust
   grace-success UNCLEAN; under retire=1 the engine was stopped either way, so fleet
   behavior is unchanged — only the pin moves):
   - line 265: `assert engine.healthy is True  # grace success: engine reusable`
     → `assert engine.healthy is False  # ENG-1 D7: deny-exhaust is unclean; retire path identical`
   - line 379: `assert engine.healthy is True  # grace success`
     → `assert engine.healthy is False  # ENG-1 D7: deny-exhaust is unclean`
   Touch NOTHING else in that file. If the surrounding lines don't match these
   anchors, report BLOCKED.

Run: `tests/test_codex_rotation.py`, `tests/test_codex_io.py`,
`tests/test_codex_approvals.py`, `tests/test_codex_retire.py` — all green. If any
`test_codex_io.py` test fails on a health assertion against a status-less fake terminal,
add `"status": "completed"` to that fake's `turn/completed` payload ONLY where the
test's intent is a clean turn, and report each as `file:test_name — added
status=completed`; if intent is ambiguous, BLOCKED.

**Commit:** `feat(codex): ENG-1 T2 — D7 affirmative health with completed-only allowlist + interrupt latch + start_thread empty-id guard`

## Task 3 — rotation itself (D1/D2/D3, R1)

**Tests first** — append to `tests/test_codex_rotation.py` EXACTLY:

```python
# --- Task 3: rotation ----------------------------------------------------------


def test_rotation_fires_on_second_turn_when_not_retiring():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    assert eng.thread_id == "thread-1"
    _run(eng)
    assert eng.thread_start_calls == 1     # no rotation on the fresh thread
    _run(eng)
    assert eng.thread_start_calls == 2     # rotated
    assert eng.thread_id == "thread-2"
    assert eng.sent_turn_threads == ["thread-1", "thread-2"]


def test_no_rotation_when_retiring():
    eng = make_rotation_engine()
    eng.start_thread()
    _run(eng)
    _run(eng)
    assert eng.thread_start_calls == 1


def test_no_rotation_after_resume():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    _run(eng)
    eng.resume_thread("t-explicit")
    _run(eng)
    assert eng.thread_start_calls == 1               # rotation must NOT fire
    assert eng.sent_turn_threads[-1] == "t-explicit"  # the resumed thread served


def test_no_rotation_after_fork():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    _run(eng)
    eng.fork_thread("t-base")
    _run(eng)
    assert eng.thread_start_calls == 1
    assert eng.sent_turn_threads[-1] == "thread-child"


def test_no_rotation_after_reset_context():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    _run(eng)
    eng.reset_context()                    # start_thread -> thread-2, counter reset
    assert eng.thread_start_calls == 2
    _run(eng)
    assert eng.thread_start_calls == 2     # no ADDITIONAL rotation
    assert eng.sent_turn_threads[-1] == "thread-2"


def test_rotation_failure_quarantines():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    _run(eng)
    eng.fail_next_thread_start = True
    with pytest.raises(AppServerError, match="thread rotation failed"):
        _run(eng)
    assert eng.healthy is False
    assert eng.thread_id == "thread-1"     # no fallback, no partial flip


def test_counter_dirty_before_send():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    eng.fail_next_turn_start = True
    with pytest.raises(AppServerError):
        _run(eng)
    assert eng._thread_turns == 1          # R1: attempted == dirty
```

Run (rev 2.2 corrected expectation): `test_rotation_fires_on_second_turn_when_not_retiring`,
`test_rotation_failure_quarantines`, and `test_counter_dirty_before_send` FAIL before the
edit; the four `test_no_rotation_*` tests PASS pre-edit (they pin the absence-of-rotation
invariants and gain their teeth from Deny-proof A in Task 4). Show the failure shapes and
report the observed split.

**Implementation** — in `codex.py`:

1. Add the method (place directly before `run_turn_with_progress`):
```python
    def _rotate_thread_if_reused(self) -> None:
        """ENG-1 D2 (grok D3b mirror): a non-retiring engine keeps its warm
        process but NEVER reuses a thread across dispatches. thread_id flips
        only on a successful thread/start; any failure quarantines (D3)."""
        if self.retire_after_turn or self._thread_turns == 0:
            return
        old_thread = self.thread_id
        try:
            self.start_thread()
        except AppServerError as exc:
            self.healthy = False
            raise AppServerError(f"thread rotation failed; engine quarantined: {exc}") from exc
        LOGGER.info(
            f"[codex] rotated thread {old_thread!r} -> {self.thread_id!r} (fresh context per dispatch)"
        )
```
2. `start_thread()` / `resume_thread()` / `fork_thread()` — in each, immediately after
   the `self.thread_id = ...` assignment, add:
```python
        self._thread_turns = 0
```
3. `run_turn_with_progress` — after the Task-2 entry block AND after the existing
   `if self.thread_id is None:` guard, insert IN ORDER, immediately before the
   `self.request("turn/start", ...)` call:
```python
        self._rotate_thread_if_reused()
        # ENG-1 R1 NORMATIVE: dirty BEFORE the send — a lost turn/start response
        # can leave a server-side-accepted turn; attempted == dirty.
        self._thread_turns += 1
        self._process_turns += 1
```

Run: `tests/test_codex_rotation.py` + the four codex test files +
`tests/test_engine_pool.py` — green.

**Commit:** `feat(codex): ENG-1 T3 — per-dispatch thread rotation with fail-closed quarantine and dirty-before-send counters`

## Task 4 — deny-proofs (G4) + sweep + CHANGELOG

1. **Deny-proof A (rotation guard is load-bearing):** comment out the
   `self._rotate_thread_if_reused()` line, run `tests/test_codex_rotation.py` — record
   which tests go red (MUST include `test_rotation_fires_on_second_turn_when_not_retiring`).
   Restore the line (uncomment — do NOT re-type), re-run, green.
2. **Deny-proof B (allowlist is load-bearing):** temporarily change
   `status == "completed"` to `status not in {"errored", "failed"}`, run — record reds
   (MUST include `test_missing_status_quarantines` and `test_unknown_status_quarantines`).
   Restore, re-run, green.
3. **Deny-proof C (tripwire):** temporarily set `supports_continuation = True`, run —
   `test_supports_continuation_tripwire` red. Restore.
   Include all three red/green transcripts in the task report.
4. Full targeted sweep:
   `tests/test_codex_rotation.py tests/test_codex_io.py tests/test_codex_approvals.py tests/test_codex_retire.py tests/test_codex_reasoning_effort.py tests/test_engine_pool.py tests/test_bridge_handle_raw.py`
   — all green; report exact counts per file.
5. `CHANGELOG.md` — under `## Unreleased — dev`, add (merge-append, do not touch other
   entries):
```markdown
- ENG-1: codex warm-engine thread rotation (2026-07-11). WHAT: BRIDGE_CODEX_RETIRE_AFTER_TURN=0
  is now SAFE — a warm app-server rotates to a fresh thread per dispatch (grok D3b mirror,
  fail-closed quarantine), health is affirmative (True only on a clean uninterrupted
  "completed" terminal — allowlist), and BRIDGE_CODEX_MAX_PROCESS_TURNS (default 20) bounds
  process lifetime via a dynamic retire_after_turn property. Fleet default (retire ON)
  unchanged; per-seat warm adoption gated on the ENG-1 live gates. WHY: retire-after-turn
  made every dispatch pay the 2.6–13s codex spawn (DSP-1 root cause) — rotation keeps the
  thread-level contamination guarantee without the spawn tax. Design:
  docs/superpowers/specs/2026-07-11-eng1-codex-thread-rotation-design.md (v1.2, 2-round panel).
```

**Commit:** `feat(codex): ENG-1 T4 — deny-proofs verified + targeted sweep + CHANGELOG`

## Post-certification drift record (audit annotation)

The plan panel (`panel-eng1plan-20260711T102509Z-016eaf`) certified **rev 2.0**. The
shipped plan is **rev 2.2**. Certified-to-shipped delta, explicitly listed:

- **rev 2.1** (uncertified, mechanical): six assertion sites `eng.is_healthy()` →
  `eng.healthy` in Task 2/3 test bodies, after luna's T2 BLOCKED proved `is_healthy()`
  unsatisfiable on process-less fakes (ANDs subprocess liveness, codex.py:200-207).
  Diagnosed and regex-patched by the plan author; INDEPENDENTLY VERIFIED downstream:
  the T2 cold gate was shown rev 2.1 explicitly and verified the five T2 sites verbatim
  (GATE PASS), the sixth site (T3's quarantine test) was verified verbatim by the T3
  gate (GATE PASS).
- **rev 2.2** (uncertified, narration-only): Task 3's pre-edit expectation corrected
  (four `no_rotation_*` pins pass vacuously pre-edit; three positive tests fail), after
  luna's T3 BLOCKED. No test or implementation code changed.

Both amendments originated as implementor BLOCKED reports — 3-for-3 precision on
genuine plan bugs this run (T2 fixture-vs-runtime, T3 red-phase-never-red, after
GROK-1's fixture-lifecycle specimen). That ledger is the business case for the
pre-flight below.

## Pre-flight fixture smoke (scripts/plan-fixture-smoke)

Two claim sub-species that static plan panels are structurally blind to (three
specimens across GROK-1 + ENG-1 — falsification requires EXECUTION):
**(A)** a fixture cannot satisfy a predicate the plan's tests assert against it;
**(B)** a test the plan claims fails pre-edit actually passes (inert pin).
Run at each dispatch boundary, against the tree as it stands, BEFORE dispatching:

```
env -u ARB_MEMORY_LOCAL_MCP PYTHONPATH=<tree>/src \
  .venv/bin/python scripts/plan-fixture-smoke <this plan> --task=<N>
```

```python fixture-smoke
# Sub-species A pin — the exact ENG-1 T2 specimen, made executable. Valid against
# BASE and post-D7 trees alike: a process-less fake can NEVER satisfy is_healthy()
# (it ANDs subprocess liveness), so plan tests must assert the `healthy` flag.
import queue
from typing import Any

from agent_redis_bridge.engines.codex import AppServerError, CodexEngine


class SmokeRotationEngine(CodexEngine):
    def __init__(self) -> None:
        super().__init__(
            cwd="/tmp", model="gpt-5.5", approval_policy="never", sandbox="workspace-write"
        )
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.turn_counter = 0

    def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        if method == "thread/start":
            return {"thread": {"id": "thread-smoke"}}
        if method == "turn/start":
            self.turn_counter += 1
            tid = f"turn-{self.turn_counter}"
            self.messages.put(
                {"method": "turn/completed", "params": {"turnId": tid, "turn": {"id": tid, "status": "completed"}}}
            )
            return {"turn": {"id": tid}}
        return {}

    def send_request_no_wait(self, method: str, params: dict[str, Any]) -> int:
        return 999

    def _get_message(self, timeout: float):
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None


eng = SmokeRotationEngine()
eng.thread_id = "thread-0"
result = eng.run_turn_with_progress("smoke", timeout=5, policy="trusted", on_event=None)
assert result.ok is True, "fake cannot complete a clean turn — fixture broken"
assert eng.process is None and eng.is_healthy() is False, (
    "is_healthy() became satisfiable on a process-less fake?! plan tests may then use it"
)
assert isinstance(eng.healthy, bool), "healthy flag missing — the assertable surface"
eng.active_turn_id = "turn-x"
eng.interrupt()  # must be callable on the fake (send_request_no_wait stubbed)
```

```python fixture-smoke task=1
# Sub-species B pin for the Task-1 dispatch boundary: the three tests the plan
# claims fail pre-edit must ACTUALLY fail against the tree at this boundary.
# (The other three are declared regression pins — expected green — and deliberately
# NOT listed here.)
T1_RED = ["test_cap_flips_retire_property", "test_supports_continuation_tripwire", "test_counters_initialized"]
import pathlib as _p
_plan = _p.Path(__file__ if "__file__" in dir() else ".")  # namespace exec — source below
T1_SRC = '''
import os
from agent_redis_bridge.engines.codex import CodexEngine

def _with_env(env, factory):
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None: os.environ.pop(k, None)
        else: os.environ[k] = v
    try:
        return factory()
    finally:
        for k, v in old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

def make_engine(retire=None, cap=None):
    return _with_env(
        {"BRIDGE_CODEX_RETIRE_AFTER_TURN": retire, "BRIDGE_CODEX_MAX_PROCESS_TURNS": cap},
        lambda: CodexEngine(cwd="/tmp", model=None, approval_policy="never", sandbox="read-only"),
    )

def test_cap_flips_retire_property():
    eng = make_engine(retire="0", cap="3")
    assert eng.retire_after_turn is False
    eng._process_turns = 3
    assert eng.retire_after_turn is True

def test_supports_continuation_tripwire():
    assert CodexEngine.supports_continuation is False

def test_counters_initialized():
    eng = make_engine()
    assert eng._thread_turns == 0 and eng._process_turns == 0 and eng._interrupted is False
'''
red_claim(T1_SRC, expect_fail=T1_RED)
```

(Task-2/3 red-claims need the cumulative test file from the prior tasks' commits — a
file-based `red_claim` variant is the tool's named v1 follow-up; this plan's T3
boundary was verified by hand via luna's BLOCKED + rev 2.2.)

## Post-plan (orchestrator, NOT luna)

Tri-model final review (cold-Opus certifies — implementation author is codex lineage;
terra non-certifying; agy + GLM), merge to dev, then live gates G2/G3/G5 on a scratch
retire=0 seat before ANY fleet seat flips.

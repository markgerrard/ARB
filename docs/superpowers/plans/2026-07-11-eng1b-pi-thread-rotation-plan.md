# ENG-1b implementation plan — pi-sdk warm-engine session rotation (rev 2.1)

> **rev 2.1** (world_at: plan 13e7fb9/ca8d969) (GLM's re-fired plan review, on rev 2): the rev-2 wiring test was
> self-defeating — `make_bridge()` defaults to `--engine codex`, so
> `engine_supports_resume=True` at construction and the True-arm was unreachable
> (luna would BLOCK, or worse, defang the assertion). The test now constructs
> `make_bridge("--engine", "pi-sdk")`, with an explicit do-not-weaken instruction.
> GLM also confirmed the [J]-class posture of the bridge touch is clean.

> **rev 2** (world_at: plan 8c937ef — also the T1-BLOCKED incident tree, whose
> report-path line then read `.../scratchpad/...` with an unresolved ellipsis;
> pre-image = parent of d3554d5) (plan panel `panel-eng1bplan-20260711T122338Z-ac94e1`: agy needs-changes/P0,
> terra needs-changes/P1, cold-Opus needs-changes/P1; GLM absent — its r2 design-review
> turn was wedged on a runaway root `find` and its slot never freed in time):
> Task 1's tests were unimplementable as specified (host handlers/state private; DI seam
> absent) → Task 1 now begins with a behavior-preserving `createHost(deps)` factory
> refactor with an explicit contract. Task 3's bridge edit was wrong in BOTH placement
> candidates (locals are `envelope`/`task_engine`, `thread_id` is not a local, and
> anything inside the effort applier is DEAD for pi — pi_sdk lacks
> `set_turn_reasoning_effort`, so that method early-returns): the edit is now a
> standalone 4-line statement with orchestrator-verified names, placed after the effort
> call in `process_request`, plus a NEW bridge-level wiring test (fence expanded for one
> named test in tests/test_bridge_handle_raw.py) so dead wiring cannot pass green.
> T3's flip block re-bound to the handler's real locals (`params`, existing `ok` —
> pi_sdk.py:483-486). Task 4 sweep scoped to explicit files (a bare `-k` over tests/
> dies collecting DSN-gated arb_messages modules; e2e excluded). Node commands gain
> `--test-force-exit` (dangling-handle hang observed on the existing suite).

**Design (CLOSED v1.2):** `docs/superpowers/specs/2026-07-11-eng1b-pi-thread-rotation-design.md`
**Implementor:** codex-bridge-dev-luna, `--effort high`, one task per dispatch, fresh
cold-Opus gate per task. **Pre-flight:** `scripts/plan-fixture-smoke <this plan> --task=<N>`
runs at EVERY dispatch boundary before the task is dispatched (orchestrator does this).
**Workspace:** `/Users/<user>/<workspace>/.claude/worktrees/eng1b` (branch
`feat/eng1b-pi-rotation` off `dev`; pre-created by the orchestrator).

## Standing contract (every task)

Identical to the ENG-1 plan's contract (TDD with shown red/green, BLOCKED-not-improvise,
one commit per task with the exact message, STATUS/TASK/SHA/TESTS/NOTES reply, report to
`/private/tmp/claude-501/-Users-mark-<workspace>/4e5d93a9-c987-4a22-b917-56a914d74fb6/scratchpad/eng1b-impl/task-<n>-report.md` — ABSOLUTE path, never relative), with these deltas:
- Python test prefix: `cd /Users/<user>/<workspace>/.claude/worktrees/eng1b && env -u ARB_MEMORY_LOCAL_MCP PYTHONPATH=$PWD/src /Users/<user>/<workspace>/.venv/bin/python -m pytest -q <files>`
  (verify `module.__file__` is the worktree before first run; BLOCKED if not).
- Node test command (host tasks): `cd /Users/<user>/<workspace>/.claude/worktrees/eng1b/tools/pi-sdk-host && node --test --test-force-exit <files>`
  (`--test-force-exit` is REQUIRED — the existing suite holds a dangling handle and
  hangs without it; node_modules provisioning is ORCHESTRATOR-side, done before
  dispatch via symlink or install.sh — the seat never runs npm OR install.sh (GLM's
  GOV-1 catch: install.sh itself runs npm, so 'run install.sh' and 'do NOT npm-install'
  were contradictory as seat instructions); if imports fail, report BLOCKED).
- Files you may touch, whole plan: `tools/pi-sdk-host/host.mjs`,
  `tools/pi-sdk-host/host.rotate.test.mjs` (new), `src/agent_redis_bridge/engines/pi_sdk.py`,
  `src/agent_redis_bridge/bridge.py` (ONLY the 4-line setter statement named in Task 3),
  `tests/test_pi_sdk_rotation.py` (new), `tests/test_bridge_handle_raw.py` (ONLY the one
  named wiring test in Task 3), `CHANGELOG.md` (Task 4 only). Hard fence:
  `engine_pool.py` (the design deliberately needs NO pool change), every other engine,
  every other test file, all docs.
- pi_sdk.py binds `logger` (lowercase, line 44); host.mjs uses `logStderr`. Do not
  transcribe codex's `LOGGER` into either.

## Task 1 — host.mjs: `thread/rotate` verb (design D-B1, R3/R4/R5)

**Step 0 (rev 2 — the DI seam; behavior-preserving refactor FIRST):** host.mjs's
handlers and `state` are module-private and `doThreadStart` calls
`startSessionWithBridge`/`createAgentSession` non-injectably — the panel confirmed the
originally-specified tests are unimplementable. Add an exported factory with THIS
contract (shape is contracted; internal naming is yours):

```js
export function createHost(deps = {}) {
  // deps: { createSession = createAgentSession, startBridge = startMcpBridge }
  // returns: { state, handlers: { threadStart(id, params), threadRotate(id, params),
  //            turnStart(id, params), turnAbort(id, params) }, replies }
  // `replies` collects every reply/replyError payload the handlers emit (array),
  // so tests assert on it instead of stdout.
}
```

The module's top level builds ONE `const host = createHost()` and the existing stdio
dispatch map delegates to `host.handlers.*` — byte-identical wire behavior. The
existing exports (`buildSessionToolArgs`, `startSessionWithBridge`, `makeTurnHandler`,
`resolveEventLogPath`) remain. GREEN GATE for step 0: the two existing node test files
pass unchanged (with `--test-force-exit`). If the current structure makes the factory
extraction ambiguous anywhere, report BLOCKED with the specific function — do not
restructure beyond the contract.

**Tests then** — create `tools/pi-sdk-host/host.rotate.test.mjs` driving
`createHost({createSession: fake, startBridge: fake})` handlers directly.
Test list (exact names):
- `rotate happy path: new thread id, old session disposed, bridge preserved, reply oldDisposed true`
- `rotate reconstructs resourceLoader when appendSystemPrompt was set`
- `rotate reuses stored authStorage and modelRegistry, fresh SessionManager`
- `rotate rejects: no thread started`
- `rotate rejects: threadId mismatch`
- `rotate rejects: active turn in flight`
- `rotate rejects while rotateInFlight; second concurrent rotate rejected`
- `turn/start rejected while rotateInFlight`
- `create-failure leaves old session installed and replies error`
- `dispose-throw completes swap and replies oldDisposed false`

**Implementation** — in `host.mjs`:
1. `doThreadStart` stores on `state.thread`: `params: {modelSpec, cwd, tools, mcpServers,
   thinkingLevel, appendSystemPrompt}`, plus `authStorage`, `modelRegistry`, and
   `toolArgs` (the `buildSessionToolArgs` result) for replay reuse.
2. Add `state.rotateInFlight = false` to the state initializer.
3. New handler `doThreadRotate(id, params)` wired into the method dispatch map as
   `"thread/rotate"`, per design D-B1 v1.2: synchronous guards (thread exists, threadId
   match, no activeTurn, `!state.rotateInFlight`) → set latch → try: rebuild
   resourceLoader iff appendSystemPrompt set (byte-parity with doThreadStart), fresh
   `SessionManager.inMemory(cwd)`, `createAgentSession` with the STORED toolArgs
   tools/customTools → on success dispose old session in try/catch (catch → `logStderr
   ("rotate_dispose_failed", ...)` + `oldDisposed=false`), swap `state.thread` (new
   `"th_" + randomUUID()`, carry params/authStorage/modelRegistry/toolArgs/bridge),
   reply `{thread: {id}, oldDisposed}` → finally: clear latch. Create-failure: reply
   ERR_INTERNAL, no state change.
4. `handleTurnStart`: add a `state.rotateInFlight` rejection (ERR_BAD_STATE) beside the
   existing thread guards; same for `doThreadStart`.

**Commit:** `feat(pi-sdk-host): ENG-1b T1 — thread/rotate verb with rotateInFlight latch, resourceLoader replay, dispose-throw oldDisposed:false`

## Task 2 — engine skeleton: property, counters, declarations (D-B3, F2/F4 parity)

**Tests first** — create `tests/test_pi_sdk_rotation.py` (mirror
`tests/test_codex_rotation.py`'s `_with_env`/`make_engine` helpers, constructing
`PiSdkEngine` with a stubbed `popen_factory` and `host_script_path` pointing at an
existing file — the reusable fake-process fixture lives in `tests/test_pi_sdk.py` (panel-verified) -
COPY its pattern into the new file, do not import across test files):
- construction pins: retire default ON; `BRIDGE_PI_RETIRE_AFTER_TURN=0` → False
  (regression pins — expected GREEN pre-edit);
- `test_cap_flips_retire_property` (BRIDGE_PI_MAX_PROCESS_TURNS=3) — RED pre-edit;
- `test_cap_zero_means_unlimited` — GREEN pre-edit (pin);
- `test_supports_continuation_tripwire` (`PiSdkEngine.supports_continuation is False`)
  — RED pre-edit (attribute does not exist; bridge getattr hides it);
- `test_counters_initialized` (`_thread_turns/_process_turns/_interrupted/
  _turn_affinity_requested/_quarantine_after_turn` all zero/False) — RED pre-edit.

**Implementation** — in `pi_sdk.py`: class attr `supports_continuation = False` with the
D10-parity comment; constructor rename `self.retire_after_turn = ...` →
`self._retire_after_turn_env = ...` (BOTH assignment sites of the existing
if/else, lines ~152-155) + `_max_process_turns` (env `BRIDGE_PI_MAX_PROCESS_TURNS`,
default 20) + the five state fields; `retire_after_turn` read-only property (codex
ENG-1 body, verbatim semantics).

**Commit:** `feat(pi-sdk): ENG-1b T2 — retire property + cap + rotation state fields + continuation tripwire`

## Task 3 — engine rotation + affirmative health + bridge setter (D-B3/D-B4/D-B5 v1.2)

**Tests first** — append to `tests/test_pi_sdk_rotation.py`:
- allowlist: `stop` and `toolUse` clean → `healthy is True`; `length`, `None`, unknown,
  `error`-field-present, interrupted-latch → False (+ caplog `non-clean terminal`);
- `test_interrupt_sets_latch` (stubbed send path);
- rotation: fires on second turn when retire=0 (fresh `thread/rotate` request observed,
  `thread_id` updated); not when retiring; not on fresh thread; quarantine on rotate
  error (healthy False, thread_id unchanged, raises `EngineError` matching
  `thread rotation failed`); counter dirty-before-send on turn/start raise;
- affinity: `set_turn_thread_affinity(True)` → next turn does NOT rotate AND the flag
  is consumed (the turn after DOES rotate); flag overwritten by a later
  `set_turn_thread_affinity(False)` without any turn in between (the R2 leak pin);
- sticky dispose latch: rotate reply `oldDisposed: false` → turn serves, terminal
  clean, `healthy is False` (the F1 pin);
- bridge setter: in `tests/test_pi_sdk_rotation.py`, a bridge-level test is OUT OF
  SCOPE — instead pin the engine API surface (`set_turn_thread_affinity` exists and
  stores) and leave the bridge wiring test to Task 4's sweep via
  `tests/test_bridge_handle_raw.py` (which must stay green).

**Implementation:**
1. `pi_sdk.py`: `_rotate_thread_if_reused(affinity_requested: bool)` per design
   (skip iff `retire_after_turn or _thread_turns == 0 or affinity_requested`; calls
   `thread/rotate` with current threadId; success → update `thread_id`, zero
   `_thread_turns`, and set `_quarantine_after_turn = True` iff reply
   `oldDisposed is False` with a loud `logger.warning`; failure → healthy False +
   raise). `run_turn_with_progress` entry (unified sequence): healthy False;
   `_interrupted = False`; capture-into-local flag consumption (F3, exactly the design
   pseudocode); rotate; `_thread_turns += 1; _process_turns += 1` BEFORE the
   `turn/start` request. Terminal handler: the D-B4 v1.2 flip block REBOUND to the
   handler's actual locals (pi_sdk.py:483-486 - the dict is `params`, `ok` already
   exists): condition `ok and not params.get("error") and params.get("stopReason") in
   {"stop", "toolUse"} and not self._interrupted and not self._quarantine_after_turn`;
   else-branch logs the design's warning line via lowercase `logger`. `interrupt()`:
   `self._interrupted = True` after the abort send (F2). `start()`: reset
   `_quarantine_after_turn = False`, `_thread_turns = 0`.
2. `bridge.py` — the ONLY permitted edit (rev 2, orchestrator-verified against the
   actual code): in `process_request`, inside the `if result is None:` block,
   IMMEDIATELY AFTER the line
   `self.apply_reasoning_effort_if_requested(envelope, task_engine)` (NOT inside that
   method — pi_sdk lacks `set_turn_reasoning_effort`, so code inside it is dead for pi),
   insert at the same indentation:
```python
                    affinity_setter = getattr(task_engine, "set_turn_thread_affinity", None)
                    if callable(affinity_setter):
                        _affinity_tid = envelope.payload.get("thread_id")
                        affinity_setter(isinstance(_affinity_tid, str) and bool(_affinity_tid) and not self.engine_supports_resume)
```
   The locals really are `envelope` and `task_engine` in that block; `thread_id` is NOT
   a local there. If the anchor line or names differ, BLOCKED.
3. **Bridge wiring test (rev 2.1 — GLM caught the fence defeating itself):** add ONE
   test to `tests/test_bridge_handle_raw.py`, following that file's existing
   fake-engine pattern, named `test_turn_thread_affinity_setter_fires_for_non_resume_engine`.
   **CRITICAL construction detail:** the file's `make_bridge(*extra)` defaults to
   `--engine codex`, which sets `engine_supports_resume = True` at `Bridge.__init__`
   (bridge.py:506) and the handle_raw path never re-derives it — under that default the
   setter's `not self.engine_supports_resume` term is False unconditionally and the
   True-arm is UNREACHABLE. The test MUST construct its bridge as
   `make_bridge("--engine", "pi-sdk")` (parse-level only — handle_raw tests inject fake
   engines, so no real pi engine is built). Then: a dispatch whose payload carries
   `thread_id` to a fake engine exposing `set_turn_thread_affinity` records a `True`
   call; the same dispatch without `thread_id` records `False`. Do NOT weaken the
   True-arm assertion to make it green — if it will not go green as specified, BLOCKED.
   This test exists because dead wiring here would otherwise pass every suite green
   (panel P1); GLM's r2 catch is why the construction detail is spelled out.

**Commit:** `feat(pi-sdk): ENG-1b T3 — session rotation with affinity-aware skip, positive-allowlist affirmative health, sticky dispose quarantine`

## Task 4 — deny-proofs + sweep + CHANGELOG

Deny-proofs (record red/green transcripts): (A) remove the rotation call →
fires-on-reuse red; (B) revert allowlist to the v1.0 denylist → length/unknown
quarantine tests red; (C) remove capture-into-local consumption (read the attr inside
the guard after reset) → affinity-preservation red; (D) `supports_continuation = True`
→ tripwire red; (E) remove the sticky-latch term from the flip → F1 pin red. Restore
after each. Sweep (rev 2 - EXPLICIT files; a bare -k over tests/ dies collecting DSN-gated
arb_messages modules, and e2e is excluded): `tests/test_pi_sdk_rotation.py
tests/test_pi_sdk.py tests/test_pi_rpc.py tests/test_engine_pool.py
tests/test_bridge_handle_raw.py` (drop any of the middle two that do not exist -
report which) + node `--test --test-force-exit host.rotate.test.mjs
host.events.test.mjs host.mcp.test.mjs`. CHANGELOG merge-append (WHAT/WHY, design ref, mirror the ENG-1 entry
shape). **Commit:** `feat(pi-sdk): ENG-1b T4 — deny-proofs verified + sweep + CHANGELOG`

## Pre-flight fixture smoke

```python fixture-smoke
# ORCHESTRATOR world-claims (specimen 4b: the provisioning near-miss, 2026-07-11).
# Run with --tree=<worktree>: these assert the world the plan hands the seat.
import pathlib
assert (TREE / "tools/pi-sdk-host/node_modules/@earendil-works/pi-coding-agent").exists(), (
    f"node_modules not provisioned under {TREE} - symlink from the main checkout before dispatching"
)
assert (TREE / "src/agent_redis_bridge/engines/pi_sdk.py").exists(), "tree is not a bridge checkout"
_report_dir = pathlib.Path(
    "/private/tmp/claude-501/-Users-mark-<workspace>/4e5d93a9-c987-4a22-b917-56a914d74fb6/scratchpad/eng1b-impl"
)
assert _report_dir.is_absolute() and _report_dir.is_dir(), (
    f"report dir missing or not absolute: {_report_dir} - the seat cannot write its reports"
)
```


```python fixture-smoke
# Sub-species A pins for the engine fixtures this plan's tests rely on.
# Valid against BASE and post-implementation trees alike.
import inspect
from agent_redis_bridge.engines.pi_sdk import PiSdkEngine
# The plan's tests assert the `healthy` FLAG (ENG-1 lesson): pin that the flag exists
# and that is_healthy() is NOT the assertable surface on a process-less fake.
sig = inspect.signature(PiSdkEngine.__init__)
assert "popen_factory" in sig.parameters, "fixture pattern relies on popen_factory injection"
import agent_redis_bridge.engines.pi_sdk as m
import logging
assert isinstance(getattr(m, "logger", None), logging.Logger), (
    "pi_sdk must bind lowercase `logger` — plan log lines depend on it (ENG-1 LOGGER lesson)"
)
```

```python fixture-smoke task=2
T2_RED = ["test_cap_flips_retire_property", "test_supports_continuation_tripwire", "test_counters_initialized"]
T2_SRC = '''
import os
from agent_redis_bridge.engines.pi_sdk import PiSdkEngine

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
        {"BRIDGE_PI_RETIRE_AFTER_TURN": retire, "BRIDGE_PI_MAX_PROCESS_TURNS": cap},
        lambda: PiSdkEngine(cwd="/tmp", model=None),
    )

def test_cap_flips_retire_property():
    eng = make_engine(retire="0", cap="3")
    assert eng.retire_after_turn is False
    eng._process_turns = 3
    assert eng.retire_after_turn is True

def test_supports_continuation_tripwire():
    assert PiSdkEngine.supports_continuation is False

def test_counters_initialized():
    eng = make_engine()
    assert eng._thread_turns == 0 and eng._process_turns == 0 and eng._interrupted is False
'''
red_claim(T2_SRC, expect_fail=T2_RED)
```

(NOTE for the orchestrator: if `PiSdkEngine(cwd="/tmp", model=None)` requires more constructor
args, the SMOKE ITSELF will say so at the task-2 boundary — that is the tool doing its
job; fix the plan's `make_engine` before dispatching, not after a BLOCKED. Task 1 is
host-side JS — no python smoke; its red-claims are the node tests, which necessarily
fail pre-edit since the file is new. Task 3/4 red-claims need the cumulative tree —
covered by the file-based red_claim follow-up; until then the T3 boundary relies on
the plan panel + luna's TDD discipline, as ENG-1 did.)

## Post-plan (orchestrator, NOT luna)

Plan panel (single round) → luna per-task with cold gates → tri-model final (cold-Opus
certifies, terra non-certifying) → merge → live gates on a scratch retire=0 pi seat:
G2 contamination, G3 HOST-process RSS + flat token-context (wedge signature must not
reappear), G5 latency. GLM's r2 timeout + the runaway-find note go in the seat ledger.

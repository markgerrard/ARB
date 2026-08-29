# ENG-1 v1: codex warm-engine thread rotation (design)

**Version: v1.2 — DESIGN CLOSED (fold-and-proceed after round 2)**

> **v1.2 changelog** (r2: terra needs-changes/P1, agy needs-changes/P1, cold-Opus
> approve/P2, GLM approve/P2 — run `panel-eng1design-r2-20260711T101309Z-8127ac`; all
> four seats confirmed R1–R6 close their r1 findings; no new structural defects):
> - **F1 (terra P1 / agy P2 / cold-Opus P2 / GLM P2-optional):** D7's clean-terminal set
>   was a blocklist — missing/unknown statuses counted as clean (fail-open against
>   protocol evolution, in the one mechanism whose point is fail-closed reuse). NOW an
>   ALLOWLIST: clean iff `status == "completed"`; `None`/unknown → UNCLEAN with a loud
>   log line naming the status (if real app-servers ever omit status on success, warm
>   mode degrades to retire-per-turn VISIBLY — G5's latency measurement and the log both
>   surface it; safety over spawn-cost). Severity split noted (execute-capable seat P1,
>   static seats P2) — orchestrator adopted the fix regardless; it costs one line.
> - **F2 (agy P1 / cold-Opus P2, convergent):** D9's read-only property collides with
>   the constructor's `self.retire_after_turn = ...` write (`codex.py:121`) —
>   `AttributeError` at construction, every seat. D9 now NAMES the required rename
>   (`self._retire_after_turn_env`) and G1 gains a construction test for both flag
>   states. (Sweep confirmed line 121 is the only breaking write site.)
> - **F3 (terra P2 / GLM):** `CodexEngine` has no `supports_continuation` attribute
>   (bridge uses `getattr(..., False)`), so D10's tripwire would `AttributeError`.
>   CodexEngine now declares `supports_continuation = False` explicitly; tripwire pins
>   it; G4's deny-proof includes the tripwire.
> - **F4 (cold-Opus P2):** D2 and D7 entry sequences unified into one ordered block.
> - GLM residual N1 (future-status leak) is subsumed by F1's allowlist; N2 is D10's
>   named posture — no change.

> **v1.1 changelog** (r1: terra block/P1, GLM needs-changes/P1, cold-Opus needs-changes/P1,
> agy approve/P2 — run `panel-eng1design-r1-20260711T095215Z-967b14`):
> - **R1 (P1, terra + cold-Opus, convergent):** D2 counter placement was self-contradictory
>   (END-of-run pseudocode vs turn-START prose) and END-increment is contamination-unsafe on
>   any raise path after `turn/start` is sent (lost response, malformed reply, process exit).
>   NORMATIVE now: increment BEFORE the `turn/start` send. Pseudocode fixed; G1 gains a
>   response-lost test. OQ1 resolved.
> - **R2 (P1, terra P1a + cold-Opus P2-1 + GLM):** new D7 — affirmative health marking
>   (grok D3a mirror): `healthy` flips False at turn entry and True ONLY on a clean,
>   uninterrupted `turn/completed`. Closes interrupted-turn warm reuse
>   (`codex.py:375` treats "interrupted" as ok=True and left the process reusable).
>   OQ3 resolved: unclean turn ⇒ process quarantined, full stop.
> - **R3 (P1, GLM):** the contamination guarantee is now explicitly bounded to THREAD-level;
>   process-level surfaces rotation does NOT clear are named (D8), and the
>   max-turns-per-process cap is REQUIRED in v1 (D9), not deferred. OQ2 resolved.
> - **R4 (P2, terra):** `start_thread()` must reject an empty thread id (it currently
>   accepts `""` where `fork_thread()` rejects it) — G1 test added.
> - **R5 (P2, agy + GLM, convergent):** latent `supports_continuation` hazard — if codex
>   ever opts in, `drive_to_completion` would rotate mid-dispatch and destroy context.
>   G1 gains a tripwire test pinning `supports_continuation is False` with the
>   enable-path requirement documented (D10).
> - **R6 (P2, cold-Opus + agy):** G2 plant must be purely conversational (never via the
>   shared arb-memory MCP, which is process-lifetime state); background-process
>   persistence named in D8.
**Author: warm-Opus orchestrator (inline; Anthropic lineage → cold-Opus non-certifying on this stage)**
**Scope decision (Mark, 2026-07-11): codex first; pi-sdk is ENG-1b, out of scope here.**
**BACKLOG: § ENG-1. Evidence base: § DSP-1 root cause + GROK-1 session rotation (shipped template).**

## Problem

Retire-after-turn (default ON, all engines, the 2026-07-10 accumulation fix) kills and
respawns the engine process on every dispatch. DSP-1's probes measured what that costs on
codex: initialize runs 2.6–7s warm and ~13s first-after-idle against a 15s budget — every
dispatch pays a multi-second spawn tax, and the engine-start-failed tail (5 occurrences,
2 days) is the distribution crossing the budget under pipeline IO load.

The alternative the fleet already ships for grok (GROK-1 v1.3 D3b): keep the process warm
and NEVER reuse a session across dispatches — rotate to a fresh session per dispatch,
quarantine on rotation failure. Contamination guarantee preserved; spawn tax deleted.

`BRIDGE_CODEX_RETIRE_AFTER_TURN=0` already exists but is UNSAFE today: pool `release()`
returns a non-retiring healthy engine to idle, and the next `acquire()` re-serves it WITH
ITS ACCUMULATED THREAD — exactly the 2026-07-08 incident (one thread, 24 dispatches, 22M
cumulative tokens). This design makes that flag safe, mirroring grok.

## Existing primitives (all verified in code 2026-07-11)

- `CodexEngine.start_thread()` (`engines/codex.py:161`) — `thread/start` on the live
  app-server; sets `self.thread_id` from the response. Already live-exercised per
  dispatch-with-`--fresh-context` via `reset_context()` (`codex.py:510` — it IS
  `start_thread()`).
- Grok template: `_rotate_session_if_reused()` (`grok_acp.py:250`) — rotate iff
  non-retiring AND served ≥1 turn; new id flips ONLY on successful response; any failure
  → `healthy = False` + raise (quarantine; pool stops the engine at release).
- Pool semantics (`engine_pool.py:124-144`): release → unhealthy→stop;
  `retire_after_turn`→stop; else→idle. No pool change needed.
- Effort: the bridge applies `set_turn_reasoning_effort` per dispatch BEFORE `run_turn`
  (`bridge.py:1971-1983`, always set, cleared when absent), and
  `thread_start_params()` reads `_effective_effort()` at thread/start time — so
  rotation-at-turn-start picks up the dispatch's effort on the fresh thread. This
  IMPROVES on today's warm-thread effort stickiness ([[codex-effort-mechanics]]).
- Worktree dispatches run on a DEDICATED engine outside the pool (stopped after the
  reply; `bridge.py:1304`) — pooled engines keep the seat's fixed cwd. Rotation never
  needs a per-dispatch cwd.

## Design

### D1 — Rotation point: lazy, at turn start, mirror grok exactly

`run_turn_with_progress()` calls `self._rotate_thread_if_reused()` as its first act
(before `turn/start`). Lazy-at-use rather than eager-on-release because: (a) grok
precedent, probe-verified; (b) a failure has a live dispatch to report to (eager rotation
failing in `release()` has nobody); (c) the fresh thread picks up this dispatch's
reasoning effort (set before `run_turn`); (d) an idle engine holds no freshly-started
thread that then goes stale.

### D2 — Rotation condition: thread-turns counter, dirtied BEFORE the turn/start send (R1)

```python
# in __init__: self._thread_turns = 0; self._process_turns = 0
# in start_thread(), resume_thread(), fork_thread(): self._thread_turns = 0
# in run_turn_with_progress(), IN THIS ORDER (unified D2+D7 entry sequence, v1.2 F4):
#   1. self.healthy = False; self._interrupted = False      # D7 affirmative entry
#   2. self._rotate_thread_if_reused()
#   3. self._thread_turns += 1; self._process_turns += 1    # BEFORE the turn/start send
#   4. self.request("turn/start", ...)

def _rotate_thread_if_reused(self) -> None:
    if self.retire_after_turn or self._thread_turns == 0:
        return
    old_thread = self.thread_id
    try:
        self.start_thread()          # thread/start; sets self.thread_id on success
    except AppServerError as exc:
        self.healthy = False
        raise AppServerError(f"thread rotation failed; engine quarantined: {exc}") from exc
    LOGGER.info(f"[codex] rotated thread {old_thread!r} -> {self.thread_id!r} (fresh context per dispatch)")  # codex.py binds LOGGER (uppercase), not the grok template's logger
```

**NORMATIVE (R1): the counter increments BEFORE `turn/start` is sent.** The moment the
request bytes leave, the server side may have accepted the turn regardless of whether we
see the reply — `request()` raises on timeout/process-exit WITHOUT marking a live process
unhealthy, `run_engine` surfaces the error without health change, and `release()` would
re-idle the engine. An END-of-run increment leaves `_thread_turns == 0` on every such
raise path (thread_id-None guard aside, all of them post-send), so the next dispatch
would SKIP rotation and reuse a server-side-dirtied thread — the exact contamination this
design exists to prevent. Increment-before-send makes every attempted turn dirty by
construction. (r1: terra + cold-Opus, independently, with disjoint evidence paths.)

Counting turns **per thread** (reset whenever the thread is replaced by ANY path) rather
than per engine closes the explicit-continuation hazard found during design: the bridge
applies `--thread-id` / `--fork-thread-id` via `resume_thread()`/`fork_thread()` BEFORE
`run_turn`, so a naive served-any-turn condition would rotate AWAY from the thread the
caller just explicitly resumed. With the per-thread counter, an explicitly
resumed/forked/fresh thread has `_thread_turns == 0` and is never rotated out from under
its own dispatch. The NEXT unrelated dispatch on the warm engine sees
`_thread_turns == 1` and rotates — correct.

`--fresh-context` (`reset_context()` → `start_thread()`) also zeroes the counter — the
subsequent rotation check is then a no-op rather than a redundant second thread/start.

`start_thread()` gains non-empty-id validation (R4): it currently accepts `""`
(`isinstance` check only, `codex.py:164`) where `fork_thread()` rejects falsy ids — a
malformed `{thread:{id:""}}` reply would silently replace a valid thread id and bypass
the rotation quarantine.

### D3 — Fail-closed quarantine (grok parity)

Any rotation failure: `healthy = False`, raise. The turn fails legibly
(`bridge-error`/turn error to the caller), `release()` sees unhealthy and stops the
process, the next dispatch cold-spawns. `self.thread_id` flips ONLY after a successful
`thread/start` response (that is `start_thread()`'s existing contract — it raises before
assignment on a bad response). There is NO fallback to the old thread.

### D4 — Configuration surface: one new knob, existing flag unchanged (revised v1.1)

`BRIDGE_CODEX_RETIRE_AFTER_TURN` keeps its exact semantics; the default (retire ON) is
unchanged fleet-wide. What changes is that `=0` becomes SAFE. The single new knob is
D9's `BRIDGE_CODEX_MAX_PROCESS_TURNS` (default 20), which only has an effect on warm
(`=0`) seats. Per-seat adoption (flipping any seat to warm mode) is an operational
decision AFTER the live gates, not part of this change.

### D5 — Old-thread disposal semantics

The abandoned thread's rollout persists in `~/.codex/sessions` — byte-for-byte the same
durability retirement produces today (retired processes leave their rollouts; explicit
`--thread-id` continuation resumes from rollouts on any process, live-proven 2026-07-10).
In-process, the app-server drops its handle when serving the new thread; whether it frees
per-thread memory is NOT assumed — it is measured by the RSS gate (G3).

### D6 — Warmup interplay

The bridge warms one engine at startup (`pool.release("__warmup__")`, `bridge.py:695`).
Under retire=ON the warmup engine dies immediately (known grok-spec residual). Under
retire=0 it survives to idle with `_thread_turns == 0`, so the first real dispatch uses
the warm, never-used thread WITHOUT a rotation — fresh context, zero extra latency. The
warmup path needs no change.

### D7 — Affirmative health marking, grok D3a mirror (R2 — resolves OQ3)

Codex's `healthy` is optimistic: set True at init, flipped False only on observed hard
failure, never re-affirmed. That model was calibrated for the kill-every-turn world; the
r1 panel showed it is unsafe to carry into warm reuse — `codex.py:375` computes
`ok = status not in {"errored", "failed"}`, so an externally-cancelled turn reports
`status="interrupted"`, `ok=True`, `healthy=True` (probe-verified real protocol behavior,
codex probe run-C), and the pool would re-idle the process with all its process-lifetime
state — precisely what retirement used to shed.

v1.1 adopts grok's D3a affirmative marking, unconditionally (same semantics under
retire=1, where an unhealthy engine and a retiring engine take the identical
`release()`→stop path, so fleet behavior today is unchanged):

- `run_turn_with_progress` entry: `self.healthy = False`, `self._interrupted = False`.
- `interrupt()` additionally sets `self._interrupted = True`.
- `healthy` flips back True at exactly ONE place: a received `turn/completed` whose
  status **== "completed"** (ALLOWLIST, v1.2 F1 — a missing (`None`) or unknown status
  is UNCLEAN and logs loudly: `[codex] non-clean terminal status=%r — quarantining warm
  process`) AND `self._interrupted` is False AND the deny budget did not exhaust the
  turn. `TurnResult.ok` keeps its existing blocklist semantics untouched — only process
  REUSE uses the allowlist.
- Every other terminal (timeout, process exit, raise, interrupted, errored, deny-exhaust)
  leaves `healthy = False` → `release()` stops the process → next dispatch cold-spawns.

`TurnResult.ok` semantics are NOT changed by this design (interrupted still reports as
it does today to the caller); only process REUSE is gated. An unclean turn costs one
cold spawn — the DSP-1 retry + init-budget raise cover that path, and unclean turns are
rare by construction.

### D8 — What rotation does NOT clear: the process-level surface, named (R3)

The contamination guarantee this design preserves is **thread-level** (conversational
context, per-thread token accumulation — the 22M-token incident class). A warm process
retains, across rotations, state that retirement cleared every turn:

- the `arb-memory-local` MCP child and its connection/session state (spawned via the
  config override at `Popen` time);
- environment and config **frozen at spawn** — an MCP token/URL rotation or env-file
  change is not picked up until the process recycles (retire=ON re-read env every
  dispatch; warm seats re-read only at cap/quarantine recycle — operational note for
  ARB redeploys: `kickstart -k` the seat, which was already true for daemon-level env);
- the models-cache/models-refresh machinery (the 185s churn loop observed in warm
  app-servers, luna stderr 2026-07-10);
- background processes spawned by tool calls in earlier dispatches (ports, daemons) —
  they outlive their thread (r1 agy).

These are bounded, not eliminated, by D9's cap; they are the reason the cap is REQUIRED
rather than optional. Continuous per-process degradation alarming beyond the cap is a
NAMED accepted residual of v1 (the visibility plane logs per-turn events; the cap bounds
the exposure window to ≤ `MAX_PROCESS_TURNS` dispatches).

### D9 — Max-turns-per-process cap, REQUIRED in v1 (R3 — resolves OQ2)

`BRIDGE_CODEX_MAX_PROCESS_TURNS` (default **20**; `0` = unlimited, discouraged). 
Mechanism — zero pool change: `retire_after_turn` becomes a **property**:

```python
@property
def retire_after_turn(self) -> bool:
    if self._retire_after_turn_env:          # today's flag, unchanged default ON
        return True
    cap = self._max_process_turns
    return cap > 0 and self._process_turns >= cap
```

**REQUIRED constructor change (v1.2 F2):** `codex.py:121` currently ASSIGNS
`self.retire_after_turn = ...` — with a read-only property that raises
`AttributeError` at construction for every seat. The `__init__` write becomes
`self._retire_after_turn_env = str(raw_retire).lower() not in {"0", "false"}` (no
setter is provided — the property is deliberately read-only). Sweep result (r2, agy +
cold-Opus): line 121 is the ONLY breaking write site; the pool reads via `getattr`,
rotation reads the property. G1 pins a construction test for both flag states.

The pool already reads `getattr(engine, "retire_after_turn", False)` dynamically at
`release()` — a capped engine simply retires itself on the release after its Nth turn.
`_process_turns` increments alongside `_thread_turns` (D2, before the send).
**Cap-turn behavior (precision note, final-review GLM P2):** at the cap turn's rotation
check `_process_turns` is still `cap-1`, so the cap turn DOES rotate to a fresh thread,
serves it, and then retires at release — contamination guarantee preserved either way;
the extra `thread/start` is a metadata RPC. Deliberately left as-is.

### D10 — supports_continuation tripwire (R5)

If codex ever sets `supports_continuation = True`, `drive_to_completion` re-prompts the
SAME engine via bare `run_engine` calls (no resume/fork between attempts,
`bridge.py:~1448`) — turn 2 of the loop would see `_thread_turns ≥ 1`, rotate, and
destroy the dispatch's own accumulated context mid-flight. Grok has the same latent gap
(pattern defect inherited from the template, not introduced here). v1 does NOT make
rotation continuation-aware (speculative); instead: **CodexEngine declares
`supports_continuation = False` explicitly as a class attribute (v1.2 F3 — the class
currently has no such attribute; the bridge's `getattr(..., False)` hides that, and the
tripwire as originally written would `AttributeError`)**, and G1 pins the tripwire
`CodexEngine.supports_continuation is False` with a comment stating the enable-path
requirement — reset `_thread_turns` per continuation attempt (treat the dispatch as one
logical thread-session) or assert rotation-incompatibility. G4's deny-proof includes
the tripwire.

## What this does NOT change (non-goals)

- pi-sdk rotation (ENG-1b: needs a host.mjs dispose-and-replace protocol change + the
  wedge-scar RSS proof), agent-sdk (retire is SDK-session-level, no material spawn),
  agy-print (spawn-per-turn by design), gemini (dead).
- Idle-TTL retirement — still out of scope (the D9 max-turns cap is v1's process-lifetime
  bound; an idle-TTL would additionally reclaim RAM on quiet seats and can be a follow-on).
- ~~codex `healthy` stays optimistic~~ — REVERSED in v1.1: D7 adopts grok's affirmative
  D3a marking (r1 P1, three seats).
- The DSP-1 residual (init budget 15s→60s) — separate small change; still wanted, since
  rotation does not eliminate daemon-start / post-quarantine cold spawns.

## Gates (all must pass before any seat flips to retire=0)

- **G1 unit (TDD):** rotation fires on second turn of a non-retiring engine; does NOT
  fire when retiring, on a fresh thread, after explicit resume/fork (the D2 hazard,
  pinned as a test), or after `--fresh-context` reset; quarantine path (`thread/start`
  error → healthy False + raise); thread_id unchanged on failed rotation. **v1.1
  additions (R1/R2/R4/R5):** counter already incremented when `turn/start` raises
  post-send (response-lost → next dispatch rotates); interrupted terminal →
  `is_healthy()` False (and same for timeout, errored, deny-exhaust); clean terminal →
  True; `start_thread` rejects empty-string thread id; tripwire
  `supports_continuation is False` (D10); cap property flips `retire_after_turn` True at
  `MAX_PROCESS_TURNS` (D9).
- **G2 contamination probe (live, GROK run-H analogue):** on a retire=0 seat, dispatch A
  plants a codeword; dispatch B (no thread-id) asks for it. B must NOT know it. Run the
  probe decorrelated across modes per [[cross-mode-decorrelation-empirical-check]]:
  plant via prose AND via a file-free tool interaction; ask via direct question AND via
  "summarize everything you know from this conversation". **R6: the plant must be purely
  conversational — never stored via the arb-memory MCP or the filesystem, which are
  process-/host-lifetime state a correct rotation would legitimately still see; a probe
  that plants there measures the wrong layer and would red-herring the gate.**
- **G3 RSS gate (live):** ≥50 rotation dispatches on one process; RSS sampled every 10;
  bounded (no monotonic growth trend). If it grows: max-turns cap remediation (OQ2).
- **G4 deny-proof:** comment out the `_rotate_thread_if_reused()` call → G1's
  fires-on-reuse test AND the G2 probe must go red (the guard is load-bearing, not
  vacuous).
- **G5 latency evidence:** measure dispatch→turn-start on the retire=0 seat vs a
  retire=1 control across ≥10 dispatches; expect multi-second improvement (this is the
  point of the change — record the number).

## Open questions — ALL RESOLVED in v1.1 (round 1)

- **OQ1 → RESOLVED (R1):** increment at turn START, before the `turn/start` send —
  normative in D2. The r1 panel (terra, cold-Opus, agy §sound-2) unanimously converged
  here; END-increment is contamination-unsafe on post-send raise paths.
- **OQ2 → RESOLVED (R3):** the cap is REQUIRED in v1 (D9), default 20. It is the only
  process-level backstop in the design's vocabulary and bounds every D8 surface.
- **OQ3 → RESOLVED (R2):** unclean turns quarantine the process, full stop (D7
  affirmative marking). The draft position ("rotation adds no new reuse of a dirty TURN,
  only of a process") was rejected by three seats: the process IS the surface —
  `codex.py:375` makes interrupted turns look healthy, and rotation cannot clear
  process-lifetime state (D8).

## Evidence trail

DSP-1 probes + occurrence forensics: `docs/BACKLOG.md § DSP-1` (root-cause section).
GROK-1 rotation: `docs/superpowers/specs/2026-07-10-grok1-acp-permission-handling-design.md`
(D3b), probe run H, live gate V5. Retire family:
`docs/superpowers/specs/2026-07-10-grok-retire-after-turn-design.md` (the accepted
cold-start residual this design revisits), [[pi-sdk-glm-wedge-root-cause]] (why retire
shipped first), `engines/codex.py:112-121` (retire rationale comment, 22M-token incident).

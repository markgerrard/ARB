# ENG-1b: pi-sdk warm-engine thread rotation (design)

**Version: v1.2 — DESIGN CLOSED (fold-and-proceed after round 2)**

> **v1.2 changelog** (r2 run `panel-eng1bdesign-r2-20260711T120322Z-c5d52b`: terra
> needs-changes/P1, agy needs-changes/P1, cold-Opus needs-changes/P1 — 3/3 convergent on
> ONE new defect, an interaction between two v1.1 remediations; GLM timed out mid-review
> on a runaway root-level `find` of its own — recorded, non-certifying seat, its r1
> length-catch already shaped R1. R1–R4/R6 confirmed closed by all three reporting seats):
> - **F1 (P1, 3/3): R5's quarantine was erased by R6's flip.** `oldDisposed:false` is a
>   SUCCESSFUL rotate reply (no raise), the turn runs, and D-B4's clean terminal
>   unconditionally set `healthy=True` — re-enabling the leaked process. NOW: the engine
>   sets a STICKY `self._quarantine_after_turn = True` on `oldDisposed:false`, and the
>   D-B4 flip site requires `not self._quarantine_after_turn`. The latch is reset only
>   in `start()` (a fresh process). G1 pins: rotate-with-failed-dispose then a CLEAN turn
>   → `healthy` stays False → pool evicts at release.
> - **F2 (P2, cold-Opus): pi's `interrupt()` must SET the `_interrupted` latch** —
>   unlike codex (which ENG-1 T2 patched), pi_sdk.interrupt() does not set it today and
>   v1.1 never said to; without this the D-B4 `not self._interrupted` term is dead code.
>   D-B3 now names the one-line interrupt() addition explicitly.
> - **F3 (P2, cold-Opus): R2 consume ordering pinned as capture-into-local** — entry
>   captures `affinity = self._turn_affinity_requested`, THEN resets the attr to False,
>   THEN passes the captured value to the rotation guard. (The v1.1 prose read literally
>   would have the guard always see False — re-creating the D-B5 bug.)
> - **F4 (P2, cold-Opus): `supports_continuation = False` tripwire carried into D-B3**
>   (ENG-1 D10 parity — pi engine declares it explicitly; G1 pins it).
> - agy recommendation adopted into D-B1: `doThreadStart` stores `authStorage`/
>   `modelRegistry` on `state.thread` for replay REUSE (cold-Opus verified reuse safe;
>   only `SessionManager.inMemory` must be fresh).

> **v1.1 changelog** (r1: terra, agy, GLM, cold-Opus — all needs-changes/P1, fully
> convergent):
> - **R1 (P1, 4/4):** D-B4 was a negative match — and GLM's own-harness read showed
>   `"length"` (context overflow, THE wedge signature) passes it, host `ok` is itself
>   lossy for `"length"`, and the SDK's EventStream partition + the project's own
>   smoke harness pin clean as `{stop, toolUse}`. D-B4 is now an ENGINE-SIDE positive
>   allowlist independent of host `ok`.
> - **R2 (P1, 4/4):** the acquire-time `on_affinity_acquire` hook leaked: a dispatch
>   dying between acquire and turn returned an engine to idle with a zeroed counter and
>   stale context. Replaced with a bridge-set PER-DISPATCH flag (always set-or-cleared,
>   mirroring `apply_reasoning_effort_if_requested`'s pattern) — no staleness window,
>   and the pool change is DELETED entirely. OQ3 dissolves.
> - **R3 (P1/P2, 4/4):** host `thread/rotate` gains a `rotateInFlight` latch
>   (`threadInitInFlight` parity — the check-then-await race terra walked at
>   host.mjs:729/251); `turn/start` and `thread/start` also reject while it is set.
> - **R4 (P2, cold-Opus + GLM):** the rotate replay must RECONSTRUCT the role-profile
>   `resourceLoader` from the stored `appendSystemPrompt` (byte-parity with
>   `doThreadStart`) — else rotated sessions silently lose seat identity.
> - **R5 (P2, agy; OQ2 resolved):** dispose-throw = serve-then-quarantine: the swap
>   completes (fresh context is delivered), the reply carries `oldDisposed:false`, and
>   the engine leaves `healthy=False` so the process dies at release. Fail-closed at
>   process granularity, zero wasted turn.
> - **R6 (cold-Opus plan-pin):** the `healthy=True` flip site is pinned in the engine
>   pseudocode (D-B4).
> - OQ1 resolved as drafted (verbatim params replay — agy verified sound).
**Author: warm-Opus orchestrator (inline; Anthropic lineage → cold-Opus non-certifying).**
**Quorum note (this stage and all ENG-1b panels): pi-GLM reviews NON-certifying on its own
engine harness (grok-precedent rule) — certify = codex pin (terra) + agy-print; GLM and
cold-Opus fully admissible.**
**Sibling: ENG-1 codex rotation, SHIPPED + live-gated same day (dev `805736b`, design v1.2)
— this design mirrors it and only documents the pi-specific deltas in full.**

## Problem

Same economics as ENG-1 (DSP-1 root cause): retire-after-turn — pi's 2026-07-09 wedge fix,
[[pi-sdk-glm-wedge-root-cause]] — kills the host process per dispatch, so every dispatch
pays a fresh `node host.mjs` spawn (30MB node_modules cold-load; one of the two engines
that hit the engine-start-failed 15s tail). `BRIDGE_PI_RETIRE_AFTER_TURN=0` exists but is
UNSAFE today for the ORIGINAL wedge reason: host.mjs keeps ONE in-memory session per
process and reuse accumulates context (measured 8.4k→58.8k tokens over 8 turns; the
overnight wedge cycle; pure quota waste on self-contained dispatches).

## Verified primitives (all checked 2026-07-11)

- **The SDK has first-class fresh-context-on-live-process:** `AgentSession.dispose(): void`
  (`dist/core/agent-session.d.ts:260`), `SessionManager.newSession(...)`
  (`session-manager.d.ts:181`), and the interactive precedent
  `ctx.waitForIdle()` + `ctx.newSession({parentSession})` — example-org/PiExtensions
  `extensions/clear.ts` ("fresh empty context linked to the current session", live in
  Mark's interactive pi). Headless rotation is the same operation composed manually:
  dispose the old session, `createAgentSession` a new one.
- **Session and MCP bridge are separable:** `startSessionWithBridge` (host.mjs:282)
  builds `toolArgs` (bridge + customTools) THEN calls `createAgentSession` with them —
  the bridge object and its customTools closures are reusable for a second session.
- **Per-turn subscription:** host subscribes to session events per turn
  (`state.thread.session.subscribe(handler)` inside the turn, host.mjs:540) and
  unsubscribes at turn end — between turns there is no live subscription to migrate.
- **host.mjs is single-thread-per-process by DESIGN** (`state.thread` singleton; second
  `thread/start` → `ERR_BAD_STATE "thread already started"`, host.mjs:255) — rotation is
  a new protocol verb, not a semantics change to `thread/start`.
- **pi_sdk.py binds `logger` (lowercase, pi_sdk.py:44)** — noted explicitly because the
  ENG-1 plan shipped a 4/4-convergent defect by transcribing grok's `logger` into codex's
  `LOGGER` module. The engines genuinely differ; the fixture smoke and plan must carry
  the right name per file.

## Design

### D-B1 — Host protocol: new `thread/rotate` verb

`{id, method: "thread/rotate", params: {threadId}}` →
`{id, result: {thread: {id: <new th_ id>}}}`.

Guards (all ERR_BAD_STATE on violation — fail-closed, no partial state):
1. `state.thread` exists and `params.threadId === state.thread.id`;
2. `state.activeTurn === null` (the headless `waitForIdle` equivalent — the engine only
   rotates between turns, but the host enforces it regardless);
3. **`state.rotateInFlight === false` (R3).** `thread/rotate` sets the latch
   synchronously BEFORE its first await and clears it in a `finally`;
   `turn/start` and `thread/start` additionally reject with ERR_BAD_STATE while the
   latch is set. This is `threadInitInFlight` parity: host stdio handlers interleave on
   the microtask queue (host.mjs:729), so a pipelined `turn/start` landing inside the
   rotation's `await createAgentSession` window would otherwise start a turn on the
   old session and have it disposed mid-turn.

Mechanism: `state.thread.params` (NEW: `doThreadStart` retains the resolved thread
parameters — modelSpec, cwd, tools, mcpServers, thinkingLevel, appendSystemPrompt) is
replayed into a fresh `createAgentSession` with a **fresh `SessionManager.inMemory(cwd)`**
and the **EXISTING toolArgs** (bridge kept — D-B2). **R4: when `appendSystemPrompt` is
set, the replay RECONSTRUCTS the `DefaultResourceLoader` exactly as `doThreadStart` does
(same override closure + `await reload()`) — omitting it would silently strip the seat's
role profile from every rotated session.** On success, `state.thread.session.dispose()`
the OLD session, swap `state.thread` (new `th_` id, same params, same bridge), reply
`{thread: {id}, oldDisposed: true}`. **R5 (dispose-throw, OQ2 resolved):** a `dispose()`
throw is caught and logged, the swap still completes, and the reply carries
`oldDisposed: false` — the ENGINE then serves the current dispatch on the fresh session
but leaves `healthy = False`, so the pool stops the process at release
(serve-then-quarantine: fail-closed at process granularity, zero wasted turn, the
un-freeable session's leak bounded to one turn). On session-CREATE failure: reply error,
state unchanged (old session still installed; the engine quarantines).

### D-B2 — MCP bridge persists across rotation (named residual)

Re-spawning MCP servers per dispatch would re-add spawn tax. The bridge and its child
processes are process-lifetime state rotation does not clear — exactly ENG-1's D8 class
(MCP child, env frozen at spawn) — bounded by the D-B3 cap, not eliminated. G2's plant
must therefore be conversational, never via arb-memory MCP (ENG-1 R6 rule carries over).

### D-B3 — Engine mirror (pi_sdk.py), codex-shape

Identical structure to ENG-1 D2/D4/D9, transcribed for pi:
- `retire_after_turn` becomes a read-only property over `_retire_after_turn_env`
  (constructor rename at pi_sdk.py:152-155 — the ENG-1 F2 lesson, named up front) OR the
  `BRIDGE_PI_MAX_PROCESS_TURNS` cap (default 20, 0=unlimited).
- `_thread_turns` (reset on any thread install) / `_process_turns` / `_interrupted`;
  increments BEFORE the `turn/start` request (R1 normative, verbatim rationale).
- `_rotate_thread_if_reused()`: guard `retire_after_turn or _thread_turns == 0`; calls
  `thread/rotate`; on success updates `self.thread_id` from the reply; on ANY error:
  `healthy = False` + raise `EngineError("thread rotation failed; engine quarantined: …")`.
  `self.thread_id` flips only on a successful reply.
- pi has ONE thread-install path (`start()`'s `thread/start`, pi_sdk.py:294 — no
  resume/fork), plus the rotation itself; both set `_thread_turns = 0`.

### D-B4 — Affirmative health, engine-side POSITIVE allowlist (revised R1, 4/4 P1)

Entry to `run_turn_with_progress`: `healthy = False`, `_interrupted = False` (pi's
existing `healthy` is optimistic — set True at start, pi_sdk.py:142, flipped False only on
wedge/exit paths). The ONLY re-affirmation site, pinned (R6):

```python
# in the turn/completed handler, AFTER TurnResult fields are extracted (ok unchanged):
if (
    completed.get("ok") is True
    and not completed.get("error")
    and completed.get("stopReason") in {"stop", "toolUse"}   # POSITIVE allowlist
    and not self._interrupted
    and not self._quarantine_after_turn   # v1.2 F1: sticky dispose-failure latch wins
):
    self.healthy = True   # the ONLY flip site
else:
    logger.warning(   # pi_sdk binds lowercase `logger` (pi_sdk.py:44) — NOT codex's LOGGER
        f"[pi-sdk] non-clean terminal ok={completed.get('ok')!r} "
        f"stopReason={completed.get('stopReason')!r} interrupted={self._interrupted} "
        f"dispose_failed={self._quarantine_after_turn} — quarantining warm process"
    )
```

v1.2 F1: `_quarantine_after_turn` is set by `_rotate_thread_if_reused` when the rotate
reply carries `oldDisposed: false`, is STICKY for the process lifetime (reset only in
`start()`), and gates the flip above — a clean turn after a failed dispose still evicts
the process at release. v1.2 F2: `interrupt()` gains `self._interrupted = True`
immediately after sending `turn/abort` (pi lacks this today — without it the
`not self._interrupted` term is dead code). v1.2 F3: the R2 flag is consumed
capture-into-local at entry — `affinity = self._turn_affinity_requested;
self._turn_affinity_requested = False; self._rotate_thread_if_reused(affinity_requested=affinity)`
— NOT read inside the rotation guard after the reset. v1.2 F4: `PiSdkEngine` declares
`supports_continuation = False` explicitly (ENG-1 D10 parity; G1 tripwire).

Why the allowlist is engine-side and independent of host `ok` (GLM, own-harness): the
SDK's `StopReason` is `{stop, length, toolUse, error, aborted}`; the host's `completeTurn`
emits `ok=true` for anything not aborted/error — so `"length"` (context overflow, the
LITERAL wedge signature) rides through host `ok`. The SDK's EventStream partition and the
project's own smoke harness (`smoke_protocol.py:205`) both pin clean as `{stop, toolUse}`.
`"length"`, `null`/missing, and every future enum member quarantine. `TurnResult.ok`
semantics unchanged.

### D-B5 — THE pi-specific hazard: affinity-requested threads must not rotate

pi is resume-less, so explicit `--thread-id` continuations ride POOL AFFINITY
(`bridge.py:923` passes thread_id to `acquire()` for non-resume engines;
`engine_pool.py:56` matches on the engine's live `thread_id`). Lazy rotation would
destroy exactly the thread affinity just delivered: affinity match → `run_turn` →
`_rotate_thread_if_reused` fires (`_thread_turns ≥ 1`) → the requested context is gone
and the "continuation" runs fresh, SILENTLY.

Fix (revised R2 — the r1 panel killed the acquire-time hook 4/4: a dispatch dying
between acquire and turn returned the engine to idle with a zeroed counter and stale
context, silently disabling rotation for the NEXT dispatch — the protected class
itself). v1.1 mechanism — a bridge-set PER-DISPATCH flag, mirroring the always-set
pattern `apply_reasoning_effort_if_requested` already uses (bridge.py:1971-1983: set
every dispatch, cleared when absent, so nothing persists across dispatches):

- bridge, immediately alongside the effort setter (once per dispatch, every dispatch):
  ```python
  setter = getattr(engine, "set_turn_thread_affinity", None)
  if callable(setter):
      setter(thread_id is not None and not self.engine_supports_resume)
  ```
- engine: `set_turn_thread_affinity(flag)` stores `self._turn_affinity_requested`;
  `_rotate_thread_if_reused` skips rotation iff the flag is True, and
  `run_turn_with_progress`'s entry CONSUMES it (reads then resets to False) — so the
  flag can never outlive the dispatch that set it, including dispatches that die before
  the turn (the next dispatch's setter overwrites it regardless).

No pool change at all (v1.0's engine_pool.py touch is DELETED; OQ3 dissolves). The
bridge touch is two lines in the one method that already does per-dispatch engine
priming, guarded by getattr so every other engine is untouched.

**Discovered sibling defect (NOT fixed here, filed):** grok's shipped
`_rotate_session_if_reused` has the same latent hole — `session_id` is
affinity-matchable (engine_pool.py:56) and rotation fires unconditionally on reuse, so a
future OPT-OUT grok seat would silently rotate away an affinity-delivered session.
Latent today (zero opt-out grok seats; V5b already gates any standup). BACKLOG note
under GROK-1 residuals; the same `on_affinity_acquire` hook is the fix when wanted.

### D-B6 — Warmup, cwd, effort

Warmup engine (`pool.release("__warmup__")`) survives under retire=0 with
`_thread_turns == 0` → first dispatch uses the warm never-used thread, no rotation
(ENG-1 D6, unchanged). Pooled pi engines keep the seat's fixed cwd (worktree dispatches
use dedicated engines outside the pool). pi has no per-turn effort override surface —
no effort interplay.

## What this does NOT change (non-goals)

- grok's latent affinity-rotation hole (filed, above); agent-sdk; idle-TTL retirement.
- host.mjs `thread/start` semantics, the turn protocol, event forwarding — untouched.
- The wedge fix's B/C arms (auto-retry event forwarding, event logs) — untouched.

## Gates

- **G1 unit:** engine — property/cap/counters/quarantine/dirty-before-send/affinity-flag
  (set_turn_thread_affinity True → rotation skipped ONCE and flag consumed; flag False →
  rotation fires on reuse; a flag set by a dispatch that never runs a turn is overwritten
  by the next dispatch's setter — the R2 leak test), the D-B4 allowlist (stop/toolUse
  clean; length/null/unknown/interrupted quarantine), mirrors of the ENG-1 suite with pi
  shapes. Bridge — the two-line setter fires on every dispatch path (affinity and not).
  Host (node, `tools/pi-sdk-host/*.test.mjs` pattern; CI needs `npm install --no-save` of
  BOTH pinned @earendil-works pkgs) — `thread/rotate`: happy path (new id, old disposed,
  bridge object preserved, resourceLoader reconstructed when appendSystemPrompt set),
  guard rejections (no thread, wrong id, active turn, rotateInFlight), pipelined
  turn/start during rotation rejected (R3), create-fails path (old session still
  installed), dispose-throws path (swap completes, `oldDisposed:false`).
- **G2 contamination (live, GLM seat):** ENG-1 shape — prose + tool plants, direct +
  summarize asks, plant NEVER via MCP (D-B2).
- **G3 RSS (live, MANDATORY — the wedge scar):** ≥50 rotation dispatches; sample the
  HOST node process RSS (the accumulation lived in host memory, not a model child);
  bounded, no trend. Also assert per-turn request context stays flat via the host event
  log's token counts (the wedge's original 8.4k→58.8k signature must NOT reappear).
- **G4 deny-proofs:** remove the rotation call → G1 fires-on-reuse + G2 red; falsify the
  D-B4 allowlist back to the v1.0 denylist → the length/unknown quarantine tests red;
  remove the flag consumption → the affinity-preservation test red; remove the
  rotateInFlight latch → the pipelined-turn/start rejection test red.
- **G5 latency:** warm median flat over the loop; only post-cap spawns elevated
  (ENG-1's within-run A/B pattern).
- **Plan pre-flight:** the ENG-1b plan MUST ship with `python fixture-smoke` blocks
  (fixture satisfiability incl. the strongest health predicates on process-less fakes,
  and red-claims per task) — `scripts/plan-fixture-smoke` at every dispatch boundary,
  per the operating-manual rule this arc created.

## Open questions — ALL RESOLVED in v1.1 (round 1)

- **OQ1 → RESOLVED as drafted:** rotation replays the ORIGINAL params verbatim (plus the
  R4 resourceLoader reconstruction); reconfiguration = retire+restart. agy verified sound.
- **OQ2 → RESOLVED (R5):** dispose-throw = serve-then-quarantine (swap completes,
  `oldDisposed:false` in the reply, engine leaves `healthy=False`, process dies at
  release). The fail-open corner is closed at process granularity.
- **OQ3 → DISSOLVED (R2):** no pool hook exists anymore; the bridge-set per-dispatch
  flag replaced it and the pool is untouched.

## Evidence trail

ENG-1 design v1.2 + live gates (dev `805736b`, BACKLOG § ENG-1); wedge root cause
[[pi-sdk-glm-wedge-root-cause]] (the RSS gate's reason); PiExtensions `clear.ts` (the
SDK precedent); host.mjs:255/282/375/540; agent-session.d.ts:260; engine_pool.py:53-59;
bridge.py:923; pi_sdk.py:44/140-157/294.

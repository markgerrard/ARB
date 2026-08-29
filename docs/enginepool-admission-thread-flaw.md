# EnginePool admission-thread blocking — latent flaw, deferred fix

**Status:** DIAGNOSED 2026-06-18, fix DEFERRED to its own panel→codex→gate build. Do NOT bolt onto an
unrelated session. This is general architectural hardening, not an agent-sdk feature.

## The flaw (general, pre-existing)
`EnginePool.acquire` (`src/agent_redis_bridge/engine_pool.py`) lazily builds **and `start()`s** a new
engine **synchronously, while holding `self._lock`, on the single inbox-admission thread**
(`engine_pool.py:86-94`). `handle_raw` calls `pool.acquire()` before spawning the per-turn worker thread.
So the entire admission loop — popping and dispatching the *next* envelope — blocks for the full duration
of an engine's `start()`. `start_engine` warms only ONE engine, so the first dispatch reuses the warm
engine cheaply, but the second dispatch (needing a 2nd engine) stalls admission for the whole startup.

This is **not** an agent-sdk bug. It has always been in the pool. **codex masks it** because its `start()`
is a sub-second local JSON-RPC `initialize` handshake (`codex.py:42`, no model call). **agent-sdk revealed
it** by being the first engine with an expensive `start()`: `client.connect()` (≤30s) + the gate + a
~60s live smoke test on the primary-cwd engine (`agent_sdk.py:96-105`). Any future engine with a
non-trivial warmup (another model seat, anything that connects/authenticates slowly) hits the same wall.
Fleet-by-default (N single-parallel seats) sidesteps it for agent-sdk but **leaves the landmine armed**.

## Live evidence (the discriminating test, not just convergence)
Fresh `--max-parallel 2` agent-sdk seat, two trusted worktree mutations fired simultaneously. The first
(B) ran to completion; the second (A) was BLMOVE'd into the Redis `processing` list and **never
admitted/logged** (no `[inbox]`, no `[turn-start]`, no worktree), staying wedged ~8.5 min until the
dispatcher timed out — exactly what `start()`-under-lock-on-the-inbox-thread predicts, and what would NOT
happen if the cause were elsewhere. (DC/escaped-defect material: the existing parallel tests
`tests/test_bridge_parallelism.py` missed this because their fake engine's `start()` is instant — see the
"fake cheaper than reality" class lesson.)

## Compounding issues (fold into the same fix)
1. **Double-engine waste on worktree dispatches:** `process_request` (`bridge.py:656-664`) builds + starts
   a FRESH worktree engine for the turn, so the pooled engine acquired as the slot token is never used —
   yet its slow `start()` (+ the primary-cwd smoke test) is paid on the admission path for nothing.
2. **Connect-time isolation:** addressed separately (per-engine `CLAUDE_CONFIG_DIR`, branch
   `fix/agent-sdk-config-dir-isolation`) — the shared `~/.claude` was the credible connect-race trigger.

## Panel verdict (3/3 — all seats in; root cause unanimous)
- **codex:** FIXABLE-WITH-CHANGES. Fix: don't use a fully-started pooled engine as the worktree slot;
  reserve a lightweight slot at admission and build/start the real worktree engine on the worker thread,
  off the inbox thread. A two-phase reserve/start EnginePool API is the general alternative.
- **cold-Opus:** FIXABLE-WITH-CHANGES, but recommends fleet-by-default for agent-sdk *concurrency need*;
  treat the off-thread-start as separate general hardening.
- **agy:** INTRA-SEAT-NOT-VIABLE-USE-FLEET (delivered on re-dispatch 2026-06-18; its first attempt died on
  a provider timeout). Same root cause at file:line. Distinctive fix-direction argument: **intra-seat
  parallelism yields NO resource savings for agent-sdk** — each turn spawns a heavy `claude` subprocess +
  model stream regardless, so N intra-seat turns ≈ N subprocesses anyway; fleet (N single-parallel seats,
  Redis load-balanced) gives the same concurrency with complete process/fs/db isolation and no new code.

**Resolution.** All three agree the root cause. On *direction*, the consensus is **fleet-by-default for
agent-sdk concurrency** (cold-Opus + agy explicitly; codex's patch is compatible). The off-thread-start
fix below is therefore framed as **general EnginePool hardening** — worth doing for a *future* cheap-/
moderate-start engine that genuinely benefits from intra-seat — and NOT as the mechanism for parallelising
agent-sdk (which is fleet). Per agy's no-resource-savings point, do not justify the fix by agent-sdk
burst-absorption alone.

## Proposed fix (to be panelled, not yet built)
- Admission reserves a lightweight permit (bounded by `max_parallel`) WITHOUT starting an engine; the
  worker thread builds/starts the engine. For worktree dispatches, skip the pooled token engine entirely.
- For default (non-worktree) dispatches, the pooled engine IS the real engine — keep pool semantics but
  move the lazy `start()` off the admission thread (or pre-warm the full pool at boot).
- **Regression test MUST use a slow-`start()` fake engine** (the current instant-start fake is why this
  escaped): push two reliable-inbox messages, assert the 2nd `[inbox]` is NOT delayed behind the 1st
  engine's start, and that two workers admit concurrently.

## Why deferred, not now
The diagnosis is satisfying, which is exactly when not to rush. This touches the success-signal/admission
path and is general — it deserves the full panel (incl. agy's owed vote) → codex TDD → completion-gate
pipeline, like the T8 fixes, on its own. Tonight's goal (parallel agent-sdk work) is met by fleet-by-
default + the config-dir isolation; this fix is decoupled from that.

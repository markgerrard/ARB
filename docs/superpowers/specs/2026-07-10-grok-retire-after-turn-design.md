# Grok-acp retire-after-turn — design/spec

Date: 2026-07-10 · Workflow A (Mark: "fix the grok-acp accumulation the same way") · Author: Fable fork (inline)
Status: spec — fourth engine in the session-accumulation family (pi `9837803`, codex `2a1f198`, agent-sdk `375d37a`)

## Problem

`GrokAcpEngine` creates ONE ACP session at `start()` (`session/new`, grok_acp.py:113) and every
`run_turn_with_progress` prompts the same `self.session_id` (grok_acp.py:151). It never sets
`retire_after_turn`, so `engine_pool.release()` (engine_pool.py:132) recycles the engine and the
session accumulates across unrelated dispatches.

**Live-proven 2026-07-10** (bake-off panel): a self-contained contamination probe dispatched to
`grok-bridge-dev` AFTER its review turn recalled the review brief's exact title
("Review brief — Session-accumulation fix family …") — cross-dispatch contamination, same class
as the codex 24-dispatch thread and the sonnet wiki-gate 15-dispatch session.

## Fix (mirror of the certified pattern)

In `GrokAcpEngine.__init__`:

```python
raw_retire = os.environ.get("BRIDGE_GROK_RETIRE_AFTER_TURN")
self.retire_after_turn = str(raw_retire).lower() not in {"0", "false"}
```

- Default ON: the pool stops the engine after every dispatch (`stop()` exists, grok_acp.py:253);
  the next dispatch gets a fresh process + fresh ACP session. No `engine_pool.py` changes.
- Opt-out `BRIDGE_GROK_RETIRE_AFTER_TURN=0`/`false` (case-insensitive) — must be set in the
  launchd plist, not the seat env file.

## Continuation surface: none, and that stays legible

grok-acp has NO `resume_thread`/`fork_thread`, so `engine_supports_resume=False` and a
`--thread-id` dispatch goes through POOL AFFINITY at acquire (bridge.py:901-908): it binds to
the live engine holding that session, or fails as **`thread-affinity-miss`**. With retirement
no engine survives to match, so every explicit continuation fails loud — correct, since grok
cannot reconstruct a dead session. (Panel correction by the grok contributor seat, P2: an
earlier draft wrongly named the failure token `thread-continuation-unsupported`; for
resume-less engines the affinity check at acquire fires first.) Replies keep returning
`thread_id` (the session id) for observability only.

## Accepted residuals (same class as the sibling fixes)

- Warmup engine retired immediately after release; per-dispatch cold start = grok CLI spawn +
  ACP initialize + session/new (a few seconds).
- Known benign per-start stderr noise ("worker quit … Auth(AuthorizationRequired)") now appears
  once per dispatch instead of once per daemon — noise only, recorded in memory.

## Out of scope

- The out-of-cwd write permission bug found today (ACP permission RPC -32603 →
  `stopReason=cancelled`) — separate defect, workaround documented (grok returns reports inline).

## Tests (mirror `tests/test_codex_retire.py`, new file `tests/test_grok_retire.py`)

1. default (env unset) → `retire_after_turn is True`
2. `BRIDGE_GROK_RETIRE_AFTER_TURN=0` → `False`
3. `"False"` (case-insensitive) → `False`

Engine constructs cheaply: `GrokAcpEngine(cwd="/tmp", model=None)` — no process spawn in
`__init__`.

## Live gate (post-deploy, seat `grok-bridge-dev`)

1. Real multi-tool READ probe (two file reads + structured reply) → ok.
2. Contamination pair: nonce dispatch → self-contained "any nonce/brief earlier?" dispatch →
   exactly `NONE`, distinct `thread_id`s.

## Deployment

Merge dev → push (after the in-flight e2e suite finishes green) → fleet clone pull → restart
ONLY `com.example.arbseat.grok-bridge-dev` after an idle-check of its log.

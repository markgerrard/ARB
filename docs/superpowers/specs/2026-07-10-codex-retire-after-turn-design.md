# Codex retire-after-turn — design/spec

Date: 2026-07-10 · Workflow A (confirmed with Mark) · Author: warm orchestrator (inline, confirmed)
Status: spec — mirrors the panel-certified pi-sdk fix (merged dev `9837803`, engine-pool retire hook `672fef9`)

## Problem

The codex engine shares the pi-sdk session-accumulation defect, triaged CONFIRMED 2026-07-10
(full record: local memory `pi-sdk-glm-wedge-root-cause.md`, ARB Memory `art-adecf10f14f1892b`):

- `engines/codex.py` starts ONE thread at engine start (`start_thread()`, codex.py:149) and every
  turn rides it (`turn_params` uses `self.thread_id`, codex.py:182).
- `CodexEngine` never sets `retire_after_turn`, so `engine_pool.release()` (engine_pool.py:132)
  recycles the engine; seats run `max_parallel=1`, so one ever-growing thread serves the seat.
- Empirical: one thread on 2026-07-08 served **24 unrelated dispatches** (pings, learn evals, ~18
  different panel briefs, the pi-wedge-fix review itself); cumulative `total_token_usage.total_tokens`
  20.9k → **22.0M**, rollout file 3.2 MB. ~80–90% token waste on heavy panel days.

Impact, severity order: (1) **cross-dispatch contamination** — panel reviewers carry other panels'
briefs and their own prior-round verdicts (independence/anchoring — a correctness problem);
(2) gpt-5.6 plan-quota burn; (3) giant-context latency/wedge risk.

## Fix

Mirror the pi-sdk fix in `CodexEngine.__init__`:

```python
raw_retire = os.environ.get("BRIDGE_CODEX_RETIRE_AFTER_TURN")
self.retire_after_turn = str(raw_retire).lower() not in {"0", "false"}
```

- Default **ON**: the pool retires (stops) the engine after every dispatch; the next dispatch gets a
  fresh process + fresh thread. No engine_pool changes — the retire hook already exists and is
  covered by `test_engine_pool.py` (release-retires/reuses/unhealthy-stops-once at lines 128–200).
- Opt-out: `BRIDGE_CODEX_RETIRE_AFTER_TURN=0` (or `false`, case-insensitive) restores warm pooling
  for a deliberately long-lived seat. NOTE (deploy-time): `BRIDGE_*` vars read via `os.environ`
  must be set in the **launchd plist**, not the seat env file (the launcher never exports the env
  file into `os.environ`). Default-ON needs no config anywhere.

## Continuity (load-bearing — must keep working)

Warm continuity is a real, used feature (recert dispatches say "YOUR round-1 findings"). It is
preserved via the **existing** dispatch surface, not pool warmth:

- payload `thread_id` → `thread/resume`; `fork_from_thread_id` → `thread/fork`
  (bridge.py:1755–1802); every reply returns `thread_id` (bridge.py:1877).
- Codex bypasses pool affinity entirely (`engine_supports_resume`, bridge.py:501/903): a
  `--thread-id` dispatch takes ANY engine — including a fresh process — and resumes on it.
- **Resume-across-process LIVE-PROVEN 2026-07-10** on `codex-bridge-dev-example`: turn 1 stored a
  nonce and returned its thread_id; full daemon restart (`launchctl kickstart -k`, pid
  66455→69890) killed the app-server; a `--thread-id` continuation on the fresh process replied
  `RECALL ZEBRA-D8479E67-PLUM` — exact recall, same thread_id echoed. Codex reconstructs threads
  from `~/.codex/sessions` rollouts, so retirement cannot break explicit continuations.

## Non-goals

- No new dispatch params (the continuity surface already exists).
- No engine_pool changes.
- No agent-sdk engine triage (genuinely untriaged — separate follow-up; do not assume either way).

## Accepted residuals (same class the pi panel accepted as P2)

- The startup warmup engine (`pool.acquire("__warmup__")`, bridge.py:686) is retired immediately
  after release: eager startup-failure surfacing is retained; startup warmth is not. Each dispatch
  pays codex app-server cold start (process spawn + initialize + thread/start, a few seconds) —
  negligible against multi-minute panel turns.
- `engine_supports_resume` is still set from the warmup engine before release (bridge.py:689) —
  unaffected by retirement.

## Tests (mirror `tests/test_pi_sdk.py:83–99`)

1. default (env unset) → `retire_after_turn is True`
2. `BRIDGE_CODEX_RETIRE_AFTER_TURN=0` → `False`
3. `BRIDGE_CODEX_RETIRE_AFTER_TURN=False` (case-insensitive) → `False`

## Live gate (post-deploy, per seat class)

1. Two sequential self-contained dispatches return **distinct** `thread_id`s.
2. One explicit `--thread-id` continuation recalls prior-turn context (nonce probe).

## Deployment

Merge dev → fleet clone (`/Users/<user>/AgentRedisBridge`) pull → restart the 9 codex seats
(2 autostart `com.example.codex-bridge.*-sol`, 5 no-autostart arbseat `-sol` labels — kickstart
required after bootstrap — plus terra/luna). Check in-flight tasks before every restart
(fleet-restart discipline); codex seats are all on effort-capable code ≥`1ade57c`, so restarts
are safe on that axis.

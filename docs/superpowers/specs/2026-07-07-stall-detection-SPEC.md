# Seat stall detection (detect-only) — design+spec (v2 — bridge-side, panel round 1 absorbed)

**Origin:** /learn brief, Mark-resolved APPROVE DETECT-ONLY. **v1 (watcher-side) was
unanimously rejected by the panel** on convergent grounds: arb-watch is an on-demand viewer
(agy P0 — unattended runs get no detection, and detection-in-the-viewer misses the entire
point); the notify contract is unimplementable from the Go-only surface (codex P1, GLM P0 —
envelope/bus machinery is bridge-side); and the watcher's data path never sees model deltas
(codex P1 — the orchestrator SSE rows are reduced; model_text/thinking exist only bridge-side
at bridge.py:1836-1870). **Mark's added requirement, first-class:** the stall signal must
reach the DISPATCHING WARM ORCHESTRATOR, not just a human watcher.

## Architecture (v2): detection in the bridge daemon, surfaced on three channels

**Detection — `src/agent_redis_bridge/bridge.py` (+ small helper module `stall_watch.py`):**
per active turn, track `last_progress_ts`. PROGRESS = tool-event kinds (`command_started`,
`tool_call`, `command_output`, `command_finished`, `tool_output`) AND `model_text` /
`model_thinking` deltas (the bridge sees both natively — a generating model is not stalled).
`turn_heartbeat` explicitly is NOT progress (the liveness-theater pin). Config:
`BRIDGE_STALL_AFTER_SECS` (default 600; `0` disables). Checked on the daemon's existing
periodic machinery (heartbeat tick) — no new thread class if one fits.

On stall (gap > threshold, once per EPISODE, re-armed only after progress resumes or the
turn ends):
1. **Event channel:** push a `stall_detected` task event (event stream + live tee) with
   `stalled_for_secs` — arb-watch and the visibility web UI receive it through their
   existing paths.
2. **Status channel:** set `stalled_at` (ISO ts) on the `task:<id>:status` hash; CLEAR it
   on next progress and on terminal states.
3. **Orchestrator channel (Mark's requirement):** `make_notify` (existing envelope
   machinery) with `event=stall_detected`, payload {task_id, seat_id, run_id,
   stalled_for_secs}, LPUSHed to the task's `from` agent's **notify_inbox** — automatic,
   no flag; the bridge knows the dispatcher's identity from the request envelope.

**Waiter surfacing — `tools/go-client` (has bus config already):** while waiting for the
reply, on each BLPOP-timeout tick, HGET the task's status hash; on first sight of a
non-empty `stalled_at` for a new episode, print ONE stderr line
`[stall] task <id> seat <seat>: no progress since <ts>` — so a warm orchestrator's routine
check of the dispatch .err file (or the eventual harness notification) carries the signal
with zero opt-in. Never changes exit codes or behavior otherwise.

**Watch display — honestly scoped (GLM R2 P1):** the visibility orchestrator snapshot is
built from `events:live`, NOT per-task status hashes — so surfacing the marker is a real
`src/arb_memory/visibility.py` change: during its existing active-task backfill/refresh it
ALSO reads `stalled_at` from each active task's status hash and includes it in the reduced
row (the status hash is the REQUIRED primary read path; a last-event-is-`stall_detected`
heuristic is NOT used — a later heartbeat event would clear it). `tools/arb-watch-go` then
renders marker + duration from the field. Overlay only; state stays `running`.

**Accepted looseness (GLM R2 P2, noted):** the bridge's notify episode-tracking and the
go-client's stderr episode-inference are independent; a clear+re-stall between BLPOP ticks
can rarely duplicate or drop a stderr line. Advisory channel; accepted.

**Never any action against the task or seat** — no cancel, kill, or reclaim, on any channel.

## Verification obligations

- Bridge unit tests (fake clock, existing bridge-test patterns): heartbeats do NOT reset
  the progress clock while an 11-min tool gap classifies stalled (the agy pin); tool event
  resets; model_text delta resets; episode semantics (one event + one notify + one status
  set per episode; re-arm after resume); terminal clears `stalled_at`; `0` disables;
  notify envelope shape + recipient = the request's `from` agent.
- go-client test: stderr line exactly once per episode; silent when never stalled.
- arb-watch Go test: row renders marker when `stalled_at` present; clears when absent.
- Full python suite + `go test ./...` green. Live gate: a deliberate wedge (dispatch a seat
  task that sleeps past threshold via a hung tool) produces all three signals — event
  visible in arb-watch, `stalled_at` in the status hash, notify in the dispatcher's
  notify_inbox, stderr line in the waiting dispatch client — and clears on completion.

## Live wedge gate — EXECUTED 2026-07-08 (all channels green)

Throwaway seat `codex-bridge-wedge` (<workspace> clone @17dda19, `BRIDGE_STALL_AFTER_SECS=30`,
`--notify-inbox 0`), task = `sleep 90` via codex, dispatched with `scripts/dispatch-dev`.
Two rounds:

- **Round 1** (`wedge-gate-20260708T073827Z-2d484f`, task `167a49d5`): event ✓ (1×
  `stall_detected`, 36s), status-hash set ✓ (by emission order), notify ✓ (1×, correct
  run_id), task completed ok and `stalled_at` absent at terminal ✓ — but the go-client
  `[stall]` stderr line NEVER fired: two stale sibling replies on the orchestrator inbox
  made BLPOP return instantly forever, and the poll only ran on BLPOP timeout
  (**poll starvation** — found only because the gate ran live). Fixed red-green:
  `waitForReply` polls on a 5s time cadence independent of BLPOP outcomes
  (`TestStallLineDespiteOrphanStarvation`).
- **Round 2** (`wedge-gate-r2-20260708T074935Z-b77618`, task `a472cafb`, orphans left in
  place deliberately): all four channels ✓ — 1× `stall_detected` in the task event stream
  AND in the DO-Valkey `events:live` tee (visibility/arb-watch path, `event_type` field);
  `stalled_at` observed live in the status hash for 9 poll ticks then cleared on resume;
  1× notify in `claude-bridge-dev`'s notify_inbox; exactly 1× `[stall]` stderr line
  despite the starvation traffic. (r2's `ok=false` was the completion gate bouncing an
  unrelated dirty CHANGELOG.md in the shared checkout, not a stall-machinery issue.)

**AGY-2 follow-up (2026-07-08, blind-until-proven — design
`2026-07-08-agy2-dark-channel-design.md`, panel-approved unanimously):** agy-print tasks now
start BLIND; stall detection is silent until a real progress event proves the channel. A
blind task past threshold surfaces as `stall_unknown` + `progress_blind` (event stream /
`events:live` / status hash — visibility plane) and NEVER as `stall_detected`/notify/
`[stall]` stderr. **Operator note:** for agy seats, a pre-first-progress wedge signals ONLY
on the visibility plane and is bounded by `--turn-timeout` — keep agy seats at
`--turn-timeout <= BRIDGE_STALL_AFTER_SECS` (current fleet: 600/600); the daemon warns at
startup when the config diverges. `BRIDGE_AGY_CONVERSATIONS_ROOT` (env-file or process env)
overrides the transcript root; a missing root now warns per turn instead of staying silent.
Live-gate case "channel dark but engine healthy" is part of this gate's test plan.

## Non-goals

Auto-recovery/kill/reclaim; per-engine thresholds; watcher-side detection of any kind;
changes to visibility auth or event schemas beyond the one new event kind + status field.

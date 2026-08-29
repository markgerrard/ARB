# AGY-2: dark progress channel must not read as a stall — design (v2.1, panel rounds 1+2 absorbed)

**Round 2 (`panel-agy2design-r2-20260708T084922Z-43835e`): certify quorum unanimous APPROVE**
(codex P2, agy none, pi-GLM P2; cold-Opus non-certifying approve P2). v2.1 absorbs the
round-2 P2 notes in place — no mechanism change.

**Origin:** engine-seat audit AGY-2 (P1, CONFIRMED; `2026-07-07-arb-engine-seat-audit.md:152`,
IMP-9), deferred out of the audit fix-stack because the fix changes stall-detection
*semantics*. Mark-approved design pass 2026-07-08. Author: warm orchestrator (inline,
Anthropic lineage) → **certify quorum = codex + pi-GLM + agy-print; cold-Opus admissible
non-certifying.**

**v2 (round 1 absorbed, run `panel-agy2design-20260708T083802Z-f10dc4`):** v1's
enumerated-dark-states design had a structural hole found convergently by cold-Opus (P1) and
GLM (F6): a *wrong-but-existing* `conversations_root` binds, never yields, never disables —
no enumerated trigger fires and the false stall survives. v2 inverts the polarity:
**blind-by-default, cleared only by proof of light** (a real progress event). Also absorbed:
codex P1 / GLM F1 / cold-Opus P2-B (turn-timeout is config, not an invariant — a blind task
crossing the threshold now emits an honest `stall_unknown` instead of relying on a backstop);
agy P1 + codex P2 (blind-marker lifecycle pinned to the same choke points as `stalled_at`);
agy P2 (blind ↔ stalled mutual exclusion); GLM F3 (lock discipline + guarded writes); GLM F5
(closed reason enum, no free text on the bus); agy F3 (warning throttle); agy F4 + codex note
(explicit env resolution at the construction site); cold-Opus P2-A (v1's `granular` re-arm
event dropped — the D3 disable is a per-turn latch, so "dark tracker later yields rows"
cannot occur; the first real progress event is itself the only "lit" signal needed).

## Problem (hinge facts, panel-verified line-by-line at `ba08be0`)

agy-print's **only** mid-turn progress events come from the per-turn `AgySqliteTranscript`
poller tailing Antigravity's conversation DBs; `agy --print` emits nothing on stdout until
`process.communicate()` returns (`agy_print.py:497-560`). The stall detector counts only
`PROGRESS_EVENTS` (`stall_watch.py:8-18`); `turn_heartbeat` and `turn_started` are
deliberately NOT progress (liveness-theater pin).

Ways the transcript channel is dark (now understood as an **open set** — this is the v2
design driver):

| # | Dark state | Mechanism | Warning today? |
|---|---|---|---|
| D1 | `ARB_TRANSCRIPT_CAPTURE=off` | tracker never constructed (`agy_print.py:537,631`) | none |
| D2a | `conversations_root` nonexistent | `poll()` silently yields nothing (`:251`); root hardcoded-by-default (`:484`), no env/CLI wiring (construction site `bridge.py:2558` passes only cwd/model) | **none — fully silent** |
| D2b | root **exists but is wrong** (path drift, misconfig) | binds, polls forever, yields nothing, never disables — **no enumerable trigger** | none |
| D3 | schema drift / decode failure / missing steps table / ambiguous nonce | per-turn `disabled=True` latch at ~6 sites (`:199-298`); once latched, `poll()` short-circuits (`:199-200`) — no recovery within the turn | one warning per disable |
| D∞ | anything not yet imagined | — | — |

**Consequence:** in any dark state every healthy agy-print turn longer than
`BRIDGE_STALL_AFTER_SECS` fires a false `stall_detected` into all four live-verified
channels. The detector cannot distinguish "no progress" from "no visibility". No test pairs
a dark tracker with `StallWatch`.

## Constraints

- **Detect-only ethos**: never act on the task/seat; signals must be honest.
- **Liveness-theater pin stands**: process-alive must NOT count as progress; fabricated
  ticks are the same lie one layer down (panel-unanimous: rejecting synthetic tick is
  correct; no viable hybrid).
- **No false alarms on the warm-orchestrator channel** (notify + `[stall]` stderr) — it is
  Mark's first-class signal; a channel that cries wolf gets ignored.
- **No invariant claims the code doesn't enforce**: `--turn-timeout` defaults 3600 while
  stall defaults 600; the "timeout backstop" is deployment convention (current fleet agy
  seats run 600), and the design must stay honest when the convention doesn't hold.
- Wedge-gate-verified behaviour for non-agy engines must not regress.

## Design v2: blind-until-proven (structural) + honest unknown + darkness-shrinking

### B′ — blind is the default; only real progress proves the channel

1. **Bridge-side structural default.** The bridge daemon knows its engine
   (`args.engine`). For engines in `BLIND_UNTIL_PROGRESS = {"agy-print"}` (agy-tmux is a
   named follow-up, per AGY-4), `StallWatch.start(task_id, blind=True)` marks every task
   blind from turn start. Being bridge-side and unconditional, it **cannot fail to arm** —
   no engine event, config probe, or dark-state enumeration is load-bearing (this closes
   D2b and D∞ by construction).
2. **Proof of light clears blind.** Any `PROGRESS_EVENTS` member for the task clears blind
   (under the existing `StallWatch._lock`, same discipline as `check`/`progress`/`end`).
   From then on, classic stall semantics apply — a *later* genuine gap fires a normal
   `stall_detected` episode. `stall_detected` thus becomes a strictly stronger claim: "a
   **proven-live** channel went quiet past threshold."
3. **Re-blind on known dark transitions.** The engine emits a normalized
   `progress_channel` event `{"state": "dark", "reason": <enum>}` on D3's disable latch
   (and at turn start when D1/D2a are already known, purely to *refine the reason*). The
   bridge re-marks blind on this event — covering the "rows flowed, then the tracker
   latched off" case, where post-progress silence is otherwise indistinguishable from a
   wedge. Reasons are a **closed enum**: `capture-off | root-missing | unproven |
   tracker-disabled`. Free-text detail (paths, exception text) goes only to the local
   `logger.warning`, never onto the bus (nothing for `events:live` redaction to worry
   about; the tee's recursive redaction at `visibility_tee.py:43` covers the rest).
4. **Blind task crossing the threshold → honest unknown, not silence, not alarm.** When
   `_check_stalls` finds a blind task past `after_secs` (once per blind episode):
   - set `progress_blind=<reason>` on the `task:<id>:status` hash (lazy — written at
     crossing, like `stalled_at`; guarded write: failure logs and retries next tick,
     mirroring the GAP-1 unmark pattern);
   - push ONE `stall_unknown` task event `{task_id, reason, unproven_for_secs}` → event
     stream + `events:live` tee (visible in arb-watch/visibility);
   - **NO notify, NO `stalled_at`, NO `[stall]` stderr line** — the orchestrator channel
     stays high-precision; the status hash and event stream carry the low-precision
     "cannot assess" signal.
   - `stall_unknown` emission is latched like a stall episode and **unmarks on emit
     failure** (GAP-1 parity, `stall_watch.py:94` pattern) so a Redis blip duplicates
     rather than silently drops it (r2 cold-Opus P2-B).
   - Mutual exclusion — **stale markers only** (r2 GLM N2 refining cold-Opus P2-C):
     marking blind blocks NEW `stall_detected` episodes and deletes a `stalled_at` only if
     no episode is currently active; an ACTIVE fired episode's `stalled_at` is NOT
     retracted (the alarm was earned while the channel was proven live — re-blind must not
     silently withdraw it). Real progress clears `progress_blind` (guarded).
   - Re-blind does NOT reset the progress clock (r2 GLM N1): `stall_unknown`'s
     `unproven_for_secs` stays coherent with the last real progress timestamp.
   - The re-blind `progress_channel: dark` emission must exist at **all three** D3 disable
     sites (`agy_print.py:206/215/228`) — an un-emitting disable path resurrects the exact
     false `stall_detected` this design kills (r2 cold-Opus P2-A; one test per site).
5. **Lifecycle parity with `stalled_at`.** `progress_blind` is deleted at exactly the
   same choke points that clear `stalled_at` today: the resume path
   (`_record_stall_progress`) and the terminal `finally` (`bridge.py:1119-1126` /
   `_clear_stalled_at` — extended to clear both fields, e.g. renamed
   `_clear_stall_status`).
6. **Wedge visibility while blind — stated honestly, and LOUDLY (r2 GLM N3, the
   load-bearing trade-off).** A real wedge in a blind state is NOT stall-detected; it
   surfaces as `stall_unknown` + `progress_blind` (visibility plane / arb-watch) and dies
   at the engine turn-timeout. **All agy wedges before first-progress lose the
   notify + `[stall]` stderr signal by design** — the runbook must say so explicitly: for
   agy seats, watch `stall_unknown` on the visibility plane. **Operator note (runbook +
   `.env.example`):** agy seats SHOULD run `--turn-timeout <= BRIDGE_STALL_AFTER_SECS`-ish
   (current fleet: 600/600) so blind wedges are bounded; a divergent config
   (stall < timeout by a wide margin) should be surfaced at daemon start — this is a
   recommendation plus a startup warning, not a code invariant, and the design does not
   lean on it.

### C — make darkness rare and loud (unchanged from v1, placement fixed)

7. **Env-wire the root:** `BRIDGE_AGY_CONVERSATIONS_ROOT`, resolved at the bridge's engine
   construction site (`bridge.py:2558`) from env-file + process env — explicit parameter,
   not an `os.environ` read inside the engine.
8. **D2a becomes loud:** at bind time, if `conversations_root` does not exist, one
   throttled warning per turn naming the path (per-tracker boolean flag — the 0.2s poll
   loop must not flood).
9. D3's existing per-disable warnings stand; the disable remains a per-turn latch (retries
   naturally next turn).

### Rejected: A — synthetic progress tick while dark (panel-unanimous)

A tick that measures nothing resets the stall clock forever on a real dark wedge —
fabricated health, `fixture-supplies-what-code-lacks` shipped to production. B′ achieves
A's only benefit (no false `stall_detected` while blind) via suppression without the lie,
and `stall_unknown` keeps the blind-wedge case visible, which A cannot do. No hybrid adds
anything but the fabrication.

## Verification obligations

- **Blind-by-default (the missing AGY-2 test):** agy-engine bridge + zero progress events
  past threshold → NO `stall_detected`/`stalled_at`/notify; `progress_blind` set with
  reason `unproven`; ONE `stall_unknown` in the event stream; both cleared at terminal.
- **Proof-of-light:** first progress event clears blind (+deletes `progress_blind`,
  guarded); a later genuine gap DOES fire a normal episode (re-arm preserved).
- **Re-blind on D3:** progress flows → `progress_channel: dark (tracker-disabled)` →
  subsequent silence yields `stall_unknown`, not `stall_detected`; a stale `stalled_at`
  from a pre-dark episode is deleted on the blind transition (mutual exclusion).
- **D2b regression case:** existing-but-wrong root → still blind (default), `stall_unknown`
  fires — the case v1 missed, pinned forever.
- **Once-per-episode:** exactly one `stall_unknown` per blind episode; blind → lit → blind
  again re-arms it.
- **Guarded writes:** blind-marker Redis failures log + retry next tick; never raise
  through `_check_stalls` or the progress path (GAP-1 parity).
- **Non-agy engines unchanged:** `BLIND_UNTIL_PROGRESS` excludes them → existing stall
  suite byte-identical behaviour.
- **C:** env override respected end-to-end (env-file and process env); D2a warning fires
  once per turn when root missing.
- **Round-2 additions:** proof-of-light racing the blind threshold-crossing emission
  (`before_mark`-style race hook, r2 codex); one test per D3 disable site emitting the
  dark event (r2 cold-Opus P2-A); `stall_unknown`/`progress_channel` payloads pass the
  `events:live` recursive redaction untouched — and the closed enum, not `redact()`, is
  the path-PII boundary (r2 GLM N4/N5); the poll-thread Redis-failure vector
  (`bridge.py:1869` sits outside the guarded try — AGY-1-adjacent) gets a failure-injection
  test; divergent stall/timeout config surfaces a startup warning.
- **Live wedge-gate addition (stall spec § test plan):** "channel dark but engine
  healthy" — temp agy seat with `ARB_TRANSCRIPT_CAPTURE=off`, low threshold, a >threshold
  multi-step turn: assert NO `[stall]` stderr line / NO notify; `progress_blind` +
  `stall_unknown` visible; then the codex wedge from the executed gate still fires all four
  channels (no regression).

## Live gate — EXECUTED 2026-07-08 (both cases green, implementation `196f597`)

- **Dark but healthy** (`wedge-dark-…`, task `4b8be8b6`, seat `agy-bridge-wedge`,
  `ARB_TRANSCRIPT_CAPTURE=off`, threshold 30s, `sleep 60`): ZERO `[stall]` stderr lines,
  zero `stall_detected`, zero notifies; exactly one `progress_channel dark/capture-off` and
  one `stall_unknown` (39s) in the event stream; `progress_blind=capture-off` observed live
  in the status hash for ~30s then cleared at terminal; task completed ok. The startup
  divergent-config warning (600 > 30) also fired as specified.
- **Classic wedge regression** (`wedge-reg-…`, task `c199f289`, seat `codex-bridge-wedge2`,
  `sleep 90`): all four channels fired exactly once (stderr line, event, notify,
  `stalled_at` set then cleared); zero `stall_unknown`. No regression.

## Non-goals

Detection teeth; per-engine thresholds; agy-tmux's analogous gap (follow-up, same
mechanism applies); arb-watch *rendering* of `progress_blind`/`stall_unknown` (status
field + event are the contract; display can follow); enforcing the turn-timeout
recommendation in code.

# ARB Observability — Slice 5a-0: capture normalization (design **v7 — folded**)

Status: DESIGN v7 — folded from re-panel round 6 (needs-changes/P1, CLOSED). For re-panel round 7.
Date: 2026-07-13. Author: warm-Opus orchestrator.
Roadmap parent: [[arb-observability-roadmap]] Slice 5. Prerequisite of `…slice5a-design.md` (v2).
Panel history: r1→v2, r2→v3, r3→v4, r4→v5, r5→v6, r6→v7. Loop bar: re-panel until 0 P0/P1.

> **v6 → v7 fold (round-6 P1s, both code-verified).** r6 VALIDATED the v6 scope correction: 3 seats (agy
> `none`, grok `P2`, cold-Opus zero-P1) confirmed `attempt_epoch` is buildable (read-once task-scoped Redis
> `INCR`), the recovery-boundary set is exhaustive (`task_id = envelope.id`), O1–O3 cover the r5 P1s, and the
> false claim is fully retracted. codex-sol@high (needs-changes/P1) then found two NEW code-verified holes the
> other seats missed:
> - **M1 (P1-2, 5a-0 owns) — claude-tail higher-epoch re-opens are SUFFIX replays, but O2 assumes FULL
>   replacement.** A re-opened tailer seeks to its persisted byte offset (`claude_tail/tailer.py:88-103`,
>   `offset.py:14-43`) and emits only the *suffix* (newly-appended turns). v6's "advance the epoch per re-open"
>   would trip O2 into deleting the whole prior attempt while the re-open never re-emits the prefix → earlier
>   turns vanish. **Fix:** the claude-tail epoch increments **only on a full-session replay from byte 0**, never
>   on an ordinary persisted-offset suffix resume (a resume reuses the same epoch — it is a continuation, not a
>   new attempt). Component 4 corrected below.
> - **M2 (P1-1) — a stale-but-live predecessor can DELETE the successor's re-parked envelope.** Reliable-inbox
>   acknowledgement is a **body-keyed** `LREM :processing 1 <raw-body>` with no owner token
>   (`redis_io.py:317-318`), called unconditionally in `process_request`'s `finally` (`bridge.py:1443-1447`);
>   ownership-loss cleanup does not join active request threads (`bridge.py:620-653,671-682`). So: A parks R
>   (epoch 1), loses its lease but stays live; B claims the identity, recovers + re-parks the identical R (epoch
>   2), runs; A's stale `finally` then `LREM`s R, deleting **B's** parking entry; if B crashes, recovery finds
>   nothing → the higher-epoch *request is lost*. The epoch stays monotonic but the execution carrying it does
>   not survive the restart it marks — O1–O3 repair projected rows, not a lost request. This is a **pre-existing
>   bridge reliable-inbox defect** (independent of observability) that 5a-0's recovery premise depends on, so
>   it is carried as a **named, cited precondition** (below), not absorbed as capture-design ownership.

> **Fold provenance.** r5 reports `…-r5-review-{codex-sol,agy,grok,cold-opus}.md` — **unanimous** needs-changes/P1.
> **Retraction (explicit):** v5's fold-H claim that "5a's UPSERT makes a request re-run idempotent-if-identical
> and last-attempt-wins-if-divergent" was **FALSE and is withdrawn.** 5a's actual SQL is start `ON CONFLICT DO
> NOTHING` (first-start-wins) + finish `DO UPDATE … latency = finish − stored_start` (last-finish vs first-start)
> — so a re-run splices attempt-1's `started_at` with attempt-2's `finished_at` (latency spans the crash gap,
> even byte-identical), tool spans on retry-unstable `tool_call_id` accumulate (overcount), and a reconnected
> predecessor can emit unfenced during lease-takeover. v5 also mis-cited `…slice5a-design.md:179-181`
> (`task_continuing` = one execution) to justify request **re-execution** — a category error. **Root cause of
> the error: 5a-0 asserted behavior of code it does not own (5a's SQL) without citation.** v6 fixes the scope,
> not just the claim.

## Scope correction (the r5 lesson)

The three surviving P1s share ONE root cause — **5a's span projection cannot distinguish attempts** — and the
fix touches **5a's SQL** (replace semantics, tool-span keying, a write fence). A slice cannot certify claims
about code outside its boundary. So v6 **narrows 5a-0 to its verifiable boundary**:

- **5a-0 owns (capture, verifiable here):** deterministic turn identity (verified sound r3–r5) + a new
  **attempt-epoch capture primitive** stamped at source (below). These are claims about the bridge — 5a-0's own code.
- **5a owns (projection, verified in 5a's cycle):** the epoch-aware **replace + fence** that makes re-runs
  correct. Carried below as **named contract obligations 5a MUST clear** (or explicitly descope with rationale)
  in its own panel — the work is deferred, the rigour is not.

## v5 → v6 fold (round-5 P1s)

| # | v5 gap (r5 finding, all verified vs code) | v6 action |
|---|-------------------------------------------|-----------|
| **L-retract** | v5 asserted "upsert = idempotent/last-wins" (`v5:18,41-45`) — false vs 5a SQL (`slice5a-design.md:111-130`). | **Withdrawn in spec text** (see banner). 5a-0 makes NO claim about 5a's conflict semantics. |
| **L-primitive (5a-0 owns)** | No attempt boundary exists; a re-run is indistinguishable from the first attempt. | **Attempt-epoch capture primitive** (Component 2). The bridge stamps a monotonic `attempt_epoch` per `(run_id, task_id)` on every eval event; it **increments** at each fresh execution boundary — a reliable-inbox recovery (`recover_processing_envelopes`, `bridge.py:870`) and a lease-takeover. This is a source-only concern (cannot be reconstructed downstream). |
| **L-contract (5a owns)** | P1-1 timestamp splice; P1-2 tool overcount; P1-3 lease-takeover fence — all in 5a's SQL. | **Named 5a obligations** (Contract, below). 5a's projection replaces-on-higher-epoch, fences-lower-epoch, and its live-gate asserts LATENCY correctness (not just row identity — cold-Opus P2). |
| **K+** | agy P2-1: clearing the active ordinal before the terminal event is emitted would unstamp the terminal. | Stamp `turn_index`/`attempt_epoch` on the terminal event FIRST, then clear active state. |

Verified **resolved / sound** (do not revisit): deterministic `turn_index` identity (all replay-key classes);
folds A–K; fold I agy-tmux mint relocation; fold J disjoint task_ids (`identity.py:38,70`); one `turn_started`
per logical turn. The permanent-Redis-key P2 remains dissolved (no id-map).

---

## Goal (5a-0 scope only)

Every capture producer emits into the durable eval payload: a canonical **`tool_call_id`** (same on both tool
edges); a **deterministic per-`(run_id,task_id)` integer `turn_index`**; and a monotonic **`attempt_epoch`** per
`(run_id,task_id)` — the capture primitives 5a needs to project correct, recovery-safe timing spans. 5a-0 makes
no claim about 5a's projection SQL. Requires a fleet bridge redeploy (covers claude-tail).

## Components

1. **Canonical `tool_call_id` coalescing (shared helper).** `first_nonempty(data.tool_call_id, data.tool_use_id,
   data.item_id)` — provider ids before presentation ids.
2. **Deterministic `turn_index` + `attempt_epoch` (capture).**
   - `turn_index` — per-`(run_id,task_id)` ordinal the bridge assigns in execution order at each proven
     logical-turn boundary (fold I); stamped on turn/tool edges until the next advance; scope `(run_id,task_id)`,
     no `seat_id` (fold J). Deterministic ⇒ a re-run reproduces the same ordinals (stable KEYS).
   - **`attempt_epoch`** — a monotonic integer per `(run_id,task_id)`, stamped on EVERY eval event, that
     **increments at each fresh execution boundary**: (a) a reliable-inbox recovery re-queues + re-runs a
     parked request (`recover_processing_envelopes`, `bridge.py:870`) → epoch += 1 for that task before
     re-processing; (b) a lease-takeover where a successor claims an identity → the successor's epoch strictly
     exceeds any a still-live predecessor could stamp (so P1-3's reconnected predecessor is distinguishable by
     a LOWER epoch). Sourced from durable state (recovery count / boot token), so it survives the very restart
     it marks. **Guarantees 5a can rely on:** epoch is non-decreasing per `(run_id,task_id)`; a given physical
     execution stamps ONE epoch on all its events; a stale predecessor's events carry an epoch strictly less
     than the current owner's.
   - Out-of-turn events (`task_started`, `agent_sdk_subscription_audit`, `task_continuing`) carry no
     `turn_index`; the terminal event is stamped BEFORE the active ordinal is cleared (fold K+).
   - No persistent Redis id-map; `turn_index` durability is determinism, `attempt_epoch` durability is the
     recovery/boot counter it derives from.
3. **agent_sdk & pi_rpc semantic fixes.** (agent_sdk) rename gate `command_finished` → `tool_permission_decided`;
   real `command_finished` from `ToolResultBlock` with `tool_call_id = block.tool_use_id`; deny → one true
   finish; `agent_sdk_subscription_audit` out of turn scope. (pi_rpc) `command_finished` only from
   `tool_execution_end`; demote `toolcall_end`; drop first-wins dedup; add `turn_completed` on clean `agent_end`;
   fix `tests/test_pi_rpc.py:472-564`.
4. **claude-tail logical-turn lifecycle.** One human `user`-prompt cycle keyed on `promptId`; `turn_started` at
   the human boundary, `turn_completed` at cycle end; `map_line` parses/carries `uuid`; per-assistant ordinal
   stays trace-only; inode-aware persisted state. **`attempt_epoch` for claude-tail (M1, fold-corrected):** an
   ordinary tailer resume seeks to its persisted byte offset (`tailer.py:88-103`) and emits only the *suffix* —
   it is a **continuation, NOT a new attempt**, and **reuses the current epoch** (so O2 never deletes the
   un-replayed prefix). The claude-tail epoch increments **only on a full-session replay from byte 0** (a genuine
   re-attempt that re-emits every turn) — equivalently, O2 may replace a prior claude-tail epoch **only** when
   the higher epoch is a proven byte-0 replay. The replay unit and the attempt boundary must match.
5. **Allowlist.** Add `tool_call_id`, `turn_index`, `attempt_epoch` to `EVAL_ALLOWLIST` (`eval_tee.py:10-19`).
6. **Fleet bridge redeploy + soak.**

## Contract: obligations 5a-0 hands to Slice 5a (named, testable — 5a cannot clear panel without addressing)

5a-0 emits `turn_index` + `attempt_epoch`; **5a's span projection MUST**, using them:

- **O1 (was P1-1 timestamp splice):** on an event whose `attempt_epoch` exceeds the stored epoch for
  `(run_id,task_id[,turn_index])`, **REPLACE** — reset `started_at`/`finished_at`/`latency`/`outcome` to the new
  attempt's values (do not retain the prior attempt's `started_at`). Result: a re-run's turn latencies are the
  re-run's, never spliced across the crash gap.
- **O2 (was P1-2 tool overcount + ghosts):** on a higher `attempt_epoch`, **delete/supersede the prior attempt's
  turn AND tool rows** for `(run_id,task_id)` (tool `tool_call_id`s re-mint per attempt and won't self-conflict;
  surplus higher-ordinal turns from a shorter re-run must be removed). Result: rollups count one attempt, no ghosts.
- **O3 (was P1-3 lease-takeover fence):** **ignore events carrying an `attempt_epoch` strictly less than the
  stored epoch** for `(run_id,task_id)` (a reconnected predecessor). Result: no concurrent mixed rows during takeover.
- **O-gate (was cold-Opus P2):** 5a's recovery live-gate asserts **latency correctness** (re-run turns show the
  re-run's real latency, not the crash-gap-inflated splice) and **no ghost rows** — not merely matching
  `turn_index`. A gate that checks only row identity is vacuously green ([[graduation-criterion-measures-what-it-claims]]).

**Open decision queued for 5a design stage (do NOT resolve here — r5-fatigue):** is crash-recovery span
*exactness* even in scope for a timing/throughput layer, or is "a crashed-and-re-run task may have imperfect
spans" an acceptable documented limitation? This depends on what consumes the latency numbers (if anything
alerts/bills on them, silently-wrong latencies are worse than missing) and must be weighed against the O1–O3
fence-design cost. 5a either implements O1–O3 or **explicitly descopes them with rationale** — it may not drop
them silently.

## Precondition P-recovery (M2, named + cited — a bridge-infra dependency 5a-0's epoch guarantee rests on)

5a-0's guarantee that `attempt_epoch` "survives the restart it marks" is **conditional** on the reliable-inbox
recovery being **owner/attempt-fenced**. It is NOT today: `remove_processing` is a body-keyed
`LREM :processing 1 <raw-body>` (`redis_io.py:317-318`) run unconditionally in `process_request`'s `finally`
(`bridge.py:1443-1447`), and ownership-loss cleanup does not join in-flight request threads
(`bridge.py:620-653,671-682`) — so a stale-but-live predecessor can `LREM` the successor's re-parked envelope
and lose the higher-epoch request. **Required fix (pre-existing bridge reliable-inbox defect, contained):** make
processing acknowledgement **owner/claim-token-owned**, not body-owned — an atomic compare-token-then-remove (or
a per-attempt processing claim) so a stale token cannot acknowledge/delete the successor's claim. This is a
distinct bridge-dispatch-infra concern (a lost request harms every dispatch, not just spans), carried here as a
**cited precondition** rather than absorbed into the capture design; 5a-0's recovery live-gate depends on it and
asserts it (below). Per [[cross-slice-claims-need-citation]], the claim is grounded in the actual code, not asserted.

## Done-criterion / live gate (5a-0 scope)

- Producer/pin roster incl. a real interactive claude-tail session.
- **claude-tail suffix vs replay (M1)**: emit an early turn, restart the tailer from a NONZERO persisted offset,
  append + emit a later turn, project under O1–O3 → BOTH turns remain with correct latency (the suffix resume
  must NOT delete the prefix); a byte-0 full replay DOES bump the epoch and replace.
- **Owner-fenced recovery (M2 gate)**: takeover with the predecessor still live — B recovers + re-parks R, A
  reaches its stale `finally`, B is killed, and C must STILL recover R and allocate an epoch greater than both
  predecessors (proves the acknowledgement is owner-fenced, not body-keyed).
- **Deterministic identity**: engine respawn / warm rotation mid-task does not collide `turn_index`.
- **`attempt_epoch` emission**: a bridge-daemon kill + reliable-inbox recovery stamps a STRICTLY HIGHER epoch on
  the re-run's events than the pre-crash attempt; a simulated lease-takeover stamps the successor a higher epoch
  than a still-live predecessor. (The *consequences* of the epoch — replace/fence/latency — are 5a's gate, O-gate.)
- Mint boundary (fold I): engine startup failure produces no phantom `eval_turn`.
- claude-tail: one prompt + ≥2 tool rounds → one `eval_turn`; restart windows preserve `turn_index`.
- ≥2 tool calls/producer; ≥1 failing/delayed tool (finish from REAL execution); read the durable EVAL STREAM +
  verify pin SHA; deny-proofs.

## Spec-stage P2 backlog (carried from the r7 CONVERGED panel — non-blocking, resolve at spec)

The r7 panel returned **unanimous approve / zero P0/P1** (design converged). These P2s are carried into the
spec stage — NOT resolved here, to avoid authoring an unreviewed resolution (the r5-error lesson). They
converge on the **claude-tail epoch** and cluster tightly:

- **claude-tail is append-only, single-writer** (cold-Opus) → its epoch is effectively always **1** and its
  spans are never legitimately O2-replaced (a re-read is a continuation, not a competing attempt). The spec
  should likely **pin the claude-tail epoch to 1 (never bump)** — which dissolves the next three edges at once.
- **byte-0 replay re-stamps `sent_at` with wall-clock** (`tailer.py:384`, cold-Opus/grok) → an
  offset-reset-to-0 (file truncate, `tailer.py:93-95`; corrupt-offset heal, `offset.py:28-34`) that bumped the
  epoch would feed O1 garbage latency. The epoch trigger must key on a genuine full-session (re)start, not any
  offset-reset — OR, better, claude-tail should derive latency from the **transcript's own line timestamps**
  (stable across re-reads → idempotent) rather than emit-time `sent_at`. Spec to decide.
- **deploy migration**: the first post-redeploy suffix resume finds no epoch counter → pin **absent ⇒ epoch 1**.
- **ordering**: claude-tail epoch allocation must land **after `run_id` resolution** (`tailer.py:268-276`).

## Non-goals
No span tables/consumer/retention/projection SQL (5a). No token/cost. No attempt-in-key (epoch is a
fence/replace trigger, not a key dimension). Not a general wire-protocol redesign.

## Open questions for the SPEC panel (5a-0)
- SP0-1 — the durable `attempt_epoch` source. **r6 panel consensus (agy/grok/cold-Opus, fold this):** a
  **task-scoped Redis `INCR` per `(run_id,task_id)`** (`RedisCli.incrby`, `redis_io.py:279-283`) — the same
  durable substrate that already carries `:processing` — **allocated once at accepted physical-execution start
  and snapshotted (read-once, cached in immutable/local execution context)**, so every event of one execution
  carries that one scalar even under the async eval flusher, and a stale predecessor's cached value is strictly
  lower. **NOT a per-daemon boot token / raw UUID** (unordered → fails cross-process takeover ordering). SP0-1
  now confirms buildability; the remaining spec work is pinning the exact key + allocation point.
- SP0-2 — the proven logical-turn boundary per engine (agy-tmux relocation detail); claude-tail cycle
  open/close precedence + human/meta classification.

# ARB Observability Slice 5a-0: Capture Normalization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every ARB capture producer emit — into the durable eval payload — the primitives Slice 5a needs to project correct, recovery-safe timing spans: a canonical `tool_call_id`, a deterministic `turn_index`, a monotonic `attempt_epoch`, and (for claude-tail) an idempotent `event_ts` plus a fail-closed `turn_clock_monotonic` clock-validity flag.

**Architecture:** Capture-only. Add a shared `canonical_tool_call_id` coalescing helper; extend the eval allowlist by five bounded scalars; add `attempt_epoch` (INCR-sourced for dispatch producers, constant `1` for claude-tail) and deterministic `turn_index` to the dispatch eval path; rebuild claude-tail's logical-turn lifecycle to emit `event_ts` + a `turn_clock_monotonic` flag earned **only** at a next-human-`user` close by one continuous scan (Option-D — no terminal earn); fix agent_sdk / pi_rpc tool-edge semantics; and land the M2 owner-fenced reliable-inbox precondition the epoch guarantee depends on. Then redeploy the fleet and soak against the live gate. **5a-0 makes NO projection claim** — the epoch-aware replace/fence semantics are handed to 5a as named obligations O1–O5.

**Tech Stack:** Python 3.14, `redis` (sync client), pytest (`uv run --extra arb-memory pytest`), the existing `agent_redis_bridge` package (`claude_tail/`, `eval_tee.py`, `bridge.py`, `redis_io.py`), the `scripts/claude_tail_hooks/` SubagentStart/Stop hooks.

## Global Constraints

Copied verbatim from the spec — every task's requirements implicitly include this section.

- **Extract-only eval boundary.** The eval payload is built by COPYING ONLY allowlisted keys out of the source event into a fresh dict (`eval_tee.py:22-25`) — never forward source-minus-denylist. Every new eval field MUST be a bounded scalar (id / int / ISO8601 timestamp / one bool). **No message text/thinking body may enter the eval stream** (`eval_io` OFF invariant). `turn_clock_monotonic`/`turn_started_ts` are a bool + a timestamp, never content.
- **All new fields ride the event `data`/payload, not top-level record fields.** `event_ts`, `turn_started_ts`, `turn_clock_monotonic`, `attempt_epoch`, `tool_call_id` go into the event `data` dict so `extract_eval_payload` (the allowlist) is load-bearing (cold-Opus fold). `turn_index` is **already** in the allowlist (`eval_tee.py:15`) — this slice makes it load-bearing on the claude-tail eval path, it is not a new member.
- **Five new allowlist members only** (`tool_call_id`, `attempt_epoch`, `event_ts`, `turn_started_ts`, `turn_clock_monotonic`); `turn_index` already present. Do NOT bump `EVAL_SCHEMA_VERSION` — additions are additive; default is no bump, no migration (confirm at panel).
- **Fail-closed, never a silent fallback.** claude-tail `event_ts` comes ONLY from the transcript line's own top-level `timestamp`; if absent, OMIT `event_ts` and bump a `claude_tail_missing_ts` counter — **never** substitute emit-time `sent_at` (that reintroduces the non-idempotency R2 removes). A synthetic/injected edge takes its `event_ts` from the transcript line that triggered it, never `_now()`/`sent_at`.
- **No timestamp correction in capture.** 5a-0 observes/flags the transcript's own clock ordering faithfully — it never reorders or alters a `timestamp`. The NULL-vs-number decision is 5a's (O4).
- **No terminal earn (Mark's Option-D, 2026-07-14).** `turn_clock_monotonic = true` is earned ONLY at a next-human-`user` close observed by the same continuous scan. The sidecar `completed:true`, the in-band `[ARB_SEAT_DONE]` marker, and any `finish()` drive ONLY the tailer finish/cleanup lifecycle — they NEVER earn `true`. A single-dispatch / last / terminal turn (no next human `user` line) is `false` ⇒ NULL; its latency is handed to 5a (O5). There is NO finalization fence, NO two-phase settle, NO version-pinned canary, NO `subagent_stop` `{final_inode,final_size}` in 5a-0.
- **No recovery machinery (Mark's Option-B, 2026-07-14).** No clock-accumulator persistence, no back-scan, no provisional close. The `turn_clock_monotonic` flag is pure fail-closed over a single continuous tailer generation. Persisting `turn_index` (position/identity state) is REQUIRED and is a DIFFERENT kind of state from the (never-persisted) clock accumulator — do not conflate them.
- **Deny-proofs must be RED when the guard is removed.** Every deny-proof in this plan asserts a property that goes green ONLY because a specific guard exists; deleting that guard MUST turn the test red. A vacuously-green guard is a plan failure ([[vacuously-green-guard-fail-loud]]).
- **Test invocation:** `uv run --extra arb-memory pytest tests/...` (bare `python`/`pytest` → `ModuleNotFoundError: psycopg`).
- **GREEN evidence must come from a CLEAN CHECKOUT, not the authoring worktree (r2-fold, codex).** Two r2 P1s were "green in the worktree, broken on a clean checkout of the committed branch": a `push_task_event` that reads `self._task_epoch` unconditionally passes in a worktree where the test bridge happens to carry the attribute but `AttributeError`s on a clean checkout unless every `Bridge.__new__` fixture is staged AND updated; and a required gate whose inputs (`git add`) were never staged. Rule: each task's final GREEN + delete-to-red evidence is recorded from a **fresh checkout of the committed SHA** (`git worktree add <tmp> <sha>` or clone), NOT the working tree the change was authored in. The gate that certifies a task is "clean checkout runs green," so an un-staged file or un-added fixture surfaces before merge, not in the next review round.
- **Deploy is gated.** The fleet redeploy + soak is PAUSED for Mark's deploy-review gate before any prod fleet redeploy. Do not redeploy prod without it.
- **CHANGELOG discipline:** every feature/fix gets a `CHANGELOG.md` entry (what AND why) — fold the entry into the task that ships the behavior ([[changelog-discipline]]).
- **Deny-proof discipline (r0-fold, REQUIRED — supersedes the v1 deny-proof shape):** a deny-proof is NOT a test that asserts the *defective* state under a monkeypatch (that stays green whether or not the production guard exists — vacuous, [[vacuously-green-guard-fail-loud]]). A deny-proof is: **(1)** a POSITIVE test that passes *because* a specific production guard exists, PLUS **(2)** an explicit **delete-to-red** step — physically delete that guard line, run the SAME positive test, confirm it goes RED, restore the guard, and record the red output in the PR ([[deny-proofs-need-adversarial-verification]]). The four v1 "assert-the-defect" tests are DELETED (see the r0 fold banner).

---

## r0 panel fold (v1 → v2) — provenance + carried fixes

> **r0 plan panel** (run `panel-slice5a0-plan-r0-20260714T083431Z-a73380`, closed `needs-changes`): certify quorum
> codex-sol@high + agy + grok ALL needs-changes/P1; cold-Opus non-cert approve/P2 (it verified the crux sound but
> MISSED the event_ts P1 the three executing seats caught — [[executing-seats-catch-taxonomy-misses]]). All seats
> confirmed the **crux is sound**: the `turn_clock_monotonic` Option-B/D mechanism is unforgeable-by-construction
> and Option-D (no terminal earn) holds. The P1s were lifecycle/coverage/method bugs *around* the sound core.
>
> **P1 folds (verified against code before folding):**
> - **P1-A — `turn_completed.event_ts` was the NEXT prompt's ts (agy+grok+codex).** `_parse_line` updated
>   `_last_causal_ts` on the boundary line before `_close_and_open_turn` read it; it contradicted the plan's own
>   Task-7 test. **Fixed inline:** `_parse_line` skips the update when `is_human_user`; `_close_and_open_turn`
>   resets `_last_causal_ts = line_ts` for the new turn (both the Task-7 and Task-8 code blocks).
> - **P1-B — same-object byte-0 re-read phantom-closed with `turn_index=0` (grok).** The poll-top discontinuity
>   check only poisoned the clock, leaving `_turn_open=True`, so the replayed opening ran `_close_and_open_turn`
>   and emitted a spurious `turn_completed` at the just-zeroed ordinal. **Fixed inline (Task 8 poll-top):** clear
>   `_turn_open` on discontinuity → the replayed opening opens a FRESH turn; the abandoned turn emits no
>   `turn_completed` ⇒ NULL. Test vi strengthened to assert NO `turn_completed` edge is emitted for the straddled
>   turn (not merely `is not True`).
> - **P1-C — dispatch `tool_call_id` never wired to codex/agy (codex).** v1 wired the helper only into claude-tail;
>   codex/agy emit only `item_id`, which isn't allowlisted → their eval tool edges had no `tool_call_id`. **Fixed
>   inline (Task 10 Step 3):** central coalescing in `push_task_event` for tool edges (covers codex/agy/cursor/pi).
> - **P1-D — prefix-commit persisted the advanced ordinal with the pre-line offset (codex).** **Fixed inline
>   (Task 7 poll offset-commit):** snapshot `ordinal_at_line_start`; prefix-commit / truncate-heal persist the
>   PRE-line ordinal, only the end-of-poll commit persists the advanced one.
> - **P1-E — deny-proofs were vacuous (codex).** See the Deny-proof discipline constraint above. **DELETE** these
>   four assert-the-defect tests and replace each with a delete-to-red step on the corresponding POSITIVE test:
>   | Deleted vacuous test | Guard line to delete-to-red | Positive test that must RED |
>   |---|---|---|
>   | `test_denyproof_sent_at_fallback_would_break_missing_ts` (Task 3) | **body mutation (r1):** replace the `_stamp_event_ts` else-branch (omit + counter) with the forbidden `data["event_ts"] = line_ts or _now()` fallback — NOT a pure counter-delete (which reds on the counter, not the substitution) | `test_absent_timestamp_omits_event_ts_and_bumps_counter` |
>   | `test_denyproof_unpersisted_turn_index_collides_on_restart` (Task 7) | **arg-mutation (r1):** make the end-of-poll commit `store(key, new_offset, 0)` (drop the ordinal) — NOT a pure `store(...)` delete (which kills offset persistence too — collateral) | `test_turn_index_is_restart_stable_across_nonzero_offset_resume` |
>   | `test_denyproof_i_…` + `test_denyproof_iii_…` (Task 8) | (a) the full-stream `_observe_clock` call in `_emit_events` → `test_i_`/`test_ii_`; (b) the eager `_turn_clock_ok=False` on the **generic-skip** arm → `test_iii_` (malformed JSON); (c) **(r1, P1-4)** the eager poison on the **DriftError** arm → `test_iii_drifterror_` (valid-JSON unknown `type`) — the two arms are separate guards proven by separate tests | see column 2 |
>   | `test_denyproof_per_event_incr_splits_one_execution_across_epochs` (Task 9) | **re-INCR per event on a REAL Bridge (r1, P1-3):** make `push_task_event` re-`incrby` per event instead of reading the `_task_epoch` snapshot — NOT a pure snapshot-delete (which KeyErrors — collateral) | `test_snapshot_once...` **run against a real `Bridge`** (harness copy is non-load-bearing) |
>   Each task's TDD steps keep the positive test; add one **delete-to-red** step naming the guard mutation above. The
>   consolidated delete-to-red run is recorded in Task 13 Step 3. **(r1 note:** these rows were refined in the v2→v3
>   fold — see the r1 banner. The authoritative delete-to-red instructions live in each task's own step.)
>
> **P2 folds:**
> - **pi_rpc `on_event` (agy+cold-Opus):** use the in-scope `on_event` param, not `self._on_event`. Fixed inline (Task 12).
> - **`...` test bodies REQUIRED-before-execution (all seats):** the agent_sdk/pi_rpc bodies (Tasks 11/12) and the
>   service-level Option-D tests v/vii (Task 8) MUST be filled with runnable code — instantiated from the existing
>   `tests/test_pi_rpc.py` (`self._engine()` / `_queue_on_prompt`), `tests/test_agent_sdk_engine.py` (gate/`_run_turn`
>   drivers), and `tests/claude_tail/test_service.py` (`FakeTailer`/`_write_json`) idioms — BEFORE that task's impl.
>   A worker MUST NOT leave a `...` in a committed test. Task 13 refuses green with any `...` remaining.
> - **Bridge-wired assertions (codex+grok):** Tasks 9/10 add at least one real `Bridge`-level test (not only the
>   inline harness) asserting `push_task_event` actually stamps `attempt_epoch`/`turn_index`/`tool_call_id` onto the
>   eval record, and the `process_request` `finally` pops the per-task maps.
> - **`uuid` carry (grok+cold-Opus):** spec Deliverable 4 says `_parse_line` carries `uuid` so edges correlate; the
>   v1 rewrite dropped it. Add the minimal carry (stamp `obj.get("uuid")` onto the event `data` for correlation —
>   NOT allowlisted, trace-only) to remove the spec/plan drift; it is not an eval primitive.
> - **Single-dispatch capture shape (cold-Opus P2-3) — stated explicitly:** a terminal / single-dispatch turn (no
>   next-human-`user` line) emits `turn_started` (with `turn_index`, `turn_started_ts`) but **NO `turn_completed`**
>   — the durable signal is the **absence of a true close**, not a `turn_clock_monotonic=false` edge. This matches
>   Deliverable 4 ("`finish()` drives only the finish/cleanup lifecycle" — it emits no `turn_completed`). **5a's O4
>   / O-gate reads "a turn with `turn_started` but no `turn_completed` ⇒ NULL", not "capture flag false".**
>   **RESOLVED (Mark, 2026-07-14): the spec's O-gate item (6) is now editorially patched** with the explicit
>   equivalence ("`false` is represented as the absence of a `turn_completed` edge; consumers/O4 MUST treat
>   `turn_started`-without-a-`true`-`turn_completed` as flag = false ⇒ NULL") — inoculating against a future 5a
>   r-round seat flagging spec-says-false / impl-shows-absence as a phantom contradiction (dormant wording drift
>   breeds phantom P1s). Not a scope change; both phrasings are the same fail-closed semantics. The live gate
>   confirms a cold-seat single dispatch's opening prompt carries `promptId` so `turn_started`+`turn_index` fire on
>   the bulk of eval traffic.
> - **Self-review "ONE residual" correction:** the placeholder-scan claim is updated — the `...` gap spans Tasks 8
>   (v/vii), 11, 12, now all marked REQUIRED-before-execution above.

---

## r1 panel fold (v2 → v3) — provenance + carried fixes

> **r1 plan panel** (run `panel-slice5a0-plan-r1-20260714T090955Z-3a7acc`, closed `needs-changes`): certify
> quorum codex-sol@high + agy + grok ALL needs-changes/P1; cold-Opus non-cert. **r1 CONFIRMED CORRECT (do NOT
> re-touch):** Fold A (event_ts no longer the next-prompt ts — clean path yields `19:00:01`), Fold B (byte-0
> phantom close fixed — no ordinal-0 edge, normal cursor-contiguous polls undisturbed), Fold C (dispatch
> `tool_call_id` traced end-to-end onto both tool edges, no leak to non-tool events). The **crux**
> (`turn_clock_monotonic` Option-B/D, no terminal earn) has been sound since r0 — do NOT re-litigate.
>
> **P1 folds (each verified against the cited code before folding — [[cross-slice-claims-need-citation]]):**
> - **P1-1 — same-object prefix-commit retry resurrects the duplicate `turn_completed` (codex-sol #1 + grok).**
>   The v2 P1-D fold snapshotted the persisted offset/ordinal, but `_close_and_open_turn` (Tasks 7/8) ALSO mutates
>   in-memory turn state (`_turn_open`, `_last_causal_ts`, `_turn_started_ts`, `logical_turn_index`,
>   `_turn_clock_ok`, `_turn_prev_ts`) before its final `turn_started` emission — and NONE of it is rolled back on a
>   same-object prefix-commit retry. Verified against code: the emit-stage except (`tailer.py:176-186`) prefix-commits
>   `line_start` + re-raises; the service (`service.py:166-171`) sets `state.failing=True` and re-polls the SAME
>   `self._tailers[key].tailer` object next tick (no reconstruction). Scenario: at a human-`user` boundary,
>   `_close_and_open_turn` emits `turn_completed[N]` OK, mutates state to N+1/open with `_last_causal_ts` = the
>   next-prompt ts, then the `turn_started[N+1]` emit throws a non-Redis bug → prefix-commit `(line_start, N)` +
>   propagate. On retry the store restores `logical_turn_index=N`, but the object still has `_turn_open=True` +
>   `_last_causal_ts` = next-prompt ts; if `_cursor_offset == line_start == offset` (the boundary line was the poll's
>   first line) the Fold-B discontinuity check does NOT fire → the replayed boundary line re-runs
>   `_close_and_open_turn` and emits a DUPLICATE `turn_completed` at ordinal N carrying the NEXT prompt's ts —
>   resurrecting P1-A as a retry defect. **Fix (Task 7, FENCED poll() code — grok flagged the v2 P1-D fold was
>   prose-only):** snapshot the FULL per-line turn-state tuple at line start (folding in the P1-D
>   `ordinal_at_line_start`); on the emit-stage prefix-commit AND the drift-emit prefix-commit, restore the full
>   tuple before re-raising, so the replayed boundary line reproduces `turn_completed[N]` with the CORRECT ts
>   (idempotent at-least-once). Ship a **same-object retry regression test** (Task 8) that injects an emit failure
>   after the boundary mutation and asserts no duplicate / mis-stamped `turn_completed` and no ordinal collision.
> - **P1-2 — claude-tail `tool_call_id` leaks onto non-tool events (cold-Opus).** Task 4's coalesce sat in
>   `_enrich_data`, which `_route_event` runs for ALL events (`tailer.py:314,330-335`). `_item_id` always returns a
>   non-empty string (`tailer.py:337-341`) and `canonical_tool_call_id` falls through to it — so
>   `task_started`/`turn_completed`/`task_finished`/`drift_error` each got a synthetic `tool_call_id` like
>   `"t:1:9:turn_completed"` in the durable allowlisted payload, poisoning 5a's join key AND failing Task 4's own
>   test (`set(tool_ids) == {"toolu_9"}` reds once `task_started` injects an id). The dispatch side (Task 10) is
>   already guarded by event type. **Fix (Task 4):** guard the claude-tail coalesce by event type — only
>   `command_started`/`command_finished`/`command_output` — mirroring the dispatch guard. This also makes Task 4's
>   test pass.
> - **P1-3 — Task-9/10 delete-to-red tested a COPY, not production (codex-sol #2).** The mapped positive
>   `test_snapshot_once_all_events_of_one_execution_share_one_epoch` ran entirely on the inline `_EpochMixinHarness`;
>   production snapshots in `Bridge.push_task_event`. Deleting the real snapshot-read left the harness test green
>   (mirror-harness vacuity — exactly what Fold E kills). **Fix (Tasks 9, 10):** the delete-to-red now drives a REAL
>   `Bridge` (`Bridge.__new__` pattern at `tests/test_push_task_event_tee.py:36-48`) with two `push_task_event`
>   calls, inspecting BOTH durable eval payloads. The inline harness tests stay as contract illustration; the
>   delete-to-red guard is the production snapshot-read.
> - **P1-4 — DriftError poison arm was unproven (codex-sol #3).** Task 8 poisons both the generic-skip arm
>   (`tailer.py:160-166`) and the DriftError arm (`tailer.py:132-157`), but mapped both to `test_iii`, which uses
>   malformed JSON → `JSONDecodeError` → the GENERIC arm ONLY (verified: `map_line` raises `DriftError` only for a
>   valid-JSON unknown `type`, `mapper.py:34`). Deleting the DriftError poison left `test_iii` green; an in-turn
>   valid-but-unmappable line then skips without poisoning → a later boundary could emit `turn_clock_monotonic=true`
>   on an unclean turn. **Fix (Task 8):** add a SECOND positive test with valid JSON of an unknown transcript `type`
>   (raises `DriftError`) and a SEPARATE delete-to-red row for the DriftError-arm poison.
>
> **P2 folds:**
> - **Deny-proof map rows made non-collateral (grok + codex + cold-Opus):** the r0-banner table + Task 8 map used
>   pure-line-delete framings that red for the WRONG reason. Corrected: the Task 3 row is the **body mutation**
>   (replace the omit-branch with the forbidden `sent_at`/`_now()` fallback), not a counter-delete; the Task 9 row is
>   **re-INCR per event**, not a snapshot pure-delete (which KeyErrors); the Task 7 ordinal row is an explicit
>   **arg-mutation** `store(new_offset, 0)` (a pure `store(...)` delete kills offset persistence too — collateral);
>   `test_iv` + the fresh-tailer path are **structural absence of state**, not deletable guards — REMOVED from the
>   delete-to-red map, kept as positive structural tests.
> - **Task 11's four agent_sdk test bodies filled INLINE + Task 13 placeholder-refusal (codex — overrides
>   agy/grok's "REQUIRED-before-execution is enough").** Nothing enforced "REQUIRED": pytest passes ellipsis-only
>   tests and the fixture-smoke verifier has no `...`-refusal, so a worker could commit four inert green tests.
>   Filled from `tests/test_agent_sdk_engine.py` idioms; Task 13 gains an explicit AST/grep `...`-refusal.
> - **`uuid` carry now actually implemented (agy + grok):** the r0 banner claimed spec Deliverable 4's `uuid` carry
>   was fixed but no task step stamped it. Added to Task 7 `_emit_events` (`obj.get("uuid")` onto event data,
>   trace-only — NOT allowlisted, so the eval extract drops it by construction).
> - **File Structure wording (grok F6):** `tool_call_id.py` is NOT "imported by every producer" — dispatch
>   producers are covered centrally in `push_task_event` (Task 10); only claude-tail imports it. Corrected.
> - **Task 8 Step 7 commit (agy):** dropped `tests/claude_tail/test_service.py` — Tests v/vii live in
>   `test_tailer.py` (vii) and the live gate (v); Task 8 touches no service test.
> - **5a follow-up noted, NOT fixed here (grok F5 + cold-Opus):** codex/agy `command_output` carries a divergent
>   `tool_call_id` (`item_id = f"{base}:output"`, `codex.py:383-389`) vs its start/finish base id — a PRE-EXISTING
>   output-edge join mismatch, not fold-introduced. Recorded as a 5a follow-up (Task 10 note); out of 5a-0 scope.

---

## r2 panel fold (v3 → v4) — provenance + the transaction-boundary matrix

> **r2 plan panel** (run `panel-slice5a0-plan-r2-20260714T110950Z-09abbf`, closed `needs-changes`,
> `{gaps:[],outcome:emitted}`): certify quorum codex-sol@high + agy + grok; cold-Opus non-cert. **codex + grok BOTH
> needs-changes/P1; agy + cold-Opus approve/none.** The decisive finding is CONVERGENT — codex (mechanical) and grok
> (static) independently landed on the SAME defect in the v3 P1-1 fold:
>
> **The convergent P1 — the per-line transaction omitted the continuity CURSOR.** v3 restored turn state and
> advanced the STORE on prefix-commit (`store(key, line_start, ordinal)`) but left `_cursor_offset` at its prior
> value (it is set only at end-of-poll, which a failed poll never reaches). On a **multi-line** poll where a
> non-first line's `turn_completed` emit fails, the store advances to `line_start` while the cursor stays at the
> poll's start → the retry's Fold-B discontinuity check misfires (it reads this tailer's own forward prefix-commit
> as a rewind), clears `_turn_open`, abandons the turn → the never-durable `turn_completed` is LOST (**false NULL**
> — the exact Option-D failure class the fold claims to kill). The v3 regression test only covered the FIRST-LINE
> window (`line_start == offset` → store not advanced → cursor stayed aligned), so it passed while the common
> multi-line window went untested. codex also found two more real P1s (fixture-smoke gate uncompletable; the
> `Bridge.__new__` fixture repair not staged) and P2s (DriftError loses `event_ts`; subscription-audit still
> mirror-tested; a macOS-inert `grep -rnP` in the anti-vacuity tooling; uuid not stamped on synthetic turn edges).
>
> **The pattern (Mark, 2026-07-14): the P1-1 transactional fix has now failed review in a NEW WINDOW each round**
> — r0/r1 found the in-memory-state gap, r2 found the cursor gap IN r1's fix. A defect that *moves through exit
> paths* is a boundary being discovered one leak at a time, which means **the invariant was never written down.**
> So v4 leads with a **matrix** (below) that states the post-exit invariant for every exit path × every piece of
> state; the patches cite cells; the deny-proofs map onto cells so an EMPTY cell is a visible gap BEFORE dispatch,
> not an r3 finding after. This is the guard→test-map discipline applied to a transaction boundary. **r3's brief
> asks for CELL COMPLETENESS (verify every cell, hunt exit paths the matrix omits), not patch correctness** — and
> keeps BOTH execution and static/mechanical seats, because matrix-completeness review is where the static seats
> earned their keep (execution seats cleared the narrow window; static seats found the wide one).

---

## r3 panel fold (v4 → v5) — the transaction REFACTOR (structure over discipline)

> **r3 matrix-completeness re-panel** (run `panel-slice5a0-plan-r3-20260714T115904Z-0a6329`, closed **unanimous
> needs-changes**, `{gaps:[],outcome:emitted}`). The matrix did its job — it converted "the defect keeps moving"
> into labeled findings — but r3 surfaced the deeper truth: across FOUR rounds (r0→r3) every P1 has been a
> **coherence failure between coupled state pieces** (store vs cursor; in-memory vs committed; first-line vs
> multi-line windows) — NEVER a defect in what `poll()` semantically does. The diagnosis is **"N writers to
> coupled state scattered across exit paths."**
>
> **The tripwire fired — by its SPIRIT (Mark, 2026-07-14).** The seats split 2–2 on whether the drift-threshold
> cell is `in-cell` (codex + grok → rewrite) or a benign truncate-heal-twin (cold-Opus + agy → refine). **That
> split IS the trip:** when the best reviewers can no longer cheaply agree whether a defect is even *inside* the
> enumerated boundary, the mechanism has exceeded reviewable complexity — the exact condition the tripwire exists
> to detect, regardless of which branch technically fired. This split is recorded here as **tripwire-fired, not
> author-downgraded** (so the rewrite-camp seats' finding is honored, not silently overruled).
>
> **Decision (Mark): structural REFACTOR, not a from-scratch rewrite.** A full `poll()` rewrite would discard four
> rounds of panel-verified semantics (emit ordering, drift detection, truncate-heal — all repeatedly confirmed
> sound) to fix a *structure* problem, and hand r4 a large novel diff to re-verify from zero. Instead, two
> structural moves make the divergence classes **unrepresentable** while inheriting the verified semantics:
> 1. **`_commit(offset, ordinal)` — the SOLE writer of the persisted offset AND the in-memory cursor.** Binding
>    them in one method makes clause-1 (`cursor == store.offset`) impossible to violate by construction — no exit
>    path can advance one without the other. The clause-1 divergence class (r2, drift-threshold) becomes
>    unrepresentable. `_cursor_offset`/`_cursor_inode` become PRIVATE with no other assignment site; a census greps
>    for rogue writers (and the census is itself red-proofed, per the macOS lesson).
> 2. **An outer poll-level snapshot/restore guard** makes turn-state mutation transactional AT THE BOUNDARY: any
>    exit that did not commit restores the poll-start snapshot, so mutated turn-state can never survive a failed
>    poll (kills the r1 class AND the r3 OSError missing-row by construction). Because a restore guard is a
>    *discipline* mechanism (it only protects paths that raise through it), its deny-proofs must cover the SNEAKY
>    exits — an early `return` mid-loop, an exception swallowed *inside* the loop — where its residual risk lives.
>
> **Every one of the four rounds' findings folds INTO one of the two structures** (the test that the refactor is
> shaped right): the r2 cursor bug + the drift-threshold cell → `_commit`'s single write path; the r1 mutated-state
> bug + the r3 OSError row → the guard's restore set. If any finding had no home in either, that would signal a
> THIRD structure not yet identified — none was found while folding.
>
> **The matrix SURVIVES, transposed** (next section): most cells now collapse to "holds by construction via
> `_commit`" or "holds via the boundary guard"; the cells that STILL need per-path reasoning are the honest
> residue and become r4's review target. If the residue exceeds a couple of cells, the refactor did not go deep
> enough — caught here, before dispatch.
>
> **r4 tripwire, RE-REGISTERED (Mark, pre-committed so r4 decides mechanically):** if r4 finds a **coherence-class
> P1 AFTER this structural collapse** — a divergence between state pieces that `_commit`/the guard was supposed to
> make impossible — that is **no longer refine-vs-rewrite: it is the full `poll()` rewrite, no third chance.** A
> genuinely NEW class (not coherence-between-state-pieces) or a missing row remains normal refinement.
>
> **Also folded (agreed r3 findings, outside the transaction):** Bridge `__new__` clean-checkout via **class-level
> `_task_epoch`/`_task_turn_index` defaults** (agy + codex — mirrors `_live_redis`; makes the AttributeError
> unrepresentable rather than staging 13 fixture edits); the AST anti-vacuity census **file list derived from the
> committed diff** (codex — it scanned only 5 of the touched suites); Open Question 2 deleted as vestigial (agy);
> the truncate-heal × cursor deny-proof gap closed (grok).

---

## r4 panel fold (v4/v5 → v6) — structure HELD; the fold absorbs the r4 windows

> **r4 structural re-panel** (run `panel-slice5a0-plan-r4-20260714T124814Z-a210dc`, closed **needs-changes**,
> `{gaps:[],outcome:emitted}`; agy approve/none, codex-sol + grok + cold-Opus needs-changes/P1). **ALL FOUR
> seats confirmed the two primitives hold in the authoritative `poll()`, and NO seat filed a coherence-class
> P1 — the re-registered tripwire did NOT fire.** Mark ruling (2026-07-14): cold-Opus's store-vs-memory flag on
> its truncate-heal finding was weighed and the **missing-row classification STANDS** — the defect is a
> fold-introduced regression inside the NAMED residue exemption, not a leak through the primitives — recorded
> **with the severity acknowledged**: on consequences it was the round's scariest finding (silent lost turns +
> stale ordinals — exactly the wrong-data class this slice exists to prevent). Severity and classification are
> different axes; the classification is about where the fix lives, and it lives in the collapsed region.
> **Tripwire clauses carry to r5 UNCHANGED as re-registered.**
>
> Five verified P1s (each hinge-claim traced by the orchestrator against plan `a1e4a4f` + the real code BEFORE
> the audit close), folded:
> 1. **(grok) Step 7's inner-restore delete-to-red went VACUOUS under the v5 outer guard.** In the first-line
>    window `committed == False`, so the outer guard performs the IDENTICAL restore — deleting the inner
>    restore stayed green. Re-keyed to the **multi-line committed-prefix window**, where the inner restore is
>    the ONLY protection (`committed=True` ⇒ the outer guard cannot mask): fail on `turn_started` AFTER a
>    durable `turn_completed`. **New vacuity variant for the corpus: red-made-REDUNDANT-by-a-later-guard** —
>    the structural fix itself vacated an existing deny-proof. **Standing rule (Mark, 2026-07-14): any fold
>    that adds or widens a guard MUST re-run the delete-to-red suite for every OTHER guard whose protected
>    window the new guard overlaps** — overlapping protection is exactly what makes existing proofs silently
>    vacuous.
> 2. **(grok) The sole-writer census missed `OffsetStore.commit()` — the worst-direction gap:** today's
>    production `poll()` is ALL `.commit()`, so the exact partial-migration drift the census exists to catch
>    would have passed it (a census green on the unmigrated present is vacuous in the dangerous direction). v6:
>    the census is built from the **SWEPT write surface** (every callsite touching store/cursor/turn-state/
>    ordinal, including `offset.py` itself), and the back-compat `get()`/`commit()` receive an **EXPLICIT
>    FATE**: deleted in Task 7 Step 3c once `poll()` migrates (grep-proved no callers remain), and
>    census-forbidden against re-introduction with their own planted-rogue red-proof.
> 3. **(cold-Opus, missing-row) v5's truncate-heal DROPPED the store-persist the current code has**
>    (`tailer.py:94` commits 0 at heal; v5 made the reset in-memory only): a zero-complete-lines heal poll
>    never reaches the end-of-poll `_commit`, the stale offset survives in the store, and once the file regrows
>    past it the heal never re-derives → mid-stream seek → silently lost turns + stale ordinals (the
>    "idempotent re-derive" claim only held while the file stayed short). v6: the heal **explicitly abandons
>    the open turn, then `_commit(key, 0, 0, st_ino)`s AT the heal** — the structure absorbs the old residue
>    cell (the abandon no longer rides the discontinuity check; the persist is back, and idempotent). New Step
>    7g proves the zero-lines/regrowth window; Step 7f's delete-to-red is re-keyed to the explicit abandon.
> 4. **(codex, missing-row) `OffsetStore.load()`'s legacy/corrupt `_reset → store(key, 0, 0)` was a
>    persisted-position writer outside `_commit`**, executed at poll-top pre-guard, in a file the census never
>    scanned. v6: **`load()` is PURE** — legacy/corrupt reads return `Position(0, 0)` with NO write; the
>    recount is re-derived per poll until the poll's own `_commit` persists the composite (same idempotency
>    argument as the heal, now made true). The census sweep covers `offset.py`: no redis write outside
>    `store()`.
> 5. **(codex) The drift-emit-FAILURE arm had no flipping deny-proof** (`test_iii_drifterror…` exercises a
>    SUCCESSFUL drift emission — the success arm only). New Step 7h: two-phase, one-shot `_emit_drift_error`
>    failure; delete-to-red = arg-mutate the arm's prefix `_commit` (the failed drift line is then skipped on
>    retry → the durable `drift_error` is lost forever → red). The arm's snapshot/restore is the same mechanism
>    as the emit arm (proven by the re-keyed Step 7); its independently-flippable halves are the commit args +
>    the cursor binding — reasoning recorded inline in Step 7h for r5 to check.
>
> **P2 batch folded:** a future mid-loop `return` would bypass the `except`-shaped outer guard → the Step 7c
> census gains a no-`return`-inside-`poll()`'s-guarded-`try` check, red-proofed; the census recurses into
> tuple-assignment targets and flags constant-name `setattr` (codex EXECUTED the v5 census and produced these
> escapes); Task 9's class-default dicts are WRITTEN by `_stamp_turn_index`/`_allocate_attempt_epoch` → writers
> lazily shadow an instance dict on first write (the "read-only" claim was false for `__new__` fixtures); the
> Task 7 provenance blocks that show a heal-persist are annotated (their shape is v6's again, by a different
> route — abandon + `_commit`, not a bare store write).
>
> **r5 asks (beyond verifying the folds):** **per-ARM coverage** — verify the deny-proof map covers BOTH arms
> of every branch in each matrix row (success arm AND failure arm), not just each row once; finding 5's shape
> generalized. Tripwire unchanged: a coherence-class P1 surviving the collapse ⇒ full `poll()` rewrite, no
> third chance.

---

## r5 panel fold (v6 → v7) — folds verified; four refine-class P1s on the fold's OWN new arms

> **r5 verification panel** (run `panel-slice5a0-plan-r5-20260714T132252Z-e90744`, closed **needs-changes**,
> `{gaps:[],outcome:emitted}`; agy + grok + cold-Opus approve/P2, codex-sol needs-changes/P1). **ALL FOUR seats
> confirm the five r4 folds close their mechanisms, the two primitives hold, and NO coherence-class P1 exists —
> the tripwire again does not fire** (unanimous on that; clauses carry to r6 UNCHANGED). The per-arm sweep ran
> on every seat (enumerations in the reports). codex-sol's four P1s — each hinge-verified (two by the seat
> EXECUTING the case) — are all arms opened or left unproven by the v6 fold itself:
> 1. **(P1, missing-arm) v6's pure `load()` opened a corrupt-composite STALL window.** `raw.decode()` sits
>    OUTSIDE the try (invalid UTF-8 ⇒ uncaught `UnicodeDecodeError` every poll) and negative ints pass the
>    validation then raise on `seek(-1)` — and because pure `load()` never overwrites and the poll dies before
>    any `_commit`, the corrupt value is PERMANENT sticky-fail (v5's eager reset self-healed it). v7: decode
>    and domain-validate INSIDE the corrupt-handling (non-UTF-8, negative offset/ordinal ⇒ `Position(0, 0)`),
>    plus both purity tests.
> 2. **(P1, new-class) `drift_count` is the non-idempotent pre-emit state Step 7h's reasoning missed.** The arm
>    increments BEFORE emitting; the failure restore covers only `_TURN_STATE_FIELDS`; the retry re-parses the
>    line and increments AGAIN — one line counted twice, threshold crossable a line early, durable event
>    carrying `count=2`. (agy/grok/cold-Opus converged on the `_last_causal_ts` counterexample, which all three
>    correctly resolved as replay-IDEMPOTENT — codex found the state that is not.) v7: the failure arm rolls
>    the increment back (`drift_count -= 1` before restore+commit); Step 7h asserts `count == 1`.
> 3. **(P1, missing-arm) heal crash-window shrunk by reordering:** v7 makes `_commit(key, 0, 0, st_ino)` the
>    FIRST side effect after detecting truncation, then the (non-throwing) in-memory abandon — a crash between
>    the two can no longer leave the stale offset durable. **Honest residual, RECORDED in the crash row:** a
>    crash BEFORE any heal poll runs, followed by regrowth past the stale offset before restart, is inherent to
>    polling an externally-truncatable file and is NOT closable at the tailer (the same lost-prefix outcome;
>    external-race class, surfaced not solved).
> 4. **(P1, missing-arm — the guard-overlap standing rule catching the v6 fold itself)** heal followed by an
>    uncommitted failure had NO flipping proof, and the natural drift (`committed = True` at the heal) would
>    leak mutated new-generation turn-state past the outer guard. v7: new Step 7i (heal-with-lines → boundary
>    mutation → one-shot OSError). *(v8 correction, grok r6 P1: mutation (a) as first stated was DEAD CODE —
>    the `committed = False` init sits after the heal and clobbers it — and the predicted symptom was wrong;
>    Step 7i now carries the corrected full mutation and the phantom ordinal-0 symptom.)*
>
> **P2 batch folded:** census gains `offset.py` `self.store(...)`-outside-`store()` + `self.__setattr__` +
> tailer-side `offset_store.redis` reach-through classes, each red-proofed (codex/grok); Task 6's interface
> sentence rewritten to match purity (codex); deny-proof map gains the outer-guard **skip-when-committed** arm
> row (mutate `if not committed` → always-restore ⇒ Step 7 multi-line reds — grok) and RECORDS the emit-stage
> `RedisError` arm as structural/out-of-scope-of-unit-red (re-raise with no commit; process dies via `run_loop`
> — grok); Step 7h's recorded reasoning REWRITTEN per the three-seat convergence (causal-ts mutation exists but
> is re-parse-idempotent; the non-idempotent state was `drift_count`, now rolled back + asserted); new
> missing-timestamp clock-arm deny-proofs (in-turn record and opening record without `timestamp` ⇒ flag false;
> delete the poison / `bool(line_ts)` ⇒ forged-`true` reds — cold-Opus); `_close_and_open_turn` bumps
> `claude_tail_missing_ts` for its synthetic edges (they bypass `_stamp_event_ts` — agy).

---

## r6 panel fold (v7 → v8) — narrow round; three authoring/propagation P1s + one input class

> **r6 narrow verification** (run `panel-slice5a0-plan-r6-20260714T134057Z-f9a8f7`, closed **needs-changes**,
> `{gaps:[],outcome:emitted}`; cold-Opus approve/P2, agy + grok + codex-sol needs-changes/P1). **All four seats
> confirm the five v7 folds close their r5 mechanisms; NO coherence-class P1 — the tripwire does not fire, third
> consecutive round.** The r6 P1s are fold-authoring defects plus one input class, all folded here:
> 1. **(agy P1)** Task 8 Step 3's `_close_and_open_turn` block omitted the `claude_tail_missing_ts` bump that
>    Step 7j's prose + test require — plan-internal propagation gap; the block now carries it (+ its own
>    delete-to-red (c) in Step 7j, per codex's P2).
> 2. **(grok P1; adjudicated against cold-Opus's contrary read by tracing the block)** Step 7i's delete-to-red
>    (a) was VACUOUS as written — `committed = False` initializes AFTER the heal, clobbering the mutation.
>    Corrected to the full drift-simulating mutation (init above the heal + set True at heal) with the corrected
>    symptom (phantom ordinal-0 close, per the grok/cold-Opus convergence — the poll-top logical rebind clobbers
>    the leaked ordinal). The inviting "ANY `_commit` this poll" comment is rewritten.
> 3. **(codex P1)** deeply-nested corrupt JSON raises `RecursionError` out of `json.loads` on the deploy Python
>    (3.11, reproduced) — one more permanent-stall input under pure load. `RecursionError` added to the except
>    tuple + a runtime-independent depth-100k test.
> 4. **(codex P2, folded with 3)** `data.get("v") != 1` accepted `True`/`1.0` — exact-int type check + tests.
> 5. **(grok/codex P2)** three stale abandon-before-`_commit` wordings from the v7 reorder sweep reconciled
>    (residue-map row, provenance bullet, 7f comment).

---

## r7 panel fold (v8 → v9) — micro round; one runtime-portability P1 + the ordering-prose stragglers

> **r7 micro verification** (run `panel-slice5a0-plan-r7-20260714T135445Z-7005a8`, closed **needs-changes**,
> `{gaps:[],outcome:emitted}`; agy approve/none, grok + codex-sol needs-changes/P1, cold-Opus needs-changes/P2).
> Items 1–4 of the v8 fold verified by all seats; **NO coherence-class P1 — fourth consecutive round.**
> 1. **(grok + codex P1, both EXECUTED it; orchestrator reproduced on the repo venv Python 3.14.5):** the
>    depth-100k RecursionError deny-proof was runtime-DEPENDENT — 3.14 parses 100k clean (raises only ~500k)
>    while the 3.11 deploy raises at ~2k, so the catch-arm delete-to-red was silently vacuous on the local
>    suite runtime. v9: depth 500k + a `pytest.raises(RecursionError)` PRECONDITION on the raw `json.loads`,
>    making the probe self-proving (a future deeper-parsing runtime fails LOUDLY instead of the guard going
>    quietly vacuous). codex's monkeypatch alternative rejected ([[fixture-supplies-what-code-lacks]]).
>    cold-Opus's "runtime-independent" claim was adjudicated wrong by execution.
> 2. **(grok/codex/cold-Opus P2):** four implementer-facing prose sites still stated abandon-before-`_commit`
>    against the authoritative v7 commit-first order (the v8 sweep was demonstrably incomplete) — all
>    reconciled: the clause-1 invariant, the composite-read provenance note, the `_commit` docstring note, and
>    the Task 8 cursor-recording note. Every non-historical mention now reads commit-FIRST-then-abandon.

---

## Transaction boundary — the `poll()` per-line consistency matrix (LOAD-BEARING; tasks + deny-proofs cite it)

> **This matrix IS the invariant.** `poll()` mutates four pieces of state across a per-line loop; every exit path
> must leave them mutually consistent. **Columns** = the four state pieces. **Rows** = every exit path. Each cell
> states what must hold *after* that exit. Tasks 7/8 implement the cells; the deny-proof map (end of this section)
> keys one delete-to-red to each load-bearing cell.

State pieces (columns):
- **store** — the persisted composite `{offset, turn_index}` in Redis (`OffsetStore`); the ONLY state a fresh tailer reads on restart.
- **cursor** — in-memory `(_cursor_inode, _cursor_offset)`; the discontinuity-check baseline. **Not persisted.**
- **turn-state** — the in-memory non-ordinal turn fields `_turn_open`, `_turn_started_ts`, `_last_causal_ts`, `_turn_clock_ok`, `_turn_prev_ts`. **Not persisted.**
- **ordinal** — `logical_turn_index`; lives BOTH in memory and in the store composite; the invariant is they agree.

| Exit path | store | cursor | turn-state | ordinal |
|---|---|---|---|---|
| **emit-success** (end-of-poll, all lines emitted) | `_commit(new_offset, logical)` | **set by `_commit`** (bound to store) | as-processed; no restore | `logical` advanced per boundaries; store.turn_index == in-memory |
| **emit-fail** (non-Redis bug, prefix-commit) | `_commit(line_start, ordinal_at_line_start)` **iff** `line_start != offset` | **set by `_commit`** — bound to store, so the r2 divergence is UNREPRESENTABLE | **inner-arm restore** to the line-start snapshot (r1 fix) | restored to `ordinal_at_line_start`; store.turn_index == in-memory |
| **drift-fail** (DriftError emit throws, prefix-commit) | `_commit(line_start, ordinal_at_line_start)` | **set by `_commit`** | inner-arm restore; eager clock-poison rolled back, RE-applied on replay (line re-raises DriftError) | restored; consistent |
| **drift-threshold** (`drift_count > threshold` → `_commit` then raise; **sticky-fail, SAME object** — v5 corrects the false "restart" label) | `_commit(new_offset, logical)` | **set by `_commit` → clause-1 HOLDS → NO false discontinuity** (v5 fixes the r3 in-cell finding) | **KEPT** (advanced past the drift, clock poisoned); committed ⇒ the outer guard skips restore | == store; forward progress |
| **truncate-heal** (`offset > st_size`, poll-top, BEFORE the discontinuity check) | **`_commit(key, 0, 0, st_ino)` AT the heal** (v6 — restores the persist v5 dropped; closes the r4 zero-lines/regrowth window: even a heal poll that reads ZERO complete lines has already persisted the reset, so regrowth past the old offset can never mask it) | **set by `_commit`** — the v5 stale-cursor exemption is GONE | **EXPLICITLY abandoned AT the heal** (`_turn_clock_ok := False; _turn_open := False`, immediately AFTER the `_commit` — v7 reorder: the persist is the FIRST side effect, so a crash between the two cannot leave the stale offset durable) — no longer rides the discontinuity check | `logical := 0`, persisted by the same `_commit` |
| **poll-top load — legacy/corrupt position** (v6 row; was the r4 unmodelled writer) | **unchanged — `load()` is PURE** (v6): legacy bare-int / corrupt composite → in-memory `Position(0, 0)`, NO write; the recount is re-derived per poll until this poll's own `_commit` persists the composite (crash before it ⇒ re-derive — idempotent) | unchanged (a same-object byte-0 re-derive leaves the cursor stale ⇒ the discontinuity row fires) | untouched at load; an open turn straddling the re-derive is abandoned by the discontinuity row | `logical := 0` in-memory on the reset path |
| **discontinuity** (poll-top: `_turn_open` AND `(st_ino,offset) != cursor`) | unchanged | unchanged (set at the poll's eventual `_commit`) | `_turn_open := False`, `_turn_clock_ok := False` (abandon; replayed opening opens FRESH) | unchanged; a GENUINE discontinuity coincides with `offset==0`/inode-key-reset ⇒ `logical==0` ⇒ idempotent re-count. **v6: fires only on genuine external discontinuities** (key-delete byte-0, inode swap, external rewind) — truncate-heal abandons explicitly and no longer relies on this row |
| **uncommitted exit** (readline/tell/budget/`OSError`/any exception with NO inner-arm commit — v5 adds this row; was the r3 missing-row) | unchanged (no `_commit` reached) | unchanged (no `_commit`) | **outer boundary guard restores the POLL-START snapshot** → the r1/r3 mutated-survivor class is UNREPRESENTABLE | unchanged; == store (poll-start) |
| **crash mid-transaction** | ONLY persisted piece; `_commit`'s store write is a single atomic Redis `SET` (offset+turn_index together) — **no torn write, no cross-write window**. **Recorded external-race residual (v7, codex r5):** a crash BEFORE any heal poll observes a truncation, followed by regrowth past the stale offset before restart, resumes mid-stream (lost prefix ⇒ NULL-class outcomes, no phantom close) — inherent to polling an externally-truncatable file; NOT closable at the tailer; surfaced, not solved | not persisted → gone; fresh tailer cursor=None | not persisted → gone; in-flight turn ⇒ **NULL** (Option-B) | persisted atomically inside the store composite |

**The post-exit INVARIANT — now ENFORCED BY STRUCTURE, not per-cell discipline (v5):**
1. **`cursor == (st_ino, store.offset)`** after every non-restart exit — **unrepresentable to violate** because `_commit(offset, ordinal)` is the SOLE writer of both the store offset AND the cursor; no exit path can advance one without the other. **v6: NO exemptions remain** — truncate-heal now routes through `_commit` too (its FIRST side effect; the explicit abandon follows — v7 order), and `load()` is pure, so EVERY persisted-position write in the process is either `_commit` (tailer side) or `OffsetStore.store()` itself (the primitive `_commit` calls — census-swept in `offset.py`: no redis write outside `store()`). The back-compat `get()`/`commit()` are deleted in Task 7 Step 3c and census-forbidden. A poll-top `cursor != (st_ino, offset)` mismatch can therefore ONLY mean a genuine external change. The r2 bug, the r3 drift-threshold finding, and the r4 heal/load windows are ALL closed here.
2. **turn-state never survives a poll mutated-and-uncommitted** — the **outer boundary guard** restores the poll-start snapshot on any exit that did not commit; the inner arms restore the line-start snapshot on committed (prefix-commit) exits. So the r1 mutated-state class and the r3 OSError missing-row are BOTH closed by construction — not by remembering to add a case per exit path.
3. On any RESTART/CRASH exit: only the store composite survives (single atomic `SET`); cursor + turn-state are process-local and lost; the in-flight turn becomes NULL (Option-B, recovery = 5a O5). No persisted cursor ⇒ **no store↔cursor cross-write window** by construction.

**Residue (the honest per-path reasoning that structure could not absorb — r5's review target):** the **ABANDON
mechanism at its two sites** — the explicit heal-abandon (deny-proof: Step 7f re-keyed) and the discontinuity
abandon (deny-proof: `test_vi`). Every store/cursor cell now holds by construction via `_commit` (clause 1), and
every turn-state cell via the inner arms + the boundary guard (clause 2). If a reviewer finds a THIRD site that
still needs per-path reasoning, the refactor did not go deep enough.

**Ordering (load-bearing for the residue rows):** at poll-top the sequence is **composite-read (pure) →
truncate-heal (`_commit(key, 0, 0, st_ino)` FIRST, then the explicit abandon — v7) → discontinuity check →
per-line loop.**
`_turn_open` is cleared (heal-abandon or discontinuity row) BEFORE any line is emitted, so a truncated/rewound
generation never emits against stale turn-state. The heal's `_commit` runs BEFORE the guard's poll-start
snapshot is taken and does NOT set `committed` — a later uncommitted failure still restores the (post-abandon)
poll-start turn-state, and the heal's persist stands on its own idempotency.

**Tripwire status:** the r3 2–2 split is recorded as **tripwire-FIRED** (v5 banner) — it drove the refactor.
**r4 outcome: the re-registered tripwire did NOT fire** — all four seats confirmed the primitives hold and filed
zero coherence-class P1s (v6 banner records Mark's ruling, including the severity acknowledgment on the
truncate-heal finding). **The clauses carry to r5 UNCHANGED:** a coherence-class P1 that survives the
`_commit`/guard collapse ⇒ full `poll()` rewrite, no third chance; a genuinely new class or missing row ⇒ normal
refinement.

### Deny-proof → cell map (guard→test discipline, applied to the boundary)

In v5 most cells hold **by construction** (via `_commit` or the guard), so their deny-proof is a **structural**
proof (the primitive is the sole writer / the guard covers the exit), plus the end-to-end behavior tests that
would red if the primitive regressed. Only the two **residue** cells keep classic per-path delete-to-reds. **An
empty row here is a visible gap BEFORE dispatch** (the r2 lesson).

| Matrix cell(s) | How it holds | Deny-proof (Task) |
|---|---|---|
| **clause-1** (emit-success/emit-fail/drift-fail/drift-threshold/truncate-heal × store+cursor) | `_commit` is the SOLE writer of store+cursor (v6: heal included; NO exemptions) | **Swept-surface sole-writer census** (T8 Step 7c): an AST census over the FULL write surface asserts writes of `_cursor_offset`/`_cursor_inode`, `offset_store.store(...)` calls, AND the forbidden back-compat `.commit(`/`.get(` calls appear ONLY inside `_commit` (`__init__` None-init excepted; tuple targets + constant-name `setattr` recursed; `offset.py` swept — no redis write outside `store()`); red-proofed by always-on planted-rogue tests per class. PLUS end-to-end `test_multiline_poll_emit_fail_on_close_does_not_drop_turn_completed` (T8 7b) + `test_drift_threshold_keeps_cursor_aligned_no_false_null` (T8 7d) — remove `_commit`'s cursor half → both red |
| **clause-2** (uncommitted-exit / OSError × turn-state) — RESTORE arm | outer boundary guard restores poll-start | `test_readline_oserror_midpoll_does_not_replay_mutated_turn_state` (T8 Step 7e) — delete the outer-guard restore → duplicate/mis-stamped close on retry reds. Sneaky-exit coverage: `test_readline_oserror_after_inloop_skip_still_restores_poll_start` — an in-loop generic-skip before the OSError (guard must still restore) |
| **clause-2 — SKIP-when-committed arm** (v7 row, grok r5) | `if not committed` gates the restore OFF after a consistent prefix-commit | mutate the guard to ALWAYS restore (drop `if not committed`) → `test_multiline_committed_prefix_inner_restore_not_masked_by_outer_guard` (Step 7) reds (restore desyncs turn-state from the committed prefix — wrong ordinal/`event_ts` on the replayed close) |
| heal × later-uncommitted-failure (v7 row — the guard-overlap rule applied to v6's own fold) | heal's `_commit` does NOT set `committed`; guard snapshot taken AFTER the heal-abandon | `test_heal_then_uncommitted_failure_restores_post_abandon_state` (T8 Step 7i) — (a) move the `committed = False` init above the heal AND set `committed=True` at the heal's `_commit` (v8: the naive at-heal set is dead code — the init clobbers it) → phantom ordinal-0 close reds; (b) move the snapshot before the abandon → phantom gen-1 close at ordinal 0 reds |
| clock predicate — missing-`timestamp` arms (v7 rows, cold-Opus r5) | `_observe_clock` missing-ts poison; open-arm `_turn_clock_ok = bool(line_ts)`; synthetic edges bump `claude_tail_missing_ts` (agy) | `test_missing_timestamp_on_in_turn_record_is_false` + `test_missing_timestamp_on_opening_record_is_false_and_counted` (T8 Step 7j) — delete the poison / mutate the open-arm to `True` → forged `true` reds |
| emit-stage **`RedisError` arm** — structural, RECORDED out-of-scope-of-unit-red (v7, grok r5) | re-raises with NO commit and NO per-line handling; the process dies via `run_loop` (crash-mid row semantics: store unadvanced, in-memory state gone) | structural — the uncommitted-exit row's guard covers the in-process path; the crash-mid row covers the death path; a unit red would require injecting infra failure mid-emit for a path whose contract is "crash" (live-gate territory, Task 13 Step 4) |
| emit-fail × **turn-state** (r1; re-keyed in v6 — the r4 grok finding) | inner-arm restore to line-start; **only load-bearing in the multi-line committed-prefix window** (`committed=True` ⇒ the outer guard cannot mask) | `test_multiline_committed_prefix_inner_restore_not_masked_by_outer_guard` (T8 Step 7, re-keyed) — delete the inner restore → phantom turn-2 close on retry. The v5 first-line test survives as a POSITIVE at-least-once test only (its delete-to-red is VACUOUS by design: the outer guard covers that window) |
| emit-fail × **store**/ordinal | `_commit(line_start, ordinal_at_line_start)` | `test_turn_index_is_restart_stable_…` (T7) — arg-mutate `_commit(new_offset, 0)` → restart collision |
| drift-fail (SUCCESS arm) × **turn-state** (clock) | eager DriftError-arm `_turn_clock_ok=False` | `test_iii_drifterror_unmappable_line_in_turn_is_false` (T8) |
| drift-fail (**FAILURE arm** — v6 row, codex r4) × store/ordinal/cursor | inner drift-arm restore + `_commit(line_start, ordinal_at_line_start)` | `test_drift_emit_failure_prefix_commits_and_replays_drift_line` (T8 Step 7h) — arg-mutate the arm's `_commit` to `(new_offset, logical)` → the failed drift line is skipped on retry → durable `drift_error` lost → red. Restore half shares the emit-arm mechanism (Step 7); cursor half shares the clause-1 mutation (7b/7d) |
| **residue:** discontinuity × turn-state | poll-top `_turn_open=False` abandon | `test_vi_same_object_byte0_reread_…` (T8) |
| **residue:** heal-abandon × turn-state (v6 re-key) | truncate-heal EXPLICITLY abandons immediately AFTER its `_commit` (v7 order: persist first) | `test_same_object_truncate_heal_abandons_open_turn` (T8 Step 7f, re-keyed): delete the explicit abandon at the heal → the `_commit`-aligned cursor keeps the discontinuity row silent → a phantom (possibly forged-`true`) close appears → reds |
| truncate-heal × **store** (v6 — the r4 cold-Opus window) | `_commit(key, 0, 0, st_ino)` AT the heal (persist survives a zero-lines poll) | `test_truncate_to_empty_persists_heal_before_regrowth` (T8 Step 7g) — mutate the heal back to in-memory-only (the v5 shape) → regrowth past the stale offset resumes mid-stream → red |
| **poll-top load** (legacy/corrupt) | `load()` is PURE (no write site exists) | structural — census sweep of `offset.py` (no redis write outside `store()`) + `test_legacy_bare_int_offset_forces_recount_not_index_zero_resume` (T7) + purity asserts in T6's legacy/corrupt tests |
| crash-mid × store atomicity | single atomic `SET` | structural — `test_store_writes_versioned_json` (T6) |

If a reviewer finds a load-bearing cell (or a residue cell) with no row here, that is a **pre-dispatch gap**.

---

## File Structure

Files this plan creates or modifies, by responsibility:

- **`src/agent_redis_bridge/tool_call_id.py`** *(create)* — the single `canonical_tool_call_id(data)` coalescing helper (Deliverable 1). **Imported by claude-tail** (Task 4, guarded to tool edges); the **dispatch producers are covered centrally in `push_task_event`** (Task 10) — NOT via a per-engine import (grok F6). Either way the SAME id lands on both tool edges.
- **`src/agent_redis_bridge/eval_tee.py`** *(modify `:10-19`)* — add the five new allowlist members (Deliverable 5).
- **`src/agent_redis_bridge/claude_tail/offset.py`** *(modify)* — the versioned `{v, offset, turn_index}` composite position value + legacy bare-int forced-recount migration (Deliverable 4).
- **`src/agent_redis_bridge/claude_tail/tailer.py`** *(modify)* — logical-turn lifecycle, `event_ts` capture, `attempt_epoch = 1`, the `turn_clock_monotonic` scan-continuity clock flag, `turn_index` on the eval path via the persisted composite, `tool_call_id` coalescing (Deliverable 4).
- **`src/agent_redis_bridge/claude_tail/mapper.py`** *(modify `_parse_line` support)* — carry the line's `timestamp` and `uuid` through so the tailer can stamp `event_ts` and correlate; `map_line` unchanged in what it emits (still drops pure-text user lines — the tailer injects `turn_started`).
- **Dispatch producers** (`bridge.py`, the agent_sdk + pi_rpc engines, `redis_io.py`) — `attempt_epoch` INCR source + snapshot, deterministic `turn_index`, agent_sdk/pi_rpc semantic fixes, the M2 owner-fenced `remove_processing`. *(Exact files pinned in the dispatch-side task block.)*
- **Tests:** `tests/test_eval_tee.py`, `tests/claude_tail/test_offset.py`, `tests/claude_tail/test_tailer.py`, `tests/claude_tail/test_service.py`, `tests/test_pi_rpc.py`, the agent_sdk test files, plus a new `tests/test_tool_call_id.py`.

---

## Task 1: `canonical_tool_call_id` shared helper (Deliverable 1)

**Files:**
- Create: `src/agent_redis_bridge/tool_call_id.py`
- Test: `tests/test_tool_call_id.py`

**Interfaces:**
- Produces: `canonical_tool_call_id(data: dict) -> str` — returns the first non-empty of `data["tool_call_id"]`, `data["tool_use_id"]`, `data["item_id"]` (provider ids before presentation ids); `""` if none present/non-empty. Every producer calls this and emits the result as `tool_call_id` on BOTH tool edges (`command_started` / `command_finished`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_call_id.py
from agent_redis_bridge.tool_call_id import canonical_tool_call_id


def test_provider_id_wins_over_presentation_id():
    # tool_call_id (provider) beats tool_use_id and item_id
    assert canonical_tool_call_id(
        {"tool_call_id": "call_prov", "tool_use_id": "toolu_pres", "item_id": "item_x"}
    ) == "call_prov"


def test_tool_use_id_second_precedence():
    assert canonical_tool_call_id({"tool_use_id": "toolu_1", "item_id": "item_x"}) == "toolu_1"


def test_item_id_last_resort():
    assert canonical_tool_call_id({"item_id": "item_x"}) == "item_x"


def test_empty_and_missing_yield_empty_string():
    assert canonical_tool_call_id({}) == ""
    assert canonical_tool_call_id({"tool_call_id": "", "tool_use_id": None}) == ""


def test_non_string_values_are_skipped():
    # a non-string id is not a usable correlation key; fall through
    assert canonical_tool_call_id({"tool_call_id": 123, "tool_use_id": "toolu_2"}) == "toolu_2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra arb-memory pytest tests/test_tool_call_id.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_redis_bridge.tool_call_id'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent_redis_bridge/tool_call_id.py
from __future__ import annotations

from typing import Any


def canonical_tool_call_id(data: dict[str, Any]) -> str:
    """Coalesce a provider tool id ahead of a presentation id so the SAME id lands on both tool
    edges (command_started / command_finished) for every producer. Provider ids
    (tool_call_id, tool_use_id) precede presentation ids (item_id); returns "" if none is a
    non-empty string."""
    for key in ("tool_call_id", "tool_use_id", "item_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra arb-memory pytest tests/test_tool_call_id.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/tool_call_id.py tests/test_tool_call_id.py
git commit -m "feat(5a-0): canonical_tool_call_id coalescing helper (Deliverable 1)"
```

---

## Task 2: Eval allowlist additions (Deliverable 5)

**Files:**
- Modify: `src/agent_redis_bridge/eval_tee.py:10-19`
- Test: `tests/test_eval_tee.py`

**Interfaces:**
- Consumes: nothing (foundation).
- Produces: `EVAL_ALLOWLIST` now admits `tool_call_id`, `attempt_epoch`, `event_ts`, `turn_started_ts`, `turn_clock_monotonic` through `extract_eval_payload`. `turn_index` already admitted. Every later task that stamps one of these into event `data` relies on this admission.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_eval_tee.py — append
def test_slice5a0_new_allowlist_members_pass_through():
    out = extract_eval_payload({
        "tool_call_id": "call_1",
        "attempt_epoch": 2,
        "event_ts": "2026-07-13T19:42:50.294Z",
        "turn_started_ts": "2026-07-13T19:42:40.000Z",
        "turn_clock_monotonic": True,
        "turn_index": 4,
    })
    assert out == {
        "tool_call_id": "call_1",
        "attempt_epoch": 2,
        "event_ts": "2026-07-13T19:42:50.294Z",
        "turn_started_ts": "2026-07-13T19:42:40.000Z",
        "turn_clock_monotonic": True,
        "turn_index": 4,
    }


def test_slice5a0_new_members_are_bounded_scalars_not_free_text():
    # DENY-PROOF: the extract-only contract still drops any non-allowlisted sibling that could
    # carry free text, even alongside the new members.
    out = extract_eval_payload({
        "event_ts": "2026-07-13T19:42:50.294Z",
        "message": "assistant said a secret",
        "thinking": "chain of thought",
    })
    assert out == {"event_ts": "2026-07-13T19:42:50.294Z"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra arb-memory pytest tests/test_eval_tee.py::test_slice5a0_new_allowlist_members_pass_through -v`
Expected: FAIL — `event_ts`/`attempt_epoch`/`tool_call_id`/`turn_started_ts`/`turn_clock_monotonic` are dropped, so `out` is missing them.

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent_redis_bridge/eval_tee.py — replace the EVAL_ALLOWLIST frozenset body (:10-19)
EVAL_ALLOWLIST = frozenset({
    # turn/usage metadata only — NO free text, NO command/output.
    # Bounded scalars: tool_name is an identifier (not user text); ok/exit_code/attempt are
    # bounded (bool/int) and carried by the task_finished/command_finished vocabulary. `status`
    # is intentionally EXCLUDED (plan-panel codex P2: free-ish string; not in v3's pinned list).
    "tool_name", "tool_call_count", "turn_index",
    "stop_reason", "finish_reason",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "latency_ms", "exit_code", "ok", "attempt",
    # Slice 5a-0 capture primitives (all bounded scalars: ids / ints / ISO8601 ts / one bool):
    "tool_call_id", "attempt_epoch", "event_ts", "turn_started_ts", "turn_clock_monotonic",
})
```

Leave `EVAL_SCHEMA_VERSION = "1"` unchanged (additive — Global Constraints).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra arb-memory pytest tests/test_eval_tee.py -v`
Expected: PASS — the two new tests plus all existing (`test_allowlist_has_no_raw_io_keys` still green: none of the five is a raw-IO key).

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/eval_tee.py tests/test_eval_tee.py
git commit -m "feat(5a-0): add five capture primitives to eval allowlist (Deliverable 5)"
```

---

## Task 3: claude-tail `event_ts` capture + fail-closed absent-timestamp (R2)

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/tailer.py` (`_emit_events`, new `_stamp_event_ts` helper, new `claude_tail_missing_ts` counter, `_maybe_emit_continuing` for the synthetic edge)
- Test: `tests/claude_tail/test_tailer.py`

**Interfaces:**
- Consumes: the eval allowlist now admits `event_ts` (Task 2).
- Produces: every claude-tail event that reaches the eval stream carries `data["event_ts"]` = the triggering transcript line's own top-level `timestamp` (a stable ISO8601 scalar, idempotent across re-reads). When the line lacks `timestamp`, `event_ts` is OMITTED and `self.claude_tail_missing_ts` is incremented — NEVER `sent_at`. A synthetic edge (e.g. `task_continuing`) takes `event_ts` from the last observed line's timestamp, never `_now()`.

**Design note:** `_parse_line` already returns `(events, obj)` where `obj` is the raw transcript dict — so `obj.get("timestamp")` is available in `_emit_events` without a mapper change. `event_ts` is injected into each event's `data` BEFORE routing, so the allowlist extract carries it to eval (a bounded ISO8601 scalar in the payload, not a top-level record field).

- [ ] **Step 1: Write the failing tests**

```python
# tests/claude_tail/test_tailer.py — append. Reuse the module's FakeRedis, _write_jsonl,
# _eval_fields, Identity, OffsetStore, _redactor helpers (already defined at top of file).

def _eval_payloads(redis):
    return [json.loads(fields["payload"]) for fields in _eval_fields(redis)]


def test_event_ts_carries_transcript_timestamp_into_eval_payload(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "timestamp": "2026-07-13T19:42:40.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript),
        Identity(run_id="run-1", task_id="task-1", seat_id="s", orchestrator="o"),
        OffsetStore(FakeRedis(), "p:"),
        live_redis=redis, trace_redis=redis, eval_redis=redis, eval_stream="eval:events",
        prefix="agent_scratch:", redactor=_redactor,
    )

    tailer.poll()

    payloads = _eval_payloads(redis)
    assert payloads, "expected eval edges from the tool_result"
    assert all(p.get("event_ts") == "2026-07-13T19:42:40.000Z" for p in payloads)


def test_event_ts_is_idempotent_across_a_byte_zero_reread(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "timestamp": "2026-07-13T19:42:40.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
    )
    redis = FakeRedis()
    offset_redis = FakeRedis()
    store = OffsetStore(offset_redis, "p:")
    ident = Identity(run_id="run-1", task_id="task-1", seat_id="s", orchestrator="o")

    def _new():
        return TranscriptTailer(str(transcript), ident, store, live_redis=redis, trace_redis=redis,
                                eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)

    _new().poll()
    first = [p["event_ts"] for p in _eval_payloads(redis) if "event_ts" in p]
    # Force a byte-0 re-read: drop the offset key so get() returns 0.
    offset_redis.values.clear()
    _new().poll()
    all_ts = [p["event_ts"] for p in _eval_payloads(redis) if "event_ts" in p]
    assert first and all_ts[len(first):] == first  # the re-emitted prefix carries the SAME event_ts


def test_absent_timestamp_omits_event_ts_and_bumps_counter(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user",  # NO timestamp field
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript),
        Identity(run_id="run-1", task_id="task-1", seat_id="s", orchestrator="o"),
        OffsetStore(FakeRedis(), "p:"),
        live_redis=redis, trace_redis=redis, eval_redis=redis, eval_stream="eval:events",
        prefix="agent_scratch:", redactor=_redactor,
    )

    tailer.poll()

    payloads = _eval_payloads(redis)
    assert payloads, "expected eval edges"
    assert all("event_ts" not in p for p in payloads)  # fail-closed: omitted, never sent_at
    assert tailer.claude_tail_missing_ts >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k "event_ts or absent_timestamp" -v`
Expected: FAIL — `event_ts` is not in the payload (no injection yet); `AttributeError: 'TranscriptTailer' object has no attribute 'claude_tail_missing_ts'`.

- [ ] **Step 3: Write the minimal implementation**

In `TranscriptTailer.__init__` (after `self.skipped_lines = 0`, `tailer.py:86`), add the counter:

```python
        self.skipped_lines = 0
        # R2 fail-closed: incremented whenever an eval-edge-producing line lacks its own
        # top-level `timestamp`; event_ts is then OMITTED (never a sent_at fallback).
        self.claude_tail_missing_ts = 0
```

In `_emit_events` (`tailer.py:216-230`), stamp `event_ts` from `obj` onto each event's `data` before routing:

```python
    def _emit_events(self, events, obj) -> int:
        self._ensure_identity_resolved()
        if events and obj.get("type") == "assistant":
            self.turn_index += 1

        line_ts = obj.get("timestamp")
        emitted = 0
        for event in events:
            self._stamp_event_ts(event, line_ts)
            started = self.lifecycle.started()
            if started is not None:
                self._has_started = True
                self._stamp_event_ts(started, line_ts)
                self._route_event(started)
                emitted += 1
            self._route_event(event)
            emitted += 1
        return emitted

    def _stamp_event_ts(self, event, line_ts) -> None:
        """Carry the transcript line's OWN timestamp into the event data as event_ts (R2 —
        idempotent latency basis). Fail closed on an absent/non-string timestamp: omit event_ts
        (never substitute sent_at) and bump the missing-ts counter for any event that will reach
        eval (LIVE_AND_TRACE / LIVE_ONLY vocab)."""
        data = event.setdefault("data", {})
        if isinstance(line_ts, str) and line_ts:
            data["event_ts"] = line_ts
        elif event["event_type"] in LIVE_AND_TRACE_EVENTS or event["event_type"] in LIVE_ONLY_EVENTS:
            self.claude_tail_missing_ts += 1
```

For the synthetic `task_continuing` edge (`_maybe_emit_continuing`, `tailer.py:294-309`), carry the last observed line's timestamp rather than `_now()`. Track it: in `_emit_events` set `self._last_line_ts = line_ts` (init `self._last_line_ts: str | None = None` in `__init__`), and in `_maybe_emit_continuing` add `"event_ts": self._last_line_ts` into the data only when it is a non-empty str (else omit + it is a synthetic edge with no natural line, so no counter bump — documented). Keep the existing `_now()`/`sent_at` on the live/trace tee unchanged (that stays the emit-time liveness signal).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k "event_ts or absent_timestamp" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Delete-to-red the fail-closed guard (r0-fold P1-E — real deny-proof, not assert-the-defect)**

The POSITIVE test `test_absent_timestamp_omits_event_ts_and_bumps_counter` (Step 1) passes ONLY because the
`_stamp_event_ts` else-branch omits `event_ts` + bumps the counter on an absent timestamp. Prove the guard is
load-bearing: in `_stamp_event_ts`, temporarily change the else-branch to the FORBIDDEN `sent_at`/`_now()`
fallback (`data["event_ts"] = line_ts or _now()`), run:

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py::test_absent_timestamp_omits_event_ts_and_bumps_counter -v`
Expected: **RED** (event_ts is now present; the counter stays 0). Restore the guard, re-run → GREEN. Record the
red output in the PR. (No `test_denyproof_*` that asserts the defective state — that would be vacuously green.)

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/tailer.py tests/claude_tail/test_tailer.py
git commit -m "feat(5a-0): claude-tail event_ts capture, fail-closed on absent timestamp (R2)"
```

---

## Task 4: claude-tail `tool_call_id` coalescing (completes Deliverable 1)

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/tailer.py` (`_item_id` / `_enrich_data`)
- Test: `tests/claude_tail/test_tailer.py`

**Interfaces:**
- Consumes: `canonical_tool_call_id` (Task 1); the allowlist admits `tool_call_id` (Task 2).
- Produces: claude-tail's tool edges (`command_started` / `command_finished` / `command_output`) each carry `data["tool_call_id"]` = `canonical_tool_call_id(data)` — the SAME id on both edges (the mapper sets `tool_use_id` on all three, so coalescing yields the identical value).

- [ ] **Step 1: Write the failing test**

```python
# tests/claude_tail/test_tailer.py — append
def test_tool_edges_carry_same_canonical_tool_call_id(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "assistant", "timestamp": "2026-07-13T19:42:41.000Z",
         "message": {"content": [{"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "pwd"}}]}},
        {"type": "user", "timestamp": "2026-07-13T19:42:42.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "ok"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript),
                              Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                              OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                              eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)

    tailer.poll()

    tool_ids = [p["tool_call_id"] for p in _eval_payloads(redis) if "tool_call_id" in p]
    assert tool_ids, "expected tool edges with tool_call_id"
    assert set(tool_ids) == {"toolu_9"}  # started + finished + output all share the id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k canonical_tool_call_id -v`
Expected: FAIL — no `tool_call_id` key in the eval payload yet.

- [ ] **Step 3: Write the minimal implementation**

Import the helper AND add the tool-edge event set at the top of `tailer.py` (near the other module
constants, `tailer.py:21-23`):

```python
from ..tool_call_id import canonical_tool_call_id

# r1-fold P1-2: the ONLY claude-tail events that carry a real tool id. tool_call_id is coalesced
# ONLY for these — mirroring the dispatch guard (Task 10) — so it never leaks onto non-tool events.
TOOL_EDGE_EVENTS = {"command_started", "command_finished", "command_output"}
```

In `_enrich_data` (`tailer.py:330-335`), coalesce `tool_call_id` **guarded to tool edges only** (mapper sets
`tool_use_id` on all three; the helper falls through to it):

```python
    def _enrich_data(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        data.setdefault("kind", event_type)
        data.setdefault("seq", self._seq)
        data.setdefault("item_id", self._item_id(event_type, data))
        # r1-fold P1-2 (cold-Opus): _enrich_data runs for ALL events (via _route_event) and _item_id
        # ALWAYS returns a non-empty string — so an UNGUARDED canonical_tool_call_id() would fall
        # through to item_id and stamp a synthetic tool_call_id (e.g. "t:1:9:turn_completed") onto
        # task_started / turn_completed / task_finished / drift_error, poisoning 5a's join key. Guard
        # by event type so ONLY genuine tool edges get one (mirrors the dispatch guard, Task 10).
        if event_type in TOOL_EDGE_EVENTS:
            cid = canonical_tool_call_id(data)
            if cid:
                data.setdefault("tool_call_id", cid)
        return data
```

(`_item_id`'s presentation-id derivation stays unchanged — `item_id` remains the presentation key; `tool_call_id`
is the new coalesced correlation key. **This guard also makes Task 4's own test pass:** without it, the injected
`task_started` edge would carry an `item_id`-derived `tool_call_id`, so `set(tool_ids) == {"toolu_9"}` would red.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k canonical_tool_call_id -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/tailer.py tests/claude_tail/test_tailer.py
git commit -m "feat(5a-0): claude-tail tool_call_id coalescing on tool edges (Deliverable 1)"
```

---

## Task 5: claude-tail `attempt_epoch = 1` constant (R1)

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/tailer.py` (`_enrich_data`)
- Test: `tests/claude_tail/test_tailer.py`

**Interfaces:**
- Consumes: the allowlist admits `attempt_epoch` (Task 2).
- Produces: EVERY claude-tail eval event carries `data["attempt_epoch"] = 1` — a compile-time constant, never allocated, never bumped (R1: claude-tail is a single forward-only tailer of an append-only transcript; its four byte-0 re-read paths are recoveries of the same session, not new attempts).

- [ ] **Step 1: Write the failing test**

```python
# tests/claude_tail/test_tailer.py — append
def test_attempt_epoch_is_constant_one_on_every_eval_event(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "timestamp": "2026-07-13T19:42:40.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript),
                              Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                              OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                              eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)

    tailer.poll()

    payloads = _eval_payloads(redis)
    assert payloads
    assert all(p.get("attempt_epoch") == 1 for p in payloads)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k attempt_epoch_is_constant -v`
Expected: FAIL — no `attempt_epoch` in payload.

- [ ] **Step 3: Write the minimal implementation**

Add a class constant and stamp it in `_enrich_data`:

```python
class TranscriptTailer:
    ...
    CLAUDE_TAIL_ATTEMPT_EPOCH = 1  # R1: constant; claude-tail never re-executes an attempt.
```

```python
    def _enrich_data(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        data.setdefault("kind", event_type)
        data.setdefault("seq", self._seq)
        data.setdefault("item_id", self._item_id(event_type, data))
        data.setdefault("attempt_epoch", self.CLAUDE_TAIL_ATTEMPT_EPOCH)
        if event_type in TOOL_EDGE_EVENTS:                  # r1-fold P1-2: tool edges only (Task 4)
            cid = canonical_tool_call_id(data)
            if cid:
                data.setdefault("tool_call_id", cid)
        return data
```

(`attempt_epoch` is stamped on EVERY event — it is per-execution metadata, not a tool id — while
`tool_call_id` stays guarded to `TOOL_EDGE_EVENTS` per the Task-4 r1 fold.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k attempt_epoch_is_constant -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/tailer.py tests/claude_tail/test_tailer.py
git commit -m "feat(5a-0): claude-tail attempt_epoch=1 constant on every eval event (R1)"
```

---

## Task 6: OffsetStore versioned `{v, offset, turn_index}` composite + legacy migration

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/offset.py`
- Test: `tests/claude_tail/test_offset.py`

**Interfaces:**
- Produces:
  - `Position` — `NamedTuple(offset: int, turn_index: int)`.
  - `OffsetStore.load(key: str) -> Position` — reads the composite; **PURE, never writes** (v6/v7). Absent key → `Position(0, 0)`. Valid `{"v":1,"offset":N,"turn_index":M}` with `N ≥ 0, M ≥ 0` → `Position(N, M)`. **Legacy bare integer (any value)** → return `Position(0, 0)` with NO write — a byte-0 recount, persisted only by the poll's own `_commit` (a legacy nonzero must NOT resume at `turn_index 0` mid-file; codex r5 P1-2). **Corrupt composite** (non-UTF-8 bytes / unparseable / wrong shape / missing keys / NEGATIVE offset or turn_index — codex r5-panel P1) → `Position(0, 0)`, no write, no raise (an uncaught decode error or a negative reaching `seek()` would sticky-fail the tailer FOREVER, since pure `load()` never overwrites the corrupt value).
  - `OffsetStore.store(key: str, offset: int, turn_index: int) -> None` — a single `SET` of the composite JSON (atomic; offset and turn_index always move together).
  - Back-compat wrappers (so Task 6 does not disturb the poll path — Task 7 migrates it): `get(key) -> int` = `load(key).offset`; `commit(key, offset) -> None` = `store(key, offset, load(key).turn_index)`.

**Design note:** `turn_index` here is **position/identity state** persisted exactly like the byte offset — NOT the (never-persisted) clock accumulator. The two must never be conflated (Global Constraints).

- [ ] **Step 1: Write the failing tests**

```python
# tests/claude_tail/test_offset.py — append (FakeRedis, OffsetStore, offset_key already imported)
import json

import pytest  # v9 (cold-Opus r8 P2): the RecursionError precondition below needs it

from agent_redis_bridge.claude_tail.offset import Position


def test_store_then_load_roundtrips_composite():
    store = OffsetStore(FakeRedis(), "p:")
    store.store("k", 1234, 4)
    assert store.load("k") == Position(offset=1234, turn_index=4)


def test_load_absent_key_is_zero_zero():
    assert OffsetStore(FakeRedis(), "p:").load("k") == Position(0, 0)


def test_store_writes_versioned_json():
    redis = FakeRedis()
    OffsetStore(redis, "p:").store("k", 1234, 4)
    stored = json.loads(redis.values["p:claude:offset:k"])
    assert stored == {"v": 1, "offset": 1234, "turn_index": 4}


def test_legacy_bare_nonzero_int_forces_byte_zero_recount():
    # codex r5 P1-2: a legacy bare int must NOT resume as {offset:N, turn_index:0} — force byte-0.
    # v6 (codex r4 P1): load() is PURE — the recount is returned in-memory with NO redis write;
    # the composite is persisted only by the poll's own _commit (sole-writer, clause-1). Re-tripping
    # the legacy read on a crash-before-commit is idempotent (always → the same byte-0 recount).
    redis = FakeRedis()
    redis.set("p:claude:offset:k", "5000")
    store = OffsetStore(redis, "p:")
    assert store.load("k") == Position(0, 0)
    # PURITY assert (v6): the raw legacy value is untouched — load() wrote NOTHING.
    assert redis.values["p:claude:offset:k"] == "5000"


def test_legacy_bare_zero_becomes_composite_zero():
    redis = FakeRedis()
    redis.set("p:claude:offset:k", "0")
    assert OffsetStore(redis, "p:").load("k") == Position(0, 0)


def test_corrupt_composite_reads_as_zero_zero_without_writing():
    # v6 (codex r4 P1): corrupt composite → Position(0,0) in-memory, NO write (load() is pure).
    redis = FakeRedis()
    redis.set("p:claude:offset:k", '{"v":1,"offset":"not-an-int"}')  # wrong shape / missing keys
    store = OffsetStore(redis, "p:")
    assert store.load("k") == Position(0, 0)
    assert redis.values["p:claude:offset:k"] == '{"v":1,"offset":"not-an-int"}'  # untouched


def test_invalid_utf8_position_reads_as_zero_zero_without_raising():
    # v7 (codex r5 P1): invalid UTF-8 must be a CORRUPT read (0,0), not an uncaught
    # UnicodeDecodeError — pure load() never overwrites, so a raise here stalls the tailer forever.
    redis = FakeRedis()
    redis.set("p:claude:offset:k", b"\xff\xfe{")
    store = OffsetStore(redis, "p:")
    assert store.load("k") == Position(0, 0)
    assert redis.values["p:claude:offset:k"] == b"\xff\xfe{"     # untouched (pure)


def test_negative_composite_reads_as_zero_zero_without_writing():
    # v7 (codex r5 P1): {-1,-1} is valid JSON ints — without the domain check it reaches
    # fh.seek(-1), which raises every poll: the same permanent-stall shape.
    redis = FakeRedis()
    redis.set("p:claude:offset:k", '{"v":1,"offset":-1,"turn_index":-1}')
    store = OffsetStore(redis, "p:")
    assert store.load("k") == Position(0, 0)
    assert redis.values["p:claude:offset:k"] == '{"v":1,"offset":-1,"turn_index":-1}'  # untouched


def test_deeply_nested_corrupt_position_reads_as_zero_zero_without_raising():
    # v8 (codex r6 P1): a deeply-nested value raises RecursionError out of json.loads on the
    # deploy Python (3.11 raises at ~2k). v9 (grok+codex r7 P1, BOTH executed): depth is
    # runtime-DEPENDENT — Python 3.14 parses 100k clean and raises only at ~500k, so a fixed
    # small depth makes the catch-arm delete-to-red silently vacuous on the local suite runtime.
    # The precondition below makes the probe SELF-PROVING: if a future runtime parses this depth
    # fine, the test fails LOUDLY here (bump the depth) instead of the guard going quietly
    # vacuous. (A json.loads monkeypatch was considered and rejected —
    # [[fixture-supplies-what-code-lacks]].) Requires `import pytest` in test_offset.py.
    payload = "[" * 500_000 + "]" * 500_000
    with pytest.raises(RecursionError):
        json.loads(payload)                        # the probe really trips THIS runtime's parser
    redis = FakeRedis()
    redis.set("p:claude:offset:k", payload)
    store = OffsetStore(redis, "p:")
    assert store.load("k") == Position(0, 0)


def test_noncanonical_version_field_reads_as_corrupt():
    # v8 (codex r6 P2): JSON true and 1.0 both compare == 1 — they must NOT pass the version
    # check and smuggle their offset into a mid-file resume.
    for v in ("true", "1.0"):
        redis = FakeRedis()
        redis.set("p:claude:offset:k", '{"v":%s,"offset":5000,"turn_index":3}' % v)
        assert OffsetStore(redis, "p:").load("k") == Position(0, 0)


def test_backcompat_get_returns_offset_and_commit_preserves_turn_index():
    redis = FakeRedis()
    store = OffsetStore(redis, "p:")
    store.store("k", 100, 7)
    assert store.get("k") == 100
    store.commit("k", 250)  # offset-only commit must NOT clobber turn_index
    assert store.load("k") == Position(offset=250, turn_index=7)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_offset.py -k "composite or legacy or corrupt or backcompat or byte_zero or invalid_utf8 or negative or nested or noncanonical" -v`
Expected: FAIL — `ImportError: cannot import name 'Position'`; no `load`/`store`.

- [ ] **Step 3: Write the implementation**

```python
# src/agent_redis_bridge/claude_tail/offset.py — full replacement of the class body
from __future__ import annotations

import json
import logging
from typing import Any, NamedTuple


logger = logging.getLogger("agent_redis_bridge.claude_tail.offset")

_COMPOSITE_VERSION = 1


def offset_key(path: str, inode: int) -> str:
    return f"{path}|{inode}"


class Position(NamedTuple):
    offset: int
    turn_index: int


class OffsetStore:
    def __init__(self, redis: Any, prefix: str) -> None:
        self.redis = redis
        self.prefix = prefix

    def load(self, key: str) -> Position:
        """v6 (codex r4 P1): PURE — never writes. A legacy/corrupt read returns Position(0, 0)
        in-memory; the composite is persisted only by the tailer's `_commit` (the sole writer),
        so re-tripping this path on a crash-before-commit is idempotent (always the same byte-0
        recount). The pre-v6 eager `_reset -> store(key, 0, 0)` was a persisted-position writer
        outside `_commit`, executed pre-guard — exactly the drift class the census forbids."""
        raw = self.redis.get(self._redis_key(key))
        if raw is None:
            return Position(0, 0)
        if isinstance(raw, bytes):
            # v7 (codex r5 P1): decode INSIDE the corrupt-handling — invalid UTF-8 raised OUT of
            # load() every poll, and with pure load() never overwriting, the tailer stalled FOREVER.
            try:
                raw = raw.decode()
            except UnicodeDecodeError:
                logger.warning("corrupt claude-tail position; recounting from byte 0", extra={"offset_key": key})
                return Position(0, 0)
        # Legacy bare integer (pre-composite deploy): force a byte-0 recount — a nonzero legacy
        # offset must not resume at turn_index 0 (codex r5 P1-2). bare "0" collapses to the same.
        if isinstance(raw, str) and raw.lstrip("-").isdigit():
            return Position(0, 0)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            # v8 (codex r6 P1): a deeply-nested corrupt value raises RecursionError out of
            # json.loads on the deploy Python (3.11, reproduced) — one more permanent-stall
            # input under pure load. Same corrupt-handling: recount, no write, no raise.
            logger.warning("corrupt claude-tail position; recounting from byte 0", extra={"offset_key": key})
            return Position(0, 0)
        if (
            not isinstance(data, dict)
            # v8 (codex r6 P2): `data.get("v") != 1` accepts True and 1.0 (both == 1) — require
            # the exact int type so a noncanonical version cannot smuggle its offset mid-file.
            or type(data.get("v")) is not int
            or data["v"] != _COMPOSITE_VERSION
            or not isinstance(data.get("offset"), int)
            or not isinstance(data.get("turn_index"), int)
            or isinstance(data.get("offset"), bool)
            or isinstance(data.get("turn_index"), bool)
            # v7 (codex r5 P1): negative ints are valid JSON but reach fh.seek(-1), which raises
            # every poll — the same permanent-stall shape as the decode error above.
            or data["offset"] < 0
            or data["turn_index"] < 0
        ):
            logger.warning("corrupt claude-tail position; recounting from byte 0", extra={"offset_key": key})
            return Position(0, 0)
        return Position(offset=data["offset"], turn_index=data["turn_index"])

    def store(self, key: str, offset: int, turn_index: int) -> None:
        offset = int(offset)
        turn_index = int(turn_index)
        if offset < 0 or turn_index < 0:
            raise ValueError("offset and turn_index must be non-negative")
        payload = json.dumps(
            {"v": _COMPOSITE_VERSION, "offset": offset, "turn_index": turn_index},
            separators=(",", ":"),
        )
        self.redis.set(self._redis_key(key), payload)

    # --- back-compat offset-only wrappers: TRANSITIONAL, explicit fate (v6, grok r4 P1) ---
    # These exist ONLY so the pre-Task-7 poll() stays green between the Task 6 and Task 7
    # commits. Task 7 Step 3c DELETES both once poll() migrates to load/_commit (grep-proved no
    # callers remain), and the Step 7c census forbids any `.commit(`/`.get(` call on an
    # offset_store receiver from ever reappearing — `commit()` writes the store WITHOUT binding
    # the tailer cursor, which is exactly the partial-migration drift shape clause-1 forbids.
    def get(self, key: str) -> int:
        return self.load(key).offset

    def commit(self, key: str, offset: int) -> None:
        self.store(key, offset, self.load(key).turn_index)

    def _redis_key(self, key: str) -> str:
        return f"{self.prefix}claude:offset:{key}"
```

- [ ] **Step 4: Run the full offset suite**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_offset.py -v`
Expected: the new tests PASS. **Update the pre-existing format-coupled tests** that asserted the bare-int representation (`test_commit_uses_prefixed_redis_key`, `test_commit_coerces_int_offsets`, `test_get_decodes_bytes_value`) to assert the composite JSON form instead (e.g. `json.loads(redis.values["p:claude:offset:k"]) == {"v":1,"offset":1234,"turn_index":0}`). The behavior they pin (prefixing, int coercion) is unchanged — only the stored representation moved to the composite. **`test_get_self_heals_corrupt_offset_to_zero` changes SEMANTICS under v6** (load is pure — corrupt reads as 0 with NO self-heal write): rewrite it to assert `get()` returns 0 AND the raw value is untouched (the heal-persist now happens only via the tailer's `_commit`, per the matrix's poll-top-load row).

- [ ] **Step 5: Run the tailer suite to confirm the wrappers keep it green**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -v`
Expected: PASS — the tailer still uses `get`/`commit`, which now transparently read/write the composite.

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/offset.py tests/claude_tail/test_offset.py
git commit -m "feat(5a-0): versioned {v,offset,turn_index} offset composite + legacy migration (codex r5 P1-2)"
```

---

## Task 7: claude-tail logical-turn lifecycle + restart-stable `turn_index` on the eval path

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/tailer.py`
- Test: `tests/claude_tail/test_tailer.py`

**Interfaces:**
- Consumes: `OffsetStore.load/store` + `Position` (Task 6); `event_ts` stamping (Task 3).
- Produces: claude-tail emits a **logical-turn lifecycle** — `turn_started` when a human `user` line is observed, `turn_completed` at the **next** human-`user` close (Option-D) — and carries a per-`(run_id,task_id)` **logical** `turn_index` into the eval payload, restart-stable via the persisted composite (Task 6). Turn/tool eval edges + `turn_started`/`turn_completed` carry `data["turn_index"]`; out-of-turn events (`task_started`, `task_continuing`, `drift_error`) do NOT. `turn_started` carries `turn_started_ts`; the `turn_clock_monotonic` flag is added on `turn_completed` in Task 8.

**Design decisions (state these in the code comments):**
1. **Two distinct ordinals.** The existing per-assistant `self.turn_index` (→ trace, `_emit_trace(turn_index=...)`, `tailer.py:368`) stays EXACTLY as-is ("per-assistant ordinal stays trace-only" — design). A NEW `self.logical_turn_index` (per-human-prompt cycle) feeds the EVAL payload only. Same field name (`turn_index`), different sink, different value — do not merge them.
2. **Human-`user` boundary signal = `promptId` presence.** `promptId` is present only on human `user` lines (0/99 on assistant lines — grok F3); agentic tool-result `user` lines lack it. A `user` line with a top-level `promptId` opens a logical turn; a `user` line without one is an in-turn tool-result.
3. **`isMeta`/`isSidechain` records are out-of-turn:** they neither open/close a turn nor advance `logical_turn_index` (nor contribute to the clock flag, Task 8).
4. **Emit ordering at a boundary (load-bearing for eval `turn_index` correctness):** on a human-`user` line closing turn N and opening N+1 — (a) emit `turn_completed` **while `logical_turn_index == N`**, (b) advance to N+1 and persist, (c) emit `turn_started`. Because `_emit_eval` reads `self.logical_turn_index` at emit time, this sequence stamps N on the close and N+1 on the open with no per-event bookkeeping.

- [ ] **Step 1: Write the failing tests**

```python
# tests/claude_tail/test_tailer.py — append. Helper: pull turn_index off eval payloads by event.
def _eval_by_event(redis):
    out = []
    for fields in _eval_fields(redis):
        out.append((fields["event_type"], json.loads(fields["payload"])))
    return out


def test_one_prompt_two_tool_rounds_is_one_logical_turn(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "do it"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "a"}}]}},
        {"type": "user", "timestamp": "2026-07-13T19:00:02.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:03.000Z",
         "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "b"}}]}},
        {"type": "user", "timestamp": "2026-07-13T19:00:04.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript),
                              Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                              OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                              eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)
    tailer.poll()

    by_event = _eval_by_event(redis)
    # exactly one turn_started(turn 1); NO turn_completed yet (no next human prompt).
    assert [e for e, _ in by_event].count("turn_started") == 1
    assert [e for e, _ in by_event].count("turn_completed") == 0
    started = next(p for e, p in by_event if e == "turn_started")
    assert started["turn_index"] == 1 and started["turn_started_ts"] == "2026-07-13T19:00:00.000Z"
    # every tool edge in the turn carries turn_index == 1
    tool_edges = [p for e, p in by_event if e in ("command_started", "command_finished", "command_output")]
    assert tool_edges and all(p["turn_index"] == 1 for p in tool_edges)


def test_second_human_prompt_closes_turn_one_and_opens_turn_two(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "text", "text": "answer one"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:05.000Z",
         "message": {"content": [{"type": "text", "text": "second"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript),
                              Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                              OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                              eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)
    tailer.poll()

    by_event = _eval_by_event(redis)
    completed = [p for e, p in by_event if e == "turn_completed"]
    started = [p for e, p in by_event if e == "turn_started"]
    assert len(completed) == 1 and completed[0]["turn_index"] == 1  # turn 1 closed by the 2nd prompt
    assert [p["turn_index"] for p in started] == [1, 2]              # two opens, ordinals 1 then 2
    # turn_completed for turn 1 carries the turn's LAST causal record timestamp, not the 2nd prompt's
    assert completed[0]["event_ts"] == "2026-07-13T19:00:01.000Z"


def test_ismeta_and_sidechain_records_do_not_advance_logical_turn(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "user", "isMeta": True, "promptId": "meta", "timestamp": "2026-07-13T19:00:00.500Z",
         "message": {"content": [{"type": "text", "text": "meta"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "a"}}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript),
                              Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                              OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                              eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)
    tailer.poll()

    by_event = _eval_by_event(redis)
    # the isMeta line must NOT open a second turn despite its promptId
    assert [e for e, _ in by_event].count("turn_started") == 1
    tool_edges = [p for e, p in by_event if e == "command_started"]
    assert tool_edges and all(p["turn_index"] == 1 for p in tool_edges)


def test_turn_index_is_restart_stable_across_nonzero_offset_resume(tmp_path):
    transcript = tmp_path / "s.jsonl"
    # turns 1..3
    lines = []
    for i in range(1, 4):
        lines.append({"type": "user", "promptId": f"p{i}", "timestamp": f"2026-07-13T19:0{i}:00.000Z",
                      "message": {"content": [{"type": "text", "text": f"prompt {i}"}]}})
    _write_jsonl(transcript, *lines)
    redis = FakeRedis()
    offset_redis = FakeRedis()
    store = OffsetStore(offset_redis, "p:")
    ident = Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o")
    TranscriptTailer(str(transcript), ident, store, live_redis=redis, trace_redis=redis,
                     eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor).poll()
    # append turn 4 and poll with a FRESH tailer resuming from the persisted nonzero offset
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "promptId": "p4", "timestamp": "2026-07-13T19:04:00.000Z",
                             "message": {"content": [{"type": "text", "text": "prompt 4"}]}}) + "\n")
    redis2 = FakeRedis()
    TranscriptTailer(str(transcript), ident, store, live_redis=redis2, trace_redis=redis2,
                     eval_redis=redis2, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor).poll()

    starts = [p["turn_index"] for e, p in _eval_by_event(redis2) if e == "turn_started"]
    assert starts == [4]  # NOT [1]; the resume restored the logical turn ordinal from the composite
```

**Delete-to-red (r0-fold P1-E, replaces the deleted `test_denyproof_unpersisted…`):** to prove the turn_index
persistence guard is load-bearing, temporarily make the poll end-of-poll commit drop the ordinal
(`store(key, new_offset, 0)`), run `test_turn_index_is_restart_stable_across_nonzero_offset_resume` → it MUST
RED (`starts == [1]`, a collision). Restore → GREEN. Record the red output.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k "logical_turn or human_prompt or restart_stable or ismeta or unpersisted" -v`
Expected: FAIL — no `turn_started`/`turn_completed` events emitted; no `logical_turn_index`.

- [ ] **Step 3: Write the implementation**

`__init__` additions (after `self.turn_index = 0`, `tailer.py:72`):

```python
        self.turn_index = 0            # per-ASSISTANT ordinal → trace only (unchanged)
        self.logical_turn_index = 0    # per-human-prompt cycle → EVAL turn_index (NEW)
        self._turn_open = False
        self._turn_started_ts: str | None = None
        self._last_causal_ts: str | None = None  # last in-turn user/assistant timestamp (turn_completed.event_ts)
```

Add the event set and turn-index stamping. Near the module constants (`tailer.py:21-23`):

```python
TURN_INDEXED_EVENTS = {"command_started", "command_finished", "command_output",
                       "turn_started", "turn_completed"}
```

In `poll()`, replace the offset read (`tailer.py:92`) and the final commit (`:188-190`) to thread the composite:

```python
        pos = self.offset_store.load(key)
        offset = pos.offset
        if offset == 0:
            self.logical_turn_index = 0     # byte-0 (re)read re-counts ordinals idempotently
        else:
            self.logical_turn_index = pos.turn_index
        if offset > stat.st_size:
            self.offset_store.store(key, 0, 0)
            offset = 0
            self.logical_turn_index = 0
```

> **SUPERSEDED PROVENANCE (v6, grok r4 P2):** this composite-read block's heal shape (a bare
> `offset_store.store(key, 0, 0)`) is the r0 derivation only. The AUTHORITATIVE heal (Task 7's `poll()` block)
> routes the persist through `_commit(key, 0, 0, st_ino)` FIRST and then EXPLICITLY ABANDONS the open turn
> (v7 order) — a bare `store()` call outside `_commit` now FAILS the Step 7c census. Do not implement from
> this block.

Everywhere `poll()` currently commits the byte offset (`self.offset_store.commit(key, X)` at the truncate-heal, prefix-commit, and end-of-poll sites), change to `self.offset_store.store(key, X, <ordinal>)` so offset and turn_index always persist together (atomic single `SET`).

**r1-fold P1-1 (codex-sol #1 + grok) — the per-line turn-state TRANSACTION (supersedes the v2 P1-D prose;
grok flagged that fold was prose-only).** The v2 P1-D fold made the persisted *offset/ordinal* transactional, but
`_close_and_open_turn` (below) and `_observe_clock` (Task 8) ALSO mutate **in-memory** turn state
(`logical_turn_index`, `_turn_open`, `_turn_started_ts`, `_last_causal_ts`, and Task-8's `_turn_clock_ok` /
`_turn_prev_ts`) BEFORE all of a boundary line's emissions finish. On a same-object prefix-commit RETRY — the
service re-polls THIS tailer object (`service.py:166-171`, no reconstruction) — none of that in-memory state was
rolled back, so the replayed boundary line re-ran `_close_and_open_turn` against mutated state and emitted a
DUPLICATE `turn_completed` carrying the NEXT prompt's ts at ordinal N (P1-A resurrected as a retry defect).
**Fix:** snapshot the FULL per-line turn-state tuple at line start; on any emit-failure that prefix-commits,
RESTORE it AND persist the PRE-line ordinal, so the retry replays from a consistent `(persisted-prefix,
in-memory-state)` pair. Add the snapshot helpers to `TranscriptTailer` (near `_emit_events`):

```python
    # r1-fold P1-1: fields mutated mid-line by _close_and_open_turn / _observe_clock. Snapshot at line
    # start; restore on a prefix-committing emit failure so a same-object retry (service.py:166-171)
    # replays the boundary line from consistent state instead of re-closing an already-mutated turn.
    # logical_turn_index is in the tuple — this SUBSUMES the v2 P1-D persisted-ordinal snapshot
    # (ordinal_at_line_start == snap["logical_turn_index"]).
    _TURN_STATE_FIELDS = ("logical_turn_index", "_turn_open", "_turn_started_ts", "_last_causal_ts")
    # (Task 8 APPENDS "_turn_clock_ok", "_turn_prev_ts" to this tuple when it adds the clock bit.)

    def _snapshot_turn_state(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self._TURN_STATE_FIELDS}

    def _restore_turn_state(self, snap: dict[str, Any]) -> None:
        for field, value in snap.items():
            setattr(self, field, value)
```

Rewrite the per-line loop's commit sites (`tailer.py:125-186`) to satisfy the **emit-fail / drift-fail rows** of
the transaction matrix: snapshot at the TOP of the loop (BEFORE parse/emit); on the emit-stage prefix-commit AND
the drift-emit prefix-commit, (a) restore the snapshot, (b) persist `store(key, line_start, ordinal_at_line_start)`,
and **(c) ALIGN THE CURSOR to the committed prefix (r2-fold P1 — the matrix's emit-fail cursor cell)**:

```python
                line_start = new_offset
                new_offset = fh.tell()
                lines += 1
                turn_state_at_line_start = self._snapshot_turn_state()      # r1-fold P1-1
                ordinal_at_line_start = turn_state_at_line_start["logical_turn_index"]

                # ---- parse/map stage ----
                try:
                    events, obj, is_human_user = self._parse_line(line)
                except DriftError as exc:
                    self._ensure_identity_resolved()
                    self.drift_count += 1
                    # (Task 8 adds `if self._turn_open: self._turn_clock_ok = False` here — eager
                    #  DriftError-arm poison. It is re-applied on replay, so the restore below is safe.)
                    try:
                        self._emit_drift_error(exc)
                    except RedisError:
                        raise
                    except Exception:
                        self.emit_failing = True
                        self._restore_turn_state(turn_state_at_line_start)   # r1-fold P1-1
                        if line_start != offset:
                            self.offset_store.store(key, line_start, ordinal_at_line_start)
                        # r2-fold P1 (matrix drift-fail row): align cursor to the committed prefix so the
                        # retry does NOT read this tailer's own forward store-advance as a discontinuity.
                        self._cursor_inode = stat.st_ino
                        self._cursor_offset = line_start
                        self.progressed = line_start != offset
                        raise
                    if self.drift_count > self.drift_threshold:
                        self.offset_store.store(key, new_offset, self.logical_turn_index)
                        self.progressed = True
                        raise _DriftThresholdExceeded("drift threshold exceeded") from exc
                    emitted += 1
                    continue
                except RedisError:
                    raise
                except Exception:
                    self.skipped_lines += 1
                    # (Task 8 adds `if self._turn_open: self._turn_clock_ok = False` here — eager
                    #  generic-skip-arm poison. This arm just `continue`s; no prefix-commit/restore.)
                    logger.warning("skipping unparseable claude transcript line",
                                   extra={"transcript_path": self.path, "line_offset": line_start})
                    continue

                # ---- emit stage ----
                try:
                    emitted += self._emit_events(events, obj, is_human_user)
                except RedisError:
                    raise
                except Exception:
                    emit_failed = True
                    self.emit_failing = True
                    self._restore_turn_state(turn_state_at_line_start)       # r1-fold P1-1: roll back
                    if line_start != offset:
                        self.offset_store.store(key, line_start, ordinal_at_line_start)
                    # r2-fold P1 (matrix emit-fail row): align cursor to the committed prefix. UNCONDITIONAL
                    # (in the first-line case line_start == offset, so cursor stays == offset — still aligned).
                    # Without this, a MULTI-LINE poll advances the store to line_start while the cursor stays
                    # at the poll's start, so the retry's Fold-B check misfires, abandons the open turn, and
                    # drops a never-durable turn_completed → false NULL (r2 codex-sol #1 + grok).
                    self._cursor_inode = stat.st_ino
                    self._cursor_offset = line_start
                    self.progressed = line_start != offset
                    raise
```

The non-prefix exit paths follow the matrix's remaining rows:
- **truncate-heal** — **SUPERSEDED PROVENANCE (v6):** this bullet's v3-era shape (bare `store(key, 0, 0)`,
  cursor unchanged, abandon-via-discontinuity) is retained for derivation history only. The AUTHORITATIVE v6
  heal `_commit(key, 0, 0, st_ino)`s FIRST then explicitly abandons the open turn (v7 order; matrix
  truncate-heal row; Steps 7f/7g/7i are its deny-proofs). Implement from the authoritative block.
- **end-of-poll** (emit-success): `store(key, new_offset, self.logical_turn_index)` + `_cursor_offset = new_offset`
  / `_cursor_inode = stat.st_ino` (Task 8's end-of-poll cursor record).

**Why restore + prefix-commit + cursor-align together (the transaction invariant — matrix clauses 1 & 3):** the
store returns to `(line_start, ordinal_at_line_start)`, the in-memory turn state returns to its pre-line snapshot,
AND the cursor is aligned to `(st_ino, line_start)` — so after the exit `cursor == (st_ino, store.offset)` and
`store.turn_index == in-memory logical_turn_index` hold together. The retry then replays the boundary line from a
fully consistent triple: no false discontinuity (cursor aligned), reproducing `turn_completed[N]` with turn N's
correct last-causal ts (an idempotent at-least-once duplicate 5a dedups by `(task_id, turn_index, event)`), never a
mis-stamped / mis-ordinal one, and never a dropped close. The **first-line same-object retry regression test** is
**Task 8 Step 7**; the **multi-line dropped-close deny-proof** (the r2 window) is **Task 8 Step 7b**.

---

#### AUTHORITATIVE v5 `poll()` + `_commit` (r3-fold structural refactor — SUPERSEDES the incremental blocks above)

> The blocks above are the incremental derivation (r1 restore, r2 cursor-align). **v5 collapses them into two
> structural primitives so the divergence classes are unrepresentable, not merely patched** (Mark, r3-fold): a
> sole-writer `_commit` (clause-1) and an outer boundary guard (clause-2). This is the code the worker implements;
> the earlier fenced blocks are provenance. Add `_commit` near `_snapshot_turn_state`:

```python
    def _commit(self, key: str, offset: int, ordinal: int, st_ino: int) -> None:
        """v5: the SOLE writer of the persisted offset AND the in-memory continuity cursor. Binding
        them here makes clause-1 (cursor == store.offset) impossible to violate — no exit path can
        advance one without the other. `_cursor_offset`/`_cursor_inode` have NO OTHER assignment site
        (enforced by the sole-writer census, Task 8 Step 7c). Truncate-heal deliberately does NOT call
        this (it resets in-memory only, leaving the cursor stale so the discontinuity check fires)."""
        self.offset_store.store(key, offset, ordinal)
        self._cursor_inode = st_ino
        self._cursor_offset = offset
```

> **v6 note:** the `_commit` docstring's v5 sentence "truncate-heal deliberately does NOT call this" is
> SUPERSEDED — the heal DOES call `_commit(key, 0, 0, st_ino)` as its FIRST side effect, then explicitly abandons the open turn (v7 order)
> (r4 cold-Opus fold; see the heal block below and the matrix's truncate-heal row). Drop that sentence from
> the docstring when implementing; the rest stands verbatim.

Restructured `poll()` (every store/cursor write goes through `_commit`; an outer guard restores turn-state on any
uncommitted exit):

```python
    def poll(self) -> int:
        started_at = time.monotonic()
        stat = os.stat(self.path)
        st_ino = stat.st_ino
        key = offset_key(self.path, st_ino)
        pos = self.offset_store.load(key)   # v6: load() is PURE — legacy/corrupt reads (0,0), NO write
        offset = pos.offset
        self.logical_turn_index = 0 if offset == 0 else pos.turn_index
        if offset > stat.st_size:
            # truncate-heal (v6 r4 cold-Opus fold; v7 reorder, codex r5 P1): _commit is the FIRST
            # side effect after detecting truncation — a crash between abandon and persist can no
            # longer leave the stale offset durable (the in-memory abandon below is non-throwing).
            # The persist survives even a heal poll that reads ZERO complete lines, so regrowth
            # past the old offset can never mask the reset (the r4 zero-lines/regrowth window).
            # The explicit abandon replaces the discontinuity ride (the _commit aligns the cursor,
            # which silences that check). Runs BEFORE the guard snapshot; deliberately does NOT
            # set `committed` (Step 7i red-proofs this): a later uncommitted failure must restore
            # the POST-abandon snapshot — this persist stands on its own idempotency.
            offset = 0
            self.logical_turn_index = 0
            self._commit(key, 0, 0, st_ino)
            if self._turn_open:
                self._turn_clock_ok = False
                self._turn_open = False
        # discontinuity check (residue row): AFTER truncate-heal, BEFORE the loop — v6: fires only on
        # GENUINE external discontinuities (key-delete byte-0 re-read, inode swap, external rewind);
        # a rewound generation clears _turn_open before any line is emitted.
        if self._turn_open and (self._cursor_inode != st_ino or self._cursor_offset != offset):
            self._turn_clock_ok = False
            self._turn_open = False

        emitted = 0
        lines = 0
        new_offset = offset
        self.at_eof = False
        emit_failed = False
        committed = False   # set True ONLY by the per-line arms / end-of-poll _commit — NEVER the
                            # heal's (Step 7i red-proofs that; a heal-commit must not skip the guard)
        poll_start_turn_state = self._snapshot_turn_state()  # outer boundary guard baseline (clause 2)
        try:
            with open(self.path, "rb") as fh:
                fh.seek(offset)
                while True:
                    if lines >= self.poll_budget_lines or (
                        lines > 0 and time.monotonic() - started_at >= self.poll_budget_secs
                    ):
                        break
                    line = fh.readline()                 # readline/tell raise OUTSIDE the per-line arms
                    if not line or not line.endswith(b"\n"):
                        self.at_eof = True
                        break
                    line_start = new_offset
                    new_offset = fh.tell()
                    lines += 1
                    turn_state_at_line_start = self._snapshot_turn_state()
                    ordinal_at_line_start = turn_state_at_line_start["logical_turn_index"]
                    try:
                        events, obj, is_human_user = self._parse_line(line)
                    except DriftError as exc:
                        self._ensure_identity_resolved()
                        self.drift_count += 1
                        if self._turn_open:
                            self._turn_clock_ok = False      # eager DriftError-arm poison
                        try:
                            self._emit_drift_error(exc)
                        except RedisError:
                            raise
                        except Exception:
                            self.emit_failing = True
                            # v7 (codex r5 P1): drift_count is NON-idempotent pre-emit state that the
                            # turn-state restore does not cover — the retry re-parses this line and
                            # increments again, double-counting it (threshold crossable a line early).
                            # Roll the increment back; the retry's re-count is then single.
                            self.drift_count -= 1
                            self._restore_turn_state(turn_state_at_line_start)
                            if line_start != offset:
                                self._commit(key, line_start, ordinal_at_line_start, st_ino)
                                committed = True
                            self.progressed = line_start != offset
                            raise
                        if self.drift_count > self.drift_threshold:
                            # v5: _commit binds the cursor (clause-1 holds → no false discontinuity next
                            # poll), keep the poisoned turn-state, mark committed so the outer guard skips
                            # restore. Sticky-fail same object; the poisoned turn closes false ⇒ NULL.
                            self._commit(key, new_offset, self.logical_turn_index, st_ino)
                            committed = True
                            self.progressed = True
                            raise _DriftThresholdExceeded("drift threshold exceeded") from exc
                        emitted += 1
                        continue
                    except RedisError:
                        raise
                    except Exception:
                        self.skipped_lines += 1
                        if self._turn_open:
                            self._turn_clock_ok = False      # generic-skip poison (re-applied on replay)
                        logger.warning("skipping unparseable claude transcript line",
                                       extra={"transcript_path": self.path, "line_offset": line_start})
                        continue
                    try:
                        emitted += self._emit_events(events, obj, is_human_user)
                    except RedisError:
                        raise
                    except Exception:
                        emit_failed = True
                        self.emit_failing = True
                        self._restore_turn_state(turn_state_at_line_start)
                        if line_start != offset:
                            self._commit(key, line_start, ordinal_at_line_start, st_ino)
                            committed = True
                        self.progressed = line_start != offset
                        raise
            if new_offset != offset:
                self._commit(key, new_offset, self.logical_turn_index, st_ino)
                committed = True
                self.progressed = True
            else:
                self.progressed = False
                if emitted == 0:
                    self._maybe_emit_continuing()
        except Exception:
            # Outer boundary guard (clause 2): any exit that did NOT commit (readline/tell/budget OSError,
            # or any non-Redis exception raised between the per-line arms) restores turn-state to the
            # poll-start snapshot — mutated turn-state can never survive a failed poll and be replayed.
            # Committed exits (prefix-commit arms, drift-threshold, end-of-poll) already left a consistent
            # (store, cursor, turn-state) triple via _commit, so they are skipped.
            if not committed:
                self._restore_turn_state(poll_start_turn_state)
            raise
        if not emit_failed:
            self.emit_failing = False
        return emitted
```

**Why this is unrepresentable-to-violate, not disciplined:** clause-1 holds because `_commit` is the ONLY writer
of `(store.offset, _cursor_offset, _cursor_inode)` — the swept-surface census (Task 8 Step 7c) fails the build if
any other assignment site, `offset_store.store(` call, or forbidden back-compat `.commit(`/`.get(` call appears,
in `tailer.py` OR `offset.py`. Clause-2 holds because the outer guard restores the poll-start snapshot on EVERY
uncommitted exit — you cannot add a new exit path that leaks mutated turn-state, because the guard wraps them all
(and the census forbids a mid-loop `return` from bypassing it). The v6 residue is the ABANDON mechanism at its
two sites (heal-abandon, discontinuity), each with its own deny-proof (Steps 7f/7g, `test_vi`).

- [ ] **Step 3c: DELETE the back-compat `OffsetStore.get()`/`commit()` wrappers (v6 — grok r4 P1, explicit fate)**

Once the restructured `poll()` above is in place, nothing calls the offset-only wrappers. Delete both methods
from `src/agent_redis_bridge/claude_tail/offset.py`, then PROVE no caller remains:

Run: `grep -rn "offset_store\.\(get\|commit\)(\|\.get(key)\|offset_store.commit" src/ tests/ | grep -v "test_offset"` — expected: NO hits in `src/`
(update any lingering `tests/` call sites to `load`/`store`; `test_backcompat_get_returns_offset_and_commit_preserves_turn_index` from Task 6 is DELETED here with the wrappers it pinned).
Then: `uv run --extra arb-memory pytest tests/claude_tail/ -v` → PASS.
The Step 7c census additionally forbids `.commit(`/`.get(` on an `offset_store` receiver from reappearing in
`tailer.py` — with its own planted-rogue red-proof — so a partially-migrated future edit fails the suite instead
of silently splitting store from cursor.

---

`_parse_line` returns a turn-boundary flag; add the `isMeta`/`isSidechain` + `promptId` classification:

```python
    def _parse_line(self, line: bytes):
        obj = json.loads(line.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("transcript line is not a JSON object")
        self._capture_first_user_marker(obj)
        self._check_done_marker(obj)
        out_of_turn = bool(obj.get("isMeta") or obj.get("isSidechain"))
        is_human_user = (obj.get("type") == "user" and bool(obj.get("promptId")) and not out_of_turn)
        # r0-fold P1-A (agy/grok/codex): do NOT update _last_causal_ts on the human-user boundary
        # line — that line OPENS the next turn, it is not part of the closing turn. If we updated it
        # here, _close_and_open_turn would stamp the closing turn_completed with the NEXT prompt's ts
        # (the inter-prompt gap), not the closing turn's last-record ts. The new turn's baseline is
        # reset inside _close_and_open_turn.
        if not out_of_turn and not is_human_user and obj.get("type") in ("user", "assistant"):
            ts = obj.get("timestamp")
            if isinstance(ts, str) and ts:
                self._last_causal_ts = ts
        # r2-fold P2 (codex): attach the decoded obj to a DriftError so the DriftError arm can stamp
        # event_ts on the durable drift_error (map_line raises from deep inside; obj is available HERE).
        try:
            events = map_line(obj)
        except DriftError as exc:
            exc.obj = obj
            raise
        return events, obj, is_human_user
```

And `_emit_drift_error` stamps `event_ts` from the attached obj so a timestamped unknown-type line's durable
`drift_error` honors Task 3's "every timestamped eval event carries event_ts" contract (r2-fold P2, codex):

```python
    def _emit_drift_error(self, exc: DriftError) -> None:
        event = {"event_type": "drift_error",
                 "data": {"message": str(exc), "count": self.drift_count, "kind": "drift_error"}}
        line_ts = getattr(exc, "obj", {}).get("timestamp") if isinstance(getattr(exc, "obj", None), dict) else None
        if isinstance(line_ts, str) and line_ts:
            event["data"]["event_ts"] = line_ts        # bounded ISO8601 scalar; allowlisted (Task 2)
        self._route_event(event)
```

Update the caller (`poll()` line 131) to unpack the third value, and `_emit_events` to take the boundary and drive the lifecycle:

```python
    def _emit_events(self, events, obj, is_human_user) -> int:
        self._ensure_identity_resolved()
        emitted = 0
        if is_human_user:
            emitted += self._close_and_open_turn(obj)
        if events and obj.get("type") == "assistant":
            self.turn_index += 1
        line_ts = obj.get("timestamp")
        line_uuid = obj.get("uuid")            # r1-fold P2 (agy+grok): trace-only correlation carry
        for event in events:
            self._stamp_event_ts(event, line_ts)
            self._stamp_uuid(event, line_uuid)
            started = self.lifecycle.started()
            if started is not None:
                self._has_started = True
                self._stamp_event_ts(started, line_ts)
                self._stamp_uuid(started, line_uuid)
                self._route_event(started)
                emitted += 1
            self._route_event(event)
            emitted += 1
        return emitted

    def _stamp_uuid(self, event, line_uuid) -> None:
        """r1-fold P2 (spec Deliverable 4): carry the transcript line's `uuid` onto the event data so
        edges correlate to the source record. This is NOT an eval primitive and is NOT allowlisted —
        `extract_eval_payload` drops it by construction, so it NEVER reaches the eval stream (5a's join
        key stays clean). Its live sink is real: `live_tee` serializes the full `data` dict, so `uuid`
        rides the LIVE/roster plane (a human-visibility correlation aid). `trace_tee` only forwards its
        structured fields, so it is absent there too — this is deliberately a live-plane-only carry."""
        if isinstance(line_uuid, str) and line_uuid:
            event.setdefault("data", {})["uuid"] = line_uuid

    def _close_and_open_turn(self, obj) -> int:
        emitted = 0
        line_ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
        # (a) close the prior turn at THIS next-human-user boundary (Option-D authoritative close),
        #     while logical_turn_index still == N.
        if self._turn_open:
            completed = {"event_type": "turn_completed",
                         "data": {"turn_completed": True}}
            if self._last_causal_ts:
                completed["data"]["event_ts"] = self._last_causal_ts
            # turn_clock_monotonic added on this event in Task 8.
            self._route_event(completed)
            emitted += 1
        # (b) advance + persist BEFORE opening the new turn.
        self.logical_turn_index += 1
        self._turn_open = True
        self._turn_started_ts = line_ts
        self._last_causal_ts = line_ts  # r0-fold P1-A: reset the new turn's event_ts baseline to its opening record
        # (c) open: turn_started carries turn_started_ts (== this line's ts); NOT the clock flag.
        started = {"event_type": "turn_started", "data": {}}
        if line_ts:
            started["data"]["turn_started_ts"] = line_ts
            started["data"]["event_ts"] = line_ts
        self._route_event(started)
        emitted += 1
        return emitted
```

Inject the eval `turn_index` in `_emit_eval` (localized so trace/live keep the per-assistant kwarg):

```python
    def _emit_eval(self, event: Event) -> None:
        if self.eval_redis is None or not self.eval_stream:
            return
        data = event["data"]
        if event["event_type"] in TURN_INDEXED_EVENTS:
            data = dict(data)
            data["turn_index"] = self.logical_turn_index  # LOGICAL ordinal on the eval path only
        record = build_eval_record(
            run_id=self.identity.run_id, task_id=self.identity.task_id, seat_id=self.identity.seat_id,
            orchestrator=self.identity.orchestrator, event=event["event_type"], sent_at=_now(), data=data,
        )
        if record is None:
            return
        try:
            self.eval_redis.xadd(self.eval_stream, record)
        except Exception:
            logger.exception("Claude tail eval tee failed for task %s event %s", self.identity.task_id, event["event_type"])
```

Route `turn_started`/`turn_completed` to eval: add them to `LIVE_AND_TRACE_EVENTS` (or a new eval-routed set) so `_route_event` sends them to `_emit_eval`. Confirm they carry no free text (they don't — only bounded scalars).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k "logical_turn or human_prompt or restart_stable or ismeta or unpersisted" -v`
Expected: PASS. Also re-run the whole tailer suite — `test_turn_index_advances_on_assistant_turns` (trace, per-assistant `["1","1","2"]`) MUST still be green (the per-assistant ordinal is untouched).

- [ ] **Step 5: Add the legacy-migration deny-proof at the tailer level (spec Tests viii)**

```python
# tests/claude_tail/test_tailer.py — append
def test_legacy_bare_int_offset_forces_recount_not_index_zero_resume(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript,
                 {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:01:00.000Z",
                  "message": {"content": [{"type": "text", "text": "one"}]}},
                 {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:02:00.000Z",
                  "message": {"content": [{"type": "text", "text": "two"}]}})
    redis = FakeRedis(); offset_redis = FakeRedis()
    ident = Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o")
    # Seed a LEGACY bare-int nonzero offset (pre-composite deploy state) at this inode's key.
    key = offset_key(str(transcript), transcript.stat().st_ino)
    offset_redis.set(f"p:claude:offset:{key}", "40")
    store = OffsetStore(offset_redis, "p:")
    TranscriptTailer(str(transcript), ident, store, live_redis=redis, trace_redis=redis,
                     eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor).poll()
    # The legacy read forced a byte-0 recount: BOTH turns are re-emitted with ordinals 1,2 —
    # NOT a mid-file resume that would have emitted turn 2 as index 0/1 and re-keyed on replay.
    starts = [p["turn_index"] for e, p in _eval_by_event(redis) if e == "turn_started"]
    assert starts == [1, 2]
```

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k legacy_bare_int_offset -v` → PASS.

- [ ] **Step 5b: `uuid` correlation carry (r1-fold P2) — proven non-inert (live-carried, eval-dropped)**

```python
# tests/claude_tail/test_tailer.py — append
def test_uuid_rides_live_data_for_correlation_but_never_reaches_eval(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "assistant", "uuid": "u-abc", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pwd"}}]}},
    )
    redis = FakeRedis()
    TranscriptTailer(str(transcript),
                     Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                     OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                     eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor).poll()
    # Load-bearing invariant: uuid is NOT allowlisted, so it NEVER reaches the eval stream (5a's join
    # key stays clean). This is the constraint that matters.
    assert all("uuid" not in p for p in _eval_payloads(redis))
    # Non-inert proof: uuid DOES ride the live/roster plane's data blob (live_tee serializes full data).
    live = [json.loads(fields["data"]) for key, fields, _ in redis.xadds if key.endswith("events:live")]
    assert any(d.get("uuid") == "u-abc" for d in live), "uuid must reach the live plane for correlation"
```

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k uuid_rides_live -v` → PASS. (If the
test redactor happens to scrub the id, weaken to asserting `"uuid"` is a key on the live edge — the invariant
that matters is the eval-drop.)

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/tailer.py tests/claude_tail/test_tailer.py
git commit -m "feat(5a-0): claude-tail logical-turn lifecycle + restart-stable eval turn_index + uuid carry (Deliverable 4)"
```

---

## Task 8: claude-tail `turn_clock_monotonic` — single-continuous-observation flag (Option-B/D)

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/tailer.py`
- Test: `tests/claude_tail/test_tailer.py` (r1-fold: Task 8 touches NO service test — Tests v/vii live in `test_tailer.py` (vii) + the live gate (v))

**Interfaces:**
- Consumes: the logical-turn lifecycle (Task 7); the allowlist admits `turn_clock_monotonic` + `turn_started_ts` (Task 2).
- Produces: each `turn_completed` eval edge carries `data["turn_clock_monotonic"]: bool` — **authoritatively `true` ONLY when ONE continuous tailer scan observed the turn's ENTIRE causal record stream from its opening human-`user` record through its next-human-`user` close, with every in-turn line cleanly parsed, timestamped, and non-decreasing.** Every other case is `false`. The flag is stamped ONLY on `turn_completed` (`turn_started` carries only `turn_started_ts`). NOTHING at a terminal stop earns `true` (Option-D).

**The `true` predicate — ALL of (any failure ⇒ `false`, by construction, no persistence):**
1. **Scan continuity (a SCAN generation, not an object generation).** Between polls the tailer records the expected `(inode, next_offset)` continuation cursor. If the next poll's actual `(inode, start_offset)` ≠ that (any rewind / byte-0 reset / truncate-heal / inode change while a turn is open), the open turn's clock bit goes sticky-`false`. A **replayed opening record does NOT authoritatively close the open turn** (its `turn_completed` carries the already-`false` bit). A fresh tailer (no in-session opening) never had `_turn_open=True`, so it emits no `turn_completed` for a straddled turn → NULL.
2. **Clean lines (eager, per-turn).** A line that fails `json.loads`/is non-`dict` (the generic skip arm, `tailer.py:160-166`) or raises `DriftError` sets the open turn's bit sticky-`false` **eagerly on that arm** — NOT inferred from the cumulative lifetime `skipped_lines` counter.
3. **Non-decreasing timestamps over the FULL causal stream.** Every in-turn `user`/`assistant` record (append order, incl. trace-only `text`/`thinking` and the dropped pure-text `user` line; `isMeta`/`isSidechain` excluded) must have a `timestamp` ≥ the previous; a backward step OR a missing required `timestamp` ⇒ `false`. The scan sees the raw `obj` in `_emit_events` (every parsed line reaches it, even when `map_line` returns `[]`).

- [ ] **Step 1: Write the failing tests (the core deny-proof battery — spec Tests i–vi)**

```python
# tests/claude_tail/test_tailer.py — append
def _final_flag(redis):
    """The turn_clock_monotonic on the (single) turn_completed eval edge, or None if none emitted."""
    for e, p in _eval_by_event(redis):
        if e == "turn_completed":
            return p.get("turn_clock_monotonic")
    return None


def _tailer(transcript, redis, store=None):
    return TranscriptTailer(str(transcript),
                            Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                            store or OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                            eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)


def test_i_trace_only_backward_child_no_tool_is_false(tmp_path):
    # spec test (i): pure-text user + an EARLIER pure thinking child + no tool → clock is false.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:10.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:05.000Z",  # BACKWARD vs its parent
         "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:20.000Z",  # next-human close
         "message": {"content": [{"type": "text", "text": "next"}]}},
    )
    redis = FakeRedis()
    _tailer(transcript, redis).poll()
    assert _final_flag(redis) is False  # trace-only inversion caught though it never reached eval


def test_ii_intermediate_inversion_between_bookends_is_false(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "a"}}]}},
        {"type": "assistant", "timestamp": "2026-07-13T18:59:59.000Z",  # inverted MIDDLE (trace-only)
         "message": {"content": [{"type": "thinking", "thinking": "back in time"}]}},
        {"type": "user", "timestamp": "2026-07-13T19:00:03.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:10.000Z",
         "message": {"content": [{"type": "text", "text": "next"}]}},
    )
    redis = FakeRedis()
    _tailer(transcript, redis).poll()
    assert _final_flag(redis) is False  # non-decreasing BOOKENDS must not hide the inverted middle


def test_iii_unclean_line_in_turn_is_false(tmp_path):
    transcript = tmp_path / "s.jsonl"
    good1 = json.dumps({"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
                        "message": {"content": [{"type": "text", "text": "go"}]}})
    corrupt = "{ this is not valid json"
    good2 = json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:10.000Z",
                        "message": {"content": [{"type": "text", "text": "next"}]}})
    transcript.write_text(good1 + "\n" + corrupt + "\n" + good2 + "\n", encoding="utf-8")
    redis = FakeRedis()
    _tailer(transcript, redis).poll()
    assert _final_flag(redis) is False  # the corrupt in-turn line poisons the turn eagerly (GENERIC-skip arm)


def test_iii_drifterror_unmappable_line_in_turn_is_false(tmp_path):
    # r1-fold P1-4 (codex-sol #3): a VALID-JSON line of an unknown transcript `type` raises DriftError
    # (mapper.py:34) — the DriftError arm, NOT the generic-skip arm (that arm needs malformed JSON,
    # test_iii above). This test exercises the SECOND poison arm so its delete-to-red is real.
    transcript = tmp_path / "s.jsonl"
    good1 = json.dumps({"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
                        "message": {"content": [{"type": "text", "text": "go"}]}})
    unmappable = json.dumps({"type": "some_unknown_future_type", "timestamp": "2026-07-13T19:00:05.000Z",
                             "message": {"content": [{"type": "text", "text": "?"}]}})  # valid JSON → DriftError
    good2 = json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:10.000Z",
                        "message": {"content": [{"type": "text", "text": "next"}]}})
    transcript.write_text(good1 + "\n" + unmappable + "\n" + good2 + "\n", encoding="utf-8")
    redis = FakeRedis()
    _tailer(transcript, redis).poll()
    assert _final_flag(redis) is False  # the DriftError in-turn line poisons the turn eagerly (DriftError arm)


def test_iv_fresh_generation_never_closes_a_straddled_turn_true(tmp_path):
    transcript = tmp_path / "s.jsonl"
    # gen 1 observes the opening only
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:10.000Z",
                              "message": {"content": [{"type": "text", "text": "go"}]}})
    redis1 = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    _tailer(transcript, redis1, store).poll()
    # a FRESH tailer (new object) resumes and sees a backward suffix + the next-human close
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:05.000Z",
                             "message": {"content": [{"type": "thinking", "thinking": "back"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:20.000Z",
                             "message": {"content": [{"type": "text", "text": "next"}]}}) + "\n")
    redis2 = FakeRedis()
    _tailer(transcript, redis2, store).poll()
    # gen 2 never observed turn 1's opening in-session → it emits NO true turn_completed for it.
    assert _final_flag(redis2) is not True


def test_vi_same_object_byte0_reread_replayed_opening_does_not_close_true(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:10.000Z",
                              "message": {"content": [{"type": "text", "text": "go"}]}})
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()  # observes the opening, turn open, cursor recorded
    # Delete the offset key mid-turn so the SAME object re-reads from byte 0 next poll, and append
    # a later BACKWARD record after the replayed opening.
    key = offset_key(str(transcript), transcript.stat().st_ino)
    offset_redis.values.pop(f"p:claude:offset:{key}", None)
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:05.000Z",
                             "message": {"content": [{"type": "thinking", "thinking": "back"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:30.000Z",
                             "message": {"content": [{"type": "text", "text": "next"}]}}) + "\n")
    tailer.poll()  # SAME object, byte-0 re-read → cursor discontinuity
    # r0-fold P1-B: the replayed opening must NOT close the straddled turn at all — so there must be
    # NO turn_completed edge stamped at the just-zeroed ordinal (not merely "not True").
    completed = [(e, p) for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert all(p.get("turn_clock_monotonic") is not True for _, p in completed)   # never a forged true
    assert not any(p.get("turn_index") == 0 for _, p in completed)                # no phantom ordinal-0 close


def test_clean_contiguous_turn_closed_at_next_human_user_is_true(tmp_path):
    # the ONE true case: a whole turn observed by one contiguous scan, all clean + non-decreasing,
    # closed at a next-human-user line.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "text", "text": "answer"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:05.000Z",
         "message": {"content": [{"type": "text", "text": "next"}]}},
    )
    redis = FakeRedis()
    _tailer(transcript, redis).poll()
    assert _final_flag(redis) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k "test_i_ or test_ii_ or test_iii_ or test_iv_ or test_vi_ or clean_contiguous" -v`
Expected: FAIL — `turn_clock_monotonic` is absent from the payload (`_final_flag` returns `None`).

- [ ] **Step 3: Write the implementation**

`__init__` additions (with the Task 7 turn state):

```python
        self._turn_clock_ok = False       # per-turn clock bit; True only under proven clean contiguous obs
        self._turn_prev_ts: str | None = None
        self._cursor_inode: int | None = None
        self._cursor_offset: int | None = None
```

**r1-fold P1-1 — extend the per-line snapshot tuple.** The two new turn-state fields (`_turn_clock_ok`,
`_turn_prev_ts`) are mutated mid-line by `_observe_clock` / `_close_and_open_turn`, so they MUST join the Task-7
`_TURN_STATE_FIELDS` transaction (else a prefix-commit retry replays with a stale clock bit). Update the tuple:

```python
    _TURN_STATE_FIELDS = ("logical_turn_index", "_turn_open", "_turn_started_ts", "_last_causal_ts",
                          "_turn_clock_ok", "_turn_prev_ts")   # r1-fold P1-1: +clock fields (Task 8)
```

Scan-continuity check at the TOP of `poll()`, after computing `key`/`offset` (before reading). **Provenance
note (v5/v6):** the AUTHORITATIVE `poll()` block in Task 7 already carries this check, placed after the
truncate-heal (which `_commit`s FIRST, then explicitly abandons — v7 order) — implement from that block; this fenced copy
documents the r0 derivation and the abandon rationale:

```python
        stat = os.stat(self.path)
        key = offset_key(self.path, stat.st_ino)
        pos = self.offset_store.load(key)
        offset = pos.offset
        # ... (Task 7 logical_turn_index restore) ...
        if self._turn_open and (
            self._cursor_inode != stat.st_ino or self._cursor_offset != offset
        ):
            # Discontinuity while a turn is open (byte-0 reset, truncate-heal, inode swap, rewind):
            # the current scan can no longer prove contiguous observation.
            # r0-fold P1-B (grok): ABANDON the straddled turn's open state, not just poison the clock.
            # If we only set _turn_clock_ok=False and left _turn_open=True, the replayed opening record
            # (a human-user line) would run _close_and_open_turn and phantom-emit a turn_completed for
            # the straddled turn carrying the just-restored logical_turn_index (0 on a byte-0 re-read) —
            # a spurious durable eval edge with turn_index=0. Clearing _turn_open makes the replayed
            # opening open a FRESH turn instead; the abandoned turn emits NO turn_completed ⇒ NULL
            # (interrupted; recovery is 5a's O5). A replayed opening never closes the turn it re-opens.
            self._turn_clock_ok = False
            self._turn_open = False
```

**Note (this discontinuity check runs AFTER the Task-7 `logical_turn_index` restore in `poll()`):** on a byte-0 re-read the restore sets `logical_turn_index = 0`, then this block clears `_turn_open`, then the replayed opening advances `0 → 1` and opens turn 1 — an idempotent re-count with no phantom close.

The non-decreasing + clean-line scan lives in `_emit_events` (runs for every parsed line, even empty `events`). Extend the Task 7 `_emit_events`:

```python
    def _emit_events(self, events, obj, is_human_user) -> int:
        self._ensure_identity_resolved()
        emitted = 0
        if is_human_user:
            emitted += self._close_and_open_turn(obj)   # resets clock bit for the NEW turn (below)
        elif self._turn_open and obj.get("type") in ("user", "assistant") \
                and not (obj.get("isMeta") or obj.get("isSidechain")):
            self._observe_clock(obj)                     # in-turn record: feed the non-decreasing check
        # --- the rest of the Task-7 body is UNCHANGED and must be kept verbatim: ---
        if events and obj.get("type") == "assistant":
            self.turn_index += 1
        line_ts = obj.get("timestamp")
        line_uuid = obj.get("uuid")                      # Task-7 uuid carry — do NOT drop
        for event in events:
            self._stamp_event_ts(event, line_ts)         # Task-3 event_ts stamp — do NOT drop
            self._stamp_uuid(event, line_uuid)
            started = self.lifecycle.started()
            if started is not None:
                self._has_started = True
                self._stamp_event_ts(started, line_ts)
                self._stamp_uuid(started, line_uuid)
                self._route_event(started)
                emitted += 1
            self._route_event(event)
            emitted += 1
        return emitted
```

```python
    def _observe_clock(self, obj) -> None:
        ts = obj.get("timestamp")
        if not (isinstance(ts, str) and ts):
            self._turn_clock_ok = False                  # required record missing timestamp
            return
        if self._turn_prev_ts is not None and ts < self._turn_prev_ts:
            self._turn_clock_ok = False                  # backward step anywhere in the full stream
        self._turn_prev_ts = ts
```

In `_close_and_open_turn` (Task 7): stamp the flag on `turn_completed` (while the bit still reflects turn N), then reset the bit + baseline for turn N+1:

```python
    def _close_and_open_turn(self, obj) -> int:
        emitted = 0
        line_ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
        line_uuid = obj.get("uuid")                      # r2-fold P2 (grok): boundary correlation anchor
        if not line_ts:
            # v8 (agy r6 P1): the synthetic turn edges bypass _stamp_event_ts, so the Task-3
            # missing-ts counter is bumped HERE (once per ts-less boundary line) — Step 7j asserts it.
            self.claude_tail_missing_ts += 1
        if self._turn_open:
            completed = {"event_type": "turn_completed",
                         "data": {"turn_completed": True,
                                  "turn_clock_monotonic": bool(self._turn_clock_ok)}}
            if self._last_causal_ts:
                completed["data"]["event_ts"] = self._last_causal_ts
            self._stamp_uuid(completed, line_uuid)       # r2-fold P2: synthetic edges also carry uuid
            self._route_event(completed)
            emitted += 1
        self.logical_turn_index += 1
        self._turn_open = True
        self._turn_started_ts = line_ts
        self._last_causal_ts = line_ts                   # r0-fold P1-A: new turn's event_ts baseline
        # NEW turn: clock starts clean, baselined at the opening human-user timestamp.
        self._turn_clock_ok = bool(line_ts)              # missing opening ts ⇒ can never be true
        self._turn_prev_ts = line_ts
        started = {"event_type": "turn_started", "data": {}}
        if line_ts:
            started["data"]["turn_started_ts"] = line_ts
            started["data"]["event_ts"] = line_ts
        self._stamp_uuid(started, line_uuid)             # r2-fold P2 (grok): stamp uuid on turn_started
        self._route_event(started)
        emitted += 1
        return emitted
```

(r2-fold P2, grok: `turn_started`/`turn_completed` are built here and routed directly, bypassing the
`_emit_events` loop — so without these two `_stamp_uuid` calls the synthetic turn edges never carried the `uuid`
the banner claims. Same trace-only, eval-dropped semantics as the tool/model edges.)

Eager clean-line poisoning in `poll()` — in the generic skip arm (`tailer.py:160-166`) and the `DriftError` arm (`tailer.py:132-157`), add (guarded on an open turn):

```python
                except Exception:
                    self.skipped_lines += 1
                    if self._turn_open:
                        self._turn_clock_ok = False       # eager per-turn poison (NOT via skipped_lines)
                    logger.warning(...)
                    continue
```

(and the same `if self._turn_open: self._turn_clock_ok = False` in the `DriftError` arm — place it right after
`self.drift_count += 1` and BEFORE `self._emit_drift_error(exc)`, so the poison lands whether or not the drift
emit throws. These two poison lines are the exact `# (Task 8 adds ...)` markers in the Task-7 poll() transaction
block. Because the r1-fold P1-1 restore rolls `_turn_clock_ok` back on a prefix-committing emit failure, the
poison is re-applied when the DriftError line re-parses on replay — idempotent, so the restore is safe.)

**Cursor recording (v5 — reconciled to the structural refactor):** the cursor is NOT recorded by per-site
assignments at the end of `poll()`; it is bound to the store inside `_commit` — the SOLE writer of
`(store.offset, _cursor_offset, _cursor_inode)` (the AUTHORITATIVE v5 `poll()` + `_commit` block in Task 7 is
the source of truth; Step 7c's sole-writer census fails the build on any other write site). Every
store-advancing exit — emit-success end-of-poll, the emit-stage and drift-fail prefix-commit arms, and the
drift-threshold exit — reaches the cursor through the same `_commit` call, so the r2-era "cursor is set in three
places" discipline is GONE: there are no per-site assignments to keep aligned. Two deliberate properties survive
from the matrix: **(v6/v7 update)** truncate-heal now ALSO routes through `_commit` — its FIRST side effect, followed by the explicit abandon (the
v5 stale-cursor exemption is gone — cold-Opus r4 fold), and the cursor is NOT part of `_TURN_STATE_FIELDS` (it is force-*aligned* to
the committed prefix via `_commit`, never *restored* to the pre-line value — restoring it to the poll-start
value is the r2 bug). Invariant after every non-restart exit: `_cursor_offset == store.offset` and
`_cursor_inode == st_ino`, so the check fires ONLY on a genuine external discontinuity.

- [ ] **Step 4: Run the battery to verify it passes**

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k "test_i_ or test_ii_ or test_iii_ or test_iv_ or test_vi_ or clean_contiguous" -v`
Expected: PASS (7 passed — incl. the r1-fold `test_iii_drifterror_…` DriftError-arm case) — the true case green, all inversions/discontinuities false.

- [ ] **Step 5: Delete-to-red each Task-8 guard (r0-fold P1-E — real deny-proofs, no assert-the-defect tests)**

The positive tests (Step 1, `test_i_`…`test_vi_` + `clean_contiguous`) are the deny-proofs. Prove each guard is
load-bearing by physically deleting it, running the SAME positive test, confirming RED, restoring, and recording
the red output ([[deny-proofs-need-adversarial-verification]]). Do NOT add any `test_denyproof_*` that asserts
the defective state under a monkeypatch — that is vacuously green ([[vacuously-green-guard-fail-loud]]). Guard → test:

| Guard to delete | Positive test that MUST RED |
|---|---|
| the full-stream `_observe_clock(obj)` call in `_emit_events` (so trace-only records are skipped) | `test_i_trace_only_backward_child_no_tool_is_false`, `test_ii_intermediate_inversion_between_bookends_is_false` |
| the eager `if self._turn_open: self._turn_clock_ok = False` on the **generic-skip** arm (`tailer.py:160-166`) | `test_iii_unclean_line_in_turn_is_false` (malformed JSON) |
| **(r1-fold P1-4)** the eager `if self._turn_open: self._turn_clock_ok = False` on the **`DriftError`** arm (`tailer.py:132-157`) — a SEPARATE guard from the skip arm | `test_iii_drifterror_unmappable_line_in_turn_is_false` (valid-JSON unknown `type`) |
| the poll-top `self._turn_open = False` (P1-B) discontinuity abandon | `test_vi_same_object_byte0_reread_replayed_opening_does_not_close_true` |

Each deletion turns exactly one positive test RED; a deletion that leaves them all green means that positive
test was not actually exercising the guard (fix the test).

> **r1-fold P2(d): `test_iv` is NOT in this delete-to-red map.** `test_iv_fresh_generation_never_closes_a_straddled_turn_true`
> and the "fresh-tailer no-in-session-opening path" it exercises are a **structural absence of state** (a brand-new
> tailer object never had `_turn_open=True`, so it emits no `turn_completed` for a turn it never opened) — there is
> no single deletable *guard line* whose removal flips it (grok + codex + cold-Opus). Keep `test_iv` as a POSITIVE
> structural test (it must stay green), but do NOT list it as a delete-to-red row — a "guard" that is the absence
> of code cannot be deleted-to-red without contriving a monkeypatch, which would be vacuous.

- [ ] **Step 6: Add the Option-D "no terminal earn" test — spec Test vii (r0-fold: concrete, was `...`)**

Test vii is the direct test for the whole Option-D scope decision. It is testable at the TAILER level (the
property is: a turn with no next-human-`user` line emits `turn_started` but NEVER a `turn_completed`, even when
the transcript ends at a done-marker / sidecar-completed cold seat). Concrete, runnable with the Task-8 helpers:

```python
# tests/claude_tail/test_tailer.py — append
def test_vii_single_dispatch_no_next_human_emits_no_turn_completed(tmp_path):
    # One human prompt + a tool round + a cold-seat done-marker assistant line, NO second human prompt.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "a"}}]}},
        {"type": "user", "timestamp": "2026-07-13T19:00:02.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:03.000Z",
         "message": {"content": [{"type": "text", "text": "done [ARB_SEAT_DONE]"}]}},
    )
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)
    tailer.poll()
    tailer.finish(ok=True)                      # terminal stop — must NOT emit a turn_completed
    by_event = _eval_by_event(redis)
    assert any(e == "turn_started" for e, _ in by_event)              # turn_started DID fire (turn_index=1)
    assert not any(e == "turn_completed" for e, _ in by_event)        # NO turn_completed ⇒ 5a NULLs (O5)
```

This holds by construction: `turn_completed` is emitted ONLY by `_close_and_open_turn` (next-human-`user`), and
`finish()`/`_finish_once`/`_cold_seat_completed`/idle paths (`service.py:193-207,460-495`) emit only the
lifecycle `task_finished`. **Delete-to-red:** temporarily make `finish()` emit a `turn_completed{turn_clock_monotonic:true}`,
run this test → it REDS (a terminal `turn_completed` appears); restore. **Test v (resumable idle-finish straddle)**
is a service+timing integration (a cold idle-finish then a same-cycle backward record on resume) — asserted in
the **live gate** (Task 13 Step 4), not unit-forced, because deterministic idle-timing needs the real `Service`
tick loop; the tailer-level guarantee above already proves no stop earns `true`.

- [ ] **Step 7: Inner-restore deny-proof, re-keyed to the multi-line committed-prefix window (r1-fold P1-1; v6 re-key per grok r4 P1)**

**v6 re-key (the r4 grok finding — a new vacuity variant: red-made-redundant-by-a-later-guard).** The v5 version
of this step failed on the boundary as the FIRST line of the poll (`line_start == offset`), where no `_commit`
runs, `committed` stays `False`, and the v5 OUTER guard performs the IDENTICAL restore the inner arm does — so
deleting the inner restore left the test green: the structural fix itself vacated this deny-proof. The inner
restore's ONLY remaining load-bearing window is **multi-line committed-prefix**: an in-turn line emits (advancing
the offset), THEN the boundary's `turn_started` throws AFTER a durable `turn_completed` — here the arm `_commit`s
(`committed=True`), the outer guard is SKIPPED by design, and the inner restore is the only thing standing
between the retry and a phantom turn-2 close. The test name states the red-reason. Standing rule recorded in the
v6 banner: a fold that adds/widens a guard must re-run the delete-to-red suite for every other guard whose
protected window it overlaps.

Both tests are two-phase for the same reason as before: phase 1 lands the cursor at the future append offset so
the retry's discontinuity check cannot fire and mask the defect.

```python
# tests/claude_tail/test_tailer.py — append. Requires `import pytest` (already imported in this file).
def test_same_object_retry_after_emit_failure_does_not_misstamp_turn_completed(tmp_path):
    # v6 NOTE: POSITIVE at-least-once test ONLY — its delete-to-red is VACUOUS BY DESIGN since v5:
    # in this first-line window `committed == False`, so the outer boundary guard performs the same
    # restore the inner arm does (grok r4 P1). The inner restore's load-bearing deny-proof is the
    # multi-line test below. Keep this green; do NOT list it as a delete-to-red row.
    transcript = tmp_path / "s.jsonl"
    # Phase 1: turn 1 opens and stays open; the poll's end-of-poll cursor lands at EOF == the byte
    # offset where the 2nd prompt will be appended. This is what makes the retry's discontinuity check
    # NOT fire (cursor_offset == boundary line_start == offset), i.e. the exact P1-1 window.
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "text", "text": "answer one"}]}},
    )
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()                          # turn 1 open; cursor recorded at EOF (= future p2 start)

    # Phase 2: append the boundary (2nd human prompt) that closes turn 1 and opens turn 2.
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z",
                             "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")

    # Inject a one-shot failure on the turn_started[2] emit — fires AFTER _close_and_open_turn emitted
    # turn_completed[1] and mutated the in-memory turn state to turn 2.
    real_route = tailer._route_event
    state = {"boom": True}

    def flaky_route(event):
        if (state["boom"] and event.get("event_type") == "turn_started"
                and event.get("data", {}).get("turn_started_ts") == "2026-07-13T19:05:00.000Z"):
            state["boom"] = False
            raise RuntimeError("injected emit bug on turn_started[2]")
        return real_route(event)

    tailer._route_event = flaky_route
    with pytest.raises(RuntimeError):
        tailer.poll()                      # boundary poll: turn_started[2] throws → prefix-commit + restore
    tailer.poll()                          # SAME object retry — discontinuity check does NOT fire (P1-1 window)

    by_event = _eval_by_event(redis)
    completed = [p for e, p in by_event if e == "turn_completed"]
    assert completed, "turn_completed[1] must be emitted"
    # NO mis-stamp: every turn_completed carries turn 1's last-causal ts, NEVER the 2nd prompt's ts.
    assert all(p.get("event_ts") == "2026-07-13T19:00:01.000Z" for p in completed)
    assert all(p.get("event_ts") != "2026-07-13T19:05:00.000Z" for p in completed)
    # NO ordinal collision: every close is turn_index 1; turn 2 opens at 2 (never 0).
    assert all(p.get("turn_index") == 1 for p in completed)
    starts = [p.get("turn_index") for e, p in by_event if e == "turn_started"]
    assert 2 in starts and 0 not in starts
```

The LOAD-BEARING deny-proof — the multi-line committed-prefix window, where the outer guard cannot mask:

```python
# tests/claude_tail/test_tailer.py — append. Requires `import pytest` (already imported).
def test_multiline_committed_prefix_inner_restore_not_masked_by_outer_guard(tmp_path):
    # v6 (grok r4 P1): THE deny-proof for the emit-fail × turn-state cell. An in-turn line emits
    # first (advancing the offset), so the boundary's failure prefix-COMMITS (`committed=True`) and
    # the outer guard is SKIPPED by design — the inner _restore_turn_state is the ONLY protection.
    transcript = tmp_path / "s.jsonl"
    # Phase 1: turn 1 opens; cursor lands at EOF1 (where the appends begin).
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()                                  # turn 1 open; cursor == store.offset == EOF1
    eof1 = transcript.stat().st_size

    # Phase 2: append an IN-TURN assistant line (emits fine → advances the offset PAST it), THEN
    # the boundary p2 — so on the boundary line `line_start != offset` (the committed-prefix window).
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
                             "message": {"content": [{"type": "text", "text": "answer one"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z",
                             "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")

    # One-shot failure on turn_started[2] — fires AFTER _close_and_open_turn durably emitted
    # turn_completed[1] and mutated in-memory turn-state to turn 2.
    real_route = tailer._route_event
    state = {"boom": True}

    def flaky_route(event):
        if (state["boom"] and event.get("event_type") == "turn_started"
                and event.get("data", {}).get("turn_started_ts") == "2026-07-13T19:05:00.000Z"):
            state["boom"] = False
            raise RuntimeError("injected emit bug on turn_started[2]")
        return real_route(event)

    tailer._route_event = flaky_route
    with pytest.raises(RuntimeError):
        tailer.poll()                              # boundary throws AFTER the durable close

    # Anti-vacuity: we ARE in the committed-prefix window — the close is already durable, and the
    # store advanced to the boundary's line start (committed=True ⇒ the outer guard was skipped).
    mid = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert len(mid) == 1 and mid[0].get("turn_index") == 1
    key = offset_key(str(transcript), transcript.stat().st_ino)
    pos = store.load(key)
    assert pos.offset > eof1                       # prefix committed past the assistant line
    assert (tailer._cursor_inode, tailer._cursor_offset) == (transcript.stat().st_ino, pos.offset)

    tailer.poll()                                  # SAME object retry — replays ONLY the boundary line

    by_event = _eval_by_event(redis)
    completed = [p for e, p in by_event if e == "turn_completed"]
    # Every close is turn 1's (the retry re-emits the SAME edge — at-least-once); the inner restore
    # prevented the phantom turn-2 close a mutated-survivor state would have produced.
    assert all(p.get("turn_index") == 1 for p in completed)
    assert all(p.get("event_ts") == "2026-07-13T19:00:01.000Z" for p in completed)
    starts = [p.get("turn_index") for e, p in by_event if e == "turn_started"]
    assert 2 in starts and 3 not in starts and 0 not in starts
```

**Delete-to-red (the re-keyed inner-restore proof):** remove the `self._restore_turn_state(turn_state_at_line_start)`
call from the emit-stage `except` arm in `poll()`, re-run `test_multiline_committed_prefix_inner_restore_not_masked_by_outer_guard`
→ it REDS: the arm still `_commit`s (`committed=True`), so the outer guard is SKIPPED and the mutated state
(turn 2 open, `_last_causal_ts` = p2's ts) survives to the retry — the replayed boundary closes "turn 2"
(`turn_index == 2` appears in completed, `3` appears in starts). **Red for exactly the stated reason: the outer
guard cannot mask this window.** Restore → GREEN; record the red output. The first-line test above must STAY
green throughout (it is positive-only). The persisted-ordinal half keeps its own delete-to-red via the Task-7
`test_turn_index_is_restart_stable_…` arg-mutation row.

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k "same_object_retry_after_emit_failure or multiline_committed_prefix" -v` → 2 PASS.

- [ ] **Step 7b: Multi-line dropped-close deny-proof (r2-fold P1 — the cursor-alignment cell)**

Step 7 covers the FIRST-LINE window (`line_start == offset` → store not advanced). The r2 defect lives in the
MULTI-LINE window: an in-turn line emits, advancing the store, THEN the boundary's `turn_completed` emit fails —
so `line_start != offset`, the store advances, and without cursor alignment the retry misfires the discontinuity
check and drops the close. This test exercises exactly that window.

```python
# tests/claude_tail/test_tailer.py — append. Requires `import pytest` (already imported).
def test_multiline_poll_emit_fail_on_close_does_not_drop_turn_completed(tmp_path):
    transcript = tmp_path / "s.jsonl"
    # Phase 1: open turn 1; the poll's end-of-poll cursor lands at EOF (where the in-turn line +
    # boundary will be appended).
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()                                  # turn 1 open; cursor at EOF1

    # Phase 2: append an IN-TURN assistant line (emits fine → advances the offset PAST it) THEN the
    # boundary p2 — so p2 is NOT the first line of the failing poll (line_start_p2 != poll offset).
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
                             "message": {"content": [{"type": "text", "text": "answer one"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z",
                             "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")

    # Inject a one-shot failure on the turn_completed emit itself (so the close is never durable on
    # the first attempt).
    real_route = tailer._route_event
    state = {"boom": True}

    def flaky_route(event):
        if state["boom"] and event.get("event_type") == "turn_completed":
            state["boom"] = False
            raise RuntimeError("injected emit bug on turn_completed[1]")
        return real_route(event)

    tailer._route_event = flaky_route
    with pytest.raises(RuntimeError):
        tailer.poll()                              # multi-line: in-turn line OK, boundary close throws
    tailer.poll()                                  # retry — cursor aligned (fix) → turn NOT abandoned

    by_event = _eval_by_event(redis)
    completed = [p for e, p in by_event if e == "turn_completed"]
    # The close is NOT lost: turn 1's turn_completed is durably emitted on the retry (no false NULL).
    assert len(completed) >= 1, "turn_completed[1] must survive the multi-line emit-fail retry"
    assert all(p.get("turn_index") == 1 for p in completed)
    assert all(p.get("event_ts") == "2026-07-13T19:00:01.000Z" for p in completed)  # turn 1's last-causal ts
```

**Delete-to-red (v5 shape):** remove the cursor half of `_commit` (its two lines `self._cursor_inode = st_ino;
self._cursor_offset = offset`), re-run → the emit-fail prefix-commit advances the store WITHOUT the cursor, so
the retry sees `_cursor_offset (EOF1) != offset (line_start_p2)` → the discontinuity check fires → `_turn_open`
cleared → the replayed boundary opens a FRESH turn → `turn_completed[1]` is NEVER emitted → `len(completed) >= 1`
REDS. Restore → GREEN; record the red output. **This is the deny-proof for the matrix's emit-fail cursor cell** —
it reds because `_commit`'s cursor binding is gone, not for a collateral reason. (The same mutation also reds
Step 7d — together they bracket clause-1 from the prefix-commit and drift-threshold sides.)

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k multiline_poll_emit_fail_on_close -v` → PASS.

- [ ] **Step 7c: SWEPT-SURFACE sole-writer census (clause-1 by construction) + its OWN red-proofs (v6-widened per grok+codex r4)**

The clause-1 claim is structural: `_commit` is the SOLE writer of the persisted offset AND the in-memory cursor.
This step makes that claim machine-checked over the **swept write surface** (Mark's r4 pin — a census green on
the unmigrated present is vacuous in the dangerous direction): an AST census that FAILS the suite on (a) any
cursor-field write outside `_commit` — direct, tuple-unpacked, or constant-name `setattr`; (b) any
`offset_store.store(...)` call outside `_commit`; (c) ANY `offset_store.commit(`/`.get(` call anywhere in
`tailer.py` (the back-compat wrappers are deleted in Task 7 Step 3c — a `.commit()` call is a store write that
does NOT bind the cursor, the exact partial-migration drift of the r4 grok P1); (d) any `return` inside
`poll()`'s guarded `try` (a mid-loop return would bypass the `except`-shaped outer guard); (e) in `offset.py`,
any redis write outside `store()` (`load()` must stay pure — the r4 codex P1). The only admissible non-`_commit`
site is `__init__` None-initialization of the cursor fields. Per verify-the-verifier (the macOS-grep lesson),
**every census class is red-proofed by an always-on test** planting that rogue shape and asserting the census
REDS. Sequencing note: this census is RED on the pre-Task-7 tree — today's `poll()` is all `.commit()`/`.get()`
and today's `offset.py` writes in `commit()` — and that red is the census WORKING (a census green on the
unmigrated present would be vacuous in the dangerous direction, Mark's r4 pin). It goes green when Task 7
migrates `poll()` and Step 3c deletes the wrappers; Step 7c lands AFTER Task 7 in execution order, so the suite
is green at every commit boundary. The non-vacuity proof is the planted-rogue battery, not a first-red.

Add with the existing imports at the top of `tests/claude_tail/test_tailer.py`:

```python
import ast
from pathlib import Path

import agent_redis_bridge.claude_tail.tailer as tailer_module
```

```python
# tests/claude_tail/test_tailer.py — append
def _iter_attr_targets(target):
    """Recurse assignment targets: plain attributes, tuple/list unpacking, starred."""
    if isinstance(target, ast.Attribute):
        yield target
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _iter_attr_targets(element)
    elif isinstance(target, ast.Starred):
        yield from _iter_attr_targets(target.value)


def _enclosing_func_name(tree, node) -> str:
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    best = None
    for f in funcs:
        if f.lineno <= node.lineno <= (f.end_lineno or f.lineno):
            if best is None or f.lineno > best.lineno:
                best = f  # innermost enclosing def wins
    return best.name if best else "<module>"


_CURSOR_FIELDS = ("_cursor_offset", "_cursor_inode")


def _cursor_sole_writer_census(source: str) -> list[str]:
    """v6 swept-surface clause-1 census over tailer.py (grok+codex r4). Forbidden outside
    `_commit`: (a) any write of the cursor fields — direct, tuple-unpacked, or via constant-name
    setattr (setattr is forbidden EVERYWHERE, `_commit` included); (b) `offset_store.store(...)`
    calls; (c) the deleted back-compat `offset_store.commit(`/`.get(` — forbidden EVERYWHERE
    (partial-migration drift: a store write that does not bind the cursor). `__init__` may only
    None-initialize the cursor fields. Guards accidental drift, not an adversary aliasing
    `self.offset_store` — reviewers own that residual (recorded)."""
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            value = getattr(node, "value", None)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for attr in _iter_attr_targets(target):
                    if attr.attr in _CURSOR_FIELDS:
                        where = _enclosing_func_name(tree, node)
                        none_init = (where == "__init__" and isinstance(value, ast.Constant)
                                     and value.value is None)
                        if where != "_commit" and not none_init:
                            violations.append(f"{where}:{node.lineno} writes {attr.attr}")
        elif isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value in _CURSOR_FIELDS):
                violations.append(
                    f"{_enclosing_func_name(tree, node)}:{node.lineno} setattr {node.args[1].value}")
            elif (isinstance(func, ast.Attribute) and func.attr == "__setattr__" and node.args
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value in _CURSOR_FIELDS):
                # v7 (codex r5 P2): self.__setattr__("_cursor_offset", …) is the same write in a coat
                violations.append(
                    f"{_enclosing_func_name(tree, node)}:{node.lineno} setattr {node.args[0].value}")
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute) \
                    and func.value.attr == "offset_store":
                where = _enclosing_func_name(tree, node)
                if func.attr == "store" and where != "_commit":
                    violations.append(f"{where}:{node.lineno} calls offset_store.store")
                elif func.attr in ("commit", "get"):
                    violations.append(f"{where}:{node.lineno} calls forbidden offset_store.{func.attr}")
        elif isinstance(node, ast.Attribute) and node.attr == "redis" \
                and isinstance(node.value, ast.Attribute) and node.value.attr == "offset_store":
            # v7 (grok r5 P2): reaching THROUGH the store to its redis handle ("write the key
            # yourself") splits store from cursor without a .store/.commit call — forbidden.
            violations.append(
                f"{_enclosing_func_name(tree, node)}:{node.lineno} reaches offset_store.redis")
    return violations


def _poll_guarded_try_has_no_return(source: str) -> list[str]:
    """v6 (codex r4 P2): the outer boundary guard is an `except`-shaped mechanism — a mid-loop
    `return` would bypass it entirely. Forbid `return` anywhere inside any `try` within poll()
    (poll's own `return emitted` sits AFTER the guarded try)."""
    tree = ast.parse(source)
    violations = []
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) and func.name == "poll":
            for try_node in ast.walk(func):
                if isinstance(try_node, ast.Try):
                    for sub in ast.walk(try_node):
                        if isinstance(sub, ast.Return):
                            violations.append(f"poll:{sub.lineno} return inside the guarded try")
    return violations


def _offset_module_write_census(source: str) -> list[str]:
    """v6 (codex r4 P1): offset.py sweep — `load()` must be PURE and `store()` the only redis
    writer. Flag any `self.redis.set(...)` outside `store` (the back-compat wrappers are deleted
    by Task 7 Step 3c; a re-added eager `_reset` would land here)."""
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        recv = node.func.value
        if node.func.attr == "set" and isinstance(recv, ast.Attribute) and recv.attr == "redis":
            where = _enclosing_func_name(tree, node)
            if where != "store":
                violations.append(f"{where}:{node.lineno} writes redis outside store()")
        elif (node.func.attr == "store" and isinstance(recv, ast.Name) and recv.id == "self"):
            # v7 (codex r5 P2): a resurrected `_reset -> self.store(...)` has no direct redis.set,
            # but is the exact deleted eager-writer shape — forbid self.store() outside store().
            where = _enclosing_func_name(tree, node)
            if where != "store":
                violations.append(f"{where}:{node.lineno} calls self.store outside store()")
    return violations


def test_commit_is_sole_writer_of_cursor_and_store():
    # Censuses the MODULES ACTUALLY IMPORTED (module.__file__) — the same code the behavior
    # tests exercise, so worktree/editable-install shadowing cannot split them.
    tailer_source = Path(tailer_module.__file__).read_text(encoding="utf-8")
    assert _cursor_sole_writer_census(tailer_source) == []
    assert _poll_guarded_try_has_no_return(tailer_source) == []
    import agent_redis_bridge.claude_tail.offset as offset_module
    offset_source = Path(offset_module.__file__).read_text(encoding="utf-8")
    assert _offset_module_write_census(offset_source) == []


def test_sole_writer_census_reds_on_planted_rogue_writers():
    # Verify-the-verifier: a census that cannot flag a planted rogue is vacuously green. Plant
    # each rogue ON an existing line (same line, semicolon — no indentation guesswork) and assert
    # the census REDS. The tailer anchor MUST land outside __init__/_commit (`self.progressed =
    # True` exists only in poll()) so the red-proof never leans on the __init__ None-init
    # exemption. AST parsing does not resolve names, so planted args need not exist in scope.
    source = Path(tailer_module.__file__).read_text(encoding="utf-8")
    anchor = "self.progressed = True"
    assert anchor in source, "plant anchor missing — the probe would silently no-op"

    def planted(rogue):
        mutated = source.replace(anchor, rogue + "; " + anchor, 1)
        assert mutated != source
        return mutated

    assert any("_cursor_offset" in v for v in _cursor_sole_writer_census(planted("self._cursor_offset = 999")))
    assert any("offset_store.store" in v for v in _cursor_sole_writer_census(planted("self.offset_store.store(key, 0, 0)")))
    # the forbidden back-compat calls (grok r4 P1 — the partial-migration drift shape)
    assert any("forbidden offset_store.commit" in v for v in _cursor_sole_writer_census(planted("self.offset_store.commit(key, 0)")))
    assert any("forbidden offset_store.get" in v for v in _cursor_sole_writer_census(planted("self.offset_store.get(key)")))
    # tuple-unpacking + setattr escapes (codex r4 P2 — found by EXECUTING the v5 census)
    assert any("_cursor_offset" in v for v in _cursor_sole_writer_census(planted("self._cursor_offset, _x = 999, 1")))
    assert any("setattr _cursor_offset" in v for v in _cursor_sole_writer_census(planted("setattr(self, '_cursor_offset', 999)")))
    # v7 (codex/grok r5 P2s): the dunder-setattr coat + the reach-through-to-redis escape
    assert any("setattr _cursor_offset" in v for v in _cursor_sole_writer_census(planted("self.__setattr__('_cursor_offset', 999)")))
    assert any("reaches offset_store.redis" in v for v in _cursor_sole_writer_census(planted("self.offset_store.redis.set(key, 0)")))
    # mid-loop return bypassing the outer guard (codex r4 P2)
    assert any("return inside" in v for v in _poll_guarded_try_has_no_return(planted("return 0")))

    # offset.py sweep red-proofs: the deleted eager-writer shapes (direct redis.set AND the
    # v7 self.store() resurrection — codex r5 P2)
    import agent_redis_bridge.claude_tail.offset as offset_module
    offset_source = Path(offset_module.__file__).read_text(encoding="utf-8")
    offset_anchor = "raw = raw.decode()"
    assert offset_anchor in offset_source, "offset.py plant anchor missing"
    rogue_offset = offset_source.replace(offset_anchor, 'self.redis.set("k", "0"); ' + offset_anchor, 1)
    assert any("outside store()" in v for v in _offset_module_write_census(rogue_offset))
    rogue_reset = offset_source.replace(offset_anchor, 'self.store(key, 0, 0); ' + offset_anchor, 1)
    assert any("self.store outside store()" in v for v in _offset_module_write_census(rogue_reset))
```

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k sole_writer -v` → 2 PASS. The census
needs no manual delete-to-red in Task 13 Step 3 — its red-proof IS the second test, and both run on every suite
invocation.

- [ ] **Step 7d: Drift-threshold keeps the cursor aligned — no false NULL (v5 fixes the r3 in-cell finding)**

The r3 2–2 split lived on this cell: pre-v5, the drift-threshold exit advanced the store WITHOUT the cursor, so
the next same-object poll saw a false discontinuity and abandoned a turn that was legitimately open (a false
NULL). In v5 the threshold path `_commit`s, so clause-1 holds by construction. Two-phase, same object; requires
`from agent_redis_bridge.claude_tail.tailer import _DriftThresholdExceeded` added to the existing tailer import.

```python
# tests/claude_tail/test_tailer.py — append. Requires `import pytest` (already imported) and
# `_DriftThresholdExceeded` (add to the existing `from ...tailer import` line).
def test_drift_threshold_keeps_cursor_aligned_no_false_null(tmp_path):
    transcript = tmp_path / "s.jsonl"
    # Phase 1: turn 1 opens and stays open; _commit lands the cursor at EOF1.
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
    )
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()                                  # turn 1 open; cursor == store.offset == EOF1
    eof1 = transcript.stat().st_size

    # Phase 2: append a valid-JSON unknown-type line (DriftError) + the boundary p2. With
    # drift_threshold = 0 the drift line trips the threshold: v5 _commit(new_offset past the
    # drift line) then raise — sticky-fail, SAME object, turn 1 kept open + clock poisoned.
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "some_unknown_future_type", "timestamp": "2026-07-13T19:00:05.000Z",
                             "message": {"content": [{"type": "text", "text": "?"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:10.000Z",
                             "message": {"content": [{"type": "text", "text": "next"}]}}) + "\n")
    tailer.drift_threshold = 0
    with pytest.raises(_DriftThresholdExceeded):
        tailer.poll()

    # Clause-1 held ON the threshold exit (the direct structural assert): the store advanced
    # past the drift line (forward progress — anti-vacuity: _commit really ran on this path)
    # AND the in-memory cursor is bound to it.
    key = offset_key(str(transcript), transcript.stat().st_ino)
    pos = store.load(key)
    assert pos.offset > eof1, "threshold exit must commit forward progress past the drift line"
    assert (tailer._cursor_inode, tailer._cursor_offset) == (transcript.stat().st_ino, pos.offset)

    # Next same-object poll: NO false discontinuity — the open turn is NOT falsely abandoned.
    # It closes at p2 with turn_clock_monotonic=false (the drift poison) ⇒ NULL downstream:
    # the CORRECT outcome — not a phantom close, not a duplicate, not a silent drop.
    tailer.poll()
    by_event = _eval_by_event(redis)
    completed = [p for e, p in by_event if e == "turn_completed"]
    assert len(completed) == 1, "turn 1 must close exactly once (no false NULL, no duplicate)"
    assert completed[0].get("turn_index") == 1
    assert completed[0].get("turn_clock_monotonic") is False   # drift-poisoned ⇒ NULL, per Option-B
    starts = [p.get("turn_index") for e, p in by_event if e == "turn_started"]
    assert 2 in starts and 0 not in starts                     # p2 opened turn 2; no phantom re-count
```

**Delete-to-red:** remove the cursor half of `_commit` (its two lines `self._cursor_inode = st_ino;
self._cursor_offset = offset`) → the threshold exit advances the store alone → the retry sees
`_cursor_offset (EOF1) != offset (past-drift)` → false discontinuity → turn 1 abandoned → `len(completed) == 1`
REDS (the false NULL resurrected); the direct clause-1 assert reds first with the exact divergence. Restore →
GREEN; record the red output. (The same mutation also reds Step 7b — the two tests bracket `_commit`'s clause-1
from the prefix-commit and drift-threshold sides.)

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k drift_threshold_keeps_cursor_aligned -v` → PASS.

- [ ] **Step 7e: readline-OSError mid-poll — the outer boundary guard's deny-proof (v5 clause-2; closes the r3 missing-row)**

The r3 missing-row: `readline()`/`tell()` can raise `OSError` AFTER a boundary line emitted (turn-state mutated
to N+1) but BEFORE any commit. Pre-v5 nothing restored turn-state on that exit; the retry would mis-stamp a
phantom close. In v5 the outer boundary guard restores the poll-start snapshot on every uncommitted exit. Note
what at-least-once makes EXPECTED here: the failed poll's correctly-stamped `turn_completed[1]` was already
routed durably, and the retry re-emits the SAME edge (offset never advanced) — a duplicate of a correct edge is
the at-least-once contract. What the guard prevents is the MIS-STAMPED close: a `turn_completed` carrying
`turn_index == 2` (and a `turn_started` at 3) that exists for no real turn.

The injection shadows the tailer MODULE's `open` (module globals win over builtins for code in that module), and
is one-shot so the retry poll opens cleanly:

```python
# tests/claude_tail/test_tailer.py — append. Requires `import pytest` and the
# `import agent_redis_bridge.claude_tail.tailer as tailer_module` from Step 7c.
class _FlakyReadFile:
    """Wraps the real transcript file; readline raises OSError after ok_reads successes."""

    def __init__(self, fh, ok_reads):
        self._fh = fh
        self._left = ok_reads

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False

    def seek(self, *args):
        return self._fh.seek(*args)

    def tell(self):
        return self._fh.tell()

    def readline(self):
        if self._left <= 0:
            raise OSError("injected mid-poll I/O failure")
        self._left -= 1
        return self._fh.readline()


def _arm_flaky_open(monkeypatch, ok_reads):
    """One-shot: the NEXT open() inside tailer.py raises OSError on readline number
    ok_reads+1; every later open is clean (so the retry poll reads normally)."""
    state = {"armed": True}

    def flaky_open(path, mode="rb", *args, **kwargs):
        fh = open(path, mode, *args, **kwargs)
        if state["armed"]:
            state["armed"] = False
            return _FlakyReadFile(fh, ok_reads)
        return fh

    monkeypatch.setattr(tailer_module, "open", flaky_open, raising=False)


def test_readline_oserror_midpoll_does_not_replay_mutated_turn_state(tmp_path, monkeypatch):
    transcript = tmp_path / "s.jsonl"
    # Phase 1: turn 1 opens and stays open; _commit lands the cursor at EOF1 (so the retry's
    # discontinuity check does NOT fire and mask the defect — same window discipline as Step 7).
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()                                  # turn 1 open; cursor == store.offset == EOF1

    # Phase 2: append an in-turn assistant line + the boundary p2. The failing poll reads both
    # (2 successful readlines — boundary emits: turn_completed[1] + turn_started[2], turn-state
    # mutated to turn 2), then the THIRD readline raises OSError with NOTHING committed.
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
                             "message": {"content": [{"type": "text", "text": "answer one"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z",
                             "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")
    _arm_flaky_open(monkeypatch, ok_reads=2)
    with pytest.raises(OSError):
        tailer.poll()

    # Anti-vacuity: the boundary DID emit before the OSError — we really are in the
    # mutated-then-uncommitted window (else this test proves nothing about the guard).
    mid = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert len(mid) == 1 and mid[0].get("turn_index") == 1

    tailer.poll()                                  # SAME object retry; guard restored poll-start state

    by_event = _eval_by_event(redis)
    completed = [p for e, p in by_event if e == "turn_completed"]
    # Every close is turn 1's, stamped with turn 1's last-causal ts — the retry re-emitted the
    # SAME edge (at-least-once), it did NOT manufacture a phantom turn-2 close.
    assert all(p.get("turn_index") == 1 for p in completed)
    assert all(p.get("event_ts") == "2026-07-13T19:00:01.000Z" for p in completed)
    starts = [p.get("turn_index") for e, p in by_event if e == "turn_started"]
    assert 2 in starts and 3 not in starts         # turn 2 re-opens; mutated state never survived


def test_readline_oserror_after_inloop_skip_still_restores_poll_start(tmp_path, monkeypatch):
    # Sneaky-exit variant (Mark Pin 1): the guard is a discipline mechanism, so its residual
    # risk lives on exits that pass THROUGH in-loop swallowed exceptions. Here a generic-skip
    # line (unparseable → except/continue, poisons the clock) runs BEFORE the boundary and the
    # OSError — the guard must still restore the poll-start snapshot afterwards.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()                                  # turn 1 open; cursor at EOF1
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write("{ this is not valid json\n")     # generic-skip arm: swallowed in-loop + clock poison
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
                             "message": {"content": [{"type": "text", "text": "answer one"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z",
                             "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")
    _arm_flaky_open(monkeypatch, ok_reads=3)       # skip + assistant + boundary read, 4th raises
    with pytest.raises(OSError):
        tailer.poll()
    tailer.poll()                                  # SAME object retry

    by_event = _eval_by_event(redis)
    completed = [p for e, p in by_event if e == "turn_completed"]
    assert completed, "turn 1's close must survive the retry"
    assert all(p.get("turn_index") == 1 for p in completed)          # no phantom turn-2 close
    # The skip's clock poison is re-applied on replay (idempotent), so every close is false ⇒ NULL.
    assert all(p.get("turn_clock_monotonic") is False for p in completed)
    starts = [p.get("turn_index") for e, p in by_event if e == "turn_started"]
    assert 3 not in starts
```

**Delete-to-red:** remove the outer-guard restore (`if not committed: self._restore_turn_state(poll_start_turn_state)`
in `poll()`'s outer `except Exception`) → the failed poll leaves turn-state at turn 2, so the retry treats the
replayed boundary as closing "turn 2": a `turn_completed` with `turn_index == 2` and a `turn_started` at 3 appear
→ BOTH tests red (`all(turn_index == 1)` and `3 not in starts` flip). Restore → GREEN; record the red output.
This is the clause-2 deny-proof: it reds because the guard is gone, and the sneaky-exit variant proves the guard
covers exits reached through in-loop swallowed exceptions, not just the clean raise path.

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k readline_oserror -v` → 2 PASS.

- [ ] **Step 7f: Same-object truncate-heal abandons the open turn (residue deny-proof; v6 re-keyed to the EXPLICIT abandon)**

`test_iv` is fresh-object; this covers the SAME-OBJECT truncate. **v6 re-key (cold-Opus r4 fold):** the heal now
`_commit`s (aligning the cursor), so the abandon no longer rides the discontinuity check — it is an EXPLICIT
`_turn_clock_ok = False; _turn_open = False` at the heal, immediately AFTER the `_commit` (v7 reorder). That
explicit abandon is the residue guard this test flips.

```python
# tests/claude_tail/test_tailer.py — append
def test_same_object_truncate_heal_abandons_open_turn(tmp_path):
    transcript = tmp_path / "s.jsonl"
    # Turn 1 opens; the assistant line is padded so the committed offset lands well past the
    # truncated generation's size (the heal precondition offset > st_size must actually hold).
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "text",
                                  "text": "a long answer that pads the transcript well past the size "
                                          "of the truncated replacement generation below"}]}},
    )
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()                                  # turn 1 open; store.offset == cursor == EOF1
    eof1 = transcript.stat().st_size

    # SAME path + SAME inode, truncated BELOW the stored offset: a shorter new generation.
    short = json.dumps({"type": "user", "promptId": "p9", "timestamp": "2026-07-13T19:10:00.000Z",
                        "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n"
    with open(transcript, "r+", encoding="utf-8") as fh:
        fh.seek(0)
        fh.write(short)
        fh.truncate()
    assert transcript.stat().st_size < eof1        # anti-vacuity: the heal branch really triggers

    tailer.poll()  # truncate-heal: _commit(key, 0, 0, st_ino) FIRST (v7), then EXPLICIT abandon of turn 1

    by_event = _eval_by_event(redis)
    completed = [p for e, p in by_event if e == "turn_completed"]
    assert not completed, "abandoned straddled turn must emit NO turn_completed (⇒ NULL; recovery is 5a O5)"
    starts = [p.get("turn_index") for e, p in by_event if e == "turn_started"]
    assert starts == [1, 1]  # gen-1 opened turn 1; the fresh generation re-counts from 1 — idempotent
```

**Delete-to-red (v6 re-key — delete the EXPLICIT abandon):** remove the heal's
`if self._turn_open: self._turn_clock_ok = False; self._turn_open = False` block (keep its `_commit`) → the
`_commit`-aligned cursor keeps the discontinuity row SILENT → `_turn_open` survives the truncate → the new
generation's p9 line CLOSES the stale turn → a phantom `turn_completed` appears (at `turn_index == 0`, and with
a clean clock it carries a forged `true`) → `assert not completed` REDS. Restore → GREEN; record the red output.
This is the residue proof that the explicit abandon is load-bearing — exactly where the matrix says the per-path
reasoning now lives. (The heal's OTHER half — the `_commit` persist — has its own deny-proof in Step 7g.)

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k truncate_heal_abandons -v` → PASS.

- [ ] **Step 7g: Truncate-to-EMPTY persists the heal BEFORE regrowth (v6 — the r4 cold-Opus zero-lines/regrowth window)**

The r4 P1's exact mechanism: if the heal is in-memory only and the healing poll reads ZERO complete lines, the
end-of-poll `_commit` never runs — the stale pre-truncate offset survives in the store, and once the file regrows
past it, `offset > st_size` no longer holds, the heal never re-derives, and the tailer seeks MID-STREAM into the
new generation: silently lost turns + stale ordinals. v6's heal-`_commit` closes it; this test pins the window.

```python
# tests/claude_tail/test_tailer.py — append
def test_truncate_to_empty_persists_heal_before_regrowth(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "text", "text": "gen-1 answer"}]}},
    )
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()                                  # turn 1 open; store.offset == EOF1
    eof1 = transcript.stat().st_size

    with open(transcript, "r+", encoding="utf-8") as fh:
        fh.truncate(0)                             # SAME inode, now EMPTY
    tailer.poll()                                  # heal poll reads ZERO complete lines
    key = offset_key(str(transcript), transcript.stat().st_ino)
    # The load-bearing assert: the reset was persisted AT the heal (not by the end-of-poll
    # commit, which never ran — zero lines were read).
    assert store.load(key) == Position(0, 0)

    # Regrow PAST the old offset: p9's line alone is padded longer than eof1, so a stale-offset
    # seek would land MID-LINE inside it (the exact r4 mechanism).
    pad = "x" * eof1
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "promptId": "p9", "timestamp": "2026-07-13T19:10:00.000Z",
                             "message": {"content": [{"type": "text", "text": pad}]}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:10:01.000Z",
                             "message": {"content": [{"type": "text", "text": "reply"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p10", "timestamp": "2026-07-13T19:11:00.000Z",
                             "message": {"content": [{"type": "text", "text": "next"}]}}) + "\n")
    assert transcript.stat().st_size > eof1        # regrowth precondition actually holds
    tailer.poll()

    by_event = _eval_by_event(redis)
    starts = [p.get("turn_index") for e, p in by_event if e == "turn_started"]
    # The regrown generation was read from byte 0 — NOTHING skipped: gen-1's turn 1, then the new
    # generation's p9 (turn 1) and p10 (turn 2). A mid-stream seek would have LOST p9's open.
    assert starts == [1, 1, 2]
    completed = [p.get("turn_index") for e, p in by_event if e == "turn_completed"]
    assert completed == [1]                        # p9's turn closed once by p10 — correct ordinal
```

Note: this test needs `Position` imported in `test_tailer.py` (add to the existing offset import:
`from agent_redis_bridge.claude_tail.offset import OffsetStore, Position, offset_key`).

**Delete-to-red (the persist half of the heal):** mutate the heal back to the v5 in-memory-only shape (delete its
`self._commit(key, 0, 0, st_ino)` line) → the heal poll persists nothing (zero lines ⇒ no end-of-poll commit
either) → the regrown poll loads the STALE `eof1` offset, `offset > st_size` no longer holds, and the seek lands
mid-line inside p9's padded record → p9's `turn_started` is never emitted → `starts == [1, 1, 2]` REDS (and
`completed == [1]` reds with it). **Red by exactly the r4 mechanism: silent lost turns from a mid-stream
resume.** Restore → GREEN; record the red output.

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k truncate_to_empty_persists -v` → PASS.

- [ ] **Step 7h: Drift-emit-FAILURE arm deny-proof (v6 — codex r4 P1: the OTHER arm of the drift branch)**

`test_iii_drifterror…` exercises the drift SUCCESS arm (emission works, poison lands). This covers the FAILURE
arm: a non-Redis bug inside `_emit_drift_error` → inner drift-arm restore + prefix `_commit` + raise. Two-phase
and multi-line so the prefix-commit really runs (`line_start != offset`).

```python
# tests/claude_tail/test_tailer.py — append
def test_drift_emit_failure_prefix_commits_and_replays_drift_line(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
    )
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()                                  # turn 1 open; cursor == store.offset == EOF1
    eof1 = transcript.stat().st_size

    # Multi-line window: an in-turn assistant line, THEN a valid-JSON unknown-type line (DriftError).
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
                             "message": {"content": [{"type": "text", "text": "answer one"}]}}) + "\n")
        fh.write(json.dumps({"type": "some_unknown_future_type", "timestamp": "2026-07-13T19:00:05.000Z",
                             "message": {"content": [{"type": "text", "text": "?"}]}}) + "\n")

    real_emit = tailer._emit_drift_error
    state = {"boom": True}

    def flaky_emit(exc):
        if state["boom"]:
            state["boom"] = False
            raise RuntimeError("injected drift-emit bug")
        return real_emit(exc)

    tailer._emit_drift_error = flaky_emit
    with pytest.raises(RuntimeError):
        tailer.poll()                              # assistant emits OK; drift emit FAILS → prefix _commit + raise

    # The arm prefix-committed past the assistant line AND bound the cursor (clause-1 on this arm).
    key = offset_key(str(transcript), transcript.stat().st_ino)
    pos = store.load(key)
    assert pos.offset > eof1
    assert (tailer._cursor_inode, tailer._cursor_offset) == (transcript.stat().st_ino, pos.offset)

    tailer.poll()                                  # retry replays ONLY the drift line; emission succeeds
    drift_payloads = [p for e, p in _eval_by_event(redis) if e == "drift_error"]
    assert len(drift_payloads) == 1                # the failed drift emission became durable EXACTLY once
    # v7 (codex r5 P1): drift_count is non-idempotent pre-emit state OUTSIDE the turn-state
    # restore — without the failure-arm rollback the retry double-counts this ONE line (count=2,
    # threshold crossable a line early). The rollback makes the retry's re-count single.
    assert drift_payloads[0].get("count") == 1
    assert tailer.drift_count == 1

    # Close correctness: the drift poison survives to the eventual close (⇒ NULL downstream).
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z",
                             "message": {"content": [{"type": "text", "text": "next"}]}}) + "\n")
    tailer.poll()
    completed = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert len(completed) == 1 and completed[0].get("turn_index") == 1
    assert completed[0].get("turn_clock_monotonic") is False
```

**Delete-to-reds (both flippable halves):** (a) arg-mutate the drift-FAILURE arm's prefix commit —
`self._commit(key, line_start, ordinal_at_line_start, st_ino)` → `self._commit(key, new_offset,
self.logical_turn_index, st_ino)` — re-run → the retry starts PAST the drift line, the failed emission is never
replayed, the durable `drift_error` is lost forever → `len(drift_payloads) == 1` REDS. (b) delete the arm's
`self.drift_count -= 1` rollback → the retry re-counts the same line → `count == 2` on the durable event →
`drift_payloads[0].get("count") == 1` and `tailer.drift_count == 1` RED. Restore → GREEN; record both red
outputs. **Recorded reasoning (REWRITTEN in v7 per the r5 three-seat convergence):** the v6 claim that "nothing
mutates pre-emit except the idempotent clock poison" was WRONG twice over — (i) `user`/`assistant`-typed lines
that DriftError inside `map_line` DO mutate `_last_causal_ts` before the raise (agy/grok/cold-Opus), but that
write is REPLAY-IDEMPOTENT: the retry re-parses the identical line and re-derives the identical value, so
deleting the restore still produces no observable divergence from that state — do NOT add a vacuous red for it;
(ii) the genuinely NON-idempotent pre-emit state is `drift_count` (codex), which the turn-state restore never
covered — now rolled back in the arm with delete-to-red (b) above. The arm's `_restore_turn_state` half remains
proven via the shared snapshot/restore mechanism (Step 7's re-keyed delete-to-red).

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k drift_emit_failure -v` → PASS.

- [ ] **Step 7i: Heal followed by an uncommitted failure — the heal/guard overlap proof (v7, codex r5 P1; the guard-overlap standing rule applied to the v6 fold itself)**

The v6 heal introduced a `_commit` that deliberately does NOT set `committed` and a guard snapshot taken AFTER
the heal-abandon. Neither choice had a flipping proof, and the natural drift — `committed = True` at the heal,
matching the nearby "ANY `_commit` this poll" comment — would make the outer guard skip its restore after a
later failure, leaking mutated new-generation turn-state to the retry. This test red-proofs BOTH choices.
Reuses `_FlakyReadFile`/`_arm_flaky_open` from Step 7e.

```python
# tests/claude_tail/test_tailer.py — append. Requires `import pytest` and Step 7e's helpers.
def test_heal_then_uncommitted_failure_restores_post_abandon_state(tmp_path, monkeypatch):
    transcript = tmp_path / "s.jsonl"
    # Gen-1: turn 1 opens; assistant padded so the NEW generation (3 short lines) is smaller.
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "text",
                                  "text": "padding " * 120}]}},
    )
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store)
    tailer.poll()                                  # gen-1 turn 1 open; store.offset == EOF1
    eof1 = transcript.stat().st_size

    # SAME inode, truncated to a SHORTER new generation: p9 + assistant9 + boundary p10.
    new_gen = (
        json.dumps({"type": "user", "promptId": "p9", "timestamp": "2026-07-13T19:10:00.000Z",
                    "message": {"content": [{"type": "text", "text": "new gen"}]}}) + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:10:01.000Z",
                      "message": {"content": [{"type": "text", "text": "reply"}]}}) + "\n"
        + json.dumps({"type": "user", "promptId": "p10", "timestamp": "2026-07-13T19:11:00.000Z",
                      "message": {"content": [{"type": "text", "text": "next"}]}}) + "\n"
    )
    with open(transcript, "r+", encoding="utf-8") as fh:
        fh.seek(0); fh.write(new_gen); fh.truncate()
    assert transcript.stat().st_size < eof1        # the heal precondition actually holds

    # The healing poll reads p9 (opens new turn 1), assistant9, p10 (closes 1 durably, opens 2 —
    # turn-state mutated), then the 4th readline raises OSError with committed == False (the
    # heal's own _commit deliberately does not count).
    _arm_flaky_open(monkeypatch, ok_reads=3)
    with pytest.raises(OSError):
        tailer.poll()

    # Anti-vacuity: the boundary DID emit before the OSError (we are in the mutated window), and
    # the heal's persist stands (offset 0 durable even though the poll failed).
    mid = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert len(mid) == 1 and mid[0].get("turn_index") == 1
    key = offset_key(str(transcript), transcript.stat().st_ino)
    assert store.load(key).offset == 0

    tailer.poll()                                  # SAME object retry: replays the new generation

    by_event = _eval_by_event(redis)
    completed = [p for e, p in by_event if e == "turn_completed"]
    # Every close is the NEW generation's turn 1 (p10's close, re-emitted on retry — at-least-once).
    # No phantom gen-1 close (turn_index 0) and no phantom turn-2 close (turn_index 2).
    assert completed and all(p.get("turn_index") == 1 for p in completed)
    assert all(p.get("event_ts") == "2026-07-13T19:10:01.000Z" for p in completed)
    starts = [p.get("turn_index") for e, p in by_event if e == "turn_started"]
    assert 2 in starts and 3 not in starts and 0 not in starts
```

**Delete-to-reds (both v6 design choices; v8 corrects mutation (a) — grok r6 P1):** (a) the naive mutation "set
`committed = True` at the heal" is DEAD CODE — `committed = False` is initialized AFTER the heal block, so the
full drift-simulating mutation is: **move the `committed = False` initialization ABOVE the heal, then set
`committed = True` at the heal's `_commit`** → the later OSError skips the outer restore → the mutated state
survives; the retry's poll-top rebind (`store == (0,0)` ⇒ `logical = 0`) means the leaked `_turn_open` produces
a **phantom `turn_index == 0` close** (grok + cold-Opus r6 convergence — NOT turn-2; the poll-top logical reset
clobbers the leaked ordinal) → `all(turn_index == 1)` REDS. (b) move the guard snapshot BEFORE the heal (so it
captures the PRE-abandon gen-1 state) → the restore resurrects gen-1's open turn → the retry's p9 phantom-closes
it at the reset ordinal (`turn_index == 0` in completed, with gen-1's `event_ts`) → REDS. Restore → GREEN;
record both red outputs. This is the r4 guard-overlap standing rule applied to the v6 fold's own new guard.

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k heal_then_uncommitted -v` → PASS.

- [ ] **Step 7j: Missing-timestamp clock arms + synthetic-edge missing-ts counter (v7 — cold-Opus + agy r5 P2s)**

True-predicate 3 says a MISSING required `timestamp` ⇒ `false` — that arm had no deny-proof (its failure mode is
a forged `true`). And the synthetic `turn_started`/`turn_completed` edges bypass `_stamp_event_ts`, so a
boundary line without a timestamp never bumped `claude_tail_missing_ts` (agy). Implementation additions: in
`_close_and_open_turn`, bump `self.claude_tail_missing_ts += 1` when `line_ts` is falsy (the synthetic edges'
counter parity with Task 3); the clock arms already exist (`_observe_clock`'s missing-ts branch; `_turn_clock_ok
= bool(line_ts)` at open).

```python
# tests/claude_tail/test_tailer.py — append
def test_missing_timestamp_on_in_turn_record_is_false(tmp_path):
    # the missing-ts arm of _observe_clock: an in-turn record WITHOUT `timestamp` poisons the turn.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant",  # NO timestamp — required record missing its clock reading
         "message": {"content": [{"type": "text", "text": "answer"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:05.000Z",
         "message": {"content": [{"type": "text", "text": "next"}]}},
    )
    redis = FakeRedis()
    _tailer(transcript, redis).poll()
    assert _final_flag(redis) is False


def test_missing_timestamp_on_opening_record_is_false_and_counted(tmp_path):
    # the opening arm: `_turn_clock_ok = bool(line_ts)` — a turn opened by a timestamp-less human
    # prompt can NEVER earn true; and the synthetic edges bump the missing-ts counter (agy r5).
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1",  # NO timestamp on the opening record
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "text", "text": "answer"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:05.000Z",
         "message": {"content": [{"type": "text", "text": "next"}]}},
    )
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)
    tailer.poll()
    assert _final_flag(redis) is False
    assert tailer.claude_tail_missing_ts >= 1      # the ts-less boundary's synthetic edges counted
```

**Delete-to-reds:** (a) delete `_observe_clock`'s missing-ts poison (`self._turn_clock_ok = False; return` on a
falsy ts) → the ts-less in-turn record no longer poisons, later records look monotonic → forged `true` →
`test_missing_timestamp_on_in_turn_record_is_false` REDS. (b) mutate the open-arm `self._turn_clock_ok =
bool(line_ts)` → `True` → a ts-less opening earns a forged `true` → the second test REDS on the flag assert.
(c) **(v8, codex r6 P2)** delete the `_close_and_open_turn` counter bump (`self.claude_tail_missing_ts += 1`)
→ the second test REDS specifically on its counter assertion (the increment has its own adversarial proof).
Restore → GREEN; record all three red outputs.

Run: `uv run --extra arb-memory pytest tests/claude_tail/test_tailer.py -k missing_timestamp -v` → 2 PASS.

- [ ] **Step 8: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/tailer.py tests/claude_tail/test_tailer.py
git commit -m "feat(5a-0): turn_clock_monotonic single-continuous-observation flag + per-line turn-state transaction, no terminal earn (Option-B/D)"
```

---

## Task 9: dispatch `attempt_epoch` — INCR source, snapshot-once, stamped on every eval event (Deliverable 2)

**Files:**
- Modify: `src/agent_redis_bridge/redis_io.py` (add `task_epoch_key`), `src/agent_redis_bridge/bridge.py` (allocate + snapshot + stamp + cleanup)
- Test: `tests/test_bridge_attempt_epoch.py` *(create)*

**Interfaces:**
- Consumes: the allowlist admits `attempt_epoch` (Task 2); `RedisCli.incrby(key, amount, ttl=)` (`redis_io.py:279`).
- Produces: EVERY dispatch eval event (all flow through `push_task_event` → `_tee_eval_event`, `bridge.py:2442/2465`) carries `data["attempt_epoch"]` — a per-`(run_id,task_id)` monotonic int, **allocated once** at accepted-worker admission via `INCR task:{task_id}:epoch`, snapshotted into `self._task_epoch[task_id]`, and stamped on every event of that execution. Recovery re-run → a fresh `process_request` → a fresh INCR → strictly higher epoch (by construction). A lease-takeover successor's INCR on the shared counter strictly exceeds the predecessor's snapshot.

**Design note (SP0-1 PINNED):** key = `RedisConfig.key(f"task:{task_id}:epoch")`, **no `run_id` dimension** (`task_id` = `envelope.id` is a per-request UUIDv4, stable across recovery; the task substrate is already `task_id`-only). Allocate inside `process_request` (`bridge.py:1235`) BEFORE the first `task_started` push (`bridge.py:1264`). Snapshot into a per-task map so the async eval flusher reads one scalar; a stale predecessor keeps its lower cached value.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bridge_attempt_epoch.py
import pytest
from agent_redis_bridge.eval_tee import extract_eval_payload


class FakeRedisCli:
    def __init__(self):
        self.counters = {}
    def incrby(self, key, amount, ttl=None):
        self.counters[key] = self.counters.get(key, 0) + amount
        return self.counters[key]


# Small harness mirroring the bridge's epoch helpers (the real methods live on Bridge; these tests
# pin the pure logic the implementation must expose as _allocate_attempt_epoch / _stamp_attempt_epoch).
class _EpochMixinHarness:
    def __init__(self, redis, prefix="p:"):
        self.redis = redis
        self._prefix = prefix
        self._task_epoch = {}
        self._events_ttl = 3600
    def _epoch_key(self, task_id):
        return f"{self._prefix}task:{task_id}:epoch"
    def _allocate_attempt_epoch(self, task_id):
        epoch = self.redis.incrby(self._epoch_key(task_id), 1, ttl=self._events_ttl)
        self._task_epoch[task_id] = epoch
        return epoch
    def _stamp_attempt_epoch(self, task_id, data):
        epoch = self._task_epoch.get(task_id)
        if epoch is not None:
            data.setdefault("attempt_epoch", epoch)
        return data


def test_snapshot_once_all_events_of_one_execution_share_one_epoch():
    h = _EpochMixinHarness(FakeRedisCli())
    h._allocate_attempt_epoch("task-1")
    d1, d2 = h._stamp_attempt_epoch("task-1", {}), h._stamp_attempt_epoch("task-1", {})
    assert d1["attempt_epoch"] == d2["attempt_epoch"] == 1
    assert extract_eval_payload(d1)["attempt_epoch"] == 1  # survives the allowlist


def test_recovery_rerun_allocates_a_strictly_higher_epoch():
    redis = FakeRedisCli()
    h = _EpochMixinHarness(redis)
    e1 = h._allocate_attempt_epoch("task-1")     # first execution
    e2 = h._allocate_attempt_epoch("task-1")     # recovery re-run (fresh process_request)
    assert e2 > e1 == 1 and e2 == 2


def test_lease_takeover_successor_epoch_exceeds_live_predecessor():
    redis = FakeRedisCli()
    pred = _EpochMixinHarness(redis)
    succ = _EpochMixinHarness(redis)
    pred._allocate_attempt_epoch("task-1")       # predecessor snapshots epoch 1
    succ._allocate_attempt_epoch("task-1")       # successor INCRs the SHARED counter → 2
    stale = pred._stamp_attempt_epoch("task-1", {})   # predecessor keeps stamping its OLD snapshot
    fresh = succ._stamp_attempt_epoch("task-1", {})
    assert stale["attempt_epoch"] == 1 and fresh["attempt_epoch"] == 2
```

The three tests above define the CONTRACT on the inline `_EpochMixinHarness` (keep them — they document the
intended snapshot/recovery/takeover semantics). **They are NOT the deny-proof:** deleting the real production
guard leaves them green (mirror-harness vacuity — codex-sol #2, r1 P1-3). The real deny-proof drives a REAL
`Bridge`, below.

**r1-fold P1-3 — REAL-`Bridge` snapshot-once test + delete-to-red (the load-bearing deny-proof).** Add this to
`tests/test_bridge_attempt_epoch.py`; its delete-to-red hits PRODUCTION `push_task_event`, not the harness:

```python
def test_real_bridge_snapshot_once_stamps_same_epoch_on_every_eval_event():
    # r1-fold P1-3: the delete-to-red must hit PRODUCTION. Drive a real Bridge via the __new__ pattern
    # (tests/test_push_task_event_tee.py:36-48) and inspect BOTH durable eval payloads.
    import json
    from types import SimpleNamespace
    from agent_redis_bridge.bridge import Bridge
    from agent_redis_bridge.redis_io import RedisConfig
    from agent_redis_bridge.envelope import Envelope

    class _FakeRedis:  # events + live xadd, plus incrby for epoch allocation
        def __init__(self):
            self.xadds, self.counters = [], {}
        def xadd(self, key, fields, *, maxlen=None, ttl=None):
            self.xadds.append((key, fields)); return "1-0"
        def incrby(self, key, amount, ttl=None):
            self.counters[key] = self.counters.get(key, 0) + amount; return self.counters[key]
        def expire(self, *a, **k):
            return True

    class _RecordingEval:
        def __init__(self):
            self.xadds = []
        def xadd(self, key, fields):
            self.xadds.append((key, fields)); return "1-0"

    b = Bridge.__new__(Bridge)
    b.redis_config = RedisConfig("127.0.0.1", "6379", "15", "agent_scratch:")
    b.redis = _FakeRedis()
    b.args = SimpleNamespace(max_task_events=500, events_ttl=60)
    b.agent_id = "codex-test"
    b.eval_redis = _RecordingEval()
    b._eval_stream = "eval:events"
    b._task_epoch = {}
    b._task_turn_index = {}          # Task 10 map; set up-front so push_task_event stays valid post-Task-10

    env = Envelope(id="task-1", sender="claude", branch="manual", recipient="codex",
                   kind="request", sent_at="x", payload={"task": "x"}, run_id="run-1")
    # Allocate ONCE, mirroring process_request's admission-time INCR + snapshot.
    b._task_epoch[env.id] = b.redis.incrby(b.redis_config.task_epoch_key(env.id), 1, ttl=60)

    b.push_task_event(env, "command_started", {"tool_name": "Bash", "tool_use_id": "toolu_1"})
    b.push_task_event(env, "command_finished", {"tool_name": "Bash", "tool_use_id": "toolu_1"})

    evals = [json.loads(fields["payload"]) for _key, fields in b.eval_redis.xadds]
    assert len(evals) == 2
    assert [e.get("attempt_epoch") for e in evals] == [1, 1]   # ONE snapshot, shared across both events
```

**Delete-to-red:** in production `push_task_event`, replace the snapshot read
`epoch = self._task_epoch.get(request.id)` with a per-event `epoch = self.redis.incrby(self.redis_config.task_epoch_key(request.id), 1)`,
re-run this test → it REDS (`[2, 3] != [1, 1]` — each event re-INCRs). Restore → GREEN; record the red output.
A pure *delete* of the `push_task_event` stamp (or of the `process_request` snapshot line) instead KeyErrors /
drops the field — collateral, NOT the split-epochs failure — so the delete-to-red is an **arg/body mutation**, not
a line-delete (r1-fold P2).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra arb-memory pytest tests/test_bridge_attempt_epoch.py -v`
Expected: the harness contract tests PASS (they define the mixin inline). `test_real_bridge_snapshot_once_…` FAILS
until Step 3 wires the epoch into `Bridge.__init__` + `push_task_event` (`AttributeError: 'Bridge' object has no
attribute '_task_epoch'` / no `attempt_epoch` in the payload).

- [ ] **Step 3: Write the bridge implementation**

Add to `redis_io.py` (near `task_events_key`, `:77`):

```python
    def task_epoch_key(self, task_id: str) -> str:
        return self.key(f"task:{task_id}:epoch")
```

**r3-fold (agy + codex): add CLASS-LEVEL defaults, not just the `__init__` init.** Under `class Bridge` (mirroring
the existing `_live_redis` class-default pattern), add:

```python
class Bridge:
    _task_epoch: dict[str, int] = {}          # class default: makes push_task_event safe on Bridge.__new__
    _task_turn_index: dict[str, int] = {}     # test fixtures that bypass __init__ (r3: ~13 such fixtures)
```

This makes the clean-checkout `AttributeError` **unrepresentable** — every `Bridge.__new__(Bridge)` fixture
resolves `self._task_epoch`/`self._task_turn_index` to the empty class dict by construction, so NO test-file edits
are needed (superseding the r2 "grep and edit every fixture" instruction, which staged only 1 of ~13 files —
codex/agy r3).

**v6 correction (codex r4 P2): the class defaults are a READ fallback ONLY — the push path is NOT read-only.**
The v5 claim that `push_task_event` only reads these maps is false once Task 10 lands: `_stamp_turn_index`
(called FROM `push_task_event`) WRITES `_task_turn_index`, so a `__new__` fixture that never shadowed an instance
dict would mutate the SHARED class dict and leak ordinals across tests keyed on the same task id
(order-dependent results). Every WRITER must lazily shadow an instance dict first. Add near the class defaults:

```python
    def _ensure_task_maps(self) -> None:
        """v6 (codex r4 P2): the class-level `_task_epoch`/`_task_turn_index` defaults exist so
        Bridge.__new__ fixtures can READ safely. Any writer must call this first — writing through
        the class default would mutate shared state across every fixture in the process."""
        if "_task_epoch" not in self.__dict__:
            self._task_epoch = {}
        if "_task_turn_index" not in self.__dict__:
            self._task_turn_index = {}
```

Call `self._ensure_task_maps()` (1) in `process_request` immediately before the epoch-allocate write below, and
(2) at the top of `_stamp_turn_index` (Task 10) before its advance-write. Real tasks still get instance
isolation — `Bridge.__init__` ALSO sets both instance dicts eagerly, so concurrent live tasks never share; the
lazy guard exists purely for `__new__` fixtures that trigger a write path.

In `process_request` (`bridge.py`), immediately after `self.record_request_started()` (`:1255`) and BEFORE `self.push_task_event(envelope, "task_started", ...)` (`:1264`):

```python
            epoch = self.redis.incrby(
                self.redis_config.task_epoch_key(envelope.id), 1, ttl=self.args.events_ttl
            )
            self._task_epoch[envelope.id] = epoch
```

At the top of `push_task_event` (`bridge.py:2442`), stamp the snapshot onto `data` before the tees (so it reaches the eval payload AND is present for the events stream/live tee — all bounded scalars):

```python
    def push_task_event(self, request: Envelope, event: str, data: dict[str, Any]) -> None:
        epoch = self._task_epoch.get(request.id)
        if epoch is not None:
            data.setdefault("attempt_epoch", epoch)
        key = self.redis_config.task_events_key(request.id)
        ...
```

Clean up in the `process_request` `finally` (with the other per-task map pops, `bridge.py:1448-1451`):

```python
                self._task_epoch.pop(envelope.id, None)
```

**No per-fixture edits needed (r3-fold — superseded by the class-level defaults above).** The r2 instruction to
"grep `Bridge.__new__(Bridge)` and add the two attributes to each" was incomplete (it staged 1 of ~13 fixtures,
codex/agy r3). The class-level `_task_epoch`/`_task_turn_index` defaults make every `Bridge.__new__` fixture safe
by construction, so the ~13 existing fixtures (`test_push_task_event_tee.py`, `test_events_live_tee.py`, the E2E
roundtrip suites, …) run green on a clean checkout with NO edits. Verify by running the broader bridge suite in
Step 4 from a clean checkout (Global Constraint).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra arb-memory pytest tests/test_bridge_attempt_epoch.py tests/test_build_eval_record.py tests/test_push_task_event_tee.py tests/test_events_live_tee.py -v`
Expected: PASS — incl. `test_real_bridge_snapshot_once_…` and the pre-existing `__new__` fixtures (unedited, safe via class defaults). (The recovery-→-higher-epoch and takeover properties get their end-to-end proof in the live gate, Task 13.)

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/redis_io.py src/agent_redis_bridge/bridge.py tests/test_bridge_attempt_epoch.py tests/test_push_task_event_tee.py
git commit -m "feat(5a-0): dispatch attempt_epoch INCR source + snapshot-once on every eval event (Deliverable 2, SP0-1)"
```

---

## Task 10: dispatch deterministic `turn_index` (Deliverable 2)

**Files:**
- Modify: `src/agent_redis_bridge/bridge.py`
- Test: `tests/test_bridge_turn_index.py` *(create)*

**Interfaces:**
- Consumes: nothing new (allowlist already has `turn_index`).
- Produces: dispatch turn/tool eval edges carry `data["turn_index"]` — a per-`(run_id,task_id)` integer ordinal advanced in execution order at each logical-turn boundary; scope `(run_id,task_id)`, **no `seat_id`** (fold J). Out-of-turn events (`task_started`, `task_continuing`, `agent_sdk_subscription_audit`) carry NO `turn_index`. The terminal event is stamped with the active ordinal BEFORE it is cleared (fold K+). Deterministic — a re-run reproduces identical ordinals (state is in-memory execution-order counting, no persistent Redis id-map).

**Boundary signal (see the Open Question below — do NOT author-assert beyond the pinned invariants):** the bridge advances the active ordinal when it observes the engine's `turn_completed` event (which every dispatch engine emits or, for pi_rpc, will emit after Task 12). Turn 1 is active from the first in-turn edge; on `turn_completed`, the completed turn's edges have already carried ordinal N; advance to N+1 for the next turn.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bridge_turn_index.py
class _TurnIndexHarness:
    OUT_OF_TURN = {"task_started", "task_continuing", "agent_sdk_subscription_audit"}
    def __init__(self):
        self._task_turn_index = {}   # task_id -> active ordinal (1-based; 0 = none yet)
    def _stamp_turn_index(self, task_id, event, data):
        if event in self.OUT_OF_TURN:
            return data              # out-of-turn: no turn_index (fold K)
        idx = self._task_turn_index.get(task_id, 0)
        if idx == 0:
            idx = 1
            self._task_turn_index[task_id] = 1
        data.setdefault("turn_index", idx)
        if event == "turn_completed":
            # terminal-for-this-turn stamped FIRST (above), THEN advance (fold K+)
            self._task_turn_index[task_id] = idx + 1
        return data


def test_tool_edges_within_a_turn_share_the_ordinal():
    h = _TurnIndexHarness()
    a = h._stamp_turn_index("t", "command_started", {})
    b = h._stamp_turn_index("t", "command_finished", {})
    assert a["turn_index"] == b["turn_index"] == 1


def test_out_of_turn_events_carry_no_turn_index():
    h = _TurnIndexHarness()
    for ev in ("task_started", "task_continuing", "agent_sdk_subscription_audit"):
        assert "turn_index" not in h._stamp_turn_index("t", ev, {})


def test_turn_completed_stamps_then_advances():
    h = _TurnIndexHarness()
    done = h._stamp_turn_index("t", "turn_completed", {})
    assert done["turn_index"] == 1                       # terminal stamped BEFORE clear (K+)
    nxt = h._stamp_turn_index("t", "command_started", {})
    assert nxt["turn_index"] == 2                        # next turn advanced


def test_deterministic_rerun_reproduces_identical_ordinals():
    seq = ["command_started", "command_finished", "turn_completed",
           "command_started", "turn_completed"]
    def run():
        h = _TurnIndexHarness()
        return [h._stamp_turn_index("t", e, {}).get("turn_index") for e in seq]
    assert run() == run() == [1, 1, 1, 2, 2]
```

- [ ] **Step 2: Run tests to verify they fail / pass the harness**

Run: `uv run --extra arb-memory pytest tests/test_bridge_turn_index.py -v`
Expected: PASS for the harness (defines the intended contract inline). The real work is wiring `_stamp_turn_index` into `Bridge.push_task_event` and initializing/clearing `self._task_turn_index` alongside `self._task_epoch` (Task 9).

- [ ] **Step 3: Write the bridge implementation**

Add `self._task_turn_index: dict[str, int] = {}` in `Bridge.__init__` (the class-level default was added alongside `_task_epoch` in Task 9 — instance init shadows it for live isolation); add the `_stamp_turn_index(task_id, event, data)` method (the harness body above, as a real method) **with `self._ensure_task_maps()` as its FIRST line** (v6, codex r4 P2 — this method WRITES the map, and a `Bridge.__new__` fixture writing through the class default would leak shared state across tests); call it in `push_task_event` right after the epoch stamp; pop the task_id in the `process_request` `finally`. Advance ONLY on `turn_completed`; leave `task_started`/`task_continuing`/`agent_sdk_subscription_audit` unstamped.

**r0-fold P1-C (codex) — central dispatch `tool_call_id` coalescing (completes Deliverable 1 for the dispatch producers).** The v1 plan wired `canonical_tool_call_id` ONLY into claude-tail (Task 4), but the spec's acceptance criterion is *every producer emits `tool_call_id` (same on both tool edges)*. **Codex and Agy emit only `item_id` on their tool edges** (`engines/codex.py:363-405`, `engines/agy_print.py:373-403`), and `item_id` is NOT eval-allowlisted — so their durable eval tool edges would carry no `tool_call_id`. Fix centrally in `push_task_event` (the single dispatch eval choke point) so it covers codex/agy/cursor/pi at once, for tool edges only:

```python
    def push_task_event(self, request: Envelope, event: str, data: dict[str, Any]) -> None:
        epoch = self._task_epoch.get(request.id)
        if epoch is not None:
            data.setdefault("attempt_epoch", epoch)
        self._stamp_turn_index(request.id, event, data)          # Task 10
        if event in {"command_started", "command_finished", "command_output"}:
            from .tool_call_id import canonical_tool_call_id
            cid = canonical_tool_call_id(data)                    # coalesces tool_call_id|tool_use_id|item_id
            if cid:
                data.setdefault("tool_call_id", cid)
        key = self.redis_config.task_events_key(request.id)
        ...
```

**r1-fold P1-3 — REAL-`Bridge` turn_index + tool_call_id test + delete-to-red (not the inline harness).** The
`_TurnIndexHarness` tests above define the contract but are non-load-bearing (mirror copy). Add a real-`Bridge`
test to `tests/test_bridge_turn_index.py` (reuse the `_FakeRedis`/`_RecordingEval`/`Bridge.__new__` setup from
Task 9's `test_real_bridge_snapshot_once_…`):

```python
def test_real_bridge_shares_turn_index_and_coalesces_tool_call_id_on_both_edges():
    # r1-fold P1-3 + P1-C: real Bridge. Two in-turn tool edges (codex/agy-shaped: only item_id set)
    # share turn_index==1 AND both get tool_call_id == item_id (dispatch coalesce, Task 10).
    import json
    from types import SimpleNamespace
    from agent_redis_bridge.bridge import Bridge
    from agent_redis_bridge.redis_io import RedisConfig
    from agent_redis_bridge.envelope import Envelope

    class _FakeRedis:
        def __init__(self):
            self.xadds, self.counters = [], {}
        def xadd(self, key, fields, *, maxlen=None, ttl=None):
            self.xadds.append((key, fields)); return "1-0"
        def incrby(self, key, amount, ttl=None):
            self.counters[key] = self.counters.get(key, 0) + amount; return self.counters[key]
        def expire(self, *a, **k):
            return True

    class _RecordingEval:
        def __init__(self):
            self.xadds = []
        def xadd(self, key, fields):
            self.xadds.append((key, fields)); return "1-0"

    b = Bridge.__new__(Bridge)
    b.redis_config = RedisConfig("127.0.0.1", "6379", "15", "agent_scratch:")
    b.redis = _FakeRedis(); b.args = SimpleNamespace(max_task_events=500, events_ttl=60)
    b.agent_id = "codex-test"; b.eval_redis = _RecordingEval(); b._eval_stream = "eval:events"
    b._task_epoch = {}; b._task_turn_index = {}

    env = Envelope(id="task-1", sender="claude", branch="manual", recipient="codex",
                   kind="request", sent_at="x", payload={"task": "x"}, run_id="run-1")
    b.push_task_event(env, "command_started", {"tool_name": "Bash", "item_id": "call_7"})
    b.push_task_event(env, "command_finished", {"tool_name": "Bash", "item_id": "call_7"})
    # r2-fold P2 (codex #5): the out-of-turn subscription-audit must NOT carry a turn_index on the
    # DURABLE eval record — proven on a real Bridge, not just via the engine payload-shape test.
    b.push_task_event(env, "agent_sdk_subscription_audit", {"kind": "agent_sdk_subscription_audit"})

    evals = [json.loads(fields["payload"]) for _key, fields in b.eval_redis.xadds]
    tool_evals = evals[:2]
    audit_eval = evals[2]
    assert [e.get("turn_index") for e in tool_evals] == [1, 1]       # shared ordinal within the turn
    assert [e.get("tool_call_id") for e in tool_evals] == ["call_7", "call_7"]  # coalesced from item_id, both edges
    assert "turn_index" not in audit_eval                            # OUT_OF_TURN → no ordinal (real Bridge)
```

**Delete-to-red:** (a) turn_index — delete the `data.setdefault("turn_index", idx)` line in `_stamp_turn_index`,
re-run → `turn_index` is absent (`[None, None] != [1, 1]`) RED; restore. (b) tool_call_id — delete the
`data.setdefault("tool_call_id", cid)` line, re-run → `[None, None]` RED; restore. Record both red outputs. (The
`turn_completed`-advances-the-ordinal behavior keeps its harness contract test `test_turn_completed_stamps_then_advances`.)

**5a follow-up (grok F5 + cold-Opus, NOT fixed here):** codex/agy `command_output` derives a DIVERGENT tool id
(`item_id = f"{base}:output"`, `codex.py:383-389` / `agy_print.py`) from its `command_started`/`command_finished`
base id — so the dispatch coalesce stamps a `tool_call_id` on `command_output` that does NOT equal the start/finish
edges' id. This is a PRE-EXISTING output-edge join mismatch, not fold-introduced, and lands OUTSIDE 5a-0's capture
scope (5a-0 delivers the same id on the two edges the spec names: `command_started`/`command_finished`). Record it
as a 5a projection follow-up (output-edge join); do NOT widen 5a-0 to chase it.

- [ ] **Step 4: Run tests + full bridge suite**

Run: `uv run --extra arb-memory pytest tests/test_bridge_turn_index.py tests/test_engine_progress_schema.py tests/test_push_task_event_tee.py -v`
Expected: PASS (incl. the real-`Bridge` test + the `Bridge.__new__` tests updated in Task 9 to set `_task_turn_index={}`).

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/bridge.py tests/test_bridge_turn_index.py tests/test_push_task_event_tee.py
git commit -m "feat(5a-0): dispatch deterministic turn_index at logical-turn boundaries (Deliverable 2, folds I/J/K)"
```

> **OPEN QUESTION for the plan panel (do NOT silently pin — SP0-2 residual).** The precise per-engine
> logical-turn boundary is only partly pinned in the design ("the proven logical-turn boundary per engine —
> the agy-tmux relocation detail — remains open", SP0-2). This task advances on `turn_completed`, which is
> correct for agent_sdk (emits it on `ResultMessage`) and pi_rpc (Task 12 adds it on clean `agent_end`), but
> **agy-tmux / codex / grok / cursor** may not emit a clean per-turn `turn_completed`. The panel must confirm
> the boundary signal for each dispatch engine, or the plan must add a per-engine `turn_completed` emission as
> a sub-task. If the panel splits on the mechanism, this is a Mark fork (surface, don't vote-count).
>
> **r0 update (agy + grok VERIFIED in-tree — residual is much smaller than the v1 wording implied):** every
> dispatch engine already emits `turn_completed` — `agy_tmux.py:130`, `codex.py:436,552`, `cursor_acp.py:256,270`,
> `grok_acp.py:215`, `pi_sdk.py:561`, `agent_sdk.py:670`; `pi_rpc` gains it in Task 12. So the `turn_completed`
> boundary signal EXISTS fleet-wide. The residual shrinks to: confirm multi-turn-per-`process_request` semantics
> (an engine that emits exactly one `turn_completed` per task is fine — turn_index advances once at task end,
> which is correct). **This is no longer a blocking gap; keep it as a verify-item, not a Mark fork.**

---

## Task 11: agent_sdk tool-edge semantic fixes (Deliverable 3)

**Files:**
- Modify: `src/agent_redis_bridge/engines/agent_sdk.py`
- Test: `tests/test_agent_sdk_engine.py`, `tests/test_engine_progress_schema.py`

**Interfaces:**
- Produces: (a) the permission gate emits `tool_permission_decided` (renamed from the misnamed `command_finished`, `agent_sdk.py:359`); (b) a REAL `command_finished` is emitted from `ToolResultBlock` in `_emit_tool_result` (`agent_sdk.py:550-575`) with `tool_call_id = block.tool_use_id`, `status`/`exit_code` from `block.is_error`; a deny produces exactly ONE true finish; (c) `agent_sdk_subscription_audit` is emitted OUTSIDE turn scope.

- [ ] **Step 1: Write the failing tests**

**r1-fold (codex): these are FULL runnable bodies, appended as METHODS of the existing `EngineTest`
(`unittest.TestCase`) class — the file is class-based (`self._engine`, `self.assertEqual`), NOT module-level
functions. `_result`, `_tooluse`, `UserMessage`, `ToolResultBlock` are already imported/defined in the file.**

```python
# tests/test_agent_sdk_engine.py — append as methods on class EngineTest(unittest.TestCase)
    def test_gate_emits_tool_permission_decided_not_command_finished(self):
        # The permission gate is a DECISION, not a tool result — it must not masquerade as a finish.
        # The gate fires on the SDK can_use_tool path, so drive _gate() directly (it is async but awaits
        # nothing; asyncio.run completes it on the main thread, independent of the engine loop thread).
        import asyncio
        from types import SimpleNamespace
        seen = []
        engine = self._engine()
        try:
            engine._turn_on_event = lambda k, d: seen.append((k, d))
            engine._turn_policy = "trusted"
            asyncio.run(engine._gate("Read", {}, SimpleNamespace(tool_use_id="t1")))
            kinds = [k for k, _ in seen]
            self.assertIn("tool_permission_decided", kinds)
            self.assertNotIn("command_finished", kinds)   # the gate must NOT emit a finish
        finally:
            engine.stop()

    def test_real_command_finished_comes_from_tool_result_block(self):
        # A ToolResultBlock yields command_output AND a REAL command_finished carrying the tool's exit
        # status (mirrors the existing test_tool_result_output_flows_to_transcript_content stream shape).
        seen = []
        msg = UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="boom", is_error=True)])
        engine = self._engine(messages=[msg, _result()])
        try:
            engine.run_turn_with_progress("x", timeout=30, policy="trusted",
                                          on_event=lambda k, d: seen.append((k, d)))
            finishes = [d for k, d in seen if k == "command_finished"]
            self.assertEqual(len(finishes), 1)                 # exactly one, from the ToolResultBlock
            self.assertEqual(finishes[0]["tool_call_id"], "t1")
            self.assertEqual(finishes[0]["status"], "failed")
            self.assertEqual(finishes[0]["exit_code"], 1)
        finally:
            engine.stop()

    def test_deny_produces_exactly_one_decision_and_no_finish(self):
        # A denied mutating tool under a non-trusted policy → exactly one tool_permission_decided(deny),
        # and NO phantom command_finished. (decide(): Write is MUTATING → denied when policy != trusted.)
        import asyncio
        from types import SimpleNamespace
        seen = []
        engine = self._engine()
        try:
            engine._turn_on_event = lambda k, d: seen.append((k, d))
            engine._turn_policy = "human"
            asyncio.run(engine._gate("Write", {}, SimpleNamespace(tool_use_id="t9")))
            decisions = [d for k, d in seen if k == "tool_permission_decided"]
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["status"], "denied")
            self.assertEqual(decisions[0]["exit_code"], 1)
            self.assertNotIn("command_finished", [k for k, _ in seen])
        finally:
            engine.stop()

    def test_subscription_audit_payload_is_out_of_turn(self):
        # The audit is emitted BEFORE _run_turn (agent_sdk.py:498-501, ahead of turn_started); the bridge
        # lists agent_sdk_subscription_audit in OUT_OF_TURN (Task 10) so the DURABLE eval record never
        # carries a turn_index. Engine-level: the payload self-stamps no turn ordinal. (r2-fold: the
        # load-bearing no-turn_index property on the durable eval record is now proven on a REAL Bridge in
        # Task 10's test_real_bridge_shares_turn_index_and_coalesces_tool_call_id_on_both_edges — not the
        # mirror harness. The subscription EMIT path itself is env/slot-gated, so it is asserted
        # structurally here rather than by standing up a subscription seat.)
        engine = self._engine()
        try:
            payload = engine._subscription_audit_payload()
            self.assertEqual(payload["kind"], "agent_sdk_subscription_audit")
            self.assertNotIn("turn_index", payload)
        finally:
            engine.stop()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra arb-memory pytest tests/test_agent_sdk_engine.py -k "tool_permission_decided or real_command_finished or deny_produces or subscription_audit_payload" -v`
Expected: FAIL for the first three — gate still emits `command_finished` (so `tool_permission_decided` is absent
and `command_finished` is present); no finish from `ToolResultBlock`. `test_subscription_audit_payload_is_out_of_turn`
is GREEN pre-impl (an invariant/regression guard: the payload already carries no `turn_index`) — that is expected,
it locks the property against a future regression.

- [ ] **Step 3: Implement**

In `_gate` (`agent_sdk.py:359`) change the emitted event name `"command_finished"` → `"tool_permission_decided"` (keep the allow/deny `kind` values). In `_emit_tool_result` (`agent_sdk.py:550-575`), after the `command_output` emit, add a real `command_finished`:

```python
        on_event(
            "command_finished",
            self._scrub_payload({
                "command": block.tool_use_id,
                "status": "failed" if block.is_error else "completed",
                "exit_code": 1 if block.is_error else 0,
                "tool_call_id": block.tool_use_id,
                "turn_id": turn_id,
                "item_id": f"{block.tool_use_id}:finished",
                "kind": "command_finished",
                "seq": self._next_progress_seq(),
            }),
        )
```

Add `agent_sdk_subscription_audit` to the bridge `OUT_OF_TURN` set (Task 10 already lists it) so it never carries `turn_index`; if the audit is emitted before the turn opens, leave its current position (it already precedes `_run_turn`, `agent_sdk.py:498-504`).

- [ ] **Step 4: Run to verify pass** — `uv run --extra arb-memory pytest tests/test_agent_sdk_engine.py tests/test_engine_progress_schema.py -v` → PASS (update any schema test that asserted the gate's old `command_finished` name).

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/engines/agent_sdk.py tests/test_agent_sdk_engine.py tests/test_engine_progress_schema.py
git commit -m "fix(5a-0): agent_sdk gate→tool_permission_decided, real command_finished from ToolResultBlock (Deliverable 3)"
```

---

## Task 12: pi_rpc tool-edge semantic fixes (Deliverable 3)

**Files:**
- Modify: `src/agent_redis_bridge/engines/pi_rpc.py`
- Test: `tests/test_pi_rpc.py:472-564` (rewrite) + new cases

**Interfaces:**
- Produces: (a) `command_finished` comes ONLY from `tool_execution_end` (the real exit status, `pi_rpc.py:133-145`); the `toolcall_end` branch (`:97-116`) is DEMOTED (no longer emits `command_finished`); (b) the first-wins dedup (`_seen_tool_ends`/`_seen_tool_starts`, `:228-231,472-483`) is removed for finishes so the real `tool_execution_end` is not masked by the premature `toolcall_end`; (c) `turn_completed` is emitted on a clean `agent_end` (`:415-419`).

- [ ] **Step 1: Rewrite the failing test**

Rewrite `test_toolcall_and_tool_execution_deduped_by_tool_call_id` (`tests/test_pi_rpc.py:472-564`) to assert the NEW contract: with both a `toolcall_end` and a `tool_execution_end(isError=True)` for `tc1`, exactly ONE `command_finished` is emitted and it carries `status == "failed"`, `exit_code == 1` (the REAL status from `tool_execution_end`), NOT the premature `completed`. Add `test_turn_completed_emitted_on_clean_agent_end` asserting a `turn_completed` event fires when `agent_end` arrives.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --extra arb-memory pytest tests/test_pi_rpc.py -k "deduped_by_tool_call_id or turn_completed_emitted" -v`
Expected: FAIL — current code lets `toolcall_end` win (`status == "completed"`); no `turn_completed`.

- [ ] **Step 3: Implement**

In `normalize_pi_event`, make the `toolcall_end` branch (`pi_rpc.py:97-116`) NOT return a `command_finished` (demote to a non-eval progress signal or drop it). Keep `tool_execution_end` (`:133-145`) as the sole `command_finished`. Remove the finish dedup (`_seen_tool_ends` and its guard at `:478-483`); keep or simplify the start dedup as needed. In `_handle_client_message`, on `agent_end` (`:415-419`), before returning `TurnResult`, emit `turn_completed`:

```python
            if message.get("type") == "agent_end":
                text = self._fetch_last_text(deadline)
                self.active_prompt_id = None
                if on_event is not None:
                    on_event("turn_completed", {"ok": True, "kind": "turn_completed"})
                result = text.strip() or "".join(chunks).strip() or f"pi-rpc prompt {prompt_id} completed."
                return TurnResult(ok=True, result=result)
```

**r0-fold P2 (agy + cold-Opus):** `agent_end` is handled in the outer turn loop (`_run_turn`, `pi_rpc.py:415-419`) where **`on_event` is already an in-scope parameter** — use it directly. Do NOT invent a `self._on_event` attribute (it does not exist on `PiRpcEngine` and would `AttributeError`). No threading needed.

- [ ] **Step 4: Run to verify pass** — `uv run --extra arb-memory pytest tests/test_pi_rpc.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/engines/pi_rpc.py tests/test_pi_rpc.py
git commit -m "fix(5a-0): pi_rpc command_finished from tool_execution_end only, turn_completed on agent_end (Deliverable 3)"
```

---

## Precondition M2 — owner-fenced processing acknowledgement (EXTERNAL dependency, not a task)

> **RESOLVED (Mark, 2026-07-14): M2 is a SEPARATE bridge-infra work item, NOT a task in this plan.** M2 is a
> reliable-inbox correctness fix that harms EVERY dispatch (a lost request), not just spans — it has its own
> blast radius, its own concurrency semantics, and (per the pipeline) its own design/plan, panel, and live gate.
> Folding it in here would make the 5a-0 *capture* panel review inbox-ownership concurrency on infra it does not
> own — the exact boundary violation the 5a-0/5a split and the option-1 scope call exist to prevent
> ([[cross-slice-claims-need-citation]]). It enters 5a-0 the way the version-pinned canary enters the spec:
> **as a tested precondition asserted with a citation, not as an absorbed task.**

**What 5a-0 owns:** nothing in M2's implementation. 5a-0's `attempt_epoch` guarantee ("survives the restart it
marks") is **conditional** on the reliable-inbox recovery being owner/attempt-fenced, and 5a-0's live gate
**asserts the landed M2 fix** (Task 13, M2 gate) — a **concrete, citable** check (the M2 fix's merged commit SHA
+ a runtime probe of the fenced behaviour), NOT an aspirational one. Until M2 has merged + cleared its own live
gate, 5a-0 is cleanly **parked on a named unmet precondition** — its own convergence is not hostage to M2's
review rounds, and an M2 gate finding blocks M2, not a 5a-0 re-panel.

**The cited defect (for the M2 work item's brief — do NOT implement here):** `remove_processing` is a body-keyed
`LREM :processing 1 <raw-body>` (`redis_io.py:317-318`) run unconditionally in `process_request`'s `finally`
(`bridge.py:1443-1447`); ownership-loss cleanup does not join in-flight request threads (`bridge.py:620-653,671-694`).
A stale-but-live predecessor's `finally` can `LREM` the successor's re-parked identical body and lose the
higher-epoch request. **Required fix:** owner/claim-token-owned acknowledgement (atomic compare-token-then-remove,
building on the existing per-boot `owner_token` `bridge.py:274`), so a stale token cannot delete the successor's
claim. **This is authored + reviewed + live-gated in its OWN plan** (`docs/superpowers/plans/…-reliable-inbox-owner-fence.md`),
kicked off in parallel so its panel latency overlaps Tasks 1–12 (Mark's sequencing pin).

---

## Task 13: allowlist end-to-end, plan-fixture-smoke, fleet redeploy + soak, live gate

**Files:**
- Verify: all touched producers; `scripts/plan-fixture-smoke`; the live gate.
- No new source (integration + verification task).

**This task is the REQUIRED verification gate. It has NO merge-to-prod authority — the fleet redeploy is PAUSED for Mark's deploy-review gate (Global Constraints).**

- [ ] **Step 1: Plan-fixture-smoke pre-flight (REQUIRED — this plan is fake-based).** Per [[plan-fixture-smoke-preflight]], run `scripts/plan-fixture-smoke` over the fixture-smoke blocks below. **r2-fold (codex): the runner exits 2 on a plan with NO `python fixture-smoke` blocks — v3 had none, so the gate was uncompletable.** v4 carries the three blocks below (fenced ` ```python fixture-smoke `), which run against the CURRENT (pre-impl) tree and prove: (a) the extract-only eval boundary is enforced by PRODUCTION `extract_eval_payload`, not by any fake; (b–c) the headline deny-proofs (composite-offset, real-`Bridge` epoch stamp) are genuinely RED pre-impl via `red_claim` (sub-species-B inert-pin guard) — proving the mirror harnesses are NOT load-bearing. Run:

`env -u ARB_MEMORY_LOCAL_MCP PYTHONPATH=src .venv/bin/python scripts/plan-fixture-smoke docs/superpowers/plans/2026-07-14-arb-observability-slice5a-0-capture-normalization.md` → expect exit 0 (`[fixture-smoke] OK`). Record the output.

```python fixture-smoke
# (a) The eval extract-only boundary is PRODUCTION's (extract_eval_payload), NOT any fake's. A green
# eval test is testing the real allowlist, not a fake that supplies the drop.
from agent_redis_bridge.eval_tee import extract_eval_payload
out = extract_eval_payload({"tool_name": "Bash", "message": "secret text", "thinking": "cot"})
assert "message" not in out and "thinking" not in out, "extract_eval_payload must drop free text"
assert out == {"tool_name": "Bash"}
# The five 5a-0 primitives are NOT yet allowlisted on the CURRENT tree (Task 2 adds them) — prove it,
# so the Task-2 positive test is a real red->green, not a fake pre-supplying the field.
assert extract_eval_payload({"event_ts": "2026-07-13T19:42:50Z"}) == {}
```

```python fixture-smoke
# (b) The composite-offset deny-proof MUST be red against the current (pre-Task-6) tree, where
# OffsetStore has no Position/load/store. A pin that PASSES now would be inert.
red_claim(
    '''
def test_offset_composite_roundtrips_precheck():
    from agent_redis_bridge.claude_tail.offset import Position, OffsetStore
    class R:
        def __init__(self): self.v = {}
        def get(self, k): return self.v.get(k)
        def set(self, k, val): self.v[k] = val
    s = OffsetStore(R(), "p:")
    s.store("k", 1234, 4)
    assert s.load("k") == Position(1234, 4)
''',
    expect_fail=["test_offset_composite_roundtrips_precheck"],
)
```

```python fixture-smoke
# (c) The r1/r2 fix moved the epoch deny-proof onto a REAL Bridge (not the _EpochMixinHarness mirror).
# Prove that real-Bridge assertion is red on the current tree (push_task_event does not stamp
# attempt_epoch yet) — so the harness contract tests are decoration and the real-Bridge test carries
# the load. Also exercises the Bridge.__new__ fixture shape the plan relies on.
red_claim(
    '''
def test_real_bridge_stamps_attempt_epoch_precheck():
    import json
    from types import SimpleNamespace
    from agent_redis_bridge.bridge import Bridge
    from agent_redis_bridge.redis_io import RedisConfig
    from agent_redis_bridge.envelope import Envelope
    class Eval:
        def __init__(self): self.x = []
        def xadd(self, k, f): self.x.append(f); return "1-0"
    class R:
        def __init__(self): self.x = []
        def xadd(self, k, f, maxlen=None, ttl=None): self.x.append((k, f)); return "1-0"
        def expire(self, *a, **k): return True
    b = Bridge.__new__(Bridge)
    b.redis_config = RedisConfig("127.0.0.1", "6379", "15", "agent_scratch:")
    b.redis = R(); b.args = SimpleNamespace(max_task_events=500, events_ttl=60)
    b.agent_id = "t"; b.eval_redis = Eval(); b._eval_stream = "eval:events"
    b._task_epoch = {"task-1": 1}
    env = Envelope(id="task-1", sender="claude", branch="manual", recipient="codex",
                   kind="request", sent_at="x", payload={"task": "x"}, run_id="run-1")
    b.push_task_event(env, "command_started", {"tool_name": "Bash"})
    payload = json.loads(b.eval_redis.x[0]["payload"])
    assert payload.get("attempt_epoch") == 1
''',
    expect_fail=["test_real_bridge_stamps_attempt_epoch_precheck"],
)
```

- [ ] **Step 1b: Placeholder / ellipsis-body refusal (r1-fold codex; r2-fold: portable + self-proven).**
  pytest passes an ellipsis-only test body, and `scripts/plan-fixture-smoke` does not scan for it — so a worker
  could commit an inert green test (the exact risk in Tasks 11/12). Refuse green if any test function in the
  touched suites has a body that is only `...`/`pass`/a docstring. **r2-fold (codex P2): the v3 `grep -rnP` check
  was INERT on macOS** — BSD `/usr/bin/grep` rejects `-P`, and the leading `!` turned that error-exit into a
  "pass," so the anti-vacuity check was itself vacuously green (the v5/monkeypatch pattern recurring at the meta
  level). Dropped. Use a single PORTABLE AST census — AND, per Mark, **verify the verifier**: run the census on a
  body that SHOULD be flagged and assert it IS (a red-proof for the anti-vacuity tool itself):

  ```bash
  uv run --extra arb-memory python - <<'PY'
  import ast, sys, tempfile, os
  def inert_test_bodies(files):
      bad = []
      for f in files:
          tree = ast.parse(open(f).read(), f)
          for node in ast.walk(tree):
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                  body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                                                       and isinstance(s.value.value, str))]  # drop docstring
                  if not body or all(isinstance(s, ast.Pass) or
                                     (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                                      and s.value.value is Ellipsis) for s in body):
                      bad.append(f"{f}::{node.name}")
      return bad

  # (self-proof) the census MUST flag a known-inert body — else the verifier is inert (r2 lesson).
  with tempfile.TemporaryDirectory() as d:
      probe = os.path.join(d, "probe.py")
      open(probe, "w").write("def test_inert():\n    ...\ndef test_real():\n    assert 1 == 1\n")
      caught = inert_test_bodies([probe])
      assert caught == [f"{probe}::test_inert"], f"anti-vacuity census is itself inert: {caught}"

  # (real) refuse if any TOUCHED test body is inert. r3-fold (codex P2): derive the file list from the
  # committed diff, NOT a hardcoded subset — v4 scanned only 5 files, so an inert body in test_eval_tee.py
  # / test_offset.py / test_tool_call_id.py / test_push_task_event_tee.py / test_engine_progress_schema.py
  # would have escaped. Enumerate every touched test file against the merge base.
  import subprocess
  base = subprocess.run(["git", "merge-base", "HEAD", "origin/dev"], capture_output=True, text=True).stdout.strip() or "HEAD~20"
  changed = subprocess.run(["git", "diff", "--name-only", base, "HEAD"], capture_output=True, text=True).stdout.split()
  files = [f for f in changed if f.startswith("tests/") and f.endswith(".py") and os.path.exists(f)]
  assert files, "no touched test files found — check the merge base"
  bad = inert_test_bodies(files)
  if bad:
      print("INERT TEST BODIES (ellipsis/pass/docstring only):", *bad, sep="\n  "); sys.exit(1)
  print("no inert test bodies (census self-proof passed)")
  PY
  ```

- [ ] **Step 2: Full targeted suite green.** `uv run --extra arb-memory pytest tests/test_tool_call_id.py tests/test_eval_tee.py tests/claude_tail/ tests/test_pi_rpc.py tests/test_agent_sdk_engine.py tests/test_engine_progress_schema.py tests/test_bridge_attempt_epoch.py tests/test_bridge_turn_index.py tests/test_push_task_event_tee.py tests/test_reliable_inbox_owner_fence.py tests/test_build_eval_record.py -v` — ALL green. (Per [[e2e-mutation-tier-run-policy]]: targeted tests + review + live gate; NOT the full suite / no push.)

- [ ] **Step 3: Delete-to-red verification for every deny-proof.** For each deny-proof, apply the guard mutation
  (delete the guard line, or the arg/body mutation where a pure delete is collateral — see the r0 banner table +
  the r1 fold), run the positive test, CONFIRM IT REDS, restore, and record the red output
  ([[deny-proofs-need-adversarial-verification]]). A deny-proof that stays green under its guard mutation is a
  vacuous guard ([[vacuously-green-guard-fail-loud]]) and blocks the task. The full set:
  - **Task 3** — body-mutate `_stamp_event_ts` else-branch to the `sent_at`/`_now()` fallback → `test_absent_timestamp_omits_event_ts_and_bumps_counter` reds.
  - **Task 6/7** — arg-mutate the end-of-poll commit to `store(key, new_offset, 0)` → `test_turn_index_is_restart_stable_across_nonzero_offset_resume` reds.
  - **Task 8** — delete `_observe_clock` call → `test_i_`/`test_ii_` red; delete the **generic-skip** eager poison → `test_iii_` red; **(r1)** delete the **DriftError** eager poison → `test_iii_drifterror_` red; delete the poll-top `_turn_open=False` abandon → `test_vi_` red. (`test_iv` is structural — NOT a delete-to-red row.)
  - **Task 8 (r1-fold P1-1; v6 re-key — grok r4 P1)** — delete the emit-stage inner `_restore_turn_state(...)` call → `test_multiline_committed_prefix_inner_restore_not_masked_by_outer_guard` reds (phantom turn-2 close; `committed=True` so the outer guard cannot mask). The first-line `test_same_object_retry_after_emit_failure_…` must STAY green under this deletion — it is positive-only (its old delete-to-red is vacuous by design under the outer guard; if it REDS, the guard shape regressed).
  - **Task 8 (v6 — drift-emit-FAILURE arm, codex r4 P1)** — arg-mutate the drift-fail arm's prefix commit to `self._commit(key, new_offset, self.logical_turn_index, st_ino)` → `test_drift_emit_failure_prefix_commits_and_replays_drift_line` reds (the failed drift line is skipped on retry → durable `drift_error` lost).
  - **Task 9 (r1-fold P1-3)** — re-INCR per event in `push_task_event` (real `Bridge`) → `test_real_bridge_snapshot_once_stamps_same_epoch_on_every_eval_event` reds.
  - **Task 10 (r1-fold P1-3)** — delete the `turn_index` / `tool_call_id` `setdefault` in `push_task_event` (real `Bridge`) → `test_real_bridge_shares_turn_index_and_coalesces_tool_call_id_on_both_edges` reds.
  - **Task 8 (v5 clause-1 — `_commit`'s cursor half)** — delete `_commit`'s two cursor lines (`self._cursor_inode = st_ino; self._cursor_offset = offset`) → `test_multiline_poll_emit_fail_on_close_does_not_drop_turn_completed` (r2 window) AND `test_drift_threshold_keeps_cursor_aligned_no_false_null` (r3 drift-threshold cell) BOTH red (close dropped → false NULL). Both MUST flip. The sole-writer census (`test_commit_is_sole_writer_of_cursor_and_store`) needs no manual mutation here — its red-proof is the always-on `test_sole_writer_census_reds_on_planted_rogue_writers`.
  - **Task 8 (v5 clause-2 — outer boundary guard)** — delete the outer-guard restore (`if not committed: self._restore_turn_state(poll_start_turn_state)`) → `test_readline_oserror_midpoll_does_not_replay_mutated_turn_state` AND `test_readline_oserror_after_inloop_skip_still_restores_poll_start` both red (a phantom turn-2 close / turn-3 open appears on the retry).
  - **Task 8 (residue: heal-abandon, v6 re-key)** — delete the heal's EXPLICIT abandon block (`if self._turn_open: _turn_clock_ok = False; _turn_open = False`, keeping its `_commit`) → `test_same_object_truncate_heal_abandons_open_turn` reds (the `_commit`-aligned cursor keeps the discontinuity silent → a phantom, possibly forged-`true`, close appears).
  - **Task 8 (v6 — heal persist, cold-Opus r4 P1)** — mutate the heal back to in-memory-only (delete its `self._commit(key, 0, 0, st_ino)` line) → `test_truncate_to_empty_persists_heal_before_regrowth` reds (zero-lines heal never persists; regrowth past the stale offset seeks mid-stream → p9's turn silently lost).
  - **Task 6/7 (v6 — load purity + wrapper deletion, codex+grok r4 P1s)** — no manual mutation: enforced by the always-on census battery (`_offset_module_write_census` red-proofed by planted `redis.set` AND `self.store(...)` in `load()`; forbidden `.commit(`/`.get(` + `offset_store.redis` reach-through + `__setattr__` red-proofed by planted calls) plus the Step 3c grep gate.
  - **Task 6 (v7 — corrupt-load stall arms, codex r5 P1)** — mutate `load()` to decode outside its guard (or drop the negative-domain check) → `test_invalid_utf8_position_reads_as_zero_zero_without_raising` / `test_negative_composite_reads_as_zero_zero_without_writing` red (an uncaught raise / a `seek(-1)` would stall the tailer permanently under pure load).
  - **Task 8 (v7 — drift_count rollback, codex r5 P1)** — delete the drift-failure arm's `self.drift_count -= 1` → `test_drift_emit_failure_prefix_commits_and_replays_drift_line` reds on `count == 1` / `tailer.drift_count == 1` (retry double-counts the line).
  - **Task 8 (v7 — heal/guard overlap, codex r5 P1; mutation (a) corrected v8, grok r6 P1)** — (a) move the `committed = False` init ABOVE the heal and set `committed = True` at the heal's `_commit` (the naive at-heal set is clobbered by the init — dead code) → `test_heal_then_uncommitted_failure_restores_post_abandon_state` reds (phantom ordinal-0 close after the poll-top logical rebind); (b) move the guard snapshot before the heal-abandon → same test reds (phantom ordinal-0 close with gen-1's `event_ts`).
  - **Task 8 (v8 — 7j counter, codex r6 P2)** — delete the `_close_and_open_turn` missing-ts bump (`self.claude_tail_missing_ts += 1`) → `test_missing_timestamp_on_opening_record_is_false_and_counted` reds specifically on its counter assertion.
  - **Task 6 (v8 — stall inputs, codex r6 P1/P2)** — remove `RecursionError` from `load()`'s except tuple → `test_deeply_nested_corrupt_position_reads_as_zero_zero_without_raising` reds (uncaught raise = permanent stall); revert the `type(data.get("v")) is int` check to `!=` → `test_noncanonical_version_field_reads_as_corrupt` reds (True/1.0 smuggle a mid-file resume).
  - **Task 8 (v7 — outer-guard SKIP arm, grok r5 P2)** — drop the guard's `if not committed` (always restore) → `test_multiline_committed_prefix_inner_restore_not_masked_by_outer_guard` reds (restore desyncs from the committed prefix).
  - **Task 8 (v7 — missing-ts clock arms, cold-Opus r5 P2)** — delete `_observe_clock`'s missing-ts poison → `test_missing_timestamp_on_in_turn_record_is_false` reds; mutate the open-arm `bool(line_ts)` → `True` → `test_missing_timestamp_on_opening_record_is_false_and_counted` reds (forged `true` both ways).

  Cross-check each row against the **Deny-proof → cell map** in the transaction-boundary matrix section — every load-bearing cell must have a flipping test here; an unmapped cell is a pre-dispatch gap.

- [ ] **Step 4: Live gate (REQUIRED for the CLI/subprocess/hook glue — [[live-verification-catches-cli-glue]]).** Against a real environment (Mark's deploy gate permitting), execute the spec's Live gate:
  - Producer/pin roster incl. a **real interactive claude-tail session**; read the durable EVAL STREAM + verify the pin SHA.
  - **claude-tail epoch-pin + latency idempotency (R1/R2):** force each of the four byte-0 re-read paths (truncate-heal, corrupt-offset heal, missing key, new inode); the re-emitted early turn carries `attempt_epoch = 1` and the SAME `event_ts`.
  - **claude-tail fail-closed timestamp:** a line lacking `timestamp` on a turn/tool edge emits NO `event_ts` and bumps `claude_tail_missing_ts` — never `event_ts == sent_at`.
  - **claude-tail single-continuous SCAN observation:** the five `false` cases + the true next-human-`user` close carry the right durable `turn_clock_monotonic` (capture side; the NULL/`clock_invalid` projection is 5a's O-gate).
  - **claude-tail single-dispatch ⇒ NULL, never wrong-`true` (Option-D):** a one-prompt cold-seat dispatch ending at sidecar `completed:true`/marker emits `turn_clock_monotonic = false` ⇒ NULL; **no terminal stop earns `true`.**
  - **claude-tail `turn_index` restart-stability + legacy migration:** turns 1–3, restart at nonzero offset, turn 4 = `turn_index 4`; a seeded legacy bare-int forces a byte-0 recount.
  - **`attempt_epoch` (dispatch):** a bridge-daemon kill + reliable-inbox recovery stamps a STRICTLY HIGHER epoch; a simulated lease-takeover stamps the successor higher than a still-live predecessor.
  - **Owner-fenced recovery (M2 gate) — CONCRETE precondition assertion (Mark's pin).** FIRST assert the M2 fix is **landed and citable**, not aspirational: (a) the M2 work-item's fix commit is an ancestor of the deployed SHA (`git merge-base --is-ancestor <m2-fix-sha> HEAD`), AND (b) a **runtime probe** of the fenced behaviour passes — a stale predecessor token's `remove_processing` is a no-op against a successor's re-parked claim (assert the entry survives). THEN the end-to-end scenario: takeover with the predecessor still live — B recovers + re-parks R, A reaches its stale `finally`, B is killed, and C must STILL recover R and allocate an epoch greater than both predecessors. If the M2 fix is NOT landed, this gate **fails closed** and 5a-0 stays parked on the named unmet precondition (it does not silently pass).
  - **Deterministic identity:** engine respawn / warm rotation mid-task does not collide `turn_index`.
  - `≥2` tool calls/producer; `≥1` failing/delayed tool (finish from REAL execution).

- [ ] **Step 5: Fleet bridge redeploy + soak — PAUSED FOR MARK'S DEPLOY-REVIEW GATE.** Only after Mark's explicit go: redeploy all seats (covers claude-tail), soak against the live gate. Additive allowlist + capture fields; no consumer/schema change in 5a-0 scope.

- [ ] **Step 6: CHANGELOG + memory.** Add the `CHANGELOG.md` entry (what + why). Update the roadmap memory ([[arb-observability-roadmap]]) once live-gated.

---

## Self-Review (author's fresh-eyes pass against the spec)

**1. Spec coverage.** Every spec deliverable maps to a task: Deliverable 1 → Tasks 1, 4, 11, 12 (helper + per-producer wiring); Deliverable 2 (`turn_index`+`attempt_epoch`) → Tasks 5, 9, 10; Deliverable 3 (agent_sdk/pi_rpc) → Tasks 11, 12; Deliverable 4 (claude-tail lifecycle) → Tasks 3, 4, 5, 6, 7, 8; Deliverable 5 (allowlist) → Task 2; Deliverable 6 (redeploy+soak) → Task 13. R1 → Task 5; R2 → Task 3. **M2 precondition → EXTERNAL work item** (split per Mark, 2026-07-14) — asserted as a landed dependency by Task 13's M2 gate, NOT implemented here. Obligations O1–O5 + O-gate + the span-exactness question → **NOT implemented here** (correctly — they are named 5a obligations; the plan hands them forward, it does not clear them). Live gate → Task 13. Tests+deny-proofs (i–viii) → Tasks 6, 7, 8, 13.

**2. Placeholder scan (r1-fold: the ONE residual is CLOSED).** Task 11's four agent_sdk test bodies are now FULL
runnable methods on `EngineTest` (`_gate` drivers + `_run_turn` on_event capture), grounded in the existing
`tests/test_agent_sdk_engine.py` idioms. Task 12's pi_rpc test is a prose *rewrite instruction* over the concrete
existing `test_toolcall_and_tool_execution_deduped_by_tool_call_id` (`tests/test_pi_rpc.py:472-564`) — a
transformation of real code, not an empty body — plus a concrete `turn_completed` code block. **Task 13 Step 1b
now hard-refuses any `...`/`pass`/docstring-only test body** (grep + AST), so an inert test cannot ship green. No
`...` remains as an author-deferred gap. All claude-tail + allowlist + offset + epoch/turn_index tasks (1–10) have
complete runnable test + impl code, and Tasks 9/10 now include real-`Bridge` (not mirror-harness) deny-proofs.

**3. Type/name consistency.** `canonical_tool_call_id` (Task 1) — same name in Tasks 4/11/12. `Position(offset, turn_index)` + `load`/`store` (Task 6) — used consistently in Tasks 7/8. `logical_turn_index` (eval) vs `turn_index` (per-assistant trace) — the two-ordinal split is stated in Tasks 7/8. `turn_clock_monotonic` / `turn_started_ts` / `event_ts` / `attempt_epoch` / `tool_call_id` — the five allowlist members (Task 2) match every producer's stamp. `_task_epoch` / `_task_turn_index` (Tasks 9/10) — parallel per-task maps, cleaned up together in the `process_request` finally. `owner_token` — the existing per-boot token, referenced only by the EXTERNAL M2 work item, not a 5a-0 task.

## Resolved forks (do NOT reopen)

- **M2 placement (Mark, 2026-07-14): SPLIT.** M2 is a separate bridge-infra work item with its own plan/panel/live gate; 5a-0's Task 13 M2 gate **asserts the landed fix** (concrete: fix-commit ancestry + a runtime probe of the fenced behaviour). 5a-0's convergence is not hostage to M2's review; an M2 finding blocks M2, not a 5a-0 re-panel. Kick M2's plan off in parallel (its panel latency overlaps Tasks 1–12).

## Open Questions (surface to the plan panel; genuine forks go to Mark — do NOT vote-count)

1. **Dispatch `turn_index` per-engine boundary (SP0-2 residual).** Task 10 advances on `turn_completed`; confirm that signal exists for agy-tmux / codex / grok / cursor, or add per-engine `turn_completed` emission. If the panel splits on the mechanism, Mark fork.
2. **`EVAL_SCHEMA_VERSION` bump.** The plan treats the five additions as additive (no bump). Confirm the panel agrees they are not a breaking change to the pinned correlation fields.

*(r3-fold: former OQ2 "engine-test fixture gap" DELETED as vestigial — agy r3. The agent_sdk/pi_rpc test bodies are fully written runnable methods in v3/v4; Task 13 Step 1b's AST census refuses any inert body.)*

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-14-arb-observability-slice5a-0-capture-normalization.md`. Two execution options:

1. **Subagent-Driven (recommended)** — a fresh subagent per task, two-stage review between tasks, fast iteration (REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`).
2. **Inline Execution** — batch execution in-session with checkpoints (REQUIRED SUB-SKILL: `superpowers:executing-plans`).

But per the pipeline, the next step is the **plan panel** (codex-sol@high + agy + grok certify; cold-Opus non-cert), THEN execution once the plan converges.

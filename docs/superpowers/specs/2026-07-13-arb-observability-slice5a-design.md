# ARB Observability — Slice 5a: span data layer + retention (design **v2 — folded**)

Status: DESIGN v2 — folded from the design-review panel (needs-changes, CLOSED). Ready for the **spec panel**.
Date: 2026-07-13. Author: warm-Opus orchestrator (brainstormed with Mark).
Roadmap parent: [[arb-observability-roadmap]] Slice 5 (span tables + retention), decomposed.
Supersedes: v1 (same path). v1 was reviewed by codex-sol + codex-terra + agy + cold-Opus and returned
**needs-changes** (one P0, four+ P1s), all reality-checked against code. This v2 folds every required change.

> **Fold provenance.** The v1→v2 changes below are driven by 4 panel reports (codex-sol,
> codex-terra, agy, cold-Opus), not included in this copy.
> Mark RATIFIED the two structural reshapes: (D5) derive spans in `EvalConsumer`; and sequence a
> capture-normalization micro-slice (**5a-0**) as a prerequisite BEFORE 5a. Both are settled.

---

## What changed from v1 (the fold, in one place)

| # | v1 said | v2 does | Why (panel finding) |
|---|---------|---------|---------------------|
| 1 | Partition raw tables weekly on `inserted_at`; `DROP PARTITION` >8wk | **DELETE partitioning entirely.** Schedule the existing `purge_expired()` nightly | **P0, unanimous.** A partitioned table's UNIQUE must include the partition key; `UNIQUE(stream_entry_id, inserted_at)` does NOT dedup PEL redelivery (fresh `now()`) → duplicate raw rows → corrupts span lookup. (codex-terra P0, cold-opus P0, agy F4, codex-sol F4) |
| 2 | New always-on `SpanConsumer` looks up `*_started` in `eval_event_raw` | **Derive spans INSIDE `EvalConsumer`**, same txn as the raw insert; write-on-started, upsert-on-finished | **P1, ratified D5.** Two consumer groups have no cross-group ordering → `*_finished` routinely processed before `*_started` commits → mass deadletter of valid spans. (all 4 reports) |
| 3 | Capture ids "already flow; only stop the tee stripping them" (contained, in-5a) | **5a-0 = its own prerequisite micro-slice**: canonical `tool_call_id` + bridge-side `turn_index` across ALL engines; fix agent_sdk's `command_finished`; add canonical ids to `EVAL_ALLOWLIST`; fleet redeploy | **P1, ratified.** FALSE that ids flow: codex emits `item_id`/`turn_id`; agent_sdk emits `tool_call_id`/`item_id` and **no real `command_finished`** (emits it in the permission gate, pre-execution); no engine emits `turn_index`. (all 4 reports) |
| 4 | outcome only on `eval_task`; close-out on `stall_detected`/`task_finished`/`turn_timeout` | **Correct terminal-event model** (below) + add `outcome` to `eval_turn` + `eval_tool_call` | **New P1 (codex-sol, cold-opus, codex-terra).** `stall_detected` is an ALARM not terminal; `task_finished` ends EVERY task incl. success; Pi RPC + agy-print emit no normal `turn_completed`. |
| 5 | FIFO fallback for pre-augmentation events (approximate spans) | **DROP it. Exact-id only** | **D4, unanimous.** Approximate rows are indistinguishable from exact rows and mispair parallel calls; in steady state FIFO would silently manufacture wrong latencies, masking the race. |
| — | eval stream on "db6" | eval stream default DB is **4** (`ARB_EVAL_REDIS_DB`, bridge.py:177) | cold-opus nit, confirmed. |

**Build order (v2):** **5a-0 (capture normalization) → 5a (span derivation + scheduled purge) → 5b (dashboard).**
5a-0 gets its OWN design→panel→build→**fleet-redeploy**→live-gate cycle first; 5a depends on its done-criterion.

Validated/unchanged from v1: timing-only scope is right (F1 — tokens genuinely absent, emitted nested under
`usage`, unreachable via the flat `extract_eval_payload`; OUT of 5a as a separate capture-completeness effort);
the 5a-data / 5b-dashboard decomposition is sound; span schemas are timing-focused; `transcript_io` already
holds the I/O text so no separate `eval_io` table; retention is a uniform 8-week window (Mark's policy call).

---

## Scope decomposition

Slice 5 (the roadmap's "span tables + retention") decomposes into shippable sub-slices, mirroring 4a/4b:

- **5a-0 (NEW, prerequisite)** — **capture normalization**: canonical `tool_call_id` + bridge-side
  `turn_index` counter across every deployed engine family; fix agent_sdk to emit a real tool-completion
  `command_finished`; add the canonical ids to `EVAL_ALLOWLIST`; fleet bridge redeploy. Own live gate.
- **5a (this doc's core)** — span **data layer**: span derivation folded into `EvalConsumer` + span tables
  (`eval_turn`, `eval_tool_call`, `eval_task`) + **scheduled `purge_expired` retention** (folded 5c). Depends on 5a-0.
- **5b (separate spec)** — timing **dashboard**: gateway `/spans` endpoint + SSE + web/TUI panels. Depends on 5a.
- Token/cost analytics is **explicitly out** (Finding 1) — a separate capture-completeness slice.

## Findings that shaped this design (verified on prod 2026-07-13 + code-verified this session)

1. **The eval stream is timing + lifecycle only — no tokens/cost.** Payloads are overwhelmingly `{}`
   (`command_output` `{}`, `command_finished` `{"exit_code":0}`, `task_finished` `{"ok":true}`).
   `EVAL_ALLOWLIST` lists `prompt_tokens/…/latency_ms/turn_index` but engines don't populate them (usage is
   emitted nested under `usage` by agent_sdk/gemini/grok/cursor; codex — the dominant seat — emits none).
   `extract_eval_payload` is a flat top-level copy (`eval_tee.py:22-25`), so nested usage is unreachable
   without a producer change → OUT of 5a. **5a derives from timestamps + lifecycle only.**
2. **No stable, uniform pairing id in eval data — and NOT a strip-only fix.** codex emits `item_id`+`turn_id`
   (`codex.py:399-408`, `:298`); agent_sdk emits `tool_call_id`+`item_id` and **no tool-completion
   `command_finished`** (it emits `command_finished` inside `_gate`, pre-execution — `agent_sdk.py:347-373`;
   the real tool result arrives as `command_output`, `:551-575`); no engine emits an integer `turn_index`.
   → normalization is a fleet-wide, per-engine, redeploy-gated change = **5a-0**.
3. **`transcript_io` already holds the I/O text** (`content` column) → no separate `eval_io` table.
4. **Both raw tables grow unbounded** (`eval_event_raw` ~94MB/207k, `transcript_io` ~143MB/98k, ~+14MB/day).
   `purge_expired()` batch-delete ALREADY EXISTS for both (`eval.py:211`, `transcript.py:225`) — retention
   just needs a scheduler, not partitioning.

---

# 5a-0 — capture normalization (prerequisite micro-slice)

> Sketched here so 5a's dependency is explicit; **5a-0 gets its own design→panel→build→redeploy→live-gate**.

**Goal:** every deployed engine emits, into the eval payload, a canonical `tool_call_id` on both tool edges
and an integer `turn_index` on every turn edge, so 5a can pair exactly by id with no per-engine special-casing.

**Work:**
- **Canonical `tool_call_id`.** Normalize each engine's tool identifier (codex `item_id`, agent_sdk/pi
  `tool_call_id`/`item_id`, claude-tail `tool_use_id`, agy-print synthetic `item_id`) into one field at the
  bridge progress boundary. Same id MUST appear on the tool's start edge and its true completion edge.
- **Bridge-side `turn_index`.** A per-`(run_id, task_id)` monotonic integer counter stamped by the bridge
  (engines emit a provider `turn_id` string, not an ordinal). Define reset/restart/retry semantics explicitly
  (a warm-thread restart must not reset it mid-task).
- **Fix agent_sdk terminal event.** agent_sdk emits `command_finished` in the permission gate (pre-execution).
  It must emit a real tool-completion event (from the `command_output`/`ToolResultBlock` path) carrying the
  same canonical `tool_call_id` as the start, so a start↔finish pair exists at all.
- **Allowlist.** Add the canonical `tool_call_id` (and confirm `turn_index`) to `EVAL_ALLOWLIST`
  (`eval_tee.py:10-25`). Additive/backward-compatible: pre-normalization events simply lack ids (→ no span,
  never a wrong span — see D4).
- **Fleet bridge redeploy** + soak.

**Done-criterion (gates 5a):** canonical `turn_index` + `tool_call_id` present, and terminal-event coverage
proven, across every DEPLOYED engine family via one real dispatch per family (per-engine paired-event
fixtures + a live fleet-wire capture). 5a MUST NOT start span materialization until this is green.

---

# 5a — span data layer (core)

## Components

1. **Span derivation folded into `EvalConsumer`** (D5, ratified). No new service, no new consumer group, no
   `SELECT eval_event_raw` grant. In the SAME consumer/transaction that inserts the raw event, project the
   span: **write-on-started, upsert-on-finished**. Race-free by construction (single group = append-order:
   the `*_started` stream id is lower than its `*_finished`, so start is processed first).
2. **Span tables** — `eval_tool_call`, `eval_turn`, `eval_task` (rollup). Timing-focused (schemas below).
3. **Retention (folded 5c)** — schedule the EXISTING `purge_expired()` nightly via a **separate one-shot
   runner** (DO `pg_cron` or an externally-scheduled container), NOT a timer in the consumer, NOT DROP
   privileges in the ingest process. **No partitioning.** Fix F5: `transcript.py` purge → `inserted_at`.
4. **Deploy** — the retention runner (+ its least-privilege role); 5a-0's bridge redeploy is a 5a-0 concern.

## Span derivation — write-on-started / upsert-on-finished (race-free)

- **On `*_started`:** INSERT the span row with `started_at`, the natural key, `finished_at = NULL`. This IS
  the durable "open span" marker — no `eval_event_raw` scan needed to find open rows.
- **On `*_finished`:** UPSERT `finished_at` + compute latency in SQL, so any arrival order is safe:

  ```sql
  INSERT INTO eval_tool_call (run_id, task_id, tool_call_id, started_at)   -- start edge
  VALUES (...)
  ON CONFLICT (run_id, task_id, tool_call_id) DO NOTHING;

  INSERT INTO eval_tool_call (run_id, task_id, tool_call_id, finished_at)  -- finish edge
  VALUES (...)
  ON CONFLICT (run_id, task_id, tool_call_id) DO UPDATE SET
    finished_at = EXCLUDED.finished_at,
    latency_ms  = EXTRACT(EPOCH FROM (EXCLUDED.finished_at - eval_tool_call.started_at)) * 1000;
  ```

- **Idempotent + crash-safe.** Upsert on the natural key; `EvalConsumer`'s existing consumer-group PEL gives
  at-least-once + crash recovery. Latency is computed from `sent_at` deltas (bridge clock, `sent_at NOT NULL`).
- **No cross-consumer dependency, no lookup miss** — the class of bug that made v1 needs-changes cannot occur.

## Terminal-event model (the new P1 fold)

Close-out must reflect real engine semantics, NOT the v1 "close on stall/task_finished as incomplete":

- **`stall_detected` is an ALARM, not terminal.** It NEVER closes a span. At most annotate a stall marker;
  later progress can clear it (`bridge.py` stall path only records status + keeps running).
- **Normal turn completion** (`turn_completed`, where the engine emits it — codex does; **Pi RPC + agy-print
  do NOT**) → close the turn `outcome=finished`.
- **`turn_timeout`** → close the active turn `outcome=timeout`.
- **`task_finished` ends EVERY task, success included** (`{"ok":true/false}`). It closes only STILL-OPEN
  children, and derives their outcome from task `ok` + presence/absence of a terminal turn edge — it does NOT
  blanket-mark them `incomplete`. For engines with no `turn_completed`, a documented task-finish-derived
  completion rule fills in (rather than mislabelling every successful turn incomplete).
- **`outcome` column added to `eval_turn` AND `eval_tool_call`** (v1 had it only on `eval_task`), so "open",
  "finished", "timeout", "incomplete" are representable rather than smuggled into a NULL `finished_at`.

## Missing edges

- **Missing finish** (crash/timeout with no finish edge): the open span (`finished_at IS NULL`) is closed by
  the terminal-event model above; never fabricate a latency.
- **Missing start** (pre-5a-0 event with no canonical id, or a genuinely dropped start): **no span emitted,
  deadletter the orphan finish** ([[evidence-store-no-silent-drop]]). **No FIFO fallback** (D4) — a wrong
  latency in a timing dataset is worse than an absent one.

## Span table schemas (timing-focused, v2)

```
eval_tool_call: run_id, task_id, seat_id, orchestrator, turn_index, tool_call_id,
                tool_name, started_at, finished_at, latency_ms, exit_code, ok, outcome,
                started_stream_id, finished_stream_id, inserted_at
                UNIQUE (run_id, task_id, tool_call_id)
                outcome ∈ {open, finished, timeout, incomplete}

eval_turn:      run_id, task_id, seat_id, orchestrator, turn_index, started_at, completed_at,
                latency_ms, tool_call_count, ok, outcome, stop_reason
                UNIQUE (run_id, task_id, turn_index)
                outcome ∈ {open, finished, timeout, incomplete}

eval_task:      run_id, task_id, seat_id, orchestrator, started_at, finished_at, duration_ms,
                turn_count, tool_call_count, ok, outcome, attempt
                UNIQUE (run_id, task_id)
                outcome ∈ {finished, timeout, stall, incomplete}
```

Notes from the panel (codex-sol P2, cold-opus P2), folded: composite natural keys (not a bare
`tool_use_id UNIQUE` — engine tool ids are not globally unique across runs/seats); `orchestrator` added to
`eval_turn`; `stop_reason`/`finish_reason` will be mostly NULL (acknowledged, timing-only). `attempt`:
`task_continuing` events share one `task_id` (`bridge.py:1636`) — attempts are folded into the one `eval_task`
row, not separate rows (hence `attempt` is metadata, not part of the key).

## Retention (folded 5c) — scheduled purge, no partitioning

- **Nightly `purge_expired(older_than_days=56)`** for `eval_event_raw` AND `transcript_io`, run by a
  **separate one-shot** (DO `pg_cron` or externally-scheduled container). Batched DELETE already exists
  (`eval.py:211`, `transcript.py:225`); this only schedules it. Preserves `UNIQUE(stream_entry_id)` →
  zero idempotency/migration risk.
- **F5 fix (required):** `transcript.py:235` purges on `ts` (client clock — a backdated `ts` from seat clock
  skew could delete a just-ingested row). Change to `inserted_at` (db clock, monotonic) to match `eval.py:221`.
- **Retention role:** DELETE on the two raw tables only; no DDL/DROP, no consumer reach. Dry-run/list mode +
  boundary test. Span tables are NOT purged (tiny, secret-free, indefinite).
- **Content-retention note:** `transcript_io` holds captured content; 8-week by `inserted_at` is Mark's
  policy call. Dropping live rows does not expire provider backups (out of this guarantee).

## Error handling

- Span projection uses the audit/eval **dual-except** handler: infra errors retry, malformed events
  deadletter (`span_deadletter`, UNIQUE `stream_entry_id`), never ack-and-drop ([[evidence-store-no-silent-drop]]).
- Least-privilege: the span projection runs in `EvalConsumer`, which already writes the raw tables; it needs
  INSERT/UPDATE on the three span tables — **and NO `SELECT eval_event_raw`** (the cross-consumer lookup is
  gone) ([[structural-not-configurational-containment]]).

## Testing / done-criterion

- **Unit:** exact-by-id start↔finish pairing; finish-before-start arrival order (proves race-freedom of the
  upsert); missing-finish close-out per the terminal-event model; `stall_detected` does NOT close a span;
  `task_finished` on a SUCCESS does not mark children incomplete; idempotent replay (dup `stream_entry_id`);
  orphan-finish deadletters (no FIFO); `purge_expired` boundary; retention-role deny-proof.
- **Live-gate (REQUIRED, not just tests, [[live-verification-catches-cli-glue]]):** dispatch a real fleet task
  → assert `eval_task`/`eval_turn`/`eval_tool_call` rows land with correct latencies, both directions;
  deny-proof: revert 5a-0's id-stamp → spans degrade to deadletter (NOT wrong latencies), test reds; confirm
  an aged row purges. (5a-0's own live gate proves canonical-id + terminal-event coverage across engines FIRST.)

## Decisions (resolved by the panel + Mark; recorded for the spec)

- **D1 — partition migration:** RESOLVED — **no partitioning.** Scheduled `purge_expired` instead (P0).
- **D2 — retention runner:** RESOLVED — **separate nightly one-shot** (pg_cron / external container).
- **D3 — capture augmentation:** RESOLVED — **its own prerequisite micro-slice 5a-0** (Mark ratified).
- **D4 — FIFO fallback:** RESOLVED — **dropped; exact-id only.**
- **D5 — new service vs extend EvalConsumer:** RESOLVED — **derive in EvalConsumer** (Mark ratified); the real
  axis was stream-self-pairing, not service count.

## Open questions for the SPEC panel (v2)

- SP1 — `turn_index` semantics across warm-thread restart/retry (5a-0 owns the contract; spec must pin how 5a
  keys on it if a restart re-numbers).
- SP2 — task-finish-derived turn completion rule for engines with no `turn_completed` (Pi RPC, agy-print):
  exact derivation from task `ok`.
- SP3 — retention scheduler mechanism choice on the DO managed PG (`pg_cron` availability vs external
  container) — implementation-level, confirm at spec time.

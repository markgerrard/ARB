# ARB audit-emit + eval-trace — design spec

**Status:** design **v3** (Workflow B). Brainstormed + design-panelled + **re-panelled** 2026-06-23.
**Re-panel (3 seats, all engaged):** codex (architecture) + M3 (security-posture) + cold-Opus
(in-session). v2 verdict was needs-changes; the re-panel confirmed the architecture/slice-cut sound and
closed #3/#5/#7 cleanly, but flagged #1/#2/#4 as mechanism-not-prose. v3 folds those: run_id integrity
scoped to **mistake-prevention** (operator decision — consistent with [[arb-threat-model-recalibration]]:
the threat is mistakes on trusted infra, not a malicious orchestrator; minter-role enforcement is
productization-era, deferred); allowlist pinned by explicit key; `kind` given a backfill migration order.
(GLM not in this panel — not counted.) Q2 end-state deferred (see Open decisions). Source memories:
[[arb-audit-emit-unwired]], [[arb-eval-trace-capture]], [[bridge-seat-role-bound-at-launch]],
[[run-isolated-verdict]], [[evidence-store-no-silent-drop]], [[arb-threat-model-recalibration]];
close-condition discipline: ARB Memory `patterns/e2e-close-conditions`.

## Goal

Make ARB record, for every real panel run, two things that don't exist yet end-to-end:
1. **Audit** — the decision provenance (dispatch / vote / verdict) per run. *Built + canaried but dormant —
   nothing emits it in production today.*
2. **Eval traces** — per-turn / tool-call / usage fidelity (tool name, metadata, tokens, latency) for
   evaluating seats. *Not built.*

Both join on one key so you can ask "show me the trace behind verdict X" and "score seat Y across this
corpus." Audit = what was decided; eval = how each seat behaved getting there.

## Why a third data class (not bolted onto audit)

| | Memory | Audit | Eval traces |
|---|---|---|---|
| Unit | artefact version | one decision (dispatch/vote/verdict) | every turn / tool call |
| Cardinality | low | low (a few/run) | high (thousands/run) |
| Retention | indefinite | indefinite (provenance) | bounded window → aggregate/drop |
| Reads | semantic + by-key | by-run, ordered-by-seq | analytical/aggregate across runs |

High-volume trace writes must not backpressure the audit single-writer (whose job is to never drop a
verdict). Eval gets its own Valkey db, its own PG tables, its own consumer.

## Architecture

### 1. The correlation spine (the load-bearing decision)

The orchestrator **mints one `run_id` (uuid) per panel** and **threads it through every dispatch envelope**
(new envelope field). Every audit row and every eval row persists that `run_id`. The join is reliable only
because the key travels with the work from the single causal artifact (the dispatch envelope) — NOT an
orchestrator-side `task_id→run_id` map (that only proves the orchestrator *believed* task X belonged to run
Y; it doesn't prove the bridge, audit writer, eval consumer, and traces all observed the same identity).

A panel run = one `run_id` spanning N dispatches (N `task_id`s).

**`run_id` integrity — mistake-prevention scope (operator decision; re-panel #2).** The re-panel
established the v2 "bridge rejects run_id from non-orchestrator senders" had no enforcement point (the
`Envelope` field set is the fixed 8 fields `id, sender, branch, recipient, kind, sent_at, payload,
in_reply_to` — there is no `run_id` field and no minter role in `AGENT_TRUSTED_SENDERS`). Per
[[arb-threat-model-recalibration]] the threat here is *mistakes on trusted infra, not a malicious
orchestrator*, so slice 1 builds mistake-prevention, NOT adversarial minter-auth:
- **`run_id` becomes a new named top-level `Envelope` field** (a concrete code change — see below), so it
  rides the single causal artifact and `to_dict`/`from_json` round-trip it as a first-class field. Because
  the field set is fixed, a payload-smuggled `payload.run_id` is structurally ignored (good).
- **Build requirement (must be in the plan, not left to guess):** add `run_id: str` to the `Envelope`
  dataclass; add it to `from_json`'s required-field validation as **non-empty** (blank/missing `run_id` is a
  hard error at the emit boundary, never a silent insert — ties [[evidence-store-no-silent-drop]]); add it to
  `to_dict`. Tests: round-trip, missing→error, blank→error, payload-smuggled-`run_id`-is-ignored.
- **Orchestrator mints by convention.** The orchestrator is the sole minter *by convention* (it owns the
  dispatch path); the bridge does not authenticate "who may mint." (M3's derive `run_id = f(sender,task_id)`
  alternative is **rejected** — it yields a *per-task* id and cannot span the N task_ids of one panel.)
- **Deferred to productization (Open decision):** a `run-minter` sender role / `AGENT_RUN_MINTER_SENDERS`
  allowlist so a non-minter's `run_id` is *rejected*. This is the adversarial-auth [[arb-threat-model-recalibration]]
  marks as not-now; revisit if ARB leaves the trusted-solo setting.
- **Stable across retries/forks.** A retry or `--fresh-context` re-dispatch of the *same* panel reuses the
  *same* `run_id`; a new `run_id` is minted only when a new panel opens.

### 2. Audit emit — orchestrator-writes-votes (this slice)

The **orchestrator** emits all three audit kinds via `AuditRun.emit`:
- `dispatch` — when it sends a seat its task.
- `vote` — when it interprets a seat's terminal reply as a vote (the orchestrator is the judge of "is this
  a vote").
- `verdict` — the panel outcome.

Audit events carry `run_id` + `task_id`. To keep provenance honest even with orchestrator-sourced votes,
distinguish **emitter** from **actor**: `source` = `orchestrator`, and the voting seat goes in the payload
as `actor` (e.g. `{"actor":"seat:codex-1","run_id":...,"reply_ref":...}`).

**`kind` is a queryable column, not just a payload key (panel P0 — cold-Opus / codex / M3).** Because every
audit row in this slice has `source = orchestrator`, the close-condition join cannot distinguish dispatch /
vote / verdict in SQL unless `kind` is first-class. Add a `kind text not null` column to `audit_events` —
but `audit_events` is **already populated** (the audit canary wrote rows), so a bare `ADD COLUMN kind text
NOT NULL` fails, and `PostgresAuditSink.write` (`src/arb_memory/audit.py`) does **not** currently write a
`kind` column. **Migration order (must be in the plan, in this order):**
1. `ALTER TABLE audit_events ADD COLUMN kind text` (nullable).
2. Backfill: `UPDATE audit_events SET kind = raw_entry->>'kind' WHERE kind IS NULL AND raw_entry->>'kind' IS NOT NULL`
   (the canary rows carry `kind` in the raw entry / payload).
3. Any row still `kind IS NULL` (unbackfillable) is **quarantined** (set to an explicit `'unknown'` sentinel
   or deadlettered — not silently NULL), so the tighten cannot fail-silently.
4. `ALTER TABLE audit_events ALTER COLUMN kind SET NOT NULL`.
5. Update `PostgresAuditSink.write` to INSERT `kind` (from `AuditRun.emit(source, kind, payload)`), and
   update the consumer SELECT/round-trip tests; assert the column and any `payload['kind']` agree.

Single `AuditConsumer` (group `arbmem-audit`) remains the sole DB writer (single-writer property unchanged).

> **Deferred (Open decision):** promoting `vote` to **bridge-emitted** ("bridge observed seat reply" — better
> provenance) is the alternative end-state. It crosses a trust boundary (bridge writing to the audit stream;
> M3 F3). Not in this slice; revisit when provenance fidelity is judged worth the trust-surface cost.

### 3. Eval trace capture — tee-at-emit, not tail

The bridge already emits `tool_call`/`turn`/usage events to ephemeral per-task streams (`task:<id>:events`,
maxlen+TTL, db12). **Tee** one extra XADD at the single choke point (`Bridge.push_task_event`) onto a durable
consolidated stream `eval:events` on its **own Valkey db** (generous/no maxlen), read by a consumer group
(PEL → at-least-once + crash recovery). **Do NOT tail the ephemeral streams** — that races the TTL and
silently loses events (the failure mode this whole line of work exists to prevent).

- The tee stamps `run_id` (from the envelope, orchestrator-authored) + `seat_id` (`from`) + `task_id` on
  every teed event.
- **Capture what each engine provides; do not normalize down to the weakest.** Fidelity is engine-dependent
  (codex/ACP: per-turn + per-tool spans; `agy_print`: turn-level only, `tool_call_count`, no per-tool). The
  schema tolerates partial fidelity.

**The tee EXTRACTS ONLY an enumerated key set — never "forward minus a denylist" (panel P0 — codex / M3 /
cold-Opus).** Filtering "everything except `model_text`/`model_thinking`" is wrong: `command_started` (raw
command + args) and `command_output` (raw tool stdout/stderr deltas) flow through `push_task_event` too
(`bridge.py:1441/1461`, `codex.py:167`) and carry secrets/PII. The re-panel's residual: a positive-list
phrasing still lets a builder implement "copy the dict, delete known-bad keys" and miss one. **So the tee
constructs the durable payload by COPYING ONLY the keys below out of the source event into a fresh dict; the
source dict is never forwarded wholesale.** Slice-1 allowed keys (everything else, including any unknown
key, is absent by construction):
- envelope/correlation: `run_id`, `task_id`, `seat_id` (`from`), `event_type`, `sent_at`
- turn/usage metadata: `tool_name`, `tool_call_count`, `turn_index`, `stop_reason`/`finish_reason`,
  `prompt_tokens`/`completion_tokens`/`total_tokens`, `latency_ms`
- **Excluded by construction in slice 1:** `command`/args text, `command_output`/stdout/stderr, `model_text`,
  `model_thinking`, any free-text field — i.e. all of `eval_io`. A **deny-proof test** seeds a synthetic
  event carrying a `command_output` field and asserts it never appears in `eval_event_raw` (delete the
  extract-allowlist → the test reds).

## Schema (PG, eval tables in their own namespace)

**This slice — staging only:**
```sql
eval_event_raw (
  id              bigserial primary key,
  run_id          text not null,
  task_id         text not null,
  seat_id         text,
  event_type      text not null,
  sent_at         timestamptz not null,
  payload         jsonb not null,         -- allowlisted metadata only; raw I/O excluded at the tee
  stream_entry_id text not null,          -- the eval:events XADD id (idempotency key)
  inserted_at     timestamptz not null default now(),
  unique (stream_entry_id)
);
```
`stream_entry_id` + `unique` + `ON CONFLICT (stream_entry_id) DO NOTHING` give **at-least-once idempotency**
(panel P1 — codex / cold-Opus): a consumer crash after INSERT-before-XACK redelivers the entry; without the
key it double-counts while the join still "passes." **`stream_entry_id` is the id Valkey assigned on XADD,
captured by the consumer from its `xreadgroup` response (NOT a producer-side or freshly-generated id)** — the
same id is re-presented on PEL redelivery, which is what makes `ON CONFLICT` catch the duplicate (re-panel
M3). Two tests, mirroring `AuditConsumer.drain_pending`: (a) redeliver one entry → single row; (b)
crash-recovery — insert without XACK, restart consumer, assert the entry re-reads from PEL, the second INSERT
is a no-op, and it is XACK'd after.

**`audit_events` — no DDL for `run_id` (panel correction, unanimous).** `run_id` is **already** a top-level
`NOT NULL`, indexed column; the v1 note "add a `run_id` column if the join needs it" was factually wrong.
The only audit-table change this slice is the **`kind` column** — added via the **backfill migration order in
§2** (nullable → backfill from `raw_entry->>'kind'` → quarantine unbackfillable → `SET NOT NULL`), because the
table is already populated by the audit canary.

**Later slices (deferred — see Non-goals):** normalized spans `eval_turn`, `eval_tool_call`, and `eval_io`
(raw text, own table, hot/cold split, opt-in), time-partitioned (DROP-PARTITION retention), BRIN on
`started_at`.

## Security guardrails (M3 ORACLE-FLAG — non-negotiable)

- **P0 — the tee allowlists; raw I/O never lands.** Per §3: only enumerated safe-metadata fields are teed;
  `command_output` and raw `command_started` text are excluded from slice 1. `eval_io` (raw prompt/response
  text) is **not shipped at all** this slice. When it later ships it is opt-in per panel, masked at the tee,
  capped at write, retention-bounded, role-gated.
- **P0 — redaction/allowlist is code, tested, before the consumer is wired.** The tee's allowlist + cap must
  be in code with a test proving a denied field (e.g. a synthetic `command_output`) does not reach
  `eval_event_raw` — not a doc promise.
- **P1 — grants land WITH the DDL, not after.** `apply_eval_grants`: `REVOKE ALL`, no `PUBLIC` grant; the
  eval consumer role gets only what it needs; the MCP read role does NOT get eval tables unless deliberately
  granted. Mirrors the memory/audit grant discipline.
- **P1 — bridge does not write the audit stream** in this slice (orchestrator-only audit), keeping the audit
  trust surface tight.
- **P1 — eval consumer is single-tenant / prefix-isolated (M3 F3).** Its own Valkey db + its own
  group/prefix; it does not share a consumer group with memory or audit. The threat model is *mistakes on
  trusted infra*, not a malicious orchestrator (see [[arb-threat-model-recalibration]]) — but write-ACL and
  isolation still bound blast radius.

## Cross-repo contract (pin before planning — panel P1, codex)

The eval bus crosses `AgentRedisBridge` (producer) and ARB Memory (consumer); an unpinned contract reproduces
the `audit.py` prefix-mismatch class (producer writes one shape, consumer reads another). Pinned values:

| Thing | Value |
|---|---|
| Eval Valkey db | **db4** on the DO Valkey instance (db3 = memory/audit; db4 = eval — adjacent, distinct from db12 live bus and db15 tests). Resolved via `ARB_EVAL_REDIS_DB=4` in deploy config; the number lives in env, not code, so both repos agree. |
| Stream key | `eval:events` (single consolidated stream) |
| Consumer group | `arbmem-eval` |
| Key prefix | `ARB_EVAL_PREFIX` (its own var — NOT `ARB_MEMORY_PREFIX`; the eval consumer must not inherit the memory/audit prefix, re-panel M3) |
| Env vars | `ARB_EVAL_REDIS_URL` / `ARB_EVAL_REDIS_DB` / `ARB_EVAL_PREFIX` — **producer (bridge) and consumer (ARB) read the identical var names**; a config test asserts both resolve to the same db + stream. |
| Stream fields | `run_id`, `task_id`, `seat_id`, `event_type`, `sent_at`, `payload` (allowlisted JSON per §3). The `stream_entry_id` is NOT a stream field — it is the XADD-assigned id the consumer reads from `xreadgroup` (see Schema). |

**Slice-1 event vocabulary (pin so the close condition's "start + terminal" is executable — re-panel codex
P2).** The tee allowlist forwards these `event_type` values in slice 1; the close condition's per-task
"≥ start + terminal" counts the start/terminal pair below. Engine fidelity varies (codex/ACP emit all;
`agy_print` emits only the task-level pair + counts) — so **only the task-level start/terminal are required
of every seat**; turn/tool rows are captured-when-present:
- **start (required):** `task_started`
- **terminal (required, exactly one of):** `task_succeeded` / `task_failed` (normalize to a `terminal`
  predicate in the close query)
- **optional (captured if the engine emits):** `turn_started`, `turn_finished`, `tool_call`
The plan pins the exact strings against the bridge's real `event_type` emissions (`push_task_event` call
sites) before writing the allowlist test — if the bridge's actual names differ, the bridge's names win and
this list is corrected, not the code.

## Delivery — slices

**Slice 1 (this spec — prove the spine, nothing else):**
1. Add `run_id` to the `Envelope` dataclass + `from_json` (non-empty validation) + `to_dict` (§1 build
   requirement). Orchestrator mints `run_id` per panel (by convention) and threads it into every dispatch
   envelope; reuses it across retries/forks of the same panel. No minter-role enforcement (deferred).
2. Bridge preserves `run_id` on task events; tees to durable `eval:events` (own db) with `run_id`/`seat_id`/
   `task_id` + **allowlist** the forwarded fields (raw-output types excluded).
3. ARB: `eval_event_raw` table (with `stream_entry_id` unique) + `apply_eval_grants` (REVOKE-ALL, same commit
   as DDL) + a minimal eval consumer draining `eval:events` → `eval_event_raw` with `ON CONFLICT DO NOTHING`;
   add `eval:events` **lag/size alarm + OOM/maxlen backstop** (mirror the audit stream's posture).
4. ARB: add `kind` column to `audit_events`; orchestrator emits `dispatch` + `verdict` (+ `vote`,
   orchestrator-sourced, `actor` in payload, `kind` in the column).
5. Run **one live panel**; verify the close condition below (cardinality + both-direction joins + negative).

**Cut from slice 1** (add only after the spine is proven): model stamping (nullable `model_id`, don't block
on it — `agy_print` may not know its model), `eval_io`, normalized `eval_turn`/`eval_tool_call`,
partitions/BRIN, retention jobs, seat-owned audit emit, bridge-emitted votes.

## Cross-repo touch points

- **`<workspace>` (orchestrator/dispatch):** mint+thread+validate `run_id`; wire `AuditRun.emit` for
  dispatch/vote/verdict with `kind`.
- **`AgentRedisBridge` (bridge engine):** preserve `run_id` on events; tee at `push_task_event` to
  `eval:events` with the allowlist. (Bridge code change — operator-directed; do not touch the fleet clone
  casually.)
- **ARB Memory:** `eval_event_raw` + `apply_eval_grants` + eval consumer; `kind` column on `audit_events`.

Sequencing keeps each repo's change independently testable; the spine is proven only by the end-to-end live
panel, not by any one repo's unit tests.

## Close condition (done criterion)

NOT "writers deployed." **Done = one LIVE panel lands rows in Postgres with correct correlation keys,
queryable, joins verified — with a transcript showing the actual `run_id`, the N task ids, and the rows.**
The join check is **cardinality, not existence** (panel P1 — codex), because "a row exists" passes while
another seat silently has none:

1. Exactly **one** distinct `run_id` for the panel across both tables.
2. Exactly **N** `dispatch` audit rows — one per dispatched seat (by `kind`).
3. A **`vote`** row for **every terminal seat** (vote coverage = terminal seats), no more.
4. Exactly **one** `verdict` row.
5. Eval rows for **every dispatched `task_id`** (no task with zero eval rows).
6. **≥ a start + a terminal** event per task in eval.
7. **Zero** eval/audit rows with a missing or foreign `run_id`.

Plus a **fail-loud negative (panel P2 — cold-Opus):** inject one event with `run_id` stripped and assert it
**dead-letters / is refused, not silently inserted** (deny-proof, inject-revert; ties
[[deny-proofs-need-adversarial-verification]] — delete the guard → this reds).

Plus the **drain barrier (separate-writer rule):** the eval consumer carries the same post-stop barrier the
seat/audit canaries do (count-stability across the stop boundary, since `eval:events` ids are emitter-clock).
Per `patterns/e2e-close-conditions`: assert the real path ran against **run-isolated** state (scope every
assertion to *this* `run_id`, never a shared `LIKE` namespace — [[run-isolated-verdict]]); reliability ≠
clean-once.

**Slice-1 retention posture:** `eval_event_raw` is staging; the live-panel rows asserted by the close are
**deleted in the same change** (run-scoped cleanup). Bounded retention / partitioning is a later slice — but
slice 1 must not leave an unbounded table or an unalarmed stream behind.

## Non-goals (this spec)

Normalized eval spans, `eval_io`, retention/partition machinery, model stamping, bridge-emitted votes,
per-dispatch role injection. All explicitly deferred to follow-on slices once the spine is live.

## Open decisions (for the operator)

1. **Q2 end-state** — keep orchestrator-emitted votes, or promote to bridge-emitted for provenance (crosses
   M3's trust-bar). Slice 1 is orchestrator-only regardless; this only affects a later slice.
2. **`run_id` minter enforcement (deferred — productization)** — a `run-minter` sender role /
   `AGENT_RUN_MINTER_SENDERS` allowlist so a non-minter's `run_id` is *rejected* at the bridge. Decided OUT
   of slice 1 (operator, 2026-06-23): the threat is mistakes not malice on trusted-solo infra
   ([[arb-threat-model-recalibration]]). Revisit if ARB leaves the trusted setting.

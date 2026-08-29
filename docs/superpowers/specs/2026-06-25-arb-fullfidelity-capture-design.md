# ARB Observability — Full-Fidelity Transcript Capture (design)

**Status:** design (autonomous, 2026-06-25). Mark WANTS Claude-Code-style subagent transcripts in ARB
Visibility: the seat's model output + thinking + granular tool calls/edits, **live** + **persisted**. Builds on
Slices 1/3/4a/4b (merged `8db0397`). Slice 5 (span-table normalization) follows this. Background + per-engine
fidelity + the privacy analysis + the tmux-agy hail-mary live in memory `arb-transcript-capture-deferred`.

## Goal
Replay exactly what a seat did — model responses, extended thinking, every tool call with its args/effects
(bash command + output; edits as file + diff) — streaming live in the agent pane and queryable in history.

## THE load-bearing constraint (why the bridge drops this today)
`handle_progress` (bridge.py:1550) `return`s on `model_text`/`model_thinking` BEFORE `push_task_event` — by
deliberate design: "per-token XADD+EXPIRE+HSET over TLS-to-DO-Valkey is ~100ms each … minutes of self-inflicted
backpressure in the stdout-read hot path, throttling the engine itself." **So full-fidelity must NOT re-introduce
per-token cross-WAN writes.** This is the design's central problem, and the first thing the panel should attack.

### Resolution (v2, panel-hardened): STRICTLY non-blocking hot path → single daemon flusher
The hot path (`handle_progress`, on the engine read-loop thread) must do the absolute minimum:
- **`handle_progress` appends each delta to a bounded `queue.Queue` via `put_nowait` — and NOTHING else.** No
  Redis, NO redaction, NO `apply_patch` parse, NO large JSON serialization, NO *blocking* `put`. On queue-full,
  `put_nowait` raises → **drop/coalesce the delta with a `truncated` marker** (fail-soft; never block the engine).
  This is the load-bearing rule (panel P0/P1, all 3 seats): a blocking put — or a mutex held across the flusher's
  cross-WAN XADD — re-introduces the exact ~100ms/token stall the drop removed, "through the lock."
- **CRITICAL fix:** the current `model_text`/`model_thinking` branch `return`s after the 8s heartbeat WITHOUT
  capturing (bridge.py:1550, agy P2). v2: **append to the queue FIRST, then the heartbeat, then return.**
- **A single daemon flusher thread** drains the queue and does ALL the heavy work OFF the hot path: redaction,
  reassembly, `apply_patch` parse, coalescing, and the two tees (live stream + durable-bound records). One global
  worker (not per-turn) — but task cleanup must NOT delete state the flusher still needs, so the queued items
  **carry all their own context** (run_id/task_id/seat/orchestrator/turn/item/kind), making cleanup independent.
- **Lifecycle:** on turn-end/timeout/error, enqueue a `turn_end` marker; the flusher does a best-effort drain.
  **Crash loss is acceptable** — the transcript path is **non-load-bearing telemetry, never the source of truth
  for anything graded** (votes/audit are durable on the independent `_emit_vote`→`audit_redis` path). State this.
- **Per-content cap (panel P2):** truncate any single `tool_output`/text item > ~256KB with a marker, so one
  runaway bash output can't OOM the buffer.

## Two sinks (live vs durable), both off the hot path
1. **Live transcript stream — `arbmem:trace` (or `transcript:<run_id>`).** Coalesced deltas for the visibility
   seat-detail to tail in real time. **Prod-reachability (deploy-readiness P0 lesson):** like the eval/audit
   tees, this uses a SEPARATE client pointable at prod Valkey (`ARB_TRACE_REDIS_URL`), NOT the bridge's local bus
   — else the prod visibility view is empty (the exact gap `events:live` has). Bounded `maxlen` + short TTL
   (ephemeral; the durable store is the record). Fail-soft.
2. **Durable transcript store — `transcript_io` (a DISTINCT table, NOT `eval_io`/the eval plane — panel P2).**
   `eval_tee.py:3` guarantees "raw I/O … model_text/_thinking absent by construction"; persisting raw text into
   the eval family would blur the invariant the eval grant deny-proof rests on. So a separate `transcript_io`
   table + its OWN grant REVOKE (door role cannot read it) + its OWN deny-proof — smaller blast radius if
   redaction ever leaks. Columns: `{run_id, task_id, seat_id, orchestrator, turn_index, item_id, seq, kind
   (model_text|thinking|tool_call|tool_output), tool_name, content (text), meta (jsonb: exit_code/file/diff-stat),
   ts}`. Written by a `TranscriptConsumer` draining the trace stream (single-writer, like EvalConsumer) → Postgres.
   **Retention/TTL + redaction (see below).**

## Reassembly — REQUIRES engine-schema normalization FIRST (panel P1, all 3 seats)
**The correlation key the original design assumed does NOT exist.** Codex emits `model_text` as
`{"delta": delta}` with no `item_id`/`turn_id` (codex.py:144); only `command_output` carries `item_id`
(codex.py:170). `agent_sdk` text/thinking lack ids (tools have `tool_call_id`, agent_sdk.py:311); ACP engines
similar. So `(task_id, item_id)` coalescing is **unbuildable for the two highest-volume kinds** (model text +
thinking) until the engines are fixed. → **Prerequisite task T-0: normalize every engine's `on_event` schema** —
thread `turn_id`, `item_id` (or `tool_call_id`), `kind`, and a monotonic `seq` through every engine's progress
callback, with documented fallback-key rules (e.g. for an engine that can't supply an item id, key by
`(task_id, turn_id, kind)` and split on `kind` change). Only then can the flusher coalesce deltas → one row per
turn/tool-call. Live frames carry the normalized ids so the renderer appends to the right block.

## Per-engine fidelity (verified; see memory)
- **Tier 1 (stream + tools):** `codex` (model_text deltas; commandExecution → command + output deltas; **edits =
  `apply_patch` commands** — parse the patch to file+diff), `agent_sdk` (Claude: + model_thinking + structured
  tool_call), `cursor/gemini/grok` ACP (+ structured tool_call). Full transcript.
- **Tier 2 (final block):** `agy-print` (Antigravity/Gemini; compute-then-print, PTY-tested no-stream) — capture
  the complete `turn_completed` result as one `model_text` row. The renderer shows a block, not a stream.
- **Follow-on track (separate mini-design):** tmux-interactive agy/Antigravity (spiked viable; trust-dialog +
  pane-parse) to upgrade agy to streaming. NOT in this slice.

## Privacy — redaction + retention (Mark's deployment: ACL'd + TLS Valkey, solo operator)
The live view is ~zero added risk (own agents, TLS+OAuth+ACL; the door CANNOT read eval/audit — grants
deny-proven). The real concern is **secrets-at-rest** (an agent reads a `.env`, a tool echoes a token). Panel
corrections folded:
- **The redactor is defense-in-depth, NOT the boundary (panel P2).** The REAL boundary = grant-REVOKE + TTL +
  TLS/ACL. Pattern-scrubbing will miss novel secret shapes; sell it as "obvious-secret scrubbing," never "proof."
- **Redaction runs in the FLUSHER (off the hot path), before BOTH the live frame AND the durable write (panel,
  codex+agy).** "Raw live" is NOT the baseline — once the visibility pane is tunneled + the bearer token shared,
  raw live leaks. Redaction **default-ON**; raw-live only via an explicit opt-in flag. Note: `agent_sdk` already
  scrubs at the engine (ScrubbedSessionStore) but **codex does NOT** — the single tee chokepoint is right because
  it covers codex's currently-unredacted output. `redact(text)->text` + a **secrets-corpus deny-proof**.
- **Prod kill-switch / disable-by-default (panel, codex).** Full-fidelity capture is OFF in prod until trusted —
  an operator config gate (`ARB_TRANSCRIPT_CAPTURE=off` default in prod), so the feature can't silently start
  persisting transcripts.
- **Retention/TTL** on `transcript_io`: time-partition + drop after N days (default e.g. 14d) — a privacy control
  (bounds the secrets window) + volume control. (The retention half of roadmap Slice 5, pulled forward.)
- **Grant boundary:** `apply_mcp_grants` REVOKEs `transcript_io` from the door role (extend grants.py + deny-proof).
- DB/backup encryption-at-rest: confirm on the managed PG (operator note).

## Render (visibility seat-detail — extends 4a/4b)
`/sse/seat/{task_id}` backfill+live already exists. Add the trace source: backfill from `eval_io` (by task_id,
ordered by ts/turn) + live-tail the trace stream filtered to task_id. The web + TUI seat panes render: model text
(streamed), thinking (collapsible), tool calls (`apply_patch` → "edited `foo.py` +12/−3" with an expandable
diff; bash → command + output), redacted spans shown as `‹redacted›`. (4b's renderers extend; new event kinds.)

## Decompose (build order — panel-revised)
- **T-0 Engine progress-schema normalization (NEW, prerequisite):** thread `turn_id` + `item_id`/`tool_call_id` +
  `kind` + monotonic `seq` through every Tier-1 engine's `on_event` (codex, agent_sdk, the ACP engines), with
  documented fallback-key rules. Pure additive to the event `data`; no behavior change. Tested per engine that
  the ids/seq are present + monotonic. Without this, reassembly is unbuildable (panel P1).
- **T-1 Bridge hot-path capture (STRICTLY non-blocking):** `handle_progress` appends to a bounded `queue.Queue`
  via `put_nowait` and nothing else (un-drop model_text/thinking → append BEFORE the heartbeat-return); drop +
  `truncated` marker on full. Tested: a deny-proof that the hot path does NO Redis/redaction/parse/blocking-put
  (e.g. a slow/wedged flusher does NOT slow `handle_progress` — the backpressure guard).
- **T-2 Flusher + live tee + redaction:** single daemon worker draining the queue → redaction (default-on, corpus
  deny-proof) → reassembly (coalesce by normalized ids) → live XADD to `ARB_TRACE_REDIS_URL` (prod-reachable
  separate client, batched/coalesced) + durable-bound records. Per-content cap. Fail-soft. Best-effort drain on
  turn-end. The capture kill-switch (`ARB_TRANSCRIPT_CAPTURE`).
- **T-3 Durable store + consumer + retention + grants:** `transcript_io` schema (+ idempotent ALTER),
  `TranscriptConsumer` (single-writer → Postgres), retention/TTL, grants REVOKE + **deny-proof (door role cannot
  read transcript_io)**. `apply_patch`→file+diff parsing lives HERE (or the renderer), never the bridge.
- **T-4 Render:** visibility seat-detail trace backfill (`transcript_io`) + live-tail (`ARB_TRACE_REDIS_URL`) on
  web + tui; apply_patch→diff view; redacted spans shown as `‹redacted›`.
- E2E: a real codex turn (model text + a bash command + an apply_patch edit) → transcript lands live + persisted +
  redacted; the backpressure deny-proof (wedged flusher ⇏ slow hot path); a planted secret is redacted in both sinks.

## Out of scope (here)
Slice 5 span-table NORMALIZATION (`eval_turn`/`eval_tool_call` analytical views over `eval_io` — next).
tmux-interactive agy streaming (follow-on). The prod deploy (operator; deploy-readiness findings saved).

## Open questions for the panel
1. Is the buffer+batch-flusher the right backpressure fix, or is an async-queue / bounded-channel cleaner? Does it
   risk loss on crash (vs the per-token write)? Acceptable trade (fail-soft telemetry)?
2. Live stream placement: separate `ARB_TRACE_REDIS_URL` (prod-reachable) vs reuse the eval bus (db-6) — collision?
3. Redaction at the tee vs at the consumer (before persist only) — does the LIVE frame also need redaction (yes if
   watchers ≠ operator; for solo, maybe live is raw, persist is redacted)? 
4. `apply_patch` parsing in the bridge vs the renderer — where does the file+diff extraction live?
5. Volume: even batched, transcripts are large. Per-turn durable rows + N-day TTL enough, or need sampling/caps?

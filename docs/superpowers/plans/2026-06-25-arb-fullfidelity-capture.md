# ARB Full-Fidelity Transcript Capture — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Capture each seat's model output + thinking + granular tool calls/edits → a live transcript stream (visibility seat-detail tails it) + a durable `transcript_io` store — without re-introducing the per-token cross-WAN backpressure the bridge deliberately removed.

**Architecture:** STRICTLY non-blocking hot path (`handle_progress` does `queue.put_nowait` + nothing else) → a single daemon **flusher** that does ALL heavy work off the hot path (redaction, reassembly, tees) → a live stream on a prod-reachable `ARB_TRACE_REDIS_URL` + a `TranscriptConsumer` → Postgres `transcript_io` (retention/TTL + door-REVOKE). Redaction default-on with a prod kill-switch. Spec: `docs/superpowers/specs/2026-06-25-arb-fullfidelity-capture-design.md` (v2). Build in worktree `arb-ff`.

## Global Constraints
- **The hot path (`handle_progress`, engine read-loop thread) must be strictly non-blocking:** ONLY a bounded `queue.Queue.put_nowait`. NO Redis, NO redaction, NO `apply_patch` parse, NO large serialization, NO *blocking* put. On `queue.Full` → drop the delta + set a `truncated` flag (fail-soft). A blocking put OR a mutex held across the flusher's cross-WAN XADD re-introduces the ~100ms/token stall — forbidden.
- **Transcript capture is non-load-bearing telemetry** — never the source of truth for anything graded (votes/audit are independent + durable). Crash loss of the in-flight buffer is acceptable.
- **Redaction runs in the FLUSHER**, before BOTH the live frame AND the durable record. **Default ON.** Prod kill-switch `ARB_TRANSCRIPT_CAPTURE` (off-by-default in prod). The redactor is defense-in-depth, NOT the boundary (grant-REVOKE + TTL + TLS/ACL are).
- **Distinct `transcript_io` table** (not `eval_io`/the eval plane). Door role gets a grant REVOKE + deny-proof.
- **Live stream on a SEPARATE prod-reachable client** (`ARB_TRACE_REDIS_URL`), not the bridge's local bus (the `events:live` cross-WAN P0).
- `apply_patch`→file+diff parsing lives in the consumer/renderer, never the bridge.

### Test harness
```bash
cd /Users/<user>/<workspace> && export PYTHONPATH="$(pwd):$(pwd)/src"
set -a; . envs/arb-memory-dev.env; set +a; export ARB_MEMORY_REDIS_URL=redis://127.0.0.1:6379/15
PYTEST=/Users/<user>/<workspace>/.venv/bin/pytest
```

---

### Task T-0: Engine progress-schema normalization (prerequisite for reassembly)

**Files:** Modify `src/agent_redis_bridge/engines/codex.py` (~139-170), `agent_sdk.py` (~296-325), `gemini_acp.py`/`cursor_acp.py`/`grok_acp.py` (the model_text/thinking/tool_call emit sites). Test: `tests/test_engine_progress_schema.py` (create) — per-engine where feasible without a live engine (unit the normalization helper + the emit shapes via the engines' internal mappers).

**Interfaces:** Every `on_event(kind, data)` data dict gains: `turn_id` (str), `item_id` (str — the engine's item/tool id, or a synthesized per-(turn,kind) id), `seq` (monotonic int **per engine-instance, never reset per turn** — panel: a per-turn reset is not task-monotonic, complicates ordering; a single counter that increments across the whole run is simpler to sort), `kind` (already on agent_sdk tool events). model_text/thinking get `item_id` = the engine item id if present else `f"{turn_id}:text"`/`:thinking` (one logical item per turn+kind). A small `next_seq()` closure over an instance counter supplies `seq`.

> **Per-engine `turn_id` / `item_id` source (panel P1 — the plan must NAME these, not say "by analogy"):**
> - **codex** (`codex.py:118` sets `turn_id` method-local in `run_turn`): thread that local; command events have `item` in scope (`item.get("id")`); model_text has no item id → synthesize `f"{turn_id}:text"`.
> - **agent_sdk** (`agent_sdk.py:296/299`): `TextBlock`/`ThinkingBlock` have **NO `.id`** (only `ToolUseBlock.id` exists, :311) → text/thinking item_id MUST be synthesized `f"{turn_id}:text"` / `f"{turn_id}:thinking"`; tool blocks keep `block.id`.
> - **ACP** (`gemini_acp.py`/`cursor_acp.py`/`grok_acp.py`): the `normalize_session_update` mapper is **module-level with no turn_id in scope** — inject `turn_id = str(self.active_prompt_id)` at the `on_event` call site (the engine sets `self.active_prompt_id` per prompt, e.g. `gemini_acp.py:95`); tool_call already carries `tool_call_id` (→ item_id), text/thinking synthesize as above.
> - **Grok preserve-vs-normalize decision (panel P1):** grok currently maps `agent_thought_chunk → model_text` with a `[thinking]` text prefix, NOT `model_thinking`. **Normalize:** emit it as `kind="model_thinking"` (drop the `[thinking]` prefix) so the renderer's collapsible-thinking treatment is uniform across engines. Document this as the one behavior change in T-0 (otherwise additive).
> - **Fallback-key rule** (engines lacking item ids): coalesce by `(task_id, turn_id, kind)`, split on `kind` change.

- [ ] **Step 1: Failing test** — assert each engine's emit for model_text/command carries `turn_id`, `item_id`, `seq` (monotonic), `kind`. For **codex**: drive the real `run_turn`/notification mapper at **codex.py:139-172** (NOT a non-existent helper — panel P1: reference the actual emit site) with synthetic app-server JSON-RPC params (`item/agentMessage/delta` with `turnId`/`itemId`; `commandExecution`); assert the emitted dict has the 4 keys. For agent_sdk: drive its block mapper (agent_sdk.py:294-330) with a TextBlock/ThinkingBlock/ToolUseBlock; assert ids+seq (text/thinking ids are SYNTHESIZED, not block.id). For grok: assert `agent_thought_chunk` emits `kind="model_thinking"`.
- [ ] **Step 2: Run → FAIL** (keys absent).
- [ ] **Step 3: Implement** — thread the fields through each emit. codex (codex.py:144): `on_event("model_text", {"delta": delta, "turn_id": turn_id, "item_id": params.get("itemId") or f"{turn_id}:text", "kind": "model_text", "seq": next_seq()})`; same shape for command_started/finished (item id from `item.get("id")`/itemId) and command_output (already has item_id — add turn_id/kind/seq). agent_sdk: add `turn_id`/`item_id` (`f"{turn_id}:text"`/`f"{turn_id}:thinking"` for text/thinking — they have no `.id`; `block.id` only for tool) + `seq` to its emits (it already has tool_call_id+kind). ACP engines: `turn_id = str(self.active_prompt_id)` at the on_event site; synthesize text/thinking ids; tool_call keeps tool_call_id; grok's `agent_thought_chunk` → `kind="model_thinking"`. Provide one monotonic `next_seq()` per engine-instance (NOT reset per turn). Document the fallback-key rule (key by `(task_id, turn_id, kind)`, split on kind change).
- [ ] **Step 4: Run → PASS** (+ existing engine tests stay green — the additions are purely additive to `data`).
- [ ] **Step 5: Commit** `feat(engines): normalize progress on_event schema (turn_id/item_id/kind/seq) for transcript reassembly`.

---

### Task T-1: Bridge hot-path capture (strictly non-blocking) + backpressure deny-proof

**Files:** Modify `src/agent_redis_bridge/bridge.py` (`handle_progress` ~1535-1556; `__init__` add the queue; `process_request` enqueue a turn_end marker before the finally ~857). **Also** extend the T-0 schema to the remaining LIVE emitters the T-0 set missed (T-0-gate I1): `src/agent_redis_bridge/engines/pi_sdk.py` (~285-313, mirror agent_sdk: synthesize `f"{turn_id}:text"`/`:thinking`, real id for tools, per-instance `seq`) and `src/agent_redis_bridge/engines/agy_print.py` (~96, Tier-2: its single compute-then-print result → ONE `model_text` item with a synthesized `f"{turn_id}:text"` id + seq). Test: `tests/test_transcript_hotpath.py` (create); extend `tests/test_engine_progress_schema.py` for pi_sdk + agy_print.

> **`_capture` must be DEFENSIVE (T-0-gate I1 — structural guard, not a per-engine patch):** capture grabs `model_text`/`model_thinking`/`command_*` from WHATEVER engine emits them — and `pi_rpc.py` and any future engine may still emit schema-less. So `_capture` must NEVER assume the T-0 fields are present: read them via `.get()` with fallbacks — `kind` defaults to the `event` arg (always present); `turn_id` defaults to the task_id; `item_id` defaults to `f"{turn_id or task_id}:{kind}"`; `seq` defaults to a bridge-side monotonic counter. A schema-less event must still produce a coalesce-able item (coarser, never crashing, never un-keyable). Test this: `test_capture_defaults_schemaless_event` feeds a bare `{"delta":"x"}` and asserts a well-formed item with all keys populated from fallbacks.

**Interfaces:** `Bridge._transcript_q: queue.Queue` (bounded, e.g. maxsize=10000) + `Bridge._transcript_truncated: int` (init **0 in `__init__`** — panel: don't rely on first-touch). `handle_progress` enqueues `{"task_id", "run_id", "seat_id", "orchestrator", "event": kind, "data": data}` via `put_nowait` for capturable kinds (model_text, model_thinking, command_started, command_finished, command_output) — including model_text/thinking which currently `return` early.

> **Capture is ADDITIVE and separate from the existing `push_task_event` (panel P1 — codex):** the command_* events already flow through `push_task_event` today (bridge.py:1570) for status/milestone streaming; that pre-existing path is unchanged and out of scope. Transcript capture adds a SECOND, non-blocking copy via `put_nowait`. The deny-proof below must cover EVERY captured kind (model_text, model_thinking, **command_output** — the high-volume one codex flagged), not just model_text, since `_capture` runs for all of them on the hot path.

- [ ] **Step 1: Failing tests**
  - `test_handle_progress_enqueues_model_text`: a Bridge with a fake queue; `handle_progress(req,"model_text",{"delta":"hi",...})` → an item is on the queue (today it returns without enqueue).
  - **Backpressure deny-proof** `test_hotpath_does_not_block_on_wedged_consumer`: set `_transcript_q` to a `queue.Queue(maxsize=1)`, fill it; call `handle_progress(...)` **for EACH capturable kind (model_text, model_thinking, command_output)** and assert each returns immediately (no exception escapes, no block) and increments `_transcript_truncated` — proving a wedged flusher cannot stall the hot path on any kind. (Pre-seed `_last_stream_heartbeat[req.id]=time.monotonic()` so the 8s heartbeat doesn't fire and confound the timing.)
  - `test_capture_is_io_free`: call **`bridge._capture(req, "model_text", {...})` in ISOLATION** (NOT the whole `handle_progress` — panel P1: handle_progress's model_text branch legitimately does the 8s heartbeat HSET, which fires on the first delta since `_last_stream_heartbeat.get(id,0.0)==0.0`; a redis-raise-on-touch test against handle_progress would red on the heartbeat, not capture, and pressure deleting the legitimate heartbeat). Monkeypatch the redis clients to raise if touched; assert `_capture` only enqueues — no redis, no redaction, no apply_patch parse.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — in `__init__`: `self._transcript_q = queue.Queue(maxsize=int(os.environ.get("ARB_TRANSCRIPT_QMAX","10000")))` and `self._transcript_truncated = 0`. Add a helper `self._capture(request, event, data)` that builds the item dict (reading only in-memory attrs) and `try: self._transcript_q.put_nowait(item) except queue.Full: self._transcript_truncated += 1` — and NOTHING else (this is the unit the io-free test pins). In `handle_progress`: the model_text/thinking branch calls `self._capture(...)` FIRST, then the 8s heartbeat, then `return` (heartbeat preserved); the command_* branch calls `self._capture(...)` alongside its existing `push_task_event`. In `process_request` before the finally cleanup, `self._capture(envelope, "turn_end", {})`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(transcript): non-blocking hot-path capture to a bounded queue (backpressure deny-proof)`.

---

### Task T-2: Flusher daemon + redaction + live tee + kill-switch

**Files:** Create `src/agent_redis_bridge/transcript_flusher.py` (the daemon + redaction + reassembly + live XADD), `src/agent_redis_bridge/redact.py` (the `redact(text)->text` + corpus). Modify `bridge.py` (`resolve_trace_redis(env)` mirroring `resolve_eval_redis`; start the flusher thread in `__init__` if `ARB_TRANSCRIPT_CAPTURE` on). Test: `tests/test_transcript_flusher.py`, `tests/test_redact.py`.

**Interfaces:** `resolve_trace_redis(env) -> (url, prefix)` (ARB_TRACE_REDIS_URL + ARB_TRACE_PREFIX). `redact(text:str)->str`. `TranscriptFlusher(queue, trace_redis, prefix, *, redactor)` with `.run()` (drain loop) + `.flush_pending()`. Live stream key `f"{prefix}arbmem:trace"`, entries `{run_id,task_id,seat_id,orchestrator,turn_index,item_id,seq,kind,tool_name,content,ts}` (content REDACTED).

- [ ] **Step 1: Failing tests**
  - `test_redact_scrubs_corpus` (in test_redact): a corpus of secrets (`AWS_SECRET=AKIA…`, `Bearer eyJ…`, `export TOKEN=…`, a PEM block, a 64-char hex) → all replaced with `‹redacted›`; non-secret prose untouched. (Deny-proof: each corpus line must be scrubbed.)
  - `test_flusher_coalesces_and_redacts`: feed the queue a few model_text deltas (same turn_id/item_id) + a planted secret delta + a turn_end; run the flusher against a fake trace redis; assert ONE coalesced live entry per item with redacted content, correct kind/ids.
  - **`test_turn_end_is_a_flush_boundary` (T-0-gate M1):** agent_sdk's `turn_id` is the stable session_id, so `f"{turn_id}:text"` item_ids REPEAT across turns — coalescing on item_id alone would merge two distinct turns' text into one row. Feed: turn-A text deltas → `turn_end` → turn-B text deltas (SAME item_id) → `turn_end`; assert TWO separate coalesced items (the `turn_end` marker forces a flush of pending items, so turn B starts fresh). The flush key is effectively `(task_id, turn_epoch, item_id)` where turn_end advances the epoch.
  - `test_flusher_capture_off_is_noop`: with `ARB_TRANSCRIPT_CAPTURE=off`, the flusher isn't started / drops items (no trace writes).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `redact.py`: a list of compiled regex (KEY=val, Bearer, AWS, PEM, high-entropy) → replace with `‹redacted›`. `transcript_flusher.py`: a loop `while not stop: item = q.get()`; coalesce deltas by `(task_id,item_id)` until the item's terminal/turn boundary; on flush, `redact(content)` → `trace_redis.xadd(stream, entry, maxlen=N, approximate=True)`. **NOTE (panel P1 — codex): `xadd` takes NO `ttl` kwarg** (the RedisCli/XADD bug that bit the eval + visibility E2Es). Bound the live stream with `maxlen` only; ephemerality comes from `maxlen` + the durable store being the record. If a wall-clock TTL on the stream key is wanted, issue a SEPARATE `EXPIRE {stream}` call after the xadd — never pass ttl to xadd. Per-content cap (truncate >256KB + marker). Fail-soft (try/except per item). `bridge.py`: `resolve_trace_redis` (mirror resolve_eval_redis), build `self.trace_redis` if url + `ARB_TRANSCRIPT_CAPTURE!=off`, start `TranscriptFlusher(...).run()` on a daemon thread.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(transcript): flusher daemon (off-hot-path) with redaction + live tee + capture kill-switch`.

---

### Task T-3: `transcript_io` store + consumer + retention + grants

**Files:** Modify `src/arb_memory/schema.sql` (`transcript_io` table + idempotent ALTER + a retention index), create `src/arb_memory/transcript.py` (`TranscriptConsumer` + `PostgresTranscriptSink` + `apply_patch`→diff parse + a `purge_expired` retention fn), modify `src/arb_memory/run.py` (`transcript` + maybe `transcript-purge` commands), `src/arb_memory/mcp/grants.py` (REVOKE transcript_io from the door role). Test: `tests/arb_memory/test_transcript_consumer.py`, extend `test_eval_grants.py` for the transcript REVOKE deny-proof.

**Interfaces:** `transcript_io` columns per design v2. `TranscriptConsumer(redis, conn_factory, *, prefix)` mirroring `EvalConsumer` (`.step()`/`.start()`). `purge_expired(conn, older_than_days)`.

- [ ] **Step 1: Failing tests** — round-trip: xadd a trace entry → consumer drains → row in `transcript_io` (sentinel content). `apply_patch` parse: a command entry with an apply_patch command → meta has `{file, added, removed}`. retention: rows older than N days are purged. **Grant deny-proof (panel P2 — cold-Opus):** the door (`mcp_role_name()`) role gets a **real failed `SELECT ... FROM transcript_io`** (catch `psycopg.errors.InsufficientPrivilege`), NOT a `has_table_privilege` bit-check — mirror `test_eval_grants.py:98` ("privilege-checks alone are vacuous"). Idempotency: re-draining the SAME stream entry id inserts no duplicate row.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — schema: `CREATE TABLE IF NOT EXISTS transcript_io (...)` with a **`stream_entry_id TEXT UNIQUE`** column (panel P2 — codex: needed for the ON CONFLICT idempotency mirror; the consumer writes the Redis stream entry id into it) + `ALTER ... ADD COLUMN IF NOT EXISTS` for any later columns + a `(ts)` index for purge. `transcript.py`: mirror `eval.py`'s consumer/sink (INSERT incl. content; `ON CONFLICT (stream_entry_id) DO NOTHING`; deadletter malformed); `_parse_apply_patch(command)->meta`; `purge_expired(conn, older_than_days)` — **delete in bounded batches** (e.g. `DELETE ... WHERE ctid IN (SELECT ctid ... LIMIT 10000)` in a loop, panel P2 — agy: avoid one long table-lock on a large purge). `grants.py`: add `transcript_io` to the door REVOKE block + the consumer GRANT. `run.py`: `transcript` service (run the consumer) + a `transcript-purge` one-shot.
- [ ] **Step 4: Apply schema via psycopg + run → PASS.**
- [ ] **Step 5: Commit** `feat(transcript): transcript_io store + consumer + retention purge + door grant REVOKE (deny-proof)`.

---

### Task T-4: Render — visibility seat-detail transcript (web + tui)

**Files:** Modify `src/arb_memory/visibility.py` (`/sse/seat/{task_id}`: add transcript backfill from `transcript_io` + live-tail of `ARB_TRACE_REDIS_URL`), `src/arb_memory/static/app.js` + `src/arb_memory/watch/app.py` (render the new kinds). Test: extend `test_visibility_seat.py`; the web node-contract + the tui smoke for the new kinds.

**Interfaces:** `/sse/seat` emits transcript events (`kind` ∈ model_text/thinking/tool_call/tool_output) interleaved with the existing eval/audit timeline, ordered by ts. The renderers show: model text (streamed), thinking (collapsible), `apply_patch`→"edited `foo.py` +N/−M" (expandable diff from meta), bash command+output, `‹redacted›` spans.

- [ ] **Step 1: Failing tests** — `_backfill_seat` (or a new `_backfill_transcript`) returns transcript_io rows for a task_id merged by ts; the SSE seat stream includes transcript kinds; the web reducer (node) + tui render handle the new kinds without error.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — visibility: add a transcript backfill query (transcript_io by task_id ORDER BY ts) merged into `/sse/seat`. **Two-stream live tail — concurrent fan-in, NOT sequential (panel P1 — codex):** the existing `/sse/seat` tails ONE stream in one blocking loop; adding the trace stream "alongside" with a second sequential `XREAD` would let one stream starve the other (a blocking read on the quiet stream stalls the busy one). Instead run **two async reader tasks** (one per stream: events:live + `ARB_TRACE_REDIS_URL` trace, each its own per-request aioredis, both filtered to task_id) feeding **one outbound `asyncio.Queue`** that the SSE response drains; cancel both reader tasks cleanly on client disconnect (`finally` → `task.cancel()` + await). app.js + watch render the new kinds (apply_patch meta → diff view; redacted display).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(transcript): visibility seat-detail renders the full transcript (web + tui)`.

---

### E2E (the empirical gate)
`tests/e2e_transcript_roundtrip.py`: drive a REAL flusher path — enqueue model_text deltas + a bash command_output + an apply_patch command (+ a planted secret) for one task → flusher → trace stream → TranscriptConsumer → `transcript_io`; assert: content captured + coalesced; the secret is `‹redacted›` in BOTH the live entry and the row; apply_patch meta parsed (file/+/−); AND the backpressure guard (a wedged flusher does not slow `handle_progress`). 3× isolated, 0 residue.

## Self-Review
Spec coverage: T-0 normalization (the reassembly prereq); T-1 non-blocking hot path + backpressure deny-proof; T-2 flusher+redaction+live-tee+kill-switch; T-3 transcript_io+consumer+retention+grant-REVOKE; T-4 render; E2E. All design-v2 decisions covered (non-blocking, distinct table, separate trace client, redaction-in-flusher-default-on-kill-switch, off-hot-path apply_patch, per-content cap, non-load-bearing telemetry). Type consistency: the trace entry field set is identical across T-1 capture → T-2 flusher → T-3 consumer → T-4 render; `resolve_trace_redis`/`TranscriptConsumer` mirror the eval equivalents.

### Plan-panel folds (2026-06-25, codex NEEDS-CHANGES + agy/cold-Opus APPROVE-WITH-NITS — all folded)
- **T-0 [P1]** Per-engine `turn_id`/`item_id` sources NAMED (codex method-local; agent_sdk text/thinking synthesize — no `.id`; ACP `turn_id=str(self.active_prompt_id)`); Grok `agent_thought_chunk` NORMALIZED to `kind="model_thinking"` (the one behavior change); codex test references the real emit site (codex.py:139-172), not a non-existent helper; `seq` monotonic per engine-instance (NOT reset per turn).
- **T-1 [P1]** Backpressure deny-proof now covers EVERY captured kind incl. command_output (codex's high-volume flag); the io-free test pins `_capture` in ISOLATION (cold-Opus: handle_progress legitimately HSETs the 8s heartbeat on the first delta — testing the whole fn reds on the heartbeat, not capture); `_transcript_truncated` init 0 in `__init__`; capture is additive to the pre-existing `push_task_event`.
- **T-2 [P1]** `xadd` takes NO `ttl` kwarg (the RedisCli/XADD bug) — bound with `maxlen`, separate `EXPIRE` if a wall-clock TTL is wanted.
- **T-3 [P2]** `stream_entry_id TEXT UNIQUE` column for the ON CONFLICT idempotency mirror; grant deny-proof is a real failed SELECT (InsufficientPrivilege), not a `has_table_privilege` bit; `purge_expired` deletes in bounded batches (no long table-lock).
- **T-4 [P1]** Two-stream live tail is concurrent fan-in (two async reader tasks → one `asyncio.Queue` → SSE, clean cancel on disconnect), NOT sequential XREADs (one stream would starve the other).

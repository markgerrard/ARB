# SPEC — Item 2: memory write-result signal (builds ON the shared prep slice)

**Status:** SPEC **v2** (spec panel₀ folded; confirm pending) · **Design:**
`docs/superpowers/specs/2026-07-12-memory-write-result-signal-design.md` **v5** (mechanism/why
authoritative there) · **Prereq:** `2026-07-13-shared-prep-slice-SPEC.md` merged (receipt contract +
`_publish_result` hook) · **Author:** warm-Opus (inline).

Adds the result channel + await onto the prep slice's `handle_write_intent` receipt.

**Spec panel₀ folded:** the persisted 4-field receipt vs the **response envelope** (which adds
`ulid`/`duplicate`/`timed_out`) are now distinct (4-seat consensus); the result-publish rides prep's
**`_publish_result` hook** so it covers *all* Item 1 terminal dispositions (no poison-exhaust hang); the
async redis client's construction site (`build_writer_app` + `run.py`) is pinned (sol/cold-Opus P2); the
delayed-receipt e2e injection point is pinned (pi-GLM P2).

## Receipt vs response envelope (SP-1 — pin one shape end-to-end)
- **Persisted receipt** (`idempotency_keys.receipt`, published to `write_result_key`, returned by
  `handle_write_intent`) = **4 fields**: `{artefact_outcome, artefact_id, version, hints_stored}`. Identical
  on first-write and replay.
- **Channel envelope** (`write_result_key` payload, published by `WriteLoop._publish_result`) = the
  4-field receipt **plus `duplicate: is_replay`** (set from `handle_write_intent`'s out-of-band replay
  flag — the writer can't derive it after `blpop`, so it must be in the payload).
- **Response envelope** (writer proxy HTTP response → MCP tool return) = the channel envelope **plus
  `ulid`** (added by the proxy, which holds it from `memory_write`'s return), and on await timeout
  `{artefact_outcome:"unknown", timed_out:true, ulid}`.

## Deliverables

### 1. Result channel (`bus.py`)
- `write_result_key(request_id, prefix) -> f"{prefix}arbmem:write_result:{request_id}"` (mirror
  `close.py`'s `close_result`).
- `bus.memory_write(..., request_id=None)`: when set, add `request_id` as a **top-level stream field**
  (not inside `payload`); `WriteLoop`'s parse carries entry-level fields alongside the parsed payload
  (incl. the malformed-deadletter path).
- **Publish via prep's `_publish_result` hook, NOT a bespoke `_handle_entry` edit** (SP-2): Item 2
  overrides `WriteLoop._publish_result(entry_id, fields, receipt_or_none, is_replay)` — iff the entry
  carried a `request_id`, `lpush` the **channel envelope** `{**receipt_4field, "duplicate": is_replay}` to
  `write_result_key` + `expire(TTL ≥ WRITE_AWAIT_CAP_S)`. `duplicate` lives in the *transient channel
  envelope* (from `is_replay`, the only replay signal), never in the persisted 4-field receipt. Because Item 1 calls the
  hook on **every** terminal disposition, this automatically covers success/dedup/replay **and Item 1's
  poison-exhaustion deadletter + row-unstorable ack** → those publish `{artefact_outcome:"failed",
  reason:"<generic-code>"}` (generic code, not raw exception), so a poison-exhausted *awaited* write
  returns immediately instead of hanging to the 30 s cap.
- Publish is **post-commit**. Duplicate/replay path **re-publishes the stored (4-field) receipt
  post-commit** with `duplicate:true` in the channel envelope (from `is_replay`), never in the stored
  4-field receipt.

### 2. Replay receipt (`schema.sql`, `bus.py`)
- `ALTER TABLE idempotency_keys ADD COLUMN IF NOT EXISTS receipt jsonb` (in `schema.sql`).
- `handle_write_intent`: persist the receipt in the `idempotency_keys` row **inside the same
  `with conn.transaction()`** as the claim + write (all SQL inside the txn — no read before it opens).
- On duplicate ULID: return the *stored* receipt (never a racy "latest"); legacy `NULL` receipt →
  `{artefact_outcome:"unknown"}`.

### 3. Writer proxy await — async redis, server-minted id (`writer.py`)
- `/publish` gains an optional `await` flag + optional caller timeout, **hard-capped server-side at
  `WRITE_AWAIT_CAP_S = 30`**.
- The proxy **mints** `request_id = uuid.uuid4().hex` itself and **rejects any client-supplied id** (length
  cap as defense-in-depth).
- When `await`: `xadd` via the sync client (loop, non-blocking), then `blpop` via a **`redis.asyncio.Redis`
  client on the event loop — NO threadpool**. Returns the **response envelope** (4-field receipt + `ulid`
  + `duplicate:bool`); on timeout `{artefact_outcome:"unknown", timed_out:true, ulid}`. Non-`await` =
  today's `xadd`-only latency.
- **Async-client construction (sol/cold-Opus P2):** `build_writer_app(sync_client, *, async_redis_client=None)`
  takes the async client (or a factory) **injectably**; `run.py` (owns the Redis URL/client construction,
  run.py:265-270) builds it for prod and passes it; tests inject a fake. Created once, `aclose()`d on
  Starlette shutdown (lifespan). Its `socket_timeout` must NOT abort a `WRITE_AWAIT_CAP_S` wait (mirror the
  writer client's no-`socket_timeout`, or > cap). **`run.py` is in the deliverables.**

### 4. MCP tools + wrappers (`mcp/tools.py`, `mcp/server.py`)
- `memory_store`/`memory_remember` gain optional `await_result`, threaded through **both** the
  `MemoryTools` methods **and** the `server.py:371` public wrappers.
- When set, `_publish` posts `await` to the proxy **with a per-request HTTP timeout ≈ 35 s** (> the 30 s
  cap; overrides the client's fixed 10 s default at `mcp/server.py:273`) and returns the response envelope
  `{accepted, ulid, artefact_outcome, artefact_id, version, hints_stored, duplicate}` (or
  `{accepted, ulid, artefact_outcome:"unknown", timed_out}`). Unset → unchanged
  **`{accepted, ulid, artefact_id}`** (preserve today's `artefact_id` — codex-terra P2, tools.py:171). MCP
  never touches Redis. (`duplicate:true` when the write was an idempotency replay.)

### Config
`WRITE_AWAIT_CAP_S=30` (owned here, in `writer.py`); result-key TTL `≥ WRITE_AWAIT_CAP_S`; MCP await HTTP
timeout `≈35`.

## Acceptance criteria
- A caller with `await_result` learns `stored`/`deduped`/`duplicate`/`failed`/`unknown` + version +
  `hints_stored`; the "artefact deduped, hint stored" case reports `deduped` + `hints_stored≥1`.
- Absent `request_id`/`await` → byte-for-byte today's fire-and-forget (no result key written).
- Server-minted single-use id → no cross-request receipt leakage.
- A 10–30 s receipt does NOT falsely report "item NOT stored" (httpx timeout > cap).
- One awaiting request does not stall a concurrent publish (async blpop).
- `uv run --extra arb-memory pytest tests/arb_memory` green.

## Tests
- `upsert_hint` insert vs dedup flag; receipt `hints_stored` incl. artefact-deduped-but-hint-stored (deny-proof).
- WriteLoop (drives `run()`): `request_id` → correct receipt on channel; absent → no key; **replay** →
  stored receipt re-published, intervening newer version doesn't change it; deadletter → `failed` result.
- Writer proxy: `await` returns receipt; **concurrent await + publish stays responsive**; timeout →
  `unknown/timed_out`; server rejects client-supplied `request_id`.
- **Delayed-receipt e2e (proves the httpx-timeout fix):** simulate consumer lag by **delaying the
  `_publish_result` publish** (inject a small sleep / gate the fake WriteLoop's publish, or hold the
  result key) so the proxy `blpop` waits > 10 s (the old httpx default) but < the ~35 s HTTP timeout —
  assert the tool returns the receipt, NOT a false "item NOT stored". No real 30 s wall-wait needed
  (pi-GLM P2).
- Migration: `ADD COLUMN IF NOT EXISTS` idempotent fresh+existing; legacy `NULL` receipt → `unknown`.

## Deploy
MCP + writer + consumers one image **incl. the `idempotency_keys.receipt` migration** — **paused for
Mark's deploy-review gate.**

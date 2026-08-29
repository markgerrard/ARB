# PLAN — Item 2 memory write-result signal (build ON prep; TDD, luna@high)

**Spec:** `docs/superpowers/specs/2026-07-13-item2-write-result-SPEC.md` v2 · **Design (mechanism):**
`2026-07-12-memory-write-result-signal-design.md` v5 · **Effort:** high · **Prereq:** `feat/prep-slice`
merged to `dev`. **Worktree:** `--worktree item2`. **Branch:** `feat/item2-write-result`.
**Env:** `uv run --extra arb-memory pytest tests/arb_memory`.

## Ordering (each step: RED test → GREEN impl)

1. **Result channel + `_publish_result` override** (`bus.py`) — NO stored-receipt/replay-recovery here
   (that needs step 2's persistence — plan panel P1).
   - RED (drives `run()`): a write carrying `request_id` → `write_result_key` has the channel envelope
     `{**receipt_4field, duplicate: is_replay}`; a write WITHOUT `request_id` → no key (backward compat);
     `duplicate:true` is set from `is_replay` on a replay; a malformed/deadletter terminal →
     `{artefact_outcome:"failed", reason:<code>}`.
   - GREEN: `write_result_key(...)`; `bus.memory_write(..., request_id=None)` adds it as a top-level stream
     field; **read `request_id` from the `fields` already handed to `_publish_result`** (do NOT re-edit
     `_parse_intent` — that's Item 1's territory; plan panel P2); override `_publish_result` to lpush the
     envelope + `expire(TTL ≥ WRITE_AWAIT_CAP_S)`.
2. **Receipt persistence + migration + replay recovery** (`schema.sql`, `bus.py`).
   - RED: `ADD COLUMN IF NOT EXISTS receipt jsonb` idempotent fresh+existing; on duplicate ULID the
     **stored** receipt is returned/re-published and an **intervening newer version does NOT change it**
     (moved here from step 1 — plan panel P1); legacy `NULL` receipt → `{artefact_outcome:"unknown"}`.
   - GREEN: persist the 4-field receipt inside `handle_write_intent`'s single `with conn.transaction()`
     (claim + write + receipt, no read before txn opens); replay reads the stored receipt.
3. **Writer proxy async await + server-minted id** (`writer.py`, `run.py`).
   - RED: `await` returns the response envelope `{**receipt, ulid, duplicate}`; a **concurrent** publish
     stays responsive while one request awaits (async blpop, no threadpool); server **rejects** a
     client-supplied `request_id`; a **requested shorter timeout is honored** and an **oversized timeout is
     clamped to 30 s** (plan panel P1); timeout → `{artefact_outcome:"unknown", timed_out, ulid}`.
   - GREEN: `/publish` mints `uuid4` request_id + honors caller timeout hard-capped at `WRITE_AWAIT_CAP_S=30`;
     `blpop` via `redis.asyncio.Redis` on the loop; `build_writer_app(sync_client, *, async_redis_client=…)`
     injectable, built in `run.py`, `aclose()` on shutdown.
   - **Deny-proof:** allow a client-supplied `request_id` through → the reject test reds (server-minted-id
     guard is real; plan panel P2).
4. **MCP tools + wrappers** (`mcp/tools.py`, `mcp/server.py`).
   - RED (registration-level, through `server.py:371` wrappers): `memory_store(await_result=…)` returns
     `{accepted, ulid, artefact_outcome, artefact_id, version, hints_stored, duplicate}` via the proxy
     (no Redis in MCP); unset → `{accepted, ulid, artefact_id}` (today's shape preserved).
   - GREEN: thread `await_result` through both `MemoryTools` methods AND the public wrappers; `_publish`
     posts `await` with a per-request HTTP timeout ≈ 35 s (> the 30 s cap; overrides the 10 s default at
     `mcp/server.py:273`).
5. **Delayed-receipt e2e** — simulate lag by delaying the `_publish_result` publish so `blpop` waits
   > 10 s but < ~35 s → assert the tool returns the receipt, NOT a false "item NOT stored" (no real 30 s
   wall-wait).

## Deny-proof
Remove the `deduped` branch label → the byte-identical-rewrite test reds (signal is real, not always
"stored"). Remove the httpx timeout override → the delayed-receipt e2e reds (false "NOT stored").

## Evidence contract (paste — per-step, plan panel P1)
For EACH step: the exact targeted `pytest` command + its **failing (RED)** output before impl and
**passing (GREEN)** output after. For each deny-proof: the **RED with the guard removed** and the restored
**GREEN**. Finish with the full `pytest tests/arb_memory` (green + counts) and the branch SHA. Config:
`WRITE_AWAIT_CAP_S=30`, result-key TTL ≥ 30, MCP await HTTP timeout ≈ 35.

## POST-MERGE integration step (orchestrator-owned merge gate — plan panel P1)
An explicit executable phase, NOT prose, run by the orchestrator after both branches build green:
1. Merge `feat/prep-slice` → `dev`; then `feat/item1-consumer-loop` → `dev`; then `feat/item2-write-result`
   → `dev` (this order — Item 1 owns `WriteLoop`'s terminal dispositions, Item 2 fills the `_publish_result`
   body).
2. Write the **cross-item RED** `tests/arb_memory/test_write_result_integration.py`: a `request_id`-carrying
   write whose handler hits Item 1's **poison-exhaustion** terminal must publish `{artefact_outcome:"failed"}`
   to `write_result_key` and the awaited proxy returns it immediately (NOT a 30 s hang). Also assert the
   awaited path over a **row-unstorable** terminal.
3. Make it GREEN (both items' code already merged); run the full targeted suite.
4. **Merge gate evidence:** paste the integration test RED→GREEN, the full `pytest tests/arb_memory`, and
   the integration `dev` SHA. Deploy incl. the `idempotency_keys.receipt` migration is **paused for Mark's
   review**.

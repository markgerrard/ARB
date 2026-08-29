# Design — Memory write result: signal stored-vs-deduped over a result channel

**Status:** DESIGN **v5** (panel₀+r2+r3+r4 folded; r5 confirm pending) · **Filed:** `docs/BACKLOG.md §
"Memory write result: signal stored-vs-deduped"` (2026-07-12) · **Author:** warm-Opus orchestrator (inline).

## Round-4 fold log (v4 → v5)

Round-4 confirm: agy + cold-Opus `approve/none`, codex-terra `nc/P1`. One code-grounded P1 + P2 polish:

- **[R4-1 — codex-terra P1] The 30 s await cap is unreachable from MCP.** The MCP `httpx.AsyncClient`
  has a fixed **10 s** timeout (`mcp/server.py:273`), so a 10–30 s receipt makes the MCP→proxy HTTP call
  raise and `_publish` falsely reports "item NOT stored" (`tools.py:120`) though the write XADDed —
  breaking the loud-result contract. **v5: the `await_result` HTTP call uses a per-request timeout >
  `WRITE_AWAIT_CAP_S` (`30 s` cap + transport margin ⇒ ~35 s), overriding the 10 s default; a
  delayed-receipt (10–30 s) e2e test guards it.** (§6)
- **[R4-2 — cold-Opus + agy P2] Two lifecycle/consistency pins:** result-key `TTL ≥ WRITE_AWAIT_CAP_S`
  (so the receipt can't expire before the awaiter reads it); the async redis client is instantiated
  **once** at app level (`build_writer_app`) and closed on Starlette shutdown (not per-request). (§6)

**Round-4 confirmed sound (kept):** server-minted single-use request_id fully kills the correlation bug;
async redis client resolves the threadpool ceiling; two-client (sync xadd / async blpop) correct;
generic failed-reason; post-commit re-publish; migration additive.

## Round-3 fold log (v3 → v4)

Round-3: cold-Opus + pi-GLM `approve/P2`, codex-terra + agy `block/P1` — the two P1s **dissolve under two
v4 decisions**, and the design is otherwise ship-ready (cold-Opus verified every fold buildable):

- **[R3-1 — codex-terra P1] Result channels weren't exclusively correlated.** Accepting a client-supplied
  `request_id` + re-publishing on replay let two concurrent same-id requests, or a retry consuming a
  timed-out request's still-TTL'd receipt, get the **wrong** receipt (`BLPOP` has no correlation check).
  **v4: the writer proxy *mints* an unguessable single-use `uuid4` `request_id` server-side and rejects
  client-supplied ids.** Under a fresh uuid per request the collision is impossible (cold-Opus + pi-GLM
  confirmed the TTL-orphan case is then benign), and R2-7's validation reduces to a server-side length cap.
- **[R3-2 — agy P1; terra/cold-Opus/pi-GLM P2] Pin the await mechanism.** `run_in_threadpool` risks anyio's
  40-thread limiter under many concurrent awaits (agy); the others rate it safe-but-bounded. **v4 commits
  to an async redis client (`redis.asyncio.Redis`) for the `blpop`** — no threadpool, no ceiling; the sync
  client stays for the loop-side `xadd`, and the non-`await` publish path never blocks the loop. Resolves
  all three views.
- **[R3-3 — pi-GLM + agy P2] Fail-fast `error` is sensitive; genericize.** The dead-letter `{artefact_
  outcome:"failed", error}` receipt should carry a **generic reason code**, not raw exception text (which
  can leak internals) — PII/sensitive-field hygiene.
- **[R3-4 — pi-GLM P2] Pin two under-specified values:** the **server-side timeout cap** (concrete
  seconds) and that the **replay re-publish is post-commit** (consistent with §3's "lpush after PG
  commit"), even though the duplicate branch is read-only.
- **[R3-5 — cold-Opus/agy P2] Caller-sweep completeness** — the only production `upsert_hint` caller is
  `write_artefact_and_hints`; add any `write_artefact_and_hints`-return consumers to the sweep; read
  `request_id` from stream fields on the malformed-payload deadletter path too.

**Round-3 confirmed sound (kept, cold-Opus SHIP):** async offload keeps bulk writers unstarved, `upsert_hint
→ inserted` derivable from `ON CONFLICT … RETURNING`, the `ALTER … ADD COLUMN` migration matches the live
prod pattern, one-txn claim+write+receipt is atomic, replay double-publish benign.

## Round-2 fold log (v2 → v3)

Round-2 (same roster) blocked v2 — codex-terra/agy/pi-GLM `P1`, cold-Opus `nc/P1`. The panel **verified
every v1 fold holds** (transport executable, atomic replay receipt via savepoint, loud timeout, pinned
naming, str→dict safe) and found two new-surface P1s + P2s, all mechanical:

- **[R2-1 — 4-seat consensus: agy + codex-terra + cold-Opus + pi-GLM, P1] Writer-proxy `blpop` blocks the
  async event loop.** `/publish` is an `async def` Starlette handler over a **synchronous** redis client
  (writer.py:15); a blocking `blpop` stalls the single event loop — and the writer is the shared write
  ingress for every seat. cold-Opus's meta-point: the "mirror close_result" framing gave false confidence
  because close_result's awaiter is a sync *caller* (the CLI script), not an async *handler*. **v3: the
  await offloads the blocking `blpop` via `run_in_threadpool` (or a sync `def` route / async redis
  client), with a finite server-side timeout cap; a non-`await` publish stays responsive.**
- **[R2-2 — 3-seat consensus: agy + codex-terra + cold-Opus, P1] `hints_stored` is not derivable.**
  `upsert_hint` (store.py:86-112) returns the hint id in *both* the insert and dedup branches, so
  `write_artefact_and_hints` can't count genuinely-new hints — "the headline of the very fold F3 closes is
  unbuildable as written." **v3: `upsert_hint` returns `(hint_id, inserted: bool)`; `write_artefact_and_hints`
  counts `inserted`; the caller sweep (F7) enumerates upsert_hint's ~15-20 sites.**
- **[R2-3 — codex-terra + agy P2] Schema migration.** `schema.sql` uses `CREATE TABLE IF NOT EXISTS`, so
  the `receipt jsonb` column won't be added to existing DBs. **v3: `ALTER TABLE idempotency_keys ADD COLUMN
  IF NOT EXISTS receipt jsonb`; a replay reading a legacy `NULL` receipt returns `{artefact_outcome:"unknown"}`
  (not `None` downstream).**
- **[R2-4 — codex-terra P2] MCP public wrappers.** `mcp/server.py:371` exposes `memory_store`/`memory_remember`
  with explicit signatures that don't pass `await_result` — changing `MemoryTools` alone leaves the option
  unreachable. **v3: thread `await_result` through both server wrappers + a registration-level test.**
- **[R2-5 — agy P2] Fail-fast on non-success.** A dead-lettered or replay-after-crash write publishes no
  result → the awaiting proxy hangs to timeout. **v3: on deadletter publish `{artefact_outcome:"failed",
  error}`; on a duplicate/replay, re-publish the stored receipt to the result channel** (at-least-once to
  the waiting proxy).
- **[R2-6 — pi-GLM + cold-Opus P2] Txn scope + INSERT/UPDATE order.** The receipt `SELECT`/`UPDATE` must
  live **inside** `handle_write_intent`'s `with conn.transaction()` (bus.py:97) — a stray read before the
  txn opens demotes the claim to a savepoint on this non-autocommit conn. **v3 states the precondition
  explicitly.**
- **[R2-7 — pi-GLM P2] `request_id` validation at the proxy.** The trust boundary is `writer.py`'s
  `/publish` (reads `request.json()`). **v3: the proxy validates/length-caps `request_id`
  (`^[A-Za-z0-9_-]{1,64}$`), even for server-minted ids**, behind the door's OAuth + the proxy bearer.

**Round-2 confirmed sound (kept):** writer-owns-await architecture, atomic receipt-in-claim, component-
specific vocab, opt-in-off default, request_id/ulid split, mirror-not-extract, top-level request_id field.

## Panel₀ fold log (v1 → v2)

Panel₀ (codex-terra@high, cold-Opus, pi-GLM, agy-print) blocked v1 (P1). Forks the panel **confirmed**:
mirror-not-extract is correct (result shapes differ; only ~4 transport lines shared; extracting would
refactor freshly-deployed `close.py` for near-zero gain — codex + cold-Opus); `request_id`/`ulid` split
is clean; opt-in `await_result` is correct **once timeout is explicit**. Folded defects:

- **[F1 — codex-terra] Transport boundary: v1 was not executable.** `MemoryTools` has only an HTTP client
  + writer URL, **no Redis client** (`tools.py:120` `_publish` POSTs to the writer proxy); the writer
  (`writer.py`) is the sole Redis touchpoint. So v1's "MCP blpops the result" and "request_id in stream
  fields" (which `_parse_intent` discards, bus.py:181) could never work. **v2: the writer proxy owns the
  await; MCP relays over HTTP; `request_id` is a top-level stream field AND `WriteLoop` reads it from the
  entry fields** (mirroring how `close.py` reads `request_id`). [Resolves the codex-vs-cold-Opus conflict:
  top-level field, not inside `payload`, with the parse updated to carry it.]
- **[F2 — codex-terra] Replay must return the *original* receipt, not a racy "latest".** On a duplicate
  ULID, `idempotency_keys` stores only key+timestamp (schema.sql:174) — no receipt. v1's "report current
  latest (id,version)" can return a *later* version written by another request. **v2: persist the write
  receipt `{artefact_outcome, artefact_id, version, hints_stored}` atomically with the idempotency claim;
  replay that exact receipt on redelivery.**
- **[F3 — cold-Opus + codex + agy] Outcome vocab can't express "artefact deduped, hints stored."**
  `upsert_hint` dedups independently of `upsert_artefact` (store.py), so a write can dedup the artefact
  while inserting a new hint; a lone `outcome:"deduped"` says "nothing changed" when something did. The 2
  shipped MCP tools don't hit it, but the raw bus API does → latent P1. **v2: component-specific receipt
  `{artefact_outcome: stored|deduped|none, artefact_id, version, hints_stored: <int>}`.**
- **[F4 — codex + cold-Opus + agy] Timeout must fail loud, not silently degrade.** v1 dropped the
  `outcome` key on `await_result` timeout — recreating the exact "poll for a version that never appears"
  bug. **v2: on timeout return `{artefact_outcome:"unknown", timed_out:true, request_id}`.** (Terminal
  dead-letter is a follow-up: surface it as a result too — noted, not in scope.)
- **[F5 — cold-Opus] Field-name consistency.** v1's §1 said `artefact_outcome`, §2/§4 said bare
  `outcome`. **v2 pins `artefact_outcome` end-to-end.**
- **[F6 — pi-GLM] `request_id` origin/shape.** **v2: server-minted `uuid.uuid4().hex`; if ever accepted
  from a client body, validated to `^[A-Za-z0-9_-]{1,64}$` and length-bounded** (else a 10 MB request_id
  becomes a 10 MB Redis key). Note: hardens `close.py`'s identical pattern as a follow-up.
- **[F7 — cold-Opus + codex] Caller sweep is bigger than "two."** `upsert_artefact`'s tuple→receipt
  change touches ~40 test unpack sites (production-safe: only `write_artefact_and_hints` unpacks it);
  `handle_write_intent`'s `str` return change breaks `test_bus_write.py:42-43`. **v2: the plan enumerates
  every caller/test** (grep `upsert_artefact`, `handle_write_intent`, `write_artefact_and_hints`).
- **[F-joint — cold-Opus] Item-1 collision is a real semantic conflict** on `handle_write_intent`'s return
  contract, not just a rebase. **v2: a pinned joint bus.py contract, landed in a shared prep slice** (see
  §7).

## Problem (unchanged framing)

The memory write path is fully async and gives the caller no signal distinguishing **stored-as-vN+1** from
**deduped-against-vN**. `store.upsert_artefact` (store.py:20-69) dedups against the latest version;
both branches return the same `(artefact_id, version)`. `bus.memory_write` → `arbmem:writes` →
`WriteLoop` → `handle_write_intent` → `write_artefact_and_hints` is fire-and-forget; MCP tools
(`tools.py:135,173`) return `{accepted, ulid}`; the publish-proxy (`writer.py`) returns bare `{ulid}`.
Cost (2026-07-12): a peer lost an afternoon polling for a "v3" that never came — its republish deduped
against v2.

## Design v5

### 1. `upsert_artefact` → structured receipt
Return `(artefact_id, version, artefact_outcome)`, `artefact_outcome ∈ {stored, deduped}` — `deduped` on
the latest-hash-match branch (store.py:41-42), `stored` on insert.

### 2. `upsert_hint` → insert flag; `write_artefact_and_hints` / `handle_write_intent` → the joint receipt (F3, F5, F-joint, R2-2)
`upsert_hint` (store.py:86-112) currently returns only the hint id in both branches, so insert-vs-dedup is
not observable. **Change it to return `(hint_id, inserted: bool)`** (`inserted` False on the
`ON CONFLICT`/dedup branch) and sweep its ~15-20 callers/tests (R2-2, F7). `write_artefact_and_hints` then
returns `{artefact_outcome, artefact_id, version, hints_stored}` where `hints_stored` = count of hints
with `inserted=True`. `handle_write_intent` returns that same receipt dict (replacing its
`"written"|"duplicate"` string — F7). A hints-only write:
`{artefact_outcome:"none", artefact_id:null, version:null, hints_stored:<n>}`. This lets a caller see
"artefact deduped, hint stored" (`artefact_outcome:"deduped", hints_stored:1`) — the F3 hole.

### 3. Result channel — mirror AC2's `close_result` (panel-confirmed)
`write_result_key(request_id, prefix) -> f"{prefix}arbmem:write_result:{request_id}"`. `WriteLoop` `lpush`es
the receipt + `expire(TTL)` after the PG commit, **iff** the entry carried a `request_id`. Post-commit
ordering (panel-confirmed no race). Mirror inline — do not extract close_result.

### 4. `request_id` transport — server-minted, single-use (F1, F6, R3-1)
`bus.memory_write(..., request_id=None)` adds `request_id` as a **top-level stream field** (not inside
`payload`). `WriteLoop` reads it from the entry fields (update `_parse_intent`/`read_one` to carry
entry-level fields alongside the parsed payload, incl. the malformed-deadletter path — R3-5). Absent
`request_id` → today's fire-and-forget, byte-for-byte unchanged. **The writer proxy mints the
`request_id` itself (`uuid.uuid4().hex`) for every `await` request and REJECTS any client-supplied id
(R3-1)** — an unguessable, single-use key means two requests can never collide on the same
`write_result_key` and a retry can't consume a prior request's TTL'd receipt. The proxy applies a length
cap as defense-in-depth; no client string reaches the Redis key.

### 5. Replay receipt (F2, R2-3, R2-5, R2-6)
Add `receipt jsonb` to `idempotency_keys` via **`ALTER TABLE idempotency_keys ADD COLUMN IF NOT EXISTS
receipt jsonb`** in `schema.sql` (R2-3 — `CREATE TABLE IF NOT EXISTS` won't add it to existing DBs).
Persist the receipt **inside the same `with conn.transaction()`** as the key claim + write
(`handle_write_intent`, bus.py:97-105) — the `INSERT` idempotency claim, the write, and the receipt
`UPDATE`/embed all inside one txn (R2-6: a read before the txn opens would demote the claim to a savepoint
on this non-autocommit conn). On a duplicate ULID: read the stored receipt and return *that* (never a racy
"latest"), **and re-publish it to `write_result_key`** so a proxy awaiting after a post-commit/pre-publish
crash still gets it (R2-5). A legacy `NULL` receipt (pre-migration key) → `{artefact_outcome:"unknown"}`,
never `None` downstream.

### 6. Await path — writer proxy owns it (async redis), MCP relays over HTTP (F1, F4, R2-1, R2-4, R2-5, R3-1, R3-2)
- **Writer proxy (`writer.py`)**: `/publish` accepts an optional `await` flag (+ optional caller timeout,
  **hard-capped server-side at `WRITE_AWAIT_CAP_S` = 30 s**). It **mints** the `request_id` server-side
  (§4, R3-1) — never from the client. When `await`: `xadd` the write (sync client, non-blocking on the
  loop), then **`blpop` via an async redis client (`redis.asyncio.Redis`), directly on the event loop —
  no threadpool** (R3-2: avoids anyio's 40-thread ceiling; the sync client stays for `xadd`). Returns the
  receipt in the HTTP response; on timeout `{artefact_outcome:"unknown", timed_out:true, request_id}` (F4).
  A non-`await` publish keeps today's latency (loop-side `xadd` only, never touches the async wait).
- **MCP tools + wrappers**: `memory_store`/`memory_remember` gain optional `await_result`, threaded
  through **both** the `MemoryTools` methods **and** the `mcp/server.py:371` public wrappers (R2-4). When
  set, `_publish` sends `await` to the proxy **with a per-request HTTP timeout > `WRITE_AWAIT_CAP_S`**
  (~35 s, overriding the client's fixed 10 s default at `mcp/server.py:273` — R4-1; else a 10–30 s receipt
  raises and `_publish` falsely reports "item NOT stored" though the write XADDed) and returns
  `{accepted, ulid, artefact_outcome, version, hints_stored}` (or `unknown/timed_out`); unset → unchanged
  `{accepted, ulid}`. **MCP never touches Redis.**
- **Lifecycle (R4-2):** the writer proxy's async redis client is created once in `build_writer_app` and
  closed on Starlette shutdown; `write_result_key`'s TTL is `≥ WRITE_AWAIT_CAP_S` so the receipt outlives
  the await window.
- **Fail-fast (R2-5, R3-3):** a **dead-lettered** write publishes `{artefact_outcome:"failed",
  reason:"<generic-code>"}` (a generic reason code, **not** raw exception text — R3-3) so the proxy
  returns immediately instead of hanging to timeout. The replay re-publish (§5) is **post-commit** (R3-4).
- Default: **opt-in off** (panel-confirmed — preserves bulk-writer latency).

### 7. Joint bus.py contract + shared prep slice (F-joint)
Both items rewrite `WriteLoop`/`handle_write_intent`. Land a **shared prep slice first**: (a) Item 1's
`consumer_loop.py` extraction + `WriteLoop` conn-factory, and (b) this item's `handle_write_intent`
structured-receipt return contract. Then Item 1 (retry/backoff/classify) and Item 2 (result channel +
receipt persistence + await) build on the shared base in parallel with minimal textual overlap. The
orchestrator integrates.

## Testing (TDD)
- **Unit** — `upsert_artefact` returns `stored` (first write, changed rewrite, A→B→A revert re-version),
  `deduped` (byte-identical rewrite of latest); `upsert_hint` returns `inserted=True` on a new hint,
  `False` on the dedup branch; `write_artefact_and_hints` receipt has correct `hints_stored` incl. the
  **artefact-deduped-but-hint-stored** case (F3/R2-2 deny-proof).
- **WriteLoop integration (drives `run()`)** — entry with `request_id` → `write_result` has the right
  receipt; without → no result key (backward compat); **replay** (same ULID redelivered) → returns *and
  re-publishes* the *stored* receipt, and an intervening newer version does NOT change it (F2/R2-5
  deny-proof); a **dead-lettered** write publishes `{artefact_outcome:"failed"}` (R2-5).
- **Writer proxy** — `await` returns the receipt; **a slow/awaiting request does not block a concurrent
  publish** (R2-1 — assert the second request returns while the first is still blocked); timeout →
  `{artefact_outcome:"unknown", timed_out}` (F4); no-`await` → `{ulid}` unchanged; oversized/invalid
  `request_id` rejected at the proxy (R2-7).
- **MCP tool + wrappers** — `memory_store(await_result=…)` returns the receipt via the proxy (no Redis in
  MCP), through the `server.py:371` public wrapper (R2-4 registration-level test); timeout degrades loud,
  never hangs.
- **Migration (R2-3)** — `ALTER … ADD COLUMN IF NOT EXISTS` is idempotent on a fresh and an existing DB; a
  replay against a legacy `NULL`-receipt key returns `{artefact_outcome:"unknown"}`, not `None`.
- **Caller sweep (F7, R2-2)** — `upsert_artefact` ~40 unpack sites + `upsert_hint` ~15-20 sites +
  `handle_write_intent` `test_bus_write.py:42-43` updated and green.

## Rollout / integration
- Ships in one image (MCP + writer + consumers). **Deploy paused for user review.**
- Shared prep slice lands before the parallel Item 1/Item 2 builds (§7). Result is written by the consumer
  *after* the PG commit — the Redis result channel is the signal, never a PG poll.

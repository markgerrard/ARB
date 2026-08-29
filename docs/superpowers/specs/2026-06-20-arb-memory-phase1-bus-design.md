# ARB Memory — Phase 1: bus transport (write-intents + read request/reply) [spec]

**Status:** SPEC (spec-panel folded; ready for the plan). Phase 1 of ARB Memory (Workflow B). Builds on
Phase 0 (`src/arb_memory/` store + write-library, on `feat/arb-memory`).
**Architecture of record:** `docs/decisions/arb-memory-architecture.md` §3, §4.

**Spec-panel verdict (2026-06-20):** cold-Opus + agy + M3 certifiers + codex implementor — **SPEC-HOLES**,
converged (GLM timed out). Folded: **(P0, silent data loss)** the idempotency-key insert + write are now ONE
transaction (§3) so a commit→XACK crash can't leave key-present/write-absent; **(P0)** the PEL/redelivery
contract is pinned (§3a — startup pending-drain, not just "not acked → retried"); **(P0)** the consumer runs
**separate concurrent read/write loops** (§3b) so a slow embed can't head-of-line-block reads (the
separate-lanes guarantee made real); **(P0)** the read path specifies an **error-reply** + the timeout
deny-proof rejects `timeout=0`; the tautological concurrent-cid test is replaced by a lanes-decoupled test
(§7); **(§6a)** the consumer validates the reply key is in the minted `arbmem:reply:<cid>` namespace.

## 0. Scope — IS / IS NOT

**IS:** the Valkey-bus transport that lets seats use ARB Memory without a second endpoint — a **single
memory consumer** that drains a write-intent stream (the one writer, §4) and answers read queries by
correlation ID, with **timeout→grep built first**. Proven against the local Valkey (db=12) + the Phase 0
pgvector DB.

**IS NOT:** no audit consumer (Phase 2), no containers/MCP/CF door (Phase 3), no orchestrator-primes skill
wiring beyond the client helper (the skill standing-instruction is Phase 3). The consumer runs as a plain
process in Phase 1 (containerized in Phase 3).

## 1. Why the bus (decision record §3, condensed)
Seats already hold an authenticated Valkey connection to the bridge. Memory rides it → **auth = bus
membership**, no CF tunnel / per-seat MCP auth / second endpoint. The memory host becomes a pure **consumer**,
never an endpoint anything connects to.

## 2. Streams + keys (all under the existing `agent_scratch:` prefix, db=12, distinct from bridge traffic)
- **Write-intent stream:** `agent_scratch:arbmem:writes` — seats `XADD` a write-intent; the consumer drains.
- **Read-request stream:** `agent_scratch:arbmem:reads` — separate lane (a read has a seat blocked on it;
  a write can sit in a backlog — §3 "separate read and write lanes"; a write burst must not delay reads).
- **Per-seat reply:** `agent_scratch:arbmem:reply:<correlation_id>` — a **short-lived list**, the seat
  `BLPOP`s it (chosen over pub-sub: pub-sub drops the message if the seat isn't subscribed at publish instant;
  `BLPOP` has no such race — §3). Consumer `LPUSH`es the result + sets a short TTL.
- Consumer group `arbmem-memory` on both streams (so Phase 2 audit can be a *separate* group on its own
  stream without sharing a cursor).

## 3. Write path (fire-and-forget, idempotent, single-writer)
- **Seat:** `XADD agent_scratch:arbmem:writes * ulid <ULID> payload <json>` and returns immediately (does not
  wait). `payload` = `{kind: "artefact"|"hint"|"artefact+hints", artefact?, hints?, source, author}`.
- **Consumer:** `XREADGROUP` (BLOCK, not poll) → for each intent:
  1. **Embed first (outside the txn):** `embed()` the hint text (the slow OpenAI call must NOT hold a
     Postgres transaction open — and on the dedicated write loop, §3b, so it can't block reads).
  2. **Idempotency + write in ONE transaction (spec-panel P0 — atomicity, no silent loss):**
     ```
     with conn.transaction():                               # outer txn
         n = INSERT INTO idempotency_keys(key) VALUES(<ULID>) ON CONFLICT DO NOTHING  # rowcount
         if n == 0: pass  # already processed → the whole txn is a no-op; fall through to XACK
         else: write_artefact_and_hints(conn, …)            # Phase 0; its conn.transaction() nests as a SAVEPOINT
     ```
     The key insert and the write **commit together or not at all**. A crash before commit → neither lands →
     redelivery re-does it. A crash AFTER commit, before `XACK` → redelivery finds the key present (`n==0`) →
     no-op → `XACK`. There is **no window where the key is committed but the write is absent** (the bug the
     two-separate-transactions design had).
  3. `XACK` only after the Postgres commit succeeds.
- The consumer is the ONLY thing that calls `write_artefact_and_hints` against the prod DB (deploy-scope
  enforces it in Phase 3; in Phase 1 it is the only process running the consumer loop). It is the **single
  embedding owner** (§4).

## 3a. Redelivery / PEL contract — PINNED (spec-panel P0, codex)
`XACK-after-commit` is necessary but not sufficient: an unacked message sits in the consumer group's PEL and
a plain `XREADGROUP … >` after restart reads only NEW messages, not the pending ones. Pinned:
- **Group creation:** `XGROUP CREATE <stream> arbmem-memory $ MKSTREAM` (idempotent; `BUSYGROUP` ignored).
- **Stable consumer name** per loop (e.g. `writer-1` / `reader-1`) so its PEL is recoverable.
- **Startup pending-drain:** on start, each loop first reads its own pending with `XREADGROUP … <consumer> 0`
  (re-process + ack any in-flight-at-crash entries) THEN switches to `… >` for new ones. (Single-consumer →
  this self-recovers; `XAUTOCLAIM` is only needed for multi-consumer, deferred with a name.)

## 3b. Consumer execution model — PINNED (spec-panel P0, agy/M3/cold-Opus)
The "separate read and write lanes" guarantee is only real if reads and writes execute **concurrently** — a
single sequential loop that embeds-then-writes would head-of-line-block reads behind a slow OpenAI call.
Pinned: the consumer runs **two independent loops** (asyncio tasks, or threads — the plan picks one),
each doing its own `XREADGROUP BLOCK` on its own stream with its own consumer name:
- **write loop** → drains `arbmem:writes` (embed + idempotent write, §3).
- **read loop** → drains `arbmem:reads` (embed query + `search_hints`/`retrieve` + reply).
A slow write embed delays only the write loop; reads keep flowing. This is what `test_reads_unaffected_by_
write_backlog` (§7) falsifies.

## 4. Read path — the sharp edges (build the timeout FIRST)
- **Seat helper `memory_query(conn, query_text, k, *, timeout_s) -> list | None`:**
  1. mint a `correlation_id`; `XADD agent_scratch:arbmem:reads * cid <id> reply <reply_key> query <text> k <k>`.
  2. `BLPOP <reply_key> timeout_s`.
  3. on a reply → parse + return the hits. **on timeout → return `None`** (the caller treats `None` as a
     cache miss and **falls through to grep**). The helper NEVER hangs.
- **Consumer (read loop):** `XREADGROUP` the reads lane → **validate the reply key** is exactly
  `agent_scratch:arbmem:reply:<cid>` for the request's `cid` (spec-panel §6a — do NOT `LPUSH` to an arbitrary
  caller-supplied key; a seat must not be able to redirect replies) → run `search_hints`/`retrieve`
  (Phase 0) → `LPUSH` a result envelope to the per-`cid` list + `EXPIRE` it (short TTL).
- **Result envelope `{status: "ok"|"error", hits?, reason?}`** (spec-panel P0 — the read path needs an
  explicit error reply, not just no-reply): on a DB/embed failure the consumer `LPUSH`es `{status:"error"}`
  so the seat unblocks immediately and treats it as a **miss → grep** (rather than waiting out the timeout).
  `status:"ok"` carries `hits`. The seat helper maps both `error` and `timeout` to `None` (cache miss).
- **Three non-negotiables (decision record §3):**
  1. **Timeout→grep is the load-bearing safety valve — implemented and TESTED before the happy path.** A
     consumer that's down/slow or a lost reply degrades to a cache miss, never a hang.
  2. **Per-seat reply routing:** the `cid` + per-`cid` reply key means concurrent seats never receive each
     other's answers. This breaks only under concurrency — so it is tested with **concurrent queries**
     (single-seat tests can't catch it).
  3. **`BLPOP` over pub-sub** for replies (the no-subscribe-race reason above).

## 5. Read model (client side only in Phase 1)
The orchestrator-primes / single-reader-then-fan-out model (§3) is a *caller* pattern: the orchestrator calls
`memory_query` once at panel setup and injects hits as hint-tier context. Phase 1 ships the `memory_query`
helper + the `memory_write` helper; the skill standing-instruction + hint-tier stamping is Phase 3 wiring.
Phase 1 just makes the helpers exist and degrade safely.

## 6. Failure modes → behavior (all must degrade, none hang)
| Failure | Behavior |
|---|---|
| Consumer down | read: `BLPOP` timeout → `None` → grep. write: intent sits in the stream, processed on consumer restart (durable). |
| Reply lost | `BLPOP` timeout → `None` → grep. |
| Duplicate write delivery | idempotency_keys `ON CONFLICT` → no-op. |
| Postgres down | consumer write txn fails → intent NOT XACK'd → retried; read returns an error result → seat treats as miss → grep. |
| Write burst | reads lane is separate → reads unaffected. |

## 7. Tests (deny-proof style; local Valkey db=12 + Phase 0 pgvector)
- `test_read_timeout_returns_none_then_grep` (**build FIRST**) — query with **no consumer running** →
  `memory_query` returns `None` **within a bounded wall-clock < timeout_s + ε** (assert the elapsed time, not
  just the `None`). Deny-proof: a blocking impl, or one that ignores `timeout_s` (`BLPOP 0` = block forever),
  hangs → the elapsed-time assertion fails. (cold-Opus: the old version passed a `timeout=0` impl.)
- `test_idempotency_and_write_are_atomic` (**the data-loss deny-proof**) — process an intent but force a
  crash/abort **between** the idempotency-key insert and the write (e.g. a DB exception inside the txn) →
  assert **NEITHER** the key NOR the write persisted (one txn rolled back) → on redelivery, both land.
  Deny-proof: the two-separate-transactions impl leaves the key present + write absent → on redelivery the
  write is skipped → **the row is missing** (silent loss) → test red.
- `test_pel_recovery_on_restart` — deliver an intent, crash the consumer **after `XREADGROUP` but before
  `XACK`** (entry in PEL), restart → the startup pending-drain (§3a) re-processes it → the write lands + the
  entry is acked. Deny-proof: a consumer that only does `XREADGROUP … >` on restart never sees the PEL entry
  → write lost.
- `test_reads_unaffected_by_write_backlog` (**replaces the tautological cid test**) — the per-`cid` reply key
  makes cross-delivery structurally impossible, so that's not the risk; the REAL risk is HOL blocking. Flood
  `arbmem:writes` with intents whose embed is artificially slow, then issue a read → the read returns within
  its timeout (separate concurrent loops, §3b). Deny-proof: a single-sequential-loop impl delays the read
  past its timeout → `None` when it should have hit → red.
- `test_reply_key_validation` — a read request carrying a reply key OUTSIDE `arbmem:reply:<cid>` → the
  consumer refuses to `LPUSH` there (§6a). Deny-proof: a trust-the-payload impl writes to the foreign key.
- `test_read_error_reply_is_a_miss` — consumer hits a DB/embed error → `LPUSH {status:"error"}` → the seat
  helper returns `None` immediately (not after the full timeout).
- `test_write_intent_is_idempotent` — same ULID twice → one row.
- `test_write_then_read_roundtrip` — `memory_write` artefact+hint → drain → `memory_query` returns it.
- The consumer loop is tested with an **injectable store** (the Phase 0 functions) so the bus logic is
  exercised without re-testing the store; a thin integration test runs the real consumer against the real DB.

## 8. Opens — resolved by the panel
- **Stream key namespacing — CONFIRMED collision-free** (cold-Opus). `agent_scratch:arbmem:*` is disjoint
  from the bridge's `agent_scratch:agent:*` / `:task:*` / `:registry:*`. Keep the `arbmem:` infix.
- **Consumer-group PEL — PINNED in §3a** (startup pending-drain; `XAUTOCLAIM` deferred-with-name to a
  multi-consumer future). No longer open.
- **`MAXLEN` — PINNED.** `XADD … MAXLEN ~ 10000` on both `arbmem:writes` and `arbmem:reads` (approximate
  trim; a wedged consumer can't OOM Valkey; reads are ephemeral so a small cap is fine; writes are durable
  intent — 10k is generous headroom before a stuck consumer is itself an incident). Tunable via config.
- **Reply key TTL — PINNED.** `EXPIRE arbmem:reply:<cid> 30` (seconds) — longer than any sane `timeout_s`,
  short enough to reap abandoned replies; the per-`cid` key + TTL means no unbounded key growth.
- **Backpressure on reads — named limit.** Consumer slow → reads time out → grep (acceptable, hint tier);
  the seat does NOT retry (one shot → miss → grep), so no retry storm. Named, not built-around.

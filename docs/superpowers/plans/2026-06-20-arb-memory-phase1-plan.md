# ARB Memory Phase 1 — Bus Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans.
> TDD; `- [ ]` checkboxes.

**Goal:** A single memory consumer over Valkey — write-intents drained idempotently into the Phase 0 store,
reads answered by correlation ID, **timeout→grep first** — with the spec-panel's atomicity/PEL/HOL fixes.

**Architecture:** `docs/superpowers/specs/2026-06-20-arb-memory-phase1-bus-design.md` (spec-panel folded).
Builds on Phase 0 `src/arb_memory/{store,embed}.py`.

**Tech Stack:** Python 3.11+, `redis` (redis-py, already a bridge dep), `psycopg`/`pgvector`/`openai` (the
`arb-memory` extra). Concurrency = **threads** (two daemon loops), matching the bridge's model.

> **Plan-panel folded (2026-06-20, PLAN-HOLES — cold-Opus+agy+M3 certifiers + codex):** (P0) the Task-4 PEL
> deny-proof was hollow (`step()` wrote the row before recovery so it passed with a no-op drain; the entry
> was XADDed before the group `$` cursor existed) → rewritten (§Task 4) to split `read_one`/`handle_and_ack`,
> create the group BEFORE the producing write, crash-seam after `XREADGROUP` before handle, and assert
> `drain_pending` writes the row AND clears the PEL; (P0) the `redis_bus`/`conn_factory`/`make_slow_embed`
> fixtures are now explicitly defined (Task 0) so TDD isn't blocked; (P0) `redis_bus` pins
> `decode_responses=True` (a bytes client silently breaks the reply-key string compare); (P1) Task 3's
> monkeypatch targets `store.write_artefact_and_hints` by attribute and throws inside the savepoint; (P2/§6a)
> Task 6 also asserts a non-None hit, and the reply-key test asserts the foreign key stays empty + the valid
> per-cid key receives only the matching envelope.

### Task 0: conftest fixtures (define BEFORE Task 1 — agy P0)
Add to `tests/arb_memory/conftest.py`:
- `redis_bus` — connect `redis.from_url(os.environ.get("ARB_MEMORY_REDIS_URL","redis://127.0.0.1:6379/15"),
  decode_responses=True)`; attach a unique `.prefix = f"arbmem_test_{uuid4().hex}:"`; on teardown delete keys
  matching `{prefix}*` (`SCAN`+`DELETE`, NOT `FLUSHDB`). `skip` if unreachable. **`decode_responses=True` is
  required** (reply-key compare + field-dict access break on bytes).
- `conn_factory` — returns a callable yielding a fresh psycopg connection on the test's scratch schema (each
  loop gets its own connection; the consumer threads must not share one); teardown closes them.
- `make_slow_embed` — `lambda base, delay: (lambda text: (time.sleep(delay), base(text))[1])` — wraps
  `fake_embed` with an artificial delay for the HOL test.

## Global Constraints
- New module `src/arb_memory/bus.py`. Still imported only by the (future) MCP-host services.
- **Factor handlers from loops:** `handle_write_intent(redis, conn, intent, *, embed)` and
  `handle_read_request(redis, conn, request, *, embed)` are pure-ish, directly testable (inject redis, conn,
  embed; simulate crashes). The two thread loops are thin wrappers around `XREADGROUP BLOCK` + the handler.
  The deny-proofs test the HANDLERS (crash injection) + an integration test runs the real loops.
- **Test isolation:** Redis tests use a **dedicated db** (`ARB_MEMORY_REDIS_URL`, default
  `redis://127.0.0.1:6379/15` — NOT db 12, the live bridge bus) and a **unique stream/group/key prefix per
  test** (`arbmem_test_<uuid>:`); `FLUSHDB`-free (prefix isolation). Skip if Redis unreachable. PG tests reuse
  the Phase 0 `scratch` fixture.
- `bus.py` imports gated so `pytest tests/` collection survives without the extra (`importorskip`).
- The atomicity fix is load-bearing — copy the §3 one-transaction pattern verbatim.
- TDD: failing test → run-to-fail → impl → pass → commit.

## File Structure
- `src/arb_memory/bus.py` — `memory_write`, `memory_query`, `handle_write_intent`, `handle_read_request`,
  `WriteLoop`, `ReadLoop`, `MemoryConsumer` (owns both loops), `ensure_group`, stream/key name helpers.
- `tests/arb_memory/conftest.py` — add `redis_bus` fixture (dedicated db + unique prefix).
- `tests/arb_memory/test_bus_read_timeout.py`, `test_bus_write.py`, `test_bus_pel.py`, `test_bus_hol.py`,
  `test_bus_read.py`.

---

### Task 1: `memory_query` + timeout→grep (BUILD FIRST — the safety valve)

**Files:** Create `src/arb_memory/bus.py` (the read-client half), `tests/arb_memory/test_bus_read_timeout.py`.

**Interfaces — Produces:** `memory_query(redis, query_text, k=8, *, timeout_s, prefix=PREFIX) -> list|None`.

- [ ] **Step 1: failing test (FIRST):**
```python
import os, time, pytest
redis = pytest.importorskip("redis")
pytestmark = pytest.mark.skipif(not _redis_reachable(), reason="no redis")

def test_read_timeout_returns_none_then_grep(redis_bus):
    start = time.monotonic()
    out = memory_query(redis_bus, "anything", timeout_s=1.0, prefix=redis_bus.prefix)  # NO consumer running
    elapsed = time.monotonic() - start
    assert out is None                       # cache miss → caller greps
    assert 0.9 <= elapsed <= 2.0             # bounded by timeout_s, NOT hang-forever and NOT instant-0
```
- [ ] **Step 2: run-to-fail** (no `memory_query`).
- [ ] **Step 3: implement** `memory_query`: mint `cid` (uuid), `reply_key = f"{prefix}arbmem:reply:{cid}"`,
  `XADD {prefix}arbmem:reads * cid <cid> reply <reply_key> query <text> k <k>` with `MAXLEN ~ 10000`; then
  `BLPOP reply_key timeout=int(ceil(timeout_s))`; on `None` → return `None`; on a value → parse the envelope,
  return `hits` if `status=="ok"` else `None`. **Never block unbounded** (BLPOP timeout is the bound).
- [ ] **Step 4: run-to-pass.**  - [ ] **Step 5: commit** — `feat(arb-memory): memory_query + timeout->grep [P1]`

---

### Task 2: `memory_write` (fire-and-forget XADD)

**Interfaces — Produces:** `memory_write(redis, *, artefact=None, hints=(), source="seat", author="unknown",
ulid=None, prefix=PREFIX) -> str` (the ULID).

- [ ] **Step 1: test** — `memory_write` XADDs to `{prefix}arbmem:writes` with a ULID + JSON payload; assert
  the stream has one entry with the right fields. Returns the ULID.
- [ ] **Steps 2-4:** implement (`ulid or new_ulid()`, `XADD … MAXLEN ~ 10000`). Does NOT wait.
- [ ] **Step 5: commit** — `feat(arb-memory): memory_write fire-and-forget [P1]`

---

### Task 3: `handle_write_intent` — atomic idempotency + write (the data-loss deny-proof)

**Interfaces — Produces:** `handle_write_intent(conn, intent, *, embed) -> str` — returns `"written"` |
`"duplicate"`; raises on failure (caller does NOT XACK on raise).

- [ ] **Step 1: failing tests:**
```python
def test_write_intent_is_idempotent(scratch, fake_embed):
    intent = {"ulid": "U1", "kind": "artefact+hints",
              "artefact": {"artefact_id": "d.md", "content": "c"},
              "hints": [{"text": "t"}]}
    assert handle_write_intent(scratch, intent, embed=fake_embed) == "written"
    assert handle_write_intent(scratch, intent, embed=fake_embed) == "duplicate"   # same ULID
    assert scratch.execute("SELECT count(*) FROM artefacts WHERE artefact_id='d.md'").fetchone()[0] == 1

def test_idempotency_and_write_are_atomic(scratch, fake_embed, monkeypatch):
    # force a failure AFTER the idempotency-key insert, INSIDE the write → whole txn must roll back
    import arb_memory.store as store
    monkeypatch.setattr(store, "write_artefact_and_hints",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        handle_write_intent(scratch, {"ulid": "U2", "kind": "artefact",
                                      "artefact": {"artefact_id": "d.md", "content": "c"}}, embed=fake_embed)
    # NEITHER the key NOR the artefact persisted (one transaction)
    assert scratch.execute("SELECT count(*) FROM idempotency_keys WHERE key='U2'").fetchone()[0] == 0
    assert scratch.execute("SELECT count(*) FROM artefacts WHERE artefact_id='d.md'").fetchone()[0] == 0
```
- [ ] **Steps 2-4:** implement the §3 pattern: embed outside the txn, then
  `with conn.transaction(): n = INSERT idempotency_keys ON CONFLICT DO NOTHING (rowcount); if n==0: return
  "duplicate"; store.write_artefact_and_hints(conn, …); return "written"`. **Call it as
  `store.write_artefact_and_hints` (module attribute), NOT `from .store import write_artefact_and_hints`**
  (plan-panel P1 cold-Opus — else the test's monkeypatch on `store.write_artefact_and_hints` misses).
  Deny-proof: a two-separate-transactions impl leaves the key committed when the write throws →
  `test_idempotency_and_write_are_atomic` finds the key present → red. (Optional 2nd test: throw INSIDE
  `write_artefact_and_hints` after its savepoint opens, to exercise the nested-savepoint rollback path.)
- [ ] **Step 5: commit** — `feat(arb-memory): atomic idempotency+write handler [P1]`

---

### Task 4: write loop + `ensure_group` + PEL recovery (deny-proof)

**Interfaces — Produces:** `ensure_group(redis, stream, group)` (called at WriteLoop construction, BEFORE any
produce); `WriteLoop.read_one() -> (entry_id, intent)|None` (`XREADGROUP … >` BLOCK, does NOT handle/ack);
`WriteLoop.handle_and_ack(entry_id, intent)` (handle_write_intent → `XACK` after commit);
`WriteLoop.drain_pending()` (`XREADGROUP … 0` → handle_and_ack each pending). Thread `run()` = `ensure_group`
+ `drain_pending` + loop(`read_one`→`handle_and_ack`).

- [ ] **Step 1: failing test (un-hollowed — plan-panel P0 cold-Opus+codex):**
```python
def test_pel_recovery_on_restart(redis_bus, conn_factory, fake_embed):
    conn = conn_factory()
    loop = WriteLoop(redis_bus, conn, embed=fake_embed, prefix=redis_bus.prefix, consumer="w1")
    # group MUST exist before the produce (else XREADGROUP > never delivers it). WriteLoop ctor ensure_group'd.
    ulid = memory_write(redis_bus, artefact={"artefact_id": "d.md", "content": "c"},
                        hints=[{"text": "t"}], prefix=redis_bus.prefix)
    entry_id, intent = loop.read_one()                 # delivered, now in PEL
    assert intent is not None
    # CRASH SEAM: simulate crash here — read happened, handle did NOT. Row must NOT exist yet.
    assert conn.execute("SELECT count(*) FROM artefacts WHERE artefact_id='d.md'").fetchone()[0] == 0
    # restart: fresh loop recovers the PEL entry
    loop2 = WriteLoop(redis_bus, conn_factory(), embed=fake_embed, prefix=redis_bus.prefix, consumer="w1")
    loop2.drain_pending()
    assert loop2.conn.execute("SELECT count(*) FROM artefacts WHERE artefact_id='d.md'").fetchone()[0] == 1
    # AND the PEL is now empty (entry acked)
    pend = redis_bus.xpending(f"{redis_bus.prefix}arbmem:writes", GROUP)
    assert pend["pending"] == 0
```
  The crash seam now precedes the write (so a no-op `drain_pending` leaves the row at 0 → red), and `XPENDING`
  asserts the entry was actually acked (a drain that handles-but-doesn't-ack → pending != 0 → red).
- [ ] **Steps 2-4:** implement `ensure_group` (`XGROUP CREATE stream GROUP $ MKSTREAM`, ignore `BUSYGROUP`) in
  the WriteLoop ctor; `read_one` (`XREADGROUP GROUP consumer COUNT 1 BLOCK … >`); `handle_and_ack`
  (handle_write_intent in its txn, then `XACK` AFTER commit); `drain_pending` (`XREADGROUP GROUP consumer 0`
  → handle_and_ack each). Deny-proof: a loop without `drain_pending` (only `>`) never recovers → row stays 0.
- [ ] **Step 5: commit** — `feat(arb-memory): write loop + PEL recovery [P1]`

---

### Task 5: read loop — validate reply key, search, error-reply

**Interfaces — Produces:** `handle_read_request(redis, conn, request, *, embed) -> None` (LPUSHes the
envelope); `ReadLoop.step()`.

- [ ] **Step 1: failing tests:** `test_reply_key_validation` (reply key outside `arbmem:reply:<cid>` → consumer
  refuses, does not LPUSH there); `test_read_error_reply_is_a_miss` (inject a search error → LPUSH
  `{status:"error"}` → `memory_query` returns `None` fast); `test_write_then_read_roundtrip`
  (write → drain → query returns the hit).
- [ ] **Steps 2-4:** implement: validate `reply == f"{prefix}arbmem:reply:{cid}"`; `embed(query)` +
  `retrieve`; `LPUSH reply {status:"ok", hits:…}` + `EXPIRE reply 30`; on exception `LPUSH {status:"error"}`.
- [ ] **Step 5: commit** — `feat(arb-memory): read loop + reply validation + error-reply [P1]`

---

### Task 6: `MemoryConsumer` (both loops concurrently) + HOL deny-proof

**Interfaces — Produces:** `MemoryConsumer(redis, conn_factory, *, embed)` running `WriteLoop` + `ReadLoop` in
two daemon threads; `start()`/`stop()`.

- [ ] **Step 1: failing test (HOL — the separate-lanes deny-proof):**
```python
def test_reads_unaffected_by_write_backlog(redis_bus, conn_factory, fake_embed):
    slow = make_slow_embed(fake_embed, delay=0.5)         # writes embed slowly
    consumer = MemoryConsumer(redis_bus, conn_factory, embed=slow, prefix=redis_bus.prefix); consumer.start()
    try:
        # PRE-SEED the queried hint and wait until the write loop has persisted it (so a hit is possible)
        memory_write(redis_bus, artefact={"artefact_id": "target", "content": "c"},
                     hints=[{"text": "the target hint"}], prefix=redis_bus.prefix)
        _wait_until(lambda: _row_exists(conn_factory(), "target"), timeout=10)
        # NOW flood the WRITES lane (each ~0.5s on the write loop) — a 10s+ backlog
        for i in range(20):
            memory_write(redis_bus, artefact={"artefact_id": f"d{i}", "content": "c"},
                         hints=[{"text": f"t{i}"}], prefix=redis_bus.prefix)
        # a READ must still return within its timeout AND hit the pre-seeded target (separate loop)
        start = time.monotonic()
        out = memory_query(redis_bus, "the target hint", k=1, timeout_s=2.0, prefix=redis_bus.prefix)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0                              # not blocked behind the write backlog
        assert out is not None and len(out) >= 1          # AND a real hit (P2: no fast-but-empty spurious pass)
    finally:
        consumer.stop()
```
(`_wait_until` / `_row_exists` are small test helpers in conftest; pre-seeding + waiting decouples the hit
assertion from the write-backlog timing.)
- [ ] **Steps 2-4:** implement `MemoryConsumer` with two threads (each its own redis + pg connection;
  `conn_factory()` per loop). Deny-proof: a single-loop impl (writes then reads sequentially) blocks the read
  behind ~10s of write embeds → the read times out / exceeds 2s → red.
- [ ] **Step 5: commit** — `feat(arb-memory): MemoryConsumer dual-loop, HOL-safe [P1]`

---

### Task 7: wrap-up — config, MAXLEN/TTL constants, collection check

- [ ] **Step 1:** centralize `PREFIX`, `WRITES`/`READS`/`reply` key helpers, `MAXLEN=10000`, `REPLY_TTL=30`,
  `GROUP="arbmem-memory"` in `bus.py`. Confirm every test module `importorskip`s and uses the dedicated redis
  db + unique prefix.
- [ ] **Step 2:** run the full Phase 1 suite (`pytest tests/arb_memory/test_bus_*.py -q`) green against local
  redis + pgvector; run `pytest tests/ --collect-only -q` (collection intact). Confirm `git diff --name-only`
  is only `src/arb_memory/**` + `tests/arb_memory/**` — no bridge-core.
- [ ] **Step 3: commit** — `feat(arb-memory): Phase 1 bus config + green [P1]`

---

## Self-Review
- **Spec coverage:** §3 atomic write → T3; §3a PEL → T4; §3b dual-loop → T6; §4 read/timeout → T1/T5;
  helpers → T1/T2; opens (MAXLEN/TTL) → T7. ✓
- **Deny-proofs real:** T1 (bounded wall-clock rejects timeout=0), T3 (two-txn leaves key → red), T4 (no
  drain_pending → PEL lost → red), T6 (single-loop → read blocked → red). ✓
- **Isolation:** dedicated redis db 15 + unique prefix (not the live db 12); importorskip; #11 lesson. ✓
- **No bridge-core contamination:** new module + the T7 diff check. ✓

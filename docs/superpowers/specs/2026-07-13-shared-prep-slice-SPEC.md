# SPEC — Shared prep slice (builds FIRST, unblocks parallel Item 1 + Item 2)

**Status:** SPEC **v2** (spec panel₀ folded; confirm pending) · **Design refs:** consumer-loop-robustness-design.md
§6 (F-joint), memory-write-result-signal-design.md §7 · **Author:** warm-Opus (inline) · **Date:** 2026-07-13.

## Spec panel₀ fold (v1 → v2)
codex-sol/terra `nc/P1`, cold-Opus `nc/P1`, agy `approve/P2`. Decomposition confirmed sound (prep
behaviour-preserving; loop-extraction correctly in Item 1). Folded seams: **(SP-1)** receipt shape split —
persisted/returned receipt = 4 fields, **no `duplicate`**; the writer/MCP *response envelope* overlays
`duplicate` + carries `ulid` (4-seat consensus). **(SP-2)** `WriteLoop._handle_entry` is pre-factored here
into a **`_publish_result` hook** at every terminal disposition (the shared seam Item 1 + Item 2 both need
— cold-Opus's poison-exhaust-hang + codex-terra's collision). **(SP-3)** conn-factory is **additive** (keep
one primary `self.conn`) + full call-site enumeration.

**Why a prep slice.** Both items touch `bus.py`'s `WriteLoop`/`handle_write_intent`. This slice lands ONLY
the truly-shared, **behaviour-preserving** contract both depend on, so Item 1 (loop robustness) and Item 2
(result channel) then build in parallel with minimal overlap. **Deliberately minimal** — the
`consumer_loop.py` extraction + Event migration is Item 1's (it lands *with* the bounded-retry, since
unifying the loop without it would just spin a stuck entry); the result channel + await is Item 2's.

## Deliverables (this slice only)

### 1. `handle_write_intent` structured-receipt return contract (`store.py`, `bus.py`)
Change the return from `"written"|"duplicate"` (str) to a **persisted receipt dict** — exactly **4 fields,
NO `duplicate`** (SP-1; `duplicate`/`ulid`/`timed_out` live only in the *response envelope*, Item 2):
```python
# persisted (idempotency_keys.receipt) AND returned by handle_write_intent:
{"artefact_outcome": "stored"|"deduped"|"none"|"refused_version_mismatch", "artefact_id": str|None,
 "version": int|None, "hints_stored": int}
```
**AMENDMENT 2026-07-30 (owner co-signed, Mark — "O-1 approved").** `refused_version_mismatch` is
added to the `artefact_outcome` enum by the ARB-B19(c) optimistic-concurrency slice
(`docs/superpowers/specs/2026-07-30-arb-b19c-expected-version-design.md`). It is emitted only when
a write carries `expected_version` and the artefact's head has moved; `version` then carries the
observational head (0 = no versions exist), never a version this request created. The field COUNT
is unchanged at 4. **Readers must treat any unrecognised `artefact_outcome` value as
non-success** — the nine in-repo readers surveyed 2026-07-30 already gate on exact membership in
`{stored, deduped}`, which is the required shape.
`handle_write_intent` returns **`(receipt, is_replay)`** — the replay flag is **out-of-band**, NOT a
receipt field. In the **prep slice**: a fresh write → `(receipt_4field, False)`; a **ulid-idempotency
replay** short-circuits before the write → `(None, True)`. **Prep does NOT persist or recover a receipt on
replay** — the `receipt` column + stored-receipt recovery are **Item 2** (which then makes a replay return
the *stored* 4-field receipt). Prep guarantees only the `is_replay` signal; `duplicate` is set from
`is_replay` in the channel envelope (Item 2), never in the persisted receipt.
- `upsert_artefact -> (artefact_id, version, artefact_outcome)`  (`artefact_outcome ∈ {stored, deduped,
  refused_version_mismatch}` — the third value added by the 2026-07-30 amendment above;
  `deduped` on the latest-hash-match branch store.py:41-42).
- `upsert_hint -> (hint_id, inserted: bool)`  (`inserted=False` on the `ON CONFLICT`/dedup branch).
- `write_artefact_and_hints` assembles `{artefact_outcome, artefact_id, version, hints_stored}`
  (`hints_stored` = count of `inserted=True`); hints-only write → `artefact_outcome:"none"`.
- `handle_write_intent` returns **`(receipt_4field, is_replay: bool)`** — `is_replay=True` on the
  ulid-idempotency replay. `is_replay` travels out-of-band (NOT a receipt field); it is the ONLY signal of
  replay, so it must be threaded to `_publish_result` (§3) → the result-channel envelope → the writer.
- **Caller sweep (grep, all stay green):** `upsert_artefact` ~55 call sites total but only **~20 UNPACK the
  tuple (`_, v =` / `aid, v =`) and break** (`test_vault_export.py`, `test_journey_export.py`,
  `test_store.py`, `test_store_provenance.py`); the other ~35 ignore the return (pi-GLM P2). `upsert_hint`
  ~19 sites; `handle_write_intent` assertion sites (`test_bus_write.py:42-43`) + any
  `write_artefact_and_hints`-return consumers. Production caller of both = only `write_artefact_and_hints`.
- **No `write_result` channel, no await, no receipt persistence** here — this slice is the return-shape
  contract + its callers only. (Item 2 adds the channel/persistence; Item 1 consumes the truthy/falsy
  ack-flow contract.)

### 2. `WriteLoop` conn-factory — additive (`bus.py`) (SP-3, pi-GLM P1-1)
`WriteLoop.__init__(… conn_factory)` does **`self.conn_factory = conn_factory; self.conn = conn_factory()`
once** — the happy path is **byte-identical** (one persistent `self.conn`, just sourced via the factory);
`self.conn_factory` is retained *only* so Item 1 can open a **fresh** conn for its exhaustion-deadletter /
canary. WriteLoop does NOT become per-entry (drop the "like the other four" phrasing — those use per-call
conns; WriteLoop keeps its persistent one).
- Pass the **callable** (not `conn_factory()`) at **every** call site: `MemoryConsumer` (bus.py:345-348)
  and the tests `test_bus_pel.py`, `test_bus_read.py`, `test_write_deadletter.py`.
- `ReadLoop` is **unchanged** (no conn-factory, no recirculation — see Item 1 §0).
- `_handle_entry`'s return contract stays **falsy=retry / truthy=ack-or-terminal** (both items rely on it).

### 3. `WriteLoop._handle_entry` → `_publish_result` hook seam (`bus.py`) (SP-2, cold-Opus P1-2, pi-GLM P1-2)
Pre-factor (behaviour-preserving) so `_handle_entry` calls **`self._publish_result(entry_id, fields,
receipt_or_none, is_replay)` at EVERY terminal disposition** — success, dedup, ulid-duplicate replay,
malformed/poison deadletter — with a **default no-op** implementation. `is_replay` (from
`handle_write_intent`'s out-of-band return) is passed so Item 2 can set `duplicate` in the channel
envelope without contaminating the persisted 4-field receipt. This is the single shared seam both items compose on:
- **Item 1** owns *which* dispositions exist and calls the hook on each — including its NEW terminals
  (poison-exhaustion deadletter; row-unstorable ack; NOT just "the deadletter path" — pi-GLM P1-2).
- **Item 2** overrides `_publish_result` to publish the receipt / `{failed}` to `write_result_key`.
So a poison-exhausted *awaited* write publishes `{failed}` for free (no hang to the 30 s cap), and the two
items edit *different* extension points (Item 1: dispositions; Item 2: the hook body) — genuinely parallel.

## Acceptance criteria
- `handle_write_intent` returns the receipt dict; `upsert_artefact`/`upsert_hint` return the tuples; every
  caller/test updated and green. **No behaviour change** beyond the return-shape (same rows written, same
  dedup, same acks).
- `WriteLoop` takes a `conn_factory`; `MemoryConsumer`/`test_bus_pel.py` updated; happy path unchanged.
- `uv run --extra arb-memory pytest tests/arb_memory` green (targeted; not e2e).

## Tests to add/update
- Unit: `upsert_artefact` returns `stored`/`deduped` (first write, changed rewrite, byte-identical rewrite,
  A→B→A revert); `upsert_hint` returns `inserted` True/False; `write_artefact_and_hints`/`handle_write_intent`
  receipt shape incl. **artefact-deduped-but-hint-stored** (`deduped` + `hints_stored≥1`).
- The full caller sweep updated + green; `test_bus_pel.py` conn-factory constructor updated.

## Out of scope (Item 1 / Item 2)
`consumer_loop.py` extraction, Event migration, poison counter, classify, cursor, circuit breaker,
migrations (Item 1); `write_result` channel, receipt persistence, await path, server-minted request_id
(Item 2).

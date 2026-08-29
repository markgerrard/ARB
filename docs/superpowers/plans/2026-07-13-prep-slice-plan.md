# PLAN — Shared prep slice (build FIRST; TDD, luna@high)

**Spec:** `docs/superpowers/specs/2026-07-13-shared-prep-slice-SPEC.md` v2 · **Effort:** high · **Regime:**
TDD (RED→GREEN per step), evidence contract, cold-gate. **Worktree:** dispatch with `--worktree prep`.

**Base:** `dev`. **Branch:** `feat/prep-slice`. **Env:** `uv run --extra arb-memory pytest tests/arb_memory`.

## Ordering (each step: write the failing test, show it RED, implement, show it GREEN)

1. **`upsert_artefact` → `(artefact_id, version, artefact_outcome)`** (`store.py`).
   - RED: `tests/arb_memory/test_store.py` — assert `stored` on first write + changed rewrite + A→B→A
     revert; `deduped` on byte-identical rewrite of latest.
   - GREEN: return the 3-tuple; dedup branch (store.py:41-42) → `deduped`, insert → `stored`.
   - Sweep: fix the ~20 unpacking call sites (`test_vault_export.py`, `test_journey_export.py`,
     `test_store.py`, `test_store_provenance.py`) to `id, v, _ =` / keep green.
2. **`upsert_hint` → `(hint_id, inserted: bool)`** (`store.py`).
   - RED: assert `inserted=True` on a new hint, `False` on the `ON CONFLICT` dedup branch.
   - GREEN: derive `inserted` from `RETURNING id` `fetchone() is not None`. Sweep the ~19 `upsert_hint`
     call sites (grep `upsert_hint(` — `store.py` internal + `test_store.py`, `test_journey_export.py`,
     `test_vault_export.py`; production caller = only `write_artefact_and_hints`), unpacking `hid, _ =`.
3. **`write_artefact_and_hints` + `handle_write_intent` → 4-field receipt + `is_replay`** (`store.py`,
   `bus.py`).
   - RED: receipt `{artefact_outcome, artefact_id, version, hints_stored}` incl. **artefact-deduped-but-
     hint-stored** (`deduped` + `hints_stored≥1`); `handle_write_intent` returns `(receipt, is_replay)`.
     On a **fresh** write `is_replay=False` + the real receipt; on a **ulid-idempotency replay**
     `is_replay=True` and **`receipt=None`** — prep does NOT persist/recover the receipt (that's Item 2
     step 2, which adds `idempotency_keys.receipt`), so prep only guarantees the `is_replay` signal, not a
     recovered receipt (plan panel P1). (`test_bus_write.py:42-43` updated to the tuple.)
   - GREEN: assemble receipt on the write path; `hints_stored` = count `inserted=True`; the idempotency
     short-circuit returns `(None, True)`.
4. **`WriteLoop` additive conn-factory** (`bus.py`).
   - RED: `test_bus_pel.py` constructs `WriteLoop(redis, conn_factory=…)`; happy-path write behaviour
     byte-identical; `MemoryConsumer`/`test_bus_read.py`/`test_write_deadletter.py` pass the callable.
   - GREEN: `self.conn_factory = conn_factory; self.conn = conn_factory()`; keep persistent-conn happy path.
5. **`WriteLoop._handle_entry` → `_publish_result(entry_id, fields, receipt_or_none, is_replay)` hook**
   (`bus.py`).
   - RED: a test subclass records hook calls; assert the hook fires at every terminal disposition
     (success/dedup/replay/malformed-deadletter) and the **default is a no-op** (behaviour unchanged).
   - GREEN: call the hook at each terminal point; default `_publish_result` returns None.

## Acceptance / evidence contract (paste — per-step, plan panel P1)
- Per step: the exact targeted `pytest` command + **RED** (pre-impl) and **GREEN** (post-impl) output.
- Final `uv run --extra arb-memory pytest tests/arb_memory` output (all green, counts).
- Confirm **no behaviour change** beyond return-shape: `git diff --stat`; the happy-path write test
  unchanged in assertions.
- The commit SHA on `feat/prep-slice`.

## Cold-gate (reviewer verifies)
Prep is behaviour-preserving (no robustness/channel logic leaked in); the receipt is exactly 4 fields;
`_publish_result` default is a no-op; every conn-factory call site updated; `is_replay` returned
out-of-band. Deny-proof: revert the hook → Item 2 would have nowhere to publish (the seam exists).

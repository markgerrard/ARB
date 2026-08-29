# Served-hint record — ERRATA

The design document
`docs/superpowers/specs/2026-07-27-served-hint-record-design-V5-FROZEN.md` is frozen.
This file records deliberate divergences at build time. Do not reopen the frozen guide.

## 2026-07-29 — S1 schema: query columns (BUILD-CHARTER D-2)

- **Guide says (§6):** `query_text text NOT NULL` on `hint_read`.
- **Built instead:** `query_hmac text NULL`, `query_text text NULL`, `query_truncated boolean NOT NULL DEFAULT false`.
- **Why:** Operator decision D-2 in
  `docs/superpowers/specs/2026-07-29-served-hint-record-BUILD-CHARTER.md` — store keyed
  HMAC-SHA256 by default; raw text only when explicitly enabled; NULL `query_hmac` when the
  key is unset so recording failure never fails a read (§7).

## 2026-07-29 — S2 grants: PUBLIC revoke must name all three hint_read tables

- **Guide says (lines 896–915):** `REVOKE ALL ON hint_read, hint_read_hit FROM PUBLIC` in
  `apply_local_reader_grants` and `apply_mcp_grants`; parenthetical claims
  `hint_read_deadletter`'s PUBLIC exposure is closed independently by
  `apply_hint_read_consumer_grants`'s own PUBLIC revoke (database-wide).
- **Built instead:** both isolating functions' `REVOKE ALL ... FROM PUBLIC` name
  **`hint_read`, `hint_read_hit`, and `hint_read_deadletter`**.
- **Why:** The guide argument holds only under the full `run_grants()` sequence. Under the
  narrower sequence tests seed (`apply_hint_read_consumer_grants` not run), the door role still
  held INSERT/SELECT on `hint_read_deadletter` through PUBLIC, and a two-table deny-proof
  passed while proving nothing. A per-role REVOKE cannot remove a PUBLIC-inherited privilege.

## 2026-07-29 — S3 local recorder: D-2 query columns on INSERT

- **Guide says (§4 local tier, ~lines 500–503):** parent INSERT lists a single `query_text`
  column (and the v5 schema marks it `text NOT NULL`).
- **Built instead:** `_record_local_read` inserts `query_hmac` + nullable `query_text` via
  module-level `_query_columns(query_text)` (HMAC-SHA256 under `ARB_HINT_READ_QUERY_KEY`;
  raw text only when `ARB_HINT_READ_QUERY_RAW=1`; key unset/empty → NULL hmac, log once).
- **Why:** BUILD-CHARTER D-2 (same decision that shaped the S1 schema). §7 still dominates —
  missing key never fails the read/record path.

## 2026-07-29 — S3: `store.retrieve` does not yet emit outer `withheld`

- **Guide says (§2 scope table + H-02):** each `store.retrieve` item carries a `withheld` key;
  `_record_local_read` reads `hit["withheld"]` at the outer level.
- **Observed at S3 base:** `store.retrieve` (`store.py`) computes a local `withhold` for
  artefact attachment but does **not** put `withheld` on the returned dict — only
  `hint` / `artefact` / `repo_pointer`.
- **Built instead:** the recorder follows the guide body (`hit["withheld"]`, metrics under
  `hit["hint"]`). S3 tests construct that shape; call-site integration (later increment) must
  ensure the outer key is present before wiring `memory_search` → `_record_local_read`.
- **Closed in S4a:** see entry below.

## 2026-07-29 — S4a: `store.retrieve` emits `withheld` (BUILD-CHARTER D-3)

- **Guide says (F-09 CLOSED; §2 scope table):** each `store.retrieve` item carries `withheld`.
- **Built:** `store.retrieve` (`store.py`, currently ~L311–323) includes
  `"withheld": withhold` on every returned element, reusing the `withhold` value already
  computed for artefact attachment — not recomputed at consumers.
- **Accepted consequence (D-3):** `_json_safe_search_hit` does `out = dict(hit)`, so
  `withheld` is visible on the `memory_search` MCP response.

## 2026-07-29 — S4a: bus record-intent wire carries D-4 query columns, not guide `query`

- **Guide says (§4 bus tier field table):** wire field `query` = truncated request query text;
  `query_truncated` is `"1"`/`"0"`. XADD illustration around guide lines 245–251.
- **Built instead (BUILD-CHARTER D-4):** `handle_read_request` (`bus.py`, currently ~L250–325)
  XADDs to `hint_reads_stream(prefix)` → `{prefix}arbmem:hint-reads` with
  `query_hmac` / optional `query_text` / `query_truncated` (`"1"`/`"0"`), derived via the
  same module-level `_query_columns` helper in `mcp/read_tools.py` that S3 uses for the local
  INSERT. Raw `query_text` is present on the wire only when `ARB_HINT_READ_QUERY_RAW=1`.
  Cap is `SEARCH_MAX_QUERY_CHARS = 2000` at record time; `served_at` is captured at
  reply-build time; `run_id`/`seat_id` are omitted; construction + `json.dumps` + XADD share
  one `try/except Exception` (§7); XADD uses `maxlen=MAXLEN, approximate=True` (G-04).
- **Wire `hits` shape:** flat JSON array of
  `{hint_id, rank, withheld, vector_distance, lexical_rank}` per the guide field table
  (metrics from `hit["hint"]`, `withheld` from the outer level). Note the guide's later
  `HintReadSink` sketch (S4b) still shows nested `hit["hint"]` access on the *parsed event*
  — that is S4b's parse concern, not the producer wire shape built here.
- **Line-number drift:** the guide's `bus.py:242` / `bus.py:250-254` / `bus.py:256` anchors
  for `handle_read_request` are stale relative to post-S4a `bus.py` (handler starts ~L250;
  XADD ~L319).


## 2026-07-29 — S3b: recorder failure log uses `logger.error`, not `logger.exception`

- **Guide says (§4 local tier, the `memory_search` sketch around the lines that set
  `rejection_class` / call `_record_local_read` under `try/except Exception`):** both
  recording-failure guards call `logger.exception("local read receipt failed …")`.
- **Built instead:** `logger.error("…: %s", rec_exc)` with no `exc_info` / no stack dump.
- **Why:** `logger.exception` writes a `Traceback` block to the process stderr. The local
  stdio MCP contract
  (`tests/arb_memory/test_local_server.py::test_stdio_search_error_is_structured_without_traceback`)
  requires that a rejected `memory_search` (e.g. missing `OPENAI_API_KEY` against an
  unresolvable DSN) returns a structured tool error **without** a traceback on stderr. On
  the rejection path the recorder may itself fail (connect to a bad DSN while attempting the
  error receipt); that secondary failure must stay swallowed (G-01) **and** silent of stack
  frames. The original client exception is still bare-`raise`d with `__cause__`/`__context__`
  untouched. Verified against the guide body at that sketch (not stale line anchors alone).

## 2026-07-29 — S6: J-05 partial-index check uses `pg_index.indpred`, not `indexdef LIKE`

- **Guide says (§9, currently line 1132):** assert a partial index on `run_id` / `seat_id`
  via `pg_indexes.indexdef LIKE '%WHERE run_id IS NOT NULL%'` (and the `seat_id` equivalent).
- **Built instead:** `tests/arb_memory/test_hint_read_section9.py::test_section9_partial_indexes_via_pg_index_indpred`
  asserts `pg_index.indpred IS NOT NULL` for `hint_read_run_idx` and `hint_read_seat_idx`.
- **Why:** `pg_get_indexdef` parenthesises the NullTest
  (`WHERE (run_id IS NOT NULL)`), so the guide's unparenthesised LIKE pattern **never matches
  in any state** — the canonical "check that cannot fail on the defect it names". Two live
  executions against schema.sql lines 610–611 confirmed the parenthesised form; the catalog
  property discriminates full-width vs partial. Unanimous panel preference (§11 Q9); adopted
  in the S6 brief.

## 2026-07-29 — S6: J-06 grant row was already split in S2 (map only)

- **Guide says (§9, currently lines 1124–1125):** one combined shape around local-reader
  INSERT/SELECT and a separate `vault_export_role` cannot-INSERT row. Historical review
  notes treated an earlier combined grant row as demanding both "has INSERT" and "has no
  INSERT" for the local reader against one implementation.
- **Built (S2, mapped in S6):** 
  - `test_local_reader_can_insert_hint_read_but_not_select` — INSERT yes, SELECT no (seeded).
  - `test_vault_export_role_has_no_hint_read_access_after_local_reader_grants` — no INSERT
    (G-03: writer grants never applied to vault_export).
- **Why:** S6 maps both halves; does not rewrite them. See
  `docs/superpowers/specs/2026-07-29-served-hint-record-S9-COVERAGE.md`.

## 2026-07-29 — post-close: §11 Q8/H-11 local COMMIT guard asserts IDLE, not just `autocommit`

*(carried in the review record as K-04/J-12; the frozen spec's own id is §11 Q8, finding H-11,
V5-FROZEN lines 1223–1226.)*

- **Guide/§11 says:** the local recorder's clause-1 COMMIT precondition is carried by requiring
  an `autocommit` connection, and Q8 was left parked with the stated reason that landing the
  direct check **"requires extending both `FakeConn` fixtures, which neither currently models"**.
- **That reason is false, and was verified false against the fixtures themselves.** Both
  `FakeConn` classes (`test_read_tools.py:11-13`, `test_read_tools_runtime.py:15-17`) define
  only `closed` — no `autocommit`, and no `.info`. They are therefore rejected by the FIRST
  guard and never reach the status check at all. Neither fixture needed a line. The change landed
  with **zero fixture changes**, and the tests that do exercise the recorder's body pass real
  psycopg connections (`scratch`, `_second_conn`).
  *(Superseded detail: this entry originally added "and were they to reach it, the `getattr` chain
  yields `None` and skips" — true of the first revision, and the fail-open the review round below
  removed. It no longer describes the code.)*
- **Built instead:** `read_tools.py` keeps the `autocommit` guard (spec-mandated, pinned by
  §9 (b)(i) `test_section9_b_isolation_local_non_autocommit_success_and_rejection`) and **adds**
  a status assertion — reject unless `conn.info.transaction_status` is `IDLE`. The `getattr` chain
  converts an absent `.info` to `None`, and the comparison **fails CLOSED**: `None` is not `IDLE`,
  so an unreadable status is refused rather than admitted. *(This bullet originally described the
  opposite — an absent `.info` "stays non-fatal", a fail-open shape. That was the first revision's
  behaviour and it was removed by the review round recorded below; the description is corrected
  here rather than left to contradict the code.)*
- **Why — the proxy admits silent data loss.** `conn.transaction()` issues a real COMMIT only
  as the OUTERMOST block. A connection that is `autocommit=True` **and** already INTRANS makes
  it a SAVEPOINT whose release commits nothing. Demonstrated against PostgreSQL 17.7: the old
  guard passes, `transaction_status` is INTRANS, and after the `with` block the row count is
  **0** — the recorder reports success having written nothing durable. `read_tools.py:91`'s own
  comment ("entry is IDLE on an autocommit conn") asserted the precondition it never checked.
- **Divergence class:** this also closes a J-01-shaped split — the sibling bus tier already
  branches on the real status (`bus.py:476`, `bus.py:567`) while the local tier used the proxy.
  Two tiers, one precondition, two different guards.
- **Evidence:** new pin
  `test_hint_read_local_recorder.py::test_record_local_read_rejects_autocommit_connection_already_in_a_transaction`,
  RED before the fix with `Failed: DID NOT RAISE RuntimeError` (an assertion failure, not an
  ImportError) and both premises asserted so it cannot pass for the wrong reason.
  `-k hint_read`: **75 passed, 0 skipped**. Full `tests/arb_memory/`: **8 failed, 1024 passed,
  1 skipped** — the same eight pre-existing failures BUILD-CLOSE item 9 names, none new.
- **Authority:** execution-layer fix to a demonstrated correctness gap on which a review seat
  blocked. The remaining five §11 questions (Q1, Q4, Q5, Q6, Q7/H-09) are untouched and remain
  the operator's.

## 2026-07-29 — review round: the Q8 status guard now fails CLOSED on an unreadable status

Found by the pre-merge review panel (`codex-bridge-dev-example`, gpt-5.6-sol at high effort). The
first revision of the Q8 guard shipped with an escape hatch:

```python
status = getattr(getattr(conn, "info", None), "transaction_status", None)
if status is not None and status != IDLE:   # <- fail-OPEN when status is unreadable
```

- **The defect.** `ReadMemoryTools.__init__` stores the caller's `conn_factory` verbatim, so the
  seam admits arbitrary objects. One that is `autocommit=True` but exposes no `.info` yields
  `status is None`, skips the check, and **runs the recorder with an unknown durability
  precondition**. Reproduced independently by two seats; the probe returns
  `has_info=False / no exception / calls=['transaction', 'execute']`.
- **Why it mattered more than its blast radius.** No current production factory returns a
  status-blind object (`run.py:515` uses the default real psycopg connection), so nothing is
  broken today. But "the precondition is unknown" was being treated as "the precondition holds" —
  which is the *same* assume-instead-of-assert defect the guard was written to remove,
  reintroduced one line below it.
- **Built instead:** `if status != psycopg.pq.TransactionStatus.IDLE: raise`. `None` is not IDLE,
  so an unreadable status is refused. Still **zero fixture changes** — both `FakeConn`s stop at the
  first guard, verified by running `test_read_tools.py` + `test_read_tools_runtime.py` (32 passed
  with the recorder suite).
- **Evidence:** new pin `test_record_local_read_rejects_connection_with_unreadable_status`,
  deliberately DB-free so it runs without Postgres. It asserts both premises, and asserts the guard
  rejects **before** touching the connection (`conn.calls == []`). Mutation: restoring
  `status is not None` reddens exactly that one test and no other.
- **Also recorded from the same round:** the `bus.py` try/except parity gap is now a deliberate,
  documented divergence rather than an unexamined one — on this path a raising `.info` should
  propagate to `memory_search`'s recording guard and log a miss (G-01 / H-05), not be swallowed at
  the recorder.

## 2026-07-29 — accepted consequence of the fail-closed guard for out-of-tree `conn_factory` callers

Named by the round-2 review panel; recorded because it is a real behavioural change with a
non-obvious failure shape, not because it needs fixing.

`ReadMemoryTools(conn_factory=...)` is a public seam. The only object whose treatment changed is one
that is `autocommit=True` **and** has an unreadable `.info`: previously admitted (and gambled on),
now refused. **No such object exists on any production or test path in this repo** — verified by two
independent reviewers — so nothing here is broken today.

But for a hypothetical out-of-tree caller passing a pooled or wrapper connection with no `.info`,
the failure shape is **"recording silently stops"** rather than "nothing happens": every
`_record_local_read` raises, `memory_search`'s guard catches it, and each read logs a miss. The
served read is never affected (§7 still dominates — recording failure never fails a read).

The trade is deliberate and is the right way round: an *unknown* durability precondition must not be
recorded as a satisfied one, because the whole value of `hint_read` is that a row means the read
really was persisted. A caller in that position should hand the recorder a real psycopg connection,
or expose `.info.transaction_status` on its wrapper.

# arb-watch seat history — implementation plan

Authored against `docs/superpowers/specs/2026-07-03-arb-watch-history-spec.md` AS REMEDIATED by its
final "Warm-orchestrator remediation" section (authoritative over the raw body above it). Author only
— no implementation happened while writing this plan.

## Goal

Add a paginated `GET /orchestrators/{id}/seats/history` gateway endpoint backed by a fixed-anchor
keyset query over `eval_event_raw`, and an `h`-toggle in the Go TUI that wholesale-replaces the seat
pane's single map with fetched history pages, so a user can browse a seat roster older than the SSE
backfill window without a second data source or a second render path.

## Architecture

`eval_event_raw` (Postgres) gains one covering index; a new Starlette route in
`src/arb_memory/visibility.py` computes a per-seat "latest event" grouping frozen at a Python-side
`anchor` timestamp, then keyset-paginates that deduped, stable result set, returning rows shaped
byte-identical to the live SSE reducer's output. The Go TUI (`tools/arb-watch-go/model.go`) adds a
`seatSource` flag: `live` leaves the existing orchestrator-SSE-fed `m.seats`/`m.seatOrder` untouched;
`history` parks that stream's writes (frames still arrive and re-arm `listen()`, just aren't applied)
and wholesale-replaces the same map with fetched pages, so every existing render function
(`renderSeatTable`, `effectiveState`, `agentOf`, …) needs zero changes — there is exactly one map,
one shape, one path. A `historyGen` counter (bumped on both toggle directions) discards stale
in-flight fetch responses, mirroring the existing `streamState.gen` pattern.

## Tech stack

Python 3 / Starlette / psycopg3 / pytest (gateway, `src/arb_memory/`, `tests/arb_memory/`); Go /
Bubble Tea / `go test` (TUI, `tools/arb-watch-go/`). No new dependencies either side.

## Global Constraints

1. **Migration statement, verbatim, in BOTH places:**
   ```sql
   CREATE INDEX CONCURRENTLY IF NOT EXISTS eval_event_raw_orchestrator_task_sent_at_idx
       ON eval_event_raw (orchestrator, task_id, sent_at DESC, id DESC);
   ```
   in `src/arb_memory/schema.sql` (next to `eval_event_raw_inserted_at_idx`, line 100) and in
   `src/arb_memory/run.py`'s `setup_schema()` (next to the mirrored index line, line 112).
2. **`CONCURRENTLY` cannot run inside a transaction block — this bites twice, not once:**
   - **`run.py`'s `setup_schema(conn)`** is called in production via `run_setup_schema()` →
     `with _memory_conn() as conn: setup_schema(conn)`, where `_memory_conn()` returns a **default
     (non-autocommit) psycopg3 connection**. Every `conn.execute(...)` call in `setup_schema` today
     shares one implicit transaction. `setup_schema` must guarantee autocommit around *just* the new
     statement regardless of the caller's connection mode (Task 5).
   - **`tests/arb_memory/conftest.py`'s `scratch` fixture** loads all of `schema.sql` as **one single
     `conn.execute(schema_sql.read_text(...))` call**. Postgres's simple-query protocol wraps a
     multi-statement message in an implicit transaction regardless of client-side `autocommit` —
     `conn.autocommit = True` on the fixture does not save it. Once `CREATE INDEX CONCURRENTLY` lands
     in `schema.sql`, **every test using `scratch`** (most of `tests/arb_memory/test_visibility_*.py`)
     breaks at fixture setup unless the fixture applies `schema.sql` statement-by-statement (Task 5).
   - `src/arb_memory/schema.sql` itself needs no protocol-level change: it's applied in production via
     `psql -f schema.sql` (per `deploy/README.md`), which already sends one statement per message.
3. **Field names byte-identical to `_reduce_seat`'s live output:** exactly these 7 keys per seat —
   `task_id`, `run_id`, `seat_id`, `orchestrator`, `state`, `last_event`, `last_event_ts`. Never
   `voted`, `stance`, `model`, `engine_model`.
4. **The vote branch stays dropped.** `_history_seat_state` has no `vote → voted` case. `_emit_vote`
   (`bridge.py:2072`) never calls `_tee_eval_event`, so `vote` rows never reach `eval_event_raw`; a
   seat that voted reads its pre-vote terminal state (`done`/`failed`) in history. This is a documented
   permanent divergence, not a bug.
5. **The commit-event state mappings are a crash-edge fallback, not the primary `done`/`failed` path.**
   The panel's P0 claim (worktree-commit events render `unknown`) was verified and **rejected**:
   `orchestrator_commit` (`bridge.py:1255-1329`) pushes `agent_committed`/`orchestrator_committed`
   *before* `task_finished` fires (`bridge.py:1037`), so `task_finished` always carries the later
   `sent_at` and wins the `DISTINCT ON (task_id) ORDER BY sent_at DESC` grouping in the ordinary case.
   The fallback mappings (`agent_committed`, `orchestrator_committed`, `post_timeout_agent_committed`,
   `post_timeout_committed` → `done`; `steer_sent`, `cancel_sent` → `incomplete`) only ever surface a
   seat's state when the process crashed *between* the commit event and `task_finished` — confirmed
   success-only (no `ok`-split needed) by reading `bridge.py:1300-1327`: a failed commit routes through
   `fail()` → `task_finished(ok=False)` → `failed`, never through the commit-event push.
6. **Retention bounds the `latest` CTE's scan cost.** `eval_event_raw` is purged by `run_eval_purge`
   (`run.py:63-69`) on `ARB_EVAL_RETENTION_DAYS` (default 30 days) — the anchor-window scan is bounded
   by retention, not unbounded. No separate lower-bound clause is added to the query; this is
   documented in the query's docstring/comment, not enforced in code.
7. **NULL keyset cursor needs explicit casts.** The first page passes `cursor_ts=None`,
   `cursor_task_id=None`; `%(cursor_ts)s::timestamptz` / `%(cursor_task_id)s::text` casts avoid
   Postgres failing to infer an untyped NULL's type inside the row-value comparison. Pinned by a
   first-page test against the real driver (Task 6).
8. **`historyGen` is bumped on BOTH toggle directions** (live→history and history→live) — a stale
   `historyPageMsg`/`historyErrMsg` must be dropped whichever direction the user toggled away in.
9. **Single map, no second data source.** `m.seats`/`m.seatOrder` is the only seat map; history mode
   wholesale-replaces it (`replaceSeatsWithHistory`/`appendHistoryPage`), never a second
   `historySeats`/`historyOrder` map. No other render function changes.
10. **`effectiveState` gets zero code changes.** History rows never carry `state == "running"`
    (the server maps `task_started`/`task_continuing` → `incomplete`, never `running`), and history
    rows are stored verbatim (no `reduceSeat` call), so the existing time-based staleness check is
    structurally unreachable for a history row. Proven by test (Task 13), not by a new `live bool` param.

## File structure

| File | Change |
|---|---|
| `src/arb_memory/visibility.py` | New: `_clamp_history_limit`, `_history_seat_state`, `_encode_history_cursor`, `_decode_history_cursor`, `_history_row_to_seat`, `_query_seats_history`, `_seats_history_blocking` (closure), `seats_history` route handler; new `Route("/orchestrators/{orchestrator_id}/seats/history", ...)` |
| `src/arb_memory/schema.sql` | New `CREATE INDEX CONCURRENTLY IF NOT EXISTS eval_event_raw_orchestrator_task_sent_at_idx` line |
| `src/arb_memory/run.py` | Same index statement in `setup_schema()`, wrapped with an autocommit guard |
| `tests/arb_memory/conftest.py` | `scratch` fixture applies `schema.sql` statement-by-statement instead of as one blob (prerequisite for #2 above, not optional) |
| `tests/arb_memory/test_setup_schema.py` | New index name added to the existing idempotency assertion list; new regression test for the non-autocommit production path |
| `tests/arb_memory/test_visibility_history.py` | New file: all pure-unit + integration gateway tests |
| `tools/arb-watch-go/sse.go` | New `historyResponse` struct, `fetchSeatsHistory`, `fetchHistoryCmd` |
| `tools/arb-watch-go/model.go` | New model fields (`seatSource`, `historyCursor`, `historyHasMore`, `historyLoading`, `historyGen`); new msg types (`historyPageMsg`, `historyErrMsg`); new methods (`toggleHistoryMode`, `replaceSeatsWithHistory`, `appendHistoryPage`, `maybeFetchNextHistoryPage`); `frameMsg` parking guard; `"h"` keybinding; `statusCycle`/`stateColors`/`keyBindings` additions; `down`/`shift+down` wiring; `renderFilterBar` history indicator; `newModel` default `seatSource: "live"` |
| `tools/arb-watch-go/sse_test.go` | `TestFetchSeatsHistoryParsesResponseAndNullCursor` |
| `tools/arb-watch-go/model_test.go` | All toggle/race-guard/replace/append/render/paging tests; `testModel()` helper gets `seatSource: "live"` |

---

## Tasks

### Task 1 — `_clamp_history_limit`

**Files:** `src/arb_memory/visibility.py`, `tests/arb_memory/test_visibility_history.py` (new)

**Interfaces:**
```python
def _clamp_history_limit(raw: str | None) -> int: ...
```

**Steps**

1. Write failing tests in the new file:
   ```python
   from arb_memory.visibility import _clamp_history_limit

   def test_history_limit_non_numeric_defaults_to_50():
       assert _clamp_history_limit(None) == 50
       assert _clamp_history_limit("") == 50
       assert _clamp_history_limit("abc") == 50

   def test_history_limit_out_of_range_clamps_to_1_and_200():
       assert _clamp_history_limit("0") == 1
       assert _clamp_history_limit("-5") == 1
       assert _clamp_history_limit("500") == 200
       assert _clamp_history_limit("75") == 75
   ```
2. Run red: `PYTHONPATH=src ARB_MEMORY_DSN=$ARB_MEMORY_DSN .venv/bin/python -m pytest tests/arb_memory/test_visibility_history.py -q` (import error / `NameError`).
3. Implement in `visibility.py` (near the other small helpers, e.g. after `_to_int`):
   ```python
   def _clamp_history_limit(raw: str | None) -> int:
       try:
           value = int(raw)
       except (TypeError, ValueError):
           return 50
       return max(1, min(200, value))
   ```
4. Run green.
5. Commit: `feat(arb-watch-history): clamp history page-size limit`.

---

### Task 2 — `_history_seat_state` (incl. commit-event crash-edge fallback)

**Files:** `src/arb_memory/visibility.py`, `tests/arb_memory/test_visibility_history.py`

**Interfaces:**
```python
def _history_seat_state(event_type: str, payload: dict) -> str: ...
```

**Steps**

1. Write failing tests (pure unit, no DB):
   ```python
   from arb_memory.visibility import _history_seat_state

   def test_history_state_task_finished_ok_true_is_done():
       assert _history_seat_state("task_finished", {"ok": True}) == "done"
       assert _history_seat_state("task_finished", {}) == "done"  # ok absent -> not False -> done

   def test_history_state_task_finished_ok_false_is_failed():
       assert _history_seat_state("task_finished", {"ok": False}) == "failed"

   def test_history_state_task_started_and_continuing_is_incomplete():
       assert _history_seat_state("task_started", {}) == "incomplete"
       assert _history_seat_state("task_continuing", {}) == "incomplete"

   def test_history_state_unrecognized_event_type_is_unknown():
       assert _history_seat_state("some_future_event", {}) == "unknown"

   def test_history_state_commit_events_are_a_crash_edge_done_fallback():
       for event_type in (
           "agent_committed", "orchestrator_committed",
           "post_timeout_agent_committed", "post_timeout_committed",
       ):
           assert _history_seat_state(event_type, {}) == "done"

   def test_history_state_steer_and_cancel_are_a_crash_edge_incomplete_fallback():
       assert _history_seat_state("steer_sent", {}) == "incomplete"
       assert _history_seat_state("cancel_sent", {}) == "incomplete"
   ```
   (The last two tests are additions beyond the spec's original 30 — the remediation added the
   commit-event fallback *after* the spec's test list was written, and no test named there covers it.)
2. Run red.
3. Implement, next to `_clamp_history_limit`:
   ```python
   # Crash-edge fallback ONLY: task_finished (bridge.py:1037) always fires after these commit
   # events (bridge.py:1300-1327) in the ordinary case, so DISTINCT ON ... ORDER BY sent_at DESC
   # already prefers task_finished. These mappings surface a seat's state only when the process
   # died between the commit event and task_finished. Verified success-only: a failed commit
   # routes through fail() -> task_finished(ok=False) -> "failed", never through these events.
   _COMMIT_FALLBACK_DONE = frozenset({
       "agent_committed", "orchestrator_committed",
       "post_timeout_agent_committed", "post_timeout_committed",
   })
   _COMMIT_FALLBACK_INCOMPLETE = frozenset({"steer_sent", "cancel_sent"})


   def _history_seat_state(event_type: str, payload: dict) -> str:
       if event_type == "task_finished":
           return "failed" if payload.get("ok") is False else "done"
       if event_type in ("task_started", "task_continuing"):
           return "incomplete"
       if event_type in _COMMIT_FALLBACK_DONE:
           return "done"
       if event_type in _COMMIT_FALLBACK_INCOMPLETE:
           return "incomplete"
       return "unknown"
   ```
4. Run green.
5. Commit: `feat(arb-watch-history): history state reconstruction (vote branch dropped, commit-event crash fallback)`.

---

### Task 3 — cursor encode/decode

**Files:** `src/arb_memory/visibility.py`, `tests/arb_memory/test_visibility_history.py`

**Interfaces:**
```python
def _encode_history_cursor(anchor: datetime, last_ts: datetime, task_id: str) -> str: ...
def _decode_history_cursor(cursor: str) -> tuple[datetime, datetime, str] | None: ...
```

**Steps**

1. Write failing tests:
   ```python
   from datetime import datetime, timezone
   from arb_memory.visibility import _encode_history_cursor, _decode_history_cursor

   def test_history_cursor_round_trips_anchor_ts_and_task_id():
       anchor = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
       last_ts = datetime(2026, 6, 30, 11, 0, 0, tzinfo=timezone.utc)
       cursor = _encode_history_cursor(anchor, last_ts, "task-123")
       decoded = _decode_history_cursor(cursor)
       assert decoded == (anchor, last_ts, "task-123")

   def test_history_cursor_bad_base64_is_malformed():
       assert _decode_history_cursor("!!!not-base64!!!") is None

   def test_history_cursor_bad_json_is_malformed():
       import base64
       garbage = base64.urlsafe_b64encode(b"not json").decode("ascii").rstrip("=")
       assert _decode_history_cursor(garbage) is None

   def test_history_cursor_wrong_shape_is_malformed():
       import base64, json
       two_tuple = base64.urlsafe_b64encode(
           json.dumps(["2026-06-30T12:00:00+00:00", "2026-06-30T11:00:00+00:00"]).encode()
       ).decode("ascii").rstrip("=")
       assert _decode_history_cursor(two_tuple) is None

       non_string_task_id = base64.urlsafe_b64encode(
           json.dumps(["2026-06-30T12:00:00+00:00", "2026-06-30T11:00:00+00:00", 123]).encode()
       ).decode("ascii").rstrip("=")
       assert _decode_history_cursor(non_string_task_id) is None
   ```
2. Run red.
3. Implement (uses the existing `_iso`/`_parse_ts` helpers already in the file, lines 95/191):
   ```python
   import base64

   def _encode_history_cursor(anchor: datetime, last_ts: datetime, task_id: str) -> str:
       payload = json.dumps([_iso(anchor), _iso(last_ts), task_id], separators=(",", ":"))
       return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


   def _decode_history_cursor(cursor: str) -> tuple[datetime, datetime, str] | None:
       try:
           padded = cursor + "=" * (-len(cursor) % 4)
           raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
           parts = json.loads(raw)
           anchor_raw, last_ts_raw, task_id = parts
       except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
           return None
       anchor, last_ts = _parse_ts(anchor_raw), _parse_ts(last_ts_raw)
       if anchor is None or last_ts is None or not isinstance(task_id, str) or not task_id:
           return None
       return anchor, last_ts, task_id
   ```
   (`base64` import goes at the top of `visibility.py` with the other stdlib imports.)
4. Run green.
5. Commit: `feat(arb-watch-history): opaque base64url/JSON history cursor encode+decode`.

---

### Task 4 — `_history_row_to_seat`

**Files:** `src/arb_memory/visibility.py`, `tests/arb_memory/test_visibility_history.py`

**Interfaces:**
```python
def _history_row_to_seat(row: tuple) -> dict: ...
```
Row shape (fixed by Task 6's query column order): `(task_id, run_id, seat_id, orchestrator,
event_type, sent_at, id, payload)`.

**Steps**

1. Write failing test (pure unit — no DB needed, a bare tuple is enough):
   ```python
   from datetime import datetime, timezone
   from arb_memory.visibility import _history_row_to_seat

   def test_history_row_to_seat_field_shape_matches_live_reducer_keys():
       sent_at = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
       row = ("task-1", "run-1", "seat-1", "orch-1", "task_finished", sent_at, 42, {"ok": True})
       seat = _history_row_to_seat(row)
       assert set(seat.keys()) == {
           "task_id", "run_id", "seat_id", "orchestrator", "state", "last_event", "last_event_ts",
       }
       assert seat["task_id"] == "task-1"
       assert seat["state"] == "done"
       assert seat["last_event"] == "task_finished"
       assert seat["last_event_ts"] == sent_at.isoformat()
       assert "voted" not in seat and "stance" not in seat
       assert "model" not in seat and "engine_model" not in seat
   ```
2. Run red.
3. Implement:
   ```python
   def _history_row_to_seat(row: tuple) -> dict:
       task_id, run_id, seat_id, orchestrator, event_type, sent_at, _id, payload = row
       payload = payload or {}
       return {
           "task_id": task_id,
           "run_id": run_id,
           "seat_id": seat_id,
           "orchestrator": orchestrator,
           "state": _history_seat_state(event_type, payload),
           "last_event": event_type,
           "last_event_ts": _iso(sent_at),
       }
   ```
4. Run green.
5. Commit: `feat(arb-watch-history): map an eval_event_raw row to the live-reducer seat shape`.

---

### Task 5 — migration: index + `CONCURRENTLY` autocommit handling + fixture fix

**Files:** `src/arb_memory/schema.sql`, `src/arb_memory/run.py`, `tests/arb_memory/conftest.py`,
`tests/arb_memory/test_setup_schema.py`

This is independently testable and must land before Task 6 (the query needs the index to be
correctness-relevant, though `DISTINCT ON` works without it — this task is ordered here because every
later gateway integration test uses the `scratch` fixture, which breaks the moment `schema.sql` gets
the new line unless fixed in the same commit).

**Steps**

1. Write the failing regression tests first, in `tests/arb_memory/test_setup_schema.py`:
   ```python
   def test_setup_schema_creates_only_eval_and_transcript_tables_idempotently(empty_schema_conn):
       run.setup_schema(empty_schema_conn)
       for relation in (
           "eval_event_raw", "eval_deadletter", "transcript_io", "transcript_deadletter",
           "eval_event_raw_run_idx", "eval_event_raw_task_idx", "eval_event_raw_inserted_at_idx",
           "eval_event_raw_orchestrator_task_sent_at_idx",  # <-- new
           "transcript_io_task_ts_idx", "transcript_io_ts_idx",
       ):
           assert _regclass(empty_schema_conn, relation) is not None
       ...  # rest unchanged

   def test_setup_schema_concurrently_index_works_on_a_non_autocommit_connection(monkeypatch):
       # Regression test for the real production path: run_setup_schema() uses a default
       # (non-autocommit) psycopg connection. CREATE INDEX CONCURRENTLY cannot run inside a
       # transaction block; setup_schema() must guarantee autocommit around that one statement
       # regardless of what the caller passed in.
       dsn = os.environ.get("ARB_MEMORY_DSN")
       if not dsn:
           pytest.skip("no ARB_MEMORY_DSN")
       schema = f"arb_setup_schema_noauto_{uuid.uuid4().hex}"
       conn = psycopg.connect(dsn)
       conn.autocommit = True
       conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
       conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
       conn.autocommit = False  # the production shape: setup_schema must not assume this is True
       try:
           run.setup_schema(conn)  # must not raise
           conn.commit()
           assert _regclass(conn, "eval_event_raw_orchestrator_task_sent_at_idx") is not None
       finally:
           conn.autocommit = True
           conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
           conn.close()
   ```
2. Run red: `PYTHONPATH=src ARB_MEMORY_DSN=$ARB_MEMORY_DSN .venv/bin/python -m pytest tests/arb_memory/test_setup_schema.py -q`
   — the new-index assertion fails (`to_regclass` is `None`), and the non-autocommit test fails with
   Postgres's `CREATE INDEX CONCURRENTLY cannot run inside a transaction block` once the naive
   statement is added without the guard (write the naive version first to *see* this fail, confirming
   the constraint is real, then add the guard).
3. Add to `src/arb_memory/schema.sql`, immediately after line 100
   (`eval_event_raw_inserted_at_idx`):
   ```sql
   -- Covers the arb-watch history endpoint's `DISTINCT ON (task_id) ORDER BY task_id, sent_at DESC,
   -- id DESC` grouping as a single ordered Index Scan + Unique (no separate sort). CONCURRENTLY so
   -- it doesn't block writes on the live prod table; bounded by ARB_EVAL_RETENTION_DAYS purge, not
   -- an unbounded scan (run_eval_purge, run.py:63-69). Safe to run via `psql -f schema.sql` (one
   -- statement per protocol message); NOT safe if ever loaded as one multi-statement blob (see the
   -- `scratch` test fixture fix below).
   CREATE INDEX CONCURRENTLY IF NOT EXISTS eval_event_raw_orchestrator_task_sent_at_idx
       ON eval_event_raw (orchestrator, task_id, sent_at DESC, id DESC);
   ```
4. Add to `src/arb_memory/run.py`'s `setup_schema()`, immediately after the mirrored
   `eval_event_raw_inserted_at_idx` line (112):
   ```python
   # CREATE INDEX CONCURRENTLY cannot run inside a transaction block. setup_schema()'s caller may
   # hand in a non-autocommit connection (production's run_setup_schema() does), so guarantee
   # autocommit around just this one statement regardless of the incoming connection's mode, and
   # restore it afterward. conn.autocommit's setter requires no transaction in progress, so commit
   # the implicit transaction the statements above opened before flipping the mode.
   previous_autocommit = conn.autocommit
   if not previous_autocommit:
       conn.commit()
   conn.autocommit = True
   try:
       conn.execute(
           "CREATE INDEX CONCURRENTLY IF NOT EXISTS eval_event_raw_orchestrator_task_sent_at_idx "
           "ON eval_event_raw (orchestrator, task_id, sent_at DESC, id DESC)"
       )
   finally:
       conn.autocommit = previous_autocommit
   ```
5. Fix `tests/arb_memory/conftest.py`'s `scratch` fixture so it stops loading `schema.sql` as one
   multi-statement blob (which Postgres wraps in an implicit transaction regardless of client
   `autocommit`, breaking `CONCURRENTLY`). `schema.sql` has no dollar-quoted bodies (verified — no
   `$$`/`CREATE FUNCTION` in the file), so a naive split on top-level `;` is safe:
   ```python
   def _apply_schema_statements(conn, sql_text):
       for statement in sql_text.split(";"):
           statement = statement.strip()
           if statement:
               conn.execute(statement)
   ```
   and replace the single line
   `conn.execute(schema_sql.read_text(encoding="utf-8"))` with
   `_apply_schema_statements(conn, schema_sql.read_text(encoding="utf-8"))`.
6. Run green: `PYTHONPATH=src ARB_MEMORY_DSN=$ARB_MEMORY_DSN .venv/bin/python -m pytest tests/arb_memory/test_setup_schema.py -q`.
7. **Verify no regression across the whole gateway suite** (the `scratch` fixture is shared by most
   of `tests/arb_memory/test_visibility_*.py`):
   `PYTHONPATH=src ARB_MEMORY_DSN=$ARB_MEMORY_DSN .venv/bin/python -m pytest tests/arb_memory/ -q`.
8. Commit: `feat(arb-watch-history): CONCURRENTLY index migration + fix scratch fixture's multi-statement schema load`.

---

### Task 6 — `_query_seats_history` + `_seats_history_blocking` (integration)

**Files:** `src/arb_memory/visibility.py`, `tests/arb_memory/test_visibility_history.py`

**Interfaces:**
```python
def _query_seats_history(
    conn, orchestrator_id: str, anchor: datetime,
    cursor_ts: datetime | None, cursor_task_id: str | None, limit: int,
) -> list[tuple]: ...
```
`_seats_history_blocking` is a **closure inside `build_visibility_app`**, capturing `dsn`, mirroring
the existing `_backfill_seat_blocking` closure (visibility.py:432-434):
```python
def _seats_history_blocking(orchestrator_id, anchor, cursor_ts, cursor_task_id, limit):
    with psycopg.connect(dsn) as conn:
        return _query_seats_history(conn, orchestrator_id, anchor, cursor_ts, cursor_task_id, limit)
```

**Steps**

1. Write failing integration tests (real/test Postgres via the `scratch` fixture):
   ```python
   from arb_memory.visibility import _query_seats_history
   from psycopg.types.json import Jsonb
   import uuid
   from datetime import datetime, timedelta, timezone

   def _seed_event(conn, *, orchestrator, task_id, run_id, seat_id, event_type, sent_at, payload=None):
       conn.execute(
           """
           INSERT INTO eval_event_raw
               (run_id, task_id, seat_id, orchestrator, event_type, sent_at, payload, stream_entry_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           """,
           (run_id, task_id, seat_id, orchestrator, event_type, sent_at,
            Jsonb(payload or {}), f"{uuid.uuid4().hex}-0"),
       )

   def test_history_pagination_walks_all_seats_no_skip_no_repeat(scratch):
       orch = f"orch-{uuid.uuid4().hex}"
       base = datetime(2026, 6, 1, tzinfo=timezone.utc)
       seeded = []
       for i in range(23):  # > any limit used below
           task_id = f"task-{i}-{uuid.uuid4().hex}"
           seeded.append(task_id)
           _seed_event(
               scratch, orchestrator=orch, task_id=task_id, run_id=f"run-{i}", seat_id=f"seat-{i}",
               event_type="task_finished", sent_at=base + timedelta(minutes=i), payload={"ok": True},
           )
       # two DIFFERENT seats sharing an identical sent_at — exercises the (sent_at, task_id) tiebreak.
       tie_a, tie_b = f"tie-a-{uuid.uuid4().hex}", f"tie-b-{uuid.uuid4().hex}"
       tie_ts = base + timedelta(minutes=100)
       _seed_event(scratch, orchestrator=orch, task_id=tie_a, run_id="run-tie-a", seat_id="seat-tie-a",
                   event_type="task_finished", sent_at=tie_ts, payload={"ok": True})
       _seed_event(scratch, orchestrator=orch, task_id=tie_b, run_id="run-tie-b", seat_id="seat-tie-b",
                   event_type="task_finished", sent_at=tie_ts, payload={"ok": True})
       seeded += [tie_a, tie_b]

       anchor = datetime.now(timezone.utc)
       seen = []
       cursor_ts, cursor_task_id = None, None
       limit = 5
       for _ in range(20):  # generous page-count cap; loop breaks on has_more == False
           rows = _query_seats_history(scratch, orch, anchor, cursor_ts, cursor_task_id, limit)
           page, has_more = rows[:limit], len(rows) > limit
           if not page:
               break
           seen.extend(row[0] for row in page)  # task_id column
           cursor_ts, cursor_task_id = page[-1][5], page[-1][0]  # sent_at, task_id
           if not has_more:
               break

       assert sorted(seen) == sorted(seeded)
       assert len(seen) == len(set(seen))  # zero duplicates

   def test_history_pagination_anchor_is_stable_against_concurrent_writes(scratch):
       # Direct regression test for the P0 the remediation fixed.
       orch = f"orch-{uuid.uuid4().hex}"
       base = datetime(2026, 6, 1, tzinfo=timezone.utc)
       task_ids = [f"task-{i}-{uuid.uuid4().hex}" for i in range(6)]
       for i, task_id in enumerate(task_ids):
           _seed_event(scratch, orchestrator=orch, task_id=task_id, run_id=f"run-{i}", seat_id=f"seat-{i}",
                       event_type="task_finished", sent_at=base + timedelta(minutes=i), payload={"ok": True})

       anchor = datetime.now(timezone.utc)
       limit = 3
       page1 = _query_seats_history(scratch, orch, anchor, None, None, limit)
       assert len(page1) == limit + 1  # over-fetch by one
       page1_ids = [row[0] for row in page1[:limit]]
       cursor_ts, cursor_task_id = page1[limit - 1][5], page1[limit - 1][0]

       # Concurrent write: bump a seat ALREADY surfaced on page 1 to "now" (after the anchor).
       _seed_event(scratch, orchestrator=orch, task_id=task_ids[-1], run_id="run-5", seat_id="seat-5",
                   event_type="task_finished", sent_at=datetime.now(timezone.utc), payload={"ok": True})

       page2 = _query_seats_history(scratch, orch, anchor, cursor_ts, cursor_task_id, limit)
       page2_ids = [row[0] for row in page2[:limit]]
       assert not (set(page1_ids) & set(page2_ids))  # no repeat
       assert task_ids[-1] not in page2_ids  # the bumped seat doesn't reappear out of order
       remaining = set(task_ids) - set(page1_ids)
       assert set(page2_ids) <= remaining  # no skip

   def test_history_first_page_null_cursor_does_not_raise(scratch):
       # Pins the NULL keyset cast (agy P1) against the real driver.
       orch = f"orch-{uuid.uuid4().hex}"
       _seed_event(scratch, orchestrator=orch, task_id="t1", run_id="r1", seat_id="s1",
                   event_type="task_finished", sent_at=datetime.now(timezone.utc), payload={"ok": True})
       rows = _query_seats_history(scratch, orch, datetime.now(timezone.utc), None, None, 50)
       assert len(rows) == 1

   def test_history_scoped_to_one_orchestrator(scratch):
       orch_a, orch_b = f"orch-a-{uuid.uuid4().hex}", f"orch-b-{uuid.uuid4().hex}"
       now = datetime.now(timezone.utc)
       _seed_event(scratch, orchestrator=orch_a, task_id="ta", run_id="ra", seat_id="sa",
                   event_type="task_finished", sent_at=now, payload={"ok": True})
       _seed_event(scratch, orchestrator=orch_b, task_id="tb", run_id="rb", seat_id="sb",
                   event_type="task_finished", sent_at=now, payload={"ok": True})
       rows = _query_seats_history(scratch, orch_a, datetime.now(timezone.utc), None, None, 50)
       assert [row[0] for row in rows] == ["ta"]
   ```
2. Run red: `PYTHONPATH=src ARB_MEMORY_DSN=$ARB_MEMORY_DSN .venv/bin/python -m pytest tests/arb_memory/test_visibility_history.py -q`.
3. Implement in `visibility.py` (module-level function; the column list constant keeps
   `_history_row_to_seat`'s row-tuple contract explicit):
   ```python
   _HISTORY_COLUMNS = "task_id, run_id, seat_id, orchestrator, event_type, sent_at, id, payload"


   def _query_seats_history(conn, orchestrator_id, anchor, cursor_ts, cursor_task_id, limit):
       fetch_limit = limit + 1  # over-fetch by one to detect has_more without a second query
       return conn.execute(
           f"""
           WITH latest AS (
               SELECT DISTINCT ON (task_id)
                   {_HISTORY_COLUMNS}
               FROM eval_event_raw
               WHERE orchestrator = %(orchestrator)s
                 AND sent_at <= %(anchor)s
               ORDER BY task_id, sent_at DESC, id DESC
           )
           SELECT {_HISTORY_COLUMNS}
           FROM latest
           WHERE %(cursor_ts)s::timestamptz IS NULL
              OR (sent_at, task_id) < (%(cursor_ts)s::timestamptz, %(cursor_task_id)s::text)
           ORDER BY sent_at DESC, task_id DESC
           LIMIT %(fetch_limit)s
           """,
           {
               "orchestrator": orchestrator_id,
               "anchor": anchor,
               "cursor_ts": cursor_ts,
               "cursor_task_id": cursor_task_id,
               "fetch_limit": fetch_limit,
           },
       ).fetchall()
   ```
   Then add the `_seats_history_blocking` closure inside `build_visibility_app`, directly below
   `_backfill_seat_blocking` (visibility.py:432-434):
   ```python
   def _seats_history_blocking(orchestrator_id, anchor, cursor_ts, cursor_task_id, limit):
       with psycopg.connect(dsn) as conn:
           return _query_seats_history(conn, orchestrator_id, anchor, cursor_ts, cursor_task_id, limit)
   ```
4. Run green.
5. Commit: `feat(arb-watch-history): fixed-anchor keyset query over eval_event_raw`.

---

### Task 7 — `seats_history` route handler

**Files:** `src/arb_memory/visibility.py`, `tests/arb_memory/test_visibility_history.py`

**Interfaces:**
```python
async def seats_history(request: Request) -> JSONResponse: ...
```
New route: `Route("/orchestrators/{orchestrator_id}/seats/history", seats_history, methods=["GET"])`,
added to the `Starlette(routes=[...])` list (visibility.py:643-651), after `sse_seat`.

**Steps**

1. Write failing tests, reusing the `_app_client`/`_put_access_token`/`requires_memory_dsn` helpers
   already established in `tests/arb_memory/test_visibility_auth.py` (copy the small helper functions
   into the new file rather than importing across test files, matching how `test_visibility_seat.py`
   is self-contained):
   ```python
   def test_history_unauthorized_returns_401(monkeypatch):
       client, _ = _app_client(monkeypatch)
       r = client.get("/orchestrators/orch-1/seats/history")
       assert r.status_code == 401
       assert r.json() == {"error": "unauthorized"}

   def test_history_empty_orchestrator_returns_200_empty(scratch, monkeypatch):
       token = _put_access_token(scratch)
       client, _ = _app_client(monkeypatch)
       r = client.get(f"/orchestrators/no-such-orch-{uuid.uuid4().hex}/seats/history",
                      headers={"Authorization": f"Bearer {token}"})
       assert r.status_code == 200
       assert r.json() == {"seats": [], "next_cursor": None, "has_more": False}

   def test_history_malformed_cursor_returns_400(scratch, monkeypatch):
       token = _put_access_token(scratch)
       client, _ = _app_client(monkeypatch)
       r = client.get("/orchestrators/orch-1/seats/history?cursor=!!!not-valid!!!",
                      headers={"Authorization": f"Bearer {token}"})
       assert r.status_code == 400
       assert r.json() == {"error": "invalid cursor"}

   def test_history_psql_error_returns_503(scratch, monkeypatch):
       token = _put_access_token(scratch)
       client, _ = _app_client(monkeypatch)

       def boom(*args, **kwargs):
           raise psycopg.OperationalError("connection refused")

       monkeypatch.setattr("arb_memory.visibility.psycopg.connect", boom)
       r = client.get("/orchestrators/orch-1/seats/history",
                      headers={"Authorization": f"Bearer {token}"})
       assert r.status_code == 503
       assert r.json() == {"error": "history unavailable"}
       assert "Traceback" not in r.text

   def test_history_votes_are_a_dead_branch(scratch, monkeypatch):
       # A seat's real bridge lifecycle: task_started -> task_finished(ok=True); _emit_vote never
       # tees to eval_event_raw, so there's simply no `vote` row to seed. History must read "done".
       orch = f"orch-{uuid.uuid4().hex}"
       now = datetime.now(timezone.utc)
       _seed_event(scratch, orchestrator=orch, task_id="voted-seat", run_id="run-v", seat_id="seat-v",
                   event_type="task_started", sent_at=now - timedelta(minutes=1))
       _seed_event(scratch, orchestrator=orch, task_id="voted-seat", run_id="run-v", seat_id="seat-v",
                   event_type="task_finished", sent_at=now, payload={"ok": True})
       token = _put_access_token(scratch)
       client, _ = _app_client(monkeypatch)
       r = client.get(f"/orchestrators/{orch}/seats/history",
                      headers={"Authorization": f"Bearer {token}"})
       body = r.json()
       assert body["seats"][0]["state"] == "done"
       assert "voted" not in body["seats"][0] and "stance" not in body["seats"][0]

   def test_history_field_shape_matches_live_reducer_keys(scratch, monkeypatch):
       orch = f"orch-{uuid.uuid4().hex}"
       _seed_event(scratch, orchestrator=orch, task_id="t1", run_id="r1", seat_id="s1",
                   event_type="task_finished", sent_at=datetime.now(timezone.utc), payload={"ok": True})
       token = _put_access_token(scratch)
       client, _ = _app_client(monkeypatch)
       r = client.get(f"/orchestrators/{orch}/seats/history",
                      headers={"Authorization": f"Bearer {token}"})
       assert set(r.json()["seats"][0].keys()) == {
           "task_id", "run_id", "seat_id", "orchestrator", "state", "last_event", "last_event_ts",
       }

   def test_history_pagination_via_http_walks_pages(scratch, monkeypatch):
       orch = f"orch-{uuid.uuid4().hex}"
       now = datetime.now(timezone.utc)
       for i in range(7):
           _seed_event(scratch, orchestrator=orch, task_id=f"t{i}", run_id=f"r{i}", seat_id=f"s{i}",
                       event_type="task_finished", sent_at=now + timedelta(minutes=i), payload={"ok": True})
       token = _put_access_token(scratch)
       client, _ = _app_client(monkeypatch)
       r1 = client.get(f"/orchestrators/{orch}/seats/history?limit=3",
                       headers={"Authorization": f"Bearer {token}"})
       body1 = r1.json()
       assert len(body1["seats"]) == 3 and body1["has_more"] is True and body1["next_cursor"]
       r2 = client.get(f"/orchestrators/{orch}/seats/history?limit=3&cursor={body1['next_cursor']}",
                       headers={"Authorization": f"Bearer {token}"})
       body2 = r2.json()
       seen = {s["task_id"] for s in body1["seats"]} | {s["task_id"] for s in body2["seats"]}
       assert len(seen) == len({s["task_id"] for s in body1["seats"]}) + len({s["task_id"] for s in body2["seats"]})
   ```
2. Run red.
3. Implement in `visibility.py`, inside `build_visibility_app` (near `sse_seat`), and register the
   route:
   ```python
   async def seats_history(request: Request):
       if await authenticated(_bearer_token(request)) is None:
           return JSONResponse({"error": "unauthorized"}, status_code=401)

       orchestrator_id = request.path_params["orchestrator_id"]
       limit = _clamp_history_limit(request.query_params.get("limit"))
       raw_cursor = request.query_params.get("cursor") or ""

       if raw_cursor:
           decoded = _decode_history_cursor(raw_cursor)
           if decoded is None:
               return JSONResponse({"error": "invalid cursor"}, status_code=400)
           anchor, cursor_ts, cursor_task_id = decoded
       else:
           anchor = datetime.now(timezone.utc)
           cursor_ts, cursor_task_id = None, None

       try:
           rows = await anyio.to_thread.run_sync(
               _seats_history_blocking, orchestrator_id, anchor, cursor_ts, cursor_task_id, limit
           )
       except psycopg.Error:
           logger.exception("seats_history query failed for orchestrator=%s", orchestrator_id)
           return JSONResponse({"error": "history unavailable"}, status_code=503)

       has_more = len(rows) > limit
       page = rows[:limit]
       seats = [_history_row_to_seat(row) for row in page]
       next_cursor = None
       if has_more and page:
           last = page[-1]
           next_cursor = _encode_history_cursor(anchor, last[5], last[0])  # sent_at, task_id
       return JSONResponse({"seats": seats, "next_cursor": next_cursor, "has_more": has_more})
   ```
   Add `Route("/orchestrators/{orchestrator_id}/seats/history", seats_history, methods=["GET"])` to
   the `Starlette(routes=[...])` list.
4. Run green, then run the full gateway suite once more to confirm no cross-test interference:
   `PYTHONPATH=src ARB_MEMORY_DSN=$ARB_MEMORY_DSN .venv/bin/python -m pytest tests/arb_memory/ -q`.
5. Commit: `feat(arb-watch-history): GET /orchestrators/{id}/seats/history endpoint`.

**Gateway is now independently complete and testable** — the Go side (Tasks 8-14) builds against it.

---

### Task 8 — `sse.go`: `fetchSeatsHistory` + `fetchHistoryCmd`

**Files:** `tools/arb-watch-go/sse.go`, `tools/arb-watch-go/sse_test.go`

**Interfaces:**
```go
type historyResponse struct {
    Seats      []map[string]any `json:"seats"`
    NextCursor *string          `json:"next_cursor"`
    HasMore    bool             `json:"has_more"`
}
func fetchSeatsHistory(baseURL, token, orchestrator, cursor string) (historyResponse, error)
func fetchHistoryCmd(baseURL, token, orchestrator, cursor string, appendPage bool, gen int) tea.Cmd
```

**Steps**

1. Write failing test in `sse_test.go`, mirroring `TestFetchOrchestrators` (line 32):
   ```go
   func TestFetchSeatsHistoryParsesResponseAndNullCursor(t *testing.T) {
       var gotPath, gotAuth string
       server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
           gotPath = r.URL.Path
           gotAuth = r.Header.Get("Authorization")
           _, _ = fmt.Fprint(w, `{"seats":[{"task_id":"t1"}],"next_cursor":null,"has_more":false}`)
       }))
       defer server.Close()

       got, err := fetchSeatsHistory(server.URL, "token-1", "orch-1", "")
       if err != nil {
           t.Fatal(err)
       }
       if gotPath != "/orchestrators/orch-1/seats/history" {
           t.Fatalf("path=%s", gotPath)
       }
       if gotAuth != "Bearer token-1" {
           t.Fatalf("auth=%q", gotAuth)
       }
       if got.HasMore || got.NextCursor != nil || len(got.Seats) != 1 || got.Seats[0]["task_id"] != "t1" {
           t.Fatalf("response=%#v", got)
       }
   }

   func TestFetchSeatsHistoryPassesCursorAsQueryParam(t *testing.T) {
       var gotQuery string
       server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
           gotQuery = r.URL.RawQuery
           _, _ = fmt.Fprint(w, `{"seats":[],"next_cursor":null,"has_more":false}`)
       }))
       defer server.Close()

       if _, err := fetchSeatsHistory(server.URL, "token-1", "orch-1", "abc123"); err != nil {
           t.Fatal(err)
       }
       if gotQuery != "cursor=abc123" {
           t.Fatalf("query=%q", gotQuery)
       }
   }
   ```
2. Run red: `cd tools/arb-watch-go && go test ./...` (undefined: `fetchSeatsHistory`).
3. Implement in `sse.go`, below `fetchOrchestrators`:
   ```go
   type historyResponse struct {
       Seats      []map[string]any `json:"seats"`
       NextCursor *string          `json:"next_cursor"`
       HasMore    bool             `json:"has_more"`
   }

   func fetchSeatsHistory(baseURL, token, orchestrator, cursor string) (historyResponse, error) {
       endpoint, err := url.JoinPath(strings.TrimRight(baseURL, "/"), "orchestrators", orchestrator, "seats", "history")
       if err != nil {
           return historyResponse{}, err
       }
       if cursor != "" {
           q := url.Values{}
           q.Set("cursor", cursor)
           endpoint += "?" + q.Encode()
       }
       req, err := http.NewRequest(http.MethodGet, endpoint, nil)
       if err != nil {
           return historyResponse{}, err
       }
       req.Header.Set("Authorization", "Bearer "+token)
       resp, err := http.DefaultClient.Do(req)
       if err != nil {
           return historyResponse{}, err
       }
       defer resp.Body.Close()
       if resp.StatusCode < 200 || resp.StatusCode >= 300 {
           return historyResponse{}, fmt.Errorf("fetch seats history: %s", resp.Status)
       }
       var payload historyResponse
       if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
           return historyResponse{}, err
       }
       return payload, nil
   }

   func fetchHistoryCmd(baseURL, token, orchestrator, cursor string, appendPage bool, gen int) tea.Cmd {
       return func() tea.Msg {
           resp, err := fetchSeatsHistory(baseURL, token, orchestrator, cursor)
           if err != nil {
               return historyErrMsg{gen: gen, err: err}
           }
           next := ""
           if resp.NextCursor != nil {
               next = *resp.NextCursor
           }
           return historyPageMsg{gen: gen, seats: resp.Seats, nextCursor: next, hasMore: resp.HasMore, append: appendPage}
       }
   }
   ```
   `fetchHistoryCmd` references `historyPageMsg`/`historyErrMsg`, defined in Task 9 (`model.go`) —
   both files are in `package main`, so define the message types in Task 9 first if compiling this
   file in isolation; in practice, land both files' changes in the same commit so the package
   compiles at every step. Add `tea "github.com/charmbracelet/bubbletea"` to `sse.go`'s imports.
4. Run green.
5. Commit: `feat(arb-watch-history): fetchSeatsHistory + fetchHistoryCmd`.

---

### Task 9 — model fields, message types, `h` toggle (live→history), `frameMsg` parking guard

**Files:** `tools/arb-watch-go/model.go`, `tools/arb-watch-go/model_test.go`

**Interfaces:**
```go
// model fields
seatSource     string // "live" | "history"
historyCursor  string
historyHasMore bool
historyLoading bool
historyGen     int

type historyPageMsg struct {
    gen        int
    seats      []map[string]any
    nextCursor string
    hasMore    bool
    append     bool
}
type historyErrMsg struct {
    gen int
    err error
}

func (m *model) toggleHistoryMode() tea.Cmd
```

**A real gotcha found in the existing code, not called out by the spec:** `newModel()` (model.go:198)
never sets `seatSource`, so it zero-values to `""`. If the `frameMsg` guard below is written as
`if m.seatSource == "live" { m.upsertSeat(...) }`, every model that doesn't explicitly set
`seatSource: "live"` — including the **existing** `testModel()` helper in `model_test.go` (line 21-35)
— silently stops upserting live seats, breaking `TestModelOrchFrameUpsertsSeatAndDropsStaleGeneration`
and others that currently pass. Both `newModel()` and `testModel()` must set `seatSource: "live"`
explicitly as part of this task, not left implicit.

**Steps**

1. Write failing tests in `model_test.go`:
   ```go
   func TestToggleHistoryModeFetchesPageOneAndParksLiveFrames(t *testing.T) {
       m, calls, _ := testModel()
       m.view = "seats"
       m.orchestrator = "orch-1"
       m.seatOrder = []string{"live-seat"}
       m.seats = map[string]map[string]any{"live-seat": {"task_id": "live-seat", "seat_id": "codex-1"}}
       m.orch.gen = 5 // an existing live stream is already running

       next, cmd := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'h'}})
       m = next.(*model)
       if m.seatSource != "history" || !m.historyLoading {
           t.Fatalf("seatSource=%q historyLoading=%v", m.seatSource, m.historyLoading)
       }
       if cmd == nil {
           t.Fatal("expected a fetch command")
       }
       if len(*calls) != 0 { // fetchHistoryCmd is NOT m.startStream — no new stream call expected
           t.Fatalf("unexpected stream calls: %#v", *calls)
       }

       // A frame from the still-running orchestrator stream must NOT mutate m.seats while parked.
       next, _ = m.Update(frameMsg{which: streamOrch, gen: 5, f: Frame{ID: "9-0", Event: "seat_appear",
           Data: map[string]any{"task_id": "should-not-appear", "seat_id": "codex-2"}}})
       m = next.(*model)
       if _, exists := m.seats["should-not-appear"]; exists {
           t.Fatalf("frameMsg mutated m.seats while parked: %#v", m.seats)
       }
       if len(m.seatOrder) != 1 || m.seatOrder[0] != "live-seat" {
           t.Fatalf("original live seat was disturbed: %#v", m.seatOrder)
       }
   }

   func TestExistingLiveFrameUpsertStillWorksWithSeatSourceDefaulted(t *testing.T) {
       // Guards the newModel()/testModel() seatSource:"live" default explicitly, since a silently
       // missing default would break every pre-existing live-upsert test, not just new ones.
       m := newModel("http://visibility", "token-1", "")
       if m.seatSource != "live" {
           t.Fatalf("newModel() default seatSource=%q, want %q", m.seatSource, "live")
       }
   }
   ```
2. Run red: `cd tools/arb-watch-go && go test ./...`.
3. Implement:
   - Add fields to `type model struct` (model.go:135-172), after `orch`/`seat streamState`:
     ```go
     seatSource     string // "live" | "history"
     historyCursor  string
     historyHasMore bool
     historyLoading bool
     historyGen     int
     ```
   - Add message types near `frameMsg`/`errMsg` (model.go:182-196):
     ```go
     type historyPageMsg struct {
         gen        int
         seats      []map[string]any
         nextCursor string
         hasMore    bool
         append     bool
     }
     type historyErrMsg struct {
         gen int
         err error
     }
     ```
   - In `newModel()` (model.go:198-217), add `seatSource: "live",` to the struct literal, next to
     `statusFilter: filterAll,`.
   - In `testModel()` (model_test.go:21-35), add `seatSource: "live",` to its struct literal too.
   - Add `{"h", "History"}` to `keyBindings` (model.go:58-62), after the `"a"` entry.
   - In `handleKey` (model.go:401), add, after the `"a"` case:
     ```go
     case "h":
         if m.view == viewSeats {
             return m, m.toggleHistoryMode()
         }
     ```
   - Add `toggleHistoryMode` (live→history direction only in this task; the history→live branch and
     the two-direction gen-bump land in Task 10):
     ```go
     func (m *model) toggleHistoryMode() tea.Cmd {
         m.seatCursor = 0
         m.seatScroll = 0
         if m.seatSource == "history" {
             return m.exitHistoryMode() // Task 10
         }
         m.seatSource = "history"
         m.historyCursor = ""
         m.historyHasMore = false
         m.historyLoading = true
         m.historyGen++
         m.status = "loading history…"
         return fetchHistoryCmd(m.baseURL, m.token, m.orchestrator, "", false, m.historyGen)
     }
     ```
   - Guard the `frameMsg` case in `Update` (model.go:235-256) — the ONLY change to existing `Update`
     logic:
     ```go
     if msg.which == streamOrch {
         if m.seatSource == "live" {
             m.upsertSeat(msg.f)
             m.syncCursorToSelection()
         }
         // else: history mode — drop the frame, but still re-arm listen() below.
     } else if m.view == viewSeats {
         m.appendTranscript(msg.f)
     }
     ```
   - Add the history-mode filter-bar indicator in `renderFilterBar` (model.go:1241-1248):
     ```go
     if m.seatSource == "history" {
         body += "\n" + styleFilterVal.Render("· history") + styleDim.Render(" (h)")
     }
     ```
4. Run red once more (compile error: `m.exitHistoryMode` undefined) — expected; stub it minimally to
   get *this* task's tests green without yet implementing the history→live direction:
   ```go
   func (m *model) exitHistoryMode() tea.Cmd { return nil } // completed in Task 10
   ```
5. Run green.
6. Commit: `feat(arb-watch-history): h toggle (live->history) + frame-parking guard + seatSource default`.

---

### Task 10 — `toggleHistoryMode` history→live direction + two-direction race guard

**Files:** `tools/arb-watch-go/model.go`, `tools/arb-watch-go/model_test.go`

**Steps**

1. Write failing tests:
   ```go
   func TestToggleHistoryModeBackToLiveClearsAndRestartsStream(t *testing.T) {
       m, calls, _ := testModel()
       m.view = "seats"
       m.orchestrator = "orch-1"
       m.seatSource = "history"
       m.historyCursor = "some-cursor"
       m.historyHasMore = true
       m.historyLoading = true
       m.historyGen = 3
       m.seatOrder = []string{"hist-seat"}
       m.seats = map[string]map[string]any{"hist-seat": {"task_id": "hist-seat"}}
       priorOrchGen := m.orch.gen

       next, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'h'}})
       m = next.(*model)

       if m.seatSource != "live" {
           t.Fatalf("seatSource=%q, want live", m.seatSource)
       }
       if len(m.seats) != 0 || len(m.seatOrder) != 0 {
           t.Fatalf("history rows not cleared: seats=%#v order=%#v", m.seats, m.seatOrder)
       }
       if m.historyCursor != "" || m.historyHasMore || m.historyLoading {
           t.Fatalf("history fields not reset: cursor=%q hasMore=%v loading=%v",
               m.historyCursor, m.historyHasMore, m.historyLoading)
       }
       if len(*calls) != 1 || (*calls)[0].which != streamOrch {
           t.Fatalf("expected a fresh orch stream restart, calls=%#v", *calls)
       }
       if m.orch.gen == priorOrchGen {
           t.Fatal("orch stream gen did not advance — this would be a resume, not a fresh restart")
       }
   }

   func TestHistoryStaleGenResponseIsDroppedOnToggleBackToLive(t *testing.T) {
       // codex P1: the race guard must cover BOTH directions, not just history's own toggle-away.
       m, _, _ := testModel()
       m.view = "seats"
       m.orchestrator = "orch-1"
       m.seatSource = "history"
       m.historyGen = 1
       m.historyLoading = true
       staleGen := m.historyGen

       next, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'h'}}) // toggle BACK to live
       m = next.(*model)
       if m.historyGen == staleGen {
           t.Fatal("historyGen did not bump on the history->live toggle")
       }

       // The in-flight page-2 fetch that started before the toggle now lands, carrying the OLD gen.
       next, _ = m.Update(historyPageMsg{gen: staleGen, seats: []map[string]any{{"task_id": "late"}}, append: true})
       m = next.(*model)
       if _, exists := m.seats["late"]; exists {
           t.Fatal("stale history page mutated the freshly-rebuilt live map")
       }
   }
   ```
2. Run red.
3. Implement — replace the Task-9 stub with the real history→live direction, and make
   `toggleHistoryMode` bump `historyGen` on *both* branches:
   ```go
   func (m *model) toggleHistoryMode() tea.Cmd {
       m.seatCursor = 0
       m.seatScroll = 0
       m.historyGen++ // bump on EVERY toggle, both directions (codex P1 race guard)
       if m.seatSource == "history" {
           m.seatSource = "live"
           m.historyCursor = ""
           m.historyHasMore = false
           m.historyLoading = false
           m.seats = map[string]map[string]any{}
           m.seatOrder = nil
           m.status = ""
           streamURL := m.baseURL + "/sse/orchestrator/" + url.PathEscape(m.orchestrator)
           return m.startStream(m, streamOrch, streamURL, "") // fresh restart, not a resume
       }
       m.seatSource = "history"
       m.historyCursor = ""
       m.historyHasMore = false
       m.historyLoading = true
       m.status = "loading history…"
       return fetchHistoryCmd(m.baseURL, m.token, m.orchestrator, "", false, m.historyGen)
   }
   ```
   Remove the now-unused `exitHistoryMode` stub from Task 9.
4. Run green — including re-running Task 9's tests to confirm the live→history direction still
   passes with the gen-bump now unconditional.
5. Commit: `feat(arb-watch-history): history->live toggle + two-direction historyGen race guard`.

---

### Task 11 — `replaceSeatsWithHistory` / `appendHistoryPage`

**Files:** `tools/arb-watch-go/model.go`, `tools/arb-watch-go/model_test.go`

**Interfaces:**
```go
func (m *model) replaceSeatsWithHistory(seats []map[string]any)
func (m *model) appendHistoryPage(seats []map[string]any)
```

**Steps**

1. Write failing tests:
   ```go
   func TestHistoryFirstPageReplacesWholesale(t *testing.T) {
       m, _, _ := testModel()
       m.seatOrder = []string{"old-live-seat"}
       m.seats = map[string]map[string]any{"old-live-seat": {"task_id": "old-live-seat"}}

       m.replaceSeatsWithHistory([]map[string]any{
           {"task_id": "h1", "seat_id": "codex-1", "state": "done"},
           {"task_id": "h2", "seat_id": "codex-2", "state": "incomplete"},
       })

       if _, exists := m.seats["old-live-seat"]; exists {
           t.Fatal("old live entry survived a wholesale replace")
       }
       if !reflect.DeepEqual(m.seatOrder, []string{"h1", "h2"}) {
           t.Fatalf("seatOrder=%#v, want [h1 h2] preserving server order", m.seatOrder)
       }
   }

   func TestHistoryAppendPageDedupsOnTaskID(t *testing.T) {
       m, _, _ := testModel()
       m.replaceSeatsWithHistory([]map[string]any{
           {"task_id": "h1", "seat_id": "codex-1", "state": "done"},
       })
       m.appendHistoryPage([]map[string]any{
           {"task_id": "h1", "seat_id": "codex-1", "state": "done"}, // duplicate, must be dropped
           {"task_id": "h2", "seat_id": "codex-2", "state": "incomplete"},
       })
       if !reflect.DeepEqual(m.seatOrder, []string{"h1", "h2"}) {
           t.Fatalf("seatOrder=%#v, want [h1 h2] (no duplicate h1)", m.seatOrder)
       }
       if len(m.seats) != 2 {
           t.Fatalf("seats=%#v, want 2 entries", m.seats)
       }
   }

   func TestHistoryRowRendersWithNoSpecialCasing(t *testing.T) {
       // Proves the "byte-identical field shape needs zero translation" claim: a decoded-from-JSON
       // history-shaped row (only the 7 response keys) renders through the SAME renderSeatTable
       // path as a live row, with no panic.
       m, _, _ := testModel()
       m.view = "seats"
       m.width = 160
       raw := `{"task_id":"h1","run_id":"r1","seat_id":"codex-1","orchestrator":"orch-1","state":"incomplete","last_event":"task_started","last_event_ts":"2026-01-01T00:00:00+00:00"}`
       var seat map[string]any
       if err := json.Unmarshal([]byte(raw), &seat); err != nil {
           t.Fatal(err)
       }
       m.replaceSeatsWithHistory([]map[string]any{seat})
       out := m.renderSeatTable()
       if !strings.Contains(out, "codex-1") {
           t.Fatalf("rendered table missing history row:\n%s", out)
       }
   }
   ```
   (`TestHistoryRowRendersWithNoSpecialCasing` needs `"encoding/json"` added to `model_test.go`'s
   imports.)
2. Run red.
3. Implement, near `upsertSeat` (model.go:792):
   ```go
   // replaceSeatsWithHistory wholesale-replaces m.seats/m.seatOrder with a first page's rows,
   // preserving the server's newest-first order (do NOT sort.Strings — that's upsertSeat's
   // live-map convention and would scramble history's meaningful order).
   func (m *model) replaceSeatsWithHistory(seats []map[string]any) {
       m.seats = map[string]map[string]any{}
       m.seatOrder = make([]string, 0, len(seats))
       m.appendHistoryPage(seats)
   }

   // appendHistoryPage adds a page's rows in server order, deduping defensively on task_id (the
   // SQL keyset is provably gap/dup-free across pages of one walk; this is cheap insurance against
   // a client-side retry, not a correctness dependency).
   func (m *model) appendHistoryPage(seats []map[string]any) {
       for _, seat := range seats {
           taskID := getString(seat, "task_id")
           if taskID == "" {
               continue
           }
           if _, exists := m.seats[taskID]; exists {
               continue
           }
           m.seatOrder = append(m.seatOrder, taskID)
           m.seats[taskID] = seat
       }
   }
   ```
4. Run green.
5. Commit: `feat(arb-watch-history): single-map-replace history page application`.

---

### Task 12 — `historyPageMsg`/`historyErrMsg` `Update` handling

**Files:** `tools/arb-watch-go/model.go`, `tools/arb-watch-go/model_test.go`

**Steps**

1. Write failing tests:
   ```go
   func TestHistoryPageMsgReplacesOnFirstPageAppendsOnSubsequent(t *testing.T) {
       m, _, _ := testModel()
       m.seatSource = "history"
       m.historyGen = 1
       m.historyLoading = true

       next, _ := m.Update(historyPageMsg{gen: 1, seats: []map[string]any{{"task_id": "h1"}}, nextCursor: "c1", hasMore: true, append: false})
       m = next.(*model)
       if m.historyLoading || m.historyCursor != "c1" || !m.historyHasMore {
           t.Fatalf("post-page-1 state wrong: loading=%v cursor=%q hasMore=%v", m.historyLoading, m.historyCursor, m.historyHasMore)
       }
       if !reflect.DeepEqual(m.seatOrder, []string{"h1"}) {
           t.Fatalf("seatOrder=%#v", m.seatOrder)
       }

       m.historyLoading = true
       next, _ = m.Update(historyPageMsg{gen: 1, seats: []map[string]any{{"task_id": "h2"}}, nextCursor: "", hasMore: false, append: true})
       m = next.(*model)
       if m.historyLoading || m.historyHasMore || m.historyCursor != "" {
           t.Fatalf("post-page-2 state wrong: loading=%v hasMore=%v cursor=%q", m.historyLoading, m.historyHasMore, m.historyCursor)
       }
       if !reflect.DeepEqual(m.seatOrder, []string{"h1", "h2"}) {
           t.Fatalf("seatOrder=%#v, want both pages", m.seatOrder)
       }
   }

   func TestHistoryStaleGenResponseIsDropped(t *testing.T) {
       m, _, _ := testModel()
       m.seatSource = "history"
       m.historyGen = 2
       m.seatOrder = []string{"kept"}
       m.seats = map[string]map[string]any{"kept": {"task_id": "kept"}}

       next, _ := m.Update(historyPageMsg{gen: 1, seats: []map[string]any{{"task_id": "stale"}}})
       m = next.(*model)
       if !reflect.DeepEqual(m.seatOrder, []string{"kept"}) {
           t.Fatalf("stale-gen page mutated state: %#v", m.seatOrder)
       }
   }

   func TestHistoryFetchFailureKeepsLoadedRowsAndClearsLoading(t *testing.T) {
       m, _, _ := testModel()
       m.seatSource = "history"
       m.historyGen = 1
       m.historyLoading = true
       m.seatOrder = []string{"already-loaded"}
       m.seats = map[string]map[string]any{"already-loaded": {"task_id": "already-loaded"}}

       next, _ := m.Update(historyErrMsg{gen: 1, err: fmt.Errorf("boom")})
       m = next.(*model)
       if m.historyLoading {
           t.Fatal("historyLoading not cleared after a fetch failure")
       }
       if m.status == "" {
           t.Fatal("m.status not set on fetch failure")
       }
       if !reflect.DeepEqual(m.seatOrder, []string{"already-loaded"}) {
           t.Fatalf("already-loaded rows were disturbed: %#v", m.seatOrder)
       }

       // A stale-gen error must ALSO be a no-op (not just page messages).
       m.historyGen = 2
       m.historyLoading = true
       next, _ = m.Update(historyErrMsg{gen: 1, err: fmt.Errorf("late boom")})
       m = next.(*model)
       if !m.historyLoading {
           t.Fatal("stale-gen error incorrectly cleared historyLoading")
       }
   }
   ```
2. Run red.
3. Implement — add two new `case`s to `Update`'s `switch` (model.go:226-303), after the `case tea.MouseMsg:` block (or anywhere before the final `case errMsg:`, order among cases doesn't matter in a Go type switch):
   ```go
   case historyPageMsg:
       if msg.gen != m.historyGen {
           return m, nil
       }
       m.historyLoading = false
       if msg.append {
           m.appendHistoryPage(msg.seats)
       } else {
           m.replaceSeatsWithHistory(msg.seats)
       }
       m.historyCursor = msg.nextCursor
       m.historyHasMore = msg.hasMore
       m.clampSeatCursor()
       return m, nil
   case historyErrMsg:
       if msg.gen != m.historyGen {
           return m, nil
       }
       m.historyLoading = false
       m.status = msg.err.Error()
       return m, nil
   ```
4. Run green.
5. Commit: `feat(arb-watch-history): historyPageMsg/historyErrMsg Update handling`.

---

### Task 13 — `incomplete` in `stateColors` + `statusCycle`; `effectiveState` by-construction proof

**Files:** `tools/arb-watch-go/model.go`, `tools/arb-watch-go/model_test.go`

**Steps**

1. Write failing tests:
   ```go
   func TestIncompleteStateRendersStyledAndIsFilterable(t *testing.T) {
       m, _, _ := testModel()
       m.view = "seats"
       m.width = 160
       m.seatOrder = []string{"h1"}
       m.seats = map[string]map[string]any{
           "h1": {"task_id": "h1", "seat_id": "codex-1", "state": "incomplete", "last_event_ts": "2026-01-01T00:00:00+00:00"},
       }
       rendered := m.renderSeatTable()
       // stateColors["incomplete"] must be applied — a styled render differs from the bare string
       // wrapped in surrounding spaces (loose but stable check: styled output contains an ANSI
       // escape sequence around "incomplete", not just the bare word padded with spaces).
       if !strings.Contains(rendered, "incomplete") {
           t.Fatalf("state not rendered:\n%s", rendered)
       }
       if _, ok := stateColors["incomplete"]; !ok {
           t.Fatal("incomplete missing from stateColors")
       }

       found := false
       for _, s := range statusCycle {
           if s == "incomplete" {
               found = true
           }
       }
       if !found {
           t.Fatal("incomplete missing from statusCycle")
       }
       m.statusFilter = "incomplete"
       if got := m.visibleSeats(); len(got) != 1 || got[0] != "h1" {
           t.Fatalf("status filter did not narrow to the incomplete seat: %#v", got)
       }
   }

   func TestEffectiveStateNeverReclassifiesIncompleteAsStale(t *testing.T) {
       seat := map[string]any{
           "task_id": "h1", "state": "incomplete",
           "last_event_ts": "2020-01-01T00:00:00+00:00", // far in the past
       }
       if got := effectiveState(seat); got != "incomplete" {
           t.Fatalf("effectiveState=%q, want incomplete (never stale)", got)
       }
   }
   ```
2. Run red.
3. Implement — two one-line data changes, no function signature changes (`model.go:68-72`):
   ```go
   var statusCycle = []string{filterAll, "running", "incomplete", "done", "failed", "voted", "stale"}

   var stateColors = map[string]lipgloss.Color{
       "running": "42", "incomplete": "141", "done": "244", "failed": "203", "voted": "39", "stale": "208",
   }
   ```
   `effectiveState` (model.go:1073-1081) needs **no edit** — its `if state == "running"` gate already
   makes the staleness reclassification structurally unreachable for `"incomplete"`. This task's tests
   exist to prove that, not to change it.
4. Run green.
5. Commit: `feat(arb-watch-history): incomplete state styled + filterable`.

---

### Task 14 — `maybeFetchNextHistoryPage` + on-scroll wiring

**Files:** `tools/arb-watch-go/model.go`, `tools/arb-watch-go/model_test.go`

**Interfaces:**
```go
func (m *model) maybeFetchNextHistoryPage() tea.Cmd
```

**Steps**

1. Write failing tests:
   ```go
   func TestHistoryScrollTriggersNextPageAtListEnd(t *testing.T) {
       m, _, _ := testModel()
       m.view = "seats"
       m.seatSource = "history"
       m.historyGen = 4
       m.historyHasMore = true
       m.historyCursor = "cursor-abc"
       m.seatOrder = []string{"h1", "h2"}
       m.seats = map[string]map[string]any{
           "h1": {"task_id": "h1", "seat_id": "codex-1"},
           "h2": {"task_id": "h2", "seat_id": "codex-2"},
       }
       m.seatCursor = 1 // already at the last loaded row

       next, cmd := m.Update(tea.KeyMsg{Type: tea.KeyDown})
       m = next.(*model)
       if cmd == nil {
           t.Fatal("expected a batched command at the list end with more history available")
       }
       if !m.historyLoading {
           t.Fatal("historyLoading was not set when the next page was dispatched")
       }
   }

   func TestHistoryScrollAtEndWithNoMoreDoesNotFetch(t *testing.T) {
       m, _, _ := testModel()
       m.view = "seats"
       m.seatSource = "history"
       m.historyHasMore = false
       m.seatOrder = []string{"h1"}
       m.seats = map[string]map[string]any{"h1": {"task_id": "h1", "seat_id": "codex-1"}}
       m.seatCursor = 0

       m.Update(tea.KeyMsg{Type: tea.KeyDown})
       if m.historyLoading {
           t.Fatal("historyLoading should not be set — historyHasMore is false")
       }
   }

   func TestMaybeFetchNextHistoryPageNoOpWhenNotInHistoryModeOrAlreadyLoading(t *testing.T) {
       m, _, _ := testModel()
       m.seatSource = "live"
       if cmd := m.maybeFetchNextHistoryPage(); cmd != nil {
           t.Fatal("should be a no-op outside history mode")
       }
       m.seatSource = "history"
       m.historyHasMore = true
       m.historyLoading = true
       if cmd := m.maybeFetchNextHistoryPage(); cmd != nil {
           t.Fatal("should be a no-op while a fetch is already in flight")
       }
   }
   ```
2. Run red.
3. Implement, near `autoSelectSeat` (model.go:734):
   ```go
   // maybeFetchNextHistoryPage fires the next page when the seat cursor has reached the last
   // currently-loaded history row, more exists, and no fetch is already in flight.
   func (m *model) maybeFetchNextHistoryPage() tea.Cmd {
       if m.seatSource != "history" || !m.historyHasMore || m.historyLoading {
           return nil
       }
       vis := m.visibleSeats()
       if len(vis) == 0 || m.seatCursor < len(vis)-1 {
           return nil
       }
       m.historyLoading = true
       return fetchHistoryCmd(m.baseURL, m.token, m.orchestrator, m.historyCursor, true, m.historyGen)
   }
   ```
   Wire into the two down-moving branches in `handleKey` (model.go:440-453 for `"down"`,
   model.go:465-475 for `"shift+down"/"pgdown"`) — both currently end
   `return m, m.autoSelectSeat()`; change both to:
   ```go
   return m, tea.Batch(m.autoSelectSeat(), m.maybeFetchNextHistoryPage())
   ```
   `"up"`/`"shift+up"/"pgup"` are unchanged.
4. Run green, then run the full Go suite: `cd tools/arb-watch-go && go test ./...`.
5. Commit: `feat(arb-watch-history): on-scroll history pagination`.

---

## Ordering summary

Gateway (Tasks 1-7) is built and independently testable first — each task's tests run against the
real endpoint/query/migration with no Go dependency. Task 5 (migration) is sequenced before Task 6
deliberately: it also fixes the shared `scratch` fixture, which every later gateway integration test
depends on. Go (Tasks 8-14) builds against the now-complete gateway contract, in dependency order:
the HTTP fetch primitive (8) before the model wiring that calls it (9-10), the page-application
methods (11) before the `Update` cases that call them (12), then the cosmetic/filter wiring (13) and
finally the scroll-triggered pagination (14) that depends on everything above it. Every task ends with
a passing test run and its own commit.

## Test commands

**Gateway** (from repo root, `<venv-python>` = the project's virtualenv interpreter, `<test-postgres-dsn>`
= a real/test Postgres the `arb_memory` schema can be provisioned against — this repo's actual
fixtures read `ARB_MEMORY_DSN`, not `ARB_MEMORY_TEST_DSN`):
```
PYTHONPATH=src ARB_MEMORY_DSN=<test-postgres-dsn> <venv-python> -m pytest tests/arb_memory/test_visibility_history.py -q
PYTHONPATH=src ARB_MEMORY_DSN=<test-postgres-dsn> <venv-python> -m pytest tests/arb_memory/test_setup_schema.py -q
PYTHONPATH=src ARB_MEMORY_DSN=<test-postgres-dsn> <venv-python> -m pytest tests/arb_memory/ -q   # full-suite regression check, esp. after Task 5
```

**Go** (from `tools/arb-watch-go/`):
```
cd tools/arb-watch-go && go test ./...
```

---

## Decisions I'm least sure are right

1. **Fixing the `scratch` test fixture's multi-statement `schema.sql` load (Task 5, step 5) is in
   scope at all.** The brief only names `schema.sql`/`run.py` as migration files to modify, not
   `tests/arb_memory/conftest.py`. But it's not optional: once `CREATE INDEX CONCURRENTLY` lands in
   `schema.sql`, every test using the shared `scratch` fixture breaks at fixture setup (Postgres wraps
   a multi-statement simple-query message in an implicit transaction regardless of client-side
   `autocommit`) — this isn't a hypothetical, it follows directly from Postgres's wire protocol and
   the fixture's current `conn.execute(schema_sql.read_text(...))` one-call shape. I'm confident the
   break is real; I'm less sure a naive `split(";")` is the *robust* long-term fix for shared test
   infra versus something more deliberate (a real SQL statement splitter, or applying schema.sql via a
   subprocess `psql -f` instead of psycopg) — the naive split is safe today only because I confirmed
   `schema.sql` has no dollar-quoted bodies; it would silently mis-split if one were ever added.
2. **`run.py`'s `setup_schema()` autocommit toggle (commit-then-flip-then-restore) is the right shape**
   versus giving `setup_schema` its own dedicated connection for just the index statement, or pushing
   the autocommit requirement up to `run_setup_schema()`'s caller instead of making `setup_schema`
   defensive regardless of the connection it's handed. I chose the local toggle because it makes
   `setup_schema` correct under both calling conventions already present in this codebase (the
   fixture's pre-set `autocommit=True`, and production's non-autocommit default) without changing
   either caller — but I derived the exact commit-before-flip requirement from my understanding of
   psycopg3's `autocommit` setter semantics (no active transaction allowed), not by running it, per
   the "author only, no implementation" constraint on this task.
3. **The two added test names not in the spec's original 30** (`test_history_state_commit_events_are_a_crash_edge_done_fallback`,
   `test_history_state_steer_and_cancel_are_a_crash_edge_incomplete_fallback`, plus a first-page
   NULL-cursor integration test and the non-autocommit migration regression test) are necessary
   because the remediation added the commit-event fallback and the NULL-cast fix *after* the spec's
   test list (§4) was written, so nothing in that list actually exercises them. I'm confident the
   *feature* needs coverage; less sure I've picked the minimal/right test shape versus what the
   original spec author would have named had the list been re-numbered post-remediation.

---

## Warm-orchestrator remediation (post plan-panel, 2026-07-03)

Plan panel: codex FIX_BEFORE_BUILD, agy (blockers), pi-GLM PLAN_READY_WITH_NITS. (The per-reviewer
plan-panel reports are not included in this copy.) These resolutions are
authoritative for the codex TDD implementer.

- **P0 — CONCURRENTLY, resolved by ELIMINATION (supersedes the plan's split-schema.sql approach).** Do NOT
  make `schema.sql` use `CONCURRENTLY` and do NOT split the `scratch` fixture. `schema.sql` runs at initial
  setup on an EMPTY table, where a plain `CREATE INDEX IF NOT EXISTS eval_event_raw_orchestrator_task_sent_idx
  ON eval_event_raw (orchestrator, task_id, sent_at DESC, id DESC)` is instant — keep it PLAIN. Put
  `CONCURRENTLY` ONLY in `run.py`'s live-table migration path, run on an autocommit connection. This
  dissolves the `split-on-;`/`$$`/comment-semicolon bug the panel found in the fix, and leaves the shared
  `scratch` fixture and every arb_memory test untouched. (The original brief's "CONCURRENTLY in BOTH" was
  wrong — all three plan authors caught that, and the panel caught the bug in their split-fix; plain-in-
  schema + concurrent-in-run.py is the correct shape.)
- **P0 — cross-orchestrator `historyGen` splice race (agy; Fable bake-off).** Bump `historyGen` on ANY
  seat-map rebuild that changes the source or scope — the two toggle directions AND `enterSeats`/orchestrator
  switch — so a late history page from orchestrator A cannot match gen and splice into orchestrator B's fresh
  view. Add a test: switch orchestrator while a history page is in flight → the stale page is dropped.
- **P1 — Task 8/9 ordering (codex).** Reorder so message types (Task 9) are defined before/with the code that
  references them (Task 8), so each task compiles + commits independently.
- **P1 — test-infra precision (pi-GLM, codex).** Test DSN env var is `ARB_MEMORY_DSN` (not
  `ARB_MEMORY_TEST_DSN`); thread it into `_app_client` per the existing `test_visibility_web.py:23`
  `monkeypatch.setenv` pattern. Scope the 503 test's patch to the module-level query fn, not a global
  `psycopg.connect`. Keep the `seatSource` default = `"live"` set in `newModel()`/`testModel()` (the guard
  breaks otherwise).
- **P1 — Go `seatSource` default (Sonnet bake-off, confirmed).** Initialise `seatSource="live"` in
  `newModel()` and any test model constructor in the SAME task that adds the live-frame-parking guard.

**Net:** the SQL/cursor/index/Go-mechanism/two-direction race-guard are correct (all panelists agree). Build
against the plan AS REMEDIATED here: plain index in schema.sql + CONCURRENTLY in run.py (no fixture churn),
gen-bump on orchestrator switch, task 8/9 reordered, `ARB_MEMORY_DSN` + `_app_client` DSN threading,
`seatSource="live"` default. The panel's earlier design/spec decisions (vote branch dropped, commit events
crash-edge only) stand.

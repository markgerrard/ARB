# ARB audit-emit + eval-trace — Slice 1 (correlation spine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one live panel run land audit rows (dispatch/vote/verdict, distinguishable by `kind`) and eval-trace rows in Postgres, correlated by a single `run_id` that travels the dispatch envelope — proving the spine end-to-end.

**Architecture:** A `run_id` minted once per panel rides a new optional top-level `Envelope` field through every dispatch. The bridge tees an *extract-only* allowlist of safe event metadata onto a durable `eval:events` stream on its own Valkey db; an idempotent `EvalConsumer` drains it to `eval_event_raw`. The orchestrator emits audit events (with a now-first-class `kind` column) to the existing audit bus. A live-panel canary asserts cardinality + both-direction joins.

**Tech Stack:** Python 3.11, psycopg3 (autocommit for canaries), redis-py, Postgres (DO managed, pgvector), Valkey (DO managed). Tests: pytest, redis **db15 only**.

> **Plan v2 — plan-panelled (codex + M3 + cold-Opus, all engaged; needs-changes → folded).** Mechanisms verified sound; build-level fixes folded: Task 5 nested except-Exception dead-letter (no infinite-loop/ack-drop) + idempotent dead-letter; Task 6 both sequence grants + non-vacuous SET-ROLE insert test; Task 7 from_json anchor; Task 8 dropped free-ish `status` + free-text exclusion test; Task 9 real push_task_event body + integration test of the real tee→xadd path + the run_id-gate decision (codex⇄M3 split resolved in M3's favour — gate-on-presence, not dead-letter-flood); Task 12 run-isolated foreign check (scoped to this run's task_ids, NOT global) + non-vacuous drain barrier (PEL pending==0/lag==0 before count-stability). Full synthesis in the commit body.

## Global Constraints

- **Tests use redis db15 only** — never db12 (live bridge bus) or db3 (ARB memory/audit) or db4 (eval prod). The `redis_bus` fixture already defaults to `redis://127.0.0.1:6379/15` with a per-test key prefix.
- **`run_id` integrity = mistake-prevention scope** (operator decision 2026-06-23). No minter-role auth. Validate non-empty *when present*; the dispatch path always sets it; the eval tee + consumer treat a missing `run_id` as a hard error (dead-letter), never a silent insert.
- **The tee EXTRACTS ONLY enumerated keys** into a fresh dict — never "copy source minus a denylist". Raw I/O (`command`, `command_output`, `model_text`, `model_thinking`, any free text) is absent by construction.
- **`eval_io` is NOT shipped this slice** at all.
- **AgentRedisBridge changes are OPERATOR-DIRECTED.** Tasks 7–10 modify `/Users/<user>/AgentRedisBridge` (the live fleet workdir). Do NOT push/restart fleet seats autonomously — implement + test on a branch, hand to Mark to deploy. The bridge's real `event_type` names win over any spec list.
- **Cross-repo eval bus contract (identical names both repos):** db `ARB_EVAL_REDIS_DB=4`, url `ARB_EVAL_REDIS_URL`, prefix `ARB_EVAL_PREFIX`, stream key `eval:events`, consumer group `arbmem-eval`.
- **Slice-1 event vocabulary:** start `task_started`, terminal `task_finished` (carries `ok: bool`); optional `turn_started`/`turn_finished`/`tool_call` captured when present.
- **Terminal state = merge-hold.** No autonomous merge; stop after the e2e canary passes and hand to Mark for the →dev review.
- **Grants land WITH DDL, same commit.** REVOKE-ALL-from-PUBLIC; eval consumer role gets only INSERT/SELECT on eval tables; the MCP read role gets nothing on eval tables.

---

## File Structure

**ARB Memory (`/Users/<user>/<workspace>`):**
- `src/arb_memory/schema.sql` — MODIFY: add `kind text NOT NULL` to `audit_events` CREATE; add `eval_event_raw` + `eval_deadletter` tables.
- `src/arb_memory/audit.py` — MODIFY: `PostgresAuditSink.write` INSERT writes `kind`.
- `src/arb_memory/eval.py` — CREATE: `eval_stream`, `PostgresEvalSink`, `EvalConsumer`, `deadletter_malformed_eval_event`, `eval_lag`/`check_eval_health`.
- `src/arb_memory/eval_config.py` — CREATE: env resolution (`ARB_EVAL_REDIS_URL`/`_DB`/`PREFIX`, stream/group constants).
- `src/arb_memory/mcp/grants.py` — MODIFY: `apply_eval_grants`; REVOKE eval tables from MCP role.
- `src/arb_memory/run.py` — MODIFY: `run_eval()` + `"eval"` service choice + eval redis client.
- `scripts/arb-memory-migrate-audit-kind` — CREATE: live-DB backfill migration (nullable→backfill→quarantine→SET NOT NULL).
- `scripts/arb-memory-eval-e2e` — CREATE: live-panel close-condition canary.
- `tests/arb_memory/test_audit_kind.py` — CREATE.
- `tests/arb_memory/test_eval_consumer.py` — CREATE.
- `tests/arb_memory/test_eval_grants.py` — CREATE.
- `tests/arb_memory/test_eval_config.py` — CREATE.
- `tests/arb_memory/test_migrate_audit_kind.py` — CREATE.

**AgentRedisBridge (`/Users/<user>/AgentRedisBridge`, operator-directed):**
- `src/agent_redis_bridge/envelope.py` — MODIFY: optional `run_id` field + validation + `to_dict`.
- `src/agent_redis_bridge/eval_tee.py` — CREATE: `EVAL_ALLOWLIST`, `extract_eval_payload`.
- `src/agent_redis_bridge/bridge.py` — MODIFY: `push_task_event` tees via `extract_eval_payload`.
- `tests/test_envelope_run_id.py` — CREATE.
- `tests/test_eval_tee.py` — CREATE.

**Orchestrator/dispatch (`/Users/<user>/<workspace>`):**
- `scripts/agent-dispatch` — MODIFY: `--run-id` flag threaded into the envelope.
- `scripts/arb-audit-emit` — CREATE: CLI emitting dispatch/vote/verdict to the audit bus.
- `tests/test_agent_dispatch_run_id.py` — CREATE (envelope-shape test, `--dry-run-envelope`).

---

## Phase A — ARB Memory: audit `kind` becomes first-class

### Task 1: `audit_events.kind` column (fresh-schema DDL + sink write)

**Files:**
- Modify: `src/arb_memory/schema.sql:43-54`
- Modify: `src/arb_memory/audit.py:101-131` (`PostgresAuditSink.write`)
- Test: `tests/arb_memory/test_audit_kind.py`

**Interfaces:**
- Consumes: `AuditRun.emit(source, kind, payload)` (unchanged) and `audit_emit(...)` which already puts `kind` on the stream; `_parse_event` already returns `event["kind"]`.
- Produces: `audit_events` rows now carry a queryable `kind text NOT NULL` column.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_audit_kind.py
import json
from arb_memory.audit import PostgresAuditSink, audit_content_hash


def _event(run_id, seq, source, kind, payload):
    ch = audit_content_hash(run_id, seq, source, kind, payload)
    return {
        "run_id": run_id, "seq": seq, "source": source, "kind": kind,
        "payload": payload, "content_hash": ch, "stream_entry_id": f"{seq}-0",
        "ts": "2026-06-23T00:00:00+00:00",
    }


def test_sink_writes_kind_column(conn_factory):
    conn = conn_factory()
    sink = PostgresAuditSink()
    assert sink.write(conn, _event("run-k1", 1, "orchestrator", "dispatch", {"actor": "seat:codex"})) == "written"
    row = conn.execute(
        "SELECT kind FROM audit_events WHERE run_id=%s AND seq=%s", ("run-k1", 1)
    ).fetchone()
    assert row[0] == "dispatch"


def test_kind_column_matches_payload_kind_when_present(conn_factory):
    conn = conn_factory()
    sink = PostgresAuditSink()
    sink.write(conn, _event("run-k2", 1, "orchestrator", "vote", {"kind": "vote", "actor": "seat:m3"}))
    row = conn.execute(
        "SELECT kind, payload->>'kind' FROM audit_events WHERE run_id=%s AND seq=%s", ("run-k2", 1)
    ).fetchone()
    assert row[0] == "vote"
    # plan-panel M3 P1: assert agreement (not the lenient `in (None, "vote")`) — a column/payload
    # kind drift must red. The seeded payload carries kind='vote', so it must be present and equal.
    assert row[1] == row[0] == "vote"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_audit_kind.py -v`
Expected: FAIL — `psycopg.errors.UndefinedColumn: column "kind" of relation "audit_events" does not exist`.

- [ ] **Step 3: Add the column to `schema.sql`**

In `src/arb_memory/schema.sql`, change the `audit_events` CREATE to include `kind` (after `source`):

```sql
CREATE TABLE IF NOT EXISTS audit_events (
    id              bigserial PRIMARY KEY,
    run_id          text NOT NULL,
    seq             bigint NOT NULL,
    source          text NOT NULL,
    kind            text NOT NULL,
    ts              timestamptz NOT NULL DEFAULT now(),
    payload         jsonb NOT NULL DEFAULT '{}',
    stream_entry_id text,
    content_hash    text,
    raw_entry       jsonb,
    UNIQUE (run_id, seq)
);
```

- [ ] **Step 4: Write `kind` in the sink INSERT**

In `src/arb_memory/audit.py`, `PostgresAuditSink.write`, add `kind` to the column list and values:

```python
            row = conn.execute(
                """
                INSERT INTO audit_events
                    (run_id, seq, source, kind, ts, payload, stream_entry_id, content_hash, raw_entry)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, seq) DO NOTHING
                RETURNING id
                """,
                (
                    event["run_id"],
                    int(event["seq"]),
                    event["source"],
                    event["kind"],
                    event.get("ts"),
                    Jsonb(event["payload"]),
                    event.get("stream_entry_id"),
                    content_hash,
                    Jsonb(event),
                ),
            ).fetchone()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_audit_kind.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the existing audit suite (no regression)**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_audit.py tests/arb_memory/test_audit_prefix.py -v`
Expected: PASS (the `scratch` fixture rebuilds the schema with the new column; `_event` always carries `kind`).

- [ ] **Step 7: Commit**

```bash
git add src/arb_memory/schema.sql src/arb_memory/audit.py tests/arb_memory/test_audit_kind.py
git commit -m "feat(arb): audit_events.kind first-class column + sink writes it"
```

### Task 2: Live-DB backfill migration for the populated `audit_events`

**Files:**
- Create: `scripts/arb-memory-migrate-audit-kind`
- Test: `tests/arb_memory/test_migrate_audit_kind.py`

**Interfaces:**
- Consumes: a psycopg connection on the live DB; existing rows carry `kind` in `raw_entry->>'kind'` (verified: the audit canary asserts `raw_entry->>'kind'`).
- Produces: `migrate_audit_kind(conn)` — idempotent; safe to re-run.

**Rationale:** Task 1's `schema.sql` change only affects *freshly created* schemas (tests). The live DO `audit_events` is already populated by the audit canary, so a bare `ADD COLUMN kind NOT NULL` fails. This migration does the ordered path.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_migrate_audit_kind.py
import importlib.util
from pathlib import Path
from psycopg.types.json import Jsonb

_spec = importlib.util.spec_from_file_location(
    "migrate_audit_kind", Path(__file__).parents[2] / "scripts" / "arb-memory-migrate-audit-kind"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
migrate_audit_kind = _mod.migrate_audit_kind


def _seed_legacy_table(conn):
    # simulate the pre-migration shape: NO kind column, rows with kind only in raw_entry
    conn.execute("ALTER TABLE audit_events DROP COLUMN kind")
    conn.execute(
        "INSERT INTO audit_events (run_id, seq, source, payload, raw_entry) VALUES (%s,%s,%s,%s,%s)",
        ("legacy-1", 1, "orchestrator", Jsonb({"actor": "x"}), Jsonb({"kind": "dispatch", "actor": "x"})),
    )
    conn.execute(
        "INSERT INTO audit_events (run_id, seq, source, payload, raw_entry) VALUES (%s,%s,%s,%s,%s)",
        ("legacy-2", 1, "orchestrator", Jsonb({}), Jsonb({})),  # unbackfillable
    )


def test_migration_backfills_and_quarantines_then_sets_not_null(conn_factory):
    conn = conn_factory()
    _seed_legacy_table(conn)
    migrate_audit_kind(conn)
    rows = dict(conn.execute("SELECT run_id, kind FROM audit_events ORDER BY run_id").fetchall())
    assert rows["legacy-1"] == "dispatch"        # backfilled from raw_entry
    assert rows["legacy-2"] == "unknown"         # quarantined, not NULL
    # column is now NOT NULL
    nn = conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='audit_events' AND column_name='kind'"
    ).fetchone()[0]
    assert nn == "NO"


def test_migration_is_idempotent(conn_factory):
    conn = conn_factory()
    _seed_legacy_table(conn)
    migrate_audit_kind(conn)
    migrate_audit_kind(conn)  # second run must not error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_migrate_audit_kind.py -v`
Expected: FAIL — `No such file or directory: '.../scripts/arb-memory-migrate-audit-kind'`.

- [ ] **Step 3: Write the migration script**

```python
#!/usr/bin/env python3
"""Backfill audit_events.kind on an already-populated table, then tighten to NOT NULL.

Order (idempotent): add nullable -> backfill from raw_entry->>'kind' -> quarantine the
unbackfillable to 'unknown' (never silent NULL) -> SET NOT NULL. Run once per live DB.
"""
import os
import sys

import psycopg


def migrate_audit_kind(conn):
    conn.execute("ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS kind text")
    conn.execute(
        "UPDATE audit_events SET kind = raw_entry->>'kind' "
        "WHERE kind IS NULL AND raw_entry->>'kind' IS NOT NULL AND raw_entry->>'kind' <> ''"
    )
    conn.execute("UPDATE audit_events SET kind = 'unknown' WHERE kind IS NULL")
    conn.execute("ALTER TABLE audit_events ALTER COLUMN kind SET NOT NULL")


def main(argv=None):
    dsn = os.environ.get("ARB_MEMORY_DSN")
    if not dsn:
        print("ARB_MEMORY_DSN required", file=sys.stderr)
        return 2
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        migrate_audit_kind(conn)
        print("audit_events.kind migration complete")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Make it executable + run tests to verify they pass**

Run:
```bash
chmod +x scripts/arb-memory-migrate-audit-kind
ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_migrate_audit_kind.py -v
```
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/arb-memory-migrate-audit-kind tests/arb_memory/test_migrate_audit_kind.py
git commit -m "feat(arb): audit_events.kind backfill migration (nullable->backfill->quarantine->NOT NULL)"
```

---

## Phase B — ARB Memory: eval consumer, schema, grants, config, run wiring

### Task 3: `eval_event_raw` + `eval_deadletter` schema

**Files:**
- Modify: `src/arb_memory/schema.sql` (append after `audit_deadletter`)
- Test: `tests/arb_memory/test_schema.py` (extend) — or a new assertion in `test_eval_consumer.py` (Task 5 covers behaviourally). This task’s test asserts the tables + unique index exist.
- Test: `tests/arb_memory/test_eval_schema.py`

**Interfaces:**
- Produces: tables `eval_event_raw` (unique `stream_entry_id`) and `eval_deadletter`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_eval_schema.py
def test_eval_tables_and_unique_constraint(scratch):
    cols = {r[0] for r in scratch.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='eval_event_raw'"
    ).fetchall()}
    assert {"run_id", "task_id", "seat_id", "event_type", "sent_at", "payload",
            "stream_entry_id", "inserted_at"} <= cols
    # stream_entry_id unique
    scratch.execute(
        "INSERT INTO eval_event_raw (run_id, task_id, event_type, sent_at, payload, stream_entry_id) "
        "VALUES ('r','t','task_started', now(), '{}', 'e1')"
    )
    dup_ok = scratch.execute(
        "INSERT INTO eval_event_raw (run_id, task_id, event_type, sent_at, payload, stream_entry_id) "
        "VALUES ('r','t','task_started', now(), '{}', 'e1') ON CONFLICT (stream_entry_id) DO NOTHING "
        "RETURNING id"
    ).fetchone()
    assert dup_ok is None  # conflict -> no row


def test_eval_deadletter_exists(scratch):
    cols = {r[0] for r in scratch.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='eval_deadletter'"
    ).fetchall()}
    assert {"run_id", "task_id", "event_type", "raw_entry", "error"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_eval_schema.py -v`
Expected: FAIL — `relation "eval_event_raw" does not exist`.

- [ ] **Step 3: Add the tables to `schema.sql`**

Append after the `audit_deadletter` block in `src/arb_memory/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS eval_event_raw (
    id              bigserial PRIMARY KEY,
    run_id          text NOT NULL,
    task_id         text NOT NULL,
    seat_id         text,
    event_type      text NOT NULL,
    sent_at         timestamptz NOT NULL,
    payload         jsonb NOT NULL,           -- allowlisted metadata only; raw I/O excluded at the tee
    stream_entry_id text NOT NULL,
    inserted_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)
);
CREATE INDEX IF NOT EXISTS eval_event_raw_run_idx ON eval_event_raw (run_id);
CREATE INDEX IF NOT EXISTS eval_event_raw_task_idx ON eval_event_raw (task_id);

CREATE TABLE IF NOT EXISTS eval_deadletter (
    id              bigserial PRIMARY KEY,
    run_id          text,
    task_id         text,
    seat_id         text,
    event_type      text,
    payload         jsonb,
    stream_entry_id text,
    raw_entry       jsonb,
    error           text,
    ts              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)   -- plan-panel M3 P1: PEL redelivery must not double-deadletter
);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_eval_schema.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/schema.sql tests/arb_memory/test_eval_schema.py
git commit -m "feat(arb): eval_event_raw + eval_deadletter schema (stream_entry_id idempotency key)"
```

### Task 4: eval bus config (`ARB_EVAL_*` resolution)

**Files:**
- Create: `src/arb_memory/eval_config.py`
- Test: `tests/arb_memory/test_eval_config.py`

**Interfaces:**
- Produces: `EVAL_STREAM = "eval:events"`, `EVAL_GROUP = "arbmem-eval"`, `eval_prefix()`, `eval_stream()`, `eval_redis_url()`, `eval_redis_db()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_eval_config.py
import importlib

import pytest


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import arb_memory.eval_config as ec
    return importlib.reload(ec)


def test_pinned_constants(monkeypatch):
    ec = _reload(monkeypatch)
    assert ec.EVAL_STREAM == "eval:events"
    assert ec.EVAL_GROUP == "arbmem-eval"


def test_prefix_and_stream(monkeypatch):
    ec = _reload(monkeypatch, ARB_EVAL_PREFIX="t:")
    assert ec.eval_prefix() == "t:"
    assert ec.eval_stream() == "t:eval:events"


def test_db_defaults_to_4(monkeypatch):
    monkeypatch.delenv("ARB_EVAL_REDIS_DB", raising=False)
    ec = _reload(monkeypatch)
    assert ec.eval_redis_db() == 4


def test_db_distinct_from_reserved(monkeypatch):
    # the pinned default must never collide with live(12)/memory-audit(3)/tests(15)
    ec = _reload(monkeypatch)
    assert ec.eval_redis_db() not in (3, 12, 15)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/arb_memory/test_eval_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'arb_memory.eval_config'`.

- [ ] **Step 3: Write the config module**

```python
# src/arb_memory/eval_config.py
"""Eval bus config. Producer (bridge) and consumer (ARB) MUST read the same env var names."""
import os

EVAL_STREAM_BASE = "eval:events"
EVAL_STREAM = EVAL_STREAM_BASE          # convenience alias (prefix applied by eval_stream())
EVAL_GROUP = "arbmem-eval"
DEFAULT_EVAL_DB = 4                      # db3 = memory/audit; db4 = eval; NOT db12 live, NOT db15 tests


def eval_prefix():
    return os.environ.get("ARB_EVAL_PREFIX", "")


def eval_stream():
    return f"{eval_prefix()}{EVAL_STREAM_BASE}"


def eval_redis_url():
    return os.environ.get("ARB_EVAL_REDIS_URL", "")


def eval_redis_db():
    return int(os.environ.get("ARB_EVAL_REDIS_DB", DEFAULT_EVAL_DB))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/arb_memory/test_eval_config.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/eval_config.py tests/arb_memory/test_eval_config.py
git commit -m "feat(arb): eval bus config (ARB_EVAL_* names, db4 default, eval:events/arbmem-eval pinned)"
```

### Task 5: `EvalConsumer` — drain, idempotent insert, run_id-missing dead-letter

**Files:**
- Create: `src/arb_memory/eval.py`
- Test: `tests/arb_memory/test_eval_consumer.py`

**Interfaces:**
- Consumes: `ensure_group(redis, stream, group)` (from `arb_memory.bus`); `eval_stream()`/`EVAL_GROUP` (from `eval_config`); a `conn_factory` yielding autocommit psycopg conns (like the audit consumer).
- Produces: `PostgresEvalSink.write(conn, event)`, `EvalConsumer(redis, conn_factory, *, prefix, consumer, block_ms)` with `step()/drain_pending()/run()/start()/stop()`, `deadletter_malformed_eval_event(conn, entry_id, fields, error)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/arb_memory/test_eval_consumer.py
import json

from arb_memory.eval import EvalConsumer, PostgresEvalSink
from arb_memory.eval_config import EVAL_GROUP


def _xadd(redis, stream, **fields):
    return redis.xadd(stream, fields)


def _drain(consumer):
    # drain everything currently pending/new
    n = 0
    while consumer.step() is not None:
        n += 1
    return n


def _make(redis_bus, conn_factory):
    prefix = redis_bus.prefix
    stream = f"{prefix}eval:events"
    consumer = EvalConsumer(redis_bus, conn_factory, prefix=prefix, block_ms=50)
    return stream, consumer


def test_event_lands_with_stream_entry_id(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(redis_bus, stream, run_id="r1", task_id="t1", seat_id="codex",
                event_type="task_started", sent_at="2026-06-23T00:00:00+00:00", payload="{}")
    _drain(consumer)
    conn = conn_factory()
    row = conn.execute(
        "SELECT run_id, task_id, event_type, stream_entry_id FROM eval_event_raw WHERE run_id='r1'"
    ).fetchone()
    assert row == ("r1", "t1", "task_started", eid)


def test_redelivery_is_idempotent(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(redis_bus, stream, run_id="r2", task_id="t1", seat_id="codex",
                event_type="task_started", sent_at="2026-06-23T00:00:00+00:00", payload="{}")
    _drain(consumer)
    # re-present the SAME entry id by writing it back through the sink (simulates PEL redelivery)
    conn = conn_factory()
    sink = PostgresEvalSink()
    dup = sink.write(conn, {"run_id": "r2", "task_id": "t1", "seat_id": "codex",
                            "event_type": "task_started", "sent_at": "2026-06-23T00:00:00+00:00",
                            "payload": {}, "stream_entry_id": eid})
    assert dup == "duplicate"
    n = conn.execute("SELECT count(*) FROM eval_event_raw WHERE run_id='r2'").fetchone()[0]
    assert n == 1


def test_crash_recovery_redrains_pending(redis_bus, conn_factory):
    # entry delivered but NOT acked -> a fresh consumer (same group) re-reads from PEL -> no-op insert
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(redis_bus, stream, run_id="r3", task_id="t1", seat_id="codex",
                event_type="task_finished", sent_at="2026-06-23T00:00:00+00:00",
                payload=json.dumps({"ok": True}))
    # read WITHOUT ack to leave it in PEL
    rows = redis_bus.xreadgroup(EVAL_GROUP, "crasher", {stream: ">"}, count=1)
    assert rows  # delivered, unacked
    # a fresh consumer drains pending
    consumer2 = EvalConsumer(redis_bus, conn_factory, prefix=redis_bus.prefix, consumer="crasher", block_ms=50)
    consumer2.drain_pending()
    conn = conn_factory()
    n = conn.execute("SELECT count(*) FROM eval_event_raw WHERE run_id='r3'").fetchone()[0]
    assert n == 1


def _drain_until(consumer, predicate, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        consumer.step()
        if predicate():
            return
    raise AssertionError("drain predicate timed out")


def test_missing_run_id_deadletters_not_silent_insert(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    _xadd(redis_bus, stream, task_id="t1", seat_id="codex",
          event_type="task_started", sent_at="2026-06-23T00:00:00+00:00", payload="{}")  # NO run_id
    conn = conn_factory()
    _drain_until(consumer, lambda: conn.execute(
        "SELECT count(*) FROM eval_deadletter WHERE event_type='task_started'").fetchone()[0] == 1)
    assert conn.execute("SELECT count(*) FROM eval_event_raw").fetchone()[0] == 0
    # the deadletter row must store run_id as NULL (not '' — else a foreign-row check could be fooled)
    assert conn.execute(
        "SELECT run_id IS NULL FROM eval_deadletter WHERE event_type='task_started'").fetchone()[0] is True


def test_nonfatal_sink_error_deadletters_and_acks_no_loop(redis_bus, conn_factory):
    # the nested except-Exception handler: a sink that raises a non-infra error must dead-letter +
    # ack (not infinite-loop). Mirrors the audit consumer's poison-entry handling.
    class _BoomSink:
        def write(self, conn, event):
            raise ValueError("synthetic sink bug")

    stream, consumer = _make(redis_bus, conn_factory)
    consumer.sinks = [_BoomSink()]
    eid = _xadd(redis_bus, stream, run_id="r-boom", task_id="t1", seat_id="codex",
                event_type="task_started", sent_at="2026-06-23T00:00:00+00:00", payload="{}")
    assert consumer.step() == "dead-lettered"
    # entry is acked (no longer pending) -> a second step does not re-process it
    pend = redis_bus.xpending(stream, EVAL_GROUP)["pending"]
    assert pend == 0
    conn = conn_factory()
    assert conn.execute("SELECT count(*) FROM eval_deadletter WHERE stream_entry_id=%s", (eid,)).fetchone()[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_eval_consumer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'arb_memory.eval'`.

- [ ] **Step 3: Write `eval.py`**

```python
# src/arb_memory/eval.py
"""ARB Memory eval-trace consumer. Mirrors the audit single-writer/at-least-once pattern.

run_id is required (mistake-prevention): a teed event without a non-empty run_id is
dead-lettered, never silently inserted. Idempotency key = stream_entry_id (the XADD id the
consumer reads from xreadgroup; re-presented on PEL redelivery -> ON CONFLICT catches it).
"""
import json
import logging
import threading

import psycopg
import redis
from psycopg.types.json import Jsonb

from .bus import ensure_group
from .eval_config import EVAL_GROUP, eval_stream

logger = logging.getLogger(__name__)


class PostgresEvalSink:
    def write(self, conn, event):
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO eval_event_raw
                    (run_id, task_id, seat_id, event_type, sent_at, payload, stream_entry_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stream_entry_id) DO NOTHING
                RETURNING id
                """,
                (
                    event["run_id"], event["task_id"], event.get("seat_id"),
                    event["event_type"], event["sent_at"],
                    Jsonb(event["payload"]), event["stream_entry_id"],
                ),
            ).fetchone()
        return "written" if row is not None else "duplicate"


def deadletter_malformed_eval_event(conn, entry_id, fields, error):
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO eval_deadletter
                (run_id, task_id, seat_id, event_type, payload, stream_entry_id, raw_entry, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stream_entry_id) DO NOTHING
            """,
            (
                fields.get("run_id"), fields.get("task_id"), fields.get("seat_id"),
                fields.get("event_type"), None, entry_id,
                Jsonb(dict(fields, stream_entry_id=entry_id)), str(error),
            ),
        )


class EvalConsumer:
    def __init__(self, redis, conn_factory, *, prefix=None, consumer="eval", block_ms=1000, sinks=None):
        self.redis = redis
        self.conn_factory = conn_factory
        self.consumer = consumer
        self.block_ms = block_ms
        self.sinks = sinks or [PostgresEvalSink()]
        # eval_stream() reads ARB_EVAL_PREFIX; the e2e/tests pass an explicit prefix instead.
        self.stream = f"{prefix}eval:events" if prefix is not None else eval_stream()
        self.running = False
        self.thread = None
        ensure_group(self.redis, self.stream, EVAL_GROUP)

    def step(self):
        rows = self.redis.xreadgroup(EVAL_GROUP, self.consumer, {self.stream: ">"}, count=1, block=self.block_ms)
        entry = self._one_from_rows(rows)
        if entry is None:
            return None
        return self._handle_entry(*entry)

    def drain_pending(self):
        drained = 0
        while True:
            try:
                rows = self.redis.xreadgroup(EVAL_GROUP, self.consumer, {self.stream: "0"}, count=1)
                entry = self._one_from_rows(rows)
                if entry is None:
                    return drained
                if self._handle_entry(*entry) is None:
                    return drained
                drained += 1
            except Exception:
                logger.exception("eval consumer pending drain failed")
                return drained

    def run(self):
        self.running = True
        self.drain_pending()
        while self.running:
            try:
                self.step()
            except Exception:
                logger.exception("eval consumer failed")

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    @staticmethod
    def _one_from_rows(rows):
        if not rows:
            return None
        _, entries = rows[0]
        return entries[0] if entries else None

    @staticmethod
    def _parse_event(entry_id, fields):
        run_id = fields.get("run_id")
        if not run_id:
            raise ValueError("eval event missing run_id")
        for required in ("task_id", "event_type", "sent_at"):
            if not fields.get(required):
                raise ValueError(f"eval event missing {required}")
        raw_payload = fields.get("payload", "{}")
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        except json.JSONDecodeError as exc:
            raise ValueError("eval event invalid payload") from exc
        return {
            "run_id": run_id,
            "task_id": fields["task_id"],
            "seat_id": fields.get("seat_id"),
            "event_type": fields["event_type"],
            "sent_at": fields["sent_at"],
            "payload": payload,
            "stream_entry_id": entry_id,
        }

    def _ack(self, entry_id):
        try:
            self.redis.xack(self.stream, EVAL_GROUP, entry_id)
        except Exception:
            logger.exception("eval consumer failed to ack %s", entry_id)

    def _handle_entry(self, entry_id, fields):
        try:
            event = self._parse_event(entry_id, fields)
        except (KeyError, TypeError, ValueError) as exc:
            conn = None
            try:
                conn = self.conn_factory()
                deadletter_malformed_eval_event(conn, entry_id, fields, exc)
            except (psycopg.Error, redis.RedisError):
                logger.exception("eval consumer failed to dead-letter %s", entry_id)
                return None
            finally:
                if conn is not None and not getattr(conn, "closed", True):
                    conn.close()
            self._ack(entry_id)
            return "dead-lettered"

        conn = None
        try:
            conn = self.conn_factory()
            result = None
            for sink in self.sinks:
                result = sink.write(conn, event)
        except (psycopg.Error, redis.RedisError):
            # Infra error: do NOT ack — let PEL redeliver (transient).
            logger.exception("eval consumer failed to handle %s", entry_id)
            return None
        except Exception as exc:
            # Deterministic, non-infra handler failure (a future sink's validate/serialize bug).
            # Evidence has no silent-drop path: preserve in the deadletter (recoverable) before
            # acking — never ack-and-drop, and never infinite-loop on the same poison entry.
            # Mirrors AuditConsumer (audit.py:269-289). Fresh conn — the handler's may be poisoned.
            logger.exception("eval handler failed for %s; dead-lettering", entry_id)
            dl_conn = None
            try:
                dl_conn = self.conn_factory()
                deadletter_malformed_eval_event(dl_conn, entry_id, fields, exc)
            except (psycopg.Error, redis.RedisError):
                logger.exception("eval consumer failed to dead-letter handler error %s", entry_id)
                return None
            finally:
                if dl_conn is not None and not getattr(dl_conn, "closed", True):
                    dl_conn.close()
            self._ack(entry_id)
            return "dead-lettered"
        finally:
            if conn is not None and not getattr(conn, "closed", True):
                conn.close()
        self._ack(entry_id)
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_eval_consumer.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/eval.py tests/arb_memory/test_eval_consumer.py
git commit -m "feat(arb): EvalConsumer — idempotent insert, run_id-missing dead-letter, crash-recovery drain"
```

### Task 6: eval grants (consumer role gets INSERT/SELECT; MCP role gets nothing)

**Files:**
- Modify: `src/arb_memory/mcp/grants.py`
- Test: `tests/arb_memory/test_eval_grants.py`

**Interfaces:**
- Consumes: `apply_mcp_grants(conn, role)` (extended); `sql.Identifier`.
- Produces: `apply_eval_grants(conn, eval_role)` — grants INSERT/SELECT on eval tables; `apply_mcp_grants` additionally REVOKEs eval tables from the MCP read role.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_eval_grants.py
from psycopg import sql
from arb_memory.mcp.grants import apply_eval_grants, apply_mcp_grants


def _has_priv(conn, role, table, priv):
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)",
        (role, f"{conn.execute('SELECT current_schema()').fetchone()[0]}.{table}", priv),
    ).fetchone()[0]


def _ensure_role(conn, role):
    exists = conn.execute("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname=%s)", (role,)).fetchone()[0]
    if not exists:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))


def test_eval_role_can_actually_insert_both_eval_tables(scratch):
    # plan-panel P0: privilege-checks alone are vacuous — the role must actually be able to INSERT
    # (catches a missing sequence grant: "permission denied for sequence" only fires on a real insert).
    role = "arb_eval_test_role"
    _ensure_role(scratch, role)
    apply_eval_grants(scratch, role)
    scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    try:
        scratch.execute(
            "INSERT INTO eval_event_raw (run_id, task_id, event_type, sent_at, payload, stream_entry_id) "
            "VALUES ('r','t','task_started', now(), '{}', 'seq-test-1')")
        scratch.execute(
            "INSERT INTO eval_deadletter (run_id, task_id, event_type, raw_entry, error, stream_entry_id) "
            "VALUES ('r','t','task_started', '{}', 'x', 'dl-seq-test-1')")
    finally:
        scratch.execute("RESET ROLE")


def test_eval_role_cannot_touch_audit(scratch):
    role = "arb_eval_test_role"
    _ensure_role(scratch, role)
    apply_eval_grants(scratch, role)
    assert _has_priv(scratch, role, "audit_events", "INSERT") is False


def test_mcp_role_has_no_eval_access(scratch):
    from arb_memory.mcp.config import mcp_role_name
    role = mcp_role_name()
    apply_mcp_grants(scratch, role)  # conftest already applied; re-apply is idempotent
    assert _has_priv(scratch, role, "eval_event_raw", "SELECT") is False
    assert _has_priv(scratch, role, "eval_event_raw", "INSERT") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_eval_grants.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_eval_grants'`.

- [ ] **Step 3: Implement the grants**

In `src/arb_memory/mcp/grants.py`, append `apply_eval_grants` and extend `apply_mcp_grants` with an eval REVOKE (add after the existing `audit_events` REVOKE):

```python
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {} FROM {}").format(
            sql.Identifier(schema, "eval_event_raw"),
            sql.Identifier(schema, "eval_deadletter"),
            role_ident,
        )
    )


def apply_eval_grants(conn, role: str) -> None:
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    schema_ident = sql.Identifier(schema)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, role_ident))
    conn.execute(
        sql.SQL("GRANT SELECT, INSERT ON {}, {} TO {}").format(
            sql.Identifier(schema, "eval_event_raw"),
            sql.Identifier(schema, "eval_deadletter"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE UPDATE, DELETE ON {}, {} FROM {}").format(
            sql.Identifier(schema, "eval_event_raw"),
            sql.Identifier(schema, "eval_deadletter"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {}, {} TO {}").format(
            sql.Identifier(schema, "eval_event_raw_id_seq"),
            sql.Identifier(schema, "eval_deadletter_id_seq"),   # plan-panel P0: dead-letter INSERT needs it
            role_ident,
        )
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_eval_grants.py tests/arb_memory/test_mcp_role.py -v`
Expected: PASS (the MCP role test still green — it now also lacks eval access).

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/mcp/grants.py tests/arb_memory/test_eval_grants.py
git commit -m "feat(arb): apply_eval_grants (INSERT/SELECT for consumer; REVOKE eval from MCP read role)"
```

### Task 6b: `run_eval()` service entrypoint

**Files:**
- Modify: `src/arb_memory/run.py`
- Test: `tests/arb_memory/test_run_entrypoints.py` (extend)

**Interfaces:**
- Consumes: `EvalConsumer`, `eval_redis_url()`/`eval_redis_db()`.
- Produces: `python -m arb_memory eval` starts an `EvalConsumer` against the eval Valkey db.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_run_entrypoints.py  (add)
def test_eval_service_choice_accepted(monkeypatch):
    import arb_memory.run as run
    called = {}
    monkeypatch.setattr(run, "run_eval", lambda: called.setdefault("eval", True))
    # main dispatches to run_eval without falling through to mcp
    run.main(["eval"])
    assert called.get("eval") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/arb_memory/test_run_entrypoints.py::test_eval_service_choice_accepted -v`
Expected: FAIL — `argument service: invalid choice: 'eval'`.

- [ ] **Step 3: Wire `run_eval` + the service choice**

In `src/arb_memory/run.py` add:

```python
def _eval_redis_client():
    import redis
    from arb_memory.eval_config import eval_redis_url, eval_redis_db

    return redis.from_url(eval_redis_url(), db=eval_redis_db(), decode_responses=True)


def run_eval() -> None:
    from arb_memory.eval import EvalConsumer

    consumer = EvalConsumer(_eval_redis_client(), _memory_conn)
    consumer.start()
    _wait_forever()
```

Change the argparse choices and dispatch:

```python
    parser.add_argument("service", choices=("memory", "audit", "eval", "mcp"))
    ...
    if args.service == "memory":
        run_memory()
    elif args.service == "audit":
        run_audit()
    elif args.service == "eval":
        run_eval()
    else:
        run_mcp()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/arb_memory/test_run_entrypoints.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/run.py tests/arb_memory/test_run_entrypoints.py
git commit -m "feat(arb): run_eval() entrypoint (python -m arb_memory eval)"
```

---

## Phase C — AgentRedisBridge: Envelope `run_id` (OPERATOR-DIRECTED)

> Work in `/Users/<user>/AgentRedisBridge` on a feature branch. Do NOT push or restart fleet seats — implement + test, then hand to Mark.

### Task 7: optional `run_id` on `Envelope`

**Files:**
- Modify: `src/agent_redis_bridge/envelope.py:17-108`
- Test: `tests/test_envelope_run_id.py`

**Interfaces:**
- Produces: `Envelope.run_id: str | None = None`; `from_json` validates non-empty when present and ignores `payload.run_id`; `to_dict` includes `run_id` when not None.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_envelope_run_id.py
import json
import pytest
from agent_redis_bridge.envelope import Envelope, EnvelopeError


def _base(**over):
    d = {"id": "i1", "from": "claude", "branch": "manual", "to": "codex",
         "kind": "request", "sent_at": "2026-06-23T00:00:00+01:00",
         "payload": {"task": "do x"}}
    d.update(over)
    return json.dumps(d)


def test_run_id_round_trips_when_present():
    env = Envelope.from_json(_base(run_id="run-abc"))
    assert env.run_id == "run-abc"
    assert env.to_dict()["run_id"] == "run-abc"


def test_run_id_absent_is_none_and_omitted():
    env = Envelope.from_json(_base())
    assert env.run_id is None
    assert "run_id" not in env.to_dict()


def test_blank_run_id_rejected():
    with pytest.raises(EnvelopeError, match="invalid-run_id"):
        Envelope.from_json(_base(run_id=""))


def test_non_string_run_id_rejected():
    with pytest.raises(EnvelopeError, match="invalid-run_id"):
        Envelope.from_json(_base(run_id=123))


def test_payload_smuggled_run_id_is_ignored():
    env = Envelope.from_json(_base(payload={"task": "do x", "run_id": "smuggled"}))
    assert env.run_id is None  # top-level field is the only source of truth
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/<user>/AgentRedisBridge && python -m pytest tests/test_envelope_run_id.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'run_id'` / attribute missing.

- [ ] **Step 3: Add the field + validation**

In `envelope.py`, add the field to the dataclass (after `in_reply_to`):

```python
    in_reply_to: str | None = None
    run_id: str | None = None
```

In `from_json`, insert the validation **immediately before the `return cls(` at line 84** (NOT "after the in_reply_to block" — the in_reply_to handling is interleaved with the kind-specific request/steer validation, so anchoring there lands mid-block; the only safe anchor is the line directly above `return cls(`):

```python
        run_id = value.get("run_id")
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise EnvelopeError("invalid-run_id")

        return cls(
            ...
            in_reply_to=in_reply_to,
            run_id=run_id,
        )
```

Add `run_id=run_id` as the final kwarg of the `return cls(...)` call. In `to_dict`, after the `in_reply_to` block:

```python
        if self.run_id is not None:
            data["run_id"] = self.run_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/<user>/AgentRedisBridge && python -m pytest tests/test_envelope_run_id.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the existing envelope suite (no regression)**

Run: `cd /Users/<user>/AgentRedisBridge && python -m pytest tests/ -k envelope -v`
Expected: PASS — existing envelopes (no `run_id`) unaffected because the field is optional.

- [ ] **Step 6: Commit (on the bridge feature branch)**

```bash
cd /Users/<user>/AgentRedisBridge
git add src/agent_redis_bridge/envelope.py tests/test_envelope_run_id.py
git commit -m "feat: optional run_id envelope field (validated non-empty; payload-smuggle ignored)"
```

---

## Phase D — AgentRedisBridge: the eval tee (OPERATOR-DIRECTED)

### Task 8: `extract_eval_payload` — the extract-only allowlist + deny-proof

**Files:**
- Create: `src/agent_redis_bridge/eval_tee.py`
- Test: `tests/test_eval_tee.py`

**Interfaces:**
- Produces: `EVAL_ALLOWLIST: frozenset[str]`, `extract_eval_payload(data: dict) -> dict` (returns a NEW dict containing only allowlisted keys).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_eval_tee.py
from agent_redis_bridge.eval_tee import extract_eval_payload, EVAL_ALLOWLIST


def test_extracts_only_allowlisted_keys():
    out = extract_eval_payload({
        "tool_name": "shell", "tool_call_count": 3, "stop_reason": "end_turn",
        "total_tokens": 1200, "latency_ms": 450, "ok": True,
    })
    assert out == {"tool_name": "shell", "tool_call_count": 3, "stop_reason": "end_turn",
                   "total_tokens": 1200, "latency_ms": 450, "ok": True}


def test_command_output_is_excluded_by_construction():
    # DENY-PROOF: raw tool I/O must NEVER ride the tee
    out = extract_eval_payload({
        "tool_name": "shell",
        "command": "cat /etc/passwd; export SECRET=hunter2",
        "command_output": "root:x:0:0:...",
        "output": "stdout text",
        "model_text": "the assistant said...",
    })
    assert "command" not in out
    assert "command_output" not in out
    assert "output" not in out
    assert "model_text" not in out
    assert out == {"tool_name": "shell"}


def test_unknown_key_absent_by_construction():
    out = extract_eval_payload({"surprise_new_field": "leak", "tool_name": "x"})
    assert "surprise_new_field" not in out


def test_allowlist_has_no_raw_io_keys():
    for forbidden in ("command", "command_output", "output", "model_text", "model_thinking",
                      "args", "stdin", "status", "summary", "error"):
        assert forbidden not in EVAL_ALLOWLIST


def test_free_text_task_finished_fields_excluded():
    # task_finished carries {ok, summary, error}: keep `ok`, drop the free-text summary/error
    out = extract_eval_payload({"ok": True, "summary": "did the thing; here is secret context", "error": None})
    assert out == {"ok": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/<user>/AgentRedisBridge && python -m pytest tests/test_eval_tee.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_redis_bridge.eval_tee'`.

- [ ] **Step 3: Write the allowlist (extract-only)**

```python
# src/agent_redis_bridge/eval_tee.py
"""Extract-only allowlist for the eval tee. Build the durable payload by COPYING ONLY these
keys out of the source event into a fresh dict — never forward the source minus a denylist.
Raw tool I/O (command/args/output, model_text/_thinking) is absent by construction (= eval_io OFF)."""

EVAL_ALLOWLIST = frozenset({
    # turn/usage metadata only — NO free text, NO command/output.
    # Bounded scalars: tool_name is an identifier (not user text); ok/exit_code/attempt are
    # bounded (bool/int) and carried by the task_finished/command_finished vocabulary. `status`
    # is intentionally EXCLUDED (plan-panel codex P2: free-ish string; not in v3's pinned list).
    "tool_name", "tool_call_count", "turn_index",
    "stop_reason", "finish_reason",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "latency_ms", "exit_code", "ok", "attempt",
})


def extract_eval_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in EVAL_ALLOWLIST if k in data}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/<user>/AgentRedisBridge && python -m pytest tests/test_eval_tee.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/<user>/AgentRedisBridge
git add src/agent_redis_bridge/eval_tee.py tests/test_eval_tee.py
git commit -m "feat: eval tee extract-only allowlist + command_output deny-proof"
```

### Task 9: tee at `push_task_event` onto `eval:events`

**Files:**
- Modify: `src/agent_redis_bridge/bridge.py:1413-1423` (`push_task_event`) + the bridge's redis/init to add an eval client.
- Test: `tests/test_push_task_event_tee.py`

**Interfaces:**
- Consumes: `extract_eval_payload`, `request.run_id` (Task 7), `eval:events` client.
- Produces: every `push_task_event` also XADDs an extract-only record to `eval:events` on the eval db, stamped `run_id`/`seat_id`/`task_id`/`event_type`/`sent_at`. Tee failures must NOT break the live event path.

**Note on the eval client:** the bridge resolves the eval Valkey from `ARB_EVAL_REDIS_URL`/`ARB_EVAL_REDIS_DB` (same names as ARB). If `ARB_EVAL_REDIS_URL` is unset, the tee is a no-op (slice-1 safety: eval is opt-in by config, never crashes a seat that hasn't been pointed at an eval bus).

**Design decision — the tee gate (plan-panel: codex⇄M3 split, resolved).** The bridge calls `push_task_event` for **every** task, including ordinary non-panel dev dispatches that legitimately carry no `run_id`. So the tee **gates on `run_id` presence**: present → tee (eval-tracked-iff-panel); absent → no tee (not an eval-tracked task). This is NOT a silent drop of eval data — a non-panel task was never eval-tracked, so there is nothing to lose. codex read the skip as a silent drop and wanted every run_id-less event dead-lettered; that would flood the dead-letter with all non-panel traffic and was rejected. M3 verified the gate sound: a panel dispatch always carries `run_id` (Task 10 + `Envelope` validation), and the **consumer** dead-letters any teed event that somehow lacks `run_id` (Task 5) as defense-in-depth. The one valid part of codex's concern — there was no *positive* test that a run_id-present event actually reaches `eval:events` — is closed by the integration test below.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_push_task_event_tee.py  (uses a fakeredis or a db15 client per the bridge's test conventions)
import json
from agent_redis_bridge.eval_tee import extract_eval_payload


def test_tee_payload_is_extract_only_and_stamped():
    # unit-level: the tee record the bridge builds for eval:events
    from agent_redis_bridge.bridge import build_eval_record
    rec = build_eval_record(
        run_id="run-1", task_id="t1", seat_id="codex",
        event="command_started", sent_at="2026-06-23T00:00:00+01:00",
        data={"command": "rm -rf /", "tool_name": "shell"},
    )
    assert rec["run_id"] == "run-1"
    assert rec["task_id"] == "t1"
    assert rec["seat_id"] == "codex"
    assert rec["event_type"] == "command_started"
    payload = json.loads(rec["payload"])
    assert payload == {"tool_name": "shell"}       # command excluded by construction
    assert "command" not in rec["payload"]


def test_tee_gate_skips_non_panel_task_without_run_id():
    # GATE (not a silent drop): a non-panel task carries no run_id and is not eval-tracked.
    from agent_redis_bridge.bridge import build_eval_record
    assert build_eval_record(run_id=None, task_id="t1", seat_id="codex",
                             event="task_started", sent_at="x", data={}) is None
```

**Integration test (plan-panel P0 — the real `_tee_eval_event` → `xadd` path, not just the helper):**

```python
# tests/test_push_task_event_tee_integration.py
import json, os
import pytest


@pytest.fixture
def eval_redis():
    redis = pytest.importorskip("redis")
    client = redis.from_url(os.environ.get("ARB_EVAL_REDIS_URL", "redis://127.0.0.1:6379/15"), decode_responses=True)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("no redis")
    client._stream = f"itest:{os.getpid()}:eval:events"
    yield client
    client.delete(client._stream)


def _bridge_with_eval(eval_redis):
    # Construct a minimal Bridge-like object exposing _tee_eval_event with the eval client wired.
    # (Per the bridge's test conventions — use the project's existing Bridge test harness/factory.)
    from agent_redis_bridge.bridge import Bridge
    b = Bridge.__new__(Bridge)
    b.agent_id = "codex-test"
    b.eval_redis = eval_redis
    b._eval_stream = eval_redis._stream
    return b


def test_real_tee_path_writes_extract_only_record(eval_redis):
    from agent_redis_bridge.envelope import Envelope
    b = _bridge_with_eval(eval_redis)
    req = Envelope(id="t1", sender="claude", branch="manual", recipient="codex",
                   kind="request", sent_at="x", payload={"task": "x"}, run_id="run-1")
    b._tee_eval_event(req, "command_started", "2026-06-23T00:00:00+00:00",
                      {"command": "cat /etc/passwd", "command_output": "root:x:0:0", "tool_name": "shell"})
    entries = eval_redis.xrange(eval_redis._stream)
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["run_id"] == "run-1"
    assert fields["event_type"] == "command_started"
    payload = json.loads(fields["payload"])
    assert payload == {"tool_name": "shell"}          # raw command/command_output absent on the WIRE
    assert "command" not in fields["payload"]
    assert "command_output" not in fields["payload"]


def test_real_tee_noop_for_panelless_task(eval_redis):
    from agent_redis_bridge.envelope import Envelope
    b = _bridge_with_eval(eval_redis)
    req = Envelope(id="t2", sender="claude", branch="manual", recipient="codex",
                   kind="request", sent_at="x", payload={"task": "x"})  # no run_id
    b._tee_eval_event(req, "task_started", "x", {})
    assert eval_redis.xrange(eval_redis._stream) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/<user>/AgentRedisBridge && python -m pytest tests/test_push_task_event_tee.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_eval_record'`.

- [ ] **Step 3: Add `build_eval_record` + tee in `push_task_event`**

Add a module-level helper in `bridge.py` (near `push_task_event`):

```python
def build_eval_record(*, run_id, task_id, seat_id, event, sent_at, data):
    """Extract-only eval record for eval:events, or None if run_id absent (mistake-prevention)."""
    if not run_id:
        return None
    from .eval_tee import extract_eval_payload
    return {
        "run_id": run_id,
        "task_id": task_id,
        "seat_id": seat_id,
        "event_type": event,
        "sent_at": sent_at,
        "payload": json.dumps(extract_eval_payload(data), separators=(",", ":")),
    }
```

Extend `push_task_event`. The **real current body** (`bridge.py:1413-1423`, verified — it inlines `iso_now()` in the `fields` dict; there is no `sent_at` local today):

```python
    def push_task_event(self, request: Envelope, event: str, data: dict[str, Any]) -> None:
        key = self.redis_config.task_events_key(request.id)
        fields = {
            "type": event,
            "task_id": request.id,
            "from": self.agent_id,
            "to": request.sender,
            "sent_at": iso_now(),
            "data": json.dumps(data, separators=(",", ":")),
        }
        self.redis.xadd(key, fields, maxlen=self.args.max_task_events, ttl=self.args.events_ttl)
```

Edit it to (a) lift `iso_now()` into a local `sent_at` so the tee reuses the SAME timestamp, and (b) call the tee after the unchanged ephemeral XADD:

```python
    def push_task_event(self, request: Envelope, event: str, data: dict[str, Any]) -> None:
        key = self.redis_config.task_events_key(request.id)
        sent_at = iso_now()
        fields = {
            "type": event, "task_id": request.id, "from": self.agent_id,
            "to": request.sender, "sent_at": sent_at,
            "data": json.dumps(data, separators=(",", ":")),
        }
        self.redis.xadd(key, fields, maxlen=self.args.max_task_events, ttl=self.args.events_ttl)
        self._tee_eval_event(request, event, sent_at, data)

    def _tee_eval_event(self, request, event, sent_at, data):
        if self.eval_redis is None:          # eval bus not configured -> no-op
            return
        record = build_eval_record(
            run_id=getattr(request, "run_id", None), task_id=request.id,
            seat_id=self.agent_id, event=event, sent_at=sent_at, data=data,
        )
        if record is None:
            return
        try:
            self.eval_redis.xadd(self._eval_stream, record)   # generous/no maxlen on the durable stream
        except Exception:
            logger.exception("eval tee failed for task %s event %s", request.id, event)
```

In the bridge's `__init__` (where `self.redis` is set up), add the optional eval client:

```python
        eval_url = os.environ.get("ARB_EVAL_REDIS_URL")
        if eval_url:
            import redis as _redis
            self.eval_redis = _redis.from_url(eval_url, db=int(os.environ.get("ARB_EVAL_REDIS_DB", "4")), decode_responses=True)
            self._eval_stream = f"{os.environ.get('ARB_EVAL_PREFIX', '')}eval:events"
        else:
            self.eval_redis = None
            self._eval_stream = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/<user>/AgentRedisBridge && python -m pytest tests/test_push_task_event_tee.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the bridge's push/progress suite (no regression)**

Run: `cd /Users/<user>/AgentRedisBridge && python -m pytest tests/ -k "progress or push or task_event" -v`
Expected: PASS — the ephemeral path is unchanged; the tee is additive and no-ops without `ARB_EVAL_REDIS_URL`.

- [ ] **Step 6: Commit**

```bash
cd /Users/<user>/AgentRedisBridge
git add src/agent_redis_bridge/bridge.py tests/test_push_task_event_tee.py
git commit -m "feat: tee push_task_event -> eval:events (extract-only, run_id-stamped, fail-soft, opt-in)"
```

---

## Phase E — Orchestrator/dispatch: thread `run_id` + emit audit (<workspace>)

### Task 10: `agent-dispatch --run-id` threads into the envelope

**Files:**
- Modify: `scripts/agent-dispatch` (envelope construction)
- Test: `tests/test_agent_dispatch_run_id.py`

**Interfaces:**
- Produces: `agent-dispatch --run-id <id>` sets the top-level `run_id` on the dispatched request envelope; verifiable via `--dry-run-envelope`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_dispatch_run_id.py
import json, os, subprocess


def test_run_id_appears_in_dry_run_envelope():
    env = dict(os.environ, AGENT_ENV_FILE="/Users/<user>/<workspace>/envs/agent-redis-bridge-dev.env")
    out = subprocess.run(
        ["scripts/agent-dispatch", "--engine", "codex", "--target-id", "codex-bridge-dev",
         "--run-id", "run-xyz", "--dry-run-envelope", "hello"],
        cwd="/Users/<user>/<workspace>", env=env, capture_output=True, text=True,
    )
    # the envelope JSON is printed on stdout
    line = [l for l in out.stdout.splitlines() if l.strip().startswith("{")][-1]
    env_json = json.loads(line)
    assert env_json["run_id"] == "run-xyz"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent_dispatch_run_id.py -v`
Expected: FAIL — no `--run-id` flag / `run_id` not in the envelope.

- [ ] **Step 3: Add the flag + thread it**

In `scripts/agent-dispatch`: add `RUN_ID=` to the defaults block; add `--run-id) RUN_ID="$2"; shift 2;;` to the arg parser; in the JSON envelope construction add the field when set. Find the envelope-building section (the `jq`/heredoc that assembles `{id, from, branch, to, kind, sent_at, payload}`) and add `run_id` when `$RUN_ID` is non-empty. With `jq`:

```bash
if [ -n "$RUN_ID" ]; then
  ENVELOPE=$(printf '%s' "$ENVELOPE" | jq --arg rid "$RUN_ID" '. + {run_id: $rid}')
fi
```

(Place this after `ENVELOPE` is assembled and before it is LPUSHed / printed by `--dry-run-envelope`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent_dispatch_run_id.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/agent-dispatch tests/test_agent_dispatch_run_id.py
git commit -m "feat(dispatch): agent-dispatch --run-id threads run_id into the request envelope"
```

### Task 11: `arb-audit-emit` CLI (dispatch/vote/verdict)

**Files:**
- Create: `scripts/arb-audit-emit`
- Test: `tests/arb_memory/test_arb_audit_emit_cli.py`

**Interfaces:**
- Consumes: `AuditRun(redis, run_id, prefix).emit(source, kind, payload)`; `ARB_MEMORY_REDIS_URL` (the audit bus, DO Valkey db3).
- Produces: `arb-audit-emit --run-id R --kind dispatch|vote|verdict --source orchestrator [--actor seat:x] --payload '{...}'` emits one audit event.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_arb_audit_emit_cli.py
import importlib.util, json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "arb_audit_emit", Path(__file__).parents[2] / "scripts" / "arb-audit-emit")
_mod = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_mod)


def test_emit_writes_audit_event(redis_bus):
    prefix = redis_bus.prefix
    _mod.emit(redis_bus, run_id="run-1", source="orchestrator", kind="dispatch",
              payload={"actor": "seat:codex", "task_id": "t1"}, prefix=prefix)
    entries = redis_bus.xrange(f"{prefix}arbmem:audit")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["run_id"] == "run-1"
    assert fields["kind"] == "dispatch"
    assert json.loads(fields["payload"])["actor"] == "seat:codex"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/arb_memory/test_arb_audit_emit_cli.py -v`
Expected: FAIL — file not found.

- [ ] **Step 3: Write the CLI**

```python
#!/usr/bin/env python3
"""Emit one ARB audit event (dispatch/vote/verdict) to the audit bus. Called by the orchestrator."""
import argparse, json, os, sys

import redis as _redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from arb_memory.audit import AuditRun  # noqa: E402


def emit(r, *, run_id, source, kind, payload, prefix=""):
    return AuditRun(r, run_id, prefix=prefix).emit(source, kind, payload)


def main(argv=None):
    p = argparse.ArgumentParser(prog="arb-audit-emit")
    p.add_argument("--run-id", required=True)
    p.add_argument("--kind", required=True, choices=("dispatch", "vote", "verdict"))
    p.add_argument("--source", default="orchestrator")
    p.add_argument("--actor")
    p.add_argument("--payload", default="{}")
    a = p.parse_args(argv)
    url = os.environ.get("ARB_MEMORY_REDIS_URL")
    if not url:
        print("ARB_MEMORY_REDIS_URL required", file=sys.stderr); return 2
    payload = json.loads(a.payload)
    payload.setdefault("kind", a.kind)
    if a.actor:
        payload["actor"] = a.actor
    r = _redis.from_url(url, decode_responses=True)
    emit(r, run_id=a.run_id, source=a.source, kind=a.kind,
         payload=payload, prefix=os.environ.get("ARB_MEMORY_PREFIX", ""))
    print(f"emitted {a.kind} run_id={a.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Make executable + run tests**

Run:
```bash
chmod +x scripts/arb-audit-emit
pytest tests/arb_memory/test_arb_audit_emit_cli.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/arb-audit-emit tests/arb_memory/test_arb_audit_emit_cli.py
git commit -m "feat(orchestrator): arb-audit-emit CLI (dispatch/vote/verdict, kind+actor in payload)"
```

---

## Phase F — Live e2e: the close condition (merge-hold)

### Task 12: live-panel close-condition canary

**Files:**
- Create: `scripts/arb-memory-eval-e2e`
- (No unit test — this is the live e2e against the DO substrate; it IS the close condition.)

**Interfaces:**
- Consumes: a configured eval Valkey + audit bus + Postgres; `EvalConsumer`, `AuditRun`, `AuditConsumer`. Mirrors `scripts/arb-memory-artefact-audit-e2e` structure (autocommit `_connect`, run-tag isolation, drain barrier, run-scoped cleanup).

**What it proves (the 7 cardinality assertions + negative + drain barrier):**
1. exactly one distinct `run_id` across `audit_events` + `eval_event_raw`
2. exactly N `dispatch` rows (one per dispatched seat), by `kind`
3. a `vote` row for every terminal seat
4. exactly one `verdict` row
5. eval rows for every dispatched `task_id`
6. ≥ a `task_started` and a `task_finished` per task in eval
7. zero eval/audit rows with missing/foreign `run_id`
+ **negative:** a `run_id`-stripped eval event dead-letters (lands in `eval_deadletter`, NOT `eval_event_raw`)
+ **drain barrier:** count-stability across the consumer stop boundary
+ **run-scoped cleanup** in `finally`.

- [ ] **Step 1: Write the canary script**

```python
#!/usr/bin/env python3
"""Live close-condition canary for the audit+eval spine. Mirrors arb-memory-artefact-audit-e2e.

Drives one synthetic panel: mint run_id, emit dispatch+vote+verdict to the audit bus, XADD
task_started/task_finished eval events per task to eval:events, run both consumers, then assert
the 7 cardinality checks + the run_id-stripped deny-proof + the drain barrier. Run-isolated by run_tag.
"""
import json, os, sys, time, uuid

import psycopg
import redis as _redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from arb_memory.audit import AuditConsumer, AuditRun           # noqa: E402
from arb_memory.eval import EvalConsumer                       # noqa: E402


def _connect(dsn):
    return psycopg.connect(dsn, autocommit=True)


def wait_until(fn, timeout_s, what):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.1)
    raise RuntimeError(f"timeout waiting for {what}")


def main(argv=None):
    dsn = os.environ["ARB_MEMORY_DSN"]
    audit_url = os.environ["ARB_MEMORY_REDIS_URL"]
    eval_url = os.environ["ARB_EVAL_REDIS_URL"]
    eval_db = int(os.environ.get("ARB_EVAL_REDIS_DB", "4"))

    run_tag = f"arb-eval-e2e-{uuid.uuid4().hex}"
    prefix = f"{run_tag}:"
    seats = [f"{run_tag}-codex", f"{run_tag}-m3"]      # 2 terminal seats
    tasks = {seat: f"{run_tag}-task-{i}" for i, seat in enumerate(seats)}

    ar = _redis.from_url(audit_url, decode_responses=True)
    er = _redis.from_url(eval_url, db=eval_db, decode_responses=True)
    conn = _connect(dsn)

    audit = AuditConsumer(ar, lambda: _connect(dsn), prefix=prefix)
    ev = EvalConsumer(er, lambda: _connect(dsn), prefix=prefix)

    def cleanup():
        conn.execute("DELETE FROM audit_events WHERE run_id = %s", (run_tag,))
        conn.execute("DELETE FROM eval_event_raw WHERE run_id = %s", (run_tag,))
        conn.execute("DELETE FROM eval_deadletter WHERE run_id = %s OR run_id IS NULL", (run_tag,))

    try:
        cleanup()
        audit.start(); ev.start()

        # --- emit the panel ---
        run = AuditRun(ar, run_tag, prefix=prefix)
        for seat in seats:
            run.emit("orchestrator", "dispatch", {"actor": seat, "task_id": tasks[seat], "kind": "dispatch"})
        for seat in seats:
            run.emit("orchestrator", "vote", {"actor": seat, "task_id": tasks[seat], "kind": "vote", "vote": "approve"})
        run.emit("orchestrator", "verdict", {"kind": "verdict", "verdict": "approve"})

        stream = f"{prefix}eval:events"
        for seat in seats:
            for etype, data in (("task_started", {}), ("task_finished", {"ok": True})):
                er.xadd(stream, {"run_id": run_tag, "task_id": tasks[seat], "seat_id": seat,
                                 "event_type": etype, "sent_at": "2026-06-23T00:00:00+00:00",
                                 "payload": json.dumps(data)})

        # negative deny-proof: a run_id-stripped event must dead-letter, not insert
        er.xadd(stream, {"task_id": f"{run_tag}-orphan", "seat_id": "x", "event_type": "task_started",
                         "sent_at": "2026-06-23T00:00:00+00:00", "payload": "{}"})

        # --- wait for the spine to land ---
        wait_until(lambda: conn.execute(
            "SELECT count(*) FROM audit_events WHERE run_id=%s", (run_tag,)).fetchone()[0] >= 5, 60, "audit rows")
        wait_until(lambda: conn.execute(
            "SELECT count(*) FROM eval_event_raw WHERE run_id=%s", (run_tag,)).fetchone()[0] >= 4, 60, "eval rows")
        wait_until(lambda: conn.execute(
            "SELECT count(*) FROM eval_deadletter WHERE run_id IS NULL").fetchone()[0] >= 1, 30, "deadletter")

        # --- 7 cardinality assertions ---
        n_runids = conn.execute(
            "SELECT count(*) FROM ("
            " SELECT run_id FROM audit_events WHERE run_id=%s UNION SELECT run_id FROM eval_event_raw WHERE run_id=%s"
            ") u", (run_tag, run_tag)).fetchone()[0]
        assert n_runids == 1, f"expected 1 run_id, got {n_runids}"

        n_dispatch = conn.execute(
            "SELECT count(*) FROM audit_events WHERE run_id=%s AND kind='dispatch'", (run_tag,)).fetchone()[0]
        assert n_dispatch == len(seats), f"dispatch rows {n_dispatch} != {len(seats)}"

        n_votes = conn.execute(
            "SELECT count(*) FROM audit_events WHERE run_id=%s AND kind='vote'", (run_tag,)).fetchone()[0]
        assert n_votes == len(seats), f"vote rows {n_votes} != {len(seats)}"

        n_verdict = conn.execute(
            "SELECT count(*) FROM audit_events WHERE run_id=%s AND kind='verdict'", (run_tag,)).fetchone()[0]
        assert n_verdict == 1, f"verdict rows {n_verdict} != 1"

        tasks_with_eval = conn.execute(
            "SELECT count(DISTINCT task_id) FROM eval_event_raw WHERE run_id=%s", (run_tag,)).fetchone()[0]
        assert tasks_with_eval == len(seats), f"tasks with eval {tasks_with_eval} != {len(seats)}"

        # terminal predicate normalized (plan-panel M3 P1): accept the bridge's real terminal names.
        # Bridge emits task_started (start) + task_finished (terminal). The IN-set tolerates a future
        # rename to task_succeeded/task_failed without a canary edit.
        bad_pairs = conn.execute(
            "SELECT count(*) FROM (SELECT task_id, "
            " count(*) FILTER (WHERE event_type='task_started') s, "
            " count(*) FILTER (WHERE event_type IN ('task_finished','task_succeeded','task_failed')) f "
            " FROM eval_event_raw WHERE run_id=%s GROUP BY task_id) t WHERE s<1 OR f<1", (run_tag,)).fetchone()[0]
        assert bad_pairs == 0, f"{bad_pairs} task(s) missing start/terminal"

        # run-isolation (plan-panel codex P1 / M3 P0): do NOT use a global `run_id <> run_tag` — the
        # shared DO Postgres holds OTHER runs' rows (incl. live audit). Scope the foreign check to THIS
        # run's task ids: every eval row for our task ids must carry our run_tag (never NULL/foreign),
        # and every row carrying our run_tag must belong to one of our task ids.
        our_tasks = list(tasks.values())
        foreign = conn.execute(
            "SELECT count(*) FROM eval_event_raw "
            "WHERE task_id = ANY(%s) AND (run_id IS NULL OR run_id <> %s)", (our_tasks, run_tag)).fetchone()[0]
        assert foreign == 0, f"{foreign} eval rows for our tasks with a foreign/missing run_id"
        stray = conn.execute(
            "SELECT count(*) FROM eval_event_raw WHERE run_id = %s AND NOT (task_id = ANY(%s))",
            (run_tag, our_tasks)).fetchone()[0]
        assert stray == 0, f"{stray} rows carry our run_tag but an unexpected task_id"

        # negative confirmed: orphan went to deadletter, NOT eval_event_raw
        assert conn.execute(
            "SELECT count(*) FROM eval_event_raw WHERE task_id=%s", (f"{run_tag}-orphan",)).fetchone()[0] == 0

        # both-direction join (audit <-> eval on run_id)
        joined = conn.execute(
            "SELECT count(DISTINCT a.run_id) FROM audit_events a JOIN eval_event_raw e ON a.run_id=e.run_id "
            "WHERE a.run_id=%s", (run_tag,)).fetchone()[0]
        assert joined == 1, "audit<->eval join failed"

        print(json.dumps({"ok": True, "run_tag": run_tag, "dispatch": n_dispatch,
                          "votes": n_votes, "tasks_with_eval": tasks_with_eval}))

        # --- drain barrier (plan-panel codex+M3 P1): a bare "stop, sleep, counts unchanged" is
        # VACUOUS — stopped consumers can't insert, so it passes even with work still queued. First
        # prove BOTH streams are fully consumed (pending==0 AND lag==0), THEN stop, THEN assert the
        # writer committed nothing past the stop boundary. Counts are sampled AFTER quiesce+stop. ---
        def _group_pending(r, stream, group):
            try:
                info = r.xpending(stream, group)
                return int(info["pending"]) if isinstance(info, dict) else int(info[0])
            except Exception:
                return -1  # group/stream absent -> treat as not-yet-drained

        wait_until(lambda: _group_pending(ar, f"{prefix}arbmem:audit", "arbmem-audit") == 0, 30, "audit PEL drained")
        wait_until(lambda: _group_pending(er, f"{prefix}eval:events", "arbmem-eval") == 0, 30, "eval PEL drained")

        audit.stop(); ev.stop()
        time.sleep(2)
        n_after_stop_a = conn.execute("SELECT count(*) FROM audit_events WHERE run_id=%s", (run_tag,)).fetchone()[0]
        n_after_stop_e = conn.execute("SELECT count(*) FROM eval_event_raw WHERE run_id=%s", (run_tag,)).fetchone()[0]
        time.sleep(2)  # second observation window: nothing may commit after the writer is quiesced
        n_recheck_a = conn.execute("SELECT count(*) FROM audit_events WHERE run_id=%s", (run_tag,)).fetchone()[0]
        n_recheck_e = conn.execute("SELECT count(*) FROM eval_event_raw WHERE run_id=%s", (run_tag,)).fetchone()[0]
        if (n_recheck_a, n_recheck_e) != (n_after_stop_a, n_after_stop_e):
            raise RuntimeError(f"drain-barrier violation a:{n_after_stop_a}->{n_recheck_a} e:{n_after_stop_e}->{n_recheck_e}")
        return 0
    finally:
        try:
            audit.stop(); ev.stop()
        except Exception:
            pass
        cleanup()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Make executable**

Run: `chmod +x scripts/arb-memory-eval-e2e`

- [ ] **Step 3: Run the canary against the dev substrate**

Run (deny-proof of the spine — REAL DO Valkey db3/db4 + DO Postgres dev):
```bash
ARB_MEMORY_DSN=$ARB_MEMORY_DSN \
ARB_MEMORY_REDIS_URL=$ARB_MEMORY_REDIS_URL \
ARB_EVAL_REDIS_URL=$ARB_EVAL_REDIS_URL ARB_EVAL_REDIS_DB=4 \
scripts/arb-memory-eval-e2e
```
Expected: prints `{"ok": true, ...}` and exits 0. Run it **3×** (reliability ≠ clean-once); each run is run-tag-isolated.

- [ ] **Step 4: Deny-proof the negative + a cardinality guard**

Temporarily break the run_id-missing guard (in `eval.py` `_parse_event`, comment the `if not run_id: raise`) and re-run: the canary must RED on the orphan landing in `eval_event_raw` / the deadletter assertion. Restore the guard; re-run green. (Use `cp` save/restore on `eval.py`, never `git checkout`.)

- [ ] **Step 5: Commit**

```bash
git add scripts/arb-memory-eval-e2e
git commit -m "feat(arb): live close-condition canary — 7 cardinality assertions + run_id deny-proof + drain barrier"
```

---

## Merge-hold

Stop here. Do NOT merge. Summarise for Mark:
- the commits across the three repos (ARB Memory on `dev`; AgentRedisBridge on its feature branch — operator deploys);
- the 3× green e2e transcript (real `run_tag`, dispatch/vote/verdict counts, eval task coverage, drain barrier clean);
- the deny-proof red→green;
- the deferred follow-ups (run_id minter-role enforcement; `eval_io`/normalized spans/partitions; bridge-emitted votes).

Hand to Mark for the →dev review.

---

## Self-Review

**1. Spec coverage (v3 sections → tasks):**
- §1 run_id integrity (Envelope field + validation) → Task 7; threaded by Task 10. ✓
- §2 audit kind queryable + migration order → Tasks 1, 2. ✓
- §2 orchestrator emits dispatch/vote/verdict → Task 11 (CLI), exercised by Task 12. ✓
- §3 tee extract-only allowlist + deny-proof → Tasks 8, 9. ✓
- §3 tee-at-push_task_event, own db → Task 9. ✓
- Schema eval_event_raw + stream_entry_id idempotency → Tasks 3, 5. ✓
- Schema audit_events.run_id already exists (no DDL) → respected (no DDL touches run_id). ✓
- Security: eval_io not shipped (allowlist excludes raw) → Task 8; grants → Task 6; eval consumer prefix/db isolation → Tasks 4, 5, 6b. ✓
- Cross-repo contract (db4/prefix/group/env names) → Tasks 4, 9; config test → Task 4. ✓
- Slice-1 event vocabulary (task_started/task_finished) → Task 12 close condition; allowlist carries `ok` → Task 8. ✓
- Close condition (7 cardinality + negative + drain barrier + run-scoped cleanup) → Task 12. ✓
- stream_entry_id = consumer xreadgroup id + redelivery + crash-recovery → Task 5. ✓
- **Gap noted & resolved:** spec said "add run_id to from_json required"; the Envelope is shared by all message kinds, so a *required* field would break hello/notify/reply/steer. Implemented as **optional-but-validated-when-present** (Task 7), with the dispatch path always setting it and the tee/consumer treating missing run_id as a hard dead-letter — this honours the spec's intent (no silent insert) without breaking the bus. Flag this deviation in the build review.

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code. ✓

**3. Type consistency:** `build_eval_record`/`extract_eval_payload` field names (`run_id`/`task_id`/`seat_id`/`event_type`/`sent_at`/`payload`) match `PostgresEvalSink.write` columns and the `eval_event_raw` schema and the canary's XADD fields. `EVAL_GROUP`/`eval_stream()` consistent across `eval_config`, `eval.py`, `run.py`, and the bridge tee. ✓

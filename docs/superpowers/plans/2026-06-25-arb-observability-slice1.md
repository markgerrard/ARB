# ARB Observability — Slice 1 (Eval → Prod) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make prod `arbmemory` the durable system-of-record for bridge **eval/trace**: real seat execution traces land in prod `eval_event_raw`, joinable by `run_id`, with the event schema frozen (versioned), a dispatch-time `run_id` fail-loud gate, the env-resolution bug fixed so the tee actually arms, and an operator `grants` command that enforces the read boundary on prod.

**Architecture:** The eval *correlation spine* (tables `eval_event_raw`/`eval_deadletter`, `EvalConsumer`, `PostgresEvalSink`, the `run_id`-or-deadletter guard, `apply_eval_grants`/`apply_mcp_grants`) already merged to `dev` at `e1db4d0`. This slice wires that dormant spine to prod: (1) add `schema_version` end-to-end to freeze the wire/storage shape; (2) fix `bridge.py` eval-Redis resolution so a URL/DB/prefix set only in the `.env` file still arms the tee; (3) turn the dispatch-time missing-`run_id` *warning* into a *fail-loud* error on panel dispatches and stamp an `audit_vote_expected` marker (the Slice-3 seam, frozen now); (4) add a `grants` CLI subcommand so the operator can apply the eval consumer grants + the `arbmemory-mcp` eval-REVOKE on prod; (5) add the `eval` service to the prod compose pointed at db-6. Cross-WAN eval emit stays **fail-soft** (best-effort telemetry, not authoritative cost data); the *missing-`run_id`* case is the only fail-loud, and it fails at dispatch, before any emit.

**Tech Stack:** Python 3.12, psycopg3 (`Jsonb`), redis-py (streams/`XADD`), pytest, bash (`agent-dispatch`, `jq`), Docker Compose, Postgres + pgvector.

## Global Constraints

- **Eval telemetry is best-effort, NOT authoritative cost data.** Cross-WAN emit is fail-soft (`_tee_eval_event` try/except, 0.5s socket timeouts). Only a **missing `run_id`** is fail-loud, and it fails at **dispatch time**, before any emit. Do not add at-least-once/spool machinery in this slice.
- **`run_id` is the logical run** (orchestrator-minted, carried on the envelope), NOT the bridge task-id. Every eval row carries `run_id` + `task_id` + `seat_id`. Audit↔eval join on `run_id`.
- **`schema_version` value for this slice is the string `"1"`.** Producer (bridge) stamps it; consumer stores whatever it receives, defaulting to `"1"` when the field is absent (back-compat with spine rows already emitted without it).
- **Eval Redis DB is `6` in prod** (`ARB_EVAL_REDIS_DB=6`) on BOTH the bridge (producer) and the `eval` service (consumer). db-4 = dev eval; db-5 = prod bus; db-12 = bridge live events (TTL); db-15 = tests.
- **Non-panel dispatches stay un-gated.** The `run_id` requirement applies ONLY when `--audit-panel` is set. A plain `agent-dispatch` with no `--run-id` must keep working unchanged (eval simply isn't tracked for it — `build_eval_record` returns `None`).
- **`schema_version` must NOT be added to the JSON `payload`.** It is a top-level stream field / table column alongside `run_id`, so it is queryable without unpacking jsonb and survives the allowlist (which only filters `payload`).

### Test harness (every task's pytest steps assume this)

```bash
cd /Users/<user>/<workspace>
export PYTHONPATH="$(pwd):$(pwd)/src"
set -a; . envs/arb-memory-dev.env; set +a          # ARB_MEMORY_DSN -> local pgvector @127.0.0.1:5544
export ARB_MEMORY_REDIS_URL=redis://127.0.0.1:6379/15
PYTEST=/Users/<user>/<workspace>/.venv/bin/pytest
```

The PG-backed tests (Tasks 1, 4) use the local pgvector via `ARB_MEMORY_DSN`. The bridge/dispatch tests (Tasks 2, 3) are pure (no PG/Redis needed).

---

### Task 1: `schema_version` end-to-end (freeze the eval event schema)

Add a `schema_version` column to both eval tables, a single source-of-truth constant, stamp it in the bridge producer, and store it in the consumer (defaulting to `"1"` for spine rows that predate it).

**Files:**
- Modify: `src/arb_memory/schema.sql:77-101` (add column to `eval_event_raw` + `eval_deadletter`)
- Modify: `src/agent_redis_bridge/eval_tee.py` (add `EVAL_SCHEMA_VERSION` constant)
- Modify: `src/agent_redis_bridge/bridge.py:93-106` (`build_eval_record` stamps it)
- Modify: `src/arb_memory/eval.py:23-40` (`PostgresEvalSink.write` INSERTs it), `:122-144` (`_parse_event` reads it), `:44-55` (`deadletter_malformed_eval_event` stores it)
- Test: `tests/arb_memory/test_eval_consumer.py` (round-trip), `tests/test_build_eval_record.py` (producer stamp — create if absent)

**Interfaces:**
- Produces: `agent_redis_bridge.eval_tee.EVAL_SCHEMA_VERSION: str = "1"`. `build_eval_record(...)` return dict gains key `"schema_version": EVAL_SCHEMA_VERSION`. `eval_event_raw` / `eval_deadletter` gain column `schema_version text`. `PostgresEvalSink.write(conn, event)` reads `event.get("schema_version", "1")`. `_parse_event(entry_id, fields)` return dict gains `"schema_version": fields.get("schema_version", "1")`.

- [ ] **Step 1: Write the failing producer test**

Create `tests/test_build_eval_record.py`:

```python
from agent_redis_bridge.bridge import build_eval_record
from agent_redis_bridge.eval_tee import EVAL_SCHEMA_VERSION


def test_build_eval_record_stamps_schema_version():
    rec = build_eval_record(
        run_id="run-1", task_id="task-1", seat_id="codex-x",
        event="turn-end", sent_at="2026-06-25T00:00:00+00:00", data={},
    )
    assert rec is not None
    assert rec["schema_version"] == EVAL_SCHEMA_VERSION == "1"


def test_build_eval_record_none_without_run_id_unaffected():
    assert build_eval_record(
        run_id="", task_id="t", seat_id="s",
        event="turn-end", sent_at="2026-06-25T00:00:00+00:00", data={},
    ) is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `$PYTEST tests/test_build_eval_record.py -v`
Expected: FAIL — `ImportError: cannot import name 'EVAL_SCHEMA_VERSION'` (and KeyError on `schema_version`).

- [ ] **Step 3: Add the constant + stamp it**

In `src/agent_redis_bridge/eval_tee.py`, add near the top (after the module docstring / existing `EVAL_ALLOWLIST`):

```python
# Frozen eval event schema version. Stamped by the bridge producer onto every eval record and stored
# as a top-level column by the consumer. Bump ONLY on a breaking change to the 6 correlation fields or
# the payload contract; add a migration when you do. Slice 1 freezes this at "1".
EVAL_SCHEMA_VERSION = "1"
```

In `src/agent_redis_bridge/bridge.py`, change `build_eval_record` (lines 93-106). Update the import line and the returned dict:

```python
def build_eval_record(*, run_id, task_id, seat_id, event, sent_at, data):
    """Extract-only eval record for eval:events, or None if run_id absent (mistake-prevention)."""
    if not run_id:
        return None
    from .eval_tee import EVAL_SCHEMA_VERSION, extract_eval_payload

    return {
        "run_id": run_id,
        "task_id": task_id,
        "seat_id": seat_id,
        "event_type": event,
        "sent_at": sent_at,
        "schema_version": EVAL_SCHEMA_VERSION,
        "payload": json.dumps(extract_eval_payload(data), separators=(",", ":")),
    }
```

- [ ] **Step 4: Run the producer test to verify it passes**

Run: `$PYTEST tests/test_build_eval_record.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Add the schema columns**

In `src/arb_memory/schema.sql`, add `schema_version` to `eval_event_raw` (after `event_type`, before `sent_at` is fine — column order is cosmetic) and a nullable one to `eval_deadletter`:

```sql
CREATE TABLE IF NOT EXISTS eval_event_raw (
    id              bigserial PRIMARY KEY,
    run_id          text NOT NULL,
    task_id         text NOT NULL,
    seat_id         text,
    event_type      text NOT NULL,
    schema_version  text NOT NULL DEFAULT '1',
    sent_at         timestamptz NOT NULL,
    payload         jsonb NOT NULL,           -- allowlisted metadata only; raw I/O excluded at the tee
    stream_entry_id text NOT NULL,
    inserted_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)
);
```

And in `eval_deadletter` add `schema_version text` after `event_type` (nullable — a malformed event may lack it):

```sql
CREATE TABLE IF NOT EXISTS eval_deadletter (
    id              bigserial PRIMARY KEY,
    run_id          text,
    task_id         text,
    seat_id         text,
    event_type      text,
    schema_version  text,
    payload         jsonb,
    stream_entry_id text,
    raw_entry       jsonb,
    error           text,
    ts              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)   -- plan-panel M3 P1: PEL redelivery must not double-deadletter
);
```

Because the table is created with `CREATE TABLE IF NOT EXISTS`, an already-migrated DB will NOT pick up the new column. Add an idempotent `ALTER` immediately after each `CREATE TABLE` block so existing prod/dev DBs gain the column:

```sql
ALTER TABLE eval_event_raw  ADD COLUMN IF NOT EXISTS schema_version text NOT NULL DEFAULT '1';
ALTER TABLE eval_deadletter ADD COLUMN IF NOT EXISTS schema_version text;
```

- [ ] **Step 6: Wire the consumer (write + parse + deadletter)**

In `src/arb_memory/eval.py`, `PostgresEvalSink.write` (lines 23-40) — add the column + value:

```python
    def write(self, conn, event):
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO eval_event_raw
                    (run_id, task_id, seat_id, event_type, schema_version, sent_at, payload, stream_entry_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stream_entry_id) DO NOTHING
                RETURNING id
                """,
                (
                    event["run_id"], event["task_id"], event.get("seat_id"),
                    event["event_type"], event.get("schema_version", "1"), event["sent_at"],
                    Jsonb(event["payload"]), event["stream_entry_id"],
                ),
            ).fetchone()
        return "written" if row is not None else "duplicate"
```

`_parse_event` return dict (lines 136-144) — add the key (default `"1"` so spine rows without the field still parse):

```python
        return {
            "run_id": run_id,
            "task_id": fields["task_id"],
            "seat_id": fields.get("seat_id"),
            "event_type": fields["event_type"],
            "schema_version": fields.get("schema_version", "1"),
            "sent_at": fields["sent_at"],
            "payload": payload,
            "stream_entry_id": entry_id,
        }
```

`deadletter_malformed_eval_event` (lines 44-55) — add the column + value, reading from the raw `fields`:

```python
            INSERT INTO eval_deadletter
                (run_id, task_id, seat_id, event_type, schema_version, payload, stream_entry_id, raw_entry, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stream_entry_id) DO NOTHING
            """,
            (
                fields.get("run_id"), fields.get("task_id"), fields.get("seat_id"),
                fields.get("event_type"), fields.get("schema_version"), None, entry_id,
                Jsonb(dict(fields, stream_entry_id=entry_id)), str(error),
            ),
```

- [ ] **Step 7: Write the failing consumer round-trip test**

Add to `tests/arb_memory/test_eval_consumer.py` (match the file's existing fixture/helper style — it already constructs a consumer against `ARB_MEMORY_DSN`; mirror its existing "happy path" test, adding the assertion). The new test:

```python
def test_eval_event_persists_schema_version(eval_conn):
    # `eval_conn` + the xadd/consume helpers follow this file's existing happy-path test.
    entry_id = _xadd_eval_event(
        run_id="run-sv", task_id="task-sv", seat_id="codex-x",
        event_type="turn-end", schema_version="1",
        sent_at="2026-06-25T00:00:00+00:00", payload="{}",
    )
    _consume_once(eval_conn)
    row = eval_conn.execute(
        "SELECT schema_version FROM eval_event_raw WHERE stream_entry_id = %s", (entry_id,)
    ).fetchone()
    assert row[0] == "1"
```

If the file lacks `_xadd_eval_event` / `_consume_once` helpers, use whatever the file's existing happy-path test uses to push and drain one event — do not invent a new harness. The only new assertion is `schema_version == "1"`.

- [ ] **Step 8: Run the consumer test (red → apply migration → green)**

Run: `$PYTEST tests/arb_memory/test_eval_consumer.py -v`
Expected first run: FAIL (column doesn't exist yet on the live test DB / `_xadd` doesn't pass the field).
Apply the schema to the test DB: `psql "$ARB_MEMORY_DSN" -f src/arb_memory/schema.sql` (idempotent — the `ALTER ... IF NOT EXISTS` adds the column).
Re-run: `$PYTEST tests/arb_memory/test_eval_consumer.py tests/arb_memory/test_eval_schema.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/arb_memory/schema.sql src/agent_redis_bridge/eval_tee.py src/agent_redis_bridge/bridge.py \
        src/arb_memory/eval.py tests/test_build_eval_record.py tests/arb_memory/test_eval_consumer.py
git commit -m "feat(eval): freeze eval event schema with schema_version end-to-end

Producer (bridge build_eval_record) stamps EVAL_SCHEMA_VERSION='1'; consumer
stores it as a top-level column on eval_event_raw + eval_deadletter, defaulting
to '1' for spine rows that predate the field. Slice 1 of ARB Observability."
```

---

### Task 2: Fix the eval-Redis env-file resolution (arm the tee from `.env`)

`bridge.py:200-218` resolves `ARB_EVAL_REDIS_URL` / `ARB_EVAL_REDIS_DB` / `ARB_EVAL_PREFIX` from `os.environ` ONLY. When those live in the bridge's `.env` file (parsed into the `env` dict at `bridge.py:113`) but are not exported, the tee silently stays disarmed — exactly the prod failure mode. Fix: exported env wins, `.env` file is the fallback. Extract to a pure helper so it's unit-testable without constructing a full `Bridge`.

**Files:**
- Modify: `src/agent_redis_bridge/bridge.py` (add `resolve_eval_redis` helper; call it in `__init__:200`)
- Test: `tests/test_resolve_eval_redis.py` (create)

**Interfaces:**
- Produces: `agent_redis_bridge.bridge.resolve_eval_redis(env: dict) -> tuple[str | None, int, str]` returning `(url, db, prefix)`. `url` is `None`/empty when unset (caller treats falsy as "no tee"). `db` defaults to `4`, `prefix` to `""`.
- Consumes (Task 1): nothing — independent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_resolve_eval_redis.py`:

```python
import importlib

bridge = importlib.import_module("agent_redis_bridge.bridge")


def test_env_file_arms_tee_when_process_env_absent(monkeypatch):
    monkeypatch.delenv("ARB_EVAL_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_EVAL_REDIS_DB", raising=False)
    monkeypatch.delenv("ARB_EVAL_PREFIX", raising=False)
    env = {"ARB_EVAL_REDIS_URL": "redis://prod:6379", "ARB_EVAL_REDIS_DB": "6", "ARB_EVAL_PREFIX": "p:"}
    url, db, prefix = bridge.resolve_eval_redis(env)
    assert url == "redis://prod:6379" and db == 6 and prefix == "p:"


def test_process_env_wins_over_env_file(monkeypatch):
    monkeypatch.setenv("ARB_EVAL_REDIS_URL", "redis://exported:6379")
    monkeypatch.setenv("ARB_EVAL_REDIS_DB", "6")
    env = {"ARB_EVAL_REDIS_URL": "redis://file:6379", "ARB_EVAL_REDIS_DB": "4"}
    url, db, _ = bridge.resolve_eval_redis(env)
    assert url == "redis://exported:6379" and db == 6


def test_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("ARB_EVAL_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_EVAL_REDIS_DB", raising=False)
    monkeypatch.delenv("ARB_EVAL_PREFIX", raising=False)
    url, db, prefix = bridge.resolve_eval_redis({})
    assert not url and db == 4 and prefix == ""
```

- [ ] **Step 2: Run it to verify it fails**

Run: `$PYTEST tests/test_resolve_eval_redis.py -v`
Expected: FAIL — `AttributeError: module 'agent_redis_bridge.bridge' has no attribute 'resolve_eval_redis'`.

- [ ] **Step 3: Add the helper + call it**

In `src/agent_redis_bridge/bridge.py`, add a module-level function (place it next to `build_eval_record`, ~line 108):

```python
def resolve_eval_redis(env):
    """Resolve eval-Redis config: exported process env wins, the parsed .env file is the fallback.

    Mistake-prevention: a URL/DB/prefix present ONLY in the bridge's .env file must still arm the eval
    tee. The old os.environ-only read silently left the tee disarmed in that case.
    """
    url = os.environ.get("ARB_EVAL_REDIS_URL") or env.get("ARB_EVAL_REDIS_URL")
    db = int(os.environ.get("ARB_EVAL_REDIS_DB") or env.get("ARB_EVAL_REDIS_DB") or "4")
    prefix = os.environ.get("ARB_EVAL_PREFIX") or env.get("ARB_EVAL_PREFIX") or ""
    return url, db, prefix
```

Then in `__init__` replace lines 200-218. The `env` dict (from `read_env_file`, line 113) is in scope:

```python
        self.redis = RedisCli(self.redis_config)
        eval_url, eval_db, eval_prefix = resolve_eval_redis(env)
        if eval_url:
            import redis as _redis

            self.eval_redis = _redis.from_url(
                eval_url,
                db=eval_db,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            resolved_eval_db = int(self.eval_redis.connection_pool.connection_kwargs.get("db", 0))
            if resolved_eval_db != eval_db:
                raise ValueError(
                    f"ARB_EVAL_REDIS_DB mismatch: configured {eval_db}, "
                    f"Redis URL resolved to {resolved_eval_db}"
                )
            self._eval_stream = f"{eval_prefix}eval:events"
        else:
            self.eval_redis = None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `$PYTEST tests/test_resolve_eval_redis.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the existing tee test to confirm no regression**

Run: `$PYTEST tests/test_push_task_event_tee.py -v`
Expected: PASS (the non-panel-no-run_id-is-not-tracked assertion at `:80` still holds — `build_eval_record` returns `None`, unchanged by this task).

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/bridge.py tests/test_resolve_eval_redis.py
git commit -m "fix(eval): resolve eval-Redis from .env file, not os.environ only

A URL/DB/prefix set only in the bridge's .env file silently left the eval tee
disarmed (os.environ-only read). Extract resolve_eval_redis(env): exported env
wins, .env is the fallback. Covers URL + DB + PREFIX. Slice 1."
```

---

### Task 3: Dispatch-time `run_id` fail-loud gate + `audit_vote_expected` marker

Today `--audit-panel` without `--run-id` *warns and exits 0* (`agent-dispatch:166-167`) — a forgetful orchestrator gets zero audit AND zero eval, no error. Make it **fail loud** (exit 1) on panel dispatches. When `--audit-panel` + `--run-id` are both set, stamp `audit_vote_expected: true` onto the envelope — the Slice-3 vote-extraction seam, frozen now. Non-panel dispatches are untouched.

**Files:**
- Modify: `scripts/agent-dispatch:166-168` (warn→error+exit), `:334-336` (stamp marker)
- Modify: `tests/test_agent_dispatch_audit_panel.py` (update 2 stale tests)

**Interfaces:**
- Produces: `agent-dispatch --audit-panel` exits non-zero with `error: --audit-panel requires --run-id` when `--run-id` is absent. When both set, the request envelope gains top-level `"audit_vote_expected": true` (alongside `run_id`).
- Consumes: nothing — independent of Tasks 1/2.

- [ ] **Step 1: Update the two stale tests (write the new expectations first)**

In `tests/test_agent_dispatch_audit_panel.py`, replace `test_audit_panel_flag_does_not_change_envelope` — the flag now DOES add a marker, so the envelope differs:

```python
def test_audit_panel_flag_adds_vote_expected_marker():
    base = json.loads(_envelope(["--run-id", "panel-x"]))
    withp = json.loads(_envelope(["--run-id", "panel-x", "--audit-panel"]))
    assert "audit_vote_expected" not in base
    assert withp["audit_vote_expected"] is True
    # the marker is the ONLY difference
    del withp["audit_vote_expected"]
    assert _stable(base) == _stable(withp)
```

Replace `test_audit_panel_without_run_id_warns` — it now fails loud:

```python
def test_audit_panel_without_run_id_fails_loud():
    out = subprocess.run([DISPATCH, "--audit-panel", "--dry-run-envelope", "hi"],
                         capture_output=True, text=True, env=ENV)
    assert out.returncode != 0
    assert "--audit-panel requires --run-id" in out.stderr
```

Leave `test_audit_panel_flag_is_accepted` and `test_audit_panel_requires_nothing_when_off` as-is (both still valid: panel+run-id exits 0; plain dispatch has no `run_id`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/test_agent_dispatch_audit_panel.py -v`
Expected: `test_audit_panel_flag_adds_vote_expected_marker` FAIL (no marker yet); `test_audit_panel_without_run_id_fails_loud` FAIL (still exits 0).

- [ ] **Step 3: Make the gate fail loud**

In `scripts/agent-dispatch`, replace lines 166-168:

```bash
if [ -n "$AUDIT_PANEL" ] && [ -z "$RUN_ID" ]; then
  echo "error: --audit-panel requires --run-id (a panel dispatch with no run_id gets zero audit AND zero eval, silently)" >&2
  exit 1
fi
```

- [ ] **Step 4: Stamp the marker on the envelope**

In `scripts/agent-dispatch`, the run_id stamp block at lines 334-336. Add the marker stamp right after it:

```bash
if [ -n "$RUN_ID" ]; then
  MSG=$(printf '%s' "$MSG" | jq -c --arg rid "$RUN_ID" '. + {run_id: $rid}')
fi
if [ -n "$AUDIT_PANEL" ]; then
  MSG=$(printf '%s' "$MSG" | jq -c '. + {audit_vote_expected: true}')
fi
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$PYTEST tests/test_agent_dispatch_audit_panel.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add scripts/agent-dispatch tests/test_agent_dispatch_audit_panel.py
git commit -m "feat(dispatch): fail loud on --audit-panel without --run-id; stamp audit_vote_expected

A panel dispatch with no run_id silently yields zero audit AND zero eval. Make
it exit 1 at dispatch time. When --audit-panel + --run-id are set, stamp
audit_vote_expected:true on the envelope (the Slice-3 vote-extraction seam,
frozen now). Non-panel dispatches unchanged. Slice 1."
```

---

### Task 4: Operator `grants` command (enforce the eval read boundary on prod)

The grant functions (`apply_eval_grants` for the consumer role, `apply_mcp_grants` which REVOKEs eval from `arbmemory-mcp`) exist and are deny-proven in `tests/arb_memory/test_eval_grants.py`. What's missing is a way to *run* them on prod. Add a `grants` subcommand to `python -m arb_memory` so the operator applies both against the live DB.

**Files:**
- Modify: `src/arb_memory/run.py` (add `run_grants()`; add `"grants"` to argparse choices + dispatch)
- Test: `tests/arb_memory/test_run_grants.py` (create)

**Interfaces:**
- Produces: `python -m arb_memory grants` applies `apply_eval_grants(conn, <consumer-role>)` and `apply_mcp_grants(conn, <mcp-role>)` against `ARB_MEMORY_DSN`, reading role names from `ARB_EVAL_CONSUMER_ROLE` (default: the DSN's own user) and `ARB_MCP_ROLE` (default `arbmemory-mcp`). Commits. Idempotent.
- Consumes: `apply_eval_grants`, `apply_mcp_grants` from `arb_memory.mcp.grants` (unchanged).

- [ ] **Step 1: Write the failing test**

Create `tests/arb_memory/test_run_grants.py`:

```python
import os
import subprocess
import sys

import psycopg


def test_grants_command_revokes_eval_from_mcp_role(monkeypatch):
    dsn = os.environ["ARB_MEMORY_DSN"]
    # arbmemory-mcp must exist on the test DB (created by the spine's test setup / schema bootstrap).
    monkeypatch.setenv("ARB_MCP_ROLE", "arbmemory-mcp")
    res = subprocess.run(
        [sys.executable, "-m", "arb_memory", "grants"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": os.environ["PYTHONPATH"]},
    )
    assert res.returncode == 0, res.stderr
    with psycopg.connect(dsn) as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        can_read = conn.execute(
            "SELECT has_table_privilege('arbmemory-mcp', %s, 'SELECT')",
            (f"{schema}.eval_event_raw",),
        ).fetchone()[0]
    assert can_read is False   # deny-proof: the MCP door role cannot read eval
```

- [ ] **Step 2: Run it to verify it fails**

Run: `$PYTEST tests/arb_memory/test_run_grants.py -v`
Expected: FAIL — argparse rejects `grants` (`invalid choice: 'grants'`), non-zero exit.

- [ ] **Step 3: Add `run_grants()` + wire argparse**

In `src/arb_memory/run.py`, add the function (near `run_eval`):

```python
def run_grants() -> None:
    import psycopg

    from arb_memory.mcp.grants import apply_eval_grants, apply_mcp_grants

    dsn = os.environ["ARB_MEMORY_DSN"]
    with psycopg.connect(dsn) as conn:
        consumer_role = os.environ.get("ARB_EVAL_CONSUMER_ROLE") or conn.info.user
        mcp_role = os.environ.get("ARB_MCP_ROLE", "arbmemory-mcp")
        apply_eval_grants(conn, consumer_role)
        apply_mcp_grants(conn, mcp_role)
        conn.commit()
    print(f"grants applied: eval-consumer={consumer_role!r} mcp-revoke={mcp_role!r}")
```

In `main()`, add `"grants"` to the choices and dispatch:

```python
    parser.add_argument("service", choices=("memory", "audit", "eval", "mcp", "writer", "grants"))
    args = parser.parse_args(argv)

    if args.service == "memory":
        run_memory()
    elif args.service == "audit":
        run_audit()
    elif args.service == "eval":
        run_eval()
    elif args.service == "writer":
        run_writer()
    elif args.service == "grants":
        run_grants()
    else:
        run_mcp()
    return 0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `$PYTEST tests/arb_memory/test_run_grants.py tests/arb_memory/test_eval_grants.py -v`
Expected: PASS. (If `arbmemory-mcp` role doesn't exist on the local test DB, create it once: `psql "$ARB_MEMORY_DSN" -c 'CREATE ROLE "arbmemory-mcp" NOLOGIN'` — the existing `test_eval_grants.py` already assumes it, so it should be present.)

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/run.py tests/arb_memory/test_run_grants.py
git commit -m "feat(eval): add 'grants' subcommand to apply eval consumer grants + mcp eval-REVOKE

python -m arb_memory grants applies apply_eval_grants(consumer) and
apply_mcp_grants(arbmemory-mcp) against ARB_MEMORY_DSN, so the operator can
enforce the eval read boundary on prod. Idempotent. Slice 1."
```

---

### Task 5: Prod `eval` compose service (db-6) + best-effort doc + CHANGELOG

Add the `eval` consumer service to the prod compose, pointed at prod eval db-6, and document the best-effort contract. The bridge's `ARB_EVAL_REDIS_DB=6` is operator config on the Mac (the bridge `.env`), not in this repo's compose — note it in the deploy doc.

**Files:**
- Modify: `deploy/docker-compose.yml` (add `eval` service)
- Modify: `CHANGELOG.md` (entry)
- Modify: `docs/superpowers/specs/2026-06-25-arb-observability-design.md` (confirm best-effort note present; add if missing)

**Interfaces:**
- Produces: a prod `eval` service running `python -m arb_memory eval`, consuming prod eval db-6 → `eval_event_raw`. No new code interface.

- [ ] **Step 1: Add the `eval` service to prod compose**

In `deploy/docker-compose.yml`, mirror the existing `audit` service block (it sits right above `writer`). Add after the `audit` block:

```yaml
  eval:
    image: arb-memory:phase3
    command: ["eval"]
    restart: unless-stopped
    depends_on:
      - memory
    environment:
      ARB_MEMORY_DSN: ${ARB_MEMORY_DSN}
      ARB_EVAL_REDIS_URL: ${ARB_EVAL_REDIS_URL}
      ARB_EVAL_REDIS_DB: "6"
```

`ARB_EVAL_REDIS_URL` points at the prod Valkey (db-6 is selected by `ARB_EVAL_REDIS_DB`, matching `eval_redis_db()` in `eval_config.py`). The DB-mismatch guard in the consumer client uses the same env var as the bridge producer, so both sides agree on db-6.

- [ ] **Step 2: Verify the compose parses + the eval service resolves**

Run:
```bash
cd /Users/<user>/<workspace>/deploy
ARB_MEMORY_DSN=x ARB_MEMORY_REDIS_URL=x ARB_EVAL_REDIS_URL=x docker compose config --services
```
Expected: lists `memory`, `audit`, `eval`, `writer`, `mcp` (compose validates; `eval` present). If `docker` is unavailable in the worker env, instead assert the block is well-formed YAML:
```bash
python3 -c "import yaml,sys; d=yaml.safe_load(open('deploy/docker-compose.yml')); assert d['services']['eval']['command']==['eval']; assert d['services']['eval']['environment']['ARB_EVAL_REDIS_DB']=='6'; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Confirm the best-effort contract is documented in the spec**

Open `docs/superpowers/specs/2026-06-25-arb-observability-design.md` and confirm the Slice-1 section states eval telemetry is best-effort (fail-soft cross-WAN) and NOT authoritative cost data, while missing-`run_id` is fail-loud at dispatch. It was folded in at v2 — verify the sentence is present; if not, add one line under the Slice-1 heading:

```markdown
> **Eval is best-effort telemetry, not authoritative cost data.** Cross-WAN emit is fail-soft
> (`_tee_eval_event` try/except, 0.5s timeouts); a dropped row is tolerated. The ONLY fail-loud case
> is a missing `run_id`, caught at dispatch time before any emit.
```

- [ ] **Step 4: Add a CHANGELOG entry**

In `CHANGELOG.md`, under the current unreleased/dev section, add:

```markdown
### ARB Observability — Slice 1 (eval → prod)
- **Eval event schema frozen** with `schema_version` end-to-end (producer stamps `"1"`, consumer
  stores it on `eval_event_raw` + `eval_deadletter`). Why: pin the wire/storage shape before prod
  volume so Visibility (Slice 4) and span tables (Slice 5) can rely on it.
- **Fixed eval-Redis env resolution** (`resolve_eval_redis`): a URL/DB/prefix set only in the bridge's
  `.env` file now arms the tee (was `os.environ`-only → silently disarmed). Why: prod config lives in
  the `.env`, not the process env.
- **Dispatch fails loud** on `--audit-panel` without `--run-id` (was warn+exit-0), and stamps
  `audit_vote_expected:true` when both are set. Why: a panel dispatch with no run_id silently yields
  zero audit AND zero eval — the core mistake this slice prevents.
- **`grants` subcommand** applies the eval consumer grants + the `arbmemory-mcp` eval-REVOKE on prod.
- **Prod `eval` service** added to compose, consuming prod eval db-6 → `eval_event_raw`.
```

- [ ] **Step 5: Run the full eval test suite as a regression gate**

Run:
```bash
$PYTEST tests/test_build_eval_record.py tests/test_resolve_eval_redis.py \
        tests/test_agent_dispatch_audit_panel.py tests/test_push_task_event_tee.py \
        tests/arb_memory/test_eval_consumer.py tests/arb_memory/test_eval_schema.py \
        tests/arb_memory/test_eval_grants.py tests/arb_memory/test_run_grants.py -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add deploy/docker-compose.yml CHANGELOG.md docs/superpowers/specs/2026-06-25-arb-observability-design.md
git commit -m "feat(deploy): add prod eval service (db-6); document best-effort eval contract

Slice 1 of ARB Observability complete: eval consumer service in prod compose
pointed at eval db-6, CHANGELOG, and the best-effort-telemetry contract."
```

---

## Self-Review

**1. Spec coverage** — the Slice-1 change-list (6 items) maps to tasks:
- schema_version end-to-end → **Task 1** ✓
- dispatch-time run_id gate + `audit_vote_expected` marker + 2 stale tests → **Task 3** ✓
- env-file resolution fix (URL+DB+PREFIX) → **Task 2** ✓
- grants deploy wiring + named-role REVOKE deny-proof → **Task 4** ✓ (deny-proof pre-exists in `test_eval_grants.py`; Task 4 adds the operator command + a command-level deny-proof)
- `ARB_EVAL_REDIS_DB=6` both sides → **Task 5** (eval service) + **Global Constraints** (bridge `.env`, operator config) ✓
- eval best-effort doc → **Task 5 Step 3-4** ✓

**2. Placeholder scan** — every code step carries the exact code; commands carry expected output. The only soft spot is Task 1 Step 7's reuse of `test_eval_consumer.py`'s existing `_xadd_eval_event`/`_consume_once` helpers (named conditionally because I haven't read that file's internals) — the instruction is explicit: reuse the file's existing happy-path push/drain, add only the `schema_version` assertion. Acceptable: it pins the behaviour, not an invented harness.

**3. Type consistency** — `schema_version` is the string `"1"` everywhere (`EVAL_SCHEMA_VERSION`, the column default, `.get(..., "1")` fallbacks). `resolve_eval_redis(env) -> (url, db:int, prefix:str)` matches its single call site. `audit_vote_expected` is boolean `true` in both the dispatch stamp and the test assertion. `grants` choice string matches between argparse and the dispatch branch.

**Coherence note for the executor:** This slice builds on the already-merged eval *spine* (`e1db4d0`). Before starting, confirm you're on a branch off current `dev` and that `tests/arb_memory/test_eval_grants.py` + `test_eval_consumer.py` are green on your substrate (they prove the spine is present). If they're red, the spine isn't there and this plan's assumptions don't hold — stop and reconcile.

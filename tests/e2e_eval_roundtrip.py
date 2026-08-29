"""E2E merge gate for ARB Observability Slice 1 — LIVE eval round-trip on the dev substrate.

Drives the REAL producer (Bridge.push_task_event -> _tee_eval_event -> build_eval_record -> XADD) and
the REAL consumer (EvalConsumer -> PostgresEvalSink -> eval_event_raw) across a real local Redis stream
and real local Postgres. Proves the Slice-1 data-plane claim that read-based review cannot:
  * a real seat trace lands in eval_event_raw, JOINABLE BY run_id  (Task 1 + Task 3 producer)
  * schema_version is stamped "1" end-to-end                       (Task 1)
  * the allowlist holds LIVE: planted raw I/O (command/output/model_text) never reaches the stored
    payload                                                        (privacy boundary)
  * resolve_eval_redis arms the tee from an env-file-only URL      (Task 2)

Isolated by a unique stream prefix + run_id; cleans up only its own keys/rows. No flushdb.
Run: PYTHONPATH=<worktree>:<worktree>/src .venv/bin/python tests/e2e_eval_roundtrip.py
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import psycopg
import redis as redislib

from agent_redis_bridge.bridge import Bridge, resolve_eval_redis
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.redis_io import RedisCli, RedisConfig
from arb_memory.eval import EvalConsumer

RUN_ID = f"e2e-{uuid.uuid4().hex[:10]}"
PREFIX = f"e2e_{uuid.uuid4().hex[:8]}:"
SEAT = "codex-bridge-dev"
DSN = os.environ["ARB_MEMORY_DSN"]
EVAL_DB = 14  # unused throwaway db (3=devbus,4=deveval,5=prodbus,6=prodeval,12=livebus,15=tests)

failures = []
def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)

print(f"E2E eval round-trip  run_id={RUN_ID}  stream={PREFIX}eval:events  db={EVAL_DB}")

# ensure schema present in public (idempotent)
with psycopg.connect(DSN) as c:
    c.autocommit = True
    c.execute(open("src/arb_memory/schema.sql", encoding="utf-8").read())

eval_redis = redislib.from_url("redis://127.0.0.1:6379", db=EVAL_DB, decode_responses=True)
stream = f"{PREFIX}eval:events"

def conn_factory():
    c = psycopg.connect(DSN)
    c.autocommit = True
    return c

try:
    # 1) Task 2 (real fn): a URL present ONLY in the parsed .env dict must arm the tee.
    url, db, pref = resolve_eval_redis({"ARB_EVAL_REDIS_URL": "redis://127.0.0.1:6379",
                                        "ARB_EVAL_REDIS_DB": str(EVAL_DB), "ARB_EVAL_PREFIX": PREFIX})
    check(bool(url) and db == EVAL_DB and pref == PREFIX,
          "resolve_eval_redis arms from env-file-only URL/DB/PREFIX (Task 2)")

    # 2) consumer first (creates the group on the stream, MKSTREAM) — matches proven ordering
    consumer = EvalConsumer(eval_redis, conn_factory, prefix=PREFIX, block_ms=50)

    # 3) REAL producer: Bridge.push_task_event tees 3 lifecycle events under one run_id.
    b = Bridge.__new__(Bridge)
    b.redis_config = RedisConfig("127.0.0.1", "6379", "15", "agent_scratch:")
    b.redis = RedisCli(b.redis_config)  # real wrapper (its xadd takes ttl), as production does
    b.args = SimpleNamespace(max_task_events=500, events_ttl=60)
    b.agent_id = SEAT
    b.eval_redis = eval_redis
    b._eval_stream = stream

    req = Envelope(id="e2e-task-1", sender="claude-bridge-dev", branch="feat/arb-observability-slice1",
                   recipient=SEAT, kind="request", sent_at="x",
                   payload={"task": "x", "audit_vote_expected": True}, run_id=RUN_ID)

    SECRETS = ["SECRET-RAW-CMD", "SECRET-RAW-OUTPUT", "SECRET-MODEL-TEXT"]
    b.push_task_event(req, "turn-start", {"tool_name": "bash"})
    b.push_task_event(req, "tool-call", {"tool_name": "bash", "ok": True, "exit_code": 0,
                                         "command": SECRETS[0], "output": SECRETS[1],
                                         "model_text": SECRETS[2], "model_thinking": "SECRET-THINK"})
    b.push_task_event(req, "turn-end", {"usage": {"input_tokens": 10, "output_tokens": 5}})
    check(eval_redis.xlen(stream) == 3, "3 eval events teed to the real stream by the bridge")

    # 4) REAL consumer drains to real Postgres
    drained = 0
    while consumer.step() is not None:
        drained += 1
    check(drained == 3, f"consumer drained 3 events (got {drained})")

    # 5) VERIFY in Postgres
    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            "SELECT event_type, seat_id, schema_version, payload, task_id FROM eval_event_raw "
            "WHERE run_id = %s ORDER BY id", (RUN_ID,)).fetchall()
        n = conn.execute("SELECT count(*) FROM eval_event_raw WHERE run_id=%s", (RUN_ID,)).fetchone()[0]

    check(len(rows) == 3, f"3 rows in eval_event_raw joinable by run_id (got {len(rows)})")
    check(n == 3, "run_id JOIN returns exactly the 3 teed events")
    check([r[0] for r in rows] == ["turn-start", "tool-call", "turn-end"], "event_type sequence preserved")
    check(all(r[1] == SEAT for r in rows), f"seat_id correlated == {SEAT}")
    check(all(r[2] == "1" for r in rows), "schema_version stamped '1' end-to-end (Task 1)")
    check(all(r[4] == "e2e-task-1" for r in rows), "task_id correlated")

    blob = json.dumps([r[3] for r in rows])
    leaked = [s for s in SECRETS + ["SECRET-THINK"] if s in blob]
    check(not leaked, f"ALLOWLIST held LIVE — no raw I/O in stored payload (leaked: {leaked})")
finally:
    with psycopg.connect(DSN) as conn:
        conn.autocommit = True
        conn.execute("DELETE FROM eval_event_raw WHERE run_id = %s", (RUN_ID,))
    for k in eval_redis.scan_iter(f"{PREFIX}*"):
        eval_redis.delete(k)
    print("cleanup: removed e2e rows + stream keys")

print()
if failures:
    print(f"E2E FAILED — {len(failures)} check(s): " + "; ".join(failures))
    sys.exit(1)
print("E2E PASS — live seat trace landed in eval_event_raw keyed by run_id; schema_version=1; "
      "seat/task correlated; allowlist held against planted secrets.")

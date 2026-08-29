"""E2E merge gate for ARB Visibility Slice 4a — LIVE producer→consumer data contract.

Proves the REAL bridge tee (Bridge.push_task_event -> _tee_live_event) produces events:live entries that the
REAL gateway consumes — the link the unit/integration tests (which hand-build entries) don't exercise:
  1. real bridge tees 3 entries (2 seats, one orchestrator) to a real events:live stream;
  2. the gateway's non-streaming GET /orchestrators (over a real ASGI transport, real OAuth token) returns
     that orchestrator from the REAL entries — and 401s without a token (deny-proof);
  3. the gateway's real _reduce_seat turns the REAL entries into both seats with correct states.
(The SSE streaming itself is covered by tests/arb_memory/test_visibility_sse.py over real redis; this E2E
proves the bridge↔gateway data contract, which those tests fake.)

Isolated by a unique prefix + throwaway db; cleans up its keys + token. Run:
  PYTHONPATH=<wt>:<wt>/src .venv/bin/python tests/e2e_visibility_roundtrip.py
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import psycopg
import redis as redislib

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.redis_io import RedisCli, RedisConfig
from arb_memory.mcp import oauth_store
from arb_memory.visibility import build_visibility_app, _reduce_seat

DSN = os.environ["ARB_MEMORY_DSN"]
BUS_DB = 14
PREFIX = f"e2e_vis_{uuid.uuid4().hex[:8]}:"
ORCH = f"claude-e2e-{uuid.uuid4().hex[:6]}"
RESOURCE = os.environ.get("ARB_MEMORY_MCP_PUBLIC_BASE_URL", "https://mem.example.com")
BUS_URL = "redis://127.0.0.1:6379"
TOKEN = f"e2e-vis-{uuid.uuid4().hex}"
CLIENT_ID = f"e2e-client-{uuid.uuid4().hex}"

failures = []
def check(c, m):
    print(("  ok: " if c else "  FAIL: ") + m)
    if not c: failures.append(m)

bus = redislib.from_url(BUS_URL, db=BUS_DB, decode_responses=True)

def seat_bridge(agent_id):
    b = Bridge.__new__(Bridge)
    b.redis_config = RedisConfig("127.0.0.1", "6379", str(BUS_DB), PREFIX)
    b.redis = RedisCli(b.redis_config)  # real wrapper (xadd takes ttl), as production does
    b.args = SimpleNamespace(max_task_events=500, events_ttl=300)
    b.agent_id = agent_id; b.eval_redis = None; b.audit_redis = None; b._audit_prefix = ""
    return b

def req(task_id):
    return Envelope(id=task_id, sender=ORCH, branch="b", recipient="x", kind="request",
                    sent_at="x", payload={}, run_id=f"run-{ORCH}")

async def main():
    print(f"E2E visibility  orch={ORCH}  prefix={PREFIX}  db={BUS_DB}")
    with psycopg.connect(DSN) as conn:
        conn.autocommit = True
        oauth_store.put_access_token(conn, token=TOKEN, client_id=CLIENT_ID, resource=RESOURCE,
                                     scopes=["memory.read"],
                                     expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))

    # 1) REAL bridge tee: two seats under one orchestrator
    seat_bridge("codex-e2e").push_task_event(req("task-A"), "task_started", {"task_id": "task-A"})
    seat_bridge("agy-e2e").push_task_event(req("task-B"), "task_started", {"task_id": "task-B"})
    seat_bridge("codex-e2e").push_task_event(req("task-A"), "task_finished", {"task_id": "task-A", "ok": True})
    entries = bus.xrange(f"{PREFIX}events:live")
    check(len(entries) == 3, f"real bridge teed 3 entries to events:live (got {len(entries)})")
    check(all(f.get("orchestrator") == ORCH for _id, f in entries), "every entry carries orchestrator=to")

    # 2) gateway /orchestrators (non-streaming) over real ASGI transport
    app = build_visibility_app(bus_redis_url=f"{BUS_URL}/{BUS_DB}", bus_prefix=PREFIX, dsn=DSN, public_base_url=RESOURCE)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://vis") as client:
        r401 = await client.get("/orchestrators")
        check(r401.status_code == 401, f"/orchestrators 401 without token (got {r401.status_code})")
        r = await client.get("/orchestrators", headers={"Authorization": f"Bearer {TOKEN}"})
        check(r.status_code == 200, f"/orchestrators 200 with token (got {r.status_code})")
        check(ORCH in r.json().get("orchestrators", []),
              f"gateway lists the orchestrator from the REAL bridge entries (got {r.json()})")

    # 3) the gateway's REAL reducer turns the REAL entries into both seats
    seats = {}
    for _id, f in entries:
        if f.get("orchestrator") == ORCH:
            seats[f["task_id"]] = _reduce_seat(seats.get(f["task_id"], {}), f)
    check({s.get("seat_id") for s in seats.values()} == {"codex-e2e", "agy-e2e"},
          f"reducer yields both seats from real entries (got {[s.get('seat_id') for s in seats.values()]})")
    check(seats.get("task-A", {}).get("state") == "done", f"finished seat -> done (got {seats.get('task-A')})")

def cleanup():
    for k in bus.scan_iter(f"{PREFIX}*"): bus.delete(k)
    with psycopg.connect(DSN) as conn:
        conn.autocommit = True
        conn.execute("DELETE FROM mcp_auth.access_tokens WHERE client_id = %s", (CLIENT_ID,))
    print("cleanup: removed e2e stream keys + token")

try:
    asyncio.run(main())
finally:
    cleanup()

print()
if failures:
    print(f"E2E FAILED — {len(failures)}: " + "; ".join(failures)); sys.exit(1)
print("E2E PASS — real bridge tee → events:live → gateway /orchestrators + reducer show both seats; 401 without token.")

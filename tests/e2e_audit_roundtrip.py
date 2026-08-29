"""E2E merge gate for ARB Observability Slice 3 — LIVE audit decision-record round-trip.

Drives the REAL emit path: orchestrator manifest (AuditRun) + the REAL Bridge._emit_vote for two fake
seats → real audit stream → real AuditConsumer → audit_events → reconcile(). Proves the Slice-3 claim that
read-based review cannot:
  POSITIVE: manifest(seq=1) + two fenced 'approve' votes → reconcile(verdict) ok=True.
  NEGATIVE: one seat's reply is BARE JSON (no fence) → _emit_vote (require_fence=True) emits NO vote →
            reconcile(verdict) ok=False with a fail-loud "never voted" gap (guards b+c + reconcile fail-loud).

Isolated by unique run_id + stream prefix; cleans up only its own keys/rows. Run:
  PYTHONPATH=<worktree>:<worktree>/src .venv/bin/python tests/e2e_audit_roundtrip.py
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import psycopg
import redis as redislib

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from arb_memory.audit import AuditRun, AuditConsumer
from arb_memory.panel_audit import reconcile

DSN = os.environ["ARB_MEMORY_DSN"]
AUDIT_DB = 14  # throwaway
RUN_OK = f"e2e-audit-ok-{uuid.uuid4().hex[:8]}"
RUN_BAD = f"e2e-audit-bad-{uuid.uuid4().hex[:8]}"
PREFIX = f"e2e_audit_{uuid.uuid4().hex[:8]}:"
ROSTER = ["seat:A", "seat:B"]
FENCED_APPROVE = 'reviewed\n```vote\n{"stance":"approve","severity":"none"}\n```'
BARE_JSON = 'looks done\n{"stance":"approve","severity":"none"}'   # NO fence -> rejected by require_fence

failures = []
def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)

audit_redis = redislib.from_url("redis://127.0.0.1:6379", db=AUDIT_DB, decode_responses=True)

def conn_factory():
    c = psycopg.connect(DSN); c.autocommit = True; return c

def seat_bridge(agent_id):
    b = Bridge.__new__(Bridge)
    b.agent_id = agent_id
    b.audit_redis = audit_redis
    b._audit_prefix = PREFIX
    return b

def req(run_id):
    return Envelope(id="t", sender="claude", branch="b", recipient="x", kind="request",
                    sent_at="x", payload={"audit_vote_expected": True}, run_id=run_id)

def ok(text):
    return SimpleNamespace(ok=True, result=text, error=None)

def run_panel(run_id, seat_b_reply):
    # consumer first so its group sees entries from id 0 (matches the eval-e2e ordering lesson)
    consumer = AuditConsumer(audit_redis, conn_factory, prefix=PREFIX, block_ms=50)
    AuditRun(audit_redis, run_id, prefix=PREFIX).emit("orchestrator", "dispatch", {"roster": ROSTER})  # seq 1
    seat_bridge("A")._emit_vote(req(run_id), ok(FENCED_APPROVE))      # seat:A vote (seq 2)
    seat_bridge("B")._emit_vote(req(run_id), ok(seat_b_reply))        # seat:B vote (or none if bare)
    drained = 0
    while consumer.step() is not None:
        drained += 1
    verdict = {"roster": ROSTER, "stances": {"seat:A": "approve", "seat:B": "approve"}}
    with psycopg.connect(DSN) as conn:
        rows = conn.execute("SELECT seq, kind, payload->>'actor' FROM audit_events WHERE run_id=%s ORDER BY seq",
                            (run_id,)).fetchall()
        result = reconcile(conn, run_id, verdict)
    return rows, result, drained

print(f"E2E audit round-trip  ok-run={RUN_OK}  bad-run={RUN_BAD}  prefix={PREFIX}")
try:
    with psycopg.connect(DSN) as c:
        c.autocommit = True
        c.execute(open("src/arb_memory/schema.sql", encoding="utf-8").read())

    # POSITIVE
    rows, result, drained = run_panel(RUN_OK, FENCED_APPROVE)
    kinds = [r[1] for r in rows]
    seqs = [r[0] for r in rows]
    check(kinds == ["dispatch", "vote", "vote"], f"manifest(seq1)+2 votes landed: {kinds}")
    check(seqs[0] == 1, f"manifest is seq 1 (got {seqs[0]})")
    check(sorted(r[2] for r in rows if r[1] == "vote") == ["seat:A", "seat:B"], "both seat votes correlated by actor")
    check(result["ok"] is True, f"reconcile OK for complete panel (gaps={result.get('gaps')})")

    # NEGATIVE — seat B reply is bare JSON: require_fence=True rejects -> no vote -> reconcile fail-loud
    rows_b, result_b, _ = run_panel(RUN_BAD, BARE_JSON)
    votes_b = sorted(r[2] for r in rows_b if r[1] == "vote")
    check(votes_b == ["seat:A"], f"bare-JSON reply produced NO vote for seat:B (votes={votes_b})")
    check(result_b["ok"] is False, "reconcile REFUSES the verdict (missing seat:B vote)")
    check(any("seat:B" in g and "never voted" in g for g in result_b["gaps"]),
          f"fail-loud gap names the never-voted seat: {result_b['gaps']}")
finally:
    with psycopg.connect(DSN) as conn:
        conn.autocommit = True
        conn.execute("DELETE FROM audit_events WHERE run_id LIKE 'e2e-audit-%'")
    for k in audit_redis.scan_iter(f"{PREFIX}*"):
        audit_redis.delete(k)
    print("cleanup: removed e2e audit rows + stream/seq keys")

print()
if failures:
    print(f"E2E FAILED — {len(failures)} check(s): " + "; ".join(failures)); sys.exit(1)
print("E2E PASS — manifest+bridge votes reconcile; bare-JSON reply → no vote → verdict REFUSED (fail-loud).")

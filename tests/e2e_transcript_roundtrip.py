"""E2E gate for full-fidelity transcript capture.

Drives the real transcript path across real local Redis and Postgres:
Bridge hot-path queue item shape -> TranscriptFlusher daemon thread ->
trace Redis stream -> TranscriptConsumer -> transcript_io.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid

import psycopg
import pytest
import redis as redislib

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.redact import REDACTED
from agent_redis_bridge.transcript_flusher import TranscriptFlusher
from arb_memory.transcript import TranscriptConsumer


DSN = os.environ["ARB_MEMORY_DSN"]
TRACE_URL = os.environ.get("ARB_TRACE_REDIS_URL", "redis://127.0.0.1:6379/15")
TEXT_SECRET = "export API_KEY=AKIA1234567890ABCDEF"
OUTPUT_SECRET = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.deadbeefdeadbeef.signature"
TOOL_NAME_SECRET = 'curl -H "Authorization: Bearer eyJtoolnametoolnametoolnametoolname" https://example.invalid'


def _check(condition, message, failures):
    print(("  ok: " if condition else "  FAIL: ") + message)
    if not condition:
        failures.append(message)


def _conn_factory():
    conn = psycopg.connect(DSN)
    conn.autocommit = True
    return conn


def _cleanup(redis_client, prefix, run_id, task_id):
    with psycopg.connect(DSN) as conn:
        conn.autocommit = True
        conn.execute("DELETE FROM transcript_io WHERE run_id = %s OR task_id = %s", (run_id, task_id))
        conn.execute("DELETE FROM transcript_deadletter WHERE run_id = %s OR task_id = %s", (run_id, task_id))
    keys = list(redis_client.scan_iter(f"{prefix}*"))
    if keys:
        redis_client.delete(*keys)


def _assert_no_residue(redis_client, prefix, run_id, task_id):
    with psycopg.connect(DSN) as conn:
        transcript_rows = conn.execute(
            "SELECT count(*) FROM transcript_io WHERE run_id = %s OR task_id = %s",
            (run_id, task_id),
        ).fetchone()[0]
        deadletter_rows = conn.execute(
            "SELECT count(*) FROM transcript_deadletter WHERE run_id = %s OR task_id = %s",
            (run_id, task_id),
        ).fetchone()[0]
    keys = list(redis_client.scan_iter(f"{prefix}*"))
    assert transcript_rows == 0
    assert deadletter_rows == 0
    assert keys == []


def _item(
    *,
    task_id,
    run_id,
    seat_id,
    orchestrator,
    event,
    turn_id,
    item_id,
    seq,
    kind,
    delta=None,
    content=None,
    tool_name="",
):
    data = {
        "turn_id": turn_id,
        "item_id": item_id,
        "seq": seq,
        "kind": kind,
    }
    if delta is not None:
        data["delta"] = delta
    if content is not None:
        data["content"] = content
    if tool_name:
        data["tool_name"] = tool_name
    return {
        "task_id": task_id,
        "run_id": run_id,
        "seat_id": seat_id,
        "orchestrator": orchestrator,
        "event": event,
        "turn_id": turn_id,
        "item_id": item_id,
        "kind": kind,
        "seq": seq,
        "data": data,
    }


def _turn_end(*, task_id, run_id, seat_id, orchestrator, seq):
    return {
        "task_id": task_id,
        "run_id": run_id,
        "seat_id": seat_id,
        "orchestrator": orchestrator,
        "event": "turn_end",
        "turn_id": task_id,
        "item_id": f"{task_id}:turn_end",
        "kind": "turn_end",
        "seq": seq,
        "data": {},
    }


def _wait_for_xlen(redis_client, stream, expected, timeout_s=5):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        length = redis_client.xlen(stream)
        if length >= expected:
            return length
        time.sleep(0.05)
    return redis_client.xlen(stream)


def _drain(consumer):
    drained = 0
    while consumer.step() is not None:
        drained += 1
    return drained


def _prove_backpressure_guard():
    bridge = Bridge.__new__(Bridge)
    bridge.agent_id = "codex-e2e"
    bridge._transcript_enabled = True
    bridge._transcript_seq = 0
    bridge._transcript_truncated = 0
    request = Envelope(
        id="e2e-backpressure-task",
        sender="claude-e2e",
        branch="feat/arb-ff",
        recipient="codex-e2e",
        kind="request",
        sent_at="x",
        payload={},
        run_id="e2e-backpressure-run",
    )

    for event in ("model_text", "model_thinking", "command_output"):
        bridge._transcript_q = queue.Queue(maxsize=1)
        bridge._transcript_q.put_nowait({"filled": True})
        bridge._transcript_truncated = 0
        started = time.monotonic()
        bridge._capture(
            request,
            event,
            {"delta": "x", "turn_id": "turn-bp", "item_id": f"turn-bp:{event}", "kind": event, "seq": 1},
        )
        assert time.monotonic() - started < 0.05
        assert bridge._transcript_truncated == 1


def test_full_fidelity_transcript_roundtrip_real_flusher_trace_consumer_postgres():
    redis_client = redislib.from_url(TRACE_URL, decode_responses=True)
    try:
        redis_client.ping()
    except redislib.RedisError as exc:
        pytest.skip(f"no redis for transcript E2E: {exc}")

    run_tag = uuid.uuid4().hex[:8]
    run_id = f"e2e-transcript-{run_tag}"
    task_id = f"e2e-transcript-task-{run_tag}"
    prefix = f"e2e_transcript_{run_tag}:"
    stream = f"{prefix}arbmem:trace"
    seat_id = "codex-e2e"
    orchestrator = "claude-e2e"
    failures: list[str] = []

    print(f"E2E transcript round-trip run_id={run_id} task_id={task_id} stream={stream}")
    print(f"  planted model_text secret: {TEXT_SECRET}")
    print(f"  planted command_output secret: {OUTPUT_SECRET}")
    print(f"  planted tool_name secret: {TOOL_NAME_SECRET}")

    with psycopg.connect(DSN) as conn:
        conn.autocommit = True
        conn.execute(open("src/arb_memory/schema.sql", encoding="utf-8").read())

    _cleanup(redis_client, prefix, run_id, task_id)

    q: queue.Queue[dict] = queue.Queue()
    consumer = TranscriptConsumer(redis_client, _conn_factory, prefix=prefix, block_ms=50)
    flusher = TranscriptFlusher(q, redis_client, prefix, poll_s=0.05)
    thread = threading.Thread(target=flusher.run, daemon=True)

    apply_patch_command = """apply_patch <<'PATCH'
*** Begin Patch
*** Update File: e2e_file.py
@@
-old
+new
+another
*** End Patch
PATCH"""

    try:
        thread.start()
        q.put(
            _item(
                task_id=task_id,
                run_id=run_id,
                seat_id=seat_id,
                orchestrator=orchestrator,
                event="model_text",
                turn_id="turn-1",
                item_id="turn-1:text",
                seq=1,
                kind="model_text",
                delta="hello ",
            )
        )
        q.put(
            _item(
                task_id=task_id,
                run_id=run_id,
                seat_id=seat_id,
                orchestrator=orchestrator,
                event="model_text",
                turn_id="turn-1",
                item_id="turn-1:text",
                seq=2,
                kind="model_text",
                delta=f"secret {TEXT_SECRET}",
            )
        )
        q.put(
            _item(
                task_id=task_id,
                run_id=run_id,
                seat_id=seat_id,
                orchestrator=orchestrator,
                event="command_started",
                turn_id="turn-1",
                item_id="turn-1:bash",
                seq=3,
                kind="command_started",
                content="bash: export then print",
                tool_name=TOOL_NAME_SECRET,
            )
        )
        q.put(
            _item(
                task_id=task_id,
                run_id=run_id,
                seat_id=seat_id,
                orchestrator=orchestrator,
                event="command_output",
                turn_id="turn-1",
                item_id="turn-1:bash",
                seq=4,
                kind="command_output",
                content=f"stdout contained {OUTPUT_SECRET}",
                tool_name=TOOL_NAME_SECRET,
            )
        )
        q.put(
            _item(
                task_id=task_id,
                run_id=run_id,
                seat_id=seat_id,
                orchestrator=orchestrator,
                event="command_finished",
                turn_id="turn-1",
                item_id="turn-1:bash",
                seq=5,
                kind="command_finished",
                content=" exit=0",
                tool_name=TOOL_NAME_SECRET,
            )
        )
        q.put(
            _item(
                task_id=task_id,
                run_id=run_id,
                seat_id=seat_id,
                orchestrator=orchestrator,
                event="command_started",
                turn_id="turn-1",
                item_id="turn-1:patch",
                seq=6,
                kind="command_started",
                content=apply_patch_command,
                tool_name="apply_patch",
            )
        )
        q.put(_turn_end(task_id=task_id, run_id=run_id, seat_id=seat_id, orchestrator=orchestrator, seq=7))

        live_count = _wait_for_xlen(redis_client, stream, 3)
        _check(live_count == 3, f"real flusher daemon wrote 3 coalesced live entries (got {live_count})", failures)

        live_entries = redis_client.xrange(stream)
        live_fields = [fields for _entry_id, fields in live_entries]
        model_entries = [fields for fields in live_fields if fields.get("kind") == "model_text"]
        patch_entries = [fields for fields in live_fields if fields.get("tool_name") == "apply_patch"]
        live_blob = json.dumps(live_fields, sort_keys=True)
        _check(len(model_entries) == 1, f"model_text deltas coalesced to one live entry (got {len(model_entries)})", failures)
        _check(TEXT_SECRET not in live_blob, "model_text planted secret absent from live trace", failures)
        _check(OUTPUT_SECRET not in live_blob, "command_output planted secret absent from live trace", failures)
        _check(TOOL_NAME_SECRET not in live_blob, "tool_name planted secret absent from live trace", failures)
        _check(REDACTED in model_entries[0].get("content", ""), "model_text live entry contains redaction marker", failures)
        bash_entries = [fields for fields in live_fields if fields.get("kind") == "command_started"]
        _check(
            bool(bash_entries) and REDACTED in bash_entries[0].get("content", ""),
            "command_output live entry contains redaction marker",
            failures,
        )
        _check(
            bool(bash_entries) and REDACTED in bash_entries[0].get("tool_name", ""),
            "tool_name live entry contains redaction marker",
            failures,
        )
        _check(len(patch_entries) == 1, f"apply_patch surfaced in live trace (got {len(patch_entries)})", failures)

        drained = _drain(consumer)
        _check(drained == 3, f"real TranscriptConsumer drained 3 trace entries (got {drained})", failures)
        before_redrain = None
        with psycopg.connect(DSN) as conn:
            rows = conn.execute(
                """
                SELECT kind, tool_name, content, meta, stream_entry_id
                FROM transcript_io
                WHERE run_id = %s AND task_id = %s
                ORDER BY id
                """,
                (run_id, task_id),
            ).fetchall()
            before_redrain = len(rows)
        _check(len(rows) == 3, f"3 rows persisted to transcript_io for this run (got {len(rows)})", failures)

        durable_blob = json.dumps(
            [{"kind": row[0], "tool_name": row[1], "content": row[2], "meta": row[3]} for row in rows],
            ensure_ascii=False,
            sort_keys=True,
        )
        _check(TEXT_SECRET not in durable_blob, "model_text planted secret absent from transcript_io", failures)
        _check(OUTPUT_SECRET not in durable_blob, "command_output planted secret absent from transcript_io", failures)
        _check(TOOL_NAME_SECRET not in durable_blob, "tool_name planted secret absent from transcript_io", failures)
        _check(REDACTED in durable_blob, "durable rows contain redaction marker", failures)
        _check(
            any(row[1] and REDACTED in row[1] for row in rows),
            "durable tool_name contains redaction marker",
            failures,
        )
        patch_row = next(row for row in rows if row[1] == "apply_patch")
        _check(
            patch_row[3] == {"file": "e2e_file.py", "added": 2, "removed": 1},
            f"apply_patch meta persisted with correct counts (got {patch_row[3]})",
            failures,
        )

        _drain(consumer)
        consumer.drain_pending()
        with psycopg.connect(DSN) as conn:
            after_redrain = conn.execute(
                "SELECT count(*) FROM transcript_io WHERE run_id = %s AND task_id = %s",
                (run_id, task_id),
            ).fetchone()[0]
        _check(after_redrain == before_redrain, "re-draining same stream adds no duplicate transcript_io rows", failures)

        _prove_backpressure_guard()
        _check(True, "backpressure guard: full queue returns immediately and increments truncation", failures)
    finally:
        flusher.stop()
        thread.join(timeout=2)
        _cleanup(redis_client, prefix, run_id, task_id)

    _assert_no_residue(redis_client, prefix, run_id, task_id)
    _check(True, "0 residue after cleanup for scoped Redis keys and transcript rows", failures)

    if failures:
        raise AssertionError("; ".join(failures))

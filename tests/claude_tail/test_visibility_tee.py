import json
from datetime import datetime, timedelta, timezone

from arb_memory.visibility import tee_states
from agent_redis_bridge.transcript_flusher import TRUNCATED, TranscriptFlusher
from agent_redis_bridge.visibility_tee import live_tee, trace_tee


class RecRedis:
    def __init__(self):
        self.xadds = []
        self.expires = []

    def xadd(self, key, fields, id="*", maxlen=None, approximate=True):
        self.xadds.append((key, fields, {"id": id, "maxlen": maxlen, "approximate": approximate}))
        return b"1-0"

    def expire(self, key, ttl):
        self.expires.append((key, ttl))
        return True


def test_live_tee_emits_exact_events_live_fields():
    r = RecRedis()
    live_tee(
        r,
        "agent_scratch:",
        run_id="run-1",
        task_id="t1",
        seat_id="codex-1",
        orchestrator="claude-bridge-dev",
        event_type="task_started",
        sent_at="2026-06-28T00:00:00+00:00",
        data={"task_id": "t1"},
        maxlen=500,
        ttl=60,
    )
    key, fields, kw = r.xadds[0]
    assert key == "agent_scratch:events:live"
    assert fields == {
        "run_id": "run-1",
        "task_id": "t1",
        "seat_id": "codex-1",
        "orchestrator": "claude-bridge-dev",
        "event_type": "task_started",
        "sent_at": "2026-06-28T00:00:00+00:00",
        "data": json.dumps({"task_id": "t1"}, separators=(",", ":")),
    }
    assert kw == {"id": "*", "maxlen": 500, "approximate": True}
    assert r.expires == [("agent_scratch:events:live", 60)]


def test_live_tee_works_against_redis_py_signature_client():
    r = RecRedis()
    entry_id = live_tee(
        r,
        "agent_scratch:",
        run_id="run-1",
        task_id="t1",
        seat_id="claude-bridge-dev",
        orchestrator="claude-bridge-dev",
        event_type="task_continuing",
        sent_at="2026-06-28T00:00:00+00:00",
        data={"kind": "task_continuing"},
        maxlen=500,
        ttl=60,
    )

    assert entry_id == b"1-0"
    assert r.xadds[0][2] == {"id": "*", "maxlen": 500, "approximate": True}
    assert r.expires == [("agent_scratch:events:live", 60)]


def test_trace_tee_emits_exact_arbmem_trace_fields():
    r = RecRedis()
    trace_tee(
        r,
        "agent_scratch:",
        run_id="run-1",
        task_id="t1",
        seat_id="codex-1",
        orchestrator="claude-bridge-dev",
        event="command_output",
        turn_index=2,
        data={
            "content": "hello",
            "tool_name": "Bash",
            "item_id": "turn-1:tool:1",
            "kind": "command_output",
        },
        seq=7,
        maxlen=500,
    )
    key, fields, kw = r.xadds[0]
    assert key == "agent_scratch:arbmem:trace"
    assert "ts" in fields
    fields = {k: v for k, v in fields.items() if k != "ts"}
    assert fields == {
        "run_id": "run-1",
        "task_id": "t1",
        "seat_id": "codex-1",
        "orchestrator": "claude-bridge-dev",
        "turn_index": "2",
        "item_id": "turn-1:tool:1",
        "seq": "7",
        "kind": "command_output",
        "tool_name": "Bash",
        "content": "hello",
    }
    assert kw == {"id": "*", "maxlen": 500, "approximate": True}


def test_trace_tee_redacts_content_and_tool_name_like_transcript_flusher():
    r = RecRedis()

    def redactor(text):
        return text.replace("password=hunter2", "password=‹redacted›").replace(
            "sk-abc123",
            "‹redacted-token›",
        )

    trace_tee(
        r,
        "agent_scratch:",
        run_id="run-1",
        task_id="t1",
        seat_id="codex-1",
        orchestrator="claude-bridge-dev",
        event="command_output",
        turn_index=3,
        data={
            "content": "Authorization: Bearer sk-abc123",
            "tool_name": "curl password=hunter2",
            "item_id": "turn-1:tool:1",
            "kind": "command_output",
        },
        seq=8,
        maxlen=500,
        redactor=redactor,
    )

    fields = r.xadds[0][1]
    assert fields["content"] == redactor("Authorization: Bearer sk-abc123")
    assert fields["tool_name"] == redactor("curl password=hunter2")


def test_trace_tee_caps_content_like_transcript_flusher():
    r = RecRedis()
    trace_tee(
        r,
        "agent_scratch:",
        run_id="run-1",
        task_id="t1",
        seat_id="codex-1",
        orchestrator="claude-bridge-dev",
        event="model_text",
        turn_index=3,
        data={
            "content": "x" * (TranscriptFlusher.content_cap + 10),
            "item_id": "turn-1:text",
            "kind": "model_text",
        },
        seq=8,
        maxlen=500,
    )

    content = r.xadds[0][1]["content"]
    assert len(content) <= TranscriptFlusher.content_cap + len(TRUNCATED)
    assert content.endswith(TRUNCATED)


def test_trace_tee_drops_empty_text_for_text_bearing_kind():
    r = RecRedis()
    entry_id = trace_tee(
        r,
        "agent_scratch:",
        run_id="run-1",
        task_id="t1",
        seat_id="codex-1",
        orchestrator="claude-bridge-dev",
        event="model_text",
        turn_index=3,
        data={
            "content": "",
            "item_id": "turn-1:text",
            "kind": "model_text",
        },
        seq=8,
        maxlen=500,
    )

    assert entry_id is None
    assert r.xadds == []


def test_trace_tee_drops_missing_kind_or_item_id():
    r = RecRedis()
    missing_item_id = trace_tee(
        r,
        "agent_scratch:",
        run_id="run-1",
        task_id="t1",
        seat_id="codex-1",
        orchestrator="claude-bridge-dev",
        event="model_text",
        turn_index=3,
        data={"content": "hello", "kind": "model_text"},
        seq=8,
        maxlen=500,
    )
    missing_kind = trace_tee(
        r,
        "agent_scratch:",
        run_id="run-1",
        task_id="t1",
        seat_id="codex-1",
        orchestrator="claude-bridge-dev",
        event="",
        turn_index=3,
        data={"content": "hello", "item_id": "turn-1:text"},
        seq=9,
        maxlen=500,
    )

    assert missing_item_id is None
    assert missing_kind is None
    assert r.xadds == []


class TeeFakeRedis:
    def __init__(self, values):
        self.values = values

    def mget(self, keys):
        return [self.values.get(k) for k in keys]


def _hb(ts, stale_after=330, **extra):
    payload = {
        "ts": ts.isoformat(), "pid": 1, "started_at": ts.isoformat(),
        "tailers": 1, "failing_tailers": 0, "skipped_lines": 0,
        "last_emit_at": None, "stale_after_s": stale_after,
    }
    payload.update(extra)
    return json.dumps(payload)


def test_tee_states_fresh_stale_missing():
    now = datetime.now(timezone.utc)
    redis = TeeFakeRedis({
        "agent_scratch:tail:heartbeat:a": _hb(now - timedelta(seconds=10)),
        "agent_scratch:tail:heartbeat:b": _hb(now - timedelta(seconds=1000)),
    })
    out = tee_states(redis, "agent_scratch:", ["a", "b", "c"], now)
    by = {t["label"]: t for t in out}
    assert by["a"]["state"] == "fresh"
    assert by["b"]["state"] == "stale"
    assert by["b"]["ts"]  # "stale since <ts>" evidence retained (spec §C)
    assert by["c"]["state"] == "missing"


def test_tee_states_staleness_uses_payload_stale_after_s():
    # spec §C (panel r2 cold-Opus P2-1): the daemon's own stale_after_s wins,
    # not a hardcoded 330.
    now = datetime.now(timezone.utc)
    redis = TeeFakeRedis({
        "agent_scratch:tail:heartbeat:slow": _hb(now - timedelta(seconds=500), stale_after=1200),
    })
    out = tee_states(redis, "agent_scratch:", ["slow"], now)
    assert out[0]["state"] == "fresh"  # 500s old but stale_after 1200


def test_tee_states_malformed_payload_is_stale_not_crash():
    now = datetime.now(timezone.utc)
    redis = TeeFakeRedis({"agent_scratch:tail:heartbeat:x": "{not json"})
    out = tee_states(redis, "agent_scratch:", ["x"], now)
    assert out[0]["state"] == "stale"

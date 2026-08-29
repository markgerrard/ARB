import json
from types import SimpleNamespace

from agent_redis_bridge.eval_tee import extract_eval_payload


class RecordingRedis:
    def __init__(self):
        self.xadds = []

    def xadd(self, key, fields, *, maxlen=None, ttl=None):
        self.xadds.append((key, fields, maxlen, ttl))
        return "1-0"


class RaisingRedis:
    def xadd(self, key, fields):
        raise TimeoutError("eval bus wedged")


def _request_with_run_id():
    from agent_redis_bridge.envelope import Envelope

    return Envelope(
        id="task-1",
        sender="claude",
        branch="manual",
        recipient="codex",
        kind="request",
        sent_at="x",
        payload={"task": "x"},
        run_id="run-1",
    )


def test_push_task_event_primary_lands_when_eval_xadd_fails():
    from agent_redis_bridge.bridge import Bridge
    from agent_redis_bridge.redis_io import RedisConfig

    b = Bridge.__new__(Bridge)
    b.redis_config = RedisConfig("127.0.0.1", "6379", "15", "agent_scratch:")
    b.redis = RecordingRedis()
    b.args = SimpleNamespace(max_task_events=500, events_ttl=60)
    b.agent_id = "codex-test"
    b.eval_redis = RaisingRedis()
    b._eval_stream = "eval:events"

    b.push_task_event(_request_with_run_id(), "task_started", {"task_id": "task-1"})

    primary = [entry for entry in b.redis.xadds if entry[0].endswith(":task:task-1:events")]
    assert len(primary) == 1
    key, fields, maxlen, ttl = primary[0]
    assert key == "agent_scratch:task:task-1:events"
    assert fields["type"] == "task_started"
    assert fields["task_id"] == "task-1"
    assert maxlen == 500
    assert ttl == 60
    assert [entry for entry in b.redis.xadds if entry[0] == "agent_scratch:events:live"]


def test_tee_payload_is_extract_only_and_stamped():
    # unit-level: the tee record the bridge builds for eval:events
    from agent_redis_bridge.bridge import build_eval_record

    rec = build_eval_record(
        run_id="run-1",
        task_id="t1",
        seat_id="codex",
        event="command_started",
        sent_at="2026-06-23T00:00:00+01:00",
        data={"command": "rm -rf /", "tool_name": "shell"},
    )
    assert rec["run_id"] == "run-1"
    assert rec["task_id"] == "t1"
    assert rec["seat_id"] == "codex"
    assert rec["event_type"] == "command_started"
    payload = json.loads(rec["payload"])
    assert payload == {"tool_name": "shell"}  # command excluded by construction
    assert "command" not in rec["payload"]


def test_tee_gate_skips_non_panel_task_without_run_id():
    # GATE (not a silent drop): a non-panel task carries no run_id and is not eval-tracked.
    from agent_redis_bridge.bridge import build_eval_record

    assert (
        build_eval_record(
            run_id=None,
            task_id="t1",
            seat_id="codex",
            event="task_started",
            sent_at="x",
            data={},
        )
        is None
    )

import json
import os

import pytest


@pytest.fixture
def eval_redis():
    redis = pytest.importorskip("redis")
    client = redis.from_url(
        os.environ.get("ARB_EVAL_REDIS_URL", "redis://127.0.0.1:6379/15"),
        decode_responses=True,
    )
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("no redis")
    client._stream = f"itest:{os.getpid()}:eval:events"
    yield client
    client.delete(client._stream)


def _bridge_with_eval(eval_redis):
    from agent_redis_bridge.bridge import Bridge

    b = Bridge.__new__(Bridge)
    b.agent_id = "codex-test"
    b.eval_redis = eval_redis
    b._eval_stream = eval_redis._stream
    return b


def test_real_tee_path_writes_extract_only_record(eval_redis):
    from agent_redis_bridge.envelope import Envelope

    b = _bridge_with_eval(eval_redis)
    req = Envelope(
        id="t1",
        sender="claude",
        branch="manual",
        recipient="codex",
        kind="request",
        sent_at="x",
        payload={"task": "x"},
        run_id="run-1",
    )
    b._tee_eval_event(
        req,
        "command_started",
        "2026-06-23T00:00:00+00:00",
        {"command": "cat /etc/passwd", "command_output": "root:x:0:0", "tool_name": "shell"},
    )
    entries = eval_redis.xrange(eval_redis._stream)
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["run_id"] == "run-1"
    assert fields["event_type"] == "command_started"
    payload = json.loads(fields["payload"])
    assert payload == {"tool_name": "shell"}  # raw command/command_output absent on the WIRE
    assert "command" not in fields["payload"]
    assert "command_output" not in fields["payload"]


def test_real_tee_noop_for_panelless_task(eval_redis):
    from agent_redis_bridge.envelope import Envelope

    b = _bridge_with_eval(eval_redis)
    req = Envelope(
        id="t2",
        sender="claude",
        branch="manual",
        recipient="codex",
        kind="request",
        sent_at="x",
        payload={"task": "x"},
    )
    b._tee_eval_event(req, "task_started", "x", {})
    assert eval_redis.xrange(eval_redis._stream) == []

class _TurnIndexHarness:
    OUT_OF_TURN = {"task_started", "task_continuing", "agent_sdk_subscription_audit"}

    def __init__(self):
        self._task_turn_index = {}

    def _stamp_turn_index(self, task_id, event, data):
        if event in self.OUT_OF_TURN:
            return data
        idx = self._task_turn_index.get(task_id, 0) or 1
        self._task_turn_index[task_id] = idx
        data.setdefault("turn_index", idx)
        if event == "turn_completed":
            self._task_turn_index[task_id] = idx + 1
        return data


def test_tool_edges_within_a_turn_share_the_ordinal():
    h = _TurnIndexHarness()
    assert h._stamp_turn_index("t", "command_started", {})["turn_index"] == 1
    assert h._stamp_turn_index("t", "command_finished", {})["turn_index"] == 1


def test_out_of_turn_events_carry_no_turn_index():
    h = _TurnIndexHarness()
    for event in _TurnIndexHarness.OUT_OF_TURN:
        assert "turn_index" not in h._stamp_turn_index("t", event, {})


def test_turn_completed_stamps_then_advances():
    h = _TurnIndexHarness()
    assert h._stamp_turn_index("t", "turn_completed", {})["turn_index"] == 1
    assert h._stamp_turn_index("t", "command_started", {})["turn_index"] == 2


def test_deterministic_rerun_reproduces_identical_ordinals():
    seq = ["command_started", "command_finished", "turn_completed", "command_started", "turn_completed"]
    def run():
        h = _TurnIndexHarness()
        return [h._stamp_turn_index("t", e, {}).get("turn_index") for e in seq]
    assert run() == run() == [1, 1, 1, 2, 2]


def test_real_bridge_shares_turn_index_and_coalesces_tool_call_id_on_both_edges():
    import json
    from types import SimpleNamespace
    from agent_redis_bridge.bridge import Bridge
    from agent_redis_bridge.envelope import Envelope
    from agent_redis_bridge.redis_io import RedisConfig

    class FakeRedis:
        def __init__(self): self.xadds, self.counters = [], {}
        def xadd(self, key, fields, *, maxlen=None, ttl=None): self.xadds.append((key, fields)); return "1-0"
        def incrby(self, key, amount, ttl=None): self.counters[key] = self.counters.get(key, 0) + amount; return self.counters[key]
        def expire(self, *a, **k): return True

    class Eval:
        def __init__(self): self.xadds = []
        def xadd(self, key, fields): self.xadds.append((key, fields)); return "1-0"

    b = Bridge.__new__(Bridge); b.redis_config = RedisConfig("127.0.0.1", "6379", "15", "agent_scratch:")
    b.redis = FakeRedis(); b.args = SimpleNamespace(max_task_events=500, events_ttl=60); b.agent_id = "codex-test"; b.eval_redis = Eval(); b._eval_stream = "eval:events"; b._task_epoch = {}; b._task_turn_index = {}
    env = Envelope(id="task-1", sender="claude", branch="manual", recipient="codex", kind="request", sent_at="x", payload={}, run_id="run-1")
    b.push_task_event(env, "command_started", {"tool_name": "Bash", "item_id": "call_7"})
    b.push_task_event(env, "command_finished", {"tool_name": "Bash", "item_id": "call_7"})
    b.push_task_event(env, "agent_sdk_subscription_audit", {"kind": "agent_sdk_subscription_audit"})
    evals = [json.loads(fields["payload"]) for _, fields in b.eval_redis.xadds]
    assert [e.get("turn_index") for e in evals[:2]] == [1, 1]
    assert [e.get("tool_call_id") for e in evals[:2]] == ["call_7", "call_7"]
    assert "turn_index" not in evals[2]

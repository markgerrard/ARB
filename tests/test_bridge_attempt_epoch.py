import json

from agent_redis_bridge.eval_tee import extract_eval_payload


class FakeRedisCli:
    def __init__(self):
        self.counters = {}

    def incrby(self, key, amount, ttl=None):
        self.counters[key] = self.counters.get(key, 0) + amount
        return self.counters[key]


class _EpochMixinHarness:
    def __init__(self, redis, prefix="p:"):
        self.redis = redis
        self._prefix = prefix
        self._task_epoch = {}
        self._events_ttl = 3600

    def _epoch_key(self, task_id):
        return f"{self._prefix}task:{task_id}:epoch"

    def _allocate_attempt_epoch(self, task_id):
        epoch = self.redis.incrby(self._epoch_key(task_id), 1, ttl=self._events_ttl)
        self._task_epoch[task_id] = epoch
        return epoch

    def _stamp_attempt_epoch(self, task_id, data):
        epoch = self._task_epoch.get(task_id)
        if epoch is not None:
            data.setdefault("attempt_epoch", epoch)
        return data


def test_snapshot_once_all_events_of_one_execution_share_one_epoch():
    h = _EpochMixinHarness(FakeRedisCli()); h._allocate_attempt_epoch("task-1")
    d1, d2 = h._stamp_attempt_epoch("task-1", {}), h._stamp_attempt_epoch("task-1", {})
    assert d1["attempt_epoch"] == d2["attempt_epoch"] == 1
    assert extract_eval_payload(d1)["attempt_epoch"] == 1


def test_recovery_rerun_allocates_a_strictly_higher_epoch():
    h = _EpochMixinHarness(FakeRedisCli()); e1 = h._allocate_attempt_epoch("task-1"); e2 = h._allocate_attempt_epoch("task-1")
    assert e2 > e1 == 1 and e2 == 2


def test_lease_takeover_successor_epoch_exceeds_live_predecessor():
    redis = FakeRedisCli(); pred = _EpochMixinHarness(redis); succ = _EpochMixinHarness(redis)
    pred._allocate_attempt_epoch("task-1"); succ._allocate_attempt_epoch("task-1")
    assert pred._stamp_attempt_epoch("task-1", {})["attempt_epoch"] == 1
    assert succ._stamp_attempt_epoch("task-1", {})["attempt_epoch"] == 2


def test_real_bridge_snapshot_once_stamps_same_epoch_on_every_eval_event():
    from types import SimpleNamespace
    from agent_redis_bridge.bridge import Bridge
    from agent_redis_bridge.envelope import Envelope
    from agent_redis_bridge.redis_io import RedisConfig

    class FakeRedis:
        def __init__(self): self.xadds, self.counters = [], {}
        def xadd(self, key, fields, *, maxlen=None, ttl=None): self.xadds.append((key, fields)); return "1-0"
        def incrby(self, key, amount, ttl=None): self.counters[key] = self.counters.get(key, 0) + amount; return self.counters[key]
        def expire(self, *a, **k): return True

    class RecordingEval:
        def __init__(self): self.xadds = []
        def xadd(self, key, fields): self.xadds.append((key, fields)); return "1-0"

    b = Bridge.__new__(Bridge); b.redis_config = RedisConfig("127.0.0.1", "6379", "15", "agent_scratch:")
    b.redis = FakeRedis(); b.args = SimpleNamespace(max_task_events=500, events_ttl=60); b.agent_id = "codex-test"
    b.eval_redis = RecordingEval(); b._eval_stream = "eval:events"; b._task_epoch = {}; b._task_turn_index = {}
    env = Envelope(id="task-1", sender="claude", branch="manual", recipient="codex", kind="request", sent_at="x", payload={"task": "x"}, run_id="run-1")
    b._task_epoch[env.id] = b.redis.incrby(b.redis_config.task_epoch_key(env.id), 1, ttl=60)
    b.push_task_event(env, "command_started", {"tool_name": "Bash", "tool_use_id": "toolu_1"})
    b.push_task_event(env, "command_finished", {"tool_name": "Bash", "tool_use_id": "toolu_1"})
    evals = [json.loads(fields["payload"]) for _key, fields in b.eval_redis.xadds]
    assert [e.get("attempt_epoch") for e in evals] == [1, 1]

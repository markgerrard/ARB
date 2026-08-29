"""Turn-liveness heartbeat: while a turn is active, the bridge tees a periodic
`turn_heartbeat` to events:live so a still-running but event-quiet seat (e.g.
agy-print blocked on the model API mid-turn) stays fresh in the visibility roster
instead of reading stale. Gated on run_id like every other live tee; throttled so
it only fires when no real live event flowed within the heartbeat interval."""

import threading
from types import SimpleNamespace

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.redis_io import RedisConfig


class RecordingRedis:
    def __init__(self):
        self.xadds = []

    def xadd(self, key, fields, **kwargs):
        self.xadds.append((key, fields, kwargs))
        return "1-0"


def _request(*, task_id="task-1", run_id="run-1"):
    return Envelope(
        id=task_id,
        sender="claude-bridge-dev",
        branch="b",
        recipient="agy-bridge-dev",
        kind="request",
        sent_at="x",
        payload={},
        run_id=run_id,
    )


def _bridge(*, heartbeat_interval=10):
    b = Bridge.__new__(Bridge)
    b.redis = RecordingRedis()
    b.redis_config = RedisConfig("h", "6379", "12", "agent_scratch:")
    b.args = SimpleNamespace(max_task_events=500, events_ttl=60, heartbeat_interval=heartbeat_interval)
    b.agent_id = "agy-bridge-dev"
    b._live_prefix = b.redis_config.prefix
    b._tee_count_lock = threading.Lock()
    b._tee_drop_count = 0
    b._tee_marker_drop_count = 0
    b.active_lock = threading.Lock()
    b.active_requests = {}
    b._last_live_tee_ts = {}
    # local synchronous live tee
    b.live_redis = b.redis
    b._live_remote = False
    b._live_flusher = None
    b.eval_redis = None
    return b


def _heartbeats(b):
    return [x for x in b.redis.xadds if x[1].get("event_type") == "turn_heartbeat"]


def test_emits_turn_heartbeat_for_quiet_active_turn_with_run_id():
    b = _bridge(heartbeat_interval=10)
    env = _request()
    b.active_requests = {env.id: env}
    b._last_live_tee_ts = {}  # no live event ever teed -> turn is quiet

    b._emit_turn_heartbeats(now=1000.0)

    hb = _heartbeats(b)
    assert len(hb) == 1
    key, fields, _ = hb[0]
    assert key == "agent_scratch:events:live"
    assert fields["run_id"] == "run-1"
    assert fields["task_id"] == "task-1"
    assert fields["seat_id"] == "agy-bridge-dev"
    assert fields["orchestrator"] == "claude-bridge-dev"


def test_no_heartbeat_when_a_real_live_event_flowed_within_the_interval():
    b = _bridge(heartbeat_interval=10)
    env = _request()
    b.active_requests = {env.id: env}
    b._last_live_tee_ts = {"task-1": 995.0}  # 5s ago, interval 10 -> still fresh

    b._emit_turn_heartbeats(now=1000.0)

    assert _heartbeats(b) == []


def test_heartbeat_without_run_id_falls_back_to_task_id():
    # cold-Opus P2-1 (roster-fix panel): un-tagged seats are now first-class in the roster (the live
    # tee falls back run_id->task_id), so the heartbeat must do the same — otherwise an un-tagged seat
    # SHOWS but goes stale after the interval despite being alive (the original agy-stale symptom).
    b = _bridge(heartbeat_interval=10)
    env = _request(run_id=None)  # id="task-1"
    b.active_requests = {env.id: env}
    b._last_live_tee_ts = {}

    b._emit_turn_heartbeats(now=1000.0)

    hb = _heartbeats(b)
    assert len(hb) == 1
    assert hb[0][1]["run_id"] == "task-1"  # fell back to task_id


def test_heartbeat_resets_the_throttle_so_next_tick_is_quiet():
    b = _bridge(heartbeat_interval=10)
    env = _request()
    b.active_requests = {env.id: env}
    b._last_live_tee_ts = {}

    b._emit_turn_heartbeats(now=1000.0)   # emits, updates last_tee to ~now via _tee_live_event
    b._emit_turn_heartbeats(now=1005.0)   # 5s later, within interval -> no second heartbeat

    assert len(_heartbeats(b)) == 1


def test_concurrent_turns_throttle_independently():
    # one chatty turn (recent real event -> no heartbeat) and one quiet turn
    # (-> heartbeat) must be throttled independently per task_id.
    b = _bridge(heartbeat_interval=10)
    chatty = _request(task_id="chatty", run_id="run-c")
    quiet = _request(task_id="quiet", run_id="run-q")
    b.active_requests = {"chatty": chatty, "quiet": quiet}
    b._last_live_tee_ts = {"chatty": 998.0}  # 2s ago -> still fresh; "quiet" absent -> stale

    b._emit_turn_heartbeats(now=1000.0)

    hb = _heartbeats(b)
    assert len(hb) == 1
    assert hb[0][1]["task_id"] == "quiet"


def test_emit_survives_tee_failure_without_touching_failure_counter():
    # A tee failure must not propagate out of the emit in a way that could reach
    # the registry heartbeat_failures counter (which kills the bridge at 3).
    class ExplodingRedis:
        def __init__(self):
            self.xadds = []

        def xadd(self, key, fields, **kwargs):
            self.xadds.append((key, fields, kwargs))
            raise TimeoutError("remote redis down")

    b = _bridge(heartbeat_interval=10)
    b.heartbeat_failures = 0
    b.live_redis = ExplodingRedis()
    env = _request()
    b.active_requests = {env.id: env}
    b._last_live_tee_ts = {}

    b._emit_turn_heartbeats(now=1000.0)  # must not raise

    assert b.heartbeat_failures == 0


def test_throttle_map_self_cleans_entries_for_finished_turns():
    # The snapshot/finally race can leave a throttle key for a turn no longer in
    # active_requests; the next tick must prune it (bounded residue, not a leak).
    b = _bridge(heartbeat_interval=10)
    env = _request(task_id="live", run_id="run-1")
    b.active_requests = {"live": env}
    b._last_live_tee_ts = {"live": 999.0, "finished-orphan": 999.0}

    b._emit_turn_heartbeats(now=2000.0)

    assert "finished-orphan" not in b._last_live_tee_ts
    assert "live" in b._last_live_tee_ts

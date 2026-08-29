import json
import threading
from types import SimpleNamespace

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.redis_io import RedisConfig
from agent_redis_bridge.stall_watch import StallWatch


class RecordingRedis:
    def __init__(self):
        self.events = []
        self.statuses = []
        self.deletes = []
        self.pushes = []
        self.expires = []

    def xadd(self, key, fields, **kwargs):
        self.events.append((key, fields, kwargs))
        return "1-0"

    def hset_key(self, key, fields, *, ttl=None):
        self.statuses.append((key, fields, ttl))

    def hdel_key(self, key, *fields):
        self.deletes.append((key, fields))

    def lpush(self, agent_id, body):
        self.pushes.append((agent_id, body))

    def lpush_key(self, key, body, *, trim=None):
        self.pushes.append((key, body))

    def expire(self, key, ttl):
        self.expires.append((key, ttl))


def _request(task_id="task-1"):
    return Envelope(
        id=task_id,
        sender="claude-bridge-dev",
        branch="dev",
        recipient="codex-bridge-dev",
        kind="request",
        sent_at="2026-07-07T00:00:00+00:00",
        payload={"task": "work"},
        run_id="run-1",
    )


def _bridge(*, after_secs=600, notify_inbox=1):
    b = Bridge.__new__(Bridge)
    b.redis = RecordingRedis()
    b.redis_config = RedisConfig("h", "6379", "12", "agent_scratch:")
    b.args = SimpleNamespace(
        max_task_events=500,
        events_ttl=60,
        status_ttl=120,
        heartbeat_interval=30,
        notify_inbox=notify_inbox,
        notify_inbox_maxlen=5000,
    )
    b.agent_id = "codex-bridge-dev"
    b.branch = "dev"
    b.eval_redis = None
    b.live_redis = b.redis
    b._live_remote = False
    b._live_flusher = None
    b._live_prefix = b.redis_config.prefix
    b._tee_count_lock = threading.Lock()
    b._tee_drop_count = 0
    b._tee_marker_drop_count = 0
    b.active_lock = threading.Lock()
    b.active_requests = {}
    b._last_live_tee_ts = {}
    b.stall_watch = StallWatch(after_secs=after_secs)
    return b


def _task_events(b, event_type):
    return [json.loads(fields["data"]) for _, fields, _ in b.redis.events if fields.get("type") == event_type]


def _notifies(b, event):
    out = []
    for _, body in b.redis.pushes:
        env = json.loads(body)
        if env.get("kind") == "notify" and env.get("payload", {}).get("event") == event:
            out.append(env)
    return out


def test_heartbeat_tick_detects_stall_without_counting_turn_heartbeat_as_progress():
    b = _bridge()
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0)

    b._emit_turn_heartbeats(now=590.0)
    b._check_stalls(now=661.0)

    assert len(_task_events(b, "stall_detected")) == 1
    assert b.redis.statuses[-1][0] == "agent_scratch:task:task-1:status"
    assert b.redis.statuses[-1][1]["stalled_at"]
    assert b.redis.statuses[-1][2] == 120
    notify = _notifies(b, "stall_detected")[0]
    assert len(_notifies(b, "stall_detected")) == 1
    assert b.redis.pushes[-1][0] == "claude-bridge-dev"
    assert notify["from"] == "codex-bridge-dev"
    assert notify["to"] == "claude-bridge-dev"
    assert notify["branch"] == "dev"
    assert notify["payload"]["data"] == {
        "task_id": "task-1",
        "seat_id": "codex-bridge-dev",
        "run_id": "run-1",
        "stalled_for_secs": 661,
    }


def test_stall_episode_rearms_after_tool_progress_and_model_delta_progress():
    b = _bridge()
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0)

    b._check_stalls(now=601.0)
    b._check_stalls(now=650.0)
    assert len(_task_events(b, "stall_detected")) == 1
    assert len(_notifies(b, "stall_detected")) == 1
    assert len([fields for _, fields, _ in b.redis.events if fields.get("type") == "stall_detected"]) == 1
    assert len([status for status in b.redis.statuses if "stalled_at" in status[1]]) == 1

    b._record_stall_progress(env, "command_output", now=700.0)
    assert b.redis.deletes[-1] == ("agent_scratch:task:task-1:status", ("stalled_at", "progress_blind"))
    b._check_stalls(now=1302.0)
    assert len(_task_events(b, "stall_detected")) == 2
    assert len(_notifies(b, "stall_detected")) == 2

    b._record_stall_progress(env, "model_thinking", now=1303.0)
    b._check_stalls(now=1903.0)
    assert len(_task_events(b, "stall_detected")) == 2
    b._record_stall_progress(env, "model_text", now=1903.0)
    b._check_stalls(now=2504.0)
    assert len(_task_events(b, "stall_detected")) == 3


def test_heartbeat_tick_refreshes_stalled_status_ttl_without_duplicate_episode():
    b = _bridge()
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0)

    b._check_stalls(now=601.0)
    b._check_stalls(now=650.0)

    stalled_statuses = [status for status in b.redis.statuses if "stalled_at" in status[1]]
    assert len(stalled_statuses) == 1
    assert stalled_statuses[0][0] == "agent_scratch:task:task-1:status"
    assert stalled_statuses[0][2] == 120
    status_expires = [expire for expire in b.redis.expires if expire[0] == "agent_scratch:task:task-1:status"]
    assert status_expires == [("agent_scratch:task:task-1:status", 120)]
    assert len(_task_events(b, "stall_detected")) == 1
    assert len(_notifies(b, "stall_detected")) == 1


def test_terminal_status_clears_stalled_at_for_completed_failed_and_cancelled():
    b = _bridge()
    states = ("completed", "failed", "cancelled")

    for state in states:
        env = _request(f"task-{state}")
        b.update_task_status(env, state=state, phase="finished", last_summary=state, ok=(state == "completed"))

    assert b.redis.deletes[-3:] == [
        ("agent_scratch:task:task-completed:status", ("stalled_at", "progress_blind")),
        ("agent_scratch:task:task-failed:status", ("stalled_at", "progress_blind")),
        ("agent_scratch:task:task-cancelled:status", ("stalled_at", "progress_blind")),
    ]


def test_stall_detection_disabled_is_silent():
    b = _bridge(after_secs=0)
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0)

    b._check_stalls(now=9999.0)

    assert _task_events(b, "stall_detected") == []
    assert _notifies(b, "stall_detected") == []


class BlipRedis(RecordingRedis):
    """Raises on the first N hset_key calls, then behaves — a transient Valkey blip."""

    def __init__(self, fail_first: int = 1):
        super().__init__()
        self.fail_remaining = fail_first

    def hset_key(self, key, fields, *, ttl=None):
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise ConnectionError("valkey blip")
        super().hset_key(key, fields, ttl=ttl)


def test_stall_episode_survives_emission_failure_and_retries_next_tick():
    # GAP-1 (panel pass 2026-07-08): check() set state.stalled=True BEFORE the
    # emissions, so a Redis blip during the detection tick dropped the episode
    # permanently — no stalled_at, no event, no notify, and no retry because the
    # next check() saw stalled=True and returned None.
    b = _bridge()
    b.redis = BlipRedis(fail_first=1)
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0)

    b._check_stalls(now=661.0)  # emission fails mid-tick; must not raise, must not lose the episode

    assert _task_events(b, "stall_detected") == []

    b._check_stalls(now=662.0)  # next tick retries the full emission set

    assert len(_task_events(b, "stall_detected")) == 1
    assert any(f.get("stalled_at") for _, f, _ in b.redis.statuses)
    assert len(_notifies(b, "stall_detected")) == 1


def _request_adhoc(task_id="task-adhoc"):
    return Envelope(
        id=task_id,
        sender="claude-bridge-dev",
        branch="dev",
        recipient="codex-bridge-dev",
        kind="request",
        sent_at="2026-07-08T00:00:00+00:00",
        payload={"task": "work"},
        run_id=None,  # ad-hoc dispatch: no run_id
    )


def _blind_bridge(*, after_secs=600):
    b = _bridge(after_secs=after_secs)
    b.args.engine = "agy-print"
    return b


def test_blind_task_emits_stall_unknown_not_stall_detected():
    # AGY-2 v2.1: an unproven channel crossing the threshold must never claim a
    # stall — status field + stall_unknown event only, no notify, no stalled_at.
    b = _blind_bridge()
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0, blind=True, blind_reason="unproven")

    b._check_stalls(now=661.0)

    assert _task_events(b, "stall_detected") == []
    assert _notifies(b, "stall_detected") == []
    assert _notifies(b, "stall_unknown") == []
    unknown = _task_events(b, "stall_unknown")
    assert unknown == [{"task_id": "task-1", "reason": "unproven", "unproven_for_secs": 661, "turn_index": 1}]
    blind_statuses = [s for s in b.redis.statuses if "progress_blind" in s[1]]
    assert blind_statuses[-1][1]["progress_blind"] == "unproven"
    assert blind_statuses[-1][2] == 120
    assert not any("stalled_at" in s[1] for s in b.redis.statuses)

    b._check_stalls(now=700.0)  # once per blind episode; later tick refreshes TTL only

    assert len(_task_events(b, "stall_unknown")) == 1
    assert ("agent_scratch:task:task-1:status", 120) in b.redis.expires


def test_progress_clears_blind_and_deletes_progress_blind_marker():
    b = _blind_bridge()
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0, blind=True, blind_reason="unproven")
    b._check_stalls(now=661.0)

    b._record_stall_progress(env, "model_text", now=700.0)

    assert b.redis.deletes[-1] == ("agent_scratch:task:task-1:status", ("stalled_at", "progress_blind"))
    # proven-live channel: a later genuine gap fires a REAL episode
    b._check_stalls(now=1301.0)
    assert len(_task_events(b, "stall_detected")) == 1


def test_progress_channel_dark_event_marks_blind_and_clears_stale_marker_only():
    b = _blind_bridge()
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0, blind=True, blind_reason="unproven")
    b._record_stall_progress(env, "model_text", now=10.0)  # proven live

    b.handle_progress(env, "progress_channel", {"state": "dark", "reason": "tracker-disabled"}, policy="trusted")

    assert b.stall_watch.is_blind(env.id)
    # stale-marker clear requested (no active episode) -> both fields deleted
    assert b.redis.deletes[-1] == ("agent_scratch:task:task-1:status", ("stalled_at", "progress_blind"))
    # the channel-state event itself reaches the task stream (observability)
    assert _task_events(b, "progress_channel") == [{"state": "dark", "reason": "tracker-disabled", "turn_index": 1}]

    b._check_stalls(now=621.0)
    assert _task_events(b, "stall_detected") == []
    unknown = _task_events(b, "stall_unknown")
    assert len(unknown) == 1
    assert unknown[0]["reason"] == "tracker-disabled"
    assert unknown[0]["unproven_for_secs"] == 611  # from last real progress, not the dark event


def test_progress_channel_dark_does_not_retract_active_stall_alarm():
    b = _bridge()
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0)
    b._check_stalls(now=661.0)
    assert len(_task_events(b, "stall_detected")) == 1
    deletes_before = list(b.redis.deletes)

    b.handle_progress(env, "progress_channel", {"state": "dark", "reason": "tracker-disabled"}, policy="trusted")

    assert b.redis.deletes == deletes_before  # active fired episode: marker NOT retracted
    assert b.stall_watch.is_blind(env.id)


def test_blind_report_survives_emission_failure_and_retries_next_tick():
    # GAP-1 parity for the blind path: a Redis blip during the stall_unknown
    # emission must re-arm the report, not silently drop the blind episode.
    b = _blind_bridge()
    b.redis = BlipRedis(fail_first=1)
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0, blind=True, blind_reason="unproven")

    b._check_stalls(now=661.0)  # hset fails; must not raise, must not lose the report
    assert _task_events(b, "stall_unknown") == []

    b._check_stalls(now=662.0)
    assert len(_task_events(b, "stall_unknown")) == 1


def test_start_stall_watch_blind_defaults_by_engine():
    # Structural blind-by-default: the bridge arms blind from its own engine
    # name — no engine event or config probe is load-bearing (closes D2b/D-inf).
    b = _blind_bridge()
    env = _request()
    b._start_stall_watch(env)
    assert b.stall_watch.is_blind(env.id)

    b2 = _bridge()
    b2.args.engine = "codex"
    env2 = _request("task-2")
    b2._start_stall_watch(env2)
    assert not b2.stall_watch.is_blind(env2.id)


def test_blind_config_warning_surfaces_divergent_stall_vs_turn_timeout():
    from agent_redis_bridge.bridge import blind_config_warning

    warning = blind_config_warning(engine="agy-print", stall_after_secs=600, turn_timeout_max=3600)
    assert warning and "--turn-timeout-max 3600" in warning and "largest grantable ceiling" in warning
    assert blind_config_warning(engine="agy-print", stall_after_secs=600, turn_timeout_max=600) is None
    assert blind_config_warning(engine="codex", stall_after_secs=600, turn_timeout_max=3600) is None
    assert blind_config_warning(engine="agy-print", stall_after_secs=0, turn_timeout_max=3600) is None


class RaisingHdelRedis(RecordingRedis):
    """hdel_key raises — a Valkey blip during the stale-marker clear."""

    def hdel_key(self, key, *fields):
        raise ConnectionError("valkey blip")


def test_progress_channel_handling_failure_never_raises_into_the_poll_thread():
    # AGY-1 class (r2 GLM N4): handle_progress runs inside the engine's poll
    # thread; a Redis failure in the dark-transition handling must degrade to a
    # warning, keep the in-memory blind mark, and never kill the thread.
    b = _blind_bridge()
    b.redis = RaisingHdelRedis()
    env = _request()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0, blind=True, blind_reason="unproven")
    b._record_stall_progress(env, "model_text", now=10.0)  # lit (hdel raise is swallowed there too)

    b.handle_progress(env, "progress_channel", {"state": "dark", "reason": "tracker-disabled"}, policy="trusted")

    assert b.stall_watch.is_blind(env.id)  # in-memory state intact despite the Redis failure


def test_stall_notify_run_id_falls_back_to_task_id_for_adhoc_dispatch():
    # GO-2 (panel-confirmed): the stall milestone was the one live path missing
    # the run_id -> task_id fallback the roster/heartbeat tees use, so an ad-hoc
    # (no-run_id) dispatch's stall notify carried run_id="" on the very channel
    # that reaches the dispatching orchestrator.
    b = _bridge()
    env = _request_adhoc()
    b.active_requests = {env.id: env}
    b.stall_watch.start(env.id, now=0.0)

    b._check_stalls(now=661.0)

    notify = _notifies(b, "stall_detected")[0]
    assert notify["payload"]["data"]["run_id"] == "task-adhoc"

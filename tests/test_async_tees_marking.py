import threading
import time
from types import SimpleNamespace

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.event_flusher import EventFlusher
from agent_redis_bridge.redis_io import RedisConfig


class RecordingRedis:
    def __init__(self):
        self.xadds = []
        self.expires = []

    def xadd(self, key, fields, **kwargs):
        self.xadds.append((key, fields, kwargs))
        return "1-0"

    # Mirror RedisCli, which exposes .expire — streams carry no per-entry TTL, so live_tee
    # bounds the stream with maxlen on xadd and applies the key TTL via a separate expire.
    def expire(self, key, ttl):
        self.expires.append((key, ttl))


class StrictRawRedis:
    def __init__(self):
        self.xadds = []

    def xadd(
        self,
        key,
        fields,
        id="*",
        maxlen=None,
        approximate=True,
        nomkstream=False,
        minid=None,
        limit=None,
    ):
        kwargs = {}
        if maxlen is not None:
            kwargs["maxlen"] = maxlen
        self.xadds.append((key, fields, kwargs))
        return "1-0"


class ExplodingRedis:
    def __init__(self):
        self.xadds = []

    def xadd(self, key, fields, **kwargs):
        self.xadds.append((key, fields, kwargs))
        raise TimeoutError("remote redis down")


def _request():
    return Envelope(
        id="task-1",
        sender="claude-bridge-dev",
        branch="b",
        recipient="codex-bridge-dev",
        kind="request",
        sent_at="x",
        payload={},
        run_id="run-1",
    )


def _bridge():
    b = Bridge.__new__(Bridge)
    b.redis = RecordingRedis()
    b.redis_config = RedisConfig("h", "6379", "12", "agent_scratch:")
    b.args = SimpleNamespace(max_task_events=500, events_ttl=60)
    b.agent_id = "codex-bridge-dev"
    b._eval_stream = "eval:events"
    b._live_prefix = b.redis_config.prefix
    b._tee_drop_count = 0
    b._tee_marker_drop_count = 0
    b._tee_count_lock = threading.Lock()
    return b


def _make_flusher(redis, stream, *, maxsize=10, on_drop=None, on_marker_drop=None):
    return EventFlusher(
        redis,
        stream,
        maxsize=maxsize,
        maxlen=500 if stream.endswith("events:live") else None,
        on_drop=on_drop,
        on_marker_drop=on_marker_drop,
    )


def test_remote_eval_tee_enqueues_without_calling_xadd_on_hot_path():
    b = _bridge()
    remote_eval = StrictRawRedis()
    live = StrictRawRedis()
    b.eval_redis = remote_eval
    b._eval_remote = True
    b.live_redis = live
    b._live_remote = True
    b._live_flusher = _make_flusher(live, "agent_scratch:events:live")
    b._eval_flusher = _make_flusher(remote_eval, "eval:events", on_drop=b._handle_async_tee_drop)

    b._tee_eval_event(_request(), "task_started", "2026-06-27T00:00:00+00:00", {"task_id": "task-1"})

    assert remote_eval.xadds == []
    assert live.xadds == []
    assert b._eval_flusher.q.qsize() == 1
    b._eval_flusher.flush_pending()
    assert len(remote_eval.xadds) == 1
    assert remote_eval.xadds[0][0] == "eval:events"


def test_live_tee_without_run_id_falls_back_to_task_id_so_seat_shows_in_roster():
    # The roster (events:live) is the human-visibility plane — a dispatch without a run_id
    # (ad-hoc / no --run-id) must still appear. Previously `if not run_id: return` dropped it,
    # so only run-tagged work (the orchestrator) showed. Fall back run_id -> task_id.
    b = _bridge()
    live = StrictRawRedis()
    b.live_redis = live
    b._live_remote = True
    b._live_flusher = _make_flusher(live, "agent_scratch:events:live")

    b._tee_live_event(
        run_id=None,
        task_id="task-9",
        seat_id="agy-bridge-dev",
        orchestrator="claude-bridge-dev",
        event_type="task_started",
        sent_at="2026-06-29T00:00:00+00:00",
        data={"task_id": "task-9"},
    )

    assert b._live_flusher.q.qsize() == 1
    b._live_flusher.flush_pending()
    assert len(live.xadds) == 1
    _key, fields, _kwargs = live.xadds[0]
    assert fields["run_id"] == "task-9"  # fell back to task_id
    assert fields["seat_id"] == "agy-bridge-dev"
    assert fields["event_type"] == "task_started"


def test_remote_live_tee_enqueues_without_calling_xadd_on_hot_path_and_flushes_with_real_xadd_signature():
    b = _bridge()
    live = StrictRawRedis()
    b.live_redis = live
    b._live_remote = True
    b._live_flusher = _make_flusher(live, "agent_scratch:events:live")

    b._tee_live_event(
        run_id="run-1",
        task_id="task-1",
        seat_id="codex-bridge-dev",
        orchestrator="claude-bridge-dev",
        event_type="task_started",
        sent_at="2026-06-27T00:00:00+00:00",
        data={"task_id": "task-1"},
    )

    assert live.xadds == []
    assert b._live_flusher.q.qsize() == 1
    b._live_flusher.flush_pending()
    assert len(live.xadds) == 1
    key, fields, kwargs = live.xadds[0]
    assert key == "agent_scratch:events:live"
    assert fields["event_type"] == "task_started"
    assert kwargs == {"maxlen": 500}


def test_default_local_live_tee_remains_synchronous_byte_unchanged():
    b = _bridge()
    b.live_redis = b.redis
    b._live_remote = False
    b._live_flusher = None
    b.eval_redis = None

    b._tee_live_event(
        run_id="run-1",
        task_id="task-1",
        seat_id="codex-bridge-dev",
        orchestrator="claude-bridge-dev",
        event_type="task_started",
        sent_at="2026-06-27T00:00:00+00:00",
        data={"task_id": "task-1"},
    )

    assert len(b.redis.xadds) == 1
    key, fields, kwargs = b.redis.xadds[0]
    assert key == "agent_scratch:events:live"
    assert fields["run_id"] == "run-1"
    # ttl is no longer an xadd kwarg (redis-py streams reject it); the stream is bounded by
    # maxlen and the key TTL is applied via a separate expire — byte-unchanged fields, ttl
    # still applied (the production RedisCli wrapper exposes .expire).
    assert kwargs == {"maxlen": 500}
    assert b.redis.expires == [("agent_scratch:events:live", 60)]


def test_remote_queue_full_queues_drop_marker_without_hot_path_xadd():
    b = _bridge()
    remote_eval = StrictRawRedis()
    live = StrictRawRedis()
    b.eval_redis = remote_eval
    b._eval_remote = True
    b.live_redis = live
    b._live_remote = True
    b._live_flusher = _make_flusher(live, "agent_scratch:events:live", maxsize=10)
    b._eval_flusher = _make_flusher(remote_eval, "eval:events", maxsize=1, on_drop=b._handle_async_tee_drop)
    assert b._eval_flusher.enqueue({"already": "full"})

    b._tee_eval_event(_request(), "task_started", "2026-06-27T00:00:00+00:00", {"task_id": "task-1"})

    assert remote_eval.xadds == []
    assert live.xadds == []
    assert b._tee_drop_count == 1
    assert b._live_flusher.q.qsize() == 1
    b._live_flusher.flush_pending()
    assert len(live.xadds) == 1
    key, fields, kwargs = live.xadds[0]
    assert key == "agent_scratch:events:live"
    assert fields["event_type"] == "dropped"
    assert fields["seat_id"] == "codex-bridge-dev"
    assert fields["run_id"] == "run-1"
    assert fields["dropped_count"] == "1"
    assert kwargs == {"maxlen": 500}


def test_remote_eval_drop_with_local_live_emits_synchronous_events_live_marker():
    b = _bridge()
    remote_eval = StrictRawRedis()
    b.eval_redis = remote_eval
    b._eval_remote = True
    b.live_redis = b.redis
    b._live_remote = False
    b._live_flusher = None
    b._eval_flusher = _make_flusher(remote_eval, "eval:events", maxsize=1, on_drop=b._handle_async_tee_drop)
    assert b._eval_flusher.enqueue({"already": "full"})

    b._tee_eval_event(_request(), "task_started", "2026-06-27T00:00:00+00:00", {"task_id": "task-1"})

    assert remote_eval.xadds == []
    assert b._tee_drop_count == 1
    assert len(b.redis.xadds) == 1
    key, fields, kwargs = b.redis.xadds[0]
    assert key == "agent_scratch:events:live"
    assert fields["event_type"] == "dropped"
    assert fields["run_id"] == "run-1"
    assert kwargs == {"maxlen": 500, "ttl": 60}


def test_remote_marker_queue_full_counts_marker_drop_without_hot_path_xadd():
    b = _bridge()
    remote_eval = StrictRawRedis()
    live = StrictRawRedis()
    b.eval_redis = remote_eval
    b._eval_remote = True
    b.live_redis = live
    b._live_remote = True
    b._live_flusher = _make_flusher(live, "agent_scratch:events:live", maxsize=1)
    b._eval_flusher = _make_flusher(remote_eval, "eval:events", maxsize=1, on_drop=b._handle_async_tee_drop)
    assert b._live_flusher.enqueue({"already": "full"})
    assert b._eval_flusher.enqueue({"already": "full"})

    b._tee_eval_event(_request(), "task_started", "2026-06-27T00:00:00+00:00", {"task_id": "task-1"})

    assert remote_eval.xadds == []
    assert live.xadds == []
    assert b._tee_drop_count == 1
    assert b._tee_marker_drop_count == 1


def test_flusher_xadd_failure_queues_drop_marker_and_counts_loss():
    b = _bridge()
    remote_eval = ExplodingRedis()
    live = StrictRawRedis()
    b.eval_redis = remote_eval
    b._eval_remote = True
    b.live_redis = live
    b._live_remote = True
    b._live_flusher = _make_flusher(live, "agent_scratch:events:live", maxsize=10)
    b._eval_flusher = _make_flusher(remote_eval, "eval:events", on_drop=b._handle_async_tee_drop)

    b._tee_eval_event(_request(), "task_started", "2026-06-27T00:00:00+00:00", {"task_id": "task-1"})
    assert remote_eval.xadds == []

    b._eval_flusher.flush_pending()

    assert len(remote_eval.xadds) == 1
    assert b._tee_drop_count == 1
    assert live.xadds == []
    b._live_flusher.flush_pending()
    assert len(live.xadds) == 1
    assert live.xadds[0][1]["event_type"] == "dropped"


class FailFirstStrictRawRedis:
    def __init__(self):
        self.calls = 0
        self.xadds = []

    def xadd(
        self,
        key,
        fields,
        id="*",
        maxlen=None,
        approximate=True,
        nomkstream=False,
        minid=None,
        limit=None,
    ):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("poisoned item")
        kwargs = {}
        if maxlen is not None:
            kwargs["maxlen"] = maxlen
        self.xadds.append((key, fields, kwargs))
        return "1-0"


def test_flusher_thread_survives_poisoned_item_and_flushes_subsequent_item():
    redis = FailFirstStrictRawRedis()

    def raising_on_drop(fields, exc):
        raise RuntimeError("bad drop callback")

    flusher = EventFlusher(redis, "eval:events", poll_s=0.01, on_drop=raising_on_drop)
    thread = threading.Thread(target=flusher.run, daemon=True)
    thread.start()
    try:
        assert flusher.enqueue({"task_id": "poison", "run_id": "run-1"})
        assert flusher.enqueue({"task_id": "next", "run_id": "run-1"})
        deadline = time.time() + 2
        while time.time() < deadline and len(redis.xadds) < 1:
            time.sleep(0.01)

        assert thread.is_alive()
        assert redis.calls >= 2
        assert flusher.process_error_count == 1
        assert len(redis.xadds) == 1
        assert redis.xadds[0][1]["task_id"] == "next"
    finally:
        flusher.stop()
        thread.join(timeout=1)

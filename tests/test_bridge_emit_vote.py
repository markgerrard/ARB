import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.engines.base import TurnResult
from agent_redis_bridge.envelope import Envelope


class RecordingRedis:
    def __init__(self): self.xadds = []; self.kv = {}
    def incr(self, k): self.kv[k] = self.kv.get(k, 0) + 1; return self.kv[k]
    def expire(self, k, s): pass
    def xadd(self, key, fields, **kw): self.xadds.append((key, fields)); return "1-0"


def _bridge(audit_redis):
    b = Bridge.__new__(Bridge)
    b.agent_id = "codex-bridge-dev"
    b.audit_redis = audit_redis
    b._audit_prefix = ""
    return b


def _req(payload, run_id="run-1"):
    return Envelope(id="t1", sender="claude", branch="b", recipient="codex-bridge-dev",
                    kind="request", sent_at="x", payload=payload, run_id=run_id)


def _ok(text): return SimpleNamespace(ok=True, result=text, error=None)


FENCED = 'reviewed\n```vote\n{"stance":"approve","severity":"none"}\n```'


def test_emits_vote_for_declared_panel():
    r = RecordingRedis()
    _bridge(r)._emit_vote(_req({"audit_vote_expected": True}), _ok(FENCED))
    assert len(r.xadds) == 1
    fields = r.xadds[0][1]
    assert fields["kind"] == "vote"
    assert json.loads(fields["payload"])["actor"] == "seat:codex-bridge-dev"
    assert json.loads(fields["payload"])["stance"] == "approve"


def test_no_vote_without_marker():
    r = RecordingRedis()
    _bridge(r)._emit_vote(_req({}), _ok(FENCED))
    assert r.xadds == []


def test_no_vote_without_run_id():
    r = RecordingRedis()
    _bridge(r)._emit_vote(_req({"audit_vote_expected": True}, run_id=None), _ok(FENCED))
    assert r.xadds == []


def test_bare_json_is_not_a_vote_guard_b():
    r = RecordingRedis()
    _bridge(r)._emit_vote(_req({"audit_vote_expected": True}), _ok('x\n{"stance":"approve","severity":"none"}'))
    assert r.xadds == []   # require_fence=True rejects bare JSON -> StanceError -> no emit (guard c)


def test_timeout_emits_synthesized_timed_out():
    r = RecordingRedis()
    res = SimpleNamespace(ok=False, result="", error="turn timed out after 60s")
    _bridge(r)._emit_vote(_req({"audit_vote_expected": True}), res)
    assert json.loads(r.xadds[0][1]["payload"])["stance"] == "timed-out"


def test_down_bus_does_not_raise_guard_d():
    class Boom:
        def incr(self, k): raise TimeoutError("audit bus wedged")
    _bridge(Boom())._emit_vote(_req({"audit_vote_expected": True}), _ok(FENCED))  # must not raise


def test_no_audit_redis_is_noop():
    b = _bridge(None)
    b._emit_vote(_req({"audit_vote_expected": True}), _ok(FENCED))  # must not raise


def test_process_request_emits_vote_before_reply(monkeypatch):
    calls = []

    class FakeRedis:
        def incrby(self, key, amount, ttl=None):
            return 1

    b = Bridge.__new__(Bridge)
    b.agent_id = "codex-bridge-dev"
    b.branch = "b"
    b.engine_name = "codex"
    b.enforce_completion = False
    b.workdir = Path.cwd()
    # ef6c5c7c added the attempt-epoch INCR inline at turn start; the skeleton
    # needs the same trio the epoch tests stub (redis / redis_config / args).
    from agent_redis_bridge.redis_io import RedisConfig

    b.redis = FakeRedis()
    b.redis_config = RedisConfig("127.0.0.1", "6379", "15", "agent_scratch:")
    b.args = SimpleNamespace(events_ttl=60, max_task_events=500)
    b.stop_event = None
    b.active_lock = mock.MagicMock()
    b.task_engines = {}
    b.active_requests = {}
    b.active_threads = {}
    b.base_cwd_turns = 0
    b.base_cwd_turn_gen = 0
    b.cancelled_tasks = set()
    b._last_stream_heartbeat = {}
    b._last_live_tee_ts = {}
    b.pool = mock.Mock()
    b.pool.release = mock.Mock()

    monkeypatch.setattr(Bridge, "_emit_vote", lambda self, e, r: calls.append("vote"))
    # `**k` absorbs keyword-only arguments on the real send_reply. 1c2af160 added
    # `turn_started=True` at the call site (bridge.py:2202) and this stub's frozen arity
    # turned the ordering guard into a permanent TypeError - red from 2026-07-20 until
    # 2026-07-26, unnoticed because a bare `pytest` aborts at collection on this host.
    monkeypatch.setattr(Bridge, "send_reply", lambda self, e, r, s, **k: calls.append("reply"))
    monkeypatch.setattr(Bridge, "record_request_started", lambda self: None)
    monkeypatch.setattr(Bridge, "update_task_status", lambda self, *a, **k: None)
    monkeypatch.setattr(Bridge, "push_task_event", lambda self, *a, **k: None)
    monkeypatch.setattr(Bridge, "send_milestone", lambda self, *a, **k: None)
    monkeypatch.setattr(Bridge, "fork_thread_if_requested", lambda self, *a, **k: None)
    monkeypatch.setattr(Bridge, "resume_thread_if_requested", lambda self, *a, **k: None)
    monkeypatch.setattr(Bridge, "reset_context_if_requested", lambda self, *a, **k: None)
    monkeypatch.setattr(Bridge, "engine_thread_id", lambda self, engine: None)
    monkeypatch.setattr(Bridge, "run_engine", lambda self, *a, **k: TurnResult(ok=True, result=FENCED))
    monkeypatch.setattr(Bridge, "drive_to_completion", lambda self, envelope, result, **k: result)
    monkeypatch.setattr(Bridge, "orchestrator_commit", lambda self, envelope, result, **k: result)
    monkeypatch.setattr(Bridge, "post_timeout_adopt", lambda self, envelope, result, **k: (result, False))
    monkeypatch.setattr(Bridge, "apply_completion_gate", lambda self, result, **k: (result, False))
    monkeypatch.setattr(Bridge, "parse_structured_for_request", lambda self, *a, **k: None)
    monkeypatch.setattr(Bridge, "write_task_result", lambda self, *a, **k: None)
    monkeypatch.setattr(Bridge, "record_turn_seconds", lambda self, seconds: None)

    b.process_request(_req({"audit_vote_expected": True}), "trusted", object())

    assert calls == ["vote", "reply"]

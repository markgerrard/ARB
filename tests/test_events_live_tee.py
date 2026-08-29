import json
from types import SimpleNamespace

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.redis_io import RedisConfig


class Rec:
    def __init__(self):
        self.xadds = []
        self.expires = []
        self.kv = {}

    def xadd(self, key, fields, **kw):
        self.xadds.append((key, fields, kw))
        return "1-0"

    def incr(self, key):
        self.kv[key] = self.kv.get(key, 0) + 1
        return self.kv[key]

    def expire(self, key, seconds):
        self.expires.append((key, seconds))
        return None


def _b():
    b = Bridge.__new__(Bridge)
    b.redis = Rec()
    b.redis_config = RedisConfig("h", "6379", "12", "agent_scratch:")
    b.live_redis = b.redis
    b._live_prefix = b.redis_config.prefix
    b.args = SimpleNamespace(max_task_events=500, events_ttl=60)
    b.agent_id = "codex-bridge-dev"
    b.audit_redis = None
    b._audit_prefix = ""
    b.eval_redis = None
    return b


def _req(payload=None, run_id="run-1"):
    return Envelope(
        id="t1",
        sender="claude-bridge-dev",
        branch="b",
        recipient="codex-bridge-dev",
        kind="request",
        sent_at="x",
        payload=payload or {},
        run_id=run_id,
    )


def _live_entries(b):
    return [(k, f, kw) for (k, f, kw) in b.redis.xadds if k.endswith("events:live")]


def test_push_task_event_tees_events_live():
    b = _b()
    b.push_task_event(_req(), "task_started", {"task_id": "t1"})

    live = _live_entries(b)
    assert len(live) == 1
    key, fields, kw = live[0]
    assert key == "agent_scratch:events:live"
    assert fields["task_id"] == "t1"
    assert fields["orchestrator"] == "claude-bridge-dev"
    assert fields["seat_id"] == "codex-bridge-dev"
    assert fields["event_type"] == "task_started"
    assert fields["run_id"] == "run-1"
    assert json.loads(fields["data"]) == {"task_id": "t1"}
    assert kw["maxlen"] == 500
    assert "ttl" not in kw
    assert b.redis.expires == [("agent_scratch:events:live", 60)]


def test_push_task_event_without_run_id_falls_back_to_task_id_in_roster():
    # The roster (events:live) is the human-visibility plane and must show every seat — including
    # ad-hoc dispatches with no run_id (previously dropped, so only the orchestrator showed in
    # arb-watch). The live tee falls back run_id->task_id so the seat appears (Run column = task_id).
    # The eval/audit plane stays run_id-gated (see _tee_eval_event / build_eval_record).
    b = _b()
    b.push_task_event(_req(run_id=None), "task_started", {"task_id": "t1"})

    live = _live_entries(b)
    assert len(live) == 1
    _key, fields, _kw = live[0]
    assert fields["run_id"] == "t1"  # fell back to the envelope/task id
    assert fields["seat_id"] == "codex-bridge-dev"
    assert fields["event_type"] == "task_started"


def test_tee_live_event_uses_distinct_live_redis_not_control_bus():
    b = _b()
    live = Rec()
    b.live_redis = live

    b._tee_live_event(
        run_id="run-1",
        task_id="t1",
        seat_id="codex-bridge-dev",
        orchestrator="claude-bridge-dev",
        event_type="task_started",
        sent_at="2026-06-27T00:00:00+00:00",
        data={"task_id": "t1"},
    )

    assert b.redis.xadds == []
    assert len(live.xadds) == 1
    key, fields, kw = live.xadds[0]
    assert key == "agent_scratch:events:live"
    assert fields["task_id"] == "t1"
    assert fields["run_id"] == "run-1"
    assert kw["maxlen"] == 500
    assert "ttl" not in kw
    assert live.expires == [("agent_scratch:events:live", 60)]


def test_emit_vote_tees_events_live_with_real_redis_config():
    b = _b()
    b.audit_redis = Rec()
    result = SimpleNamespace(
        ok=True,
        result='reviewed\n```vote\n{"stance":"approve","severity":"none"}\n```',
        error=None,
    )

    b._emit_vote(_req(payload={"audit_vote_expected": True}), result)

    assert len(b.audit_redis.xadds) == 1
    live = _live_entries(b)
    assert len(live) == 1
    key, fields, kw = live[0]
    assert key == "agent_scratch:events:live"
    assert fields["task_id"] == "t1"
    assert fields["orchestrator"] == "claude-bridge-dev"
    assert fields["seat_id"] == "codex-bridge-dev"
    assert fields["event_type"] == "vote"
    assert fields["run_id"] == "run-1"
    assert json.loads(fields["data"]) == {"stance": "approve"}
    assert kw["maxlen"] == 500
    assert "ttl" not in kw
    assert b.redis.expires == [("agent_scratch:events:live", 60)]


def test_command_secrets_are_redacted_before_events_live_xadd():
    # VIS-1 (panel-confirmed): events:live shipped raw command text + command
    # output; only client-side JS kept it off-screen. A direct authenticated
    # API reader got every secret ever typed into a shell command. Redaction
    # must be a SERVER-SIDE boundary at the XADD, not a client rendering choice.
    b = _b()
    b.push_task_event(
        _req(),
        "command_started",
        {"command": "curl -H 'Authorization: Bearer sk-ant-SECRET123456789'", "kind": "command_started"},
    )
    b.push_task_event(
        _req(),
        "command_output",
        {"delta": "export OPENAI_API_KEY=sk-proj-DEADBEEFDEADBEEF01\n", "kind": "command_output"},
    )

    blob = json.dumps([json.loads(f["data"]) for _, f, _ in _live_entries(b)])
    assert "sk-ant-SECRET123456789" not in blob
    assert "sk-proj-DEADBEEFDEADBEEF01" not in blob
    # Structure is preserved (keys still present, just scrubbed values).
    first = json.loads(_live_entries(b)[0][1]["data"])
    assert "command" in first


def test_secrets_in_nested_list_and_unlisted_fields_are_also_redacted():
    # VIS-1 hardening (panel P2, GLM + cold-Opus, 2026-07-08): the key-denylist
    # left real emitters unredacted on events:live — task_finished.summary/.error
    # and orchestrator_committed.message — plus any nested/list-carried secret.
    # Redaction now walks every string value; redact() is a no-op on non-secret
    # text so applying it broadly is safe.
    # Secret shapes redact() reliably matches (Bearer / AKIA / hex64 / secret
    # assignment), one per structural position, so this isolates RECURSION
    # coverage rather than redact()'s pattern set (whose incompleteness is
    # pre-existing and shared with the trace tee, out of scope here).
    b = _b()
    b.push_task_event(
        _req(),
        "task_finished",
        {
            "kind": "task_finished",
            "summary": "done; Bearer abcdefghij0123456789KLMNOPQRST leaked in summary",
            "error": "auth failed: AKIAIOSFODNN7EXAMPLE",
            "meta": {"nested": "password=hunter2SuperSecretValue"},
            "outputs": ["ok", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"],
        },
    )

    blob = json.dumps([json.loads(f["data"]) for _, f, _ in _live_entries(b)])
    for secret in (
        "abcdefghij0123456789KLMNOPQRST",
        "AKIAIOSFODNN7EXAMPLE",
        "hunter2SuperSecretValue",
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    ):
        assert secret not in blob, f"{secret} survived redaction"
    payload = json.loads(_live_entries(b)[0][1]["data"])
    assert set(payload) >= {"summary", "error", "meta", "outputs"}  # structure preserved


def test_stall_unknown_and_progress_channel_pass_the_live_tee_unmangled():
    # AGY-2 v2.1 (r2 GLM N4/N5): the new event kinds carry only closed-enum
    # reasons and integers — the recursive redaction must pass them through
    # untouched, and the closed enum (not redact()) is the path-PII boundary.
    b = _b()
    b.push_task_event(_req(), "stall_unknown", {"task_id": "task-1", "reason": "unproven", "unproven_for_secs": 661})
    b.push_task_event(_req(), "progress_channel", {"state": "dark", "reason": "tracker-disabled"})

    datas = [json.loads(f["data"]) for _, f, _ in _live_entries(b)]
    assert {"task_id": "task-1", "reason": "unproven", "unproven_for_secs": 661, "turn_index": 1} in datas
    assert {"state": "dark", "reason": "tracker-disabled", "turn_index": 1} in datas
    kinds = [f.get("event_type") or f.get("type") for _, f, _ in _live_entries(b)]
    assert "stall_unknown" in kinds and "progress_channel" in kinds

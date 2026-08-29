import json
import os

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from agent_redis_bridge.claude_tail.__main__ import run_loop
from agent_redis_bridge.claude_tail.finality import FinalityManager
from agent_redis_bridge.claude_tail.service import Service, build_service_from_env


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.ttls.pop(key, None)

    def scan_iter(self, match=None):
        import fnmatch

        for key in list(self.values):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    def hgetall(self, key):
        return {}


class FakeTailer:
    instances = []

    def __init__(
        self,
        path,
        identity,
        offset_store,
        live_redis,
        trace_redis,
        prefix,
        redactor,
        eval_redis=None,
        eval_stream=None,
        trace_prefix="",
        cold_agent_id=None,
        cold_session_id=None,
        identity_locked=False,
    ):
        self.path = path
        self.identity = identity
        self.cold_agent_id = cold_agent_id
        self.cold_session_id = cold_session_id
        self.identity_locked = identity_locked
        self.live_redis = live_redis
        self.trace_redis = trace_redis
        self.eval_redis = eval_redis
        self.eval_stream = eval_stream
        self.prefix = prefix
        self.trace_prefix = trace_prefix
        self.poll_count = 0
        self.finished = []
        self.completed = False
        self.at_eof = True
        self.progressed = False
        self.emit_failing = False
        self.skipped_lines = 0
        self.poll_exc = None
        FakeTailer.instances.append(self)

    def poll(self):
        self.poll_count += 1
        if self.poll_exc is not None:
            raise self.poll_exc
        return 0

    def finish(self, *, ok=True):
        self.finished.append(ok)
        return {"event_type": "task_finished", "data": {"ok": ok}}


@pytest.fixture(autouse=True)
def _clear_fake_tailer_instances():
    FakeTailer.instances.clear()


class HashRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.hashes = {}
        self.hdels = []

    def hgetall(self, key):
        return self.hashes.get(key, {})

    def hdel(self, key, field):
        self.hdels.append((key, field))
        self.hashes.get(key, {}).pop(field, None)


def _write_json(path, value):
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")


def _write_registry(path, warm_transcript):
    _write_json(
        path,
        [{"session_id": "warm-session", "transcript_path": str(warm_transcript), "project": "bridge", "workspace": "dev"}],
    )


def test_tick_discovers_warm_and_cold_tailers_of_right_kind(tmp_path):
    warm_transcript = tmp_path / "warm.jsonl"
    cold_dir = tmp_path / "tasks"
    cold_transcript = cold_dir / "agent-7.output"
    registry = tmp_path / "registry.json"
    warm_transcript.write_text("", encoding="utf-8")
    cold_dir.mkdir()
    cold_transcript.write_text("", encoding="utf-8")
    _write_json(
        registry,
        [
            {
                "session_id": "warm-session",
                "transcript_path": str(warm_transcript),
                "seat_id": "claude-bridge-dev",
                "run_id": "warm-session",
                "project": "bridge",
                "workspace": "dev",
            }
        ],
    )
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(registry),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
    )

    polled = service.tick()

    assert polled == 2
    warm = next(tailer for tailer in FakeTailer.instances if tailer.path == str(warm_transcript))
    cold = next(tailer for tailer in FakeTailer.instances if tailer.path == str(cold_transcript))
    assert warm.identity.run_id == "warm-session"
    assert warm.identity.seat_id == "claude-bridge-dev"
    assert warm.cold_agent_id is None
    assert warm.cold_session_id is None
    assert cold.identity.run_id == "agent-7"
    assert cold.identity.seat_id == "cold-opus-agent-7"
    assert cold.cold_agent_id == "agent-7"
    assert cold.cold_session_id == "agent-7"
    assert warm.poll_count == 1
    assert cold.poll_count == 1
    service.tick()
    assert len(FakeTailer.instances) == 2
    assert warm.poll_count == 2
    assert cold.poll_count == 2


def test_cold_sidecar_overrides_orchestrator_and_locks_identity(tmp_path):
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-7.output").write_text("", encoding="utf-8")
    _write_json(cold_dir / "agent-7.arb-tail.json", {"orchestrator": "claude-bridge-dev", "completed": False})
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
    )

    service.tick()

    cold = FakeTailer.instances[0]
    assert cold.identity.orchestrator == "claude-bridge-dev"
    assert cold.identity.seat_id == "cold-opus-agent-7"  # unchanged default, not overridden
    assert cold.identity_locked is True


def test_cold_seat_uses_project_workspace_seat_id_when_env_set(tmp_path, monkeypatch):
    # Bridge-seat parity: codex-bridge-dev / agy-bridge-dev never show a raw GUID, so a cold-Opus
    # seat shouldn't either, once the daemon knows its own project/workspace. Never run_id -- that
    # stays session-id/marker-derived per the run-id-label fix; only seat_id changes here.
    monkeypatch.setenv("ARB_CLAUDE_TAIL_PROJECT", "bridge")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_WORKSPACE", "dev")
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-9.output").write_text("", encoding="utf-8")
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
    )

    service.tick()

    cold = FakeTailer.instances[0]
    assert cold.identity.seat_id == "cold-opus-bridge-dev"
    assert cold.identity.run_id == "agent-9"  # unaffected -- still the raw agent-id fallback


def test_cold_seat_keeps_guid_seat_id_when_project_workspace_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("ARB_CLAUDE_TAIL_PROJECT", raising=False)
    monkeypatch.delenv("ARB_CLAUDE_TAIL_WORKSPACE", raising=False)
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-10.output").write_text("", encoding="utf-8")
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
    )

    service.tick()

    cold = FakeTailer.instances[0]
    assert cold.identity.seat_id == "cold-opus-agent-10"


def test_cold_seat_without_sidecar_keeps_existing_unlocked_behavior(tmp_path):
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-8.output").write_text("", encoding="utf-8")
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
    )

    service.tick()

    cold = FakeTailer.instances[0]
    assert cold.identity.orchestrator == ""
    assert cold.identity_locked is False


def test_cold_seat_with_malformed_sidecar_falls_back_to_unlocked(tmp_path):
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-9.output").write_text("", encoding="utf-8")
    (cold_dir / "agent-9.arb-tail.json").write_text("{not valid json", encoding="utf-8")
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
    )

    service.tick()

    cold = FakeTailer.instances[0]
    assert cold.identity.orchestrator == ""
    assert cold.identity_locked is False


def test_cold_seat_completed_sidecar_finishes_and_deletes_files_same_tick(tmp_path):
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    output_path = cold_dir / "agent-5.output"
    sidecar_path = cold_dir / "agent-5.arb-tail.json"
    output_path.write_text("x\n", encoding="utf-8")
    _write_json(sidecar_path, {"orchestrator": "claude-bridge-dev", "completed": False})
    now = [100.0]
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
        time_func=lambda: now[0],
        idle_finish_secs=300.0,
    )
    service.tick()
    assert FakeTailer.instances[0].finished == []  # not completed yet

    # The orchestrator's SubagentStop hook would do this rewrite -- simulate it directly.
    _write_json(sidecar_path, {"orchestrator": "claude-bridge-dev", "completed": True})
    now[0] = 100.5  # well under idle_finish_secs -- must NOT need to wait for idle-finish
    service.tick()

    assert FakeTailer.instances[0].finished == [True]
    assert not output_path.exists()
    assert not sidecar_path.exists()


def test_never_earning_cold_seat_is_deleted_after_finality_abandon(tmp_path):
    class NeverEarningTailer(FakeTailer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._turn_open = True
            self._has_started = True
            self._turn_clock_ok = True
            self._turn_start_offset = 0
            self._turn_started_ts = "2026-07-15T00:00:00+00:00"
            self._last_causal_ts = "2026-07-15T00:00:01+00:00"
            self.logical_turn_index = 1

        def finish(self, *, ok=True):
            self.finished.append(ok)
            return {"event_type": "task_finished", "data": {"ok": ok}}

    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    output_path = cold_dir / "agent-5.output"
    sidecar_path = cold_dir / "agent-5.arb-tail.json"
    output_path.write_text(
        json.dumps({"type": "user", "promptId": "p", "timestamp": "2026-07-15T00:00:00+00:00"})
        + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-07-15T00:00:01+00:00"})
        + "\n",
        encoding="utf-8",
    )
    _write_json(sidecar_path, {"completed": True})
    now = [100.0]
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=NeverEarningTailer,
        time_func=lambda: now[0],
        finality_abandon_secs=1.0,
        fd_probe=lambda inode: [{"mode": "w"}],
    )

    service.tick()  # nominate and place the hold
    now[0] = 101.0
    service.tick()  # abandon and mark the turn skipped for this service lifetime
    assert output_path.exists() and sidecar_path.exists()
    now[0] = 102.0
    service.tick()  # must fall through to finish + delete, not re-nominate

    assert FakeTailer.instances[0].finished == [True]
    assert not output_path.exists()
    assert not sidecar_path.exists()


def test_permanently_dirty_cold_seat_is_deleted_after_finality_abandon(tmp_path):
    class PermanentlyDirtyTailer(FakeTailer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._turn_open = True
            self._has_started = True
            self._turn_clock_ok = True
            self._turn_start_offset = 0
            self._turn_started_ts = "2026-07-15T00:00:00+00:00"
            self._last_causal_ts = "2026-07-15T00:00:01+00:00"
            self.logical_turn_index = 1

        def finish(self, *, ok=True):
            self.finished.append(ok)
            return {"event_type": "task_finished", "data": {"ok": ok}}

    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    output_path = cold_dir / "agent-6.output"
    sidecar_path = cold_dir / "agent-6.arb-tail.json"
    output_path.write_text(
        json.dumps({"type": "assistant", "timestamp": "2026-07-15T00:00:01+00:00"})
        + "\nnot-json\n",
        encoding="utf-8",
    )
    _write_json(sidecar_path, {"completed": True})
    now = [100.0]
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=PermanentlyDirtyTailer,
        time_func=lambda: now[0],
        finality_abandon_secs=3.0,
        finality_manager=FinalityManager(
            FakeRedis(),
            "agent_scratch:",
            fd_probe=lambda inode: [],
            abandon_secs=3.0,
            time_func=lambda: now[0],
        ),
    )

    for tick_now in (100.0, 101.0, 102.0, 103.0, 104.0):
        now[0] = tick_now
        service.tick()

    assert PermanentlyDirtyTailer.instances[0].finished == [True]
    assert not output_path.exists()
    assert not sidecar_path.exists()


def test_transiently_dirty_cold_seat_can_still_earn_before_abandon(tmp_path):
    class TransientlyDirtyTailer(FakeTailer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._turn_open = True
            self._has_started = True
            self._turn_clock_ok = True
            self._turn_start_offset = 0
            self._turn_started_ts = "2026-07-15T00:00:00+00:00"
            self._last_causal_ts = "2026-07-15T00:00:01+00:00"
            self.logical_turn_index = 1

        def finish(self, *, ok=True):
            self.finished.append(ok)
            return {"event_type": "task_finished", "data": {"ok": ok}}

    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    output_path = cold_dir / "agent-7.output"
    sidecar_path = cold_dir / "agent-7.arb-tail.json"
    clean_output = (
        json.dumps({"type": "assistant", "timestamp": "2026-07-15T00:00:01+00:00"}) + "\n"
    )
    output_path.write_text(clean_output[:-1], encoding="utf-8")
    _write_json(sidecar_path, {"completed": True})
    now = [100.0]
    events = []
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=TransientlyDirtyTailer,
        time_func=lambda: now[0],
        finality_manager=FinalityManager(
            FakeRedis(),
            "agent_scratch:",
            fd_probe=lambda inode: [],
            emit=lambda name, record: events.append(name),
            abandon_secs=10.0,
            time_func=lambda: now[0],
        ),
    )

    for tick_now in (100.0, 101.0, 102.0):
        now[0] = tick_now
        service.tick()
    output_path.write_text(clean_output, encoding="utf-8")
    for tick_now in (103.0, 104.0, 105.0):
        now[0] = tick_now
        service.tick()

    assert events == ["turn_finalized"]
    assert output_path.exists() and sidecar_path.exists()


def test_cold_seat_without_completed_sidecar_keeps_polling_normally(tmp_path):
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-6.output").write_text("x\n", encoding="utf-8")
    _write_json(cold_dir / "agent-6.arb-tail.json", {"orchestrator": "claude-bridge-dev", "completed": False})
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
        idle_finish_secs=300.0,
    )

    service.tick()
    service.tick()

    assert FakeTailer.instances[0].finished == []
    assert FakeTailer.instances[0].poll_count == 2


def test_deregistered_warm_session_finishes_once(tmp_path):
    warm_transcript = tmp_path / "warm.jsonl"
    registry = tmp_path / "registry.json"
    warm_transcript.write_text("", encoding="utf-8")
    _write_registry(registry, warm_transcript)
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(registry),
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=FakeTailer,
    )
    service.tick()
    _write_json(registry, [])

    service.tick()
    service.tick()

    assert FakeTailer.instances[0].finished == [True]


def test_idle_warm_session_does_not_finish_and_keeps_polling(tmp_path):
    # A warm orchestrator that pauses (thinking / waiting on the model / between
    # user turns) is NOT done — it is bounded by its SessionEnd hook, not by idle.
    # Idle-finishing it would abandon the tailer (finished tailers are skipped
    # forever) so its transcript would freeze mid-session.
    warm_transcript = tmp_path / "warm.jsonl"
    registry = tmp_path / "registry.json"
    warm_transcript.write_text("", encoding="utf-8")
    _write_registry(registry, warm_transcript)
    now = [100.0]
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(registry),
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=FakeTailer,
        time_func=lambda: now[0],
        idle_finish_secs=5.0,
    )
    service.tick()

    now[0] = 106.0  # well past idle_finish_secs
    service.tick()
    service.tick()

    assert FakeTailer.instances[0].finished == []        # never idle-finished
    assert FakeTailer.instances[0].poll_count == 3        # still being polled


def test_idle_cold_session_finishes_once(tmp_path):
    # A cold-Opus .output that has gone quiet past the idle threshold IS done —
    # its lifecycle is bounded by file activity, not a hook.
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    cold_transcript = cold_dir / "agent-9.output"
    cold_transcript.write_text("", encoding="utf-8")
    now = [100.0]
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
        time_func=lambda: now[0],
        idle_finish_secs=5.0,
    )
    service.tick()

    now[0] = 106.0
    service.tick()
    service.tick()

    assert FakeTailer.instances[0].finished == [True]


def test_cold_seat_completed_marker_finishes_promptly_without_idle(tmp_path):
    # When a cold seat reports completed (its [ARB_SEAT_DONE] marker), finish it this tick —
    # no need to wait out idle_finish_secs.
    class CompletingTailer(FakeTailer):
        def poll(self):
            super().poll()
            self.completed = True
            return 0

    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-3.output").write_text("x\n", encoding="utf-8")
    now = [100.0]
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=CompletingTailer,
        time_func=lambda: now[0],
        idle_finish_secs=9999.0,  # idle would never fire in this window
    )

    service.tick()  # first poll sets completed -> finish same tick

    assert FakeTailer.instances[0].finished == [True]


def test_finished_cold_seat_resumes_when_output_grows(tmp_path):
    # A cold-Opus that thinks past the idle threshold gets a "looks-done" idle-finish, then
    # writes its report. Finishing must be resumable: when the .output grows again the seat is
    # recreated (re-emits task_started, tails the rest) instead of being skipped forever.
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    cold = cold_dir / "agent-9.output"
    cold.write_text("start\n", encoding="utf-8")
    now = [100.0]
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
        time_func=lambda: now[0],
        idle_finish_secs=5.0,
    )
    service.tick()

    now[0] = 106.0
    service.tick()
    assert FakeTailer.instances[0].finished == [True]

    cold.write_text("start\nmore report output after a long think\n", encoding="utf-8")
    now[0] = 107.0
    service.tick()

    for_cold = [t for t in FakeTailer.instances if t.path == str(cold)]
    assert len(for_cold) == 2          # resumed via a fresh tailer instance
    assert for_cold[1].poll_count >= 1  # and it is being polled again


def test_poll_exception_isolated_so_other_tailers_continue(tmp_path):
    class RaisingTailer(FakeTailer):
        def poll(self):
            super().poll()
            if self.path.endswith("bad.output"):
                raise RuntimeError("bad transcript")
            return 0

    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "bad.output").write_text("", encoding="utf-8")
    (cold_dir / "good.output").write_text("", encoding="utf-8")
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=RaisingTailer,
    )

    assert service.tick() == 1

    assert {tailer.path.rsplit("/", 1)[-1]: tailer.poll_count for tailer in FakeTailer.instances} == {
        "bad.output": 1,
        "good.output": 1,
    }


def test_malformed_registry_file_and_redis_value_are_tolerated(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text("{not-json", encoding="utf-8")
    redis = FakeRedis()
    redis.values["agent_scratch:claude:tail:warm_sessions"] = "{not-json"

    file_service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), cold_dir=str(tmp_path / "tasks"), tailer_cls=FakeTailer)
    redis_service = Service(redis=redis, prefix="agent_scratch:", cold_dir=str(tmp_path / "tasks"), tailer_cls=FakeTailer)

    assert file_service.tick() == 0
    assert redis_service.tick() == 0


def test_redis_hash_registry_skips_malformed_field_and_keeps_good(tmp_path):
    warm_transcript = tmp_path / "warm.jsonl"
    warm_transcript.write_text("", encoding="utf-8")
    redis = HashRedis()
    redis.hashes["agent_scratch:claude:registry"] = {
        "good": json.dumps({"session_id": "good", "transcript_path": str(warm_transcript), "seat_id": "claude-bridge-dev", "run_id": "good"}),
        "bad": "{not-json",
    }
    service = Service(
        redis=redis,
        prefix="agent_scratch:",
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=FakeTailer,
    )

    assert service.tick() == 1
    assert FakeTailer.instances[-1].identity.run_id == "good"


def test_warm_discovery_skips_missing_transcript_but_keeps_existing_file(tmp_path):
    warm_transcript = tmp_path / "warm.jsonl"
    warm_transcript.write_text("", encoding="utf-8")
    missing_transcript = tmp_path / "gone.jsonl"
    registry = tmp_path / "registry.json"
    _write_json(
        registry,
        [
            {"session_id": "missing", "transcript_path": str(missing_transcript), "project": "bridge", "workspace": "dev"},
            {"session_id": "warm", "transcript_path": str(warm_transcript), "project": "bridge", "workspace": "dev"},
        ],
    )
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(registry),
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=FakeTailer,
    )

    assert service.tick() == 1

    assert [tailer.path for tailer in FakeTailer.instances] == [str(warm_transcript)]


def test_redis_hash_registry_prunes_missing_warm_transcript(tmp_path):
    warm_transcript = tmp_path / "warm.jsonl"
    warm_transcript.write_text("", encoding="utf-8")
    missing_transcript = tmp_path / "gone.jsonl"
    redis = HashRedis()
    key = "agent_scratch:claude:registry"
    redis.hashes[key] = {
        "missing": json.dumps({"session_id": "missing", "transcript_path": str(missing_transcript), "seat_id": "claude-missing", "run_id": "missing"}),
        "warm": json.dumps({"session_id": "warm", "transcript_path": str(warm_transcript), "seat_id": "claude-warm", "run_id": "warm"}),
    }
    service = Service(
        redis=redis,
        prefix="agent_scratch:",
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=FakeTailer,
    )

    assert service.tick() == 1

    assert redis.hdels == [(key, "missing")]
    assert set(redis.hashes[key]) == {"warm"}
    assert [tailer.identity.run_id for tailer in FakeTailer.instances] == ["warm"]


def test_registry_stat_permission_error_keeps_record_and_retries_without_churn(tmp_path, monkeypatch):
    transcript = tmp_path / "warm.jsonl"
    transcript.write_text("", encoding="utf-8")
    redis = HashRedis()
    key = "agent_scratch:claude:registry"
    redis.hashes[key] = {
        "warm": json.dumps({"session_id": "warm", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}),
    }
    service = Service(
        redis=redis,
        prefix="agent_scratch:",
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=FakeTailer,
    )
    real_stat = os.stat
    blocked = True

    def selective_stat(path, *args, **kwargs):
        if blocked and path == str(transcript):
            raise PermissionError("no stat permission")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", selective_stat)

    service.tick()

    assert redis.hdels == []
    assert FakeTailer.instances == []

    blocked = False
    service.tick()

    assert len(FakeTailer.instances) == 1
    assert FakeTailer.instances[0].poll_count == 1


def test_stale_warm_registry_entry_does_not_log_exception_or_create_tailer(tmp_path, caplog):
    redis = HashRedis()
    redis.hashes["agent_scratch:claude:registry"] = {
        "missing": json.dumps({"session_id": "missing", "transcript_path": str(tmp_path / "gone.jsonl"), "seat_id": "claude-missing", "run_id": "missing"}),
    }
    service = Service(
        redis=redis,
        prefix="agent_scratch:",
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=FakeTailer,
    )

    assert service.tick() == 0
    assert service.tick() == 0

    assert FakeTailer.instances == []
    assert service._tailers == {}
    assert "claude transcript tailer failed" not in caplog.text
    assert "Traceback" not in caplog.text


def test_warm_transcript_vanishing_mid_poll_finishes_and_removes_once(tmp_path, caplog):
    class VanishingTailer(FakeTailer):
        def poll(self):
            super().poll()
            raise FileNotFoundError(self.path)

    warm_transcript = tmp_path / "warm.jsonl"
    warm_transcript.write_text("", encoding="utf-8")
    registry = tmp_path / "registry.json"
    _write_registry(registry, warm_transcript)
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(registry),
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=VanishingTailer,
    )

    assert service.tick() == 0
    warm_transcript.unlink()
    assert service.tick() == 0

    assert len(FakeTailer.instances) == 1
    assert FakeTailer.instances[0].poll_count == 1
    assert FakeTailer.instances[0].finished == [True]
    assert service._tailers == {}
    assert "claude transcript tailer failed" not in caplog.text
    assert "Traceback" not in caplog.text
    assert "claude transcript vanished" in caplog.text


def test_cold_discovery_skips_old_output_files(tmp_path):
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    fresh = cold_dir / "fresh.output"
    old = cold_dir / "old.output"
    fresh.write_text("", encoding="utf-8")
    old.write_text("", encoding="utf-8")
    os.utime(fresh, (100.0, 100.0))
    os.utime(old, (10.0, 10.0))
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
        time_func=lambda: 101.0,
        wall_time_func=lambda: 101.0,
        cold_max_age_secs=60.0,
    )

    assert service.tick() == 1
    assert [tailer.path.rsplit("/", 1)[-1] for tailer in FakeTailer.instances] == ["fresh.output"]


def test_cold_discovery_tails_symlinked_output_when_target_is_present(tmp_path):
    cold_dir = tmp_path / "claude-home" / "tasks"
    source_dir = tmp_path / "claude-temp" / "session-1" / "tasks"
    cold_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    source = source_dir / "agent-7.output"
    source.write_text("", encoding="utf-8")
    mirror = cold_dir / "agent-7.output"
    mirror.symlink_to(source)

    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
    )

    assert service.tick() == 1
    assert FakeTailer.instances[-1].path == str(mirror)


def test_run_loop_survives_bad_tick():
    class BadService:
        def __init__(self):
            self.calls = 0

        def tick(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("bad tick")

    service = BadService()
    sleeps = []

    run_loop(service, interval=0.1, sleep_func=sleeps.append, max_ticks=2)

    assert service.calls == 2
    assert sleeps == [0.1, 0.1]


def test_build_service_from_env_uses_separate_live_trace_clients_with_keepalive(monkeypatch):
    calls = []

    class RedisFactory:
        @staticmethod
        def from_url(url, **kwargs):
            calls.append((url, kwargs))
            return f"client:{url}"

    monkeypatch.setenv("AGENT_REDIS_URL", "redis://agent/0")
    monkeypatch.setenv("AGENT_REDIS_PREFIX", "agent_scratch:")
    monkeypatch.setenv("ARB_TRACE_PREFIX", "")
    monkeypatch.setenv("ARB_LIVE_REDIS_URL", "redis://live/0")
    monkeypatch.setenv("ARB_TRACE_REDIS_URL", "redis://trace/0")
    monkeypatch.delenv("ARB_EVAL_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_EVAL_REDIS_DB", raising=False)
    monkeypatch.delenv("ARB_EVAL_PREFIX", raising=False)
    monkeypatch.setattr("redis.Redis", RedisFactory)

    service = build_service_from_env()

    assert service.redis == "client:redis://agent/0"
    assert service.live_redis == "client:redis://live/0"
    assert service.trace_redis == "client:redis://trace/0"
    assert service.prefix == "agent_scratch:"
    assert service.trace_prefix == ""
    assert [url for url, _ in calls] == ["redis://agent/0", "redis://live/0", "redis://trace/0"]
    for _, kwargs in calls:
        assert kwargs["socket_keepalive"] is True
        assert kwargs["health_check_interval"] == 30
        assert kwargs["socket_connect_timeout"] == 2.0
        assert kwargs["socket_timeout"] == 2.0


def test_service_passes_distinct_trace_prefix_to_tailers(tmp_path):
    warm_transcript = tmp_path / "warm.jsonl"
    registry = tmp_path / "registry.json"
    warm_transcript.write_text("", encoding="utf-8")
    _write_registry(registry, warm_transcript)
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        trace_prefix="",
        registry_path=str(registry),
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=FakeTailer,
    )

    assert service.tick() == 1

    assert FakeTailer.instances[0].prefix == "agent_scratch:"
    assert FakeTailer.instances[0].trace_prefix == ""


def test_service_passes_eval_sink_to_tailers(tmp_path):
    warm_transcript = tmp_path / "warm.jsonl"
    registry = tmp_path / "registry.json"
    warm_transcript.write_text("", encoding="utf-8")
    _write_registry(registry, warm_transcript)
    eval_redis = FakeRedis()
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        eval_redis=eval_redis,
        eval_stream="fleet:eval:events",
        registry_path=str(registry),
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=FakeTailer,
    )

    assert service.tick() == 1

    assert FakeTailer.instances[0].eval_redis is eval_redis
    assert FakeTailer.instances[0].eval_stream == "fleet:eval:events"


def test_build_service_from_env_warns_when_only_one_live_trace_url_is_set(monkeypatch, caplog):
    class RedisFactory:
        @staticmethod
        def from_url(url, **kwargs):
            return f"client:{url}"

    monkeypatch.setenv("AGENT_REDIS_URL", "redis://agent/0")
    monkeypatch.setenv("AGENT_REDIS_PREFIX", "agent_scratch:")
    monkeypatch.delenv("ARB_EVAL_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_EVAL_REDIS_DB", raising=False)
    monkeypatch.delenv("ARB_EVAL_PREFIX", raising=False)
    monkeypatch.setenv("ARB_LIVE_REDIS_URL", "redis://live/0")
    monkeypatch.delenv("ARB_TRACE_REDIS_URL", raising=False)
    monkeypatch.setattr("redis.Redis", RedisFactory)

    live_only = build_service_from_env()

    assert live_only.live_redis == "client:redis://live/0"
    assert live_only.trace_redis == "client:redis://live/0"
    assert "ARB_TRACE_REDIS_URL is unset" in caplog.text

    caplog.clear()
    monkeypatch.delenv("ARB_LIVE_REDIS_URL", raising=False)
    monkeypatch.setenv("ARB_TRACE_REDIS_URL", "redis://trace/0")

    trace_only = build_service_from_env()

    assert trace_only.live_redis == "client:redis://trace/0"
    assert trace_only.trace_redis == "client:redis://trace/0"
    assert "ARB_LIVE_REDIS_URL is unset" in caplog.text


def test_build_service_from_env_uses_eval_client_and_stream(monkeypatch):
    calls = []

    class RedisFactory:
        @staticmethod
        def from_url(url, **kwargs):
            calls.append((url, kwargs))
            return f"client:{url}:{kwargs.get('db', 'url-db')}"

    monkeypatch.setenv("AGENT_REDIS_URL", "redis://agent/0")
    monkeypatch.setenv("AGENT_REDIS_PREFIX", "agent_scratch:")
    monkeypatch.setenv("ARB_EVAL_REDIS_URL", "redis://eval/4")
    monkeypatch.setenv("ARB_EVAL_REDIS_DB", "6")
    monkeypatch.setenv("ARB_EVAL_PREFIX", "fleet:")
    monkeypatch.delenv("ARB_LIVE_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_TRACE_REDIS_URL", raising=False)
    monkeypatch.setattr("redis.Redis", RedisFactory)

    service = build_service_from_env()

    assert service.eval_redis == "client:redis://eval/4:6"
    assert service.eval_stream == "fleet:eval:events"
    assert ("redis://eval/4", {"decode_responses": True, "socket_keepalive": True, "health_check_interval": 30, "socket_connect_timeout": 2.0, "socket_timeout": 2.0, "db": 6}) in calls


def test_build_service_from_env_disarms_eval_when_unset(monkeypatch, caplog):
    class RedisFactory:
        @staticmethod
        def from_url(url, **kwargs):
            return f"client:{url}"

    monkeypatch.setenv("AGENT_REDIS_URL", "redis://agent/0")
    monkeypatch.delenv("ARB_EVAL_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_EVAL_REDIS_DB", raising=False)
    monkeypatch.delenv("ARB_EVAL_PREFIX", raising=False)
    monkeypatch.delenv("ARB_LIVE_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_TRACE_REDIS_URL", raising=False)
    monkeypatch.setattr("redis.Redis", RedisFactory)

    service = build_service_from_env()

    assert service.eval_redis is None
    assert service.eval_stream is None
    assert "Claude tail eval tee is disabled" in caplog.text


def _service_with_one_warm(tmp_path, redis=None):
    redis = redis or FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps([{"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}]), encoding="utf-8")
    service = Service(
        redis=redis,
        prefix="agent_scratch:",
        registry_path=str(registry),
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=FakeTailer,
    )
    return service, transcript, registry


def test_tick_reraises_redis_error_from_tailer(tmp_path):
    service, transcript, registry = _service_with_one_warm(tmp_path)
    service.tick()  # creates the tailer
    FakeTailer.instances[-1].poll_exc = RedisConnectionError("broken pipe")
    with pytest.raises(RedisConnectionError):
        service.tick()


def test_tick_marks_tailer_failing_on_oserror_and_clears_on_clean_poll(tmp_path):
    service, transcript, registry = _service_with_one_warm(tmp_path)
    service.tick()
    tailer = FakeTailer.instances[-1]
    tailer.poll_exc = PermissionError("no read")
    service.tick()
    state = next(iter(service._tailers.values()))
    assert state.failing is True
    tailer.poll_exc = None
    service.tick()
    assert state.failing is False


def test_tick_marks_failing_from_emit_failing_attr(tmp_path):
    service, transcript, registry = _service_with_one_warm(tmp_path)
    service.tick()
    FakeTailer.instances[-1].emit_failing = True
    service.tick()
    assert next(iter(service._tailers.values())).failing is True


def test_run_loop_reraises_redis_error_and_swallows_others():
    class Boom:
        def __init__(self, exc):
            self.exc = exc
            self.ticks = 0

        def tick(self):
            self.ticks += 1
            raise self.exc

    swallowed = Boom(ValueError("x"))
    run_loop(swallowed, interval=0, sleep_func=lambda _s: None, max_ticks=3)
    assert swallowed.ticks == 3

    fatal = Boom(RedisConnectionError("bus down"))
    with pytest.raises(RedisConnectionError):
        run_loop(fatal, interval=0, sleep_func=lambda _s: None, max_ticks=3)
    assert fatal.ticks == 1  # exited on the FIRST infra failure


def test_run_loop_marks_watchdog_every_tick():
    class Ticker:
        def tick(self):
            return 0

    class FakeWatchdog:
        def __init__(self):
            self.marks = 0

        def mark_tick(self):
            self.marks += 1

    wd = FakeWatchdog()
    run_loop(Ticker(), interval=0, sleep_func=lambda _s: None, max_ticks=4, watchdog=wd)
    assert wd.marks == 4


def test_run_loop_marks_watchdog_after_swallowed_tick_exception():
    class BadTicker:
        def tick(self):
            raise ValueError("bad tick")

    class FakeWatchdog:
        def __init__(self):
            self.marks = 0

        def mark_tick(self):
            self.marks += 1

    wd = FakeWatchdog()
    run_loop(BadTicker(), interval=0, sleep_func=lambda _s: None, max_ticks=2, watchdog=wd)
    assert wd.marks == 2


def _write_cold(tmp_path, name="agent1", lines=1):
    cold_dir = tmp_path / "cold"
    cold_dir.mkdir(exist_ok=True)
    out = cold_dir / f"{name}.output"
    out.write_text("x\n" * lines, encoding="utf-8")
    return cold_dir, out


def test_cold_sidecar_completed_does_not_finish_before_eof(tmp_path):
    # spec §A Finish paths (panel r3 convergent P0): a budget-limited poll +
    # completed:true must NOT finish+delete an undrained transcript.
    # ORDERING MATTERS (plan panel, grok P0): create the tailer and put it
    # mid-backlog BEFORE the sidecar flips to completed — FakeTailer defaults
    # at_eof=True, so a pre-completed sidecar would (correctly!) finish on
    # the very first tick and the test would fail against right behavior.
    cold_dir, out = _write_cold(tmp_path)
    (tmp_path / "empty.json").write_text("[]", encoding="utf-8")
    service = Service(redis=FakeRedis(), prefix="agent_scratch:", registry_path=str(tmp_path / "empty.json"), cold_dir=str(cold_dir), tailer_cls=FakeTailer)

    service.tick()  # creates tailer (no sidecar yet)
    tailer = FakeTailer.instances[-1]
    tailer.at_eof = False  # budget-limited chunk, backlog remains
    (cold_dir / "agent1.arb-tail.json").write_text(json.dumps({"completed": True}), encoding="utf-8")
    service.tick()
    assert out.exists()  # NOT deleted
    state = next(iter(service._tailers.values()))
    assert state.finished is False

    tailer.at_eof = True
    service.tick()
    assert not out.exists()  # drained -> finish + delete


def test_inband_completed_gates_on_eof(tmp_path):
    cold_dir, out = _write_cold(tmp_path)
    service = Service(redis=FakeRedis(), prefix="agent_scratch:", registry_path=str(tmp_path / "empty.json"), cold_dir=str(cold_dir), tailer_cls=FakeTailer)
    service.tick()
    tailer = FakeTailer.instances[-1]
    tailer.completed = True
    tailer.at_eof = False
    service.tick()
    assert next(iter(service._tailers.values())).finished is False
    tailer.at_eof = True
    service.tick()
    assert next(iter(service._tailers.values())).finished is True


def test_idle_finish_requires_eof_and_offset_progress_counts_as_activity(tmp_path):
    # spec §A Finish paths (panel r3 agy P1): a zero-event multi-chunk
    # catch-up must not idle-finish mid-file.
    cold_dir, out = _write_cold(tmp_path)
    clock = {"now": 0.0}
    service = Service(
        redis=FakeRedis(), prefix="agent_scratch:", registry_path=str(tmp_path / "empty.json"),
        cold_dir=str(cold_dir), tailer_cls=FakeTailer, idle_finish_secs=10.0,
        time_func=lambda: clock["now"],
    )
    service.tick()
    tailer = FakeTailer.instances[-1]

    tailer.at_eof = False
    tailer.progressed = True  # catching up through event-less lines
    clock["now"] = 100.0
    service.tick()
    assert next(iter(service._tailers.values())).finished is False  # progress = activity

    tailer.progressed = False
    tailer.at_eof = False
    clock["now"] = 200.0
    service.tick()
    assert next(iter(service._tailers.values())).finished is False  # idle but NOT at_eof

    tailer.at_eof = True
    clock["now"] = 300.0
    service.tick()
    assert next(iter(service._tailers.values())).finished is True  # at_eof + idle window


def test_tick_deadline_round_robin_no_starvation(tmp_path):
    # spec §A budgets (panel r3 codex P0 + grok P2): the tick returns at the
    # deadline and resumes with the NEXT tailer — every tailer progresses
    # across ticks.
    cold_dir, _ = _write_cold(tmp_path, "a")
    _write_cold(tmp_path, "b")
    _write_cold(tmp_path, "c")
    clock = {"now": 0.0}

    class SlowTailer(FakeTailer):
        def poll(self):
            clock["now"] += 10.0  # each poll consumes 10s
            return super().poll()

    service = Service(
        redis=FakeRedis(), prefix="agent_scratch:", registry_path=str(tmp_path / "empty.json"),
        cold_dir=str(cold_dir), tailer_cls=SlowTailer, tick_deadline_secs=15.0,
        time_func=lambda: clock["now"],
    )
    service.tick()  # tailers created; deadline cuts polling short
    service.tick()
    service.tick()
    counts = sorted(t.poll_count for t in SlowTailer.instances[-3:])
    assert counts[0] >= 1  # nobody starved after three ticks


def test_idle_finish_activity_sources(tmp_path):
    # task_continuing must NOT defeat idle-finish (spec §A, panel r4 cold-Opus P2)
    cold_dir, out = _write_cold(tmp_path)
    clock = {"now": 0.0}
    service = Service(
        redis=FakeRedis(), prefix="agent_scratch:", registry_path=str(tmp_path / "empty.json"),
        cold_dir=str(cold_dir), tailer_cls=FakeTailer, idle_finish_secs=10.0,
        time_func=lambda: clock["now"],
    )
    service.tick()
    tailer = FakeTailer.instances[-1]
    tailer.at_eof = True
    tailer.progressed = False
    # FakeTailer.poll returns 0 — a real tailer emitting ONLY task_continuing
    # also reports emitted 0 after this task's tailer change.
    clock["now"] = 50.0
    service.tick()
    assert next(iter(service._tailers.values())).finished is True


DRAIN_KEY = "agent_scratch:claude:draining:s1"


def _registry_with(tmp_path, transcript):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps([{"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}]), encoding="utf-8")
    return registry


def test_deregister_mid_backlog_writes_record_and_keeps_draining(tmp_path):
    # 5i(a): registry record removed mid-backlog -> draining record written,
    # tailer REMAINS polled, drains to EOF across ticks, finishes, record gone.
    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    registry = _registry_with(tmp_path, transcript)
    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), cold_dir=str(tmp_path / "tasks"), tailer_cls=FakeTailer)
    service.tick()
    tailer = FakeTailer.instances[-1]
    tailer.at_eof = False  # backlog remains

    registry.write_text("[]", encoding="utf-8")  # deregister
    service.tick()
    assert redis.get(DRAIN_KEY) is not None  # unconditional fallback write
    assert tailer.poll_count >= 2  # still polled

    service.tick()
    assert tailer.poll_count >= 3  # rediscovered via the record, still polled

    tailer.at_eof = True
    service.tick()
    assert redis.get(DRAIN_KEY) is None  # deleted at the at_eof finish
    assert not service._tailers  # drained and gone


def _heartbeats(redis):
    return [(k, v) for k, v in redis.values.items() if "tail:heartbeat:" in k]


def test_heartbeat_written_with_eight_fields_and_ttl(tmp_path):
    redis = FakeRedis()
    live = FakeRedis()
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    clock = {"now": 0.0}
    service = Service(
        redis=redis, live_redis=live, prefix="agent_scratch:",
        registry_path=str(registry), cold_dir=str(tmp_path / "tasks"), tailer_cls=FakeTailer,
        heartbeat_label="claude-tail.test", stale_after_s=330,
        time_func=lambda: clock["now"], wall_time_func=lambda: 1000.0,
    )
    service.tick()

    key = "agent_scratch:tail:heartbeat:claude-tail.test"
    payload = json.loads(live.values[key])
    assert set(payload) == {"ts", "pid", "started_at", "tailers", "failing_tailers", "skipped_lines", "last_emit_at", "stale_after_s"}
    assert payload["stale_after_s"] == 330
    assert payload["tailers"] == 0
    assert live.ttls[key] == 604800


def test_heartbeat_throttled_to_ten_seconds(tmp_path):
    redis, live = FakeRedis(), FakeRedis()
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    clock = {"now": 0.0}
    service = Service(
        redis=redis, live_redis=live, prefix="agent_scratch:", registry_path=str(registry),
        tailer_cls=FakeTailer, cold_dir=str(tmp_path / "tasks"), heartbeat_label="claude-tail.test",
        time_func=lambda: clock["now"], wall_time_func=lambda: 1000.0,
    )

    writes = []
    orig_set = live.set
    live.set = lambda k, v, ex=None: (writes.append(k), orig_set(k, v, ex=ex))

    service.tick()  # start+end within 0s -> exactly one write
    assert len([w for w in writes if "heartbeat" in w]) == 1
    clock["now"] = 5.0
    service.tick()  # still inside the 10s throttle
    assert len([w for w in writes if "heartbeat" in w]) == 1
    clock["now"] = 11.0
    service.tick()
    assert len([w for w in writes if "heartbeat" in w]) == 2


def test_heartbeat_counts_failing_and_skipped(tmp_path):
    redis, live = FakeRedis(), FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    registry = _registry_with(tmp_path, transcript)
    clock = {"now": 0.0}
    service = Service(
        redis=redis, live_redis=live, prefix="agent_scratch:", registry_path=str(registry),
        tailer_cls=FakeTailer, cold_dir=str(tmp_path / "tasks"), heartbeat_label="claude-tail.test",
        time_func=lambda: clock["now"], wall_time_func=lambda: 1000.0,
    )
    service.tick()
    tailer = FakeTailer.instances[-1]
    tailer.emit_failing = True
    tailer.skipped_lines = 7
    clock["now"] = 11.0
    service.tick()
    payload = json.loads(live.values["agent_scratch:tail:heartbeat:claude-tail.test"])
    assert payload["failing_tailers"] == 1
    assert payload["skipped_lines"] == 7
    assert payload["tailers"] == 1


def test_heartbeat_write_failure_raises(tmp_path):
    class DeadLive(FakeRedis):
        def set(self, key, value, ex=None):
            if "heartbeat" in key:
                raise RedisConnectionError("bus down")
            super().set(key, value, ex=ex)

    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    service = Service(
        redis=FakeRedis(), live_redis=DeadLive(), prefix="agent_scratch:",
        registry_path=str(registry), cold_dir=str(tmp_path / "tasks"), tailer_cls=FakeTailer, heartbeat_label="claude-tail.test",
    )
    with pytest.raises(RedisConnectionError):
        service.tick()


def test_restart_rediscovers_from_draining_record(tmp_path):
    # 5i(b): fresh service (restart) rediscovers via the record and resumes.
    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    record = {"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=604800)
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")

    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), cold_dir=str(tmp_path / "tasks"), tailer_cls=FakeTailer)
    service.tick()
    tailer = FakeTailer.instances[-1]
    assert tailer.path == str(transcript)

    tailer.at_eof = True
    service.tick()
    assert redis.get(DRAIN_KEY) is None
    assert any(t.finished for t in [tailer])


def test_reregister_during_drain_supersedes(tmp_path):
    # 5i(c): flap -> single tailer, draining record dropped, no finish.
    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    record = {"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=604800)
    registry = _registry_with(tmp_path, transcript)  # registry HAS the session

    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), cold_dir=str(tmp_path / "tasks"), tailer_cls=FakeTailer)
    service.tick()
    assert redis.get(DRAIN_KEY) is None  # registry supersedes; record dropped
    assert len([t for t in FakeTailer.instances if t.path == str(transcript)]) == 1
    assert not next(iter(service._tailers.values())).finished


def test_transcript_deleted_mid_drain_prunes_record_one_finish(tmp_path):
    # 5i(d): vanished transcript -> record pruned, exactly ONE finish, no
    # per-tick tailer re-creation churn (panel r5 convergent).
    class DrainingTailer(FakeTailer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.at_eof = False

    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    record = {"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=604800)
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), cold_dir=str(tmp_path / "tasks"), tailer_cls=DrainingTailer)
    service.tick()  # draining tailer exists
    FakeTailer.instances[-1].at_eof = False  # STILL DRAINING — without this
    # the at_eof finish deletes the record on tick 1 and the FileNotFound
    # path is never exercised (plan panel, cold-Opus P1-3: vacuous test whose
    # deny-proof could not go red).
    n_tailers = len(FakeTailer.instances)

    transcript.unlink()
    FakeTailer.instances[-1].poll_exc = FileNotFoundError()
    service.tick()
    assert redis.get(DRAIN_KEY) is None  # deleted on the FileNotFound finish

    service.tick()
    service.tick()
    assert len(FakeTailer.instances) == n_tailers  # no re-creation churn


def test_draining_stat_permission_error_keeps_record_and_retries(tmp_path, monkeypatch):
    class DrainingTailer(FakeTailer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.at_eof = False

    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    record = {"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=604800)
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    service = Service(
        redis=redis,
        prefix="agent_scratch:",
        registry_path=str(registry),
        cold_dir=str(tmp_path / "tasks"),
        tailer_cls=DrainingTailer,
    )
    real_stat = os.stat
    blocked = True

    def selective_stat(path, *args, **kwargs):
        if blocked and path == str(transcript):
            raise PermissionError("no stat permission")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", selective_stat)

    service.tick()

    assert redis.get(DRAIN_KEY) is not None
    assert FakeTailer.instances == []

    blocked = False
    service.tick()

    assert redis.get(DRAIN_KEY) is not None
    assert len(FakeTailer.instances) == 1


def test_restart_with_missing_transcript_deletes_record_silently(tmp_path):
    # plan panel, codex P1: the restart path (record exists, transcript gone,
    # NO in-memory tailer) must delete the record without creating a tailer,
    # without a finish (no lifecycle ever started in this process), without
    # churn or crash. Spec §A prune bullet, restart parenthetical.
    redis = FakeRedis()
    record = {"session_id": "s1", "transcript_path": str(tmp_path / "gone.jsonl"), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=604800)
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), cold_dir=str(tmp_path / "tasks"), tailer_cls=FakeTailer)

    service.tick()
    assert redis.get(DRAIN_KEY) is None  # pruned
    assert FakeTailer.instances == []  # no tailer ever created
    service.tick()  # no churn, no crash
    assert FakeTailer.instances == []


def test_draining_ttl_refreshed_each_tick(tmp_path):
    class DrainingTailer(FakeTailer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.at_eof = False

    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    record = {"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=10)  # stale TTL
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), cold_dir=str(tmp_path / "tasks"), tailer_cls=DrainingTailer)
    service.tick()
    service.tick()
    assert redis.ttls[DRAIN_KEY] == 604800  # refreshed while draining


def test_configure_logging_creates_dir_and_rotating_handler(tmp_path, monkeypatch):
    import logging

    from agent_redis_bridge.claude_tail.__main__ import configure_logging

    target = tmp_path / "deep" / "nested" / "tail.log"
    monkeypatch.setenv("ARB_CLAUDE_TAIL_LOG_FILE", str(target))
    root = logging.getLogger("agent_redis_bridge")
    before = list(root.handlers)
    before_level = root.level
    try:
        path = configure_logging("claude-tail.test")
        assert path == str(target)
        assert target.parent.is_dir()
        handler = [h for h in root.handlers if h not in before][-1]
        from logging.handlers import RotatingFileHandler

        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 3
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        root.setLevel(before_level)


def test_configure_logging_accepts_bare_filename(tmp_path, monkeypatch):
    import logging

    from agent_redis_bridge.claude_tail.__main__ import configure_logging

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARB_CLAUDE_TAIL_LOG_FILE", "tail.log")
    root = logging.getLogger("agent_redis_bridge")
    before = list(root.handlers)
    before_level = root.level
    try:
        path = configure_logging("claude-tail.test")
        assert path == "tail.log"
        assert (tmp_path / "tail.log").is_file()
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        root.setLevel(before_level)


def test_build_watchdog_env(monkeypatch):
    from agent_redis_bridge.claude_tail.__main__ import build_watchdog

    monkeypatch.setenv("ARB_CLAUDE_TAIL_WATCHDOG_SECS", "0")
    assert build_watchdog(1.0) is None

    monkeypatch.setenv("ARB_CLAUDE_TAIL_WATCHDOG_SECS", "300")
    wd = build_watchdog(1.0)
    assert wd is not None and wd.effective_threshold == 300.0


def test_build_service_from_env_wires_heartbeat_and_budgets(monkeypatch):
    from agent_redis_bridge.claude_tail.tailer import TranscriptTailer

    # Register the deployment singleton class attrs before the builder mutates
    # them, so monkeypatch restores the original values at teardown.
    monkeypatch.setattr(TranscriptTailer, "poll_budget_lines", TranscriptTailer.poll_budget_lines)
    monkeypatch.setattr(TranscriptTailer, "poll_budget_secs", TranscriptTailer.poll_budget_secs)
    monkeypatch.setenv("AGENT_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_HEARTBEAT_LABEL", "claude-tail.envtest")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_TICK_DEADLINE_SECS", "12")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_POLL_BUDGET_LINES", "42")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_POLL_BUDGET_SECS", "0.5")
    service = build_service_from_env(stale_after_s=390)
    assert service.heartbeat_label == "claude-tail.envtest"
    assert service.tick_deadline_secs == 12.0
    assert service.stale_after_s == 390
    assert TranscriptTailer.poll_budget_lines == 42
    assert TranscriptTailer.poll_budget_secs == 0.5

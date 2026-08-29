import json
from types import SimpleNamespace

import pytest

import agent_redis_bridge.claude_tail.finality as finality_module
from agent_redis_bridge.claude_tail.finality import FdInfo, FinalityManager, FinalityWatchStore
from agent_redis_bridge.claude_tail.identity import Identity
from agent_redis_bridge.claude_tail.offset import OffsetStore
from agent_redis_bridge.claude_tail.service import Service
from agent_redis_bridge.claude_tail.tailer import TranscriptTailer


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    def scan_iter(self, match=None):
        import fnmatch
        for key in list(self.values):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


class RecordingRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.xadds = []

    def xadd(self, stream, fields):
        self.xadds.append((stream, fields))
        return "1-0"


def _tailer(path, *, turn_index=1, start_offset=0, clean=True):
    return SimpleNamespace(
        path=str(path),
        identity=Identity("run-1", "task-1", "cold-opus", "orch"),
        _turn_open=True,
        _has_started=True,
        completed=False,
        _turn_clock_ok=clean,
        _turn_start_offset=start_offset,
        _turn_started_ts="2026-07-15T00:00:00+00:00",
        _last_causal_ts="2026-07-15T00:00:01+00:00",
        logical_turn_index=turn_index,
    )


def test_watch_store_uses_resolved_path_inode_and_turn_key():
    redis = FakeRedis()
    store = FinalityWatchStore(redis, "p:")
    record = {"target_path": "/tmp/transcript.jsonl", "target_inode": 17, "turn_index": 2}
    key = store.put(record)
    assert key == "p:claude:finality:watch:/tmp/transcript.jsonl|17:2"
    assert store.get(key) == record


def test_tailer_turn_start_offset_is_continuity_state_and_restores():
    tailer = TranscriptTailer(
        "/tmp/unused.jsonl", Identity("run", "task", "seat", "orch"), OffsetStore(FakeRedis(), "p:"),
        live_redis=FakeRedis(), trace_redis=FakeRedis(), prefix="p:", redactor=lambda value: value,
    )
    tailer._route_event = lambda event: None
    tailer._close_and_open_turn({"timestamp": "2026-07-15T00:00:00+00:00"}, 42)
    assert tailer._turn_start_offset == 42
    snapshot = tailer._snapshot_turn_state()
    tailer._turn_start_offset = None
    tailer._restore_turn_state(snapshot)
    assert tailer._turn_start_offset == 42


def test_nomination_places_hold_and_zero_write_fd_earns_after_confirmation(tmp_path):
    target = tmp_path / "target.jsonl"
    target.write_text(json.dumps({"type": "assistant", "timestamp": "2026-07-15T00:00:01+00:00"}) + "\n")
    sidecar = tmp_path / "seat.arb-tail.json"
    sidecar.write_text(json.dumps({"completed": True}))
    events = []
    manager = FinalityManager(
        FakeRedis(), "p:", fd_probe=lambda inode: [], emit=lambda name, record: events.append((name, record))
    )
    tailer = _tailer(target)
    assert manager.nominate("cold:seat", tailer, str(sidecar), now=1.0)
    assert manager.is_held("cold:seat")
    manager.tick(now=1.0)
    manager.tick(now=2.0)
    manager.tick(now=3.0)
    assert events and events[0][0] == "turn_finalized"
    assert manager.is_held("cold:seat")
    assert next(manager.store.scan())[1]["status"] == "watched"


def test_open_write_fd_and_probe_error_never_earn(tmp_path):
    target = tmp_path / "target.jsonl"
    target.write_text("{}\n")
    sidecar = tmp_path / "sidecar"
    sidecar.write_text(json.dumps({"completed": True}))
    responses = [[FdInfo(3, "w")], [FdInfo(3, "w")], [FdInfo(3, "w")], [FdInfo(3, "w")]]
    def probe(inode):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    manager = FinalityManager(FakeRedis(), "p:", fd_probe=probe)
    tailer = _tailer(target)
    manager.nominate("cold:seat", tailer, str(sidecar), now=1.0)
    manager.tick(now=1.0)
    manager.tick(now=2.0)
    manager.tick(now=3.0)
    manager.tick(now=4.0)
    assert not list(manager.store.scan())


def test_abandon_releases_hold_without_finalization(tmp_path):
    target = tmp_path / "target.jsonl"
    target.write_text("{}\n")
    sidecar = tmp_path / "sidecar"
    sidecar.write_text(json.dumps({"completed": True}))
    manager = FinalityManager(FakeRedis(), "p:", abandon_secs=3, fd_probe=lambda inode: [FdInfo(3, "w")])
    tailer = _tailer(target)
    manager.nominate("cold:seat", tailer, str(sidecar), now=1.0)
    manager.tick(now=1.0)
    manager.tick(now=5.0)
    assert not manager.is_held("cold:seat")
    assert not list(manager.store.scan())


def test_watch_is_written_before_finalized_emit_and_same_size_rewrite_retracts(tmp_path):
    target = tmp_path / "target.jsonl"
    output = tmp_path / "seat.output"
    sidecar = tmp_path / "seat.arb-tail.json"
    target.write_text(json.dumps({"type": "assistant", "timestamp": "2026-07-15T00:00:01+00:00"}) + "\n")
    output.symlink_to(target)
    sidecar.write_text(json.dumps({"completed": True}))
    redis = FakeRedis()
    events = []
    manager = FinalityManager(redis, "p:", fd_probe=lambda inode: [], emit=lambda name, record: events.append((name, record)))
    assert manager.nominate("cold:seat", _tailer(output), str(sidecar), now=1.0)
    manager.tick(now=1.0)
    manager.tick(now=2.0)
    manager.tick(now=3.0)
    assert events[0][0] == "turn_finalized"
    assert redis.values  # durable watch exists before the callback returns
    original = target.read_text()
    target.write_text(original.replace("assistant", "assistant", 1).replace("00:00:01", "00:00:02"))
    restarted = FinalityManager(redis, "p:", fd_probe=lambda inode: [], emit=lambda name, record: events.append((name, record)))
    restarted.rearm()
    assert events[-1][0] == "turn_finality_retracted"
    assert not output.exists()
    assert not sidecar.exists()


def test_rearm_retracts_assistant_append_after_restart(tmp_path):
    target = tmp_path / "target.jsonl"
    output = tmp_path / "seat.output"
    sidecar = tmp_path / "seat.arb-tail.json"
    target.write_text(json.dumps({"type": "assistant"}) + "\n")
    output.symlink_to(target)
    sidecar.write_text("{}")
    redis = FakeRedis()
    events = []
    record = _watch_record(target, output, sidecar, status="watched")
    FinalityWatchStore(redis, "p:").put(record)
    with target.open("a") as handle:
        handle.write(json.dumps({"type": "assistant", "timestamp": "2026-07-15T00:00:02+00:00"}) + "\n")
    restarted = FinalityManager(redis, "p:", emit=lambda name, item: events.append(name))
    restarted.rearm()
    assert events == ["turn_finality_retracted"]


def test_linux_missing_lsof_and_proc_probe_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(finality_module.sys, "platform", "linux")
    monkeypatch.setattr(
        finality_module.subprocess, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("lsof")),
    )
    monkeypatch.setattr(
        finality_module, "_proc_fd_probe",
        lambda inode: (_ for _ in ()).throw(RuntimeError("/proc unavailable")),
    )
    with pytest.raises(RuntimeError, match="fd probe failed|/proc unavailable"):
        finality_module._default_fd_probe(123)


def test_lsof_permission_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        finality_module.subprocess,
        "run",
        lambda *args, **kwargs: finality_module.subprocess.CompletedProcess(
            args, 2, "", "lsof: permission denied\n"
        ),
    )
    with pytest.raises(RuntimeError, match="fd probe failed"):
        finality_module._default_fd_probe(123)


def test_lsof_warning_noise_is_trusted_empty(monkeypatch):
    monkeypatch.setattr(
        finality_module.subprocess,
        "run",
        lambda *args, **kwargs: finality_module.subprocess.CompletedProcess(
            args,
            1,
            "",
            "lsof: WARNING: can't stat() unrelated mount\n"
            "lsof: can't stat() another unrelated path\n",
        ),
    )
    assert finality_module._default_fd_probe(123) == []


def test_lsof_rc1_permission_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        finality_module.subprocess,
        "run",
        lambda *args, **kwargs: finality_module.subprocess.CompletedProcess(
            args, 1, "", "lsof: permission denied on target\n"
        ),
    )
    with pytest.raises(RuntimeError, match="fd probe failed"):
        finality_module._default_fd_probe(123)


def test_lsof_empty_result_is_trusted(monkeypatch):
    monkeypatch.setattr(
        finality_module.subprocess,
        "run",
        lambda *args, **kwargs: finality_module.subprocess.CompletedProcess(args, 1, "", ""),
    )
    assert finality_module._default_fd_probe(123) == []


def test_proc_fd_probe_unreadable_pid_is_fail_closed(monkeypatch):
    def listdir(path):
        if path == "/proc":
            return ["123"]
        raise PermissionError("permission denied")

    monkeypatch.setattr(finality_module.os, "listdir", listdir)
    with pytest.raises(RuntimeError, match="fd probe failed"):
        finality_module._proc_fd_probe(123)


def test_watch_write_failure_never_emits_and_keeps_hold(tmp_path):
    class FailingRedis(FakeRedis):
        def set(self, key, value, ex=None):
            raise RuntimeError("watch store unavailable")

    target = tmp_path / "target.jsonl"
    target.write_text("{}\n")
    sidecar = tmp_path / "sidecar"
    sidecar.write_text(json.dumps({"completed": True}))
    manager = FinalityManager(FailingRedis(), "p:", fd_probe=lambda inode: [], emit=lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert manager.nominate("cold:seat", _tailer(target), str(sidecar), now=1.0)
    manager.tick(now=1.0)
    manager.tick(now=2.0)
    manager.tick(now=3.0)
    assert manager.is_held("cold:seat")


def test_service_finality_emit_routes_explicit_turn_index_and_epoch():
    eval_redis = RecordingRedis()
    service = Service(redis=FakeRedis(), prefix="p:", eval_redis=eval_redis, eval_stream="p:eval:events")
    service._emit_finality("turn_finalized", {
        "run_id": "run-1", "task_id": "task-1", "seat_id": "cold-opus", "orchestrator": "orch",
        "turn_index": 7, "event_ts": "2026-07-15T00:00:01+00:00",
        "turn_started_ts": "2026-07-15T00:00:00+00:00", "target_inode": 4, "observed_size": 9,
    })
    stream, fields = eval_redis.xadds[0]
    assert stream == "p:eval:events"
    assert fields["event_type"] == "turn_finalized"
    payload = json.loads(fields["payload"])
    assert payload["turn_index"] == 7
    assert payload["attempt_epoch"] == 1
    assert payload["finality_evidence"] == "fd_quiescence"


def _watch_record(target, output, sidecar, *, status="watched", service_key="cold:seat"):
    data = target.read_bytes()
    import hashlib
    return {
        "run_id": "run-1", "task_id": "task-1", "seat_id": "cold-opus", "orchestrator": "orch",
        "turn_index": 1, "target_path": str(target), "target_inode": target.stat().st_ino,
        "turn_start_offset": 0, "observed_size": len(data), "digest": hashlib.sha256(data).hexdigest(),
        "event_ts": "2026-07-15T00:00:01+00:00", "turn_started_ts": "2026-07-15T00:00:00+00:00",
        "status": status, "finalized_at": 1.0, "horizon_end": 9999.0,
        "service_key": service_key, "output_path": str(output), "sidecar_path": str(sidecar),
    }


def test_nine_cell_rearm_matrix(tmp_path):
    valid = json.dumps({"type": "assistant"}) + "\n"
    for status in ("watched", "closing", "retracted"):
        for presence, matches in (("present", True), ("present", False), ("missing", False)):
            target = tmp_path / f"{status}-{presence}-{matches}.jsonl"
            output = tmp_path / f"{status}-{presence}-{matches}.output"
            sidecar = tmp_path / f"{status}-{presence}-{matches}.arb-tail.json"
            target.write_text(valid)
            output.symlink_to(target)
            sidecar.write_text("{}")
            redis = FakeRedis()
            events = []
            manager = FinalityManager(redis, "p:", emit=lambda name, record: events.append(name))
            record = _watch_record(target, output, sidecar, status=status)
            key = manager.store.put(record, ex=604800 if status == "retracted" else None)
            if not matches:
                if presence == "missing":
                    target.unlink()
                else:
                    target.write_text(json.dumps({"type": "assistant", "changed": True}) + "\n")
            manager.rearm()
            if status == "watched" and matches:
                assert manager.store.get(key)["status"] == "watched"
                assert events == ["turn_finalized"]
            elif status == "watched":
                assert manager.store.get(key)["status"] == "retracted"
                assert events == ["turn_finality_retracted"]
            elif status == "closing":
                assert manager.store.get(key) is None
                assert events == []
                assert not output.exists() and not sidecar.exists()
            else:
                assert manager.store.get(key)["status"] == "retracted"
                assert events == []


def test_startup_renomination_scans_byte_zero_without_emitting_capture_edges(tmp_path):
    target = tmp_path / "target.jsonl"
    output = tmp_path / "seat.output"
    content = (
        json.dumps({"type": "user", "promptId": "p", "timestamp": "2026-07-15T00:00:00+00:00"}) + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-07-15T00:00:01+00:00"}) + "\n"
    )
    target.write_text(content)
    output.symlink_to(target)
    sidecar = tmp_path / "sidecar"
    manager = FinalityManager(FakeRedis(), "p:", fd_probe=lambda inode: [])
    tailer = _tailer(output)
    tailer._turn_open = False
    sidecar.write_text(json.dumps({"completed": True}))
    assert manager.startup_renominate("cold:seat", tailer, str(sidecar)) is True
    assert manager.is_held("cold:seat")
    assert tailer._turn_start_offset == 0


def test_horizon_transient_fd_failure_defers_close(tmp_path):
    target = tmp_path / "target.jsonl"
    output = tmp_path / "seat.output"
    sidecar = tmp_path / "seat.arb-tail.json"
    target.write_text("{}\n")
    output.symlink_to(target)
    sidecar.write_text("{}")
    manager = FinalityManager(
        FakeRedis(), "p:", horizon_secs=1,
        fd_probe=lambda inode: (_ for _ in ()).throw(RuntimeError("lsof timeout")),
    )
    # Seed a valid watched record and let the horizon check run.
    key = manager.store.put(_watch_record(target, output, sidecar, status="watched"))
    manager.tick(now=10000.0)
    assert manager.store.get(key)["status"] == "watched"


def test_watch_revalidates_in_range_mutation_before_horizon(tmp_path):
    target = tmp_path / "target.jsonl"
    output = tmp_path / "seat.output"
    sidecar = tmp_path / "seat.arb-tail.json"
    target.write_text(json.dumps({"type": "assistant"}) + "\n")
    output.symlink_to(target)
    sidecar.write_text("{}")
    events = []
    manager = FinalityManager(
        FakeRedis(), "p:", horizon_secs=100, fd_probe=lambda inode: [],
        emit=lambda name, record: events.append(name), wall_time_func=lambda: 10.0,
    )
    key = manager.store.put(_watch_record(target, output, sidecar, status="watched"))
    target.write_text(json.dumps({"type": "assistant", "changed": True}) + "\n")
    manager.tick(now=2.0, wall_now=10.0)
    assert manager.store.get(key)["status"] == "retracted"
    assert events == ["turn_finality_retracted"]


def test_missing_target_retracts_before_horizon(tmp_path):
    target = tmp_path / "target.jsonl"
    output = tmp_path / "seat.output"
    sidecar = tmp_path / "seat.arb-tail.json"
    target.write_text("{}\n")
    output.symlink_to(target)
    sidecar.write_text("{}")
    events = []
    manager = FinalityManager(FakeRedis(), "p:", emit=lambda name, record: events.append(name))
    key = manager.store.put(_watch_record(target, output, sidecar, status="watched"))
    target.unlink()
    manager.tick(now=2.0, wall_now=2.0)
    assert manager.store.get(key)["status"] == "retracted"
    assert events == ["turn_finality_retracted"]


def test_appended_later_human_turn_does_not_retract_at_horizon(tmp_path):
    target = tmp_path / "target.jsonl"
    output = tmp_path / "seat.output"
    sidecar = tmp_path / "seat.arb-tail.json"
    target.write_text(json.dumps({"type": "assistant"}) + "\n")
    output.symlink_to(target)
    sidecar.write_text("{}")
    events = []
    manager = FinalityManager(FakeRedis(), "p:", fd_probe=lambda inode: [], emit=lambda name, record: events.append(name))
    key = manager.store.put(_watch_record(target, output, sidecar, status="watched"))
    with target.open("a") as handle:
        handle.write(json.dumps({"type": "user", "promptId": "later"}) + "\n")
        handle.write(json.dumps({"type": "assistant"}) + "\n")
    manager.tick(now=2.0, wall_now=10000.0)
    assert manager.store.get(key) is None
    assert events == []


def test_malformed_append_defers_instead_of_retracting(tmp_path):
    target = tmp_path / "target.jsonl"
    output = tmp_path / "seat.output"
    sidecar = tmp_path / "seat.arb-tail.json"
    target.write_text(json.dumps({"type": "assistant"}) + "\n")
    output.symlink_to(target)
    sidecar.write_text("{}")
    manager = FinalityManager(FakeRedis(), "p:", emit=lambda *_: None)
    key = manager.store.put(_watch_record(target, output, sidecar, status="watched"))
    with target.open("ab") as handle:
        handle.write(b"not-json\n")

    manager.tick(now=2.0, wall_now=2.0)

    assert manager.store.get(key)["status"] == "watched"


def test_wall_clock_horizon_survives_manager_restart(tmp_path):
    target = tmp_path / "target.jsonl"
    output = tmp_path / "seat.output"
    sidecar = tmp_path / "seat.arb-tail.json"
    target.write_text(json.dumps({"type": "assistant"}) + "\n")
    output.symlink_to(target)
    sidecar.write_text(json.dumps({"completed": True}))
    redis = FakeRedis()
    emitted = []
    first = FinalityManager(
        redis, "p:", fd_probe=lambda inode: [], horizon_secs=10,
        wall_time_func=lambda: 100.0, emit=lambda name, record: emitted.append(name),
    )
    assert first.nominate("cold:seat", _tailer(output), str(sidecar), now=1.0)
    first.tick(now=1.0, wall_now=100.0)
    first.tick(now=2.0, wall_now=100.0)
    first.tick(now=3.0, wall_now=100.0)
    record = next(first.store.scan())[1]
    assert record["finalized_at"] == 100.0
    assert record["horizon_end"] == 110.0

    restarted = FinalityManager(
        redis, "p:", fd_probe=lambda inode: [], wall_time_func=lambda: 111.0,
        emit=lambda name, record: emitted.append(name),
    )
    restarted.rearm()
    restarted.tick(now=4.0, wall_now=111.0)
    assert not list(restarted.store.scan())


def test_warm_deregister_draining_flap_restart_never_nominates(tmp_path):
    target = tmp_path / "target.jsonl"
    sidecar = tmp_path / "draining.arb-tail.json"
    target.write_text("{}\n")
    sidecar.write_text(json.dumps({"completed": True}))
    redis = FakeRedis()
    manager = FinalityManager(redis, "p:")
    tailer = _tailer(target)
    assert manager.nominate("warm:session", tailer, str(sidecar)) is False
    restarted = FinalityManager(redis, "p:")
    assert restarted.nominate("warm:session", tailer, str(sidecar)) is False
    assert not manager.held_keys and not restarted.held_keys

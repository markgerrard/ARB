import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_loader("arb_orch_panel", SourceFileLoader("arb_orch_panel", str(ROOT / "scripts" / "arb-orch-panel")))
PANEL = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = PANEL
SPEC.loader.exec_module(PANEL)


def _fake_dispatch(path: Path, delay: float = 0.25) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys,time\n"
        "print('task-id: ' + sys.argv[1], file=sys.stderr, flush=True)\n"
        "print(json.dumps({'ok': True, 'result': 'x' * 2000000}), flush=True)\n"
        f"time.sleep({delay})\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _install_publish_stub(bridge_root: Path) -> None:
    """Stub arb-memory-harness-publish under bridge_root/scripts (Slice 1d-iv)."""
    scripts = bridge_root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    publish = scripts / "arb-memory-harness-publish"
    publish.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'artefact_id': 'art-stub-1', 'version': 1}))\n"
    )
    publish.chmod(publish.stat().st_mode | stat.S_IXUSR)


def _dispatch_env() -> dict:
    env = os.environ.copy()
    # publish-then-quartet requires the credential present at publish time
    env.setdefault("ARB_MEMORY_REDIS_URL", "redis://test-stub-publish")
    return env


def test_dispatch_one_drains_both_pipes_and_persists_task_id_before_exit(tmp_path, monkeypatch):
    bridge_root = tmp_path / "bridge"
    _install_publish_stub(bridge_root)
    monkeypatch.setattr(PANEL, "BRIDGE_ROOT", bridge_root)
    dispatch = tmp_path / "dispatch.py"
    _fake_dispatch(dispatch)
    out = tmp_path / "seat.out"
    err = tmp_path / "seat.err"
    registry = PANEL.TaskIdRegistry(tmp_path / "task-ids.json")
    started = time.monotonic()
    meta = PANEL.dispatch_one(
        dispatch=dispatch,
        engine="fake",
        target="seat-a",
        run_id="run-a",
        timeout=5,
        repo=tmp_path,
        prompt="x",
        out_path=out,
        err_path=err,
        audit_panel=False,
        dry_run=False,
        env=_dispatch_env(),
        task_ids=registry,
        slug="seat-a",
    )
    assert meta["ok"] is True
    assert "x" * 1000 in out.read_text()
    assert json.loads((tmp_path / "task-ids.json").read_text())["seat-a"]
    assert time.monotonic() - started < 5


def test_task_id_registry_is_valid_under_concurrent_updates(tmp_path):
    registry = PANEL.TaskIdRegistry(tmp_path / "task-ids.json")
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: registry.update(f"seat-{i}", f"task-{i}"), range(8)))
    assert json.loads((tmp_path / "task-ids.json").read_text()) == {f"seat-{i}": f"task-{i}" for i in range(8)}


def test_two_running_dispatches_publish_ids_before_either_finishes(tmp_path, monkeypatch):
    bridge_root = tmp_path / "bridge"
    _install_publish_stub(bridge_root)
    monkeypatch.setattr(PANEL, "BRIDGE_ROOT", bridge_root)
    dispatch = tmp_path / "dispatch.py"
    _fake_dispatch(dispatch, delay=0.6)
    registry = PANEL.TaskIdRegistry(tmp_path / "task-ids.json")
    from threading import Thread

    threads = []
    for slug in ("seat-a", "seat-b"):
        thread = Thread(target=PANEL.dispatch_one, kwargs={
            "dispatch": dispatch, "engine": "fake", "target": slug, "run_id": "run",
            "timeout": 5, "repo": tmp_path, "prompt": "x", "out_path": tmp_path / f"{slug}.out",
            "err_path": tmp_path / f"{slug}.err", "audit_panel": False, "dry_run": False,
            "env": _dispatch_env(), "task_ids": registry, "slug": slug,
        })
        thread.start()
        threads.append(thread)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and len(registry.read()) < 2:
        time.sleep(0.01)
    assert set(registry.read()) == {"seat-a", "seat-b"}
    assert any(thread.is_alive() for thread in threads)
    for thread in threads:
        thread.join()


def test_cold_event_allowlist_excludes_transcript():
    assert PANEL.allowed_cold_event({"kind": "turn_heartbeat"})
    assert PANEL.allowed_cold_event({"kind": "command_output"})
    assert not PANEL.allowed_cold_event({"kind": "model_text", "text": "secret"})
    assert not PANEL.allowed_cold_event({"kind": "model_thinking", "text": "secret"})
    # production task:{id}:events uses type= from push_task_event
    assert PANEL.allowed_cold_event({"type": "turn_heartbeat", "task_id": "t1"})
    assert PANEL.allowed_cold_event({"type": "command_started", "task_id": "t1"})
    assert not PANEL.allowed_cold_event({"type": "model_text"})


@pytest.mark.parametrize("event", [
    "task_continuing",
    "agent_committed",
    "orchestrator_committed",
    "post_timeout_agent_committed",
    "post_timeout_committed",
    "steer_rejected",
    "cancel_rejected",
])
def test_cold_event_allowlist_includes_lifecycle_and_control_rejections(event):
    assert PANEL.allowed_cold_event({"type": event, "task_id": "t1"})


def test_venv_reexec_uses_one_shot_sentinel(tmp_path, monkeypatch):
    fake_python = tmp_path / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.touch()
    calls = []
    monkeypatch.setattr(PANEL, "_VENV_PY", fake_python)
    monkeypatch.setattr(PANEL, "_VENV_PREFIX", (tmp_path / ".venv").resolve())
    monkeypatch.setattr(PANEL.sys, "prefix", str(tmp_path / "other-prefix"))
    monkeypatch.delenv(PANEL._VENV_REEXEC_SENTINEL, raising=False)
    monkeypatch.setattr(PANEL.os, "execv", lambda executable, argv: calls.append((executable, argv)))

    PANEL._reexec_in_venv_once()
    PANEL._reexec_in_venv_once()

    assert len(calls) == 1
    assert PANEL._VENV_REEXEC_SENTINEL not in os.environ


def test_fallback_uses_only_known_task_ids():
    events = {"task-a": [{"kind": "task_started"}], "task-unknown": [{"kind": "secret"}]}
    got = list(PANEL.fallback_task_events(events, {"seat-a": "task-a"}))
    assert got == [("seat-a", {"kind": "task_started"})]


def test_events_tee_falls_back_to_known_task_stream_when_live_empty(tmp_path):
    class EmptyLiveClient:
        def xrevrange(self, stream, count=1):
            return []

        def xread(self, streams, count=100, block=0):
            stream, cursor = next(iter(streams.items()))
            if stream.endswith("events:live"):
                return []
            return [(stream, [("2-0", {"kind": "turn_heartbeat", "run_id": "run-1"})])]

        def ping(self):
            return True

    registry = PANEL.TaskIdRegistry(tmp_path / "task-ids.json")
    registry.update("seat-a", "task-a")
    stop = __import__("threading").Event()
    thread = PANEL.start_events_tee(
        out_dir=tmp_path,
        run_id="run-1",
        env={},
        stop=stop,
        task_ids=registry,
        client_factory=lambda _env: (EmptyLiveClient(), "agent_scratch:"),
    )
    deadline = time.monotonic() + 1
    event_file = tmp_path / "seat-a.events.jsonl"
    while time.monotonic() < deadline and not event_file.exists():
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=1)
    assert event_file.exists()
    assert json.loads(event_file.read_text().splitlines()[0])["kind"] == "turn_heartbeat"


def test_events_tee_url_bus_decodes_responses(tmp_path, monkeypatch):
    # F15: a URL-configured bus (the managed/production shape) must decode
    # responses — bytes-keyed xread fields fail allowed_cold_event's str-key
    # lookups and the run_id filter, so the tee writes zero events.
    calls: dict = {}

    class FakeClient:
        def ping(self):
            return True

        def xrevrange(self, stream, count=1):
            return []

        def xread(self, streams, count=100, block=0):
            return []

    class FakeRedis:
        @staticmethod
        def from_url(url, **kwargs):
            calls["url"] = url
            calls["kwargs"] = kwargs
            return FakeClient()

    fake_module = type(sys)("redis")
    fake_module.Redis = FakeRedis
    monkeypatch.setitem(sys.modules, "redis", fake_module)

    stop = __import__("threading").Event()
    stop.set()
    thread = PANEL.start_events_tee(
        out_dir=tmp_path,
        run_id="run-1",
        env={"ARB_LIVE_REDIS_URL": "redis://example.invalid:6379/12"},
        stop=stop,
    )
    thread.join(timeout=1)
    assert calls["url"] == "redis://example.invalid:6379/12"
    assert calls["kwargs"].get("decode_responses") is True


def test_certifying_panel_cannot_disable_audit_without_lock():
    with pytest.raises(SystemExit):
        PANEL.validate_panel_input_policy([{"targetId": "seat", "certifying": True}], no_audit_panel=True, panel_input_locked=False)
    PANEL.validate_panel_input_policy([{"targetId": "seat", "certifying": True}], no_audit_panel=False, panel_input_locked=False)


def test_events_gc_only_after_summary(tmp_path, monkeypatch):
    event_file = tmp_path / "seat.events.jsonl"
    event_file.write_text("{}\n")
    assert PANEL.gc_seat_events(tmp_path, summary_written=False) == []
    assert event_file.exists()
    assert PANEL.gc_seat_events(tmp_path, summary_written=True) == [event_file]
    assert not event_file.exists()
    event_file.write_text("{}\n")
    monkeypatch.setenv("ARB_KEEP_SEAT_EVENTS", "1")
    assert PANEL.gc_seat_events(tmp_path, summary_written=True) == []
    assert event_file.exists()


def test_panel_locked_control_predicate():
    assert PANEL.panel_input_lock_reason({"audit_vote_expected": True}) == "panel_task_input_locked"
    assert PANEL.panel_input_lock_reason({"panel_input_locked": True}) == "panel_task_input_locked"
    assert PANEL.panel_input_lock_reason({"certifying": True}) == "panel_task_input_locked"
    assert PANEL.panel_input_lock_reason({}) is None


PI_EXTENSIONS_WIZARD = Path(
    "/Volumes/<workspace>/repos/PiExtensions/.claude/worktrees/arb-subagents-impl/extensions/arb-orch-wizard.ts"
)


def _external_file_contains(path: Path, needle: str, timeout: float = 0.5) -> bool:
    try:
        return subprocess.run(
            ["grep", "-Fq", "--", needle, str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def test_external_path_reachability_is_bounded(monkeypatch):
    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", time_out)
    assert not _external_file_contains(PI_EXTENSIONS_WIZARD, "scripts/dispatch-dev", timeout=0.01)


@pytest.mark.skipif(
    not _external_file_contains(PI_EXTENSIONS_WIZARD, "scripts/dispatch-dev"),
    reason="PiExtensions arb-orch wizard is absent or its volume is unreachable",
)
def test_panel_and_standup_paths_use_dispatch_dev():
    panel_source = (ROOT / "scripts" / "arb-orch-panel").read_text()
    assert 'DEFAULT_DISPATCH = SCRIPT_DIR / "dispatch-dev"' in panel_source
    assert _external_file_contains(PI_EXTENSIONS_WIZARD, "scripts/dispatch-dev")


def test_dispatch_dev_cancel_requires_task_id_and_routes_ctl():
    source = (ROOT / "scripts" / "dispatch-dev").read_text()
    assert 'if [ "${1:-}" = "--cancel" ]' in source
    assert "cancel --task-id \"$CANCEL_TASK\"" in source
    assert "--target \"$CANCEL_TO\"" in source

"""ENG-1b pi-sdk rotation state tests."""

from __future__ import annotations

import io
import logging
import os
import queue
from pathlib import Path

import pytest

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.pi_sdk import PiSdkEngine


def _with_env(env, factory):
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        return factory()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.on_write = None

    def write(self, value: str) -> None:
        self.lines.append(value)
        if self.on_write is not None:
            self.on_write(value)

    def flush(self) -> None:
        pass


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.terminated = False
        self._wait_calls = 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int | None = None) -> int:
        self._wait_calls += 1
        return 0

    def kill(self) -> None:
        self.terminated = True

    def poll(self) -> int | None:
        return None


def make_engine(retire=None, cap=None):
    return _with_env(
        {"BRIDGE_PI_RETIRE_AFTER_TURN": retire, "BRIDGE_PI_MAX_PROCESS_TURNS": cap},
        lambda: PiSdkEngine(
            cwd="/tmp",
            model=None,
            host_script_path=str(Path(__file__).resolve()),
            popen_factory=lambda *args, **kwargs: FakeProcess(),
        ),
    )


# The construction pins and cap-zero pin are expected GREEN before the edit;
# the cap, continuation tripwire, and counter tests are expected RED.


def test_construction_retire_default_on():
    eng = make_engine(retire=None)
    assert eng.retire_after_turn is True


def test_construction_retire_opt_out():
    eng = make_engine(retire="0")
    assert eng.retire_after_turn is False


def test_cap_flips_retire_property():
    eng = make_engine(retire="0", cap="3")
    assert eng.retire_after_turn is False
    eng._process_turns = 3
    assert eng.retire_after_turn is True


def test_cap_zero_means_unlimited():
    eng = make_engine(retire="0", cap="0")
    eng._process_turns = 10_000
    assert eng.retire_after_turn is False


def test_supports_continuation_tripwire():
    assert PiSdkEngine.supports_continuation is False


def test_counters_initialized():
    eng = make_engine()
    assert eng._thread_turns == 0
    assert eng._process_turns == 0
    assert eng._interrupted is False
    assert eng._turn_affinity_requested is False
    assert eng._quarantine_after_turn is False


class RotationEngine(PiSdkEngine):
    def __init__(self) -> None:
        super().__init__(
            cwd="/tmp",
            model=None,
            host_script_path=str(Path(__file__).resolve()),
            popen_factory=lambda *args, **kwargs: FakeProcess(),
        )
        self.messages: queue.Queue[dict[str, object]] = queue.Queue()
        self.thread_counter = 0
        self.turn_counter = 0
        self.rotate_requests: list[dict[str, object]] = []
        self.sent_no_wait: list[tuple[str, dict[str, object]]] = []
        self.fail_rotate = False
        self.fail_turn_start = False
        self.stop_reason: str | None = "stop"
        self.terminal_error = False
        self.old_disposed = True

    def request(self, method: str, params: dict[str, object], *, timeout: int) -> dict[str, object]:
        if method == "initialize":
            return {}
        if method == "thread/start":
            self.thread_counter += 1
            return {"thread": {"id": f"thread-{self.thread_counter}"}}
        if method == "thread/rotate":
            self.rotate_requests.append(params)
            if self.fail_rotate:
                raise EngineError("scripted rotate failure")
            self.thread_counter += 1
            return {
                "thread": {"id": f"thread-{self.thread_counter}"},
                "oldDisposed": self.old_disposed,
            }
        if method == "turn/start":
            if self.fail_turn_start:
                raise EngineError("scripted turn/start failure")
            self.turn_counter += 1
            turn_id = f"turn-{self.turn_counter}"
            params = {
                "turnId": turn_id,
                "ok": True,
                "finalText": "done",
                "toolCalls": 0,
                "stopReason": self.stop_reason,
            }
            if self.terminal_error:
                params["error"] = "present"
            self.messages.put({"method": "turn/completed", "params": params})
            return {"turn": {"id": turn_id}}
        return {}

    def send_request_no_wait(self, method: str, params: dict[str, object]) -> int:
        self.sent_no_wait.append((method, params))
        return 1

    def _get_notification(self, timeout: float) -> dict[str, object] | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None


def make_rotation_engine(retire=None, cap=None) -> RotationEngine:
    return _with_env(
        {"BRIDGE_PI_RETIRE_AFTER_TURN": retire, "BRIDGE_PI_MAX_PROCESS_TURNS": cap},
        RotationEngine,
    )


def _run(eng: RotationEngine, on_event=None):
    return eng.run_turn_with_progress("hi", timeout=5, policy="trusted", on_event=on_event)


@pytest.mark.parametrize("stop_reason", ["stop", "toolUse"])
def test_clean_terminal_allowlist_reaffirms_health(stop_reason, caplog):
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.stop_reason = stop_reason
    with caplog.at_level(logging.WARNING, logger="agent_redis_bridge.engines.pi_sdk"):
        result = _run(eng)
    assert result.ok is True
    assert eng.healthy is True
    assert "non-clean terminal" not in caplog.text


@pytest.mark.parametrize("stop_reason", ["length", None, "unknown"])
def test_non_clean_terminal_quarantines(stop_reason, caplog):
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.stop_reason = stop_reason
    with caplog.at_level(logging.WARNING, logger="agent_redis_bridge.engines.pi_sdk"):
        _run(eng)
    assert eng.healthy is False
    assert "non-clean terminal" in caplog.text


def test_error_field_present_quarantines(caplog):
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.terminal_error = True
    with caplog.at_level(logging.WARNING, logger="agent_redis_bridge.engines.pi_sdk"):
        _run(eng)
    assert eng.healthy is False
    assert "non-clean terminal" in caplog.text


def test_interrupt_sets_latch():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.active_turn_id = "turn-x"
    eng.interrupt()
    assert eng._interrupted is True
    assert eng.sent_no_wait and eng.sent_no_wait[-1][0] == "turn/abort"


def test_interrupted_latch_quarantines_clean_terminal(caplog):
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"

    def on_event(name, _data):
        if name == "turn_started":
            eng.interrupt()

    with caplog.at_level(logging.WARNING, logger="agent_redis_bridge.engines.pi_sdk"):
        _run(eng, on_event=on_event)
    assert eng.healthy is False
    assert "non-clean terminal" in caplog.text


def test_rotation_fires_on_second_turn_when_not_retiring():
    eng = make_rotation_engine(retire="0")
    eng.thread_id = "thread-1"
    eng.thread_counter = 1
    _run(eng)
    assert eng.rotate_requests == []
    _run(eng)
    assert eng.rotate_requests == [{"threadId": "thread-1"}]
    assert eng.thread_id == "thread-2"
    assert eng._thread_turns == 1


def test_automatic_rotation_carries_effective_thinking_level():
    eng = make_rotation_engine(retire="0")
    eng.thinking_level = "medium"
    eng.effective_reasoning_effort = "medium"
    eng.thread_id = "thread-1"
    _run(eng)
    _run(eng)
    assert eng.rotate_requests == [{"threadId": "thread-1", "thinkingLevel": "medium"}]
    assert eng._thread_params["thinkingLevel"] == "medium"


def test_no_rotation_when_retiring():
    eng = make_rotation_engine()
    eng.thread_id = "thread-1"
    _run(eng)
    _run(eng)
    assert eng.rotate_requests == []


def test_no_rotation_on_fresh_thread():
    eng = make_rotation_engine(retire="0")
    eng.thread_id = "thread-1"
    _run(eng)
    assert eng.rotate_requests == []


def test_rotation_failure_quarantines_and_preserves_thread():
    eng = make_rotation_engine(retire="0")
    eng.thread_id = "thread-1"
    _run(eng)
    eng.fail_rotate = True
    with pytest.raises(EngineError, match="thread rotation failed"):
        _run(eng)
    assert eng.healthy is False
    assert eng.thread_id == "thread-1"


def test_counter_dirty_before_turn_start_send():
    eng = make_rotation_engine(retire="0")
    eng.thread_id = "thread-1"
    eng.fail_turn_start = True
    result = _run(eng)
    assert result.ok is False
    assert eng._thread_turns == 1
    assert eng._process_turns == 1


def test_affinity_skips_one_turn_then_is_consumed():
    eng = make_rotation_engine(retire="0")
    eng.thread_id = "thread-1"
    _run(eng)
    eng.set_turn_thread_affinity(True)
    _run(eng)
    assert eng.rotate_requests == []
    assert eng._turn_affinity_requested is False
    _run(eng)
    assert eng.rotate_requests == [{"threadId": "thread-1"}]


def test_later_affinity_false_overwrites_pending_true():
    eng = make_rotation_engine(retire="0")
    eng.thread_id = "thread-1"
    _run(eng)
    eng.set_turn_thread_affinity(True)
    eng.set_turn_thread_affinity(False)
    _run(eng)
    assert eng.rotate_requests == [{"threadId": "thread-1"}]


def test_sticky_dispose_quarantine_survives_clean_terminal():
    eng = make_rotation_engine(retire="0")
    eng.thread_id = "thread-1"
    _run(eng)
    eng.old_disposed = False
    _run(eng)
    assert eng.healthy is False
    assert eng._quarantine_after_turn is True


def test_start_resets_quarantine_and_thread_turns():
    eng = make_rotation_engine()
    eng._quarantine_after_turn = True
    eng._thread_turns = 3
    eng.start()
    assert eng._quarantine_after_turn is False
    assert eng._thread_turns == 0


def test_affinity_setter_stores_value():
    eng = make_rotation_engine()
    eng.set_turn_thread_affinity(True)
    assert eng._turn_affinity_requested is True
    eng.set_turn_thread_affinity(False)
    assert eng._turn_affinity_requested is False

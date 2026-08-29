from __future__ import annotations

import os
import queue
import time
from pathlib import Path
from unittest import mock

# The 50ms budget asserts a real latency property and holds on any developer
# machine. Shared CI runners can't promise wall-clock latency (observed 2-4s of
# scheduler noise on this exact loop), so there the budget only needs to
# distinguish "returned promptly" from the regression this test guards against —
# a wedged consumer BLOCKING the hotpath, which hangs indefinitely, not seconds.
HOTPATH_BUDGET_S = 5.0 if os.environ.get("GITHUB_ACTIONS") else 0.05

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.engines.base import TurnResult

from test_bridge_handle_raw import FakeRedis, make_bridge, request_json


class RaisingRedis:
    def __getattr__(self, name: str):
        raise AssertionError(f"redis touched during _capture: {name}")


class CapturingEngine:
    supports_continuation = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        return TurnResult(ok=True, result="done")


def _request():
    return type(
        "Req",
        (),
        {
            "id": "task-1",
            "run_id": "run-1",
            "sender": "orchestrator-1",
            "payload": {},
        },
    )()


def _stop_background_transcript(bridge):
    if getattr(bridge, "_transcript_flusher", None) is not None:
        bridge._transcript_flusher.stop()
    if getattr(bridge, "_transcript_thread", None) is not None:
        bridge._transcript_thread.join(timeout=1)
    bridge._transcript_enabled = True
    bridge._transcript_q = queue.Queue()


def test_handle_progress_enqueues_model_text() -> None:
    bridge = make_bridge("--dry-run")
    _stop_background_transcript(bridge)
    bridge.redis = FakeRedis()  # type: ignore[assignment]
    req = _request()

    bridge.handle_progress(
        req,
        "model_text",
        {"delta": "hi", "turn_id": "turn-1", "item_id": "turn-1:text", "kind": "model_text", "seq": 1},
        policy="trusted",
    )

    item = bridge._transcript_q.get_nowait()
    assert item["task_id"] == "task-1"
    assert item["run_id"] == "run-1"
    assert item["seat_id"] == bridge.agent_id
    assert item["orchestrator"] == "orchestrator-1"
    assert item["event"] == "model_text"
    assert item["data"]["delta"] == "hi"
    assert item["turn_id"] == "turn-1"
    assert item["item_id"] == "turn-1:text"
    assert item["kind"] == "model_text"
    assert item["seq"] == 1


def test_hotpath_does_not_block_on_wedged_consumer() -> None:
    bridge = make_bridge("--dry-run")
    _stop_background_transcript(bridge)
    bridge.redis = FakeRedis()  # type: ignore[assignment]
    req = _request()

    for event in ("model_text", "model_thinking", "command_output"):
        bridge._transcript_q = queue.Queue(maxsize=1)
        bridge._transcript_q.put_nowait({"filled": True})
        bridge._transcript_truncated = 0
        bridge._last_stream_heartbeat[req.id] = time.monotonic()

        started = time.monotonic()
        bridge.handle_progress(
            req,
            event,
            {"delta": "x", "turn_id": "turn-1", "item_id": f"turn-1:{event}", "kind": event, "seq": 1},
            policy="trusted",
        )

        assert time.monotonic() - started < HOTPATH_BUDGET_S
        assert bridge._transcript_truncated == 1


def test_capture_is_io_free() -> None:
    bridge = make_bridge("--dry-run")
    _stop_background_transcript(bridge)
    bridge.redis = RaisingRedis()  # type: ignore[assignment]
    bridge.eval_redis = RaisingRedis()
    bridge.audit_redis = RaisingRedis()

    bridge._capture(
        _request(),
        "model_text",
        {"delta": "hi", "turn_id": "turn-1", "item_id": "turn-1:text", "kind": "model_text", "seq": 3},
    )

    item = bridge._transcript_q.get_nowait()
    assert item["data"]["delta"] == "hi"
    assert item["turn_id"] == "turn-1"
    assert item["item_id"] == "turn-1:text"
    assert item["kind"] == "model_text"
    assert item["seq"] == 3


def test_capture_defaults_schemaless_event() -> None:
    bridge = make_bridge("--dry-run")
    _stop_background_transcript(bridge)

    bridge._capture(_request(), "model_text", {"delta": "x"})

    item = bridge._transcript_q.get_nowait()
    assert item["task_id"] == "task-1"
    assert item["event"] == "model_text"
    assert item["turn_id"] == "task-1"
    assert item["item_id"] == "task-1:model_text"
    assert item["kind"] == "model_text"
    assert isinstance(item["seq"], int)
    assert item["data"]["turn_id"] == "task-1"
    assert item["data"]["item_id"] == "task-1:model_text"
    assert item["data"]["kind"] == "model_text"
    assert isinstance(item["data"]["seq"], int)


def test_capture_disabled_enqueues_nothing() -> None:
    bridge = make_bridge("--dry-run")
    _stop_background_transcript(bridge)
    bridge._transcript_enabled = False

    bridge._capture(_request(), "model_text", {"delta": "x"})

    assert bridge._transcript_q.empty()


def test_process_request_enqueues_turn_end_marker() -> None:
    bridge = make_bridge("--no-enforce-completion")
    _stop_background_transcript(bridge)
    bridge.redis = FakeRedis()  # type: ignore[assignment]

    with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=CapturingEngine()):
        bridge.handle_raw(request_json("req-turn-end", payload={"task": "finish"}))
        bridge.join_active_thread()

    events = []
    while True:
        try:
            events.append(bridge._transcript_q.get_nowait())
        except queue.Empty:
            break

    turn_end = next(item for item in events if item["event"] == "turn_end")
    assert turn_end["task_id"] == "req-turn-end"
    assert turn_end["turn_id"] == "req-turn-end"
    assert turn_end["item_id"] == "req-turn-end:turn_end"

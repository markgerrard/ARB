"""Unit tests for the pi_sdk engine.

These exercise the engine's JSON-RPC plumbing + event mapping against a
fake subprocess. They do NOT spawn a real Node harness — that's covered
by the protocol smoke at tools/pi-sdk-host/smoke_protocol.py.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_redis_bridge.engines.base import EngineError, TurnResult
from agent_redis_bridge.engines.pi_sdk import PiSdkEngine


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


class InitTimeoutEnvTests(unittest.TestCase):
    def test_default_init_timeout_is_60(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            eng = PiSdkEngine(cwd="/tmp/p", model=None)

        self.assertEqual(eng._init_timeout, 60)

    def test_env_overrides_init_timeout(self) -> None:
        with mock.patch.dict(os.environ, {"BRIDGE_ENGINE_INIT_TIMEOUT_S": "33"}, clear=True):
            eng = PiSdkEngine(cwd="/tmp/p", model=None)

        self.assertEqual(eng._init_timeout, 33)

    def test_initialize_uses_init_timeout(self) -> None:
        captured: list[tuple[str, int]] = []

        class CaptureStartEngine(PiSdkEngine):
            def request(self, method: str, params: dict, *, timeout: int) -> dict:
                captured.append((method, timeout))
                if method == "thread/start":
                    return {"thread": {"id": "t"}}
                return {}

            def _read_stdout(self) -> None:
                pass

        fake = FakeProcess()
        with mock.patch.dict(os.environ, {"BRIDGE_ENGINE_INIT_TIMEOUT_S": "7"}, clear=True):
            eng = CaptureStartEngine(
                cwd="/tmp/p",
                model=None,
                popen_factory=lambda *args, **kwargs: fake,
            )
            with mock.patch("agent_redis_bridge.engines.pi_sdk.start_stderr_drain", return_value=None):
                eng.start()

        self.assertIn(("initialize", 7), captured)

    def test_explicit_probe_timeout_still_wins(self) -> None:
        fake = FakeProcess()
        with mock.patch.dict(os.environ, {}, clear=True):
            eng = PiSdkEngine(
                cwd="/tmp/p",
                model=None,
                popen_factory=lambda *args, **kwargs: fake,
            )
            with mock.patch("agent_redis_bridge.engines.pi_sdk.start_stderr_drain", return_value=None):
                with self.assertRaisesRegex(EngineError, "initialize timed out after 0s"):
                    eng.start(probe_timeout=0)

    def test_scored_provider_environment_is_explicitly_bound_to_host_child(self) -> None:
        fake = FakeProcess()
        captured: dict = {}

        def spawn(*args, **kwargs):
            captured.update(kwargs)
            return fake

        provider_env = {"PATH": "/usr/bin:/bin", "DUMMY_API_KEY": "secret"}
        eng = PiSdkEngine(cwd="/tmp/p", model=None, popen_factory=spawn, process_env=provider_env)
        with mock.patch("agent_redis_bridge.engines.pi_sdk.start_stderr_drain", return_value=None):
            with mock.patch.object(eng, "request", side_effect=[{}, {"thread": {"id": "t"}}]):
                eng.start()
        self.assertEqual(captured["env"], provider_env)


class PiSdkBridgeWiringTest(unittest.TestCase):
    def test_build_engine_passes_explicit_role_profile(self) -> None:
        from agent_redis_bridge.bridge import build_engine

        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "reviewer.md"
            profile.write_text("Review strictly.", encoding="utf-8")
            args = argparse.Namespace(
                engine="pi-sdk",
                model="minimax/MiniMax-M3",
                pi_tools=None,
                role_profile_file=str(profile),
            )

            engine = build_engine(args, cwd="/tmp/p")

        self.assertIsInstance(engine, PiSdkEngine)
        self.assertEqual(engine.append_system_prompt, "Review strictly.")


class RetireAfterTurnConfigTest(unittest.TestCase):
    def test_retire_after_turn_defaults_on(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            engine = PiSdkEngine(cwd="/tmp/p", model=None, host_script_path="/dev/null")

        self.assertTrue(engine.retire_after_turn)

    def test_retire_after_turn_zero_disables(self) -> None:
        with mock.patch.dict(os.environ, {"BRIDGE_PI_RETIRE_AFTER_TURN": "0"}, clear=True):
            engine = PiSdkEngine(cwd="/tmp/p", model=None, host_script_path="/dev/null")

        self.assertFalse(engine.retire_after_turn)

    def test_retire_after_turn_false_disables_case_insensitive(self) -> None:
        with mock.patch.dict(os.environ, {"BRIDGE_PI_RETIRE_AFTER_TURN": "False"}, clear=True):
            engine = PiSdkEngine(cwd="/tmp/p", model=None, host_script_path="/dev/null")

        self.assertFalse(engine.retire_after_turn)


class _LiveQueueStdout:
    """Stdout shim that yields whatever the test queues, blocking otherwise.

    Lets a test push response/notification lines after the engine has
    started its reader thread, so the engine sees them just like a real
    Node subprocess writing line-by-line.
    """

    def __init__(self) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        while True:
            value = self._lines.get()
            if value is None:
                raise StopIteration
            return value

    def push(self, obj: dict) -> None:
        self._lines.put(json.dumps(obj) + "\n")

    def close(self) -> None:
        self._closed = True
        self._lines.put(None)


def _make_engine() -> tuple[PiSdkEngine, FakeProcess, _LiveQueueStdout]:
    fake = FakeProcess()
    live = _LiveQueueStdout()
    fake.stdout = live  # type: ignore[assignment]
    engine = PiSdkEngine(
        cwd="/tmp/p",
        model="openrouter/qwen/qwen3-coder-next",
        host_script_path="/dev/null",  # bypass file-existence check via patching
        popen_factory=lambda *args, **kwargs: fake,
    )
    engine.process = fake  # type: ignore[assignment]
    engine.reader_thread = threading.Thread(target=engine._read_stdout, daemon=True)
    engine.reader_thread.start()
    return engine, fake, live


def _autoreply(fake: FakeProcess, live: _LiveQueueStdout) -> threading.Thread:
    """Watch fake stdin and push a result back for each request id."""

    def _reply_loop() -> None:
        seen = 0
        while True:
            time.sleep(0.01)
            if len(fake.stdin.lines) <= seen:
                continue
            for line in fake.stdin.lines[seen:]:
                seen += 1
                msg = json.loads(line)
                method = msg.get("method")
                if method == "initialize":
                    live.push({"id": msg["id"], "result": {"serverInfo": {"name": "pi-sdk-host", "version": "0.0.0"}, "capabilities": {}}})
                elif method == "thread/start":
                    live.push({"id": msg["id"], "result": {"thread": {"id": "th_test"}}})
                elif method == "turn/start":
                    live.push({"id": msg["id"], "result": {"turn": {"id": "tn_test"}}})
                # turn/abort + shutdown are handled by test-specific drivers.

    t = threading.Thread(target=_reply_loop, daemon=True)
    t.start()
    return t


class StartupTest(unittest.TestCase):
    def test_start_sends_initialize_then_thread_start_and_stores_thread_id(self) -> None:
        engine, fake, live = _make_engine()
        _autoreply(fake, live)
        # Skip start()'s spawn path — manually drive its protocol exchanges.
        engine.request(
            "initialize",
            {"clientInfo": {"name": "t", "version": "0"}, "capabilities": {}},
            timeout=2,
        )
        response = engine.request("thread/start", {"cwd": "/tmp"}, timeout=2)
        engine.thread_id = response["thread"]["id"]
        self.assertEqual(engine.thread_id, "th_test")
        # Initialize and thread/start should both have been written.
        methods = [json.loads(line)["method"] for line in fake.stdin.lines]
        self.assertEqual(methods, ["initialize", "thread/start"])
        live.close()


class RunTurnTest(unittest.TestCase):
    def test_run_turn_streams_progress_and_returns_final_text(self) -> None:
        engine, fake, live = _make_engine()
        _autoreply(fake, live)
        engine.thread_id = "th_test"
        engine.pi_tools = "read"  # any value: marks non-full-tools (avoids policy guard)

        events: list[tuple[str, dict]] = []

        def on_event(name: str, payload: dict) -> None:
            events.append((name, payload))

        def drive_turn() -> None:
            # Push notifications after turn/start has been ack'd.
            # Give the reader thread a beat to pop the ack response.
            time.sleep(0.05)
            live.push({"method": "turn/started", "params": {"turnId": "tn_test"}})
            live.push({"method": "turn/textDelta", "params": {"turnId": "tn_test", "delta": "Hello "}})
            live.push(
                {
                    "method": "turn/toolStarted",
                    "params": {
                        "turnId": "tn_test",
                        "toolCallId": "c1",
                        "toolName": "read",
                        "args": {"path": "src/arb_memory/visibility.py"},
                    },
                }
            )
            live.push(
                {
                    "method": "turn/toolFinished",
                    "params": {
                        "turnId": "tn_test",
                        "toolCallId": "c1",
                        "toolName": "read",
                        "result": {"content": [{"type": "text", "text": "file contents"}]},
                        "isError": False,
                    },
                }
            )
            live.push({"method": "turn/textDelta", "params": {"turnId": "tn_test", "delta": "world"}})
            live.push({"method": "turn/completed", "params": {
                "turnId": "tn_test",
                "ok": True,
                "finalText": "Hello world",
                "toolCalls": 1,
                "stopReason": "stop",
            }})

        threading.Thread(target=drive_turn, daemon=True).start()

        result = engine.run_turn_with_progress(
            "say hi",
            timeout=5,
            policy="trusted",
            on_event=on_event,
        )

        self.assertEqual(
            result,
            TurnResult(ok=True, result="Hello world", stop_reason="stop", tool_calls=1),
        )
        # turn_started, model_text x2, command_started, command_finished, turn_completed
        event_names = [e[0] for e in events]
        self.assertEqual(event_names[0], "turn_started")
        self.assertIn("model_text", event_names)
        self.assertIn("command_started", event_names)
        self.assertIn("command_output", event_names)
        self.assertIn("command_finished", event_names)
        self.assertEqual(event_names[-1], "turn_completed")
        by_name = {name: payload for name, payload in events}
        self.assertEqual(by_name["command_started"]["kind"], "command_started")
        self.assertEqual(by_name["command_started"]["tool_name"], "read")
        self.assertEqual(by_name["command_started"]["command"], 'read {"path":"src/arb_memory/visibility.py"}')
        self.assertEqual(by_name["command_started"]["content"], by_name["command_started"]["command"])
        self.assertEqual(by_name["command_output"]["kind"], "command_output")
        self.assertEqual(by_name["command_output"]["tool_name"], "read")
        self.assertEqual(by_name["command_output"]["delta"], "file contents")
        self.assertEqual(by_name["command_output"]["item_id"], "c1:output")
        self.assertEqual(by_name["command_finished"]["kind"], "command_finished")
        self.assertEqual(by_name["command_finished"]["tool_name"], "read")
        live.close()

    def test_run_turn_returns_error_when_turn_completed_reports_not_ok(self) -> None:
        engine, fake, live = _make_engine()
        _autoreply(fake, live)
        engine.thread_id = "th_test"
        engine.pi_tools = "read"

        def drive_turn() -> None:
            time.sleep(0.05)
            live.push({"method": "turn/completed", "params": {
                "turnId": "tn_test",
                "ok": False,
                "finalText": "",
                "toolCalls": 0,
                "stopReason": "aborted",
                "error": "aborted",
            }})

        threading.Thread(target=drive_turn, daemon=True).start()
        result = engine.run_turn_with_progress("anything", timeout=5, policy="trusted", on_event=None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "aborted")
        live.close()

    def test_non_trusted_turn_refused_when_engine_has_full_tools(self) -> None:
        engine, fake, live = _make_engine()
        engine.thread_id = "th_test"
        # pi_tools is None by default → full tools → reject non-trusted
        result = engine.run_turn_with_progress("anything", timeout=5, policy="human", on_event=None)
        self.assertFalse(result.ok)
        self.assertIn("non-trusted", result.error or "")
        live.close()

    def test_degenerate_pi_tools_string_refused_at_construction(self) -> None:
        # Tri-model P0 (agy + opus): "," parses to empty list, must NOT
        # silently fall through to full tools while bypassing the policy
        # guard. Engine refuses to construct.
        with self.assertRaises(EngineError) as ctx:
            PiSdkEngine(cwd="/tmp/p", model=None, pi_tools=" , , ", host_script_path="/dev/null")
        self.assertIn("parses to empty tool list", str(ctx.exception))


class ParseTest(unittest.TestCase):
    def test_reader_routes_response_to_responses_and_notification_to_queue(self) -> None:
        fake = FakeProcess()
        fake.stdout = io.StringIO(
            json.dumps({"id": 1, "result": {"ok": True}}) + "\n"
            + json.dumps({"method": "turn/textDelta", "params": {"turnId": "x", "delta": "hi"}}) + "\n"
            + json.dumps({"method": "turn/completed", "params": {"turnId": "x", "ok": True}}) + "\n"
        )
        engine = PiSdkEngine(
            cwd="/tmp/p", model=None, host_script_path="/dev/null",
            popen_factory=lambda *args, **kwargs: fake,
        )
        engine.process = fake  # type: ignore
        engine._read_stdout()
        self.assertEqual(engine.responses.get(1), {"id": 1, "result": {"ok": True}})
        first = engine.notifications.get_nowait()
        second = engine.notifications.get_nowait()
        self.assertEqual(first["method"], "turn/textDelta")
        self.assertEqual(second["method"], "turn/completed")


if __name__ == "__main__":
    unittest.main()


class SteerHonestyTest(unittest.TestCase):
    def test_steer_raises_instead_of_faking_success(self) -> None:
        # Audit PSK-2 (panel-confirmed): steer() silently discarded the message
        # and returned a turn id, so the bridge emitted steer_sent for an
        # instruction that never reached the model.
        engine, fake, live = _make_engine()
        engine.thread_id = "th_test"
        engine.active_turn_id = "tn_test"

        with self.assertRaises(EngineError):
            engine.steer("change course")


class AckWatchdogTest(unittest.TestCase):
    def test_turn_with_no_output_fails_fast_and_marks_unhealthy(self) -> None:
        # Audit PSK-3 (panel-confirmed): pi_rpc aborts a wedged turn after
        # BRIDGE_PI_ACK_TIMEOUT; pi_sdk waited out the full turn timeout —
        # exactly the unauthenticated-provider kevent wedge shape.
        engine, fake, live = _make_engine()
        _autoreply(fake, live)  # acks turn/start; then only the unconditional turn/started
        engine.thread_id = "th_test"
        engine.pi_tools = "read"
        engine.ack_timeout = 0.4

        def push_turn_started() -> None:
            # host.mjs:506 notifies turn/started synchronously BEFORE
            # session.prompt() — i.e. before the wedge point. The watchdog
            # must not count it as output (cold-Opus panel P1, 2026-07-08:
            # this test originally omitted it and was vacuously green while
            # the real watchdog could never fire).
            time.sleep(0.05)
            live.push({"method": "turn/started", "params": {"turnId": "tn_test"}})

        threading.Thread(target=push_turn_started, daemon=True).start()

        started = time.monotonic()
        result = engine.run_turn_with_progress("wedge", timeout=15, policy="trusted", on_event=None)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5, "wedged turn must abort at the ack timeout, not the turn timeout")
        self.assertFalse(result.ok)
        self.assertIn("no output", result.error)
        self.assertFalse(engine.is_healthy())

    def test_first_notification_disarms_the_watchdog(self) -> None:
        engine, fake, live = _make_engine()
        _autoreply(fake, live)
        engine.thread_id = "th_test"
        engine.pi_tools = "read"
        engine.ack_timeout = 1.0

        def drive() -> None:
            time.sleep(0.05)
            live.push({"method": "turn/started", "params": {"turnId": "tn_test"}})
            time.sleep(0.05)
            live.push({"method": "turn/textDelta", "params": {"turnId": "tn_test", "delta": "hi"}})
            time.sleep(1.4)  # longer than ack_timeout: must NOT abort once output has started
            live.push({"method": "turn/completed", "params": {
                "turnId": "tn_test", "ok": True, "finalText": "hi", "toolCalls": 0, "stopReason": "stop",
            }})

        threading.Thread(target=drive, daemon=True).start()

        result = engine.run_turn_with_progress("slow", timeout=15, policy="trusted", on_event=None)

        self.assertTrue(result.ok, f"turn failed: {result.error}")
        self.assertEqual(result.result, "hi")

    def test_auto_retry_notification_disarms_watchdog_and_emits_progress(self) -> None:
        engine, fake, live = _make_engine()
        _autoreply(fake, live)
        engine.thread_id = "th_test"
        engine.pi_tools = "read"
        engine.ack_timeout = 0.4
        events: list[tuple[str, dict]] = []

        def drive() -> None:
            time.sleep(0.05)
            live.push({"method": "turn/started", "params": {"turnId": "tn_test"}})
            time.sleep(0.05)
            live.push({
                "method": "turn/autoRetry",
                "params": {"turnId": "tn_test", "phase": "start", "attempt": 1, "delayMs": 2000},
            })
            time.sleep(0.7)
            live.push({"method": "turn/completed", "params": {
                "turnId": "tn_test", "ok": True, "finalText": "done", "toolCalls": 0, "stopReason": "stop",
            }})

        threading.Thread(target=drive, daemon=True).start()

        result = engine.run_turn_with_progress("retry", timeout=5, policy="trusted", on_event=lambda name, payload: events.append((name, payload)))

        self.assertTrue(result.ok, f"turn failed: {result.error}")
        self.assertEqual(result.result, "done")
        retry_events = [(name, payload) for name, payload in events if name == "engine_retrying"]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0][1]["turn_id"], "tn_test")
        self.assertEqual(retry_events[0][1]["phase"], "start")
        self.assertEqual(retry_events[0][1]["attempt"], 1)
        self.assertIsInstance(retry_events[0][1]["seq"], int)

    def test_compaction_notification_emits_progress_without_text_chunk(self) -> None:
        engine, fake, live = _make_engine()
        _autoreply(fake, live)
        engine.thread_id = "th_test"
        engine.pi_tools = "read"
        events: list[tuple[str, dict]] = []

        def drive() -> None:
            time.sleep(0.05)
            live.push({"method": "turn/compaction", "params": {"turnId": "tn_test", "phase": "start", "reason": "context_pressure"}})
            live.push({"method": "turn/completed", "params": {
                "turnId": "tn_test", "ok": True, "finalText": "", "toolCalls": 0, "stopReason": "stop",
            }})

        threading.Thread(target=drive, daemon=True).start()

        result = engine.run_turn_with_progress("compact", timeout=5, policy="trusted", on_event=lambda name, payload: events.append((name, payload)))

        self.assertTrue(result.ok, f"turn failed: {result.error}")
        self.assertEqual(result.result, "pi-sdk turn tn_test completed.")
        self.assertIn("engine_compacting", [name for name, _payload in events])
        self.assertNotIn("model_text", [name for name, _payload in events])

    def test_auto_retry_for_other_turn_does_not_disarm_watchdog(self) -> None:
        engine, fake, live = _make_engine()
        _autoreply(fake, live)
        engine.thread_id = "th_test"
        engine.pi_tools = "read"
        engine.ack_timeout = 0.4

        def drive() -> None:
            time.sleep(0.05)
            live.push({"method": "turn/autoRetry", "params": {"turnId": "tn_other", "phase": "start", "attempt": 1}})

        threading.Thread(target=drive, daemon=True).start()

        result = engine.run_turn_with_progress("wrong-turn", timeout=5, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertIn("no output", result.error)
        self.assertFalse(engine.is_healthy())

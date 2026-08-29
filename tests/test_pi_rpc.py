import io
import json
import queue
import threading
import time
import unittest
import argparse

from agent_redis_bridge.engines.pi_rpc import PiRpcEngine


class FakeStdin:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.on_write = None

    def write(self, value: bytes) -> None:
        self.chunks.append(value)
        if self.on_write is not None:
            self.on_write(value)

    def flush(self) -> None:
        pass


class FakeProcess:
    def __init__(self, byte_lines: list[bytes] | None = None) -> None:
        self.stdin = FakeStdin()
        self.stdout = iter(byte_lines or [])
        self.stderr = iter([])
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.terminated = True

    def poll(self) -> int | None:
        return None


class PiFramingTest(unittest.TestCase):
    def test_reader_splits_on_lf_only_and_preserves_cr(self) -> None:
        # Feed a single RAW byte blob (not pre-split) through a real binary stream so the
        # test exercises the actual LF-only splitting: the interior \r in record 1 must NOT
        # split it, and the two records must come back whole. io.BytesIO iterates exactly
        # like the unbuffered/buffered binary pipe the engine reads in production.
        rec1 = json.dumps(
            {"type": "response", "command": "get_state", "id": "x", "success": True,
             "data": {"note": "line1\rline2"}}
        ).encode("utf-8")
        rec2 = json.dumps({"type": "agent_end"}).encode("utf-8")
        blob = rec1 + b"\n" + rec2 + b"\n"
        fake = FakeProcess()
        fake.stdout = io.BytesIO(blob)
        engine = PiRpcEngine(cwd="/tmp/p", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake

        engine._read_stdout()

        first = engine.messages.get_nowait()
        second = engine.messages.get_nowait()
        self.assertEqual(first["data"]["note"], "line1\rline2")   # interior \r preserved, not split
        self.assertEqual(second["type"], "agent_end")
        self.assertTrue(engine.messages.empty())                  # exactly two records, split on \n only


class PiTurnTest(unittest.TestCase):
    def _engine(self) -> tuple[PiRpcEngine, FakeProcess]:
        fake = FakeProcess()
        eng = PiRpcEngine(cwd="/tmp/p", model=None, popen_factory=lambda *args, **kwargs: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        return eng, fake

    def _queue_on_prompt(self, eng: PiRpcEngine, fake: FakeProcess, messages: list[dict]) -> None:
        def on_write(value: bytes) -> None:
            sent = json.loads(value.decode())
            if sent.get("type") == "prompt":
                for message in messages:
                    eng.messages.put(message)

        fake.stdin.on_write = on_write

    def test_happy_path_collects_text_and_uses_get_last(self) -> None:
        eng, fake = self._engine()
        self._queue_on_prompt(
            eng,
            fake,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Hel"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "lo"}},
                {
                    "type": "tool_execution_start",
                    "toolCallId": "c1",
                    "toolName": "bash",
                    "args": {"command": "ls"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "c1",
                    "toolName": "bash",
                    "isError": False,
                    "result": {},
                },
                {"type": "agent_end", "messages": []},
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": 2,
                    "success": True,
                    "data": {"text": "Hello"},
                },
            ],
        )
        events: list[tuple[str, dict]] = []

        res = eng.run_turn_with_progress(
            "hi", timeout=2, policy="trusted", on_event=lambda event, data: events.append((event, data))
        )

        self.assertTrue(res.ok)
        self.assertEqual(res.result, "Hello")
        self.assertIn(("model_text", {"delta": "Hel"}), events)
        self.assertTrue(any(event == "command_started" for event, _ in events))
        self.assertTrue(any(event == "command_finished" for event, _ in events))
        sent = [json.loads(chunk.decode()) for chunk in fake.stdin.chunks]
        self.assertEqual(sent[0]["type"], "prompt")
        self.assertEqual(sent[0]["message"], "hi")
        self.assertEqual(sent[-1]["type"], "get_last_assistant_text")

    def test_rejected_prompt_returns_not_ok_without_agent_end(self) -> None:
        eng, fake = self._engine()
        self._queue_on_prompt(
            eng,
            fake,
            [{"type": "response", "command": "prompt", "id": 1, "success": False, "error": "bad prompt"}],
        )

        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)

        self.assertFalse(res.ok)
        self.assertIn("bad prompt", res.error or "")

    def test_message_error_event_ends_turn_not_ok(self) -> None:
        eng, fake = self._engine()
        self._queue_on_prompt(
            eng,
            fake,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                {"type": "message_update", "assistantMessageEvent": {"type": "error", "reason": "error"}},
            ],
        )

        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)

        self.assertFalse(res.ok)

    def test_auto_retry_final_failure_ends_turn_with_finalerror(self) -> None:
        # rpc.md names the field `finalError` (not `error`) on a failed auto_retry_end.
        eng, fake = self._engine()
        self._queue_on_prompt(
            eng,
            fake,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                {"type": "auto_retry_end", "success": False, "attempt": 3,
                 "finalError": "529 overloaded_error: Overloaded"},
            ],
        )

        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)

        self.assertFalse(res.ok)
        self.assertIn("overloaded", (res.error or "").lower())

    def test_stale_events_drained_before_prompt(self) -> None:
        eng, fake = self._engine()
        eng.messages.put({"type": "agent_end"})
        self._queue_on_prompt(
            eng,
            fake,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": False, "error": "fresh rejection"},
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": 2,
                    "success": True,
                    "data": {"text": "stale success"},
                },
            ],
        )

        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)

        self.assertFalse(res.ok)
        self.assertIn("fresh rejection", res.error or "")

    def test_timeout_marks_engine_unhealthy(self) -> None:
        eng, fake = self._engine()
        self._queue_on_prompt(
            eng,
            fake,
            [{"type": "response", "command": "prompt", "id": 1, "success": True}],
        )

        res = eng.run_turn_with_progress("hi", timeout=0, policy="trusted", on_event=None)

        self.assertFalse(res.ok)
        self.assertFalse(eng.healthy)
        sent = [json.loads(chunk.decode()) for chunk in fake.stdin.chunks]
        self.assertTrue(any(message["type"] == "abort" for message in sent))

    def test_no_ack_wedge_fails_fast_and_marks_unhealthy(self) -> None:
        # Pi never acknowledges the prompt (queue nothing). The ack watchdog
        # must fail the turn quickly — well before the full turn timeout —
        # abort, and mark the engine unhealthy so the pool respawns it.
        eng, fake = self._engine()
        eng.ack_timeout = 0.2  # don't wait the default 30s in the test

        start = time.monotonic()
        res = eng.run_turn_with_progress("hi", timeout=30, policy="trusted", on_event=None)
        elapsed = time.monotonic() - start

        self.assertFalse(res.ok)
        self.assertIn("acknowledge", res.error)
        self.assertFalse(eng.healthy)
        self.assertLess(elapsed, 5)  # fast-fail, not the 30s turn timeout
        sent = [json.loads(chunk.decode()) for chunk in fake.stdin.chunks]
        self.assertTrue(any(message["type"] == "abort" for message in sent))

    def test_ack_then_silence_still_uses_full_timeout(self) -> None:
        # Once pi acks, the watchdog disengages and the normal turn timeout
        # governs — a slow-thinking model must not be killed by the ack window.
        eng, fake = self._engine()
        eng.ack_timeout = 0.1
        self._queue_on_prompt(
            eng, fake,
            [{"type": "response", "command": "prompt", "id": 1, "success": True}],
        )
        start = time.monotonic()
        res = eng.run_turn_with_progress("hi", timeout=1, policy="trusted", on_event=None)
        elapsed = time.monotonic() - start
        self.assertFalse(res.ok)
        self.assertIn("timed out", res.error)  # full-timeout path, not the ack wedge
        self.assertGreaterEqual(elapsed, 1)

    def test_get_last_ignores_late_events_and_matches_id(self) -> None:
        eng, fake = self._engine()
        self._queue_on_prompt(
            eng,
            fake,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                {"type": "agent_end"},
                {"type": "queue_update", "steering": [], "followUp": []},
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": 2,
                    "success": True,
                    "data": {"text": "final"},
                },
            ],
        )

        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)

        self.assertTrue(res.ok)
        self.assertEqual(res.result, "final")

    def test_empty_text_falls_back_to_placeholder(self) -> None:
        eng, fake = self._engine()
        self._queue_on_prompt(
            eng,
            fake,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                {"type": "agent_end"},
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": 2,
                    "success": True,
                    "data": {"text": None},
                },
            ],
        )

        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)

        self.assertTrue(res.ok)
        self.assertIn("completed", res.result)

    def test_start_probe_raises_when_process_dead(self) -> None:
        from agent_redis_bridge.engines.base import EngineError

        fake = FakeProcess()
        eng = PiRpcEngine(cwd="/tmp/p", model=None, popen_factory=lambda *args, **kwargs: fake)

        with self.assertRaises(EngineError):
            eng.start(probe_timeout=0)

    def test_steer_and_interrupt_return_str(self) -> None:
        eng, _fake = self._engine()
        eng.active_prompt_id = 7

        self.assertIsInstance(eng.steer("go"), str)
        self.assertIsInstance(eng.interrupt(), str)

    def test_full_tools_instance_refuses_nontrusted_turn(self) -> None:
        eng, _fake = self._engine()

        res = eng.run_turn_with_progress("hi", timeout=2, policy="human", on_event=None)

        self.assertFalse(res.ok)
        self.assertIn("non-trusted", (res.error or "").lower())

    def test_review_instance_serves_nontrusted_turn(self) -> None:
        fake = FakeProcess()
        eng = PiRpcEngine(
            cwd="/tmp/p",
            model=None,
            pi_tools="read,grep,find,ls",
            popen_factory=lambda *args, **kwargs: fake,
        )
        eng.process = fake
        eng.messages = queue.Queue()
        self._queue_on_prompt(
            eng,
            fake,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                {"type": "agent_end"},
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": 2,
                    "success": True,
                    "data": {"text": "reviewed"},
                },
            ],
        )

        res = eng.run_turn_with_progress("review", timeout=2, policy="human", on_event=None)

        self.assertTrue(res.ok)
        self.assertEqual(res.result, "reviewed")

    def test_extension_ui_dialog_is_cancelled_fireforget_ignored(self) -> None:
        eng, fake = self._engine()

        eng._handle_client_message({"type": "extension_ui_request", "id": "u1", "method": "confirm", "title": "ok?"})
        eng._handle_client_message({"type": "extension_ui_request", "id": "u2", "method": "notify", "message": "hi"})

        sent = [json.loads(chunk.decode()) for chunk in fake.stdin.chunks]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["type"], "extension_ui_response")
        self.assertEqual(sent[0]["id"], "u1")
        self.assertEqual(sent[0]["confirmed"], False)

    def test_camelcase_toolcall_start_maps_to_command_started(self) -> None:
        eng, fake = self._engine()
        self._queue_on_prompt(
            eng,
            fake,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                {
                    "type": "message_update",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {"command": "ls"}}
                        ],
                    },
                    "assistantMessageEvent": {
                        "type": "toolcall_start",
                        "contentIndex": 0,
                        "partial": {
                            "role": "assistant",
                            "content": [
                                {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {"command": "ls"}}
                            ],
                        },
                    },
                },
                {"type": "agent_end", "messages": []},
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": 2,
                    "success": True,
                    "data": {"text": "ok"},
                },
            ],
        )
        events: list[tuple[str, dict]] = []

        res = eng.run_turn_with_progress(
            "hi", timeout=2, policy="trusted", on_event=lambda event, data: events.append((event, data))
        )

        self.assertTrue(res.ok)
        started = [d for e, d in events if e == "command_started"]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["tool_call_id"], "tc1")
        self.assertEqual(started[0]["command"], "ls")
        self.assertEqual(started[0]["kind"], "bash")

    def test_camelcase_toolcall_end_is_not_a_command_finished(self) -> None:
        eng, fake = self._engine()
        self._queue_on_prompt(
            eng,
            fake,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                {
                    "type": "message_update",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {"command": "ls"}}
                        ],
                    },
                    "assistantMessageEvent": {
                        "type": "toolcall_end",
                        "contentIndex": 0,
                        "toolCall": {
                            "type": "toolCall",
                            "id": "tc1",
                            "name": "bash",
                            "arguments": {"command": "ls"},
                        },
                        "partial": {
                            "role": "assistant",
                            "content": [
                                {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {"command": "ls"}}
                            ],
                        },
                    },
                },
                {"type": "agent_end", "messages": []},
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": 2,
                    "success": True,
                    "data": {"text": "ok"},
                },
            ],
        )
        events: list[tuple[str, dict]] = []

        res = eng.run_turn_with_progress(
            "hi", timeout=2, policy="trusted", on_event=lambda event, data: events.append((event, data))
        )

        self.assertTrue(res.ok)
        self.assertEqual([d for e, d in events if e == "command_finished"], [])
        self.assertIn("turn_completed", [e for e, _ in events])

    def test_toolcall_and_tool_execution_deduped_by_tool_call_id(self) -> None:
        eng, fake = self._engine()
        self._queue_on_prompt(
            eng,
            fake,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                # camelCase start arrives first
                {
                    "type": "message_update",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {}}
                        ],
                    },
                    "assistantMessageEvent": {
                        "type": "toolcall_start",
                        "contentIndex": 0,
                        "partial": {
                            "role": "assistant",
                            "content": [
                                {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {}}
                            ],
                        },
                    },
                },
                # snake_case start for same id — should be deduped
                {
                    "type": "tool_execution_start",
                    "toolCallId": "tc1",
                    "toolName": "bash",
                    "args": {"command": "ls"},
                },
                # camelCase end arrives first
                {
                    "type": "message_update",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {}}
                        ],
                    },
                    "assistantMessageEvent": {
                        "type": "toolcall_end",
                        "contentIndex": 0,
                        "toolCall": {
                            "type": "toolCall",
                            "id": "tc1",
                            "name": "bash",
                            "arguments": {},
                        },
                        "partial": {
                            "role": "assistant",
                            "content": [
                                {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {}}
                            ],
                        },
                    },
                },
                # snake_case end for same id — should be deduped
                {
                    "type": "tool_execution_end",
                    "toolCallId": "tc1",
                    "toolName": "bash",
                    "isError": True,
                    "result": {},
                },
                {"type": "agent_end", "messages": []},
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": 2,
                    "success": True,
                    "data": {"text": "ok"},
                },
            ],
        )
        events: list[tuple[str, dict]] = []

        res = eng.run_turn_with_progress(
            "hi", timeout=2, policy="trusted", on_event=lambda event, data: events.append((event, data))
        )

        self.assertTrue(res.ok)
        started = [d for e, d in events if e == "command_started"]
        finished = [d for e, d in events if e == "command_finished"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(finished), 1)
        # start is deduped by id; finish comes only from real execution completion.
        self.assertEqual(started[0]["tool_call_id"], "tc1")
        self.assertEqual(finished[0]["tool_call_id"], "tc1")
        self.assertEqual(finished[0]["status"], "failed")
        self.assertEqual(finished[0]["exit_code"], 1)
        self.assertEqual([d for e, d in events if e == "turn_completed"], [{"ok": True, "kind": "turn_completed"}])

    def test_two_engines_run_concurrently_without_cross_talk(self) -> None:
        eng_a, fake_a = self._engine()
        eng_b, fake_b = self._engine()
        self._queue_on_prompt(
            eng_a,
            fake_a,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                {"type": "agent_end"},
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": 2,
                    "success": True,
                    "data": {"text": "result-a"},
                },
            ],
        )
        self._queue_on_prompt(
            eng_b,
            fake_b,
            [
                {"type": "response", "command": "prompt", "id": 1, "success": True},
                {"type": "agent_end"},
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": 2,
                    "success": True,
                    "data": {"text": "result-b"},
                },
            ],
        )
        results: dict[str, str] = {}

        def run(name: str, engine: PiRpcEngine) -> None:
            result = engine.run_turn_with_progress(name, timeout=2, policy="trusted", on_event=None)
            results[name] = result.result

        threads = [threading.Thread(target=run, args=("a", eng_a)), threading.Thread(target=run, args=("b", eng_b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results, {"a": "result-a", "b": "result-b"})
        self.assertTrue(eng_a.is_healthy())
        self.assertTrue(eng_b.is_healthy())


class PiBridgeWiringTest(unittest.TestCase):
    def test_build_engine_returns_pi_rpc(self) -> None:
        from agent_redis_bridge.bridge import ENGINE_TO_TOOL, build_engine

        self.assertEqual(ENGINE_TO_TOOL["pi-rpc"], "pi")
        args = argparse.Namespace(engine="pi-rpc", model="minimax/MiniMax-M3", pi_tools=None)

        eng = build_engine(args, cwd="/tmp/p")

        self.assertIsInstance(eng, PiRpcEngine)
        self.assertEqual(eng.model, "minimax/MiniMax-M3")


if __name__ == "__main__":
    unittest.main()

import queue
import os
import unittest
from typing import Any
from unittest.mock import patch

from agent_redis_bridge.engines.codex import AppServerError, CodexEngine


class FakeCodexEngine(CodexEngine):
    def __init__(self) -> None:
        super().__init__(
            cwd="/tmp",
            model="gpt-5.5",
            approval_policy="never",
            sandbox="workspace-write",
        )
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.thread_counter = 1
        self.turn_counter = 0
        self.sent_turn_threads: list[str] = []
        self.resumed_threads: list[str] = []
        self.forked_threads: list[tuple[str, dict[str, Any]]] = []
        self.fork_response: dict[str, Any] = {"thread": {"id": "thread-child"}}

    def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        if method == "thread/start":
            self.thread_counter += 1
            return {"thread": {"id": f"thread-{self.thread_counter}"}}
        if method == "thread/resume":
            self.resumed_threads.append(params["threadId"])
            return {}
        if method == "thread/fork":
            self.forked_threads.append((params["threadId"], dict(params)))
            return self.fork_response
        if method == "turn/start":
            self.sent_turn_threads.append(params["threadId"])
            self.turn_counter += 1
            return {"turn": {"id": f"turn-{self.turn_counter}"}}
        return {}

    def _get_message(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None


class LoopbackCodexEngine(CodexEngine):
    def __init__(self) -> None:
        super().__init__(
            cwd="/tmp",
            model="gpt-5.5",
            approval_policy="never",
            sandbox="workspace-write",
        )
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.sent: list[dict[str, Any]] = []

    def _send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        method = payload.get("method")
        if method == "thread/fork":
            self.messages.put({"method": "thread/notice", "params": {"ignored": True}})
            self.messages.put({"id": payload["id"], "result": {"thread": {"id": "thread-child"}}})
        elif method == "turn/start":
            turn_id = "turn-child"
            self.messages.put({"id": payload["id"], "result": {"turn": {"id": turn_id}}})
            self.messages.put({"method": "turn/completed", "params": {"turnId": turn_id}})

    def _get_message(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None


class InitTimeoutEnvTests(unittest.TestCase):
    def test_default_init_timeout_is_60(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            eng = FakeCodexEngine()

        self.assertEqual(eng._init_timeout, 60)

    def test_env_overrides_init_timeout(self) -> None:
        with patch.dict(os.environ, {"BRIDGE_ENGINE_INIT_TIMEOUT_S": "33"}, clear=True):
            eng = FakeCodexEngine()

        self.assertEqual(eng._init_timeout, 33)

    def test_initialize_uses_init_timeout(self) -> None:
        captured: list[tuple[str, int]] = []

        class CaptureStartEngine(CodexEngine):
            def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
                captured.append((method, timeout))
                if method == "thread/start":
                    return {"thread": {"id": "t"}}
                return {}

            def _read_stdout(self) -> None:
                pass

        with patch.dict(os.environ, {"BRIDGE_ENGINE_INIT_TIMEOUT_S": "7"}, clear=True):
            eng = CaptureStartEngine(
                cwd="/tmp",
                model="gpt-5.5",
                approval_policy="never",
                sandbox="workspace-write",
            )
            with patch("agent_redis_bridge.engines.codex.subprocess.Popen"), \
                 patch("agent_redis_bridge.engines.codex.start_stderr_drain", return_value=None):
                eng.start()

        self.assertIn(("initialize", 7), captured)


class CodexIoTest(unittest.TestCase):
    def test_command_args_default(self) -> None:
        app_server = CodexEngine(
            cwd="/tmp",
            model="gpt-5.5",
            approval_policy="never",
            sandbox="workspace-write",
        )

        self.assertEqual(app_server.command_args(), ["codex", "app-server", "--listen", "stdio://"])

    def test_command_args_with_bypass(self) -> None:
        app_server = CodexEngine(
            cwd="/tmp",
            model="gpt-5.5",
            approval_policy="never",
            sandbox="workspace-write",
            bypass_approvals_and_sandbox=True,
        )

        self.assertEqual(
            app_server.command_args(),
            ["codex", "--dangerously-bypass-approvals-and-sandbox", "app-server", "--listen", "stdio://"],
        )

    def test_thread_start_params_default_sandbox(self) -> None:
        app_server = CodexEngine(
            cwd="/tmp",
            model="gpt-5.5",
            approval_policy="never",
            sandbox="workspace-write",
        )

        self.assertEqual(app_server.thread_start_params()["sandbox"], "workspace-write")

    def test_thread_start_params_bypass_uses_danger_full_access(self) -> None:
        app_server = CodexEngine(
            cwd="/tmp",
            model="gpt-5.5",
            approval_policy="never",
            sandbox="workspace-write",
            bypass_approvals_and_sandbox=True,
        )

        self.assertEqual(app_server.thread_start_params()["sandbox"], "danger-full-access")
        self.assertEqual(app_server.thread_start_params()["approvalPolicy"], "never")

    def test_reset_context_starts_new_thread(self) -> None:
        engine = FakeCodexEngine()
        engine.thread_id = "thread-1"

        self.assertEqual(engine.reset_context(), "thread-2")

        self.assertEqual(engine.thread_id, "thread-2")

    def test_turn_uses_reset_thread(self) -> None:
        engine = FakeCodexEngine()
        engine.thread_id = "thread-1"
        engine.reset_context()
        engine.messages.put({"method": "turn/completed", "params": {"turnId": "turn-1"}})

        result = engine.run_turn_with_progress(
            "do work",
            timeout=1,
            policy="trusted",
            on_event=None,
        )

        self.assertTrue(result.ok)
        self.assertEqual(engine.sent_turn_threads, ["thread-2"])

    def test_resume_thread_uses_thread_resume_and_updates_live_thread(self) -> None:
        engine = FakeCodexEngine()
        engine.thread_id = "thread-1"

        self.assertEqual(engine.resume_thread("thread-existing"), "thread-existing")

        self.assertEqual(engine.resumed_threads, ["thread-existing"])
        self.assertEqual(engine.thread_id, "thread-existing")

    def test_fork_thread_uses_thread_fork_and_updates_live_thread_to_child(self) -> None:
        engine = FakeCodexEngine()
        engine.thread_id = "thread-1"

        self.assertEqual(engine.fork_thread("thread-base"), "thread-child")
        engine.messages.put({"method": "turn/completed", "params": {"turnId": "turn-1"}})
        result = engine.run_turn_with_progress(
            "do work",
            timeout=1,
            policy="trusted",
            on_event=None,
        )

        self.assertTrue(result.ok)
        self.assertEqual(engine.forked_threads, [("thread-base", {"threadId": "thread-base"})])
        self.assertEqual(engine.thread_id, "thread-child")
        self.assertEqual(engine.sent_turn_threads, ["thread-child"])

    def test_fork_thread_rejects_malformed_response(self) -> None:
        engine = FakeCodexEngine()
        engine.fork_response = {"thread": {}}

        with self.assertRaisesRegex(AppServerError, "thread/fork did not return thread.id"):
            engine.fork_thread("thread-base")

    def test_fork_thread_ignores_unrelated_notification_before_response(self) -> None:
        engine = LoopbackCodexEngine()

        self.assertEqual(engine.fork_thread("thread-base"), "thread-child")
        result = engine.run_turn_with_progress(
            "do work",
            timeout=1,
            policy="trusted",
            on_event=None,
        )

        self.assertTrue(result.ok)
        self.assertEqual(engine.sent[0]["method"], "thread/fork")
        self.assertEqual(engine.sent[0]["params"], {"threadId": "thread-base"})
        self.assertEqual(engine.sent[1]["params"]["threadId"], "thread-child")


if __name__ == "__main__":
    unittest.main()


class _DeadProcess:
    returncode = 137

    def poll(self) -> int:
        return 137


class _AliveProcess:
    returncode = None

    def poll(self) -> None:
        return None


class _TurnOnlyEngine(CodexEngine):
    """Real turn/request loops + real _get_message; turn/start stubbed."""

    def __init__(self, process) -> None:
        super().__init__(cwd="/tmp", model=None, approval_policy="never", sandbox="workspace-write")
        self.process = process
        self.thread_id = "thread-1"

    def request(self, method, params, *, timeout):
        if method == "turn/start":
            return {"turn": {"id": "turn-9"}}
        return {}


class _SendStubEngine(CodexEngine):
    """Real request() wait loop; _send stubbed so no pipe is needed."""

    def __init__(self, process) -> None:
        super().__init__(cwd="/tmp", model=None, approval_policy="never", sandbox="workspace-write")
        self.process = process

    def _send(self, payload) -> None:
        pass


class CodexHealthContractTest(unittest.TestCase):
    """Audit CDX-3 (panel-confirmed): codex had no is_healthy() at all — the pool
    recycled a dead subprocess forever — and no process-liveness fast-fail, so a
    mid-turn child death was only discovered by waiting out the full timeout."""

    def test_is_healthy_false_when_process_dead(self) -> None:
        eng = _TurnOnlyEngine(_DeadProcess())
        self.assertFalse(eng.is_healthy())

    def test_is_healthy_true_when_process_alive(self) -> None:
        eng = _TurnOnlyEngine(_AliveProcess())
        self.assertTrue(eng.is_healthy())

    def test_turn_fails_fast_when_process_dies_mid_turn(self) -> None:
        import time as _time

        eng = _TurnOnlyEngine(_DeadProcess())
        started = _time.monotonic()

        result = eng.run_turn_with_progress("work", timeout=6, policy="trusted", on_event=None)

        elapsed = _time.monotonic() - started
        self.assertLess(elapsed, 3, "dead child must fail fast, not wait out the timeout")
        self.assertFalse(result.ok)
        self.assertIn("exited", result.error)
        self.assertIn("137", result.error)
        self.assertFalse(eng.is_healthy())

    def test_request_fails_fast_when_process_dead(self) -> None:
        import time as _time

        eng = _SendStubEngine(_DeadProcess())
        started = _time.monotonic()

        with self.assertRaises(AppServerError) as ctx:
            eng.request("thread/start", {}, timeout=6)

        self.assertLess(_time.monotonic() - started, 3)
        self.assertIn("exited", str(ctx.exception))

    def test_turn_timeout_marks_engine_unhealthy(self) -> None:
        eng = _TurnOnlyEngine(_AliveProcess())

        result = eng.run_turn_with_progress("work", timeout=1, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertFalse(eng.is_healthy())

    def test_read_stdout_exception_flips_unhealthy_instead_of_silent_thread_death(self) -> None:
        class ExplodingStdout:
            def __iter__(self):
                return self

            def __next__(self):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        class ProcWithBadStdout(_AliveProcess):
            stdout = ExplodingStdout()

        eng = _TurnOnlyEngine(ProcWithBadStdout())

        eng._read_stdout()  # must swallow and mark, not raise out of the thread

        self.assertFalse(eng.is_healthy())


def test_seat_memory_disable_env_adds_config_override(monkeypatch):
    """Panel-seat exclusion rule: AGENT_BRIDGE_CODEX_DISABLE_AUTO_MEMORY=1 must
    force features.auto_memory=false so reviewer seats stay cold even when the
    operator's interactive ~/.codex config enables memory."""
    from agent_redis_bridge.engines import codex as codex_engine

    monkeypatch.setenv("AGENT_BRIDGE_CODEX_DISABLE_AUTO_MEMORY", "1")
    engine = codex_engine.CodexEngine.__new__(codex_engine.CodexEngine)
    engine.bypass_approvals_and_sandbox = False
    args = codex_engine.CodexEngine.command_args(engine)
    assert "-c" in args and "features.auto_memory=false" in args

    monkeypatch.delenv("AGENT_BRIDGE_CODEX_DISABLE_AUTO_MEMORY")
    args = codex_engine.CodexEngine.command_args(engine)
    assert "features.auto_memory=false" not in args

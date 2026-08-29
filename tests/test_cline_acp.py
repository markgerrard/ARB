import queue
import unittest

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.cline_acp import ClineAcpEngine, normalize_session_update


class FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, value: str) -> None:
        self.lines.append(value)

    def flush(self) -> None:
        pass


class FakeProcess:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.stdin = FakeStdin()
        self.stdout = iter(lines or [])
        self.stderr = iter([])
        self.terminated = False
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.terminated = True

    def poll(self) -> int | None:
        return self.returncode


MODEL = "deepseek/deepseek-v4-flash"
WRONG_DEFAULT = "anthropic/claude-sonnet-4.6"


def _config_options(current: str) -> list[dict]:
    return [
        {
            "type": "select",
            "id": "provider",
            "currentValue": "cline",
            "options": [{"value": "cline", "name": "Cline Usage-Billing"}],
        },
        {
            "type": "select",
            "id": "model",
            "currentValue": current,
            "options": [
                {"value": WRONG_DEFAULT, "name": "Claude Sonnet 4.6"},
                {"value": MODEL, "name": "DeepSeek V4 Flash"},
            ],
        },
    ]


def _session_new_result(current: str = WRONG_DEFAULT) -> dict:
    return {
        "sessionId": "sess-1",
        "modes": {
            "availableModes": [{"id": "plan", "name": "Plan"}, {"id": "act", "name": "Act"}],
            "currentModeId": "act",
        },
        "models": {
            "availableModels": [
                {"modelId": WRONG_DEFAULT, "name": "Claude Sonnet 4.6"},
                {"modelId": MODEL, "name": "DeepSeek V4 Flash"},
            ],
            "currentModelId": current,
        },
        "configOptions": _config_options(current),
    }


class ClineAcpTestBase(unittest.TestCase):
    def _engine(self, model: str | None = MODEL) -> tuple[ClineAcpEngine, FakeProcess]:
        fake = FakeProcess()
        eng = ClineAcpEngine(cwd="/tmp/c", model=model, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        return eng, fake

    def _respond_start_sequence(
        self,
        eng: ClineAcpEngine,
        *,
        session_new: dict | None = None,
        set_model_result: dict | None = None,
    ) -> None:
        # ids are allocated in send order: 1=initialize, 2=session/new, 3=set_config_option
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}})
        eng.messages.put(
            {"jsonrpc": "2.0", "id": 2, "result": session_new or _session_new_result()}
        )
        if set_model_result is not None:
            eng.messages.put({"jsonrpc": "2.0", "id": 3, "result": set_model_result})


class ClineAcpStartTest(ClineAcpTestBase):
    def test_command_args(self) -> None:
        eng, _ = self._engine()
        self.assertEqual(eng.command_args(), ["cline", "--acp"])

    def test_start_sets_model_and_verifies_readback(self) -> None:
        eng, fake = self._engine()
        self._respond_start_sequence(
            eng, set_model_result={"configOptions": _config_options(MODEL)}
        )
        eng._start_handshake()
        self.assertEqual(eng.session_id, "sess-1")
        # the set_config_option request must have targeted the model configId
        joined = "".join(fake.stdin.lines)
        self.assertIn('"session/set_config_option"', joined)
        self.assertIn(MODEL, joined)

    def test_start_without_model_raises(self) -> None:
        eng, _ = self._engine(model=None)
        self._respond_start_sequence(eng)
        with self.assertRaises(EngineError) as ctx:
            eng._start_handshake()
        self.assertIn("explicit --model", str(ctx.exception))

    def test_start_with_unresolvable_model_raises(self) -> None:
        eng, _ = self._engine(model="deepseek/does-not-exist")
        self._respond_start_sequence(eng)
        with self.assertRaises(EngineError) as ctx:
            eng._start_handshake()
        self.assertIn("could not resolve model", str(ctx.exception))

    def test_start_readback_mismatch_raises(self) -> None:
        # set_config_option "succeeds" but the echoed currentValue is still the
        # session default — the fail-open shape the hard-fail design exists for.
        eng, _ = self._engine()
        self._respond_start_sequence(
            eng, set_model_result={"configOptions": _config_options(WRONG_DEFAULT)}
        )
        with self.assertRaises(EngineError) as ctx:
            eng._start_handshake()
        self.assertIn("read-back", str(ctx.exception))
        self.assertIn(WRONG_DEFAULT, str(ctx.exception))

    def test_start_readback_missing_config_raises(self) -> None:
        eng, _ = self._engine()
        self._respond_start_sequence(eng, set_model_result={})
        with self.assertRaises(EngineError) as ctx:
            eng._start_handshake()
        self.assertIn("read-back", str(ctx.exception))

    def test_resolve_model_by_name(self) -> None:
        eng, _ = self._engine(model="DeepSeek V4 Flash")
        self._respond_start_sequence(
            eng, set_model_result={"configOptions": _config_options(MODEL)}
        )
        eng._start_handshake()
        joined = "".join(eng.process.stdin.lines)  # type: ignore[union-attr]
        self.assertIn(f'"value": "{MODEL}"'.replace(" ", ""), joined.replace(" ", ""))


class ClineAcpPolicyTest(ClineAcpTestBase):
    def _started(self) -> tuple[ClineAcpEngine, FakeProcess]:
        eng, fake = self._engine()
        self._respond_start_sequence(
            eng, set_model_result={"configOptions": _config_options(MODEL)}
        )
        eng._start_handshake()
        fake.stdin.lines.clear()
        return eng, fake

    def test_trusted_maps_to_act(self) -> None:
        eng, fake = self._started()
        eng.messages.put({"jsonrpc": "2.0", "id": 4, "result": {}})
        eng.set_session_mode_for_policy("trusted")
        joined = "".join(fake.stdin.lines)
        self.assertIn('"session/set_mode"', joined)
        self.assertIn('"act"', joined)

    def test_human_maps_to_plan(self) -> None:
        eng, fake = self._started()
        eng.messages.put({"jsonrpc": "2.0", "id": 4, "result": {}})
        eng.set_session_mode_for_policy("human")
        joined = "".join(fake.stdin.lines)
        self.assertIn('"plan"', joined)

    def test_permission_allowed_when_trusted(self) -> None:
        eng, fake = self._started()
        eng._respond_to_client_request(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "sess-1",
                    "toolCall": {"title": "run_commands: pwd"},
                    "options": [
                        {"optionId": "allow_once", "kind": "allow_once"},
                        {"optionId": "reject_once", "kind": "reject_once"},
                    ],
                },
            },
            policy="trusted",
        )
        self.assertIn('"selected"', fake.stdin.lines[-1])
        self.assertIn('"allow_once"', fake.stdin.lines[-1])

    def test_permission_denied_when_human(self) -> None:
        eng, fake = self._started()
        eng._respond_to_client_request(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "sess-1",
                    "toolCall": {"title": "run_commands: rm -rf /"},
                    "options": [{"optionId": "allow_once", "kind": "allow_once"}],
                },
            },
            policy="human",
        )
        self.assertIn('"cancelled"', fake.stdin.lines[-1])
        self.assertEqual(eng._deny_count, 1)
        self.assertEqual(eng._last_denied_title, "run_commands: rm -rf /")

    def test_permission_denied_for_foreign_session_even_when_trusted(self) -> None:
        eng, fake = self._started()
        eng._respond_to_client_request(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "someone-else",
                    "options": [{"optionId": "allow_once", "kind": "allow_once"}],
                },
            },
            policy="trusted",
        )
        self.assertIn('"cancelled"', fake.stdin.lines[-1])

    def test_permission_denied_outside_authorizing_turn(self) -> None:
        eng, fake = self._started()
        eng._respond_to_client_request(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "sess-1",
                    "options": [{"optionId": "allow_once", "kind": "allow_once"}],
                },
            },
            policy=None,
        )
        self.assertIn('"cancelled"', fake.stdin.lines[-1])
        # None-policy denials are fail-closed guards, not model deny-loops
        self.assertEqual(eng._deny_count, 0)

    def test_unknown_client_method_rejected(self) -> None:
        eng, fake = self._started()
        eng._respond_to_client_request(
            {"jsonrpc": "2.0", "id": 9, "method": "fs/read_text_file", "params": {}},
            policy="trusted",
        )
        self.assertIn("-32601", fake.stdin.lines[-1])


class ClineAcpTurnTest(ClineAcpTestBase):
    def _started(self) -> tuple[ClineAcpEngine, FakeProcess]:
        eng, fake = self._engine()
        self._respond_start_sequence(
            eng, set_model_result={"configOptions": _config_options(MODEL)}
        )
        eng._start_handshake()
        fake.stdin.lines.clear()
        return eng, fake

    def test_turn_end_turn_ok(self) -> None:
        eng, _ = self._started()
        eng.messages.put({"jsonrpc": "2.0", "id": 4, "result": {}})  # set_mode
        eng.messages.put(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sess-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "HELLO"},
                    },
                },
            }
        )
        eng.messages.put({"jsonrpc": "2.0", "id": 5, "result": {"stopReason": "end_turn"}})
        result = eng.run_turn_with_progress("say hello", timeout=10, policy="trusted", on_event=None)
        self.assertTrue(result.ok)
        self.assertEqual(result.result, "HELLO")
        self.assertEqual(result.stop_reason, "end_turn")

    def test_turn_cancelled_not_ok(self) -> None:
        eng, _ = self._started()
        eng.messages.put({"jsonrpc": "2.0", "id": 4, "result": {}})
        eng.messages.put({"jsonrpc": "2.0", "id": 5, "result": {"stopReason": "cancelled"}})
        result = eng.run_turn_with_progress("task", timeout=10, policy="trusted", on_event=None)
        self.assertFalse(result.ok)
        self.assertIn("cancelled", result.error or "")

    def test_turn_process_exit_reported(self) -> None:
        eng, fake = self._started()
        eng.messages.put({"jsonrpc": "2.0", "id": 4, "result": {}})
        fake.returncode = 1
        result = eng.run_turn_with_progress("task", timeout=3, policy="trusted", on_event=None)
        self.assertFalse(result.ok)
        self.assertIn("exited", result.error or "")
        self.assertFalse(eng.is_healthy())

    def test_retires_after_turn(self) -> None:
        eng, _ = self._engine()
        self.assertTrue(eng.retire_after_turn)
        self.assertFalse(eng.supports_thread_resume)
        self.assertFalse(eng.supports_continuation)


class ClineNormalizeTest(unittest.TestCase):
    def test_message_chunk(self) -> None:
        event = normalize_session_update(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
        )
        self.assertEqual(event, ("model_text", {"delta": "hi"}))

    def test_thought_chunk_meaningful(self) -> None:
        event = normalize_session_update(
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "thinking about the problem."},
            }
        )
        assert event is not None
        self.assertEqual(event[0], "model_thinking")

    def test_thought_chunk_fragment_suppressed(self) -> None:
        event = normalize_session_update(
            {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hm"}}
        )
        self.assertIsNone(event)

    def test_tool_call_pending_starts(self) -> None:
        titles: dict[str, str] = {}
        event = normalize_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "run_commands: pwd",
                "status": "pending",
            },
            titles,
        )
        assert event is not None
        name, data = event
        self.assertEqual(name, "command_started")
        self.assertEqual(data["command"], "run_commands: pwd")
        self.assertEqual(titles["t1"], "run_commands: pwd")

    def test_tool_call_update_completed_reuses_title(self) -> None:
        titles = {"t1": "run_commands: pwd"}
        event = normalize_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"},
            titles,
        )
        assert event is not None
        name, data = event
        self.assertEqual(name, "command_finished")
        self.assertEqual(data["command"], "run_commands: pwd")
        self.assertEqual(data["exit_code"], 0)

    def test_tool_call_failed_exit_code(self) -> None:
        event = normalize_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "failed"}
        )
        assert event is not None
        self.assertEqual(event[1]["exit_code"], 1)

    def test_diff_path_enrichment(self) -> None:
        event = normalize_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t2",
                "title": "edit",
                "status": "pending",
                "content": [{"type": "diff", "path": "/tmp/x.py"}],
            }
        )
        assert event is not None
        self.assertEqual(event[1]["path"], "/tmp/x.py")
        self.assertIn("/tmp/x.py", event[1]["command"])

    def test_session_info_update(self) -> None:
        event = normalize_session_update(
            {"sessionUpdate": "session_info_update", "title": "My session"}
        )
        self.assertEqual(event, ("session_info", {"title": "My session"}))

    def test_session_info_update_timestamp_only_dropped(self) -> None:
        # Observed cline 3.0.48 shape: a pure updatedAt heartbeat, no title.
        # Dropped rather than surfaced as session_update_unknown noise.
        event = normalize_session_update(
            {"sessionUpdate": "session_info_update", "updatedAt": "2026-08-01T07:28:10.052Z"}
        )
        self.assertIsNone(event)

    def test_unknown_update_passthrough(self) -> None:
        event = normalize_session_update({"sessionUpdate": "brand_new_thing"})
        self.assertEqual(event, ("session_update_unknown", {"sessionUpdate": "brand_new_thing"}))

    def test_malformed_update_dropped(self) -> None:
        self.assertIsNone(normalize_session_update({}))


class ClineRegistrationTest(unittest.TestCase):
    def test_engine_registered(self) -> None:
        from agent_redis_bridge.bridge import ENGINE_TO_TOOL, normalize_engine_name

        self.assertEqual(ENGINE_TO_TOOL.get("cline-acp"), "cline")
        self.assertEqual(normalize_engine_name("cline-acp"), "cline-acp")

    def test_support_tier_experimental(self) -> None:
        from agent_redis_bridge.engines.support_tiers import EXPERIMENTAL, SUPPORT_TIERS

        self.assertEqual(SUPPORT_TIERS.get("cline-acp"), EXPERIMENTAL)


if __name__ == "__main__":
    unittest.main()

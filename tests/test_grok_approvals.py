import json
import os
import queue
import unittest

from agent_redis_bridge.engines._acp import _select_allow_option
from agent_redis_bridge.engines.grok_acp import GrokAcpEngine


ASK_OPTIONS = [
    {"optionId": "allow-edits-session", "name": "Always allow edits", "kind": "allow_always"},
    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
]


class SelectAllowOptionTest(unittest.TestCase):
    def test_prefers_allow_once_over_allow_always(self) -> None:
        self.assertEqual(_select_allow_option({"options": ASK_OPTIONS}), "allow-once")

    def test_no_allow_kind_and_no_allow_substring_returns_none(self) -> None:
        # V3(b) fixture discipline: reject-only optionIds with NO "allow" substring,
        # otherwise the substring fallback defeats the case (cold-Opus r1).
        options = [{"optionId": "deny-once", "name": "Deny", "kind": "reject_once"}]
        self.assertIsNone(_select_allow_option({"options": options}))

    def test_cursor_namespace_still_resolves(self) -> None:
        from agent_redis_bridge.engines.cursor_acp import _select_allow_option as cursor_select

        self.assertIs(cursor_select, _select_allow_option)


class FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, value: str) -> None:
        self.lines.append(value)

    def flush(self) -> None:
        pass


class FakeStdout:
    def __init__(self, messages: list[dict]) -> None:
        self.lines = [json.dumps(m) + "\n" for m in messages]

    def __iter__(self):
        return iter(self.lines)


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout([])
        self.stderr = FakeStdout([])
        self.terminated = False
        self.exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 0

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        self.terminated = True
        self.exit_code = -9


def make_engine(*, retire: bool = True) -> GrokAcpEngine:
    engine = GrokAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: FakeProcess())
    engine.retire_after_turn = retire
    engine.process = FakeProcess()  # type: ignore[assignment]
    engine.session_id = "sess-1"
    engine.messages = queue.Queue()
    return engine


def ask(rid: int, session_id: str = "sess-1", title: str = "write file") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": {"toolCallId": f"tc-{rid}", "kind": "edit", "title": title},
            "options": list(ASK_OPTIONS),
        },
    }


def sent(engine: GrokAcpEngine) -> list[dict]:
    return [json.loads(line) for line in engine.process.stdin.lines]


class SessionIdGateTest(unittest.TestCase):
    def test_stale_session_ask_denied_even_under_trusted(self) -> None:
        engine = make_engine()  # session_id == "sess-1"
        events: list[tuple[str, dict]] = []
        with self.assertLogs("agent_redis_bridge.engines.grok_acp", level="WARNING"):
            engine._respond_to_client_request(
                ask(0, session_id="sess-OLD"), policy="trusted",
                on_event=lambda n, d: events.append((n, d)),
            )
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(engine._deny_count, 0)   # unbudgeted: not this turn's ask
        self.assertEqual(events, [])              # no event mis-attributed to the turn

    def test_missing_session_id_fails_the_gate(self) -> None:
        engine = make_engine()
        message = ask(0)
        del message["params"]["sessionId"]
        engine._respond_to_client_request(message, policy="trusted", on_event=None)
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})

    def test_current_session_ask_still_allowed_under_trusted(self) -> None:
        engine = make_engine()
        engine._respond_to_client_request(ask(0, session_id="sess-1"), policy="trusted", on_event=None)
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"]["outcome"]["outcome"], "selected")


class TrustedAllowTest(unittest.TestCase):
    def test_trusted_ask_answered_with_selected_offered_allow_once(self) -> None:
        engine = make_engine()
        engine._respond_to_client_request(ask(0), policy="trusted", on_event=None)
        reply = sent(engine)[-1]
        self.assertEqual(reply["id"], 0)
        self.assertEqual(reply["result"], {"outcome": {"outcome": "selected", "optionId": "allow-once"}})

    def test_trusted_with_no_allow_option_cancels_fail_closed_floor(self) -> None:
        engine = make_engine()
        message = ask(0)
        message["params"]["options"] = [{"optionId": "deny-once", "name": "Deny", "kind": "reject_once"}]
        with self.assertLogs("agent_redis_bridge.engines.grok_acp", level="WARNING"):
            engine._respond_to_client_request(message, policy="trusted", on_event=None)
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})

    def test_trusted_with_malformed_params_cancels(self) -> None:
        engine = make_engine()
        message = ask(0)
        message["params"] = None
        engine._respond_to_client_request(message, policy="trusted", on_event=None)
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})

    def test_stale_decision_state_is_gone(self) -> None:
        engine = make_engine()
        engine.request = lambda *a, **k: {}  # type: ignore[method-assign]  # mode RPC not under test
        engine.set_session_mode_for_policy("trusted")
        self.assertFalse(hasattr(engine, "_auto_approve_permissions"))
        self.assertFalse(hasattr(engine, "_policy"))

    def test_unknown_method_still_gets_32601(self) -> None:
        engine = make_engine()
        engine._respond_to_client_request(
            {"jsonrpc": "2.0", "id": 7, "method": "xai/some_new_thing", "params": {}},
            policy="trusted",
            on_event=None,
        )
        reply = sent(engine)[-1]
        self.assertEqual(reply["error"]["code"], -32601)


class DenyPathTest(unittest.TestCase):
    def test_non_trusted_ask_denied_with_event_and_count(self) -> None:
        engine = make_engine()
        engine.active_prompt_id = 2
        events: list[tuple[str, dict]] = []
        engine._respond_to_client_request(
            ask(0, title="rm -rf /"), policy="human", on_event=lambda n, d: events.append((n, d))
        )
        reply = sent(engine)[-1]
        self.assertEqual(reply["id"], 0)
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(engine._deny_count, 1)
        self.assertEqual(engine._last_denied_title, "rm -rf /")
        name, data = events[-1]
        self.assertEqual(name, "command_denied")
        self.assertEqual(data["command"], "rm -rf /")
        self.assertEqual(data["kind"], "command_denied")
        self.assertEqual(data["turn_id"], "2")
        self.assertEqual(data["item_id"], "tc-0")
        self.assertEqual(data["deny_count"], 1)
        self.assertEqual(data["deny_budget"], engine.deny_budget)
        self.assertIsInstance(data["seq"], int)

    def test_non_trusted_deny_without_callback_still_counts(self) -> None:
        # V3(d): budget accounting must not depend on observability wiring.
        engine = make_engine()
        with self.assertLogs("agent_redis_bridge.engines.grok_acp", level="WARNING"):
            engine._respond_to_client_request(ask(0), policy="human", on_event=None)
        self.assertEqual(engine._deny_count, 1)

    def test_policy_none_denies_without_budget_count_or_event(self) -> None:
        # V3(e)/V4(c): inter-turn ask => unconditional deny, unbudgeted, no event.
        engine = make_engine()
        events: list[tuple[str, dict]] = []
        with self.assertLogs("agent_redis_bridge.engines.grok_acp", level="WARNING"):
            engine._respond_to_client_request(ask(0), policy=None, on_event=lambda n, d: events.append((n, d)))
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(engine._deny_count, 0)
        self.assertEqual(events, [])

    def test_deny_budget_env_default_is_ten(self) -> None:
        import unittest.mock as mock
        with mock.patch.dict("os.environ", clear=False):
            for var in ("BRIDGE_APPROVAL_DENY_BUDGET", "BRIDGE_APPROVAL_GRACE_S"):
                os.environ.pop(var, None)
            engine = make_engine()
        self.assertEqual(engine.deny_budget, 10)
        self.assertEqual(engine.approval_grace_s, 10.0)


def _turn_messages(*mid: dict) -> list[dict]:
    """set_mode response (id 1), then mid-turn messages, then prompt response (id 2)."""
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        *mid,
        {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}},
    ]


def run_turn(engine: GrokAcpEngine, messages: list[dict], *, policy: str = "trusted", timeout: int = 2):
    for message in messages:
        engine.messages.put(message)
    events: list[tuple[str, dict]] = []
    result = engine.run_turn_with_progress(
        "task", timeout=timeout, policy=policy, on_event=lambda n, d: events.append((n, d))
    )
    return result, events


class InertnessTest(unittest.TestCase):
    def test_no_ask_trusted_turn_stream_is_unchanged(self) -> None:
        # V4(a): default engine, no asks => exactly the pre-change wire sequence.
        engine = make_engine()
        result, events = run_turn(engine, _turn_messages())
        self.assertTrue(result.ok)
        self.assertTrue(engine.is_healthy())
        methods = [m.get("method") for m in sent(engine)]
        self.assertEqual(methods, ["session/set_mode", "session/prompt"])
        self.assertNotIn("command_denied", [n for n, _ in events])

    def test_unknown_method_in_turn_loop_gets_32601(self) -> None:
        engine = make_engine()
        unknown = {"jsonrpc": "2.0", "id": 9, "method": "xai/mystery", "params": {}}
        result, _ = run_turn(engine, _turn_messages(unknown))
        self.assertTrue(result.ok)
        reply = next(m for m in sent(engine) if m.get("id") == 9 and "error" in m)
        self.assertEqual(reply["error"]["code"], -32601)

    def test_unknown_method_in_request_wait_gets_32601(self) -> None:
        engine = make_engine()
        engine.messages.put({"jsonrpc": "2.0", "id": 9, "method": "xai/mystery", "params": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        engine.request("some/method", {}, timeout=2)
        reply = next(m for m in sent(engine) if m.get("id") == 9 and "error" in m)
        self.assertEqual(reply["error"]["code"], -32601)

    def test_ask_in_request_wait_denied_unconditionally(self) -> None:
        # V4(c): inter-turn ask => deny + log, unbudgeted (policy=None path).
        engine = make_engine()
        engine.messages.put(ask(9))
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        with self.assertLogs("agent_redis_bridge.engines.grok_acp", level="WARNING"):
            engine.request("some/method", {}, timeout=2)
        reply = next(m for m in sent(engine) if m.get("id") == 9)
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(engine._deny_count, 0)


class SessionRotationTest(unittest.TestCase):
    def test_default_retire_engine_never_rotates(self) -> None:
        engine = make_engine(retire=True)
        engine._turns_served = 1
        result, _ = run_turn(engine, _turn_messages())
        self.assertTrue(result.ok)
        methods = [m.get("method") for m in sent(engine)]
        self.assertNotIn("session/new", methods)  # V4a: retire-ON stream unchanged

    def test_non_retiring_second_turn_rotates_before_prompt(self) -> None:
        engine = make_engine(retire=False)
        engine._turns_served = 1
        messages = [
            {"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "sess-2"}},  # rotation session/new
            {"jsonrpc": "2.0", "id": 2, "result": {}},                        # set_mode
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},  # prompt
        ]
        result, _ = run_turn(engine, messages)
        self.assertTrue(result.ok)
        methods = [m.get("method") for m in sent(engine)]
        self.assertEqual(methods.index("session/new") + 1, methods.index("session/set_mode"))
        self.assertLess(methods.index("session/new"), methods.index("session/prompt"))
        self.assertEqual(engine.session_id, "sess-2")
        prompt = next(m for m in sent(engine) if m.get("method") == "session/prompt")
        self.assertEqual(prompt["params"]["sessionId"], "sess-2")

    def test_non_retiring_first_turn_does_not_rotate(self) -> None:
        engine = make_engine(retire=False)
        engine._turns_served = 0
        result, _ = run_turn(engine, _turn_messages())
        self.assertTrue(result.ok)
        self.assertNotIn("session/new", [m.get("method") for m in sent(engine)])

    def test_failed_rotation_quarantines_and_never_reuses_old_session(self) -> None:
        from agent_redis_bridge.engines.base import EngineError
        engine = make_engine(retire=False)
        engine._turns_served = 1
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "no"}})
        with self.assertRaises(EngineError):
            engine.run_turn_with_progress("task", timeout=2, policy="trusted", on_event=None)
        self.assertFalse(engine.is_healthy())
        self.assertEqual(engine.session_id, "sess-1")  # build pin 1: flip only on success
        self.assertNotIn("session/prompt", [m.get("method") for m in sent(engine)])


class BudgetExhaustionTest(unittest.TestCase):
    def _engine(self) -> GrokAcpEngine:
        engine = make_engine()
        engine.deny_budget = 2
        engine.approval_grace_s = 0.3
        return engine

    def test_grace_success_returns_legible_error_and_unclean(self) -> None:
        engine = self._engine()
        messages = [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            ask(0, title="w1"), ask(1, title="w2"), ask(2, title="w3"),  # 3rd exceeds budget 2
            {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "cancelled"}},
        ]
        result, events = run_turn(engine, messages, policy="human", timeout=5)
        self.assertFalse(result.ok)
        self.assertIn("deny budget exhausted (3 denials)", result.error)
        self.assertIn("w3", result.error)
        wire = sent(engine)
        denials = [m for m in wire if m.get("result", {}).get("outcome", {}).get("outcome") == "cancelled"]
        self.assertEqual(len(denials), 3)  # the exceeding ask is still ANSWERED (D1 holds)
        self.assertTrue(any(m.get("method") == "session/cancel" for m in wire))
        counts = [d["deny_count"] for n, d in events if n == "command_denied"]
        self.assertEqual(counts, [1, 2, 3])
        self.assertFalse(engine.is_healthy())  # grace success is STILL an unclean end

    def test_grace_expiry_returns_within_bound_and_unhealthy(self) -> None:
        import time
        engine = self._engine()
        messages = [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            ask(0), ask(1), ask(2),
            # no prompt response ever
        ]
        start = time.monotonic()
        result, _ = run_turn(engine, messages, policy="human", timeout=30)
        elapsed = time.monotonic() - start
        self.assertFalse(result.ok)
        self.assertIn("deny budget exhausted", result.error)
        self.assertLess(elapsed, 5)  # grace bound (0.3s) not the 30s turn timeout
        self.assertFalse(engine.is_healthy())

    def test_asks_during_grace_are_still_answered(self) -> None:
        engine = self._engine()
        messages = [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            ask(0), ask(1), ask(2),
            ask(3, title="late"),  # arrives during the grace drain
            {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "cancelled"}},
        ]
        result, _ = run_turn(engine, messages, policy="human", timeout=5)
        wire = sent(engine)
        denials = [m for m in wire if m.get("result", {}).get("outcome", {}).get("outcome") == "cancelled"]
        self.assertEqual(len(denials), 4)  # answer-everything holds through the drain


class HealthTest(unittest.TestCase):
    def test_deny_count_resets_at_prompt_start(self) -> None:
        engine = make_engine()
        engine._deny_count = 5
        engine._last_denied_title = "stale"
        result, _ = run_turn(engine, _turn_messages())
        self.assertTrue(result.ok)
        self.assertEqual(engine._deny_count, 0)
        self.assertIsNone(engine._last_denied_title)

    def test_clean_end_turn_marks_healthy(self) -> None:
        engine = make_engine()
        result, _ = run_turn(engine, _turn_messages())
        self.assertTrue(result.ok)
        self.assertTrue(engine.is_healthy())

    def test_cleanly_failed_turn_reidles_by_design_d3b(self) -> None:
        """CHARACTERIZATION (spec v1.3 D3b, operator-adjudicated 2026-07-10):
        a protocol-clean terminal response with a failure stopReason (failed/
        refusal/cancelled, NO interrupt from us) is a CLEAN end — the engine
        stays reusable. Turn failure and engine sickness are separate
        predicates: a refusal is a healthy engine enforcing policy, and the
        affirmative marking already keeps truncated/mid-flight/transport
        failures out of the reusable path (they never produce a clean
        stopReason). Recorded dissent + revisit tripwires: spec D3b rationale.
        If you believe this test is wrong, read that dissent before changing
        the behavior."""
        engine = make_engine(retire=False)
        result, _ = run_turn(engine, [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "failed"}},
        ])
        self.assertFalse(result.ok)          # the TURN failed...
        self.assertTrue(engine.is_healthy()) # ...the ENGINE is fine, by design

    def test_error_prompt_response_is_unclean(self) -> None:
        # Build pin 2: a JSON-RPC-error prompt response leaves the engine unhealthy.
        engine = make_engine()
        result, _ = run_turn(engine, [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "boom"}},
        ])
        self.assertFalse(result.ok)
        self.assertFalse(engine.is_healthy())

    def test_raised_engine_error_leaves_unhealthy(self) -> None:
        # V7(c2): a non-dict result raises EngineError on a LIVE child; the
        # affirmative marking (False at prompt start) must survive the raise.
        from agent_redis_bridge.engines.base import EngineError
        engine = make_engine()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": "not-a-dict"})
        with self.assertRaises(EngineError):
            engine.run_turn_with_progress("task", timeout=2, policy="trusted", on_event=None)
        self.assertFalse(engine.is_healthy())

    def test_dead_child_mid_turn_returns_promptly_and_unhealthy(self) -> None:
        # V7(c): no message ever arrives and the child is dead -> exit, not timeout-spin.
        engine = make_engine()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.process.exit_code = 1  # dead before the prompt resolves
        result, _ = run_turn(engine, [], timeout=30)
        self.assertFalse(result.ok)
        self.assertIn("exited", result.error)
        self.assertFalse(engine.is_healthy())

    def test_reader_death_marks_unhealthy(self) -> None:
        engine = make_engine()

        class ExplodingStdout:
            def __iter__(self):
                raise RuntimeError("pipe torn")

        engine.process.stdout = ExplodingStdout()
        engine._read_stdout()
        self.assertFalse(engine.healthy)

    def test_external_interrupt_makes_turn_unclean(self) -> None:
        engine = make_engine()
        for message in [{"jsonrpc": "2.0", "id": 1, "result": {}}]:
            engine.messages.put(message)
        # interrupt() called mid-turn (e.g. bridge cancel); response still arrives.
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "cancelled"}})
        engine.session_id = "sess-1"

        def on_event(name, data):
            if name == "turn_started":
                engine.interrupt()

        result = engine.run_turn_with_progress("task", timeout=2, policy="trusted", on_event=on_event)
        self.assertFalse(result.ok)
        self.assertFalse(engine.is_healthy())


class PoolQuarantineTest(unittest.TestCase):
    """V7 pool arms MUST pin retire_after_turn=False: otherwise release()'s
    retire branch stops the engine without consulting is_healthy() and the
    assertion passes vacuously (cold-Opus r3)."""

    def _pooled_engine(self) -> tuple:
        from agent_redis_bridge.engine_pool import EnginePool
        engine = make_engine(retire=False)
        engine.reader_thread = None  # no real reader in fixtures
        # acquire() start()s factory-fresh engines (engine_pool.py:88-93); this
        # fixture engine is pre-wired by make_engine, so starting is a no-op.
        engine.start = lambda: None
        pool = EnginePool(lambda: engine, max_size=1)
        acquired = pool.acquire("task-1")
        self.assertIs(acquired, engine)
        return pool, engine

    def test_unhealthy_engine_is_stopped_not_reidled(self) -> None:
        pool, engine = self._pooled_engine()
        engine.healthy = False  # as left by grace expiry / dead child / raise path
        self.assertFalse(engine.is_healthy())
        pool.release("task-1")
        self.assertNotIn(engine, pool._idle)          # never re-idled
        self.assertTrue(engine.process.terminated)     # stop() was called

    def test_healthy_engine_is_reidled_no_over_revocation(self) -> None:
        pool, engine = self._pooled_engine()
        engine.healthy = True  # as left by a clean end_turn
        self.assertTrue(engine.is_healthy())
        pool.release("task-1")
        self.assertIn(engine, pool._idle)
        self.assertFalse(engine.process.terminated)


if __name__ == "__main__":
    unittest.main()

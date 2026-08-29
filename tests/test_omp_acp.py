"""omp-acp engine — oh-my-pi over ACP.

Pins the four ways omp diverges from the gemini-acp base, each of which was a
live failure before it was handled: the `acp` subcommand shape, the suppressed
`session/set_model` (omp answers "Unknown ACP ext method" and the base would
fail start()), the `default`/`plan` session modes (omp has no `yolo`), and the
`--tools` allowlist that lets readonly_gate certify an omp seat.
"""

import json
import queue
import subprocess
import unittest

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.omp_acp import OmpAcpEngine

from test_gemini_acp import FakeProcess


def ok_preflight(calls: list | None = None):
    """A stub for omp's `--version` flag check that always passes."""

    def _run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="17.2.4\n", stderr="")

    return _run


class OmpAcpCommandShapeTest(unittest.TestCase):
    def test_bare_command_is_omp_acp(self) -> None:
        engine = OmpAcpEngine(cwd="/tmp/project", model=None)
        self.assertEqual(engine.command_args(), ["omp", "acp"])

    def test_command_override_keeps_acp_subcommand(self) -> None:
        engine = OmpAcpEngine(cwd="/tmp/project", model=None, command="/opt/homebrew/bin/omp")
        self.assertEqual(engine.command_args(), ["/opt/homebrew/bin/omp", "acp"])

    def test_flags_precede_the_subcommand(self) -> None:
        # `omp acp --flags` also parses, but only the flags-first form was
        # behaviourally verified, so the shape is pinned.
        engine = OmpAcpEngine(
            cwd="/tmp/project",
            model="openrouter/anthropic/claude-haiku-latest",
            pi_tools="read,grep",
            append_system_prompt="You are a reviewer.",
        )
        args = engine.command_args()
        self.assertEqual(args[-1], "acp")
        self.assertEqual(
            args,
            [
                "omp",
                "--tools",
                "read,grep",
                "--append-system-prompt",
                "You are a reviewer.",
                "--model",
                "openrouter/anthropic/claude-haiku-latest",
                "acp",
            ],
        )

    def test_tools_are_normalized_not_passed_through(self) -> None:
        engine = OmpAcpEngine(cwd="/tmp/project", model=None, pi_tools=" read , grep ")
        self.assertIn("read,grep", engine.command_args())

    def test_absent_tools_emit_no_flag(self) -> None:
        # No allowlist => omp's own default surface; the bridge must not invent
        # an empty `--tools` (which omp would read as "no tools at all").
        engine = OmpAcpEngine(cwd="/tmp/project", model=None, pi_tools=None)
        self.assertNotIn("--tools", engine.command_args())


class OmpAcpDegenerateToolsGuardTest(unittest.TestCase):
    """A non-empty allowlist that parses to nothing must refuse to construct.

    Silently falling back would spawn omp with all 29 built-ins, including
    browser/computer/github — a far wider surface than the pi engines' fallback.
    """

    def test_degenerate_comma_refuses(self) -> None:
        with self.assertRaises(EngineError) as ctx:
            OmpAcpEngine(cwd="/tmp/project", model=None, pi_tools=",")
        self.assertIn("29", str(ctx.exception))

    def test_whitespace_only_refuses(self) -> None:
        with self.assertRaises(EngineError):
            OmpAcpEngine(cwd="/tmp/project", model=None, pi_tools="   ")

    def test_none_is_allowed(self) -> None:
        # Unset is a legitimate "use omp's defaults"; only a *typo* is refused.
        OmpAcpEngine(cwd="/tmp/project", model=None, pi_tools=None)


class OmpAcpSessionTest(unittest.TestCase):
    def test_start_does_not_send_set_model(self) -> None:
        # The regression this guards: omp answers session/set_model with
        # "Unknown ACP ext method", which the base turns into an EngineError and
        # start() fails. The model is pinned via --model instead.
        fake = FakeProcess(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "session-omp"}},
            ]
        )
        engine = OmpAcpEngine(
            cwd="/tmp/project",
            model="openrouter/anthropic/claude-haiku-latest",
            popen_factory=lambda *a, **k: fake,
            preflight_runner=ok_preflight(),
        )

        engine.start()

        self.assertEqual(engine.session_id, "session-omp")
        sent = [json.loads(line) for line in fake.stdin.lines]
        methods = [m["method"] for m in sent]
        self.assertEqual(methods, ["initialize", "session/new"])
        self.assertNotIn("session/set_model", methods)

    def test_model_survives_start_for_respawn(self) -> None:
        # start_session() suppresses self.model across the super() call; it must
        # be restored, or a pool respawn would drop the seat's model pin.
        fake = FakeProcess(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "session-omp"}},
            ]
        )
        engine = OmpAcpEngine(
            cwd="/tmp/project", model="glm-5.2", popen_factory=lambda *a, **k: fake,
            preflight_runner=ok_preflight(),
        )
        engine.start()
        self.assertEqual(engine.model, "glm-5.2")
        self.assertIn("--model", engine.command_args())

    def test_trusted_policy_sends_default_mode(self) -> None:
        fake = FakeProcess()
        engine = OmpAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: fake,
                              preflight_runner=ok_preflight())
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-omp"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        result = engine.run_turn_with_progress("Do a task", timeout=1, policy="trusted", on_event=None)

        self.assertTrue(result.ok)
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "session/set_mode")
        # NOT "yolo" — omp rejects it; its two modes are default and plan.
        self.assertEqual(sent[0]["params"]["modeId"], "default")
        self.assertEqual(sent[1]["method"], "session/prompt")

    def test_untrusted_policy_sends_plan_mode(self) -> None:
        fake = FakeProcess()
        engine = OmpAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: fake,
                              preflight_runner=ok_preflight())
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-omp"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        engine.run_turn_with_progress("Review this", timeout=1, policy="untrusted", on_event=None)

        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["params"]["modeId"], "plan")

    def test_refusal_stop_reason_fails_turn(self) -> None:
        fake = FakeProcess()
        engine = OmpAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: fake,
                              preflight_runner=ok_preflight())
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-omp"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "refusal"}})

        result = engine.run_turn_with_progress("Do a task", timeout=1, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertIn("stopReason=refusal", result.error or "")


class OmpAcpPreflightTest(unittest.TestCase):
    """omp's flag validator runs BEFORE the ACP handshake.

    Without it, a bad `--tools` name makes omp exit rc=2 instantly while the
    base's `initialize` waits out the full 60s timeout and reports a wedge —
    hiding omp's own explanation. The live trap: pi's canonical reviewer
    allowlist `read,grep,find,ls` is INVALID for omp (no `find`, no `ls`).
    """

    def _engine(self, runner, **kw):
        return OmpAcpEngine(
            cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: FakeProcess(),
            preflight_runner=runner, **kw
        )

    def test_preflight_swaps_acp_for_version(self) -> None:
        calls: list = []
        engine = self._engine(ok_preflight(calls), pi_tools="read,grep,glob")
        engine._preflight_spawn_flags()
        argv = calls[0][0]
        self.assertEqual(argv[-1], "--version")
        self.assertNotIn("acp", argv)
        # the flags actually being validated are the ones we will spawn with
        self.assertIn("--tools", argv)
        self.assertIn("read,grep,glob", argv)

    def test_rejected_flags_raise_with_omps_own_message(self) -> None:
        def bad_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 2,
                stdout="",
                stderr="Error: Unknown tool in --tools: ls. Valid tools: read, bash, edit, ...\n",
            )

        engine = self._engine(bad_run, pi_tools="read,grep,find,ls")
        with self.assertRaises(EngineError) as ctx:
            engine.start()
        msg = str(ctx.exception)
        self.assertIn("exit 2", msg)
        # the operator must see WHICH tool was wrong, not "initialize timed out"
        self.assertIn("Unknown tool in --tools: ls", msg)
        self.assertNotIn("timed out", msg)

    def test_missing_binary_names_the_install_route(self) -> None:
        def enoent(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        engine = self._engine(enoent)
        with self.assertRaises(EngineError) as ctx:
            engine.start()
        self.assertIn("brew install", str(ctx.exception))

    def test_preflight_timeout_refuses_rather_than_wedging(self) -> None:
        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

        engine = self._engine(hang)
        with self.assertRaises(EngineError) as ctx:
            engine.start()
        self.assertIn("--version", str(ctx.exception))

    def test_preflight_runs_before_any_spawn(self) -> None:
        # If the preflight fails, the ACP subprocess must never be spawned.
        spawned: list = []

        def bad_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="Error: nope\n")

        def popen(*a, **k):
            spawned.append(a)
            return FakeProcess()

        engine = OmpAcpEngine(
            cwd="/tmp/project", model=None, popen_factory=popen, preflight_runner=bad_run
        )
        with self.assertRaises(EngineError):
            engine.start()
        self.assertEqual(spawned, [])


class OmpAcpRoleProfileTest(unittest.TestCase):
    def test_engine_declares_it_consumes_the_role_profile(self) -> None:
        # True => Bridge.role_profile_for_turn returns None and does NOT also
        # prepend the profile to the task text (it goes via the spawn flag).
        self.assertTrue(OmpAcpEngine.consumes_role_profile)


class AcpPermissionPolicyTest(unittest.TestCase):
    """Permission asks are answered from the ACTIVE TURN's policy.

    The live defect this fixes: the gemini-acp base cancels every
    `session/request_permission`, so an omp dispatch reached the seat, ran, and
    came back `stopReason=cancelled` with no tool results.
    """

    def _engine(self):
        fake = FakeProcess()
        engine = OmpAcpEngine(
            cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: fake,
            preflight_runner=ok_preflight(),
        )
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-omp"
        return engine, fake

    @staticmethod
    def _ask(session_id="session-omp", request_id=99):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "options": [
                    {"optionId": "reject-once", "kind": "reject_once", "name": "No"},
                    {"optionId": "allow-once", "kind": "allow_once", "name": "Yes"},
                ],
            },
        }

    @staticmethod
    def _last_outcome(fake):
        sent = [json.loads(line) for line in fake.stdin.lines]
        return sent[-1]["result"]["outcome"]

    def test_trusted_turn_selects_the_allow_option(self) -> None:
        engine, fake = self._engine()
        engine._active_policy = "trusted"
        engine._respond_to_client_request(self._ask())
        self.assertEqual(
            self._last_outcome(fake), {"outcome": "selected", "optionId": "allow-once"}
        )

    def test_untrusted_turn_denies(self) -> None:
        engine, fake = self._engine()
        engine._active_policy = "untrusted"
        engine._respond_to_client_request(self._ask())
        self.assertEqual(self._last_outcome(fake), {"outcome": "cancelled"})

    def test_outside_a_turn_denies(self) -> None:
        # The handshake request() paths share this responder; with no active
        # turn there is no authority to grant, so it must deny.
        engine, fake = self._engine()
        self.assertIsNone(engine._active_policy)
        engine._respond_to_client_request(self._ask())
        self.assertEqual(self._last_outcome(fake), {"outcome": "cancelled"})

    def test_stale_session_ask_denied_even_when_trusted(self) -> None:
        engine, fake = self._engine()
        engine._active_policy = "trusted"
        engine._respond_to_client_request(self._ask(session_id="some-other-session"))
        self.assertEqual(self._last_outcome(fake), {"outcome": "cancelled"})

    def test_trusted_ask_with_no_allow_option_denied(self) -> None:
        engine, fake = self._engine()
        engine._active_policy = "trusted"
        ask = self._ask()
        ask["params"]["options"] = [{"optionId": "reject-once", "kind": "reject_once"}]
        engine._respond_to_client_request(ask)
        self.assertEqual(self._last_outcome(fake), {"outcome": "cancelled"})

    def test_policy_is_cleared_after_the_turn(self) -> None:
        # If it leaked, a later stray ask would be authorized by a dead turn.
        engine, fake = self._engine()
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        engine.run_turn_with_progress("t", timeout=1, policy="trusted", on_event=None)
        self.assertIsNone(engine._active_policy)

    def test_ask_left_over_from_a_previous_turn_cannot_ride_the_next_trusted_turn(self) -> None:
        """A permission ask that ARRIVES in turn A must not be GRANTED in turn B.

        The mixin authorizes at dequeue time from whatever policy is active then.
        An ask the agent raised during an untrusted turn, but which was still
        queued when that turn returned, was therefore answered under the NEXT
        turn's policy — so a write refused under `plan` mode got approved by an
        unrelated later dispatch.

        grok-acp, whose reviewed floor this mixin is derived from, is not exposed:
        it retires after every turn, and a non-retiring grok seat rotates
        `session/new` per dispatch so the D3b session gate catches the stale ask
        (`grok_acp._rotate_session_if_reused`). The gemini-acp family does neither
        — `session/new` is issued once in `start()` — so the mixin inherited D3b's
        wording without the invariant that makes it bite across turns.

        Panel finding, run panel-omp-opencode-arc-20260803T125825Z-570c21.
        """
        engine, fake = self._engine()
        engine.messages = queue.Queue()

        # --- turn A: untrusted. The ask arrives but is never dequeued, because
        # the prompt response ends the turn first.
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})           # set_mode
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        engine.run_turn_with_progress("a", timeout=1, policy="untrusted", on_event=None)
        engine.messages.put(self._ask(request_id=99))  # left over, unanswered

        # --- turn B: trusted, same session (the family never re-keys it).
        engine.messages.put({"jsonrpc": "2.0", "id": 3, "result": {}})           # set_mode
        engine.messages.put({"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "end_turn"}})
        engine.run_turn_with_progress("b", timeout=1, policy="trusted", on_event=None)

        answers = [
            json.loads(line)
            for line in fake.stdin.lines
            if json.loads(line).get("id") == 99 and "result" in json.loads(line)
        ]
        self.assertTrue(answers, "the stale ask was never answered at all")
        self.assertEqual(
            answers[-1]["result"]["outcome"]["outcome"],
            "cancelled",
            "an ask raised during an UNTRUSTED turn was granted under a later trusted turn",
        )


    def test_stale_drain_preserves_non_permission_messages_in_order(self) -> None:
        # The stale-ask drain must not eat unrelated traffic: it pulls the whole
        # queue to inspect it, so a bug there would silently swallow a pending
        # prompt response (e.g. one that arrived after a turn timed out).
        engine, _fake = self._engine()
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 7, "result": {"first": True}})
        engine.messages.put(self._ask(request_id=99))
        engine.messages.put({"jsonrpc": "2.0", "id": 8, "result": {"second": True}})

        engine._cancel_stale_permission_asks()

        survivors = []
        while not engine.messages.empty():
            survivors.append(engine.messages.get_nowait())
        self.assertEqual([m["id"] for m in survivors], [7, 8])
        self.assertEqual(survivors[0]["result"], {"first": True})



# Keep this LAST. It previously sat above AcpPermissionPolicyTest, so a direct
# `python tests/test_omp_acp.py` ran unittest.main() before the permission-policy
# class was defined: 19 tests ran instead of 25, silently dropping all six —
# including test_outside_a_turn_denies and
# test_stale_session_ask_denied_even_when_trusted, the two that pin the
# fail-closed permission floor. pytest collected them, so CI never noticed.
# Panel finding, run panel-omp-opencode-arc-20260803T125825Z-570c21.
if __name__ == "__main__":
    unittest.main()

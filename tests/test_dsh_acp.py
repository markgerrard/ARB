"""dsh-acp engine — DeepSeek Harness over its ACP transport.

Pins dsh's divergences from the gemini-acp base, each established by probing the
real runtime on 2026-08-17 or by a reviewing seat citing the harness source
(design note: `docs/superpowers/specs/2026-08-17-dsh-acp-engine-design.md`):

- the `node <bin> --config <cordis>` command shape;
- `session/set_model` is `-32601`, so the base's model call must NOT be sent;
- `session/set_mode` is `-32601` too, so the per-turn mode call must NOT be
  sent — left inherited it kills EVERY turn before the model is reached;
- `mcpServers` non-empty is `invalidParams` upstream, so it must be refused
  here with a named cause rather than forwarded.

The "must not send" tests are the load-bearing ones: delete either override in
the engine and they fail. A test that only asserted a turn succeeds would stay
green against a fake that answers anything.

The construction guards exist because each corresponds to a failure that is
SILENT or opaque in production: an unset persistence root bounces every turn as
dirty_after_commit, an unresolvable composition dies as a bare handshake
timeout, and a model mismatch serves a different model than the dispatch
recorded. All were filed by the review panel
(panel-dsh-acp-20260817T2200Z-e4cc59).
"""

import json
import os
import queue
import tempfile
import unittest
from unittest import mock

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.dsh_acp import PROBE_PACKAGE, DshAcpEngine

from test_gemini_acp import FakeProcess


class DshLayout:
    """A minimal on-disk layout satisfying the engine's construction guards.

    `node_modules` carries the probe PACKAGE, not just the directory: the guard
    checks for a resolvable plugin, so a fixture that created a bare directory
    would no longer represent a valid layout.
    """

    def __init__(
        self,
        tmp: str,
        *,
        with_node_modules: bool = True,
        with_probe_package: bool = True,
    ) -> None:
        self.root = tmp
        self.bin = os.path.join(tmp, "bin.js")
        self.cordis_dir = os.path.join(tmp, "configs", "dsh")
        os.makedirs(self.cordis_dir, exist_ok=True)
        self.cordis = os.path.join(self.cordis_dir, "acp-agent.cordis.yml")
        self.sessions = os.path.join(tmp, "sessions")
        os.makedirs(self.sessions, exist_ok=True)
        with open(self.bin, "w") as fh:
            fh.write("// fake runtime\n")
        with open(self.cordis, "w") as fh:
            fh.write("- id: acp-agent\n")
        if with_node_modules:
            nm = os.path.join(self.cordis_dir, "node_modules")
            os.makedirs(nm, exist_ok=True)
            if with_probe_package:
                os.makedirs(
                    os.path.join(nm, "@deepseek-ai", PROBE_PACKAGE), exist_ok=True
                )

    def env(self, **extra: str) -> dict[str, str]:
        base = {
            "DSH_ACP_BIN": self.bin,
            "DSH_ACP_CORDIS": self.cordis,
            "DSH_SESSION_ROOT": self.sessions,
            "PATH": os.environ.get("PATH", ""),
        }
        base.update(extra)
        return base


def _cwd() -> str:
    """The session cwd the guards require: the daemon's own cwd.

    dsh roots its sandbox at the runtime process's cwd while the session cwd
    arrives separately, so the engine refuses a seat where the two differ.
    """
    return os.getcwd()


class DshAcpCommandShapeTest(unittest.TestCase):
    def test_command_args_is_node_bin_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            with mock.patch.dict(os.environ, layout.env(), clear=True):
                engine = DshAcpEngine(cwd=_cwd(), model=None)
            self.assertEqual(
                engine.command_args(),
                ["node", layout.bin, "--config", layout.cordis],
            )

    def test_runtime_node_override_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            env = layout.env(DSH_RUNTIME_NODE="/opt/homebrew/bin/node")
            with mock.patch.dict(os.environ, env, clear=True):
                engine = DshAcpEngine(cwd=_cwd(), model=None)
            self.assertEqual(engine.command_args()[0], "/opt/homebrew/bin/node")


class DshAcpConstructionGuardTest(unittest.TestCase):
    def test_missing_bin_env_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            env = layout.env()
            env.pop("DSH_ACP_BIN")
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(EngineError) as ctx:
                    DshAcpEngine(cwd=_cwd(), model=None)
            self.assertIn("DSH_ACP_BIN", str(ctx.exception))

    def test_missing_cordis_env_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            env = layout.env()
            env.pop("DSH_ACP_CORDIS")
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(EngineError) as ctx:
                    DshAcpEngine(cwd=_cwd(), model=None)
            self.assertIn("DSH_ACP_CORDIS", str(ctx.exception))

    def test_nonexistent_bin_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            env = layout.env(DSH_ACP_BIN=os.path.join(tmp, "nope.js"))
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(EngineError) as ctx:
                    DshAcpEngine(cwd=_cwd(), model=None)
            self.assertIn("does not exist", str(ctx.exception))


class DshAcpPluginResolutionTest(unittest.TestCase):
    def test_no_node_modules_refuses_with_the_correct_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp, with_node_modules=False)
            with mock.patch.dict(os.environ, layout.env(), clear=True):
                with self.assertRaises(EngineError) as ctx:
                    DshAcpEngine(cwd=_cwd(), model=None)
            message = str(ctx.exception)
            self.assertIn("node_modules", message)
            # The instruction must name the workspace package that DECLARES the
            # plugins. Naming the harness root sends the operator to a directory
            # that satisfies a weaker check and still boot-fails.
            self.assertIn("examples/node_modules", message)

    def test_empty_node_modules_is_refused_not_accepted(self) -> None:
        # REGRESSION. The first version of this guard checked only that a
        # directory named node_modules existed, so an empty one — or the wrong
        # module tree — passed the preflight and regressed to the opaque
        # handshake timeout the guard exists to prevent. Filed by all three
        # reviewing seats.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp, with_probe_package=False)
            with mock.patch.dict(os.environ, layout.env(), clear=True):
                with self.assertRaises(EngineError) as ctx:
                    DshAcpEngine(cwd=_cwd(), model=None)
            self.assertIn(PROBE_PACKAGE, str(ctx.exception))

    def test_probe_package_in_an_ancestor_directory_is_accepted(self) -> None:
        # Node's resolution walks upward, mirroring the harness's own layout
        # where the composition sits below examples/node_modules.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp, with_node_modules=False)
            os.makedirs(
                os.path.join(tmp, "node_modules", "@deepseek-ai", PROBE_PACKAGE),
                exist_ok=True,
            )
            with mock.patch.dict(os.environ, layout.env(), clear=True):
                engine = DshAcpEngine(cwd=_cwd(), model=None)
            self.assertIsNotNone(engine)


class DshAcpPersistenceGuardTest(unittest.TestCase):
    def test_unset_persistence_root_refuses(self) -> None:
        # Unset, the composition falls back to './.sessions' INSIDE the working
        # tree and the completion gate bounces every turn dirty_after_commit --
        # a turn that fully succeeded reports ok=false. Observed live.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            env = layout.env()
            env.pop("DSH_SESSION_ROOT")
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(EngineError) as ctx:
                    DshAcpEngine(cwd=_cwd(), model=None)
            self.assertIn("dirty_after_commit", str(ctx.exception))

    def test_persistence_root_inside_the_workdir_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            inside = os.path.join(_cwd(), ".sessions")
            env = layout.env(DSH_SESSION_ROOT=inside)
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(EngineError) as ctx:
                    DshAcpEngine(cwd=_cwd(), model=None)
            self.assertIn("inside the", str(ctx.exception))

    def test_snapshot_sessions_root_also_satisfies_the_guard(self) -> None:
        # The ACP composition reads DSH_SNAPSHOT_SESSIONS_ROOT; the jsonrpc one
        # reads DSH_SESSION_ROOT. Either must satisfy the check, or the guard
        # would reject a correctly-configured ACP seat.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            env = layout.env()
            env.pop("DSH_SESSION_ROOT")
            env["DSH_SNAPSHOT_SESSIONS_ROOT"] = layout.sessions
            with mock.patch.dict(os.environ, env, clear=True):
                engine = DshAcpEngine(cwd=_cwd(), model=None)
            self.assertIsNotNone(engine)


class DshAcpSandboxRootTest(unittest.TestCase):
    def test_session_cwd_differing_from_process_cwd_refuses(self) -> None:
        # dsh roots its sandbox at the runtime process's cwd, while the session
        # cwd arrives separately in session/new. They are independent knobs that
        # merely coincided during the live gate.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            with mock.patch.dict(os.environ, layout.env(), clear=True):
                with self.assertRaises(EngineError) as ctx:
                    DshAcpEngine(cwd=os.path.join(tmp, "elsewhere"), model=None)
            self.assertIn("sandbox", str(ctx.exception))


class DshAcpModelPinTest(unittest.TestCase):
    def test_model_disagreeing_with_seat_env_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            env = layout.env(DSH_ACP_MODEL="deepseek-v4-flash")
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(EngineError) as ctx:
                    DshAcpEngine(cwd=_cwd(), model="deepseek-v4-pro")
            message = str(ctx.exception)
            self.assertIn("session/set_model", message)
            self.assertIn("deepseek-v4-flash", message)

    def test_empty_model_env_refuses(self) -> None:
        # Python's falsy `or` and JavaScript's nullish `??` differ on exactly
        # this input: the guard would substitute the default and report an
        # agreement it has not established, while the composition boots the
        # runtime with an empty model.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            env = layout.env(DSH_ACP_MODEL="")
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(EngineError) as ctx:
                    DshAcpEngine(cwd=_cwd(), model="deepseek-v4-pro")
            self.assertIn("empty", str(ctx.exception))

    def test_model_agreeing_with_seat_env_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            env = layout.env(DSH_ACP_MODEL="deepseek-v4-flash")
            with mock.patch.dict(os.environ, env, clear=True):
                engine = DshAcpEngine(cwd=_cwd(), model="deepseek-v4-flash")
            self.assertEqual(engine.model, "deepseek-v4-flash")

    def test_model_matching_the_default_is_accepted_without_seat_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            with mock.patch.dict(os.environ, layout.env(), clear=True):
                engine = DshAcpEngine(cwd=_cwd(), model="deepseek-v4-pro")
            self.assertEqual(engine.model, "deepseek-v4-pro")


class DshAcpSessionTest(unittest.TestCase):
    def _engine(
        self, layout: DshLayout, fake: FakeProcess, model: str | None = None
    ) -> DshAcpEngine:
        with mock.patch.dict(os.environ, layout.env(), clear=True):
            return DshAcpEngine(
                cwd=_cwd(), model=model, popen_factory=lambda *a, **k: fake
            )

    def test_start_handshakes_and_creates_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            fake = FakeProcess(
                [
                    {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
                    {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "ses_dsh"}},
                ]
            )
            engine = self._engine(layout, fake)
            engine.start()

            self.assertEqual(engine.session_id, "ses_dsh")
            sent = [json.loads(line) for line in fake.stdin.lines]
            self.assertEqual(sent[0]["method"], "initialize")
            self.assertEqual(sent[1]["method"], "session/new")
            self.assertEqual(sent[1]["params"]["cwd"], _cwd())
            self.assertEqual(sent[1]["params"]["mcpServers"], [])

    def test_set_model_is_never_sent_even_with_a_model_pin(self) -> None:
        # LOAD-BEARING. dsh answers session/set_model with -32601; the base
        # sends it whenever a model is set. Delete the start_session override
        # and this fails.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            fake = FakeProcess(
                [
                    {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
                    {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "ses_dsh"}},
                ]
            )
            engine = self._engine(layout, fake, model="deepseek-v4-pro")
            engine.start()

            methods = [json.loads(line)["method"] for line in fake.stdin.lines]
            self.assertNotIn("session/set_model", methods)
            self.assertEqual(methods, ["initialize", "session/new"])

    def test_non_empty_mcp_servers_is_refused_with_a_named_cause(self) -> None:
        # dsh's ACP server throws invalidParams for a non-empty mcpServers list
        # (packages/acp/acp/src/index.ts:435). That is NOT -32601, so it is not
        # covered by the two method omissions; forwarding it blindly would make
        # every session on an MCP-configured host fail at session/new with an
        # upstream schema error rather than a named cause.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            fake = FakeProcess(
                [{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}]
            )
            engine = self._engine(layout, fake)
            with mock.patch(
                "agent_redis_bridge.local_memory_mcp.local_memory_mcp_servers",
                return_value=[{"name": "arb-memory", "url": "http://127.0.0.1:1"}],
            ):
                with self.assertRaises(EngineError) as ctx:
                    engine.start()
            self.assertIn("mcpServers", str(ctx.exception))


class DshAcpTurnTest(unittest.TestCase):
    def _prepared(self, layout: DshLayout, fake: FakeProcess) -> DshAcpEngine:
        with mock.patch.dict(os.environ, layout.env(), clear=True):
            engine = DshAcpEngine(
                cwd=_cwd(), model=None, popen_factory=lambda *a, **k: fake
            )
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "ses_dsh"
        engine.messages = queue.Queue()
        return engine

    def test_set_mode_is_never_sent_and_the_turn_still_runs(self) -> None:
        # LOAD-BEARING. The base sends session/set_mode before EVERY turn and
        # dsh answers -32601. Delete the no-op override and this fails.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            fake = FakeProcess()
            engine = self._prepared(layout, fake)
            engine.messages.put(
                {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}}
            )

            result = engine.run_turn_with_progress(
                "Do a task", timeout=1, policy="trusted", on_event=None
            )

            self.assertTrue(result.ok)
            methods = [json.loads(line)["method"] for line in fake.stdin.lines]
            self.assertNotIn("session/set_mode", methods)
            self.assertEqual(methods[0], "session/prompt")

    def test_untrusted_policy_also_sends_no_mode_frame(self) -> None:
        # Records the limitation rather than hiding it: trusted and untrusted
        # get the SAME posture, because posture is fixed at spawn by
        # DSH_PERMISSION_MODE. A dsh seat is not a trusted codex seat.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            fake = FakeProcess()
            engine = self._prepared(layout, fake)
            engine.messages.put(
                {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}}
            )

            engine.run_turn_with_progress(
                "Review this", timeout=1, policy="untrusted", on_event=None
            )

            methods = [json.loads(line)["method"] for line in fake.stdin.lines]
            self.assertNotIn("session/set_mode", methods)

    def test_refusal_stop_reason_fails_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            fake = FakeProcess()
            engine = self._prepared(layout, fake)
            engine.messages.put(
                {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "refusal"}}
            )

            result = engine.run_turn_with_progress(
                "Do a task", timeout=1, policy="trusted", on_event=None
            )

            self.assertFalse(result.ok)
            self.assertIn("stopReason=refusal", result.error or "")


class DshAcpCapabilityTest(unittest.TestCase):
    def test_thread_resume_is_not_declared(self) -> None:
        # session/load was never probed; declaring resume on an untested method
        # is exactly the unverified claim the design refuses to make.
        self.assertFalse(DshAcpEngine.supports_thread_resume)

    def test_engine_does_not_consume_the_role_profile(self) -> None:
        self.assertFalse(getattr(DshAcpEngine, "consumes_role_profile", False))

    def test_engine_is_classified_in_the_support_tier_table(self) -> None:
        # An unclassified adapter looks exactly like a live, certifying one.
        # All three reviewing seats filed this; two existing tests were red.
        from agent_redis_bridge.engines.support_tiers import EXPERIMENTAL, tier_for

        self.assertEqual(tier_for("dsh-acp"), EXPERIMENTAL)


if __name__ == "__main__":
    unittest.main()

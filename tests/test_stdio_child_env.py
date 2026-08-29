"""The shared child-env scrub for ACP engine subprocesses.

Panel finding (panel-devin-acp-20260718T111820Z-ac7270): every ACP adapter
spawned its child with the bridge daemon's complete environment — bus
credentials included. The scrub is shared-layer so cursor/grok/devin cannot
drift apart on it.
"""

import os
import unittest
from unittest import mock

from agent_redis_bridge.engines._stdio import scrubbed_child_env


class ScrubbedChildEnvTest(unittest.TestCase):
    def test_drops_bus_credentials_keeps_the_rest(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "HOME": "/Users/x",
            "DEVIN_API_KEY": "the-childs-own-auth-stays",
            "AGENT_REDIS_HOST": "bus.example",
            "AGENT_REDIS_PASSWORD": "hunter2",
            "REDISCLI_AUTH": "hunter2",
            "ARB_MEMORY_REDIS_URL": "rediss://:secret@bus:6379/9",
            "ARB_LIVE_REDIS_URL": "rediss://:secret@bus:6379/8",
            "ARB_BRIDGE_BUS_URL": "rediss://:secret@bus:6379/7",
            "ARB_GATE_READER_DSN": "postgresql://reader@db/arb_memory",
            "ARB_GATE_READER_ROLE": "arb_gate_reader",
            "ARB_GATE_LANE_WRITER_DSN": "postgresql://lw@db/arb_memory",
            "ARB_GATE_LANE_WRITER_ROLE": "arb_gate_lw_seat_a",
            "ARB_GATE_LANE_WRITER_CONSUMER_ID": "consumer-a",
            "ARB_GATE_LANE_WRITER_LANE": "gated",
            "ARB_MEMORY_LOCAL_DSN": "postgresql://local@db/arb_memory",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            scrubbed = scrubbed_child_env()
        self.assertEqual(
            set(scrubbed),
            {"PATH", "HOME", "DEVIN_API_KEY", "ARB_MEMORY_LOCAL_DSN"},
        )
        self.assertEqual(scrubbed["DEVIN_API_KEY"], "the-childs-own-auth-stays")
        self.assertEqual(scrubbed["ARB_MEMORY_LOCAL_DSN"], "postgresql://local@db/arb_memory")
        self.assertNotIn("ARB_GATE_READER_DSN", scrubbed)
        self.assertNotIn("ARB_GATE_READER_ROLE", scrubbed)
        self.assertNotIn("ARB_GATE_LANE_WRITER_DSN", scrubbed)
        self.assertNotIn("ARB_GATE_LANE_WRITER_ROLE", scrubbed)
        self.assertNotIn("ARB_MEMORY_REDIS_URL", scrubbed)


class AcpEnginesUseScrubTest(unittest.TestCase):
    """Each ACP adapter's start() must pass the scrubbed env to Popen."""

    def _capture_env(self, engine_cls, module_name: str, start_responses: list[dict]):
        import queue as _q

        captured: dict = {}

        class FakeStdin:
            def write(self, v):
                pass

            def flush(self):
                pass

        class FakeProcess:
            stdin = FakeStdin()
            stdout = iter([])
            stderr = iter([])

            def poll(self):
                return None

        def factory(*args, **kwargs):
            captured.update(kwargs)
            return FakeProcess()

        eng = engine_cls(cwd="/tmp/x", model=None, popen_factory=factory)
        eng.messages = _q.Queue()
        for r in start_responses:
            eng.messages.put(r)
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin", "AGENT_REDIS_PASSWORD": "s3cret"}, clear=True):
            with mock.patch(f"agent_redis_bridge.engines.{module_name}.start_stderr_drain", return_value=None):
                try:
                    eng.start()
                except Exception:
                    pass  # session bootstrap specifics don't matter; the spawn kwargs do
        return captured

    def test_cursor_start_scrubs_env(self) -> None:
        from agent_redis_bridge.engines.cursor_acp import CursorAcpEngine

        captured = self._capture_env(
            CursorAcpEngine,
            "_acp_base",
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
                # cursor sends authenticate between initialize and session/new —
                # starving it makes session/new burn its full 30s timeout
                # (caught by the devin author-seat review).
                {"jsonrpc": "2.0", "id": 2, "result": {}},
                {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "s"}},
            ],
        )
        self.assertIn("env", captured)
        self.assertNotIn("AGENT_REDIS_PASSWORD", captured["env"])

    def test_grok_start_scrubs_env(self) -> None:
        from agent_redis_bridge.engines.grok_acp import GrokAcpEngine

        captured = self._capture_env(
            GrokAcpEngine,
            "_acp_base",
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s"}},
            ],
        )
        self.assertIn("env", captured)
        self.assertNotIn("AGENT_REDIS_PASSWORD", captured["env"])

    def test_kimi_code_start_scrubs_env(self) -> None:
        """kimi-code inherits its spawn from the shared ACP base (AcpEngineBase,
        under GeminiAcpEngine) — the scrub must land there (the base is live
        via its subclasses even though the gemini engine itself is
        deprecated)."""
        from agent_redis_bridge.engines.kimi_code_acp import KimiCodeAcpEngine

        captured = self._capture_env(
            KimiCodeAcpEngine,
            "_acp_base",
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s"}},
            ],
        )
        self.assertIn("env", captured)
        self.assertNotIn("AGENT_REDIS_PASSWORD", captured["env"])

    def test_mini_agent_start_scrubs_env(self) -> None:
        from agent_redis_bridge.engines.mini_agent_acp import MiniAgentAcpEngine

        captured = self._capture_env(
            MiniAgentAcpEngine,
            "_acp_base",
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s"}},
            ],
        )
        self.assertIn("env", captured)
        self.assertNotIn("AGENT_REDIS_PASSWORD", captured["env"])

    def test_devin_start_scrubs_env(self) -> None:
        from agent_redis_bridge.engines.devin_acp import DevinAcpEngine

        captured = self._capture_env(
            DevinAcpEngine,
            "_acp_base",
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s", "configOptions": []}},
            ],
        )
        self.assertIn("env", captured)
        self.assertNotIn("AGENT_REDIS_PASSWORD", captured["env"])


class WorkhorseEnginesUseScrubTest(unittest.TestCase):
    """The non-ACP spawns (pi-rpc, pi-sdk host, agy --print, codex app-server)
    must pass the scrubbed env too (k3 r2b residual: 'fleet-wide' was only the
    ACP family). Factories capture the spawn kwargs then raise — protocol
    bootstrap is irrelevant, only the spawn call is under test."""

    POLLUTED = {"PATH": "/usr/bin", "HOME": "/Users/x", "AGENT_REDIS_PASSWORD": "s3cret"}

    def _factory(self, captured: dict):
        def factory(*args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("captured-spawn")

        return factory

    def _assert_scrubbed(self, captured: dict) -> None:
        self.assertIn("env", captured)
        self.assertNotIn("AGENT_REDIS_PASSWORD", captured["env"])
        self.assertIn("PATH", captured["env"])

    def test_pi_rpc_spawn_scrubs_env(self) -> None:
        from agent_redis_bridge.engines.pi_rpc import PiRpcEngine

        captured: dict = {}
        eng = PiRpcEngine(cwd="/tmp/x", model=None, popen_factory=self._factory(captured))
        with mock.patch.dict(os.environ, self.POLLUTED, clear=True):
            try:
                eng.start()
            except Exception:
                pass
        self._assert_scrubbed(captured)

    def test_pi_sdk_spawn_scrubs_env(self) -> None:
        import tempfile

        from agent_redis_bridge.engines.pi_sdk import PiSdkEngine

        captured: dict = {}
        with tempfile.NamedTemporaryFile(suffix=".mjs") as host:
            eng = PiSdkEngine(
                cwd="/tmp/x",
                model="zai/glm-5.2",
                host_script_path=host.name,
                popen_factory=self._factory(captured),
            )
            with mock.patch.dict(os.environ, self.POLLUTED, clear=True):
                try:
                    eng.start()
                except Exception:
                    pass
        self._assert_scrubbed(captured)

    def test_agy_print_spawn_scrubs_env(self) -> None:
        import tempfile

        from agent_redis_bridge.engines.agy_print import AgyPrintEngine

        captured: dict = {}
        with tempfile.TemporaryDirectory() as conv_root:
            from pathlib import Path

            eng = AgyPrintEngine(
                cwd="/tmp/x",
                model=None,
                popen_factory=self._factory(captured),
                conversations_root=Path(conv_root),
            )
            with mock.patch.dict(os.environ, self.POLLUTED, clear=True):
                try:
                    eng.run_turn_with_progress("task", timeout=5, policy="human", on_event=None)
                except Exception:
                    pass
        self._assert_scrubbed(captured)

    def test_codex_spawn_scrubs_env(self) -> None:
        from agent_redis_bridge.engines.codex import CodexEngine

        captured: dict = {}

        def fake_popen(*args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("captured-spawn")

        eng = CodexEngine(cwd="/tmp/x", model=None, approval_policy="never", sandbox="read-only")
        with mock.patch.dict(os.environ, self.POLLUTED, clear=True):
            with mock.patch("agent_redis_bridge.engines.codex.subprocess.Popen", side_effect=fake_popen):
                try:
                    eng.start()
                except Exception:
                    pass
        self._assert_scrubbed(captured)


class BusCredentialPredicateTest(unittest.TestCase):
    """is_bus_credential is the single shared definition of 'bus credential';
    scrubbed_child_env (ACP engines) and the agent-sdk overlay blanking must
    both derive from it so the two mechanisms cannot drift apart.

    Defined before ``unittest.main()`` so ``python -m`` discovery runs these
    arms; a class after main is dead when the file is executed as a script.
    """

    def test_matches_every_bus_credential_shape(self) -> None:
        from agent_redis_bridge.engines._stdio import is_bus_credential

        for name in (
            "AGENT_REDIS_HOST",
            "AGENT_REDIS_PASSWORD",
            "REDISCLI_AUTH",
            "ARB_MEMORY_REDIS_URL",
            "ARB_LIVE_REDIS_URL",
            "ARB_BRIDGE_BUS_URL",
        ):
            self.assertTrue(is_bus_credential(name), name)

    def test_never_matches_child_auth_or_plumbing(self) -> None:
        from agent_redis_bridge.engines._stdio import is_bus_credential

        for name in (
            "PATH",
            "HOME",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
            "ANTHROPIC_API_KEY",
            "DEVIN_API_KEY",
        ):
            self.assertFalse(is_bus_credential(name), name)

    def test_gate_reader_credentials_are_scrubbed_separately(self) -> None:
        from agent_redis_bridge.engines._stdio import (
            is_bus_credential,
            is_gate_daemon_credential,
            is_gate_reader_credential,
        )

        for name in (
            "ARB_GATE_READER_DSN",
            "ARB_GATE_READER_ROLE",
            "ARB_GATE_LANE_WRITER_DSN",
            "ARB_GATE_LANE_WRITER_ROLE",
        ):
            self.assertTrue(is_gate_daemon_credential(name), name)
            self.assertTrue(is_gate_reader_credential(name), name)
            self.assertFalse(is_bus_credential(name), name)
        self.assertFalse(is_gate_daemon_credential("ARB_MEMORY_LOCAL_DSN"))


class RealSubprocessScrubProbeTest(unittest.TestCase):
    """Real child-process probes per Popen adapter family (Stage 1d-i).

    Each child prints presence booleans only — never secret values. Hydration
    DSN must be present; lane-writer DSN/role and harness-publish URL absent.
    """

    POLLUTED = {
        "PATH": "/usr/bin",
        "HOME": "/tmp",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://local-reader@db/arb_memory",
        "ARB_GATE_LANE_WRITER_DSN": "postgresql://lw-secret@db/arb_memory",
        "ARB_GATE_LANE_WRITER_ROLE": "arb_gate_lw_seat_a",
        "ARB_MEMORY_REDIS_URL": "rediss://:publish-secret@bus:6379/9",
        "AGENT_REDIS_PASSWORD": "bus-secret",
    }

    PROBE = (
        "import json,os;"
        "print(json.dumps({"
        "'local':bool(os.environ.get('ARB_MEMORY_LOCAL_DSN')),"
        "'lane_dsn':bool(os.environ.get('ARB_GATE_LANE_WRITER_DSN')),"
        "'lane_role':bool(os.environ.get('ARB_GATE_LANE_WRITER_ROLE')),"
        "'publish':bool(os.environ.get('ARB_MEMORY_REDIS_URL')),"
        "'bus_pw':bool(os.environ.get('AGENT_REDIS_PASSWORD')),"
        "}))"
    )

    def _assert_probe_output(self, stdout: str) -> None:
        import json

        data = json.loads(stdout.strip().splitlines()[-1])
        self.assertTrue(data["local"], "hydration DSN must be present")
        self.assertFalse(data["lane_dsn"], "lane-writer DSN must be absent")
        self.assertFalse(data["lane_role"], "lane-writer role must be absent")
        self.assertFalse(data["publish"], "harness publish URL must be absent")
        self.assertFalse(data["bus_pw"], "bus password must be absent")

    def _run_scrubbed_python(self) -> str:
        import subprocess
        import sys

        from agent_redis_bridge.engines._stdio import scrubbed_child_env

        with mock.patch.dict(os.environ, self.POLLUTED, clear=True):
            env = scrubbed_child_env()
        proc = subprocess.run(
            [sys.executable, "-c", self.PROBE],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_scrubbed_child_env_real_subprocess_probe(self) -> None:
        self._assert_probe_output(self._run_scrubbed_python())

    def test_prove_env_scrub_capability_returns_v2(self) -> None:
        from agent_redis_bridge.engines._stdio import (
            ENV_SCRUB_CAPABILITY,
            prove_env_scrub_capability,
        )

        with mock.patch.dict(os.environ, self.POLLUTED, clear=False):
            cap = prove_env_scrub_capability(dict(self.POLLUTED))
        self.assertEqual(cap, ENV_SCRUB_CAPABILITY)
        self.assertEqual(cap, "bus-and-gate-daemon-creds-v2")

    def test_prove_env_scrub_capability_default_path_mutates_no_global_environ(
        self,
    ) -> None:
        """Heartbeat path (env=None) must not clear/rebuild process-global os.environ.

        rem2 P1-B: _temporary_environ did os.environ.clear()+update on every
        reassert_liveness tick, racing concurrent engine spawns.
        """
        from agent_redis_bridge.engines._stdio import (
            ENV_SCRUB_CAPABILITY,
            prove_env_scrub_capability,
        )

        # The hazard is a TRANSIENT clear/rebuild DURING the call on the heartbeat
        # thread, not a net change after it — _temporary_environ restores on exit,
        # so any before/after comparison is green whether or not the swap happened
        # (that comparison was the vacuous original; it could not fail). Assert the
        # observable act instead: os.environ.clear() is never invoked on the
        # default (env=None) path. Reintroducing the swap (mutate_environ=True)
        # makes clear() fire and reds this test.
        marker_key = "ARB_TEST_ENV_SCRUB_MARKER"
        marker_val = "prove-must-not-clear-me"
        with mock.patch.dict(
            os.environ,
            {**self.POLLUTED, marker_key: marker_val},
            clear=False,
        ):
            before = dict(os.environ)
            with mock.patch.object(
                os.environ, "clear", side_effect=AssertionError(
                    "heartbeat prove path cleared process-global os.environ (P1-B race)"
                ),
            ):
                cap = prove_env_scrub_capability()  # env is None — heartbeat shape
            after = dict(os.environ)

        self.assertEqual(cap, ENV_SCRUB_CAPABILITY)
        self.assertEqual(before, after, "os.environ contents must be unchanged")
        self.assertEqual(after.get(marker_key), marker_val)


class SelectableFamilyRealChildEnvTest(unittest.TestCase):
    """Real child-env probes for the three families that escaped 1d-i's suite.

    Each probe launches (or faithfully constructs the env for) a real child
    through the family's production spawn path and asserts lane-writer + bus
    credentials are ABSENT. Presence booleans only — never secret values.
    These must go RED against unscrubbed agy-tmux / scored process_env paths.
    """

    POLLUTED = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://local-reader@db/arb_memory",
        "ARB_GATE_LANE_WRITER_DSN": "postgresql://lw-secret@db/arb_memory",
        "ARB_GATE_LANE_WRITER_ROLE": "arb_gate_lw_seat_a",
        "ARB_GATE_LANE_WRITER_CONSUMER_ID": "consumer-a",
        "ARB_GATE_LANE_WRITER_LANE": "gated",
        "ARB_MEMORY_REDIS_URL": "rediss://:publish-secret@bus:6379/9",
        "AGENT_REDIS_PASSWORD": "bus-secret",
        "OPENAI_API_KEY": "provider-key-stays",
    }

    PROBE = (
        "import json,os;"
        "print(json.dumps({"
        "'local':bool(os.environ.get('ARB_MEMORY_LOCAL_DSN')),"
        "'lane_dsn':bool(os.environ.get('ARB_GATE_LANE_WRITER_DSN')),"
        "'lane_role':bool(os.environ.get('ARB_GATE_LANE_WRITER_ROLE')),"
        "'lane_consumer':bool(os.environ.get('ARB_GATE_LANE_WRITER_CONSUMER_ID')),"
        "'lane':bool(os.environ.get('ARB_GATE_LANE_WRITER_LANE')),"
        "'publish':bool(os.environ.get('ARB_MEMORY_REDIS_URL')),"
        "'bus_pw':bool(os.environ.get('AGENT_REDIS_PASSWORD')),"
        "'provider':bool(os.environ.get('OPENAI_API_KEY')),"
        "}))"
    )

    def _assert_contained(self, data: dict) -> None:
        self.assertTrue(data.get("local"), "hydration DSN must be present")
        self.assertFalse(data.get("lane_dsn"), "lane-writer DSN must be absent")
        self.assertFalse(data.get("lane_role"), "lane-writer role must be absent")
        self.assertFalse(data.get("lane_consumer"), "lane-writer consumer must be absent")
        self.assertFalse(data.get("lane"), "lane-writer lane must be absent")
        self.assertFalse(data.get("publish"), "harness publish URL must be absent")
        self.assertFalse(data.get("bus_pw"), "bus password must be absent")

    def _probe_with_env(self, env: dict[str, str] | None) -> dict:
        import json
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-c", self.PROBE],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_agy_tmux_real_child_env_contains_no_lane_writer_or_bus(self) -> None:
        """Production AgyTmuxEngine._run must supply a scrubbed env to its child.

        A run_command that omits env= inherits the daemon env — the P1 leak.
        The test uses the env the engine actually passed (or inherits if none).
        """
        import subprocess
        import sys
        from types import SimpleNamespace

        from agent_redis_bridge.engines.agy_tmux import AgyTmuxEngine

        probe_data: dict = {}

        def run_command(args, **kwargs):
            # Faithful to subprocess.run: missing env → inherit current process.
            child_env = kwargs["env"] if "env" in kwargs else None
            proc = subprocess.run(
                [sys.executable, "-c", self.PROBE],
                capture_output=True,
                text=True,
                env=child_env,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                import json

                probe_data.update(json.loads(proc.stdout.strip().splitlines()[-1]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, self.POLLUTED, clear=True):
            eng = AgyTmuxEngine(
                cwd="/tmp",
                command="agy",
                tmux_command="tmux",
                run_command=run_command,
            )
            eng._run(["tmux", "new-session", "-d", "-s", "probe"], check=False)

        self.assertTrue(probe_data, "probe child must have run")
        self._assert_contained(probe_data)

    def test_scored_pi_sdk_process_env_real_child_contains_no_lane_writer_or_bus(
        self,
    ) -> None:
        """Scored provider_env={**os.environ, ...} must not reintroduce secrets.

        Exercises the production PiSdkEngine start path with a scored-style
        process_env merge (scored_plane.py:461 shape).
        """
        import json
        import subprocess
        import sys
        import tempfile

        from agent_redis_bridge.engines.pi_sdk import PiSdkEngine

        probe_data: dict = {}

        def factory(*args, **kwargs):
            child_env = kwargs["env"] if "env" in kwargs else None
            proc = subprocess.run(
                [sys.executable, "-c", self.PROBE],
                capture_output=True,
                text=True,
                env=child_env,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            probe_data.update(json.loads(proc.stdout.strip().splitlines()[-1]))
            raise RuntimeError("captured-spawn")

        with tempfile.NamedTemporaryFile(suffix=".mjs") as host:
            with mock.patch.dict(os.environ, self.POLLUTED, clear=True):
                # scored_plane merge shape — full daemon env plus provider keys.
                process_env = {**os.environ, "OPENAI_API_KEY": "provider-key-stays"}
                eng = PiSdkEngine(
                    cwd="/tmp",
                    model="zai/glm-5.2",
                    host_script_path=host.name,
                    popen_factory=factory,
                    process_env=process_env,
                )
                try:
                    eng.start()
                except RuntimeError as exc:
                    self.assertIn("captured-spawn", str(exc))

        self.assertTrue(probe_data, "probe child must have run")
        self._assert_contained(probe_data)
        # Provider keys must survive the scrub.
        self.assertTrue(probe_data.get("provider"), "provider API key must remain")

    def test_openinterpreter_process_env_real_child_contains_no_lane_writer_or_bus(
        self,
    ) -> None:
        """OpenInterpreter popen_kwargs env must be scrubbed when process_env set.

        Also covers the inherit case: when process_env is None the child must
        still not receive daemon credentials (always pass a scrubbed env).
        """
        import json
        import subprocess
        import sys

        from agent_redis_bridge.engines.openinterpreter import (
            CellToolPlaneBroker,
            OpenInterpreterEngine,
        )

        with mock.patch.dict(os.environ, self.POLLUTED, clear=True):
            process_env = {**os.environ, "OPENAI_API_KEY": "provider-key-stays"}
            eng = OpenInterpreterEngine(
                cwd="/tmp",
                provider="p",
                model="m",
                harness="zcode",
                tool_broker=CellToolPlaneBroker(handler=lambda _: {}),
                process_env=process_env,
            )
            kwargs = eng.popen_kwargs()
            self.assertIn("env", kwargs, "openinterpreter must set env= (not inherit)")
            data = self._probe_with_env(kwargs["env"])

        self._assert_contained(data)
        self.assertTrue(data.get("provider"), "provider API key must remain")

    def test_openinterpreter_none_process_env_does_not_inherit_daemon_creds(
        self,
    ) -> None:
        from agent_redis_bridge.engines.openinterpreter import (
            CellToolPlaneBroker,
            OpenInterpreterEngine,
        )

        with mock.patch.dict(os.environ, self.POLLUTED, clear=True):
            eng = OpenInterpreterEngine(
                cwd="/tmp",
                provider="p",
                model="m",
                harness="zcode",
                tool_broker=CellToolPlaneBroker(handler=lambda _: {}),
                process_env=None,
            )
            kwargs = eng.popen_kwargs()
            self.assertIn("env", kwargs, "must not fall back to full inheritance")
            data = self._probe_with_env(kwargs["env"])

        self._assert_contained(data)

    def test_prove_env_scrub_capability_rejects_leaky_family_or_uses_real_child(
        self,
    ) -> None:
        """v2 advertisement must exercise the selected family's real child env.

        Predicate-only simulation is not enough: prove must fail for a family
        that does not contain, or succeed only after a real child probe.
        """
        from agent_redis_bridge.engines._stdio import (
            ENV_SCRUB_CAPABILITY,
            prove_env_scrub_capability,
        )

        with mock.patch.dict(os.environ, self.POLLUTED, clear=False):
            for family in ("agy-tmux", "pi-sdk", "openinterpreter", "codex"):
                with self.subTest(family=family):
                    cap = prove_env_scrub_capability(
                        dict(self.POLLUTED), engine_name=family
                    )
                    self.assertEqual(cap, ENV_SCRUB_CAPABILITY)


if __name__ == "__main__":
    unittest.main()

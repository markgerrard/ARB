from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from agent_redis_bridge.bridge import Bridge, build_parser
from agent_redis_bridge.redis_io import IdentityOwnedError


class FakeRedis:
    def __init__(self) -> None:
        self.registry: dict[str, dict[str, str]] = {}
        self.status: dict[str, tuple[str, int]] = {}

    def register(
        self,
        *,
        agent_id: str,
        tool: str,
        project: str,
        workspace: str,
        branch: str,
        path: str,
        registered_at: str,
        pid: int,
        owner_token: str,
        ttl: int,
        env_scrub: str = "",
        worker_vantage: str = "",
        task_wire: str = "",
        brief_hydrate: str = "",
        registration_generation: str | None = None,
        readonly_tools: str = "",
    ) -> None:
        # Mirror RedisCli.register kwargs so future field additions fail loudly
        # here rather than only at the **kwargs subclass (ConflictOnceRedis).
        self.registry[agent_id] = {
            "tool": tool,
            "project": project,
            "workspace": workspace,
            "current_branch": branch,
            "path": path,
            "registered_at": registered_at,
            "pid": str(pid),
            "owner_token": owner_token,
            "env_scrub": env_scrub,
            "worker_vantage": worker_vantage,
            "task_wire": task_wire,
            "brief_hydrate": brief_hydrate,
            "registration_generation": registration_generation or owner_token,
            "readonly_tools": readonly_tools,
        }
        self.status[agent_id] = (f"alive:{owner_token}", ttl)

    def heartbeat(self, agent_id: str, owner_token: str, ttl: int) -> None:
        self.status[agent_id] = (f"alive:{owner_token}", ttl)

    def consumer_heartbeat(self, agent_id: str, owner_token: str, ttl: int) -> None:
        pass


class OneBeatStopEvent:
    def __init__(self) -> None:
        self.waits = 0

    def wait(self, timeout: float) -> bool:
        self.waits += 1
        return self.waits > 1


class ConflictOnceRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.register_calls = 0

    def register(self, **kwargs) -> None:
        self.register_calls += 1
        if self.register_calls == 1:
            raise IdentityOwnedError(kwargs["agent_id"], "alive:old-boot", 0)
        super().register(**kwargs)


class BridgeIdentityTest(unittest.TestCase):
    def test_fake_redis_register_signature_tracks_redis_cli(self) -> None:
        """Strict FakeRedis must accept every RedisCli.register keyword.

        ConflictOnceRedis(**kwargs) cannot catch drift; this pin fails when the
        real register grows a field that the primary double still omits.
        """
        import inspect

        from agent_redis_bridge.redis_io import RedisCli

        real = inspect.signature(RedisCli.register)
        fake = inspect.signature(FakeRedis.register)
        real_params = {
            name
            for name, p in real.parameters.items()
            if name != "self" and p.kind == inspect.Parameter.KEYWORD_ONLY
        }
        fake_params = {
            name
            for name, p in fake.parameters.items()
            if name != "self" and p.kind == inspect.Parameter.KEYWORD_ONLY
        }
        self.assertEqual(fake_params, real_params)

    def test_agent_id_defaults_to_three_segments_without_role(self) -> None:
        bridge = make_bridge()

        self.assertEqual(bridge.agent_id, "codex-project-c-dev")

    def test_agent_id_uses_four_segments_with_role(self) -> None:
        bridge = make_bridge("--role", "impl")

        self.assertEqual(bridge.agent_id, "codex-project-c-dev-impl")

    def test_empty_role_keeps_three_segment_agent_id(self) -> None:
        bridge = make_bridge("--role", "")

        self.assertEqual(bridge.agent_id, "codex-project-c-dev")

    def test_role_rejects_invalid_characters(self) -> None:
        with self.assertRaisesRegex(ValueError, "--role"):
            make_bridge("--role", "Impl")

    def test_usage_key_defaults_to_agent_id(self) -> None:
        bridge = make_bridge("--role", "impl")

        self.assertEqual(
            bridge.redis_config.usage_key(bridge.usage_identity, "20260426", "requests"),
            "agent_scratch:usage:codex-project-c-dev-impl:20260426:requests",
        )

    def test_usage_key_uses_scope_when_set(self) -> None:
        bridge = make_bridge("--role", "impl", "--usage-scope", "codex-project-c-dev-shared")

        self.assertEqual(
            bridge.redis_config.usage_key(bridge.usage_identity, "20260426", "turn_seconds"),
            "agent_scratch:usage:codex-project-c-dev-shared:20260426:turn_seconds",
        )

    def test_gemini_engine_uses_gemini_tool_prefix(self) -> None:
        bridge = make_bridge("--engine", "gemini-acp", "--role", "impl")

        self.assertEqual(bridge.agent_id, "gemini-project-c-dev-impl")

    def test_heartbeat_recreates_deleted_registry_with_stable_registered_at(self) -> None:
        bridge = make_bridge("--heartbeat-interval", "1", "--heartbeat-ttl", "60")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        bridge.register()
        registered_at = fake.registry[bridge.agent_id]["registered_at"]
        del fake.registry[bridge.agent_id]
        bridge.stop_event = OneBeatStopEvent()  # type: ignore[assignment]

        bridge.heartbeat_loop()

        self.assertEqual(fake.registry[bridge.agent_id]["registered_at"], registered_at)
        self.assertEqual(fake.registry[bridge.agent_id]["pid"], str(os.getpid()))
        self.assertEqual(fake.registry[bridge.agent_id]["owner_token"], bridge.owner_token)
        self.assertEqual(fake.status[bridge.agent_id], (f"alive:{bridge.owner_token}", 60))
        self.assertEqual(bridge.heartbeat_failures, 0)

    def test_startup_waits_in_process_for_stale_identity_lease(self) -> None:
        bridge = make_bridge("--identity-claim-timeout", "5")
        fake = ConflictOnceRedis()
        bridge.redis = fake  # type: ignore[assignment]
        bridge.stop_event.wait = mock.Mock(return_value=False)  # type: ignore[method-assign]

        bridge.register()

        self.assertEqual(fake.register_calls, 2)
        bridge.stop_event.wait.assert_called_once_with(1)  # type: ignore[attr-defined]
        self.assertEqual(fake.registry[bridge.agent_id]["owner_token"], bridge.owner_token)

    def test_completion_enforcement_defaults_on(self) -> None:
        self.assertTrue(make_bridge().enforce_completion)

    def test_ambient_environment_cannot_disable_completion(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_ENFORCE_COMPLETION": "0"}):
            self.assertTrue(make_bridge().enforce_completion)

    def test_durable_daemon_refuses_disabled_completion(self) -> None:
        bridge = make_bridge("--no-enforce-completion")

        with self.assertRaisesRegex(RuntimeError, "diagnostic one-shot"):
            bridge.run()

    def test_heartbeat_loop_stops_immediately_when_deposed(self) -> None:
        bridge = make_bridge()
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        def deposed_register(**kwargs) -> None:
            raise IdentityOwnedError(kwargs["agent_id"], "alive:successor", 42)

        fake.register = deposed_register  # type: ignore[method-assign]

        class RecordingStopEvent(OneBeatStopEvent):
            def __init__(self) -> None:
                super().__init__()
                self.set_calls = 0

            def set(self) -> None:
                self.set_calls += 1

        stop_event = RecordingStopEvent()
        bridge.stop_event = stop_event  # type: ignore[assignment]

        bridge.heartbeat_loop()

        # Deposition stops on the FIRST failure — never the 3-strike ladder.
        self.assertEqual(stop_event.set_calls, 1)
        self.assertEqual(bridge.heartbeat_failures, 0)


def make_bridge(*extra: str) -> Bridge:
    with tempfile.TemporaryDirectory() as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text(
            "AGENT_REDIS_HOST=127.0.0.1\n"
            "AGENT_REDIS_PORT=6390\n"
            "AGENT_REDIS_DB=12\n"
            "AGENT_REDIS_PREFIX=agent_scratch:\n"
            "AGENT_WORKSPACE=dev\n"
            "AGENT_PROJECT=project-c\n"
        )
        args = build_parser().parse_args(
            [
                "--env-file",
                str(env_file),
                "--workdir",
                "/srv/projects/example-bridge",
                *extra,
            ]
        )
        return Bridge(args)


if __name__ == "__main__":
    unittest.main()


class EnvScrubAdvertisementTest(unittest.TestCase):
    """The registry env_scrub field is the faba author-round guard's evidence:
    every engine advertises bus-and-gate-daemon-creds-v2 only after the
    selected-engine scrub self-check executes (Stage 1d-i)."""

    def test_agent_sdk_engine_advertises_bus_and_gate_daemon_scrub(self) -> None:
        from agent_redis_bridge.engines._stdio import ENV_SCRUB_CAPABILITY

        bridge = make_bridge("--engine", "asdk", "--role", "opus")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        bridge.reassert_liveness()
        self.assertEqual(
            fake.registry[bridge.agent_id]["env_scrub"], ENV_SCRUB_CAPABILITY
        )
        self.assertEqual(ENV_SCRUB_CAPABILITY, "bus-and-gate-daemon-creds-v2")

    def test_popen_engine_also_advertises_v2_after_self_check(self) -> None:
        from agent_redis_bridge.engines._stdio import ENV_SCRUB_CAPABILITY

        bridge = make_bridge()
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        bridge.reassert_liveness()
        self.assertEqual(
            fake.registry[bridge.agent_id]["env_scrub"], ENV_SCRUB_CAPABILITY
        )

    def test_reassert_liveness_calls_prove_not_bare_capability_constant(self) -> None:
        """Source pin: v2 advertisement must execute prove_env_scrub_capability.

        Returning ENV_SCRUB_CAPABILITY without the self-check is the defect this
        kills (Stage 1d-i mutation: capability without proof).
        """
        from pathlib import Path

        import agent_redis_bridge.bridge as bridge_mod

        src = Path(bridge_mod.__file__).read_text(encoding="utf-8")
        # Must call prove with the selected engine (real-child family probe), not
        # a bare capability constant and not a predicate-only zero-arg call alone.
        self.assertIn("prove_env_scrub_capability(engine_name=self.engine_name)", src)
        # Must not reintroduce the agent-sdk-only exception.
        self.assertNotIn(
            'env_scrub=ENV_SCRUB_CAPABILITY if self.engine_name == "agent-sdk"',
            src,
        )

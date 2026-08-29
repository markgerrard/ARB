import os
import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

import pytest

from agent_redis_bridge import claim_gate
from agent_redis_bridge.bridge import Bridge, build_parser
from agent_redis_bridge.engines.codex import CodexEngine
from agent_redis_bridge.readonly_gate import ReadonlyGateError


class BridgePolicyTest(unittest.TestCase):
    def test_approval_policy_for_sender_policy(self) -> None:
        self.assertEqual(CodexEngine.approval_policy_for_policy("trusted"), "never")
        self.assertEqual(CodexEngine.approval_policy_for_policy("human"), "on-request")
        self.assertEqual(CodexEngine.approval_policy_for_policy("reject"), "on-request")


class BridgeEnvFilePrecedenceTest(unittest.TestCase):
    def setUp(self) -> None:
        # These tests exercise control/eval/live precedence, not the independent
        # audit plane. Never let an operator's real audit bus leak into unit setup.
        self._audit_env = patch.dict(os.environ, {"ARB_MEMORY_REDIS_URL": ""}, clear=False)
        self._audit_env.start()
        self.addCleanup(self._audit_env.stop)

    def test_sender_policy_resolved_from_env_file_when_no_cli_flag(self) -> None:
        with patch.dict("os.environ", {"AGENT_TRUSTED_SENDERS": "shell-sender=trusted"}, clear=False):
            bridge = make_env_bridge(
                "AGENT_TRUSTED_SENDERS=env-sender=human\n",
                "--agent-id",
                "codex-project-c-dev",
            )

        self.assertEqual(bridge.sender_policies, {"env-sender": "human"})

    def test_sender_policy_cli_flag_overrides_env_file(self) -> None:
        bridge = make_env_bridge(
            "AGENT_TRUSTED_SENDERS=env-sender=trusted\n",
            "--sender-policy",
            "cli-sender=human",
        )

        self.assertEqual(bridge.sender_policies, {"cli-sender": "human"})

    def test_workdir_resolved_from_env_file_when_no_cli_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_workdir = Path(temp_dir) / "env-workdir"
            env_workdir.mkdir()

            bridge = make_env_bridge(f"AGENT_WORKDIR={env_workdir}\n")

        self.assertEqual(bridge.workdir, env_workdir.resolve())

    def test_workdir_cli_flag_overrides_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cli_workdir = Path(temp_dir) / "cli-workdir"
            env_workdir = Path(temp_dir) / "env-workdir"
            cli_workdir.mkdir()
            env_workdir.mkdir()

            bridge = make_env_bridge(f"AGENT_WORKDIR={env_workdir}\n", "--workdir", str(cli_workdir))

        self.assertEqual(bridge.workdir, cli_workdir.resolve())

    def test_warns_when_no_sender_policies_are_configured(self) -> None:
        with patch.dict("os.environ", {"AGENT_TRUSTED_SENDERS": ""}, clear=False):
            with self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs:
                bridge = make_env_bridge("")

        self.assertEqual(bridge.sender_policies, {})
        self.assertTrue(
            any(
                "[bridge] WARNING: no sender policies configured. "
                "Bridge will reject all dispatches until AGENT_TRUSTED_SENDERS is set "
                "in the env file (preferred) or --sender-policy is passed on the CLI."
                in line
                for line in logs.output
            )
        )

    def test_warns_when_parallel_with_inbox_notifies(self) -> None:
        with self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs:
            make_env_bridge("", "--max-parallel", "4", "--notify-inbox", "1")
        self.assertTrue(
            any(
                "[bridge] WARNING: max_parallel>1 with notify_inbox=1 routes every task "
                "notify into the caller's :inbox, which floods it under parallel load. "
                "Set BRIDGE_NOTIFY_INBOX=0 (or --notify-inbox 0) to route notifies to a "
                "separate :notify_inbox list for any parallel orchestrator."
                in line
                for line in logs.output
            )
        )

    def test_no_notify_warning_when_serial_or_separate_inbox(self) -> None:
        # serial (default max_parallel=1) -> no notify-flood warning even with notify_inbox=1
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("BRIDGE_MAX_PARALLEL", None)
            with self.assertNoLogs("agent_redis_bridge.bridge", level="WARNING"):
                make_env_bridge("", "--notify-inbox", "1", "--sender-policy", "sender=trusted")
        # parallel but notifies routed to the separate list -> no warning
        with self.assertNoLogs("agent_redis_bridge.bridge", level="WARNING"):
            make_env_bridge("", "--max-parallel", "4", "--notify-inbox", "0", "--sender-policy", "sender=trusted")

    def test_eval_redis_client_uses_fail_fast_socket_timeouts(self) -> None:
        fake_eval = FakeEvalRedis(db=4)
        with patch.dict(
            os.environ,
            {
                "ARB_EVAL_REDIS_URL": "redis://127.0.0.1:6379",
                "ARB_EVAL_REDIS_DB": "4",
                "ARB_MEMORY_REDIS_URL": "",
            },
            clear=False,
        ):
            with patch("redis.from_url", return_value=fake_eval) as from_url:
                bridge = make_env_bridge("")

        self.assertIs(bridge.eval_redis, fake_eval)
        from_url.assert_called_once_with(
            "redis://127.0.0.1:6379",
            db=4,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            socket_keepalive=True,
            health_check_interval=30,
        )

    def test_eval_redis_db_mismatch_raises_at_init(self) -> None:
        fake_eval = FakeEvalRedis(db=3)
        with patch.dict(
            os.environ,
            {"ARB_EVAL_REDIS_URL": "redis://127.0.0.1:6379/3", "ARB_EVAL_REDIS_DB": "4"},
            clear=False,
        ):
            with patch("redis.from_url", return_value=fake_eval):
                with self.assertRaisesRegex(ValueError, "ARB_EVAL_REDIS_DB mismatch"):
                    make_env_bridge("")

    def test_live_redis_defaults_to_control_redis_when_unconfigured(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARB_LIVE_REDIS_URL", None)
            os.environ.pop("ARB_LIVE_PREFIX", None)
            bridge = make_env_bridge("")

        self.assertIs(bridge.live_redis, bridge.redis)

    def test_live_redis_resolved_from_env_file(self) -> None:
        fake_live = object()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARB_LIVE_REDIS_URL", None)
            os.environ.pop("ARB_LIVE_PREFIX", None)
            os.environ.pop("ARB_MEMORY_REDIS_URL", None)
            with patch("redis.from_url", return_value=fake_live) as from_url:
                bridge = make_env_bridge(
                    "ARB_LIVE_REDIS_URL=redis://live.example:6379/5\n"
                    "ARB_LIVE_PREFIX=fleet:\n"
                )

        self.assertIs(bridge.live_redis, fake_live)
        self.assertEqual(bridge._live_prefix, "fleet:")
        from_url.assert_called_once_with(
            "redis://live.example:6379/5",
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            socket_keepalive=True,
            health_check_interval=30,
        )

    def test_warns_when_eval_remote_without_live_remote(self) -> None:
        fake_eval = FakeEvalRedis(db=4)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARB_LIVE_REDIS_URL", None)
            os.environ.pop("ARB_LIVE_PREFIX", None)
            with patch("redis.from_url", return_value=fake_eval):
                with self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs:
                    bridge = make_env_bridge(
                        "ARB_EVAL_REDIS_URL=redis://eval.example:6379\n"
                        "ARB_EVAL_REDIS_DB=4\n"
                    )
        try:
            self.assertTrue(
                any("ARB_LIVE_REDIS_URL is not set" in line for line in logs.output),
                logs.output,
            )
        finally:
            bridge._eval_flusher.stop()
            bridge._eval_thread.join(timeout=1)


class BridgeReadonlyGateIntegrationTest(unittest.TestCase):
    """The gate must resolve its config from the ENV-FILE (the documented
    read-only pi-seat shape), not just process env, and run before serving.
    Regression cover for the tri-model review finding: env-file-configured seats
    were silently fail-open."""

    def setUp(self) -> None:
        self._audit_env = patch.dict(os.environ, {"ARB_MEMORY_REDIS_URL": ""}, clear=False)
        self._audit_env.start()
        self.addCleanup(self._audit_env.stop)

    def _clean_env_bridge(self, env_overrides: str, *extra: str) -> Bridge:
        # Ensure the gate/tool vars come from the env-file under test, not from a
        # leaked process env, so we exercise env-file resolution specifically.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARB_REQUIRE_READONLY_TOOLS", None)
            os.environ.pop("BRIDGE_PI_TOOLS", None)
            return make_env_bridge(env_overrides, *extra)

    def test_pi_tools_resolved_from_env_file(self) -> None:
        bridge = self._clean_env_bridge(
            "BRIDGE_PI_TOOLS=read,grep,find,ls\n", "--engine", "pi-sdk"
        )
        # The engine reads args.pi_tools; without env-file resolution it would be
        # None -> full toolset.
        self.assertEqual(bridge.args.pi_tools, "read,grep,find,ls")

    def test_gate_passes_when_env_file_declares_readonly_and_allowlist(self) -> None:
        bridge = self._clean_env_bridge(
            "ARB_REQUIRE_READONLY_TOOLS=read,grep,find,ls\n"
            "BRIDGE_PI_TOOLS=read,grep,find,ls\n",
            "--engine",
            "pi-sdk",
        )
        bridge.enforce_readonly_gate()  # must NOT raise

    def test_gate_refuses_when_env_file_drops_pi_tools(self) -> None:
        # The fail-open via the documented config path: marker set in env-file,
        # BRIDGE_PI_TOOLS absent -> pi would fall back to full tools -> refuse.
        bridge = self._clean_env_bridge(
            "ARB_REQUIRE_READONLY_TOOLS=read,grep,find,ls\n", "--engine", "pi-sdk"
        )
        with self.assertRaises(ReadonlyGateError):
            bridge.enforce_readonly_gate()

    def test_gate_refuses_when_env_file_surface_has_write_tool(self) -> None:
        bridge = self._clean_env_bridge(
            "ARB_REQUIRE_READONLY_TOOLS=read,grep,find,ls\n"
            "BRIDGE_PI_TOOLS=read,write\n",
            "--engine",
            "pi-sdk",
        )
        with self.assertRaises(ReadonlyGateError):
            bridge.enforce_readonly_gate()

    def test_gate_noop_without_marker(self) -> None:
        bridge = self._clean_env_bridge("", "--engine", "pi-sdk")
        bridge.enforce_readonly_gate()  # no marker -> no-op


def make_env_bridge(env_overrides: str, *extra: str) -> Bridge:
    with tempfile.TemporaryDirectory() as temp_dir:
        workdir = Path(temp_dir) / "workdir"
        workdir.mkdir()
        env_file = Path(temp_dir) / ".env"
        env_file.write_text(
            "AGENT_REDIS_HOST=127.0.0.1\n"
            "AGENT_REDIS_PORT=6390\n"
            "AGENT_REDIS_DB=12\n"
            "AGENT_REDIS_PREFIX=agent_scratch:\n"
            "AGENT_WORKSPACE=dev\n"
            "AGENT_PROJECT=project-c\n"
            f"{env_overrides}"
        )
        with patch("agent_redis_bridge.bridge.DEFAULT_WORKDIR", None):
            args = build_parser().parse_args(["--env-file", str(env_file), *extra])
        if args.workdir is None and "AGENT_WORKDIR=" not in env_overrides:
            args.workdir = str(workdir)
        return Bridge(args)


class FakeConnectionPool:
    def __init__(self, db: int) -> None:
        self.connection_kwargs = {"db": db}


class FakeEvalRedis:
    def __init__(self, db: int) -> None:
        self.connection_pool = FakeConnectionPool(db)


# ---------------------------------------------------------------------------
# Claim-gate rollout / lifecycle (Slice 1c Task 3)
# ---------------------------------------------------------------------------


def test_claim_gate_defaults_off(monkeypatch):
    monkeypatch.delenv("BRIDGE_CLAIM_GATE", raising=False)
    assert build_parser().parse_args([]).claim_gate is False


def test_claim_gate_can_be_enabled_only_explicitly(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    assert build_parser().parse_args([]).claim_gate is True


def test_missing_reader_dsn_never_falls_back_to_process_owner_dsns(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.delenv("ARB_GATE_READER_DSN", raising=False)
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://owner@db/arb_memory")
    monkeypatch.setenv("ARB_MEMORY_MCP_DSN", "postgresql://writer@db/arb_memory")
    with patch(
        "agent_redis_bridge.claim_resolver.PsycopgClaimResolver"
    ) as resolver_type:
        with pytest.raises(RuntimeError, match="ARB_GATE_READER_DSN"):
            make_env_bridge("")
    resolver_type.assert_not_called()


def test_owner_dsn_in_app_env_is_never_a_gate_reader_fallback(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.delenv("ARB_GATE_READER_DSN", raising=False)
    with pytest.raises(RuntimeError, match="ARB_GATE_READER_DSN"):
        make_env_bridge("ARB_MEMORY_DSN=postgresql://owner@db/arb_memory\n")


def test_enabled_gate_builds_daemon_scoped_resolver_from_process_secret(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_READER_ROLE", "arb_gate_reader")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_DSN", "postgresql://writer@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_ROLE", "seat_writer")
    with patch(
        "agent_redis_bridge.claim_resolver.PsycopgClaimResolver"
    ) as resolver_type:
        with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter"):
            bridge = make_env_bridge("")
    resolver_type.assert_called_once_with(
        "postgresql://reader@db/arb_memory",
        expected_role="arb_gate_reader",
    )
    assert bridge.claim_resolver is resolver_type.return_value
    assert bridge.claim_gate_enabled is True


def _resolver_mock(**attrs):
    """Mock with assert_* methods allowed (unittest.Mock reserves assert_ names)."""
    return Mock(unsafe=True, **attrs)


def test_reader_readiness_precedes_registration_and_engine_start(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_DSN", "postgresql://writer@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_ROLE", "seat_writer")
    with patch(
        "agent_redis_bridge.claim_resolver.PsycopgClaimResolver"
    ) as resolver_type:
        with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
            resolver = _resolver_mock()
            writer = _writer_mock()
            resolver_type.return_value = resolver
            writer_type.return_value = writer
            bridge = make_env_bridge("")

    order: list[str] = []
    bridge.claim_resolver.assert_ready.side_effect = lambda: order.append("reader-ready")
    bridge.lane_writer.assert_ready.side_effect = lambda: order.append("writer-ready")
    bridge.register = lambda: order.append("register")
    bridge.start_engine = lambda: order.append("engine")
    bridge.inbox_loop = lambda: 0
    bridge.cleanup = lambda: None
    bridge.reconcile_worktree_leases = lambda: None
    assert bridge.run() == 0
    assert order.index("reader-ready") < order.index("register")
    assert order.index("reader-ready") < order.index("engine")


def test_reader_readiness_failure_cleans_up_and_refuses_to_register(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_DSN", "postgresql://writer@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_ROLE", "seat_writer")
    with patch(
        "agent_redis_bridge.claim_resolver.PsycopgClaimResolver"
    ) as resolver_type:
        with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
            resolver = _resolver_mock()
            writer = _writer_mock()
            resolver_type.return_value = resolver
            writer_type.return_value = writer
            bridge = make_env_bridge("")

    bridge.claim_resolver.assert_ready.side_effect = claim_gate.StoreUnreachable(
        "denied"
    )
    bridge.register = Mock(side_effect=AssertionError("must not register"))
    # cleanup runs real redis.cleanup — keep hermetic
    with patch.object(bridge.redis, "cleanup"), patch.object(bridge.pool, "stop_all"):
        with pytest.raises(claim_gate.StoreUnreachable, match="denied"):
            bridge.run()
    bridge.claim_resolver.close.assert_called_once_with()
    bridge.lane_writer.close.assert_called_once_with()
    bridge.register.assert_not_called()


def test_cleanup_closes_claim_resolver(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_DSN", "postgresql://writer@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_ROLE", "seat_writer")
    with patch(
        "agent_redis_bridge.claim_resolver.PsycopgClaimResolver"
    ) as resolver_type:
        with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
            resolver = _resolver_mock()
            writer = _writer_mock()
            resolver_type.return_value = resolver
            writer_type.return_value = writer
            bridge = make_env_bridge("")

    with patch.object(bridge.redis, "cleanup"), patch.object(bridge.pool, "stop_all"):
        bridge.cleanup()
    bridge.claim_resolver.close.assert_called_once_with()


# ---------------------------------------------------------------------------
# Slice 1d-ii: lane-writer construction / readiness / no-fallback
# ---------------------------------------------------------------------------


def _writer_mock(**attrs):
    return Mock(unsafe=True, **attrs)


def test_worktree_lane_defaults_to_gated(monkeypatch):
    monkeypatch.delenv("BRIDGE_WORKTREE_LANE", raising=False)
    bridge = make_env_bridge("")
    assert bridge.worktree_lane == "gated"


def test_worktree_lane_accepts_only_gated_or_exempt(monkeypatch):
    monkeypatch.setenv("BRIDGE_WORKTREE_LANE", "exempt")
    bridge = make_env_bridge("")
    assert bridge.worktree_lane == "exempt"

    monkeypatch.setenv("BRIDGE_WORKTREE_LANE", "maybe")
    with pytest.raises(RuntimeError, match="BRIDGE_WORKTREE_LANE"):
        make_env_bridge("")


def test_gate_off_without_lane_writer_dsn_preserves_legacy_construction(monkeypatch):
    monkeypatch.delenv("BRIDGE_CLAIM_GATE", raising=False)
    monkeypatch.delenv("ARB_GATE_LANE_WRITER_DSN", raising=False)
    with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
        bridge = make_env_bridge("")
    writer_type.assert_not_called()
    assert bridge.lane_writer is None
    assert bridge.claim_gate_enabled is False


def test_gate_on_without_lane_writer_dsn_refuses_before_registration(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.delenv("ARB_GATE_LANE_WRITER_DSN", raising=False)
    with patch("agent_redis_bridge.claim_resolver.PsycopgClaimResolver"):
        with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
            with pytest.raises(RuntimeError, match="ARB_GATE_LANE_WRITER_DSN"):
                make_env_bridge("")
    writer_type.assert_not_called()


def test_lane_writer_never_falls_back_to_owner_reader_or_mcp_dsns(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.delenv("ARB_GATE_LANE_WRITER_DSN", raising=False)
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://owner@db/arb_memory")
    monkeypatch.setenv("ARB_MEMORY_MCP_DSN", "postgresql://mcp@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    with patch("agent_redis_bridge.claim_resolver.PsycopgClaimResolver"):
        with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
            with pytest.raises(RuntimeError, match="ARB_GATE_LANE_WRITER_DSN"):
                make_env_bridge("")
    writer_type.assert_not_called()


def test_lane_writer_dsn_in_app_env_is_never_a_process_secret_fallback(monkeypatch):
    """Writer secret is process-env only — app-repo env-file must not supply it."""
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.delenv("ARB_GATE_LANE_WRITER_DSN", raising=False)
    with patch("agent_redis_bridge.claim_resolver.PsycopgClaimResolver"):
        with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
            with pytest.raises(RuntimeError, match="ARB_GATE_LANE_WRITER_DSN"):
                make_env_bridge(
                    "ARB_GATE_LANE_WRITER_DSN=postgresql://writer@db/arb_memory\n"
                )
    writer_type.assert_not_called()


def test_enabled_gate_builds_lane_writer_with_agent_id_and_server_lane(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_DSN", "postgresql://seat_a@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_ROLE", "seat_a_writer")
    monkeypatch.setenv("BRIDGE_WORKTREE_LANE", "exempt")
    with patch("agent_redis_bridge.claim_resolver.PsycopgClaimResolver") as resolver_type:
        with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
            bridge = make_env_bridge("", "--agent-id", "codex-project-c-dev")
    writer_type.assert_called_once_with(
        "postgresql://seat_a@db/arb_memory",
        expected_role="seat_a_writer",
        expected_consumer_id="codex-project-c-dev",
        expected_lane="exempt",
    )
    assert bridge.lane_writer is writer_type.return_value
    assert bridge.worktree_lane == "exempt"
    resolver_type.assert_called_once()


def test_gate_off_with_lane_writer_dsn_still_constructs_writer_for_rollout(monkeypatch):
    monkeypatch.delenv("BRIDGE_CLAIM_GATE", raising=False)
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_DSN", "postgresql://seat_a@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_ROLE", "seat_a_writer")
    with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
        bridge = make_env_bridge("", "--agent-id", "codex-project-c-dev")
    writer_type.assert_called_once()
    assert bridge.lane_writer is writer_type.return_value
    assert bridge.claim_gate_enabled is False


def test_writer_readiness_and_reconcile_precede_register_and_engine_start(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_DSN", "postgresql://seat_a@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_ROLE", "seat_a_writer")
    with patch("agent_redis_bridge.claim_resolver.PsycopgClaimResolver") as resolver_type:
        with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
            resolver = _resolver_mock()
            writer = _writer_mock()
            resolver_type.return_value = resolver
            writer_type.return_value = writer
            bridge = make_env_bridge("")

    order: list[str] = []
    bridge.claim_resolver.assert_ready.side_effect = lambda: order.append("reader-ready")
    bridge.lane_writer.assert_ready.side_effect = lambda: order.append("writer-ready")
    bridge.reconcile_worktree_leases = lambda: order.append("reconcile") or []
    bridge.register = lambda: order.append("register")
    bridge.start_engine = lambda: order.append("engine")
    bridge.inbox_loop = lambda: 0
    bridge.cleanup = lambda: None
    assert bridge.run() == 0
    assert order.index("reader-ready") < order.index("register")
    assert order.index("writer-ready") < order.index("register")
    assert order.index("reconcile") < order.index("register")
    assert order.index("writer-ready") < order.index("engine")
    assert order.index("reconcile") < order.index("engine")


def test_writer_readiness_failure_cleans_up_resolver_and_writer(monkeypatch):
    from agent_redis_bridge.lane_writer import LaneStoreUnreachable

    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_DSN", "postgresql://seat_a@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_ROLE", "seat_a_writer")
    with patch("agent_redis_bridge.claim_resolver.PsycopgClaimResolver") as resolver_type:
        with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
            resolver = _resolver_mock()
            writer = _writer_mock()
            resolver_type.return_value = resolver
            writer_type.return_value = writer
            bridge = make_env_bridge("")

    bridge.lane_writer.assert_ready.side_effect = LaneStoreUnreachable("writer denied")
    bridge.register = Mock(side_effect=AssertionError("must not register"))
    with patch.object(bridge.redis, "cleanup"), patch.object(bridge.pool, "stop_all"):
        with pytest.raises(LaneStoreUnreachable, match="writer denied"):
            bridge.run()
    bridge.claim_resolver.close.assert_called_once_with()
    bridge.lane_writer.close.assert_called_once_with()
    bridge.register.assert_not_called()


def test_cleanup_closes_lane_writer(monkeypatch):
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_DSN", "postgresql://seat_a@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_LANE_WRITER_ROLE", "seat_a_writer")
    with patch("agent_redis_bridge.lane_writer.PsycopgLaneWriter") as writer_type:
        writer = _writer_mock()
        writer_type.return_value = writer
        bridge = make_env_bridge("")

    with patch.object(bridge.redis, "cleanup"), patch.object(bridge.pool, "stop_all"):
        bridge.cleanup()
    bridge.lane_writer.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

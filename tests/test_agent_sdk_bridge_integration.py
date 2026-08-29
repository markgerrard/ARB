from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_redis_bridge import bridge as bridge_mod
from agent_redis_bridge.bridge import Bridge, build_parser
from agent_redis_bridge.engines import agent_sdk as agent_sdk_engine_mod
from agent_redis_bridge.engines.base import EngineError, TurnResult


class RecordingAgentSdkEngine:
    instances: list["RecordingAgentSdkEngine"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        RecordingAgentSdkEngine.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass


class FakeRedis:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict[str, str]]] = []

    def lpush(self, agent_id: str, body: str) -> None:
        self.replies.append((agent_id, body))

    def lpush_key(self, key: str, body: str, *, trim: int | None = None) -> None:
        pass

    def xadd(self, key: str, fields: dict[str, str], *, maxlen: int | None = None, ttl: int | None = None) -> str:
        self.events.append((key, fields))
        return "1-0"

    def hset_key(self, key: str, fields: dict[str, str], *, ttl: int | None = None) -> None:
        pass

    def set_key(self, key: str, value: str, *, ttl: int | None = None) -> None:
        pass

    def get_int(self, key: str) -> int:
        return 0

    def incrby(self, key: str, amount: int, *, ttl: int | None = None) -> int:
        return amount


class AgentSdkBridgeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        RecordingAgentSdkEngine.instances.clear()

    def test_engine_is_registered_with_asdk_tool_prefix(self) -> None:
        # agent-sdk seats register under the short tool/seat name "asdk"; "asdk" is also a
        # --engine alias that normalizes to "agent-sdk".
        self.assertEqual(bridge_mod.ENGINE_TO_TOOL["agent-sdk"], "asdk")
        self.assertEqual(bridge_mod.ENGINE_TO_TOOL["asdk"], "asdk")

    def test_asdk_alias_normalizes_to_agent_sdk_on_all_paths(self) -> None:
        # --engine asdk must normalize centrally (not only in main()), so engine_name gates and
        # build_engine route to agent-sdk; the short name survives only in the tool/seat id.
        bridge = make_bridge(
            "AGENT_SDK_MINIMAX_KEY=minimax-key\n", "--engine", "asdk", "--model", "minimax-m3"
        )
        self.assertEqual(bridge.engine_name, "agent-sdk")
        self.assertEqual(bridge.args.engine, "agent-sdk")
        self.assertEqual(bridge.agent_id, "asdk-project-c-dev-m3")
        with mock.patch.object(agent_sdk_engine_mod, "AgentSdkEngine", RecordingAgentSdkEngine):
            engine = bridge_mod.build_engine(bridge.args, cwd="/tmp/project")
        self.assertIs(engine, RecordingAgentSdkEngine.instances[-1])

    def test_agent_id_defaults_to_model_slug_role(self) -> None:
        bridge = make_bridge(
            "AGENT_SDK_MINIMAX_KEY=minimax-key\n",
            "--engine",
            "agent-sdk",
            "--model",
            "minimax-m3",
        )

        self.assertEqual(bridge.agent_id, "asdk-project-c-dev-m3")
        self.assertEqual(bridge.args._derived_agent_id, "asdk-project-c-dev-m3")

    def test_build_engine_passes_agent_sdk_configuration(self) -> None:
        with mock.patch.object(agent_sdk_engine_mod, "AgentSdkEngine", RecordingAgentSdkEngine):
            bridge = make_bridge(
                "AGENT_SDK_MINIMAX_KEY=minimax-key\n"
                "BRIDGE_AGENT_SDK_TOOLS=Read,Grep,Glob\n",
                "--engine",
                "agent-sdk",
                "--model",
                "minimax-m3",
                "--agent-sdk-session-root",
                "/tmp/sessions",
                "--agent-sdk-oneshot",
            )
            engine = bridge_mod.build_engine(bridge.args, cwd="/tmp/project")

        self.assertIs(engine, RecordingAgentSdkEngine.instances[0])
        self.assertEqual(engine.kwargs["cwd"], "/tmp/project")
        self.assertEqual(engine.kwargs["model"], "minimax-m3")
        self.assertEqual(engine.kwargs["tool_ceiling"], "Read,Grep,Glob")
        self.assertEqual(engine.kwargs["key"], "minimax-key")
        self.assertEqual(engine.kwargs["session_root"], "/tmp/sessions")
        self.assertTrue(engine.kwargs["oneshot"])
        self.assertEqual(engine.kwargs["agent_id"], "asdk-project-c-dev-m3")

    def test_build_engine_defaults_session_root_to_xdg_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as state_home:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": state_home}, clear=False):
                with mock.patch.object(agent_sdk_engine_mod, "AgentSdkEngine", RecordingAgentSdkEngine):
                    bridge = make_bridge("AGENT_SDK_MINIMAX_KEY=minimax-key\n", "--engine", "agent-sdk")
                    engine = bridge_mod.build_engine(
                        bridge.args,
                        cwd=str(bridge.workdir / ".claude" / "worktrees" / "task"),
                    )

        self.assertIs(engine, RecordingAgentSdkEngine.instances[0])
        self.assertEqual(
            engine.kwargs["session_root"],
            str(Path(state_home) / "agent-redis-bridge" / "agent-sdk-sessions"),
        )

    def test_build_engine_enables_live_smoke_only_for_primary_workdir(self) -> None:
        with mock.patch.object(agent_sdk_engine_mod, "AgentSdkEngine", RecordingAgentSdkEngine):
            bridge = make_bridge(
                "AGENT_SDK_MINIMAX_KEY=minimax-key\n",
                "--engine",
                "agent-sdk",
            )
            primary = bridge_mod.build_engine(bridge.args, cwd=str(bridge.workdir))
            worktree = bridge_mod.build_engine(
                bridge.args,
                cwd=str(bridge.workdir / ".claude" / "worktrees" / "task"),
            )

        self.assertIs(primary, RecordingAgentSdkEngine.instances[0])
        self.assertIs(worktree, RecordingAgentSdkEngine.instances[1])
        self.assertTrue(primary.kwargs["live_smoke_test"])
        self.assertFalse(worktree.kwargs["live_smoke_test"])

    def test_build_engine_requires_provider_key(self) -> None:
        bridge = make_bridge("", "--engine", "agent-sdk", "--model", "minimax-m3")

        with self.assertRaises(EngineError):
            bridge_mod.build_engine(bridge.args, cwd="/tmp/project")

    def test_build_engine_subscription_uses_claude_code_oauth_without_vendor_key(self) -> None:
        with mock.patch.object(agent_sdk_engine_mod, "AgentSdkEngine", RecordingAgentSdkEngine):
            bridge = make_bridge(
                "CLAUDE_CODE_OAUTH_TOKEN=oauth-token\n"
                "BRIDGE_AGENT_SDK_TOOLS=Read,Grep,Glob,LS\n",
                "--engine",
                "agent-sdk",
                "--model",
                "opus-4.8",
            )
            engine = bridge_mod.build_engine(bridge.args, cwd="/tmp/project")

        self.assertIs(engine, RecordingAgentSdkEngine.instances[0])
        self.assertEqual(engine.kwargs["model"], "opus-4.8")
        self.assertEqual(engine.kwargs["key"], "oauth-token")
        self.assertEqual(engine.kwargs["tool_ceiling"], "Read,Grep,Glob,LS")

    def test_start_engine_runs_before_register_for_agent_sdk(self) -> None:
        bridge = make_bridge("AGENT_SDK_MINIMAX_KEY=minimax-key\n", "--engine", "agent-sdk")
        order: list[str] = []
        bridge.enforce_readonly_gate = lambda: order.append("gate")  # type: ignore[method-assign]
        bridge.start_engine = lambda: order.append("start")  # type: ignore[method-assign]
        bridge.register = lambda: order.append("register")  # type: ignore[method-assign]
        bridge.inbox_loop = lambda: 0  # type: ignore[method-assign]
        bridge.cleanup = lambda: order.append("cleanup")  # type: ignore[method-assign]

        self.assertEqual(bridge.run(), 0)

        self.assertEqual(order, ["gate", "start", "register", "cleanup"])

    def test_readonly_gate_certifies_agent_sdk_surface(self) -> None:
        bridge = make_bridge(
            "AGENT_SDK_MINIMAX_KEY=minimax-key\n"
            "ARB_REQUIRE_READONLY_TOOLS=Read,Grep,Glob\n"
            "BRIDGE_AGENT_SDK_TOOLS=Read,Grep\n",
            "--engine",
            "agent-sdk",
        )

        bridge.enforce_readonly_gate()

    def test_trusted_stateful_request_without_worktree_refuses_before_pool(self) -> None:
        bridge = make_bridge("AGENT_SDK_MINIMAX_KEY=minimax-key\n", "--engine", "agent-sdk")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch.object(bridge.pool, "acquire", side_effect=AssertionError("pool touched")):
            self.assertFalse(bridge.handle_raw(request_json(bridge.agent_id)))

        self.assertEqual(len(fake.replies), 1)
        reply = json.loads(fake.replies[0][1])
        self.assertFalse(reply["payload"]["ok"])
        self.assertIn("worktree", reply["payload"]["error"])

    def test_oneshot_agent_sdk_allows_trusted_request_without_worktree(self) -> None:
        bridge = make_bridge(
            "AGENT_SDK_MINIMAX_KEY=minimax-key\n",
            "--engine",
            "agent-sdk",
            "--agent-sdk-oneshot",
        )
        fake_engine = mock.Mock()
        fake_engine.run_turn_with_progress.return_value = TurnResult(ok=True, result="ok")
        bridge.pool.acquire = mock.Mock(return_value=fake_engine)  # type: ignore[method-assign]
        bridge.redis = FakeRedis()  # type: ignore[assignment]

        self.assertFalse(bridge.handle_raw(request_json(bridge.agent_id)))
        bridge.join_active_thread()
        bridge.pool.acquire.assert_called_once()

    def test_agent_sdk_reply_carries_engine_session_id(self) -> None:
        bridge = make_bridge(
            "AGENT_SDK_MINIMAX_KEY=minimax-key\n",
            "--engine",
            "agent-sdk",
            "--agent-sdk-oneshot",
        )
        fake_engine = mock.Mock()
        fake_engine.session_id = "sid-bridge"
        fake_engine.run_turn_with_progress.return_value = TurnResult(ok=True, result="ok")
        bridge.pool.acquire = mock.Mock(return_value=fake_engine)  # type: ignore[method-assign]
        fake_redis = FakeRedis()
        bridge.redis = fake_redis  # type: ignore[assignment]

        self.assertFalse(bridge.handle_raw(request_json(bridge.agent_id)))
        bridge.join_active_thread()

        reply = next(json.loads(body) for _, body in fake_redis.replies if json.loads(body)["kind"] == "reply")
        self.assertEqual(reply["payload"]["thread_id"], "sid-bridge")

    def test_bridge_passes_orchestrator_context_to_agent_sdk_engine(self) -> None:
        bridge = make_bridge(
            "CLAUDE_CODE_OAUTH_TOKEN=oauth-token\n"
            "AGENT_TRUSTED_SENDERS=claude-code-reviewer=trusted\n",
            "--engine",
            "agent-sdk",
            "--model",
            "opus-4.8",
            "--agent-sdk-oneshot",
        )
        fake_engine = mock.Mock()
        fake_engine.run_turn_with_progress.return_value = TurnResult(ok=True, result="ok")
        bridge.pool.acquire = mock.Mock(return_value=fake_engine)  # type: ignore[method-assign]
        bridge.redis = FakeRedis()  # type: ignore[assignment]

        self.assertFalse(
            bridge.handle_raw(
                request_json(
                    bridge.agent_id,
                    sender="claude-code-reviewer",
                    payload={"task": "review", "orchestrator_model": "claude-opus-4-8"},
                )
            )
        )
        bridge.join_active_thread()

        fake_engine.set_turn_audit_context.assert_called_once_with(
            orchestrator_identity="claude-code-reviewer",
            orchestrator_model="claude-opus-4-8",
        )

    def test_bridge_does_not_use_target_model_as_orchestrator_model(self) -> None:
        bridge = make_bridge(
            "CLAUDE_CODE_OAUTH_TOKEN=oauth-token\n"
            "AGENT_TRUSTED_SENDERS=claude-sonnet-reviewer=trusted\n",
            "--engine",
            "agent-sdk",
            "--model",
            "opus-4.8",
            "--agent-sdk-oneshot",
        )
        fake_engine = mock.Mock()
        fake_engine.run_turn_with_progress.return_value = TurnResult(ok=True, result="ok")
        bridge.pool.acquire = mock.Mock(return_value=fake_engine)  # type: ignore[method-assign]
        bridge.redis = FakeRedis()  # type: ignore[assignment]

        self.assertFalse(
            bridge.handle_raw(
                request_json(
                    bridge.agent_id,
                    sender="claude-sonnet-reviewer",
                    payload={"task": "review", "model": "claude-opus-4-8"},
                )
            )
        )
        bridge.join_active_thread()

        fake_engine.set_turn_audit_context.assert_called_once_with(
            orchestrator_identity="claude-sonnet-reviewer",
            orchestrator_model=None,
        )

    def test_codex_to_subscription_opus_verdict_round_trip_with_stubbed_seat(self) -> None:
        bridge = make_bridge(
            "CLAUDE_CODE_OAUTH_TOKEN=oauth-token\n"
            "AGENT_TRUSTED_SENDERS=codex-project-c-dev=trusted\n",
            "--engine",
            "agent-sdk",
            "--model",
            "opus-4.8",
            "--agent-sdk-oneshot",
        )
        fake_engine = mock.Mock()
        fake_engine.session_id = "sid-opus"
        fake_engine.run_turn_with_progress.return_value = TurnResult(
            ok=True,
            result="VERDICT: SHIP",
            thread_id="sid-opus",
        )
        bridge.pool.acquire = mock.Mock(return_value=fake_engine)  # type: ignore[method-assign]
        fake_redis = FakeRedis()
        bridge.redis = fake_redis  # type: ignore[assignment]

        self.assertFalse(
            bridge.handle_raw(
                request_json(
                    bridge.agent_id,
                    sender="codex-project-c-dev",
                    payload={"task": "review the diff", "orchestrator_model": "gpt-5"},
                )
            )
        )
        bridge.join_active_thread()

        reply = next(json.loads(body) for _, body in fake_redis.replies if json.loads(body)["kind"] == "reply")
        self.assertEqual(reply["from"], bridge.agent_id)
        self.assertEqual(reply["to"], "codex-project-c-dev")
        self.assertEqual(reply["payload"]["result"], "VERDICT: SHIP")
        self.assertEqual(reply["payload"]["thread_id"], "sid-opus")


def make_bridge(env_overrides: str, *extra: str) -> Bridge:
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
            "AGENT_TRUSTED_SENDERS=claude-project-c-dev=trusted\n"
            f"{env_overrides}"
        )
        args = build_parser().parse_args(
            ["--env-file", str(env_file), "--workdir", str(workdir), *extra]
        )
        return Bridge(args)


def request_json(recipient: str, *, sender: str = "claude-project-c-dev", payload: dict | None = None) -> str:
    return json.dumps(
        {
            "id": "req-agent-sdk",
            "from": sender,
            "branch": "manual",
            "to": recipient,
            "kind": "request",
            "sent_at": "2026-06-18T12:00:00+01:00",
            "payload": payload or {"task": "change files"},
        },
        separators=(",", ":"),
    )


if __name__ == "__main__":
    unittest.main()

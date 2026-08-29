from argparse import Namespace
from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

from agent_redis_bridge.bridge import Bridge, build_parser
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.engines.base import EngineError, TurnResult
from agent_redis_bridge.engines.cursor_acp import CursorAcpEngine


class FakeRedis:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict[str, str]]] = []
        self.statuses: list[tuple[str, dict[str, str]]] = []
        self.results: list[tuple[str, str]] = []
        self.ints: dict[str, int] = {}

    def lpush(self, agent_id: str, body: str) -> None:
        self.replies.append((agent_id, body))

    def xadd(self, key: str, fields: dict[str, str], *, maxlen: int | None = None, ttl: int | None = None) -> str:
        self.events.append((key, fields))
        return "1-0"

    def hset_key(self, key: str, fields: dict[str, str], *, ttl: int | None = None) -> None:
        self.statuses.append((key, fields))

    def set_key(self, key: str, value: str, *, ttl: int | None = None) -> None:
        self.results.append((key, value))

    def get_int(self, key: str) -> int:
        return self.ints.get(key, 0)

    def get_str(self, key: str) -> str | None:
        for result_key, value in reversed(self.results):
            if result_key == key:
                return value
        return None

    def incrby(self, key: str, amount: int, *, ttl: int | None = None) -> int:
        self.ints[key] = self.ints.get(key, 0) + amount
        return self.ints[key]


class BridgeHandleRawTest(unittest.TestCase):
    def test_dry_run_run_engine_succeeds_without_engine(self) -> None:
        bridge = make_bridge("--dry-run")

        result = bridge.run_engine(Envelope.from_json(request_json("req-dry-run-no-engine")), policy="trusted")

        self.assertTrue(result.ok)
        self.assertEqual(result.result, "DRY RUN - no codex call made")
        self.assertIsNone(result.error)

    def test_request_fans_out_events_status_result_and_reply(self) -> None:
        bridge = make_bridge("--dry-run")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        # Engine stubbed: the pool spawns the engine binary before run_engine's
        # dry-run short-circuit, so without this the test needs `codex` on PATH.
        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=RecordingEngine("unused (dry-run)")):
            bridge.handle_raw(request_json("req-1"))
            bridge.join_active_thread()

        # .get, not [] — fake.events now also carries live-tee entries (key
        # agent_scratch:events:live, field event_type not type) now that
        # live_redis correctly falls back to the same fake when unconfigured.
        self.assertTrue(any(fields.get("type") == "task_started" for _, fields in fake.events))
        self.assertTrue(any(fields.get("type") == "task_finished" for _, fields in fake.events))
        self.assertTrue(any(fields["state"] == "completed" for _, fields in fake.statuses))
        self.assertEqual(len(fake.results), 1)
        self.assertTrue(any(json.loads(body)["kind"] == "reply" for _, body in fake.replies))

    def test_sender_rejection_replies_without_task_events(self) -> None:
        bridge = make_bridge("--dry-run", "--unknown-sender-policy", "reject")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        bridge.handle_raw(request_json("req-2", sender="unknown-agent"))

        self.assertEqual(fake.events, [])
        self.assertEqual(len(fake.replies), 1)
        reply = json.loads(fake.replies[0][1])
        self.assertFalse(reply["payload"]["ok"])
        self.assertEqual(reply["payload"]["error"], "sender rejected: unknown-agent")

    def test_turn_timeout_override_is_honored_and_echoed_everywhere(self) -> None:
        engine = RecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion", "--turn-timeout", "60", "--turn-timeout-max", "300")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-timeout", payload={"task": "Work.", "turn_timeout": 30}))
            bridge.join_active_thread()

        self.assertEqual(engine.timeouts, [30])
        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        for record in (reply, result):
            self.assertEqual(record["turn_timeout_requested"], 30)
            self.assertEqual(record["turn_timeout_served"], 30)
        started = next(fields for _, fields in fake.statuses if fields["phase"] == "starting")
        self.assertEqual(started["turn_timeout_requested"], 30)
        self.assertEqual(started["turn_timeout_served"], 30)

    def test_absent_turn_timeout_uses_default_and_omits_echoes(self) -> None:
        engine = RecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion", "--turn-timeout", "60", "--turn-timeout-max", "300")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-timeout-default"))
            bridge.join_active_thread()

        self.assertEqual(engine.timeouts, [60])
        self.assertNotIn("turn_timeout_requested", json.loads(fake.replies[-1][1])["payload"])
        self.assertNotIn("turn_timeout_served", json.loads(fake.results[-1][1]))
        self.assertTrue(all("turn_timeout_requested" not in fields for _, fields in fake.statuses))

    def test_invalid_and_over_max_turn_timeouts_refuse_with_correlated_reply(self) -> None:
        for index, value in enumerate((True, "x", 0, -5, 301)):
            with self.subTest(value=value):
                bridge = make_bridge("--turn-timeout", "60", "--turn-timeout-max", "300")
                fake = FakeRedis()
                bridge.redis = fake  # type: ignore[assignment]
                bridge.pool.acquire = mock.Mock(side_effect=AssertionError("engine work must not start"))  # type: ignore[method-assign]

                bridge.handle_raw(request_json(f"req-invalid-{index}", payload={"task": "Work.", "turn_timeout": value}))

                self.assertEqual(len(fake.replies), 1)
                reply = json.loads(fake.replies[0][1])
                self.assertEqual(reply["in_reply_to"], f"req-invalid-{index}")
                self.assertFalse(reply["payload"]["ok"])
                rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                self.assertIn(rendered, reply["payload"]["error"])
                self.assertEqual(reply["payload"]["turn_timeout_requested"], value)
                self.assertNotIn("turn_timeout_served", reply["payload"])
                if value == 301:
                    self.assertIn("300", reply["payload"]["error"])

    def test_non_trusted_sender_turn_timeout_refuses_naming_policy(self) -> None:
        for policy in ("human", "reject"):
            with self.subTest(policy=policy):
                bridge = make_bridge("--unknown-sender-policy", policy)
                fake = FakeRedis()
                bridge.redis = fake  # type: ignore[assignment]
                bridge.pool.acquire = mock.Mock(side_effect=AssertionError("engine work must not start"))  # type: ignore[method-assign]

                request_id = f"req-{policy}-timeout"
                bridge.handle_raw(request_json(request_id, sender="other-agent", payload={"task": "Work.", "turn_timeout": 30}))

                reply = json.loads(fake.replies[0][1])
                self.assertEqual(reply["in_reply_to"], request_id)
                self.assertIn(f"policy={policy}", reply["payload"]["error"])
                self.assertEqual(reply["payload"]["turn_timeout_requested"], 30)
                self.assertNotIn("turn_timeout_served", reply["payload"])

    def test_commit_message_helper_uses_seat_default_not_task_override(self) -> None:
        class HelperEngine(RecordingEngine):
            supports_continuation = True

        engine = HelperEngine("feat: describe work")
        bridge = make_bridge("--turn-timeout", "60", "--turn-timeout-max", "300")
        envelope = Envelope.from_json(
            request_json("req-helper-timeout", payload={"task": "Work.", "turn_timeout": 240})
        )
        bridge.commit_message_from_model = True

        message = bridge._commit_message(envelope, engine, "trusted")

        self.assertEqual(message, "feat: describe work")
        self.assertEqual(engine.timeouts, [60])

    def test_startup_refuses_invalid_turn_timeout_configurations(self) -> None:
        for values in (("0", "300"), ("60", "0"), ("60", "59")):
            with self.subTest(default=values[0], maximum=values[1]):
                with self.assertRaisesRegex(
                    ValueError,
                    rf"--turn-timeout {values[0]}, --turn-timeout-max {values[1]}",
                ):
                    make_bridge("--turn-timeout", values[0], "--turn-timeout-max", values[1])

    def test_duplicate_request_is_dropped(self) -> None:
        bridge = make_bridge("--dry-run")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=RecordingEngine("unused (dry-run)")):
            bridge.handle_raw(request_json("req-3"))
            bridge.join_active_thread()
            bridge.handle_raw(request_json("req-3"))

        replies = [json.loads(body) for _, body in fake.replies if json.loads(body)["kind"] == "reply"]
        self.assertEqual(len(replies), 1)

    def test_self_message_is_dropped(self) -> None:
        bridge = make_bridge("--dry-run")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        bridge.handle_raw(request_json("req-4", sender="codex-project-c-dev"))

        self.assertEqual(fake.replies, [])
        self.assertEqual(fake.events, [])

    def test_pool_acquire_engine_error_replies_cleanly(self) -> None:
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        bridge.pool.acquire = mock.Mock(side_effect=EngineError("start failed"))  # type: ignore[method-assign]

        self.assertFalse(bridge.handle_raw(request_json("req-engine-start-failed")))

        self.assertEqual(len(fake.replies), 1)
        reply = json.loads(fake.replies[0][1])["payload"]
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "engine-start-failed: start failed")

    def test_cursor_fast_flag_threads_to_cursor_engine(self) -> None:
        bridge = make_bridge("--engine", "cursor-acp", "--cursor-fast")
        engine = bridge.pool._factory()

        self.assertIsInstance(engine, CursorAcpEngine)
        self.assertTrue(engine.fast)

    def test_daily_request_limit_rejects_before_turn(self) -> None:
        # Runs the REAL check_usage_budget (no mock-out): the budget decision is
        # the no-I/O usage_budget module (ARB-B13) and the adapter reads TODAY's
        # usage key, so seeding today's count exercises the whole refusal path.
        from datetime import datetime

        bridge = make_bridge("--dry-run", "--daily-request-limit", "1")
        fake = FakeRedis()
        day = datetime.now().astimezone().strftime("%Y%m%d")
        fake.ints[bridge.redis_config.usage_key(bridge.usage_identity, day, "requests")] = 1
        bridge.redis = fake  # type: ignore[assignment]

        bridge.handle_raw(request_json("req-5"))

        self.assertEqual(fake.events, [])
        self.assertEqual(len(fake.replies), 1)
        reply = json.loads(fake.replies[0][1])
        self.assertEqual(reply["payload"]["error"], "daily-request-limit-reached")

    def test_expect_structured_adds_directive_to_prompt(self) -> None:
        engine = RecordingEngine('Done.\n```json\n{"status":"DONE"}\n```')
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-6", payload={"task": "Dry run task.", "expect_structured": True}))
            bridge.join_active_thread()

        self.assertEqual(len(engine.prompts), 1)
        self.assertIn("Dry run task.", engine.prompts[0])
        self.assertIn("<structured_reply_instructions>", engine.prompts[0])

    def test_without_expect_structured_does_not_change_prompt_or_payload_shape(self) -> None:
        engine = RecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-7", payload={"task": "Plain task."}))
            bridge.join_active_thread()

        self.assertEqual(engine.prompts, ["Plain task."])
        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertNotIn("structured", reply)
        self.assertNotIn("structured", result)
        self.assertIn("thread_id", reply)
        self.assertIn("thread_id", result)
        self.assertIsNone(reply["thread_id"])
        self.assertIsNone(result["thread_id"])

    def test_valid_structured_reply_is_attached_to_reply_and_result(self) -> None:
        engine = RecordingEngine(
            'Done.\n```json\n{"status":"DONE_WITH_CONCERNS","summary":"ok","concerns":["risk"]}\n```'
        )
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-8", payload={"task": "Structured task.", "expect_structured": True}))
            bridge.join_active_thread()

        expected = {"status": "DONE_WITH_CONCERNS", "summary": "ok", "concerns": ["risk"]}
        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertEqual(reply["structured"], expected)
        self.assertEqual(result["structured"], expected)

    def test_invalid_structured_reply_warns_and_attaches_null(self) -> None:
        engine = RecordingEngine("Done with no block.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with (
            mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine),
            self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs,
        ):
            bridge.handle_raw(request_json("req-9", payload={"task": "Structured task.", "expect_structured": True}))
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertIsNone(reply["structured"])
        self.assertIsNone(result["structured"])
        self.assertTrue(
            any(
                "[bridge-warning] structured-reply-parse-failed task_id=req-9 error=missing-structured-block"
                in line
                for line in logs.output
            )
        )

    def test_empty_structured_reply_attaches_null_without_warning(self) -> None:
        engine = RecordingEngine("")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with self.assertNoLogs("agent_redis_bridge.bridge", level="WARNING"):
            with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
                bridge.handle_raw(
                    request_json("req-10", payload={"task": "Structured task.", "expect_structured": True})
                )
                bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertIsNone(reply["structured"])
        self.assertIsNone(result["structured"])

    def test_string_false_expect_structured_does_not_opt_in(self) -> None:
        engine = RecordingEngine('Done.\n```json\n{"status":"DONE"}\n```')
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-11", payload={"task": "Plain task.", "expect_structured": "false"}))
            bridge.join_active_thread()

        self.assertEqual(engine.prompts, ["Plain task."])
        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertNotIn("structured", reply)
        self.assertNotIn("structured", result)

    def test_fresh_context_request_resets_before_first_turn(self) -> None:
        engine = ResetRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-12", payload={"task": "Fresh task.", "fresh_context": True}))
            bridge.join_active_thread()

        self.assertEqual(engine.events, ["reset", "turn:Fresh task."])

    def test_thread_id_resumes_before_first_turn(self) -> None:
        engine = ThreadRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-thread", payload={"task": "Resume task.", "thread_id": "thread-abc"}))
            bridge.join_active_thread()

        self.assertEqual(engine.events, ["resume:thread-abc", "turn:Resume task."])

    def test_resume_capable_engine_bypasses_affinity_miss(self) -> None:
        engine = ThreadRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.start_engine()
            self.assertIsNone(engine.thread_id)
            bridge.handle_raw(
                request_json("req-thread-bypass", payload={"task": "Resume task.", "thread_id": "missing"})
            )
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["thread_id"], "missing")
        self.assertEqual(engine.events, ["resume:missing", "turn:Resume task."])

    def test_thread_id_suppresses_fresh_context_default(self) -> None:
        engine = ThreadRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion", "--fresh-context-default")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-thread-default", payload={"task": "Resume task.", "thread_id": "thread-abc"}))
            bridge.join_active_thread()

        self.assertEqual(engine.events, ["resume:thread-abc", "turn:Resume task."])

    def test_fork_from_thread_id_forks_before_first_turn_and_replies_child_id(self) -> None:
        engine = ForkRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-fork", payload={"task": "Fork task.", "fork_from_thread_id": "thread-base"}))
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["thread_id"], "thread-child")
        self.assertEqual(result["thread_id"], "thread-child")
        self.assertEqual(engine.events, ["fork:thread-base", "turn:Fork task."])

    def test_fork_from_thread_id_suppresses_fresh_context_default(self) -> None:
        engine = ForkRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion", "--fresh-context-default")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(
                request_json("req-fork-default", payload={"task": "Fork task.", "fork_from_thread_id": "thread-base"})
            )
            bridge.join_active_thread()

        self.assertEqual(engine.events, ["fork:thread-base", "turn:Fork task."])

    def test_unsupported_fork_from_thread_id_fails_without_turn(self) -> None:
        engine = RecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(
                request_json("req-fork-unsupported", payload={"task": "Fork task.", "fork_from_thread_id": "thread-base"})
            )
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "thread-fork-unsupported engine=codex")
        self.assertEqual(result["error"], "thread-fork-unsupported engine=codex")
        self.assertEqual(engine.prompts, [])

    def test_fork_thread_failure_fails_without_turn_and_uses_fork_label(self) -> None:
        engine = ForkRecordingEngine("Done.", fork_error=RuntimeError("missing thread"))
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as logs:
            with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
                bridge.handle_raw(
                    request_json("req-fork-fail", payload={"task": "Fork task.", "fork_from_thread_id": "thread-base"})
                )
                bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "thread-fork-failed: missing thread")
        self.assertEqual(result["error"], "thread-fork-failed: missing thread")
        self.assertEqual(engine.events, ["fork:thread-base"])
        self.assertFalse(any("worktree-setup-failed" in line for line in logs.output))

    def test_unsupported_thread_id_fails_without_turn(self) -> None:
        engine = RecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-thread-unsupported", payload={"task": "Resume task.", "thread_id": "thread-abc"}))
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "thread-continuation-unsupported engine=codex")
        self.assertEqual(result["error"], "thread-continuation-unsupported engine=codex")
        self.assertEqual(engine.prompts, [])

    def test_resume_thread_failure_fails_without_turn(self) -> None:
        engine = ThreadRecordingEngine("Done.", resume_error=RuntimeError("missing thread"))
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-thread-fail", payload={"task": "Resume task.", "thread_id": "thread-abc"}))
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "thread-resume-failed: missing thread")
        self.assertEqual(result["error"], "thread-resume-failed: missing thread")
        self.assertEqual(engine.events, ["resume:thread-abc"])

    def test_reply_and_result_carry_thread_id_when_known(self) -> None:
        engine = ThreadRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-thread-known", payload={"task": "Resume task.", "thread_id": "thread-abc"}))
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        result = json.loads(fake.results[-1][1])
        self.assertEqual(reply["thread_id"], "thread-abc")
        self.assertEqual(result["thread_id"], "thread-abc")

    def test_session_thread_id_affinity_hit_runs_without_resume(self) -> None:
        engine = SessionRecordingEngine("Done.", session_id="session-abc")
        bridge = make_bridge("--engine", "gemini-acp", "--agent-id", "codex-project-c-dev", "--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.start_engine()
            bridge.handle_raw(
                request_json("req-session-hit", payload={"task": "Resume task.", "thread_id": "session-abc"})
            )
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["thread_id"], "session-abc")
        self.assertEqual(engine.prompts, ["Resume task."])

    def test_session_thread_id_affinity_miss_fails_without_turn(self) -> None:
        bridge = make_bridge("--engine", "gemini-acp", "--agent-id", "codex-project-c-dev", "--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        bridge.handle_raw(request_json("req-session-miss", payload={"task": "Resume task.", "thread_id": "missing"}))

        reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "thread-affinity-miss thread=missing")
        self.assertEqual(fake.results, [])

    def test_session_fork_from_thread_id_fails_unsupported_not_affinity(self) -> None:
        engine = SessionRecordingEngine("Done.", session_id="session-abc")
        bridge = make_bridge("--engine", "gemini-acp", "--agent-id", "codex-project-c-dev", "--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.start_engine()
            bridge.handle_raw(
                request_json(
                    "req-session-fork",
                    payload={"task": "Fork task.", "fork_from_thread_id": "missing"},
                )
            )
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "thread-fork-unsupported engine=gemini-acp")
        self.assertEqual(engine.prompts, [])

    def test_session_thread_id_affinity_busy_reports_owner_task(self) -> None:
        engine = SessionRecordingEngine("Done.", session_id="session-abc")
        bridge = make_bridge("--engine", "gemini-acp", "--agent-id", "codex-project-c-dev", "--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.start_engine()
            self.assertIs(bridge.pool.acquire("owner-task", thread_id="session-abc"), engine)
            bridge.handle_raw(
                request_json("req-session-busy", payload={"task": "Resume task.", "thread_id": "session-abc"})
            )

        reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "thread-affinity-busy task=owner-task")
        self.assertEqual(engine.prompts, [])
        bridge.pool.release("owner-task")

    def test_session_thread_id_worktree_rejected_before_pool_work(self) -> None:
        bridge = make_bridge("--engine", "gemini-acp", "--agent-id", "codex-project-c-dev", "--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=AssertionError("unexpected engine")):
            bridge.handle_raw(
                request_json(
                    "req-session-worktree",
                    payload={
                        "task": "Resume task.",
                        "thread_id": "session-abc",
                        "worktree": {"name": "taskA", "cleanup": "keep"},
                    },
                )
            )

        reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "thread-affinity-worktree-incompatible")

    def test_max_parallel_one_affinity_busy_then_hit_after_release(self) -> None:
        engine = SessionRecordingEngine("Done.", session_id="session-abc")
        bridge = make_bridge(
            "--engine",
            "gemini-acp",
            "--agent-id",
            "codex-project-c-dev",
            "--no-enforce-completion",
            "--max-parallel",
            "1",
        )
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.start_engine()
            self.assertIs(bridge.pool.acquire("owner-task", thread_id="session-abc"), engine)
            bridge.handle_raw(
                request_json("req-session-busy", payload={"task": "Resume task.", "thread_id": "session-abc"})
            )
            bridge.pool.release("owner-task")
            bridge.handle_raw(
                request_json("req-session-hit", payload={"task": "Resume task.", "thread_id": "session-abc"})
            )
            bridge.join_active_thread()

        first_reply = json.loads(fake.replies[0][1])["payload"]
        second_reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertEqual(first_reply["error"], "thread-affinity-busy task=owner-task")
        self.assertTrue(second_reply["ok"])
        self.assertEqual(engine.prompts, ["Resume task."])

    def test_fresh_context_absent_and_warm_false_do_not_reset(self) -> None:
        for payload in ({"task": "Warm task."}, {"task": "Warm task.", "fresh_context": False}):
            engine = ResetRecordingEngine("Done.")
            bridge = make_bridge("--no-enforce-completion")
            fake = FakeRedis()
            bridge.redis = fake  # type: ignore[assignment]

            with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
                bridge.handle_raw(request_json("req-warm", payload=payload))
                bridge.join_active_thread()

            self.assertEqual(engine.events, ["turn:Warm task."])

    def test_fresh_context_default_applies_only_when_payload_absent(self) -> None:
        engine = ResetRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion", "--fresh-context-default")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-13", payload={"task": "Default fresh."}))
            bridge.join_active_thread()

        self.assertEqual(engine.events, ["reset", "turn:Default fresh."])

        engine = ResetRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion", "--fresh-context-default")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-14", payload={"task": "Explicit warm.", "fresh_context": False}))
            bridge.join_active_thread()

        self.assertEqual(engine.events, ["turn:Explicit warm."])

    def test_unsupported_fresh_context_warns_and_proceeds(self) -> None:
        engine = RecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with (
            mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine),
            self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs,
        ):
            bridge.handle_raw(request_json("req-15", payload={"task": "Fresh task.", "fresh_context": True}))
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertTrue(reply["ok"])
        self.assertTrue(
            any("[bridge-warning] fresh-context-unsupported engine=codex task_id=req-15" in line for line in logs.output)
        )

    def test_reasoning_effort_applied_to_engine_before_turn(self) -> None:
        engine = EffortRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(
                request_json("req-eff", payload={"task": "Do it.", "reasoning_effort": "xhigh"})
            )
            bridge.join_active_thread()

        # effort must be set BEFORE the turn runs
        self.assertEqual(engine.events, ["effort:xhigh", "turn:Do it."])
        reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertTrue(reply["ok"])

    def test_absent_reasoning_effort_clears_prior_effort_no_leak(self) -> None:
        # A warm seat must not carry a prior dispatch's effort into a later no-effort
        # dispatch: each dispatch's effort is explicit (requested value, or cleared).
        engine = EffortRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-noeff", payload={"task": "Do it."}))
            bridge.join_active_thread()

        # setter is called with None (clear) before the turn, even absent a payload effort
        self.assertEqual(engine.events, ["effort:cleared", "turn:Do it."])

    def test_pi_sdk_affinity_wiring_sets_true_for_thread_affinity(self) -> None:
        engine = AffinityRecordingEngine("Done.")
        bridge = make_bridge("--engine", "pi-sdk")
        bridge.enforce_completion = False
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        envelope = Envelope(
            id="req-pi-affinity",
            sender="claude-project-c-dev",
            branch="manual",
            recipient=bridge.agent_id,
            kind="request",
            sent_at="2026-07-11T12:00:00+01:00",
            payload={"task": "Do it.", "thread_id": "thread-affinity"},
        )

        bridge.process_request(envelope, policy="trusted", engine=engine)

        self.assertEqual(engine.events, ["effort:cleared", "affinity:True", "turn:Do it."])
        self.assertEqual(engine.affinity_requests, [True])

    def test_pi_sdk_affinity_wiring_sets_false_without_thread_affinity(self) -> None:
        engine = AffinityRecordingEngine("Done.")
        bridge = make_bridge("--engine", "pi-sdk")
        bridge.enforce_completion = False
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        envelope = Envelope(
            id="req-pi-no-affinity",
            sender="claude-project-c-dev",
            branch="manual",
            recipient=bridge.agent_id,
            kind="request",
            sent_at="2026-07-11T12:00:00+01:00",
            payload={"task": "Do it."},
        )

        bridge.process_request(envelope, policy="trusted", engine=engine)

        self.assertEqual(engine.events, ["effort:cleared", "affinity:False", "turn:Do it."])
        self.assertEqual(engine.affinity_requests, [False])

    def test_unsupported_reasoning_effort_warns_and_proceeds(self) -> None:
        engine = RecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with (
            mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine),
            self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs,
        ):
            bridge.handle_raw(
                request_json("req-eff-x", payload={"task": "Do it.", "reasoning_effort": "high"})
            )
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertTrue(reply["ok"])
        self.assertTrue(
            any("reasoning-effort-unsupported engine=codex task_id=req-eff-x" in line for line in logs.output)
        )

    def test_invalid_reasoning_effort_warns_and_ignores(self) -> None:
        engine = EffortRecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with (
            mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine),
            self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs,
        ):
            bridge.handle_raw(
                request_json("req-eff-bad", payload={"task": "Do it.", "reasoning_effort": "turbo"})
            )
            bridge.join_active_thread()

        # bad value is ignored (CLI is the strict gate); the turn still runs
        self.assertEqual(engine.events, ["turn:Do it."])
        self.assertTrue(
            any("reasoning-effort-invalid" in line for line in logs.output)
        )

    def test_reset_context_failure_warns_and_proceeds(self) -> None:
        engine = ResetRecordingEngine("Done.", reset_error=RuntimeError("boom"))
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with (
            mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine),
            self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs,
        ):
            bridge.handle_raw(request_json("req-16", payload={"task": "Fresh task.", "fresh_context": True}))
            bridge.join_active_thread()

        reply = json.loads(fake.replies[-1][1])["payload"]
        self.assertTrue(reply["ok"])
        self.assertEqual(engine.events, ["reset", "turn:Fresh task."])
        self.assertTrue(
            any(
                "[bridge-warning] fresh-context-reset-failed engine=codex task_id=req-16 error=boom" in line
                for line in logs.output
            )
        )

    def test_role_profile_wraps_codex_style_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "reviewer.md"
            profile.write_text("Review strictly.", encoding="utf-8")
            engine = RecordingEngine("Done.")
            bridge = make_bridge("--no-enforce-completion", "--role-profile-file", str(profile))
            fake = FakeRedis()
            bridge.redis = fake  # type: ignore[assignment]

            with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
                bridge.handle_raw(request_json("req-17", payload={"task": "Inspect this."}))
                bridge.join_active_thread()

        self.assertEqual(len(engine.prompts), 1)
        self.assertTrue(engine.prompts[0].startswith("<system_guidance>\nReview strictly.\n</system_guidance>"))
        self.assertIn("\n\nInspect this.", engine.prompts[0])

    def test_native_role_profile_engine_is_not_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "reviewer.md"
            profile.write_text("Review strictly.", encoding="utf-8")
            engine = NativeRoleProfileEngine("Done.")
            bridge = make_bridge("--no-enforce-completion", "--role-profile-file", str(profile))
            fake = FakeRedis()
            bridge.redis = fake  # type: ignore[assignment]

            with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
                bridge.handle_raw(request_json("req-18", payload={"task": "Inspect this."}))
                bridge.join_active_thread()

        self.assertEqual(engine.prompts, ["Inspect this."])

    def test_no_role_profile_leaves_prompt_unchanged(self) -> None:
        engine = RecordingEngine("Done.")
        bridge = make_bridge("--no-enforce-completion")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.handle_raw(request_json("req-19", payload={"task": "Inspect this."}))
            bridge.join_active_thread()

        self.assertEqual(engine.prompts, ["Inspect this."])

    def test_unavailable_role_profile_warns_and_runs_bare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing-role-profile-for-bridge-test.md"
            engine = RecordingEngine("Done.")
            with self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs:
                bridge = make_bridge("--no-enforce-completion", "--role-profile-file", str(missing))
                fake = FakeRedis()
                bridge.redis = fake  # type: ignore[assignment]

                with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
                    bridge.handle_raw(request_json("req-20", payload={"task": "Inspect this."}))
                    bridge.join_active_thread()

        self.assertEqual(engine.prompts, ["Inspect this."])
        self.assertTrue(any("[bridge-warning] role-profile-unavailable path=" in line for line in logs.output))

    def test_role_profile_composes_with_expect_structured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "reviewer.md"
            profile.write_text("Review strictly.", encoding="utf-8")
            engine = RecordingEngine("Done.")
            bridge = make_bridge("--no-enforce-completion", "--role-profile-file", str(profile))
            fake = FakeRedis()
            bridge.redis = fake  # type: ignore[assignment]

            with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
                bridge.handle_raw(
                    request_json("req-21", payload={"task": "Inspect this.", "expect_structured": True})
                )
                bridge.join_active_thread()

        prompt = engine.prompts[0]
        self.assertLess(prompt.index("<system_guidance>"), prompt.index("Inspect this."))
        self.assertLess(prompt.index("Inspect this."), prompt.index("<structured_reply_instructions>"))


class RecordingEngine:
    supports_continuation = False

    def __init__(self, result: str) -> None:
        self.result = result
        self.prompts: list[str] = []
        self.timeouts: list[int] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        self.prompts.append(task)
        self.timeouts.append(timeout)
        return TurnResult(ok=True, result=self.result)


class ResetRecordingEngine(RecordingEngine):
    def __init__(self, result: str, reset_error: Exception | None = None) -> None:
        super().__init__(result)
        self.reset_error = reset_error
        self.events: list[str] = []

    def reset_context(self) -> str:
        self.events.append("reset")
        if self.reset_error is not None:
            raise self.reset_error
        return "fresh"

    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        self.prompts.append(task)
        self.events.append(f"turn:{task}")
        return TurnResult(ok=True, result=self.result)


class ThreadRecordingEngine(ResetRecordingEngine):
    supports_thread_resume = True
    def __init__(self, result: str, resume_error: Exception | None = None) -> None:
        super().__init__(result)
        self.resume_error = resume_error
        self.thread_id: str | None = None

    def resume_thread(self, thread_id: str) -> str:
        self.events.append(f"resume:{thread_id}")
        if self.resume_error is not None:
            raise self.resume_error
        self.thread_id = thread_id
        return thread_id


class ForkRecordingEngine(ResetRecordingEngine):
    def __init__(self, result: str, fork_error: Exception | None = None) -> None:
        super().__init__(result)
        self.fork_error = fork_error
        self.thread_id: str | None = None

    def fork_thread(self, thread_id: str) -> str:
        self.events.append(f"fork:{thread_id}")
        if self.fork_error is not None:
            raise self.fork_error
        self.thread_id = "thread-child"
        return self.thread_id


class EffortRecordingEngine(ResetRecordingEngine):
    def set_turn_reasoning_effort(self, effort: str | None) -> None:
        self.events.append(f"effort:{effort}" if effort is not None else "effort:cleared")


class AffinityRecordingEngine(EffortRecordingEngine):
    def __init__(self, result: str) -> None:
        super().__init__(result)
        self.affinity_requests: list[bool] = []
        self.thread_id = "thread-affinity"

    def set_turn_thread_affinity(self, requested: bool) -> None:
        self.affinity_requests.append(requested)
        self.events.append(f"affinity:{requested}")


class SessionRecordingEngine(RecordingEngine):
    def __init__(self, result: str, session_id: str) -> None:
        super().__init__(result)
        self.session_id = session_id


class NativeRoleProfileEngine(RecordingEngine):
    consumes_role_profile = True


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
                "--sender-policy",
                "claude-project-c-dev=trusted",
                *extra,
            ]
        )
        return Bridge(args)


def request_json(
    request_id: str,
    sender: str = "claude-project-c-dev",
    payload: dict[str, object] | None = None,
) -> str:
    return json.dumps(
        {
            "id": request_id,
            "from": sender,
            "branch": "manual",
            "to": "codex-project-c-dev",
            "kind": "request",
            "sent_at": "2026-04-26T19:00:00+01:00",
            "payload": payload if payload is not None else {"task": "Dry run task."},
        },
        separators=(",", ":"),
    )


if __name__ == "__main__":
    unittest.main()

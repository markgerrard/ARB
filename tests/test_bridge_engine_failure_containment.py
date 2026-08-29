"""Engine failures and observability failures must never lose the reply.

Audit 2026-07-07 (ASK-1, AGY-1, IMP-4): run_engine caught only EngineError, so a
non-EngineError escaping an engine (claude_agent_sdk.ProcessError, UnicodeDecodeError,
FileNotFoundError from a missing binary, ...) killed the worker thread with no reply and
no task_finished. handle_progress performed bare Redis writes, so a transient
Valkey/TLS blip inside an engine's on_event callback killed the agy-print poll thread
(observability dark) or the whole turn.
"""

from pathlib import Path
import tempfile
import unittest

from agent_redis_bridge.bridge import Bridge, build_parser
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.engines.base import TurnResult


class ExplodingEngine:
    """Engine whose turn raises a non-EngineError, as a broken SDK/subprocess would."""

    supports_continuation = False

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.healthy = True

    def start(self) -> None:  # pragma: no cover - not started in these tests
        pass

    def stop(self) -> None:  # pragma: no cover
        pass

    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        raise self.exc


class CallbackEngine:
    """Engine that forwards one event through on_event, like agy-print's poller."""

    supports_continuation = False

    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        on_event("command_finished", {"command": "probe", "status": "DONE", "exit_code": 0})
        return TurnResult(ok=True, result="done")


class RaisingRedis:
    """Every write raises, as during a Valkey outage. Reads are unused here."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise ConnectionError(f"redis down ({name})")

        return _raise


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


def request(task_id: str = "task-contain-1") -> Envelope:
    return Envelope(
        id=task_id,
        sender="claude-project-c-dev",
        branch="dev",
        recipient="codex-project-c-dev",
        kind="request",
        sent_at="2026-07-08T00:00:00+00:00",
        payload={"task": "work"},
        run_id="run-1",
    )


class RunEngineNonEngineErrorTest(unittest.TestCase):
    def test_non_engine_error_returns_failed_turn_result_instead_of_raising(self) -> None:
        bridge = make_bridge()
        engine = ExplodingEngine(ValueError("boom mid-turn"))

        result = bridge.run_engine(request(), policy="trusted", engine=engine)

        self.assertFalse(result.ok)
        self.assertIn("ValueError", result.error)
        self.assertIn("boom mid-turn", result.error)

    def test_non_engine_error_marks_engine_unhealthy_so_pool_discards_it(self) -> None:
        bridge = make_bridge()
        engine = ExplodingEngine(RuntimeError("sdk transport died"))

        bridge.run_engine(request(), policy="trusted", engine=engine)

        self.assertFalse(engine.healthy)


class HandleProgressRedisFailureTest(unittest.TestCase):
    def test_redis_outage_inside_on_event_does_not_kill_the_turn(self) -> None:
        bridge = make_bridge()
        bridge.redis = RaisingRedis()  # type: ignore[assignment]
        engine = CallbackEngine()

        result = bridge.run_engine(request(), policy="trusted", engine=engine)

        self.assertTrue(result.ok)
        self.assertEqual(result.result, "done")

    def test_handle_progress_still_records_stall_progress_when_redis_is_down(self) -> None:
        bridge = make_bridge()
        bridge.redis = RaisingRedis()  # type: ignore[assignment]
        env = request()
        bridge.stall_watch.start(env.id, now=0.0)

        bridge.handle_progress(env, "command_finished", {"command": "x"}, policy="trusted")

        # In-memory stall bookkeeping must happen before (and independently of)
        # the Redis emission, so a dark bus never manufactures a false stall.
        state = bridge.stall_watch._tasks.get(env.id)
        self.assertIsNotNone(state)
        self.assertGreater(state.last_progress_ts, 0.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class HandleProgressResumeBlipTest(unittest.TestCase):
    def test_redis_outage_during_stall_resume_clear_does_not_escape(self) -> None:
        # GLM panel P2 (2026-07-08): _record_stall_progress ran OUTSIDE the
        # emission try; its resume path (_clear_stalled_at -> bare hdel_key)
        # could still raise out of an engine poll thread on a Redis blip.
        bridge = make_bridge()
        bridge.redis = RaisingRedis()  # type: ignore[assignment]
        env = request("task-resume-blip")
        bridge.stall_watch.start(env.id, now=0.0)
        # Force the task into the stalled state so the next progress RESUMES it.
        assert bridge.stall_watch.check(env.id, now=10_000.0) is not None

        bridge.handle_progress(env, "command_finished", {"command": "x"}, policy="trusted")

        # In-memory resume must have happened despite the dark bus.
        self.assertFalse(bridge.stall_watch.is_stalled(env.id))

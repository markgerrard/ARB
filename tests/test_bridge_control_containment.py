"""A steer/cancel against a broken engine must never crash the daemon (audit CDX-2).

Chain pre-fix: codex `_send` let a write to a closed pipe raise raw BrokenPipeError;
`handle_control` caught only EngineError; the control-lane `handle_raw(ctl_raw)` call in
`inbox_loop` was unguarded (unlike the request lane) — so one late cancel against a dead
engine propagated to main() and exited the daemon, killing every in-flight parallel task.
"""

import json
from pathlib import Path
import tempfile
import unittest

from agent_redis_bridge.bridge import Bridge, build_parser
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.codex import CodexEngine


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


class RecordingRedis:
    def __init__(self) -> None:
        self.pushes: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict]] = []
        self.statuses: list[tuple[str, dict]] = []

    def lpush(self, agent_id: str, body: str) -> None:
        self.pushes.append((agent_id, body))

    def lpush_key(self, key: str, body: str, *, trim=None) -> None:
        self.pushes.append((key, body))

    def xadd(self, key: str, fields: dict, **kwargs) -> str:
        self.events.append((key, fields))
        return "1-0"

    def hset_key(self, key: str, fields: dict, *, ttl=None) -> None:
        self.statuses.append((key, fields))

    def hdel_key(self, key: str, *fields) -> None:
        pass

    def expire(self, key: str, ttl: int) -> None:
        pass


class DeadPipeStdin:
    def write(self, value: str) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:  # pragma: no cover - write raises first
        pass


class DeadPipeProcess:
    def __init__(self) -> None:
        self.stdin = DeadPipeStdin()
        self.stdout = iter([])
        self.stderr = iter([])

    def poll(self):
        return None


class BrokenSteerEngine:
    def steer(self, message: str) -> str:
        raise BrokenPipeError(32, "Broken pipe")

    def interrupt(self) -> str:
        raise BrokenPipeError(32, "Broken pipe")


def steer_envelope(task_id: str) -> Envelope:
    return Envelope(
        id="ctl-1",
        sender="claude-project-c-dev",
        branch="dev",
        recipient="codex-project-c-dev",
        kind="steer",
        sent_at="2026-07-08T00:00:00+00:00",
        payload={"task_id": task_id, "message": "change course"},
        run_id="run-1",
    )


def cancel_envelope(task_id: str) -> Envelope:
    return Envelope(
        id="ctl-2",
        sender="claude-project-c-dev",
        branch="dev",
        recipient="codex-project-c-dev",
        kind="cancel",
        sent_at="2026-07-08T00:00:00+00:00",
        payload={"task_id": task_id},
        run_id="run-1",
    )


class RecordingControlEngine:
    def __init__(self) -> None:
        self.steers: list[str] = []
        self.interrupts = 0

    def steer(self, message: str) -> str:
        self.steers.append(message)
        return "turn-steer"

    def interrupt(self) -> str:
        self.interrupts += 1
        return "turn-cancel"


class CodexSendDeadPipeTest(unittest.TestCase):
    def test_steer_on_dead_pipe_raises_engine_error_not_oserror(self) -> None:
        eng = CodexEngine(cwd="/tmp/x", model=None, approval_policy="on-request", sandbox="workspace-write")
        eng.process = DeadPipeProcess()
        eng.thread_id = "th-1"
        eng.active_turn_id = "turn-1"

        with self.assertRaises(EngineError):
            eng.steer("stop that")

    def test_interrupt_on_dead_pipe_raises_engine_error_not_oserror(self) -> None:
        eng = CodexEngine(cwd="/tmp/x", model=None, approval_policy="on-request", sandbox="workspace-write")
        eng.process = DeadPipeProcess()
        eng.thread_id = "th-1"
        eng.active_turn_id = "turn-1"

        with self.assertRaises(EngineError):
            eng.interrupt()


class HandleControlContainmentTest(unittest.TestCase):
    def _bridge_with_broken_engine(self) -> tuple[Bridge, RecordingRedis, Envelope]:
        bridge = make_bridge()
        redis = RecordingRedis()
        bridge.redis = redis  # type: ignore[assignment]
        request = Envelope(
            id="task-77",
            sender="claude-project-c-dev",
            branch="dev",
            recipient="codex-project-c-dev",
            kind="request",
            sent_at="2026-07-08T00:00:00+00:00",
            payload={"task": "long work"},
            run_id="run-1",
        )
        with bridge.active_lock:
            bridge.active_requests[request.id] = request
            bridge.task_engines[request.id] = BrokenSteerEngine()
        return bridge, redis, request

    def _milestones(self, redis: RecordingRedis, event: str) -> list[dict]:
        out = []
        for _, body in redis.pushes:
            env = json.loads(body)
            if env.get("payload", {}).get("event") == event:
                out.append(env)
        return out

    def test_steer_against_broken_engine_reports_steer_failed_not_crash(self) -> None:
        bridge, redis, request = self._bridge_with_broken_engine()

        bridge.handle_control(steer_envelope(request.id))  # must not raise

        self.assertTrue(self._milestones(redis, "steer_failed"))
        self.assertFalse(self._milestones(redis, "steer_sent"))

    def test_locked_panel_rejects_steer_and_cancel_and_tees_both_events(self) -> None:
        bridge = make_bridge()
        redis = RecordingRedis()
        bridge.redis = redis  # type: ignore[assignment]
        request = Envelope(
            id="task-locked",
            sender="claude-project-c-dev",
            branch="dev",
            recipient="codex-project-c-dev",
            kind="request",
            sent_at="2026-07-08T00:00:00+00:00",
            payload={"task": "certify", "certifying": True},
            run_id="run-1",
        )
        engine = RecordingControlEngine()
        with bridge.active_lock:
            bridge.active_requests[request.id] = request
            bridge.task_engines[request.id] = engine

        bridge.handle_control(steer_envelope(request.id))
        bridge.handle_control(cancel_envelope(request.id))

        self.assertEqual(engine.steers, [])
        self.assertEqual(engine.interrupts, 0)
        self.assertTrue(self._milestones(redis, "steer_rejected"))
        self.assertTrue(self._milestones(redis, "cancel_rejected"))
        task_event_types = [fields["type"] for _, fields in redis.events if "type" in fields]
        self.assertIn("steer_rejected", task_event_types)
        self.assertIn("cancel_rejected", task_event_types)


class ControlDrainContainmentTest(unittest.TestCase):
    def test_control_drain_survives_handle_raw_raising(self) -> None:
        bridge = make_bridge()
        redis = RecordingRedis()
        bridge.redis = redis  # type: ignore[assignment]
        control = steer_envelope("task-77").to_json()
        served = [control]

        def lpop_control(agent_id: str):
            return served.pop(0) if served else None

        bridge.redis.lpop_control = lpop_control  # type: ignore[attr-defined]
        bridge.handle_raw = lambda raw, **kw: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]

        bridge._drain_control_lane()  # must swallow, log, and keep the daemon alive

        self.assertEqual(served, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

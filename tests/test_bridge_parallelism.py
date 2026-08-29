from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_redis_bridge.bridge import Bridge, build_parser
from agent_redis_bridge.engines.base import TurnResult


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

    def incrby(self, key: str, amount: int, *, ttl: int | None = None) -> int:
        self.ints[key] = self.ints.get(key, 0) + amount
        return self.ints[key]


class GatedEngine:
    """A fake engine that holds its concurrency slot until the test releases a
    shared gate. This makes the cap tests DETERMINISTIC: slot occupancy is
    controlled by the gate, not by a real codex app-server's spawn time racing a
    wall-clock sleep (the old --dry-run approach was flaky precisely because
    `pool.acquire` -> `engine.start()` spawns a real codex process whose latency
    sometimes exceeded the 1s dry-run sleep, so an earlier worker released its
    slot before the next request arrived)."""

    def __init__(self, cwd: str, gate: threading.Event) -> None:
        self.cwd = cwd
        self._gate = gate

    def start(self) -> None:  # fast, no real process
        pass

    def stop(self) -> None:
        pass

    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        # Block until the test opens the gate, so the slot stays occupied for the
        # whole window in which the test fires the next request(s).
        if not self._gate.wait(timeout=10):
            return TurnResult(ok=False, result="", error="gate timeout (test bug)")
        return TurnResult(ok=True, result="done")


class MaxParallelCapTest(unittest.TestCase):
    """Cap enforcement, deterministically: slots are held by a gate, not by timing."""

    def _gated_factory(self, gate: threading.Event):
        def factory(args, *, cwd):
            return GatedEngine(cwd, gate)
        return factory

    def _replies(self, fake: FakeRedis) -> dict[str, dict]:
        out = {}
        for _, body in fake.replies:
            r = json.loads(body)
            if r["kind"] == "reply":
                out[r["in_reply_to"]] = r["payload"]
        return out

    def test_max_parallel_one_rejects_second_concurrent_request(self) -> None:
        gate = threading.Event()
        bridge = make_bridge()
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=self._gated_factory(gate)):
            bridge.handle_raw(request_json("req-A"))  # takes the only slot, blocks on gate
            bridge.handle_raw(request_json("req-B"))  # cap=1 -> rejected synchronously
            gate.set()
            bridge.join_active_threads()
        reps = self._replies(bridge.redis)  # type: ignore[arg-type]
        self.assertTrue(reps["req-A"]["ok"])
        self.assertFalse(reps["req-B"]["ok"])
        self.assertIn("bridge busy", reps["req-B"]["error"])

    def test_max_parallel_two_accepts_both_concurrent_requests(self) -> None:
        gate = threading.Event()
        bridge = make_bridge("--max-parallel", "2")
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=self._gated_factory(gate)):
            bridge.handle_raw(request_json("req-A"))
            bridge.handle_raw(request_json("req-B"))
            gate.set()
            bridge.join_active_threads()
        reps = self._replies(bridge.redis)  # type: ignore[arg-type]
        self.assertTrue(reps["req-A"]["ok"])
        self.assertTrue(reps["req-B"]["ok"])

    def test_max_parallel_two_third_request_busy_rejected(self) -> None:
        gate = threading.Event()
        bridge = make_bridge("--max-parallel", "2")
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=self._gated_factory(gate)):
            bridge.handle_raw(request_json("req-A"))  # slot 1, blocks
            bridge.handle_raw(request_json("req-B"))  # slot 2, blocks
            bridge.handle_raw(request_json("req-C"))  # cap=2 -> rejected synchronously
            gate.set()
            bridge.join_active_threads()
        reps = self._replies(bridge.redis)  # type: ignore[arg-type]
        self.assertTrue(reps["req-A"]["ok"])
        self.assertTrue(reps["req-B"]["ok"])
        self.assertFalse(reps["req-C"]["ok"])
        self.assertIn("bridge busy", reps["req-C"]["error"])


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


def request_json(request_id: str, sender: str = "claude-project-c-dev") -> str:
    return json.dumps(
        {
            "id": request_id,
            "from": sender,
            "branch": "manual",
            "to": "codex-project-c-dev",
            "kind": "request",
            "sent_at": "2026-04-26T19:00:00+01:00",
            "payload": {"task": "Dry run task."},
        },
        separators=(",", ":"),
    )


if __name__ == "__main__":
    unittest.main()

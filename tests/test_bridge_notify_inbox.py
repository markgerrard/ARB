from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_redis_bridge.bridge as bridge_module
from agent_redis_bridge.bridge import Bridge, build_parser
from test_bridge_handle_raw import RecordingEngine


def _stub_engine():
    # Engine stubbed: the pool spawns the engine binary before run_engine's
    # dry-run short-circuit, so without this these tests need `codex` on PATH.
    return mock.patch(
        "agent_redis_bridge.bridge.build_engine",
        return_value=RecordingEngine("unused (dry-run)"),
    )


class FakeRedis:
    """Test double tracking which key receives each LPUSH."""

    def __init__(self) -> None:
        self.pushes: list[tuple[str, str]] = []  # (key, body)
        self.events: list[tuple[str, dict[str, str]]] = []
        self.statuses: list[tuple[str, dict[str, str]]] = []
        self.results: list[tuple[str, str]] = []
        self.ints: dict[str, int] = {}

    def lpush(self, agent_id: str, body: str) -> None:
        self.pushes.append(("INBOX", body, agent_id))

    def lpush_key(self, key: str, body: str, *, trim: int | None = None) -> None:
        self.pushes.append(("NOTIFY", body, key))

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
                "--dry-run",
                *extra,
            ]
        )
        return Bridge(args)


class NotifyInboxRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_notify_inbox = os.environ.pop("BRIDGE_NOTIFY_INBOX", None)

    def tearDown(self) -> None:
        os.environ.pop("BRIDGE_NOTIFY_INBOX", None)
        try:
            importlib.reload(bridge_module)
        finally:
            if self._original_notify_inbox is not None:
                os.environ["BRIDGE_NOTIFY_INBOX"] = self._original_notify_inbox

    def test_default_routes_notifies_to_inbox(self) -> None:
        bridge = make_bridge()
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with _stub_engine():
            bridge.handle_raw(request_json("req-notify-A"))
            bridge.join_active_threads()

        notify_pushes = [p for p in fake.pushes if json.loads(p[1]).get("kind") == "notify"]
        self.assertTrue(notify_pushes, "expected at least one notify push")
        for push in notify_pushes:
            destination, _body, _key_or_agent = push
            self.assertEqual(destination, "INBOX")

    def test_flag_off_routes_notifies_to_separate_key(self) -> None:
        bridge = make_bridge("--notify-inbox", "0")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with _stub_engine():
            bridge.handle_raw(request_json("req-notify-B"))
            bridge.join_active_threads()

        notify_pushes = [p for p in fake.pushes if json.loads(p[1]).get("kind") == "notify"]
        self.assertTrue(notify_pushes, "expected at least one notify push")
        for push in notify_pushes:
            destination, _body, key = push
            self.assertEqual(destination, "NOTIFY")
            self.assertTrue(key.endswith(":notify_inbox"), f"expected :notify_inbox suffix, got {key}")

    def test_flag_off_still_routes_replies_to_inbox(self) -> None:
        bridge = make_bridge("--notify-inbox", "0")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        with _stub_engine():
            bridge.handle_raw(request_json("req-notify-C"))
            bridge.join_active_threads()

        reply_pushes = [p for p in fake.pushes if json.loads(p[1]).get("kind") == "reply"]
        self.assertTrue(reply_pushes, "expected at least one reply push")
        for push in reply_pushes:
            destination, _body, _ = push
            self.assertEqual(destination, "INBOX")

    def test_env_var_overrides_default(self) -> None:
        with mock.patch.dict(os.environ, {"BRIDGE_NOTIFY_INBOX": "0"}):
            reloaded = importlib.reload(bridge_module)
            parser = reloaded.build_parser()
            args = parser.parse_args([])
        self.assertEqual(args.notify_inbox, 0)

    def test_cli_flag_overrides_env(self) -> None:
        with mock.patch.dict(os.environ, {"BRIDGE_NOTIFY_INBOX": "0"}):
            reloaded = importlib.reload(bridge_module)
            parser = reloaded.build_parser()
            args = parser.parse_args(["--notify-inbox", "1"])
        self.assertEqual(args.notify_inbox, 1)


if __name__ == "__main__":
    unittest.main()

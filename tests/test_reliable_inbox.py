import json
import unittest
from unittest import mock

from redis.exceptions import ConnectionError as RedisConnectionError, ResponseError

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.redis_io import OwnershipLostError
from test_bridge_handle_raw import RecordingEngine, make_bridge, request_json


class ReliableRedis:
    def __init__(self) -> None:
        self.inbox: list[str] = []
        self.processing: list[str] = []
        self.replies: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict[str, str]]] = []
        self.statuses: list[tuple[str, dict[str, str]]] = []
        self.results: list[tuple[str, str]] = []
        self.ints: dict[str, int] = {}
        self.blmove_unsupported = False
        self.blmove_calls = 0
        self.blpop_calls = 0
        self.remove_calls: list[str] = []
        self.claim_calls: list[tuple[str, str, str, int]] = []
        self.clear_claim_calls: list[tuple[str, str]] = []
        self.consumer_heartbeats: list[tuple[str, str, int]] = []
        self.consumer_error: Exception | None = None
        self.on_blmove = None
        self.blmove_error_message = "unknown command 'BLMOVE'"

    def lpush(self, agent_id: str, body: str) -> None:
        self.replies.append((agent_id, body))

    def lpop_control(self, agent_id: str) -> str | None:
        return None

    def consumer_heartbeat(self, agent_id: str, owner_token: str, ttl: int) -> None:
        if self.consumer_error is not None:
            error, self.consumer_error = self.consumer_error, None
            raise error
        self.consumer_heartbeats.append((agent_id, owner_token, ttl))

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

    def blmove_to_processing(self, agent_id: str, timeout: int) -> str | None:
        self.blmove_calls += 1
        if self.blmove_unsupported:
            raise ResponseError(self.blmove_error_message)
        if not self.inbox:
            return None
        raw = self.inbox.pop(0)
        self.processing.append(raw)
        if self.on_blmove is not None:
            self.on_blmove()
        return raw

    def blpop(self, agent_id: str, timeout: int) -> str | None:
        self.blpop_calls += 1
        if not self.inbox:
            return None
        return self.inbox.pop(0)

    def recover_processing_to_inbox(self, agent_id: str) -> str | None:
        if not self.processing:
            return None
        raw = self.processing.pop()
        self.inbox.insert(0, raw)
        return raw

    def recover_validated(self, agent_id: str, body: str) -> int:
        if not self.processing or self.processing[-1] != body:
            return 0
        self.processing.pop()
        self.inbox.insert(0, body)
        return 1

    def peek_processing(self, agent_id: str) -> str | None:
        return self.processing[-1] if self.processing else None

    def claim_processing(self, agent_id: str, body: str, owner_token: str, ttl: int) -> None:
        self.claim_calls.append((agent_id, body, owner_token, ttl))

    def clear_processing_claim(self, agent_id: str, body: str) -> None:
        self.clear_claim_calls.append((agent_id, body))

    def remove_processing(self, agent_id: str, body: str, *, owner_token: str) -> int:
        self.remove_calls.append(body)
        try:
            self.processing.remove(body)
        except ValueError:
            return 0
        return 1


class ReliableInboxTest(unittest.TestCase):
    def setUp(self) -> None:
        # Engine stubbed class-wide: inbox_loop acquires (spawns) the engine
        # before run_engine's dry-run short-circuit, so without this every
        # request-handling test here needs a real `codex` binary on PATH.
        patcher = mock.patch(
            "agent_redis_bridge.bridge.build_engine",
            return_value=RecordingEngine("unused (dry-run)"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reliable_pop_removes_processing_after_handled_envelope(self) -> None:
        bridge = make_bridge("--dry-run", "--once")
        fake = ReliableRedis()
        raw = request_json("req-reliable")
        fake.inbox.append(raw)
        bridge.redis = fake  # type: ignore[assignment]

        bridge.inbox_loop()

        self.assertEqual(fake.processing, [])
        self.assertEqual(fake.remove_calls, [raw])
        self.assertEqual(len(reply_envelopes(fake)), 1)

    def test_transient_consumer_heartbeat_error_does_not_crash_daemon(self) -> None:
        bridge = make_bridge("--dry-run", "--once")
        fake = ReliableRedis()
        fake.consumer_error = RedisConnectionError("redis blip")
        fake.inbox.append(request_json("req-after-heartbeat-blip"))
        bridge.redis = fake  # type: ignore[assignment]

        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as logs:
            bridge.inbox_loop()

        self.assertEqual(len(reply_envelopes(fake)), 1)
        self.assertTrue(any("consumer-heartbeat-fail redis blip" in line for line in logs.output))

    def test_consumer_ownership_loss_stops_before_consuming(self) -> None:
        bridge = make_bridge("--dry-run", "--once")
        fake = ReliableRedis()
        fake.consumer_error = OwnershipLostError("successor owns identity")
        raw = request_json("req-must-stay-queued")
        fake.inbox.append(raw)
        bridge.redis = fake  # type: ignore[assignment]

        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as logs:
            bridge.inbox_loop()

        self.assertEqual(fake.inbox, [raw])
        self.assertTrue(bridge.stop_event.is_set())
        self.assertTrue(any("consumer-ownership-lost" in line for line in logs.output))

    def test_once_skips_oversize_without_counting_then_processes_valid_envelope(self) -> None:
        valid = request_json("req-valid-after-oversize")
        bridge = make_bridge("--dry-run", "--once", "--max-message-bytes", str(len(valid.encode()) + 10))
        fake = ReliableRedis()
        oversize = "x" * (bridge.args.max_message_bytes + 1)
        fake.inbox.extend([oversize, valid])
        bridge.redis = fake  # type: ignore[assignment]

        bridge.inbox_loop()

        self.assertEqual(fake.processing, [])
        self.assertEqual(fake.remove_calls, [oversize, valid])
        replies = reply_envelopes(fake)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["in_reply_to"], "req-valid-after-oversize")

    def test_handler_exception_still_removes_processing_entry(self) -> None:
        bridge = make_bridge("--once")
        fake = ReliableRedis()
        raw = request_json("req-raises")
        fake.inbox.append(raw)
        bridge.redis = fake  # type: ignore[assignment]

        def raise_from_handler(_raw: str, **_kwargs: object) -> None:
            raise RuntimeError("boom")

        bridge.handle_raw = raise_from_handler  # type: ignore[method-assign]
        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as logs:
            bridge.inbox_loop()

        self.assertEqual(fake.processing, [])
        self.assertEqual(fake.remove_calls, [raw])
        self.assertTrue(any("[bridge-error] inbox-handle-failed boom" in line for line in logs.output))

    def test_startup_recovers_processing_entries_and_processes_them(self) -> None:
        bridge = make_bridge("--dry-run", "--once")
        fake = ReliableRedis()
        raw = request_json("req-recovered")
        fake.processing.extend([raw, "not json"])
        bridge.redis = fake  # type: ignore[assignment]

        with self.assertLogs("agent_redis_bridge.bridge", level="INFO") as logs:
            bridge.inbox_loop()

        self.assertEqual(fake.processing, [])
        replies = reply_envelopes(fake)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["in_reply_to"], "req-recovered")
        self.assertEqual(fake.inbox, ["not json"])
        self.assertTrue(any("[bridge] recovered in-flight envelope id=req-recovered" in line for line in logs.output))
        self.assertTrue(any("[bridge] recovered in-flight envelope id=unknown" in line for line in logs.output))

    def test_shutdown_after_blmove_leaves_envelope_parked(self) -> None:
        bridge = make_bridge("--dry-run", "--once")
        fake = ReliableRedis()
        raw = request_json("req-shutdown")
        fake.inbox.append(raw)
        fake.on_blmove = bridge.stop_event.set
        bridge.redis = fake  # type: ignore[assignment]

        with self.assertLogs("agent_redis_bridge.bridge", level="INFO") as logs:
            bridge.inbox_loop()

        self.assertEqual(fake.processing, [raw])
        self.assertEqual(fake.remove_calls, [])
        self.assertEqual(fake.replies, [])
        self.assertTrue(
            any(
                "[bridge] shutdown with parked envelope id=req-shutdown (will recover on restart)" in line
                for line in logs.output
            )
        )

    def test_blmove_unsupported_warns_once_and_falls_back_to_blpop(self) -> None:
        bridge = make_bridge("--dry-run", "--once")
        fake = ReliableRedis()
        fake.blmove_unsupported = True
        raw = request_json("req-fallback")
        fake.inbox.append(raw)
        bridge.redis = fake  # type: ignore[assignment]

        with self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs:
            bridge.inbox_loop()

        self.assertFalse(bridge.reliable_inbox)
        self.assertEqual(fake.blmove_calls, 1)
        self.assertEqual(fake.blpop_calls, 1)
        self.assertEqual(fake.processing, [])
        self.assertEqual(len(reply_envelopes(fake)), 1)
        warnings = [
            record
            for record in logs.records
            if "[bridge-warning] blmove-unsupported falling back to blpop (at-most-once delivery)"
            in record.getMessage()
        ]
        self.assertEqual(len(warnings), 1)

    def test_blmove_unsupported_detection_is_case_insensitive(self) -> None:
        bridge = make_bridge("--dry-run", "--once")
        fake = ReliableRedis()
        fake.blmove_unsupported = True
        fake.blmove_error_message = "ERR UNKNOWN COMMAND 'bLmOvE' with args"
        raw = request_json("req-fallback-mixed-case")
        fake.inbox.append(raw)
        bridge.redis = fake  # type: ignore[assignment]

        with self.assertLogs("agent_redis_bridge.bridge", level="WARNING") as logs:
            bridge.inbox_loop()

        self.assertFalse(bridge.reliable_inbox)
        self.assertEqual(fake.blmove_calls, 1)
        self.assertEqual(fake.blpop_calls, 1)
        self.assertEqual(len(reply_envelopes(fake)), 1)
        warnings = [
            record
            for record in logs.records
            if "[bridge-warning] blmove-unsupported falling back to blpop (at-most-once delivery)"
            in record.getMessage()
        ]
        self.assertEqual(len(warnings), 1)

    def test_lua_wrapped_lmove_unsupported_detection(self) -> None:
        error = ResponseError(
            "Unknown Redis command called from script script: abc, "
            "on @user_script:1. command name: lmove"
        )

        self.assertTrue(Bridge.is_blmove_unsupported(error))


def reply_envelopes(fake: ReliableRedis) -> list[dict[str, object]]:
    return [json.loads(body) for _, body in fake.replies if json.loads(body).get("kind") == "reply"]


if __name__ == "__main__":
    unittest.main()

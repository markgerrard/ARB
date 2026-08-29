import hashlib
from argparse import Namespace
from unittest.mock import Mock

import pytest

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.redis_io import (
    OWNER_FENCE_SCRIPT,
    RECOVER_VALIDATED_SCRIPT,
    RedisCli,
    RedisConfig,
)
from test_bridge_handle_raw import make_bridge, request_json


class LuaCapableFakeRedis:
    """Minimal fake for the processing-claim EVAL contract."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.strings[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lrem(self, key: str, count: int, value: str) -> int:
        values = self.lists.get(key, [])
        removed = 0
        remaining: list[str] = []
        for current in values:
            if current == value and (count == 0 or removed < count):
                removed += 1
            else:
                remaining.append(current)
        self.lists[key] = remaining
        return removed

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        values = self.lists.get(key, [])
        return values[start:] if stop == -1 else values[start : stop + 1]

    def lindex(self, key: str, index: int) -> str | None:
        values = self.lists.get(key, [])
        return values[index] if -len(values) <= index < len(values) else None

    def eval(self, script: str, numkeys: int, *args: str) -> int:
        if script == OWNER_FENCE_SCRIPT:
            assert numkeys == 2
            assert len(args) == 4
            processing_key, claim_key = args[:numkeys]
            body, owner_token = args[numkeys:]
            if self.strings.get(claim_key) != owner_token:
                return 0
            removed = self.lrem(processing_key, 1, body)
            self.strings.pop(claim_key, None)
            return removed
        assert script == RECOVER_VALIDATED_SCRIPT
        assert numkeys == 3
        assert len(args) == 4
        processing_key, inbox_key, claim_key = args[:numkeys]
        (body,) = args[numkeys:]
        if self.lindex(processing_key, -1) != body:
            return 0
        self.lists[processing_key].pop()
        self.strings.pop(claim_key, None)
        self.lists.setdefault(inbox_key, []).insert(0, body)
        return 1


def _cli() -> RedisCli:
    cfg = RedisConfig(
        prefix="agent_scratch:",
        host="localhost",
        port="6379",
        db="0",
        user="",
        password="",
        tls=False,
    )
    cli = RedisCli.__new__(RedisCli)
    cli.config = cfg
    cli.client = LuaCapableFakeRedis()
    return cli


AGENT = "codex-bridge-dev-example"
BODY = '{"id":"task-R","kind":"request"}'


def test_stale_predecessor_remove_does_not_delete_successors_reparked_entry():
    cli = _cli()
    cli.client.rpush(cli.config.processing_key(AGENT), BODY)
    cli.claim_processing(AGENT, BODY, "owner-A", ttl=3600)
    cli.client.lrem(cli.config.processing_key(AGENT), 1, BODY)
    cli.client.rpush(cli.config.processing_key(AGENT), BODY)
    cli.claim_processing(AGENT, BODY, "owner-B", ttl=3600)

    removed = cli.remove_processing(AGENT, BODY, "owner-A")

    assert removed == 0
    assert cli.client.lrange(cli.config.processing_key(AGENT), 0, -1) == [BODY]
    assert cli.client.get(cli.config.processing_claim_key(AGENT, BODY)) == "owner-B"


def test_owner_removes_its_own_entry_and_clears_claim():
    cli = _cli()
    cli.client.rpush(cli.config.processing_key(AGENT), BODY)
    cli.claim_processing(AGENT, BODY, "owner-A", ttl=3600)

    removed = cli.remove_processing(AGENT, BODY, "owner-A")

    assert removed == 1
    assert cli.client.lrange(cli.config.processing_key(AGENT), 0, -1) == []
    assert cli.client.get(cli.config.processing_claim_key(AGENT, BODY)) is None


def test_missing_claim_is_a_noop_not_an_unconditional_lrem():
    cli = _cli()
    cli.client.rpush(cli.config.processing_key(AGENT), BODY)

    removed = cli.remove_processing(AGENT, BODY, "owner-A")

    assert removed == 0
    assert cli.client.lrange(cli.config.processing_key(AGENT), 0, -1) == [BODY]


class RecoveryWindowRedis:
    def __init__(self) -> None:
        self.inbox: list[str] = []
        self.processing: list[str] = []
        self.claims: dict[str, str] = {}
        self.clear_calls: list[tuple[str, str]] = []
        self.crash_after_clear = False
        self.crash_after_move = False

    def peek_processing(self, agent_id: str) -> str | None:
        return self.processing[-1] if self.processing else None

    def recover_processing_to_inbox(self, agent_id: str) -> str | None:
        if not self.processing:
            return None
        body = self.processing.pop()
        self.inbox.insert(0, body)
        if self.crash_after_move:
            self.crash_after_move = False
            raise RuntimeError("simulated crash after move")
        return body

    def recover_validated(self, agent_id: str, body: str) -> int:
        if not self.processing or self.processing[-1] != body:
            return 0
        if self.crash_after_clear:
            self.crash_after_clear = False
            raise RuntimeError("simulated failure before atomic recovery")
        self.clear_calls.append((agent_id, body))
        self.claims.pop(body, None)
        self.processing.pop()
        self.inbox.insert(0, body)
        if self.crash_after_move:
            self.crash_after_move = False
            raise RuntimeError("simulated failure after atomic recovery")
        return 1

    def clear_processing_claim(self, agent_id: str, body: str) -> None:
        self.clear_calls.append((agent_id, body))
        self.claims.pop(body, None)
        if self.crash_after_clear:
            self.crash_after_clear = False
            raise RuntimeError("simulated crash after clear")

    def blmove_to_processing(self, agent_id: str, timeout: int) -> str | None:
        if not self.inbox:
            return None
        body = self.inbox.pop(0)
        self.processing.append(body)
        return body

    def claim_processing(self, agent_id: str, body: str, owner_token: str, ttl: int) -> None:
        self.claims[body] = owner_token

    def remove_processing(self, agent_id: str, body: str, owner_token: str) -> int:
        if self.claims.get(body) != owner_token:
            return 0
        self.claims.pop(body, None)
        self.processing.remove(body)
        return 1


class PeekMoveMismatchRedis:
    def __init__(self) -> None:
        self.inbox: list[str] = []
        self.processing = ["B", "A"]
        self.claims = {"B": "owner-P", "A": "owner-P"}
        self.interleave_fired = False
        self.moved: list[tuple[str, bool]] = []

    def peek_processing(self, agent_id: str) -> str | None:
        if not self.processing:
            return None
        body = self.processing[-1]
        if not self.interleave_fired:
            self.interleave_fired = True
            self.processing.pop()
            self.claims.pop(body, None)
        return body

    def clear_processing_claim(self, agent_id: str, body: str) -> None:
        self.claims.pop(body, None)

    def recover_processing_to_inbox(self, agent_id: str) -> str | None:
        if not self.processing:
            return None
        body = self.processing.pop()
        self.inbox.insert(0, body)
        self.moved.append((body, body not in self.claims))
        return body

    def recover_validated(self, agent_id: str, body: str) -> int:
        if not self.processing or self.processing[-1] != body:
            return 0
        self.processing.pop()
        self.claims.pop(body, None)
        self.inbox.insert(0, body)
        self.moved.append((body, body not in self.claims))
        return 1

    def blmove_to_processing(self, agent_id: str, timeout: int) -> str | None:
        if not self.inbox:
            return None
        body = self.inbox.pop(0)
        self.processing.append(body)
        return body

    def claim_processing(self, agent_id: str, body: str, owner_token: str, ttl: int) -> None:
        self.claims[body] = owner_token

    def remove_processing(self, agent_id: str, body: str, owner_token: str) -> int:
        if self.claims.get(body) != owner_token:
            return 0
        self.claims.pop(body, None)
        self.processing.remove(body)
        return 1


def test_peek_move_mismatch_only_moves_body_after_atomic_validation():
    bridge = Bridge.__new__(Bridge)
    bridge.reliable_inbox = True
    bridge.agent_id = AGENT
    redis = PeekMoveMismatchRedis()
    bridge.redis = redis

    bridge.recover_processing_envelopes()

    assert redis.moved == [("B", True)]
    assert redis.inbox == ["B"]
    assert redis.processing == []
    assert redis.claims == {}

    assert redis.blmove_to_processing(AGENT, timeout=0) == "B"
    redis.claim_processing(AGENT, "B", "owner-S", ttl=3600)
    assert redis.remove_processing(AGENT, "B", "owner-P") == 0
    assert redis.processing == ["B"]


def test_recovery_clears_predecessor_claim_before_successor_repark_window():
    bridge = Bridge.__new__(Bridge)
    bridge.reliable_inbox = True
    bridge.agent_id = AGENT
    redis = RecoveryWindowRedis()
    bridge.redis = redis
    redis.processing.append(BODY)
    redis.claim_processing(AGENT, BODY, "owner-A", ttl=3600)

    bridge.recover_processing_envelopes()
    assert redis.clear_calls == [(AGENT, BODY)]

    assert redis.blmove_to_processing(AGENT, timeout=0) == BODY
    assert redis.remove_processing(AGENT, BODY, "owner-A") == 0
    assert redis.processing == [BODY]

    redis.claim_processing(AGENT, BODY, "owner-B", ttl=3600)
    assert redis.remove_processing(AGENT, BODY, "owner-B") == 1
    assert redis.processing == []


def test_prefix_a_stale_remove_is_the_legitimate_owner_fence_case():
    redis = RecoveryWindowRedis()
    redis.processing.append(BODY)
    redis.claim_processing(AGENT, BODY, "owner-A", ttl=3600)

    assert redis.remove_processing(AGENT, BODY, "owner-A") == 1
    assert redis.processing == []


def test_failure_after_atomic_recovery_cannot_delete_reparked_body():
    bridge = Bridge.__new__(Bridge)
    bridge.reliable_inbox = True
    bridge.agent_id = AGENT
    redis = RecoveryWindowRedis()
    bridge.redis = redis
    redis.processing.append(BODY)
    redis.claim_processing(AGENT, BODY, "owner-A", ttl=3600)
    redis.crash_after_move = True

    bridge.recover_processing_envelopes()

    assert redis.blmove_to_processing(AGENT, timeout=0) == BODY
    assert redis.remove_processing(AGENT, BODY, "owner-A") == 0
    assert redis.processing == [BODY]


def test_failure_before_atomic_recovery_is_idempotent_and_recovers_once():
    bridge = Bridge.__new__(Bridge)
    bridge.reliable_inbox = True
    bridge.agent_id = AGENT
    redis = RecoveryWindowRedis()
    bridge.redis = redis
    redis.processing.append(BODY)
    redis.claim_processing(AGENT, BODY, "owner-A", ttl=3600)
    redis.crash_after_clear = True

    bridge.recover_processing_envelopes()

    assert redis.processing == [BODY]
    assert redis.inbox == []
    assert redis.claims == {BODY: "owner-A"}

    bridge.recover_processing_envelopes()

    assert redis.processing == []
    assert redis.inbox == [BODY]
    assert redis.claims == {}


def test_completed_recovery_stale_remove_cannot_delete_successor_repark_window():
    bridge = Bridge.__new__(Bridge)
    bridge.reliable_inbox = True
    bridge.agent_id = AGENT
    redis = RecoveryWindowRedis()
    bridge.redis = redis
    redis.processing.append(BODY)
    redis.claim_processing(AGENT, BODY, "owner-A", ttl=3600)

    bridge.recover_processing_envelopes()

    assert redis.inbox == [BODY]
    assert redis.claims == {}
    assert redis.blmove_to_processing(AGENT, timeout=0) == BODY
    assert redis.remove_processing(AGENT, BODY, "owner-A") == 0
    assert redis.processing == [BODY]


def test_claim_key_is_body_scoped():
    cli = _cli()

    assert cli.config.processing_claim_key(AGENT, BODY).endswith(
        hashlib.sha256(BODY.encode()).hexdigest()[:32]
    )
    assert cli.config.processing_claim_key(AGENT, BODY) != cli.config.processing_claim_key(AGENT, "other")


def test_recover_validated_moves_only_the_peeked_rightmost_body():
    cli = _cli()
    other_body = '{"id":"task-S","kind":"request"}'
    cli.client.rpush(cli.config.processing_key(AGENT), BODY)
    cli.client.rpush(cli.config.processing_key(AGENT), other_body)
    cli.claim_processing(AGENT, other_body, "owner-A", ttl=3600)

    assert cli.recover_validated(AGENT, other_body) == 1
    assert cli.client.lrange(cli.config.processing_key(AGENT), 0, -1) == [BODY]
    assert cli.client.lrange(cli.config.inbox_key(AGENT), 0, -1) == [other_body]
    assert cli.client.get(cli.config.processing_claim_key(AGENT, other_body)) is None


def test_peek_processing_reads_the_right_end_used_by_recovery_move():
    cli = _cli()
    other_body = '{"id":"task-S","kind":"request"}'
    cli.client.rpush(cli.config.processing_key(AGENT), BODY)
    cli.client.rpush(cli.config.processing_key(AGENT), other_body)

    assert cli.peek_processing(AGENT) == other_body


class BridgeRedisSpy:
    def __init__(self, body: str = BODY) -> None:
        self.body = body
        self.claim_calls: list[tuple[str, str, str, int]] = []
        self.remove_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def blmove_to_processing(self, agent_id: str, timeout: int) -> str:
        return self.body

    def claim_processing(self, agent_id: str, body: str, owner_token: str, ttl: int) -> None:
        self.claim_calls.append((agent_id, body, owner_token, ttl))

    def consumer_heartbeat(self, agent_id: str, owner_token: str, ttl: int) -> None:
        return None

    def lpop_control(self, agent_id: str) -> None:
        return None

    def recover_processing_to_inbox(self, agent_id: str) -> None:
        return None

    def remove_processing(self, *args: object, **kwargs: object) -> int:
        self.remove_calls.append((args, kwargs))
        return 1


def test_pop_inbox_claims_parked_body_under_owner_token():
    bridge = Bridge.__new__(Bridge)
    bridge.args = Namespace(
        blpop_timeout=30,
        turn_timeout=60,
        turn_timeout_max=100,
        max_continuation_turns=2,
        events_ttl=90,
    )
    bridge.max_continuation_turns = 2
    bridge.reliable_inbox = True
    bridge.agent_id = AGENT
    bridge.owner_token = "owner-A"
    redis = BridgeRedisSpy()
    bridge.redis = redis

    result = bridge.pop_inbox()

    assert result == (BODY, True)
    assert redis.claim_calls == [(AGENT, BODY, "owner-A", 300)]


def test_pop_inbox_claim_ttl_uses_clamped_continuation_count():
    bridge = Bridge.__new__(Bridge)
    bridge.args = Namespace(
        blpop_timeout=30,
        turn_timeout=60,
        turn_timeout_max=100,
        max_continuation_turns=-2,
        events_ttl=90,
    )
    bridge.max_continuation_turns = 0
    bridge.reliable_inbox = True
    bridge.agent_id = AGENT
    bridge.owner_token = "owner-A"
    redis = BridgeRedisSpy()
    bridge.redis = redis

    result = bridge.pop_inbox()

    assert result == (BODY, True)
    assert redis.claim_calls == [(AGENT, BODY, "owner-A", 100)]


def test_both_remove_sites_pass_owner_token():
    inbox_bridge = make_bridge("--dry-run", "--once")
    inbox_redis = BridgeRedisSpy()
    inbox_bridge.redis = inbox_redis
    inbox_bridge.recover_processing_envelopes = lambda: None
    inbox_bridge.handle_raw = lambda raw, **kwargs: False

    inbox_bridge.inbox_loop()

    assert inbox_redis.remove_calls == [
        ((inbox_bridge.agent_id, BODY), {"owner_token": inbox_bridge.owner_token})
    ]

    process_bridge = make_bridge("--dry-run")
    process_redis = BridgeRedisSpy()
    process_bridge.redis = process_redis
    process_bridge.pool.release = Mock()
    process_bridge._capture = Mock()

    def fail_before_processing() -> None:
        raise RuntimeError("stop before processing")

    process_bridge.record_request_started = fail_before_processing
    envelope = Envelope.from_json(request_json("req-owner-fence"))

    with pytest.raises(RuntimeError, match="stop before processing"):
        process_bridge.process_request(
            envelope,
            policy="trusted",
            processing_raw=BODY,
        )

    assert process_redis.remove_calls == [
        ((process_bridge.agent_id, BODY), {"owner_token": process_bridge.owner_token})
    ]

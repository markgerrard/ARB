import json

import pytest

from agent_redis_bridge.claude_tail.offset import OffsetStore, Position, offset_key


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


def test_get_default_zero():
    assert OffsetStore(FakeRedis(), "p:").load("k") == Position(0, 0)


def test_commit_then_get():
    store = OffsetStore(FakeRedis(), "p:")
    store.store("k", 1234, 0)
    assert store.load("k") == Position(1234, 0)


def test_commit_uses_prefixed_redis_key():
    redis = FakeRedis()
    OffsetStore(redis, "p:").store("k", 1234, 0)
    assert json.loads(redis.values["p:claude:offset:k"]) == {"v": 1, "offset": 1234, "turn_index": 0}


def test_get_decodes_bytes_value():
    redis = FakeRedis()
    redis.values["p:claude:offset:k"] = b'{"v":1,"offset":42,"turn_index":0}'
    assert OffsetStore(redis, "p:").load("k").offset == 42


def test_offset_key_includes_inode():
    assert offset_key("/a/b.jsonl", 99) == "/a/b.jsonl|99"


def test_offset_key_composes_with_store_prefix_once():
    redis = FakeRedis()
    store = OffsetStore(redis, "p:")
    key = offset_key("/a/b.jsonl", 99)

    store.store(key, 1234, 0)

    assert store.load(key).offset == 1234
    assert json.loads(redis.values["p:claude:offset:/a/b.jsonl|99"]) == {"v": 1, "offset": 1234, "turn_index": 0}


def test_commit_coerces_int_offsets():
    redis = FakeRedis()
    OffsetStore(redis, "p:").store("k", "42", 0)
    assert json.loads(redis.values["p:claude:offset:k"]) == {"v": 1, "offset": 42, "turn_index": 0}


def test_commit_rejects_negative_offsets():
    with pytest.raises(ValueError):
        OffsetStore(FakeRedis(), "p:").store("k", -1, 0)


def test_get_reads_corrupt_offset_as_zero_without_writing():
    redis = FakeRedis()
    store = OffsetStore(redis, "p:")
    redis.set("p:claude:offset:k", "not-an-int")

    assert store.load("k") == Position(0, 0)
    assert redis.get("p:claude:offset:k") == "not-an-int"


def test_get_self_heals_none_like_corruption():
    class WeirdRedis(FakeRedis):
        def get(self, key):
            return ["not", "a", "str"]

    store = OffsetStore(WeirdRedis(), "p:")
    assert store.load("k") == Position(0, 0)


def test_store_then_load_roundtrips_composite():
    store = OffsetStore(FakeRedis(), "p:")
    store.store("k", 1234, 4)
    assert store.load("k") == Position(offset=1234, turn_index=4)


def test_load_absent_key_is_zero_zero():
    assert OffsetStore(FakeRedis(), "p:").load("k") == Position(0, 0)


def test_store_writes_versioned_json():
    redis = FakeRedis()
    OffsetStore(redis, "p:").store("k", 1234, 4)
    stored = json.loads(redis.values["p:claude:offset:k"])
    assert stored == {"v": 1, "offset": 1234, "turn_index": 4}


def test_legacy_bare_nonzero_int_forces_byte_zero_recount():
    redis = FakeRedis()
    redis.set("p:claude:offset:k", "5000")
    store = OffsetStore(redis, "p:")
    assert store.load("k") == Position(0, 0)
    assert redis.values["p:claude:offset:k"] == "5000"


def test_legacy_bare_zero_becomes_composite_zero():
    redis = FakeRedis()
    redis.set("p:claude:offset:k", "0")
    assert OffsetStore(redis, "p:").load("k") == Position(0, 0)


def test_corrupt_composite_reads_as_zero_zero_without_writing():
    redis = FakeRedis()
    redis.set("p:claude:offset:k", '{"v":1,"offset":"not-an-int"}')
    store = OffsetStore(redis, "p:")
    assert store.load("k") == Position(0, 0)
    assert redis.values["p:claude:offset:k"] == '{"v":1,"offset":"not-an-int"}'


def test_invalid_utf8_position_reads_as_zero_zero_without_raising():
    redis = FakeRedis()
    redis.set("p:claude:offset:k", b"\xff\xfe{")
    store = OffsetStore(redis, "p:")
    assert store.load("k") == Position(0, 0)
    assert redis.values["p:claude:offset:k"] == b"\xff\xfe{"


def test_negative_composite_reads_as_zero_zero_without_writing():
    redis = FakeRedis()
    redis.set("p:claude:offset:k", '{"v":1,"offset":-1,"turn_index":-1}')
    store = OffsetStore(redis, "p:")
    assert store.load("k") == Position(0, 0)
    assert redis.values["p:claude:offset:k"] == '{"v":1,"offset":-1,"turn_index":-1}'


def test_deeply_nested_corrupt_position_reads_as_zero_zero_without_raising():
    payload = "[" * 500_000 + "]" * 500_000
    with pytest.raises(RecursionError):
        json.loads(payload)
    redis = FakeRedis()
    redis.set("p:claude:offset:k", payload)
    assert OffsetStore(redis, "p:").load("k") == Position(0, 0)


def test_noncanonical_version_field_reads_as_corrupt():
    for v in ("true", "1.0"):
        redis = FakeRedis()
        redis.set("p:claude:offset:k", '{"v":%s,"offset":5000,"turn_index":3}' % v)
        assert OffsetStore(redis, "p:").load("k") == Position(0, 0)

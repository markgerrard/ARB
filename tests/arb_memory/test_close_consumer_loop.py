import json

import psycopg

from arb_memory import close
from arb_memory.consumer_loop import POISON_RETRY_LIMIT


class CloseRedis:
    def __init__(self):
        self.reads = []
        self.acked = []
        self.results = []
        self.owner = None

    def xgroup_create(self, *args, **kwargs):
        return None

    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        mode = next(iter(streams.values()))
        self.reads.append(mode)
        if mode == "0" and len(self.reads) == 1:
            return []
        if mode == ">":
            return [("stream", [("1-0", self.fields)])]
        if self.acked:
            return []
        return [("stream", [("1-0", self.fields)])]

    @property
    def fields(self):
        return {
            "request_id": "req-1",
            "run_id": "run-1",
            "verdict": json.dumps({"kind": "verdict"}),
        }

    def xack(self, stream, group, entry_id):
        self.acked.append(entry_id)
        self.owner.stop()

    def lpush(self, key, value):
        self.results.append((key, json.loads(value)))

    def expire(self, key, seconds):
        return None


class FakeConn:
    closed = False

    def close(self):
        self.closed = True


def test_close_poison_exhaustion_publishes_immediate_exit_3(monkeypatch):
    redis = CloseRedis()
    consumer = close.CloseConsumer(redis, lambda: FakeConn(), prefix="fleet:", block_ms=0)
    redis.owner = consumer
    monkeypatch.setattr(close, "close_core", lambda *args, **kwargs: (_ for _ in ()).throw(psycopg.DataError("bad")))
    monkeypatch.setattr(close, "deadletter_malformed_close_request", lambda *args: None)

    consumer.run()

    assert redis.acked == ["1-0"]
    assert redis.results == [
        ("fleet:arbmem:audit:close_result:req-1", {"outcome": "infra_exhausted", "exit_code": 3, "gaps": []})
    ]
    assert sum(mode != "0" or index > 0 for index, mode in enumerate(redis.reads)) >= POISON_RETRY_LIMIT

"""Cross-item merge gate (Item 1 poison-exhaustion terminal × Item 2 _publish_result hook).

Neither parallel branch could build this — it needs Item 1's `_retry_or_exhaust` exhaustion path AND
Item 2's `_publish_result` hook body together. It proves a poison-exhausted *awaited* write publishes
`{artefact_outcome:"failed"}` to the result channel and acks, instead of hanging to the proxy's await cap.
"""

import json

import psycopg

from arb_memory import bus
from arb_memory.consumer_loop import POISON_RETRY_LIMIT


class _PoisonRedeliverRedis:
    """Redelivers one request_id-carrying entry on every pending read until it is acked, then stops the
    loop. Models the PEL recirculation that lets the in-memory poison counter reach its limit."""

    def __init__(self, entry):
        self.entry = entry
        self.acked = False
        self.published = []
        self.expirations = []
        self.loop = None

    def xgroup_create(self, *args, **kwargs):
        pass

    def xreadgroup(self, group, consumer, streams, **kwargs):
        mode = next(iter(streams.values()))
        if self.acked:
            if self.loop is not None:
                self.loop.stop()
            return []
        if mode == ">":  # no *new* deliveries; the entry recirculates via the pending/cursor read
            return []
        return [("stream", [self.entry])]

    def xack(self, stream, group, entry_id):
        self.acked = True

    def lpush(self, key, value):
        self.published.append((key, json.loads(value)))

    def expire(self, key, ttl):
        self.expirations.append((key, ttl))


def test_poison_exhausted_awaited_write_publishes_failed_result(monkeypatch):
    entry = ("1-0", {"ulid": "U1", "request_id": "req-1", "payload": json.dumps({"ulid": "U1"})})
    redis = _PoisonRedeliverRedis(entry)
    loop = bus.WriteLoop(redis, lambda: object(), embed=lambda text: [], prefix="test:")
    redis.loop = loop

    # deadletter helper "succeeds" (a reachable sink) so exhaustion evicts + publishes, rather than
    # opening the sink circuit.
    monkeypatch.setattr(loop, "_deadletter", lambda *args: True)

    calls = {"n": 0}

    def _poison(_conn, intent, **kwargs):
        calls["n"] += 1
        raise psycopg.DataError("deterministic poison")

    monkeypatch.setattr(bus, "handle_write_intent", _poison)

    loop.run()

    # the awaited write got a loud terminal result on the channel (NOT a hang), and the entry is acked
    assert (
        bus.write_result_key("req-1", "test:"),
        {"artefact_outcome": "failed", "reason": "deadlettered", "duplicate": False},
    ) in redis.published
    assert redis.acked is True
    # the poison budget was actually exercised to its bound (not deadlettered on the first failure)
    assert calls["n"] >= POISON_RETRY_LIMIT

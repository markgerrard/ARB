"""A consumer must recover from losing its group, not stall on it forever.

The 2026-08-09 incident: managed Valkey (`allkeys-lru`, no AOF) evicted the
stream and took its consumer group with it. The next producer XADD recreated
the stream ALONE. Every consumer stayed UP with zero restarts, logging
NOGROUP, while `arbmem:writes` silently accumulated entries nobody would ever
read. `memory_store` kept returning `accepted: true` and nothing reached
PostgreSQL — measured 2026-08-11: `xinfo_groups: []`, `artefact_rows 0`,
`write_deadletter_rows 0`. Not deadlettered; stranded.

`ensure_group` ran only in `__init__`, and NOGROUP is a `ResponseError` —
hence a `RedisError` — so the loop treated it as transient and backed off
against it forever. No amount of waiting brings a deleted group back.
"""

from __future__ import annotations

import logging

import redis

from arb_memory.consumer_loop import StreamConsumerLoop, is_missing_group

NOGROUP_MESSAGE = (
    "NOGROUP No such key 'arbmem:writes' or consumer group 'arbmem-writer' "
    "in XREADGROUP with GROUP option"
)


class GrouplessRedis:
    """Raises NOGROUP until the group is (re)created, then serves one entry."""

    def __init__(self, *, entries_after_recreate=(("1-0", {"kind": "new"}),), max_attempts=5):
        self.group_exists = False
        self.created = []
        self.served = False
        self.entries_after_recreate = list(entries_after_recreate)
        self.attempts = 0
        self.max_attempts = max_attempts
        self.loop = None

    def xgroup_create(self, stream, group, id="$", mkstream=False):
        self.created.append((stream, group, id))
        self.group_exists = True

    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        if not self.group_exists:
            # Bound the test. An unrepaired loop backs off against NOGROUP
            # forever — that is the defect — so without this the mutation
            # check would hang instead of failing, and a hang is a much
            # weaker signal than a red assertion.
            self.attempts += 1
            if self.loop is not None and self.attempts >= self.max_attempts:
                self.loop.stop()
            raise redis.ResponseError(NOGROUP_MESSAGE)
        mode = next(iter(streams.values()))
        if mode == ">" and not self.served and self.entries_after_recreate:
            self.served = True
            return [("stream", self.entries_after_recreate)]
        return []

    def xack(self, stream, group, entry_id):
        return None


class RecordingLoop(StreamConsumerLoop):
    def __init__(self, fake, ledger_prefix=""):
        self.redis = fake
        self.stream = "arbmem:writes"
        self.group = "arbmem-writer"
        self.consumer = "writer"
        self.block_ms = 0
        self.handled = []
        # Explicit, like every real subclass: `ledger_prefix` has no default on
        # `_init_consumer_loop` precisely so this cannot be forgotten.
        self.prefix = ledger_prefix
        self._init_consumer_loop(ledger_prefix=ledger_prefix)

    def _handle_entry(self, entry_id, fields):
        self.handled.append((entry_id, fields))
        self._ack(entry_id)
        self.stop()
        return True


def _loop(fake, monkeypatch, ledger_prefix=""):
    # Backoff would otherwise make this test sleep for real seconds.
    monkeypatch.setenv("ARB_CONSUMER_BACKOFF_CAP_S", "0.001")
    loop = RecordingLoop(fake, ledger_prefix=ledger_prefix)
    fake.loop = loop
    return loop


def test_nogroup_is_recognised_as_a_missing_group():
    assert is_missing_group(redis.ResponseError(NOGROUP_MESSAGE))


def test_an_ordinary_redis_error_is_not_mistaken_for_a_missing_group():
    """Do not recreate groups in response to unrelated faults."""
    assert not is_missing_group(redis.ResponseError("WRONGTYPE Operation against a key"))
    assert not is_missing_group(redis.ConnectionError("connection reset"))


def test_loop_recreates_a_vanished_group_and_resumes_draining(monkeypatch):
    fake = GrouplessRedis()
    loop = _loop(fake, monkeypatch)

    loop.run()

    assert fake.created, "group was never recreated — the loop stalled forever"
    assert loop.handled == [("1-0", {"kind": "new"})], "did not resume draining after repair"


def test_recreation_uses_dollar_so_a_backlog_is_not_blindly_replayed(monkeypatch):
    """Restoring service and replaying a backlog are different decisions.

    eval and trace persist the raw XADD stream id as globally unique, so an
    automatic replay after a stream was recreated can collide.
    """
    fake = GrouplessRedis()
    loop = _loop(fake, monkeypatch)

    loop.run()

    assert [entry[2] for entry in fake.created] == ["$"]


def test_repair_is_logged_loudly_because_silence_is_how_this_hid(monkeypatch, caplog):
    fake = GrouplessRedis()
    loop = _loop(fake, monkeypatch)

    with caplog.at_level(logging.ERROR, logger="arb_memory.consumer_loop"):
        loop.run()

    assert any(
        "was MISSING and has been recreated" in record.getMessage()
        for record in caplog.records
    ), "a silent repair reproduces the invisibility that let this run for a day"


def test_a_failing_recreate_does_not_crash_the_consumer(monkeypatch):
    """A consumer that dies on repair is worse than one that keeps retrying."""

    class UnrepairableRedis(GrouplessRedis):
        def xgroup_create(self, stream, group, id="$", mkstream=False):
            self.created.append((stream, group, id))
            raise redis.ResponseError("NOPERM this user has no permissions to run 'xgroup'")

    fake = UnrepairableRedis()
    loop = _loop(fake, monkeypatch)
    loop._stop.set()  # one guarded pass, then exit

    loop.run()  # must not raise

    assert fake.created, "repair was not even attempted"


# --- ReadLoop: the eighth consumer, and the one that does NOT inherit --------
#
# d1c29779 put the repair on StreamConsumerLoop and so fixed seven consumers.
# ReadLoop is not a subclass, so it stayed stalled — prod measured 2,879
# read-loop failures in 24h on arbmem:reads/arbmem-memory. A shared failure
# mode needs its remedy somewhere nothing is excluded from; these tests fail
# against d1c29779.

from arb_memory.bus import PREFIX, ReadLoop, reads_stream  # noqa: E402


class GrouplessReadRedis(GrouplessRedis):
    """Same fake, but reads return no entries — ReadLoop's pending lane is cleanup-only."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.successful_reads = 0

    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        if not self.group_exists:
            self.attempts += 1
            if self.loop is not None and self.attempts >= self.max_attempts:
                self.loop.stop()
            raise redis.ResponseError(NOGROUP_MESSAGE)
        self.successful_reads += 1
        if self.loop is not None:
            self.loop.stop()
        return []


def _read_loop(monkeypatch):
    monkeypatch.setenv("ARB_CONSUMER_BACKOFF_CAP_S", "0.001")
    fake = GrouplessReadRedis()
    loop = ReadLoop(fake, lambda: object(), embed=None, prefix=PREFIX, consumer="reader")
    # __init__ legitimately creates the group; the eviction happens AFTER that,
    # which is the real sequence and the one the fix has to survive.
    fake.created.clear()
    fake.group_exists = False
    fake.loop = loop
    return loop, fake


def test_readloop_recreates_a_vanished_group_and_resumes(monkeypatch):
    loop, fake = _read_loop(monkeypatch)

    loop.run()

    assert fake.created, "ReadLoop never recreated its group — it is not a StreamConsumerLoop"
    assert fake.successful_reads >= 1, "did not resume reading after repair"


def test_readloop_recreates_at_dollar_not_zero(monkeypatch):
    loop, fake = _read_loop(monkeypatch)

    loop.run()

    assert [entry[2] for entry in fake.created] == ["$"]


def test_readloop_repairs_on_the_pending_cleanup_path_too(monkeypatch):
    """run() calls drain_pending FIRST, and that catch re-raises rather than swallowing."""
    loop, fake = _read_loop(monkeypatch)

    try:
        loop.drain_pending()
    except redis.RedisError:
        pass  # re-raising is existing behaviour and is preserved

    assert fake.created, "pending-cleanup path did not repair before re-raising"


def test_readloop_targets_its_own_stream_and_group(monkeypatch):
    """A repair aimed at the wrong key would 'succeed' and fix nothing."""
    loop, fake = _read_loop(monkeypatch)

    loop.run()

    assert fake.created[0][0] == reads_stream(PREFIX)
    assert fake.created[0][1] == loop.group


# --- Strand measurement (option A, 2026-08-19) ------------------------------
#
# The loud log line above already existed on 2026-08-09 and the loss STILL went
# unnoticed for ten days: it took a manual producer-ULID join against
# `idempotency_keys` to prove ten `arbmem:writes` entries were skipped rather
# than processed. A line nobody reads is not a signal. These pin the durable
# record that bounds that reconciliation.


class MeasurableRedis(GrouplessRedis):
    """A GrouplessRedis that can also answer the strand-measurement calls."""

    def __init__(self, *, retained=7, **kwargs):
        super().__init__(**kwargs)
        self.retained = retained
        self.recorded = []
        self.call_order = []

    def xlen(self, stream):
        self.call_order.append(("xlen", stream))
        return self.retained

    def xrange(self, stream, count=None):
        self.call_order.append(("xrange", stream))
        return [("100-0", {})]

    def xrevrange(self, stream, count=None):
        self.call_order.append(("xrevrange", stream))
        return [("900-0", {})]

    def xgroup_create(self, stream, group, id="$", mkstream=False):
        self.call_order.append(("xgroup_create", stream))
        return super().xgroup_create(stream, group, id=id, mkstream=mkstream)

    def xadd(self, stream, fields, maxlen=None, approximate=None):
        self.call_order.append(("xadd", stream))
        self.recorded.append((stream, fields))
        return "1-1"


def test_the_strand_is_measured_BEFORE_the_recreate(monkeypatch):
    """Ordering is the whole trick: `$` moves on recreate and the evidence goes.

    Measure after, and you record the state of a stream that has already been
    abandoned — a number that looks like evidence and means nothing.
    """
    fake = MeasurableRedis()
    loop = _loop(fake, monkeypatch)

    loop.run()

    kinds = [name for name, _ in fake.call_order]
    assert "xgroup_create" in kinds, "precondition: the repair must have run"
    assert kinds.index("xlen") < kinds.index("xgroup_create"), (
        "measuring after the recreate records a stream that is already orphaned"
    )


def test_repair_records_the_strand_best_effort(monkeypatch):
    fake = MeasurableRedis(retained=7)
    loop = _loop(fake, monkeypatch)

    loop.run()

    assert fake.recorded, "no record means the next reconciliation starts from nothing again"
    stream, fields = fake.recorded[0]
    assert stream == "arbmem:group-repairs"
    assert fields["retained"] == "7"
    assert fields["first_retained"] == "100-0"
    assert fields["last_retained"] == "900-0"
    assert fields["group_start_id"] == "900-0", (
        "the record must carry the EXACT id the group was created from, not a "
        "literal $ — a reconciliation needs the cutoff, not the sigil"
    )
    assert "upper bound" in fields["note"], (
        "retained counts the whole stream; most of it was already processed. "
        "A record that reads as a loss count would overstate every incident."
    )


def test_a_failing_measurement_does_not_prevent_the_repair(monkeypatch):
    """Restoring service beats recording why it broke."""

    class UnmeasurableRedis(MeasurableRedis):
        def xlen(self, stream):
            raise redis.ResponseError("NOPERM this user has no permissions to run 'xlen'")

    fake = UnmeasurableRedis()
    loop = _loop(fake, monkeypatch)

    loop.run()

    assert fake.created, (
        "a consumer that will not repair itself because it could not measure "
        "the damage is strictly worse than one that repairs blindly"
    )


def test_a_failing_record_does_not_prevent_the_repair(monkeypatch):
    class UnrecordableRedis(MeasurableRedis):
        def xadd(self, stream, fields, maxlen=None, approximate=None):
            raise redis.ResponseError("NOPERM this user has no permissions to run 'xadd'")

    fake = UnrecordableRedis()
    loop = _loop(fake, monkeypatch)

    loop.run()

    assert fake.created


# --- P1/P2 from codex-arbmem-prod's review of 5b5d9762 ----------------------


def _stream_id(value):
    """Redis stream ids order by (ms, seq), never lexicographically."""
    ms, _, seq = str(value).partition("-")
    return (int(ms), int(seq or 0))


class RacingRedis(MeasurableRedis):
    """A producer XADDs in the window between measurement and group creation.

    prod reproduced exactly this: the record said last_retained=900-0 while the
    group resolved `$` to 1000-0, so entry 1000-0 was skipped AND outside the
    recorded range — `retained` stops being an upper bound and a reconciliation
    trusting the record misses real writes.
    """

    RACER = "1000-0"

    def xgroup_create(self, stream, group, id="$", mkstream=False):
        # The race: an entry lands after we measured, before the group exists.
        self.latest = self.RACER
        resolved = self.latest if id == "$" else id
        self.resolved_start = resolved
        return super().xgroup_create(stream, group, id=id, mkstream=mkstream)


def test_an_entry_arriving_during_the_repair_is_not_silently_skipped(monkeypatch):
    """The fix: create from the MEASURED id, so the race window is deliverable.

    Creating from `$` resolves past the racer and drops it with no trace. The
    measured id sits before it, so the new group delivers it normally — the
    window closes in the safe direction instead of the lossy one.
    """
    fake = RacingRedis()
    loop = _loop(fake, monkeypatch)

    loop.run()

    created_ids = [entry[2] for entry in fake.created]
    assert created_ids == ["900-0"], (
        f"expected creation from the measured last id, got {created_ids}"
    )
    assert fake.resolved_start != RacingRedis.RACER, (
        "the group started at the racing entry, so that entry is skipped and "
        "the record does not cover it — this is prod's P1 exactly"
    )


def test_the_racing_entry_is_covered_by_the_recorded_cutoff(monkeypatch):
    """The record must let a reconciliation reason about the boundary."""
    fake = RacingRedis()
    loop = _loop(fake, monkeypatch)

    loop.run()

    _, fields = fake.recorded[0]
    assert fields["group_start_id"] == "900-0"
    assert _stream_id(fields["group_start_id"]) < _stream_id(RacingRedis.RACER), (
        "anything after the recorded cutoff is delivered, so it is not lost; "
        "the cutoff is only meaningful if it precedes the racer. "
        "NB: compared as (ms, seq) — '900-0' > '1000-0' as strings, which is "
        "how a lexicographic check would pass this while meaning nothing."
    )


class PrefixedRedis(MeasurableRedis):
    """A tenant-prefixed stream, as five of the consumers support."""


def test_the_record_follows_the_consumers_explicit_prefix(monkeypatch):
    """prod's P2: reading ARB_MEMORY_PREFIX sends a tenant repair to the
    unprefixed ledger — cross-namespace pollution, or NOPERM under
    prefix-scoped ACLs. Routed explicitly from the consumer, never inferred."""
    monkeypatch.delenv("ARB_MEMORY_PREFIX", raising=False)
    fake = PrefixedRedis()
    loop = _loop(fake, monkeypatch, ledger_prefix="tenant-a:")
    loop.stream = "tenant-a:arbmem:writes"

    loop.run()

    recorded_streams = [stream for stream, _ in fake.recorded]
    assert recorded_streams == ["tenant-a:arbmem:group-repairs"], (
        f"repair ledger escaped its namespace: {recorded_streams}"
    )


def test_an_unprefixed_stream_still_records_unprefixed(monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_PREFIX", raising=False)
    fake = MeasurableRedis()
    loop = _loop(fake, monkeypatch)

    loop.run()

    assert [stream for stream, _ in fake.recorded] == ["arbmem:group-repairs"]


def test_an_eval_shaped_stream_still_routes_to_its_tenant_ledger(monkeypatch):
    """prod's counterexample that killed inference.

    `EvalConsumer`'s stream is `{prefix}eval:events` — no `arbmem:` marker — so
    deriving the prefix from the stream name sent a tenant eval repair to the
    UNPREFIXED ledger. Explicit routing has no such blind spot; this pins the
    exact shape that inference missed.
    """
    monkeypatch.delenv("ARB_MEMORY_PREFIX", raising=False)
    fake = PrefixedRedis()
    loop = _loop(fake, monkeypatch, ledger_prefix="tenant-a:")
    loop.stream = "tenant-a:eval:events"

    loop.run()

    assert [stream for stream, _ in fake.recorded] == ["tenant-a:arbmem:group-repairs"], (
        "an eval repair escaped its namespace — this is the shape substring "
        "inference could not see"
    )


def test_the_dollar_fallback_is_recorded_as_DEGRADED_not_as_a_cutoff(monkeypatch):
    """prod's P2b: a degraded path must not inherit the healthy path's claims.

    When the stream cannot be measured we still create at `$` — correct
    fail-open service restoration — but the resolved boundary is an id nobody
    captured. Recording `$` in a field documented as an exact cutoff, while
    still calling `retained` an upper bound, is how an undercount gets trusted
    a second time.
    """
    class UnmeasurableTailRedis(MeasurableRedis):
        def xrevrange(self, stream, count=None):
            raise redis.ResponseError("NOPERM this user has no permissions to run 'xrevrange'")

    fake = UnmeasurableTailRedis()
    loop = _loop(fake, monkeypatch)

    loop.run()

    assert [entry[2] for entry in fake.created] == ["$"], "fallback must still restore service"
    _, fields = fake.recorded[0]
    assert fields["boundary_exact"] == "0"
    assert fields["group_start_policy"] == "$"
    assert fields["group_start_id"] == "", (
        "putting the symbol $ in a field documented as an exact id invites a "
        "reader to reason about a boundary that was never captured"
    )
    assert "BOUNDARY UNKNOWN" in fields["note"]
    assert "upper bound" not in fields["note"], (
        "the upper-bound claim does not hold when the cutoff is unknown"
    )


# --- Constructor-level ledger routing (prod's third finding) ----------------
#
# The tests above assign `loop.prefix` by hand, so they prove the ROUTING
# HELPER and not the real wiring. codex-arbmem-prod went to the actual
# constructors and found two that escaped: EvalConsumer stored the raw optional
# argument (None when the prefix comes from ARB_EVAL_PREFIX) and
# TranscriptConsumer stored no prefix at all — both with correctly namespaced
# SOURCE streams and an unprefixed ledger. A fake that is told the answer cannot
# catch a constructor that never worked it out.

import pytest
from unittest.mock import MagicMock


def _consumer_cases(prefix):
    from arb_memory.audit import AuditConsumer
    from arb_memory.bus import WriteLoop
    from arb_memory.close import CloseConsumer
    from arb_memory.eval import EvalConsumer
    from arb_memory.fetch import FetchConsumer
    from arb_memory.hint_reads import HintReadConsumer
    from arb_memory.transcript import TranscriptConsumer

    r, f, embed = MagicMock(), MagicMock(), (lambda text: [0.0])
    return {
        "WriteLoop": lambda: WriteLoop(r, f, embed=embed, prefix=prefix),
        "FetchConsumer": lambda: FetchConsumer(r, f, prefix=prefix),
        "AuditConsumer": lambda: AuditConsumer(r, f, prefix=prefix),
        "HintReadConsumer": lambda: HintReadConsumer(r, f, prefix=prefix),
        "EvalConsumer": lambda: EvalConsumer(r, f, prefix=prefix),
        "TranscriptConsumer": lambda: TranscriptConsumer(r, f, prefix=prefix),
        "CloseConsumer": lambda: CloseConsumer(r, f, prefix=prefix),
    }


@pytest.mark.parametrize("name", sorted(_consumer_cases("tenant-x:")))
def test_every_consumer_routes_its_ledger_into_its_own_namespace(name):
    consumer = _consumer_cases("tenant-x:")[name]()

    assert consumer._repair_ledger_stream() == "tenant-x:arbmem:group-repairs", (
        f"{name} source stream is {consumer.stream!r} but its repair ledger "
        f"is {consumer._repair_ledger_stream()!r} — the record escapes the "
        "namespace the stream lives in"
    )


def test_eval_routes_from_the_EFFECTIVE_prefix_not_the_raw_argument(monkeypatch):
    """prod's exact repro: prefix argument omitted, ARB_EVAL_PREFIX set."""
    monkeypatch.setenv("ARB_EVAL_PREFIX", "tenant-e:")
    monkeypatch.setenv("ARB_MEMORY_PREFIX", "")
    from arb_memory.eval import EvalConsumer

    consumer = EvalConsumer(MagicMock(), MagicMock())

    assert consumer.stream == "tenant-e:eval:events"
    assert consumer._repair_ledger_stream() == "tenant-e:arbmem:group-repairs", (
        "storing the raw None argument sent this tenant's repair records to the "
        "unprefixed ledger while its source stream was namespaced correctly"
    )


def test_a_subclass_that_forgets_the_ledger_prefix_cannot_construct():
    """The structural half of the fix: make the miss impossible, not unlikely.

    A default here would let a future consumer silently inherit the unprefixed
    ledger — which is exactly how Eval and Transcript escaped.
    """
    from arb_memory.consumer_loop import StreamConsumerLoop

    class ForgetfulConsumer(StreamConsumerLoop):
        def __init__(self):
            self._init_consumer_loop()

    with pytest.raises(TypeError):
        ForgetfulConsumer()

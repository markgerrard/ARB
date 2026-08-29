"""Shared mechanics for Redis stream consumers."""

from __future__ import annotations

import os
import threading
import logging
import math
from datetime import datetime, timezone

import redis


logger = logging.getLogger(__name__)
POISON_RETRY_LIMIT = int(os.environ.get("ARB_CONSUMER_POISON_RETRY_LIMIT", "5"))
# Repairs are rare and each record is small; keep plenty of history because the
# reconciliation this feeds may happen days after the fault (2026-08-09 took ten).
GROUP_REPAIR_RECORD_MAXLEN = int(os.environ.get("ARB_GROUP_REPAIR_RECORD_MAXLEN", "1000"))


def backoff_delay(failures: int, *, base: float = 0.5, cap: float | None = None) -> float:
    if cap is None:
        cap = float(os.environ.get("ARB_CONSUMER_BACKOFF_CAP_S", "30"))
    if failures <= 0 or base <= 0 or cap <= base:
        return min(base, cap)
    capped_failure = math.ceil(math.log2(cap / base))
    if failures >= capped_failure:
        return cap
    return base * (2 ** failures)


def is_missing_group(exc) -> bool:
    """True when Redis says the consumer group is gone.

    A stream can lose its group while the consumer stays connected and
    healthy: an eviction removes the stream (and the group with it), and the
    next producer XADD recreates the stream ALONE. Redis then answers every
    XREADGROUP with NOGROUP. That is a `ResponseError`, hence a `RedisError`,
    so a loop that only classifies transient-vs-poison backs off against it
    forever — the container stays up, its logs stay quiet, and the stream
    silently accumulates entries nobody will ever read.
    """
    return isinstance(exc, redis.ResponseError) and "NOGROUP" in str(exc)


def classify_infra_error(exc) -> str:
    """Classify backend failures without an unsafe Redis allowlist."""
    import psycopg

    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return "transient"
    if isinstance(exc, psycopg.Error):
        return "poison"
    if isinstance(exc, redis.DataError):
        return "poison"
    if isinstance(exc, redis.RedisError):
        return "transient"
    raise TypeError(f"not an infrastructure error: {type(exc).__name__}")


def sanitize_json(value):
    """Make a value safe for PostgreSQL Jsonb without dropping its shape."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {sanitize_json(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json(item) for item in value]
    return value


def repair_missing_group(
    exc, redis_client, stream, group, *, occurrence=None, ledger_stream=None
) -> bool:
    """Recreate a vanished consumer group. Returns True if `exc` was one.

    ONE policy, deliberately reachable by every caller rather than living on a
    base class. `d1c29779` put the repair inside `StreamConsumerLoop` and so
    fixed seven consumers while leaving `ReadLoop` — which is not a subclass —
    stalled: 2,879 read-loop failures in 24h on `arbmem:reads`. A shared
    failure mode needs a shared remedy in a place nothing is excluded from.

    Recreated from an explicitly measured id (falling back to `$`), NOT from
    `0`: entries already in the stream are NOT replayed. eval and trace persist
    the raw XADD stream id as globally unique, so a blind replay after a stream
    was recreated can collide. Restoring service and recovering a backlog are
    different operations.

    Returning True for a missing group even when the recreate FAILS is
    deliberate — the caller learns what kind of fault it saw, not whether the
    remedy worked; a NOPERM on xgroup must not be reported as an ordinary
    transient error.

    The strand is MEASURED before the recreate and recorded best-effort, because
    this log line already existed on 2026-08-09 and the loss still went
    unnoticed for ten days: it took a manual producer-ULID join against
    `idempotency_keys` to establish that ten `arbmem:writes` entries were
    skipped rather than processed. A line nobody reads is not a signal. The
    record bounds the search space for that reconciliation so it does not have
    to be reconstructed from scratch next time.

    What the numbers do and do not mean: `retained` is the whole stream at
    repair time, and MOST of it was almost certainly processed by the group
    before it vanished. It is an upper bound on the strand, not the strand.
    Only a join against the consumer's own idempotency record can say which
    entries were genuinely missed — that is exactly the operation this record
    exists to make possible.

    The group is created from the EXPLICITLY MEASURED last id, not from `$`.
    A live reproduction surfaced the race that makes `$` wrong here: measure,
    then a producer XADDs, then create — the group resolves `$` past the
    measured boundary and skips an entry that the record does not cover, so
    `retained` stops being an upper bound and a reconciliation trusting it
    misses real writes. Creating from the measured id closes the window in the
    safe direction: anything a producer adds during it lands AFTER the cutoff
    and is delivered normally instead of being silently skipped. `$` remains
    the fallback when the stream cannot be measured (empty, or the read was
    refused), which is the pre-existing behaviour.
    """
    if not is_missing_group(exc):
        return False
    from .bus import ensure_group

    # Measured BEFORE the recreate: afterwards `$` has moved and the evidence
    # is gone. Never allowed to prevent the repair — restoring service beats
    # recording why it broke.
    strand = _measure_strand(redis_client, stream)
    start_id = strand.get("last_retained") or "$"

    try:
        ensure_group(redis_client, stream, group, start_id=start_id)
    except redis.RedisError:
        logger.exception("failed to recreate missing consumer group %s on %s", group, stream)
        return True
    logger.error(
        "consumer group %s on stream %s was MISSING and has been recreated from %s%s. "
        "Entries at or before that id are NOT replayed and remain unread; "
        "recovering them is a separate deliberate operation. "
        "retained=%s first_retained=%s last_retained=%s. %s",
        group,
        stream,
        start_id,
        "" if occurrence is None else f" (occurrence {occurrence})",
        strand.get("retained", "unknown"),
        strand.get("first_retained", "unknown"),
        strand.get("last_retained", "unknown"),
        (
            "retained is an UPPER BOUND on the strand, not the strand — most "
            "were already processed."
            if start_id != "$"
            else "BOUNDARY UNKNOWN: created at $ because the stream could not "
            "be measured; retained is NOT a bound here and entries after it "
            "may also have been skipped."
        ),
    )
    _record_group_repair(
        redis_client, stream, group, occurrence, strand, start_id, ledger_stream
    )
    return True


def group_repairs_stream(prefix=None):
    """Stream carrying one record per group repair.

    Deliberately not imported from `bus` — `bus` imports THIS module, and the
    repair path must stay reachable by callers that are not consumer loops.

    The prefix is ROUTED EXPLICITLY by each consumer, never inferred. An
    earlier revision derived it from the source stream by looking for an
    `arbmem:` marker and claimed that "cannot disagree by construction";
    a live incident refuted it via `EvalConsumer`, whose stream is
    `{prefix}eval:events` and contains no such marker, so a tenant eval repair
    landed in the UNPREFIXED ledger. Inference covered six stream shapes and
    silently missed the seventh — which is the argument against inference, not
    an argument for a better pattern.
    """
    if prefix is None:
        prefix = os.environ.get("ARB_MEMORY_PREFIX", "")
    return f"{prefix}arbmem:group-repairs"


def _measure_strand(redis_client, stream):
    """Best-effort snapshot of what the recreate is about to orphan."""
    strand = {}
    try:
        strand["retained"] = redis_client.xlen(stream)
    except Exception:  # noqa: BLE001 - measurement must never break repair
        logger.warning("could not measure retained length on %s", stream, exc_info=True)
    for key, call in (
        ("first_retained", lambda: redis_client.xrange(stream, count=1)),
        ("last_retained", lambda: redis_client.xrevrange(stream, count=1)),
    ):
        try:
            rows = call()
            if rows:
                strand[key] = rows[0][0]
        except Exception:  # noqa: BLE001
            logger.warning("could not measure %s on %s", key, stream, exc_info=True)
    return strand


def _record_group_repair(
    redis_client, stream, group, occurrence, strand, start_id, ledger_stream=None
):
    """Persist the repair so a later reconciliation has something to start from.

    BEST-EFFORT, not durable. Written to Redis because that is the only sink
    EVERY caller has — tying it to a Postgres handle would exclude `ReadLoop`
    all over again, which is the `d1c29779` mistake this function exists to
    avoid.

    The `$` fallback is recorded as EXPLICITLY DEGRADED. A live incident
    forced XREVRANGE to NOPERM while XLEN succeeded, then raced an XADD in: the
    record claimed `retained` was an upper bound and put the symbolic `$` in a
    field documented as an exact cutoff, while the real resolved boundary was
    an id nobody captured. A degraded path that inherits the healthy path's
    guarantees is how an undercount gets trusted twice. So when the boundary is
    unknown it is reported as unknown, and the upper-bound claim is withheld.

    Honest limit: this lands in the same store whose eviction causes the fault
    it records. On a lossy instance (`allkeys-lru`, no AOF) the record can go
    the same way as the backlog. It is strictly better than nothing and
    strictly weaker than persistent storage; treat its absence as
    uninformative — never as evidence that no repair happened.
    """
    boundary_exact = start_id != "$"
    fields = {
        "stream": stream,
        "group": group,
        "occurrence": "" if occurrence is None else str(occurrence),
        # Exact cutoff when measured; blank when the boundary is unknown, so a
        # reader never mistakes the symbol `$` for an id it can reason about.
        "group_start_id": start_id if boundary_exact else "",
        "group_start_policy": "measured" if boundary_exact else "$",
        "boundary_exact": "1" if boundary_exact else "0",
        "retained": str(strand.get("retained", "")),
        "first_retained": str(strand.get("first_retained", "")),
        "last_retained": str(strand.get("last_retained", "")),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "retained is an upper bound on the strand, not the strand"
            if boundary_exact
            else "BOUNDARY UNKNOWN: created at $, resolved id not captured; "
            "retained is NOT a bound and entries after it may also be skipped"
        ),
    }
    try:
        redis_client.xadd(
            ledger_stream or group_repairs_stream(),
            fields,
        maxlen=GROUP_REPAIR_RECORD_MAXLEN,
            approximate=True,
        )
    except Exception:  # noqa: BLE001 - recording must never break repair
        logger.warning(
            "could not record group repair for %s on %s; the log line above is the only trace",
            group,
            stream,
            exc_info=True,
        )


class StreamConsumerLoop:
    """Recirculating loop shared by the write-like stream consumers."""

    def _init_consumer_loop(self, *, ledger_prefix):
        """`ledger_prefix` is REQUIRED, deliberately.

        Production found that `getattr(self, "prefix", None)` silently
        produced an unprefixed ledger for the two subclasses that do not store
        their EFFECTIVE prefix — EvalConsumer keeps the raw optional argument
        (None when the prefix comes from ARB_EVAL_PREFIX) and TranscriptConsumer
        never stored one at all. Both had correctly namespaced source streams
        while their repair records escaped. A keyword with no default turns that
        class of miss into a TypeError at construction: a new subclass cannot
        forget to answer the question.
        """
        self._ledger_prefix = ledger_prefix
        self._stop = threading.Event()
        self._infra_this_iteration = False
        self._pending_cursor = "0"
        self._poison = {}
        self._poison_retry_limit = int(os.environ.get("ARB_CONSUMER_POISON_RETRY_LIMIT", str(POISON_RETRY_LIMIT)))
        self._deadletter_sink_open = False
        self._group_reensured = 0

    def _repair_ledger_stream(self):
        """Where THIS consumer's repair records go.

        Explicit per-consumer routing, not inference: every consumer that
        supports a prefix knows its own, and `EvalConsumer`'s stream shape
        (`{prefix}eval:events`) proved inference cannot be made general.
        """
        return group_repairs_stream(prefix=self._ledger_prefix)

    def reensure_group(self):
        """Recreate this loop's group. See `repair_missing_group`."""
        self._group_reensured += 1
        from .bus import ensure_group

        ensure_group(self.redis, self.stream, self.group)

    def _handle_redis_error(self, exc):
        """Record a Redis fault, and repair it in-band if it is a lost group.

        Backing off against NOGROUP is futile — no amount of waiting brings a
        deleted group back. Every path that catches RedisError must route
        through here, including `drain_pending`, which swallows its own
        exception rather than re-raising; a consumer that only ever drains
        pending entries would otherwise never repair itself.
        """
        self._infra_this_iteration = True
        if is_missing_group(exc):
            self._group_reensured += 1
            repair_missing_group(
                exc,
                self.redis,
                self.stream,
                self.group,
                occurrence=self._group_reensured,
                ledger_stream=self._repair_ledger_stream(),
            )

    def _run_guarded(self, operation):
        """Run one loop step, converting Redis faults into a backoff signal."""
        try:
            operation()
        except redis.RedisError as exc:
            self._handle_redis_error(exc)

    def run(self):
        failures = 0
        self._infra_this_iteration = False
        self._run_guarded(self.drain_pending)
        if self._infra_this_iteration:
            failures = 1
            self._stop.wait(backoff_delay(failures))
        while not self._stop.is_set():
            self._infra_this_iteration = False
            self._run_guarded(self._tick)
            if self._infra_this_iteration:
                failures += 1
                self._stop.wait(backoff_delay(failures))
            else:
                failures = 0

    def _tick(self):
        self.step()
        rows = self.redis.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: self._pending_cursor},
            count=1,
        )
        entry = self._one_from_rows(rows)
        if entry is None:
            self._pending_cursor = "0"
            return None
        self._pending_cursor = entry[0]
        return self._handle_entry(*entry)

    def step(self):
        rows = self.redis.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: ">"},
            count=1,
            block=self.block_ms,
        )
        entry = self._one_from_rows(rows)
        if entry is None:
            return None
        return self._handle_entry(*entry)

    def drain_pending(self, *, limit=None):
        drained = 0
        while limit is None or drained < limit:
            try:
                rows = self.redis.xreadgroup(
                    self.group,
                    self.consumer,
                    {self.stream: "0"},
                    count=1,
                )
            except redis.RedisError as exc:
                self._handle_redis_error(exc)
                return drained
            entry = self._one_from_rows(rows)
            if entry is None:
                return drained
            result = self._handle_entry(*entry)
            if not result:
                return drained
            drained += 1
        return drained

    def stop(self):
        self._stop.set()

    def _ack(self, entry_id):
        try:
            self.redis.xack(self.stream, self.group, entry_id)
        except redis.RedisError:
            self._infra_this_iteration = True
            raise

    def _publish_terminal(self, entry_id, fields):
        hook = getattr(self, "_publish_result", None)
        if hook is not None:
            hook(entry_id, fields, None, False)

    def _with_fresh_conn(self, callback):
        conn = None
        try:
            conn = self.conn_factory()
            return callback(conn)
        finally:
            if conn is not None and not getattr(conn, "closed", True):
                conn.close()

    def _canary_deadletter_sink(self):
        """Probe the deadletter table; subclasses may provide a narrower probe."""
        table = getattr(self, "deadletter_table", None)
        if not table:
            return False
        from psycopg.types.json import Jsonb

        conn = None
        try:
            conn = self.conn_factory()
            with conn.transaction():
                if table == "write_deadletter":
                    conn.execute(
                        "INSERT INTO write_deadletter (ulid, payload, error, stream_entry_id) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (stream_entry_id) DO NOTHING",
                        ("__canary__", Jsonb({}), "canary", "__canary__"),
                    )
                elif table == "audit_close_deadletter":
                    conn.execute(
                        "INSERT INTO audit_close_deadletter "
                        "(raw_entry, error, stream_entry_id) VALUES (%s, %s, %s) "
                        "ON CONFLICT (stream_entry_id) DO NOTHING",
                        (Jsonb({}), "canary", "__canary__"),
                    )
                else:
                    conn.execute(
                        f"INSERT INTO {table} (stream_entry_id) VALUES (%s) "
                        "ON CONFLICT (stream_entry_id) DO NOTHING",
                        ("__canary__",),
                    )
            return True
        except Exception:
            return False
        finally:
            if conn is not None and not getattr(conn, "closed", True):
                conn.close()

    def _deadletter_failed(self, entry_id, fields, error, on_terminal):
        try:
            disposition = classify_infra_error(error)
        except TypeError:
            disposition = "poison"
        if disposition == "transient":
            self._infra_this_iteration = True
            return None
        if self._canary_deadletter_sink():
            self._poison.pop(entry_id, None)
            logger.error("deadletter-unstorable entry_id=%s error=%s", entry_id, error)
            if on_terminal is not None:
                on_terminal()
            self._publish_terminal(entry_id, fields)
            self._ack(entry_id)
            return True
        self._deadletter_sink_open = True
        self._poison.clear()
        self._infra_this_iteration = True
        logger.error("deadletter-sink-poison entry_id=%s error=%s", entry_id, error)
        return False

    def _retry_or_exhaust(self, entry_id, fields, error, deadletter, on_terminal=None):
        try:
            disposition = classify_infra_error(error)
        except TypeError:
            disposition = "poison"
        if disposition == "transient":
            self._poison.pop(entry_id, None)
            self._infra_this_iteration = True
            return None
        if self._deadletter_sink_open:
            if self._canary_deadletter_sink():
                self._deadletter_sink_open = False
            else:
                self._infra_this_iteration = True
                return None
        count = self._poison[entry_id] = self._poison.get(entry_id, 0) + 1
        if count < self._poison_retry_limit:
            return None
        try:
            result = deadletter()
            if result is False:
                raise RuntimeError("deadletter helper returned false")
        except Exception as deadletter_error:
            return self._deadletter_failed(entry_id, fields, deadletter_error, on_terminal)
        self._poison.pop(entry_id, None)
        if on_terminal is not None:
            on_terminal()
        self._publish_terminal(entry_id, fields)
        self._ack(entry_id)
        return True

    @staticmethod
    def _one_from_rows(rows):
        if not rows:
            return None
        _, entries = rows[0]
        return entries[0] if entries else None

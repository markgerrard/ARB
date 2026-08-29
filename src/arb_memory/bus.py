"""ARB Memory bus helpers require a Redis client with decode_responses=True."""

import json
import logging
import math
import os
import threading
import uuid
from datetime import datetime, timezone

import redis

from .consumer_loop import (
    StreamConsumerLoop,
    group_repairs_stream,
    backoff_delay,
    classify_infra_error,
    repair_missing_group,
    sanitize_json,
)

logger = logging.getLogger(__name__)
PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
MAXLEN = 10_000
REPLY_TTL = 30
WRITE_RESULT_TTL = 30
GROUP = "arbmem-memory"
# Cap for hint_read query columns (guide §6 / mcp/config.py search_max_query_chars default).
SEARCH_MAX_QUERY_CHARS = 2000


def reads_stream(prefix=PREFIX):
    return f"{prefix}arbmem:reads"


def writes_stream(prefix=PREFIX):
    return f"{prefix}arbmem:writes"


def hint_reads_stream(prefix=PREFIX):
    """Record-intent stream for served-hint recording (bus tier)."""
    return f"{prefix}arbmem:hint-reads"


def reply_key(cid, prefix=PREFIX):
    return f"{prefix}arbmem:reply:{cid}"


def write_result_key(request_id, prefix=PREFIX):
    return f"{prefix}arbmem:write_result:{request_id}"


def ensure_group(redis, stream, group, start_id="$"):
    """Create the group if absent. Default `$` keeps every existing caller's
    behaviour; the repair path passes an explicitly measured id so the
    measure-then-create window cannot skip an entry (see
    `consumer_loop.repair_missing_group`)."""
    try:
        redis.xgroup_create(stream, group, id=start_id, mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def new_ulid():
    return uuid.uuid4().hex


def memory_write(
    redis,
    *,
    artefact=None,
    hints=(),
    source="seat",
    author="unknown",
    ulid=None,
    request_id=None,
    prefix=PREFIX,
):
    # Third door (design §3): expected_version without a result channel would be
    # committed, acked, and never observed — refuse before XADD.
    if (
        isinstance(artefact, dict)
        and artefact.get("expected_version") is not None
        and request_id is None
    ):
        raise ValueError("expected_version requires a result channel")
    ulid = ulid or new_ulid()
    payload_hints = []
    for hint in hints:
        payload_hint = dict(hint)
        payload_hint.setdefault("source", source)
        payload_hint.setdefault("author", author)
        payload_hints.append(payload_hint)

    if artefact is not None and payload_hints:
        kind = "artefact+hints"
    elif artefact is not None:
        kind = "artefact"
    else:
        kind = "hints"

    payload = {
        "ulid": ulid,
        "kind": kind,
        "artefact": artefact,
        "hints": payload_hints,
    }
    fields = {"ulid": ulid, "payload": json.dumps(payload)}
    if request_id is not None:
        fields["request_id"] = request_id
    redis.xadd(
        writes_stream(prefix),
        fields,
        maxlen=MAXLEN,
        approximate=True,
    )
    return ulid


def _prepare_hints(hints, embed):
    prepared = []
    for hint in hints:
        row = dict(hint)
        row["embedding"] = embed(row["text"])
        prepared.append(row)
    return prepared


def handle_write_intent(conn, intent, *, embed):
    from . import store
    from psycopg.types.json import Jsonb

    hints = _prepare_hints(intent.get("hints", ()), embed)
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO idempotency_keys (key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING key",
            (intent["ulid"],),
        ).fetchone()
        if row is None:
            stored = conn.execute(
                "SELECT receipt FROM idempotency_keys WHERE key = %s",
                (intent["ulid"],),
            ).fetchone()
            return (stored[0] if stored and stored[0] is not None else {"artefact_outcome": "unknown"}), True
        receipt = store.write_artefact_and_hints(conn, artefact=intent.get("artefact"), hints=hints)
        conn.execute(
            "UPDATE idempotency_keys SET receipt = %s WHERE key = %s",
            (Jsonb(receipt), intent["ulid"]),
        )
        return receipt, False


class WriteLoop(StreamConsumerLoop):
    def __init__(self, redis, conn_factory, *, embed, prefix=PREFIX, consumer="writer", block_ms=1000):
        self.redis = redis
        self.conn_factory = conn_factory
        self.conn = conn_factory()
        self.embed = embed
        self.prefix = prefix
        self.consumer = consumer
        self.block_ms = block_ms
        self.stream = writes_stream(prefix)
        self.group = GROUP
        self.deadletter_table = "write_deadletter"
        self._init_consumer_loop(ledger_prefix=self.prefix)
        ensure_group(self.redis, self.stream, GROUP)

    def read_one(self):
        rows = self.redis.xreadgroup(
            GROUP,
            self.consumer,
            {self.stream: ">"},
            count=1,
            block=self.block_ms,
        )
        entry = self._one_from_rows(rows)
        if entry is None:
            return None
        entry_id, fields = entry
        return entry_id, self._parse_intent(fields)

    def handle_and_ack(self, entry_id, intent):
        handle_write_intent(self.conn, intent, embed=self.embed)
        self.redis.xack(self.stream, GROUP, entry_id)

    @staticmethod
    def _parse_intent(fields):
        intent = json.loads(fields["payload"])
        if not isinstance(intent, dict) or "ulid" not in intent:
            raise ValueError("write intent missing required ulid")
        return intent

    def _deadletter(self, entry_id, ulid, payload, error):
        from psycopg.types.json import Jsonb

        conn = None
        try:
            conn = self.conn_factory()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO write_deadletter "
                    "(ulid, payload, error, stream_entry_id) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (stream_entry_id) DO NOTHING",
                    (ulid, Jsonb(sanitize_json(payload)), str(error)[:2000], entry_id),
                )
            return True
        except Exception:
            logger.exception("failed to deadletter write entry %s", ulid)
            raise
        finally:
            if conn is not None and not getattr(conn, "closed", True):
                conn.close()

    def _publish_result(self, entry_id, fields, receipt, is_replay):
        request_id = fields.get("request_id")
        if request_id is None:
            return None
        envelope = dict(receipt or {"artefact_outcome": "failed", "reason": "deadlettered"})
        envelope["duplicate"] = is_replay
        key = write_result_key(request_id, self.prefix)
        self.redis.lpush(key, json.dumps(envelope, default=str))
        self.redis.expire(key, WRITE_RESULT_TTL)

    def _handle_entry(self, entry_id, fields):
        try:
            intent = self._parse_intent(fields)
        except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
            logger.exception("deadlettering malformed write entry %s", entry_id)
            try:
                if not self._deadletter(entry_id, fields.get("ulid", "unknown"), {"raw": fields}, exc):
                    raise RuntimeError("deadletter helper returned false")
            except Exception as deadletter_error:
                return self._deadletter_failed(entry_id, fields, deadletter_error, None)
            self._publish_terminal(entry_id, fields)
            self._ack(entry_id)
            return True

        try:
            receipt, is_replay = handle_write_intent(self.conn, intent, embed=self.embed)
        except Exception as exc:
            logger.exception("write loop failed to handle entry %s", entry_id)
            try:
                if classify_infra_error(exc) == "transient":
                    try:
                        old_conn = self.conn
                        new_conn = self.conn_factory()
                        self.conn = new_conn
                        if old_conn is not new_conn and not getattr(old_conn, "closed", True):
                            try:
                                old_conn.close()
                            except Exception:
                                logger.exception("write loop failed to close superseded connection")
                    except Exception:
                        logger.exception("write loop failed to refresh connection after %s", entry_id)
            except TypeError:
                pass
            return self._retry_or_exhaust(
                entry_id,
                fields,
                exc,
                lambda: self._deadletter(entry_id, intent["ulid"], intent, exc),
            )
        self._poison.pop(entry_id, None)
        self._publish_result(entry_id, fields, receipt, is_replay)
        self._ack(entry_id)
        return True

def handle_read_request(redis, conn, request, *, embed, prefix=PREFIX):
    from . import store
    from .mcp.read_tools import _query_columns

    cid = request["cid"]
    reply = request["reply"]
    if reply != reply_key(cid, prefix=prefix):
        return

    failure = None
    hits = None
    try:
        hits = store.retrieve(conn, request["query"], k=int(request.get("k", 8)), embed=embed)
        envelope = {"status": "ok", "hits": hits}
        outcome = "ok"
    except Exception as exc:
        # LOG, don't just swallow. Pre-2026-07-28 this except block was silent, so a read lane
        # dead for 7 days produced zero log lines — the caller sees `None`, which is the
        # documented "fall back to grep" signal, so a broken lane is indistinguishable from an
        # ordinary cache miss at every layer. Silence was the reason nobody noticed.
        logger.exception("read request %s failed", cid)
        failure = exc
        envelope = {"status": "error", "error": str(exc)}
        outcome = "error"

    # Producer timestamp for the record-intent: reply-build time, not XADD time.
    served_at = datetime.now(timezone.utc).isoformat()
    redis.lpush(reply, json.dumps(envelope, default=str))
    redis.expire(reply, REPLY_TTL)

    # Fire-and-forget record-intent (S4a). Construction + serialization + XADD share one
    # guard so a construction-time failure cannot escape and block ReadLoop's _ack (§7).
    try:
        raw_query = request["query"]
        capped = raw_query[:SEARCH_MAX_QUERY_CHARS]
        query_truncated = "1" if len(raw_query) > SEARCH_MAX_QUERY_CHARS else "0"
        query_hmac, query_text_to_store = _query_columns(capped)
        k = int(request.get("k", 8))

        wire_hits = []
        if outcome == "ok" and hits is not None:
            for rank, hit in enumerate(hits, start=1):
                inner = hit["hint"]  # H-02: metrics under store.retrieve's "hint" key
                wire_hits.append(
                    {
                        "hint_id": inner["id"],
                        "rank": rank,
                        "withheld": hit["withheld"],  # outer level (Part A / D-3)
                        "vector_distance": inner.get("vector_distance"),
                        "lexical_rank": inner.get("lexical_rank"),
                    }
                )

        fields = {
            "query_truncated": query_truncated,
            "k": str(k),
            "outcome": outcome,
            "cid": cid,
            "served_at": served_at,
            "hits": json.dumps(wire_hits),
        }
        if query_hmac is not None:
            fields["query_hmac"] = query_hmac
        if query_text_to_store is not None:
            fields["query_text"] = query_text_to_store
        if outcome == "ok":
            fields["hit_count"] = str(len(hits) if hits is not None else 0)
        # run_id / seat_id omitted — not on the current wire request; consumer stores NULL.

        redis.xadd(
            hint_reads_stream(prefix),
            fields,
            maxlen=MAXLEN,
            approximate=True,
        )
    except Exception:
        logger.exception("hint-read record-intent failed for %s", cid)

    # Returned so ReadLoop can refresh a reaped connection. The reply is ALWAYS sent first: a
    # blocked seat must be unblocked whether or not the lane can recover.
    return failure


class ReadLoop:
    """Read pending recovery is cleanup-only: stale reads are acked without re-replying."""

    def __init__(self, redis, conn_factory, *, embed, prefix=PREFIX, consumer="reader", block_ms=1000):
        self.redis = redis
        # A CALLABLE is REQUIRED. Incident 2026-07-28: MemoryConsumer handed ReadLoop a
        # materialised conn_factory(), so the lane could not rebuild and recall stayed dead for
        # 7 days. The first remediation accepted a bare connection by wrapping it in a lambda
        # returning the same object — three review seats showed that is a SILENT no-reconnect
        # trap (refresh creates nothing, closes nothing, logs nothing). Refuse it loudly.
        if not callable(conn_factory):
            raise TypeError(
                "ReadLoop requires a callable conn_factory, not a connection: a materialised "
                "connection cannot be rebuilt when the database reaps it"
            )
        self.conn_factory = conn_factory
        self.conn = self._new_conn()
        self.embed = embed
        self.prefix = prefix
        self.consumer = consumer
        self.block_ms = block_ms
        self.stream = reads_stream(prefix)
        self.group = GROUP
        self._stop = threading.Event()
        ensure_group(self.redis, self.stream, self.group)

    def step(self):
        rows = self.redis.xreadgroup(
            GROUP,
            self.consumer,
            {self.stream: ">"},
            count=1,
            block=self.block_ms,
        )
        entry = self._one_from_rows(rows)
        if entry is None:
            return None
        return self._handle_entry(*entry)

    def drain_pending(self):
        while True:
            try:
                rows = self.redis.xreadgroup(GROUP, self.consumer, {self.stream: "0"}, count=1)
                entry = self._one_from_rows(rows)
                if entry is None:
                    return
                entry_id, _ = entry
                self._ack(entry_id)
            except redis.RedisError as exc:
                logger.exception("read loop pending cleanup failed")
                # ReadLoop is not a StreamConsumerLoop, so it does not inherit
                # the repair; wiring it explicitly is the whole point of the
                # policy living in a free function.
                repair_missing_group(
                    exc,
                    self.redis,
                    self.stream,
                    self.group,
                    ledger_stream=group_repairs_stream(prefix=self.prefix),
                )
                raise

    def run(self):
        failures = 0
        try:
            self.drain_pending()
        except redis.RedisError:
            failures = 1
            logger.exception("read loop pending cleanup failed")
            self._stop.wait(backoff_delay(failures))
        while not self._stop.is_set():
            infra = False
            try:
                self.step()
            except redis.RedisError as exc:
                infra = True
                logger.exception("read loop failed")
                repair_missing_group(
                    exc,
                    self.redis,
                    self.stream,
                    self.group,
                    ledger_stream=group_repairs_stream(prefix=self.prefix),
                )
            if infra:
                failures += 1
                self._stop.wait(backoff_delay(failures))
            else:
                failures = 0

    def stop(self):
        self._stop.set()

    @staticmethod
    def _one_from_rows(rows):
        if not rows:
            return None
        _, entries = rows[0]
        if not entries:
            return None
        return entries[0]

    @staticmethod
    def _parse_request(fields):
        for key in ("cid", "reply", "query"):
            if key not in fields:
                raise ValueError(f"read request missing required {key}")
        return fields

    def _ack(self, entry_id):
        try:
            self.redis.xack(self.stream, GROUP, entry_id)
        except redis.RedisError:
            raise

    def _handle_entry(self, entry_id, fields):
        try:
            request = self._parse_request(fields)
        except (TypeError, ValueError):
            logger.exception("dropping malformed read entry %s", entry_id)
            self._ack(entry_id)
            return None

        try:
            failure = handle_read_request(
                self.redis, self.conn, request, embed=self.embed, prefix=self.prefix
            )
        except Exception:
            logger.exception("read loop failed to handle entry %s", entry_id)
            return None
        if failure is not None:
            self._refresh_conn(failure, entry_id)
        self._ack(entry_id)
        return request

    def _new_conn(self):
        """A connection for the read lane, in autocommit.

        Reads need no transaction, and a non-autocommit connection is left INTRANS by the first
        SELECT and never returns to IDLE (`store.retrieve` is SELECT-only: no commit, no
        rollback). That INTRANS-idle state is what the server reaps
        (`idle_in_transaction_session_timeout` -> SQLSTATE 25P03) and it pins a transaction
        snapshot on `hints` indefinitely even when nothing reaps it. Verified live in production:
        one backend idle-in-transaction for 3h32m, xact_age == time since restart.
        """
        import psycopg  # lazy: arb_memory.client imports this module and must stay light
                          # (test_client_import_does_not_load_server_dependencies)

        conn = self.conn_factory()
        # A stub with no connection API at all has nothing to configure: several tests exercise
        # only the Redis xack/pending paths and pass `object()`. ABSENCE of the attribute means
        # "not a psycopg connection"; PRESENCE means the contract below is enforced, raising on
        # failure. Note `hasattr` only swallows AttributeError, so a property that raises anything
        # else still propagates -- a real connection can never slip through this branch.
        if not hasattr(conn, "autocommit"):
            return conn
        if conn.autocommit:
            return conn
        try:
            # Branch on the ACTUAL status: `!= IDLE` lumps INTRANS and INERROR together, and
            # COMMIT is correct for only one of them (round 3, opus5, P1).
            status = conn.info.transaction_status
            if status == psycopg.pq.TransactionStatus.INERROR:
                # REFUSE IT. Round 4 (opus5 F4, codex F1, agy F2 -- three seats converging).
                #
                # COMMIT on an ABORTED transaction executes as ROLLBACK and raises NOTHING
                # (verified, psycopg 3.3.4), so by the time we are here the factory's session setup
                # is already gone and unrecoverable. The previous version logged that and served
                # the connection anyway. opus5 measured where that leads: the lane is live,
                # autocommit and IDLE, on the wrong schema; every read raises UndefinedTable, which
                # `classify_infra_error` calls "poison"; `_conn_unusable()` is False because the
                # connection is perfectly healthy; so `_refresh_conn` NEVER rebuilds. One log line
                # at construction, then permanent silence, and `memory_query` returns None --
                # indistinguishable from a cache miss at every layer. That is the EXACT 7-day
                # outage failure class, reintroduced by the branch added to eliminate it.
                #
                # It was also internally inconsistent: a commit FAILURE (setup possibly still
                # intact) closed and raised, while INERROR (setup DEFINITELY lost) logged and
                # served -- the strictly worse case got the lenient treatment. Both refuse now.
                logger.error(
                    "read loop: factory returned an ABORTED transaction (INERROR); its session "
                    "setup is irrecoverable, so the connection is being REFUSED rather than "
                    "served against a possibly-wrong schema."
                )
                raise RuntimeError(
                    "ReadLoop requires a connection with no aborted transaction: the factory "
                    "returned one in INERROR, whose session setup is lost. Serving it would give "
                    "a lane that reads from the wrong schema and never rebuilds."
                )
            if status != psycopg.pq.TransactionStatus.IDLE:
                # INTRANS -- and incidentally UNKNOWN/ACTIVE, which reach `commit()` and raise, so
                # they are handled by the close-and-raise below. Round 4 (opus5 F5) corrected an
                # earlier comment here that claimed this branch was INTRANS-only.
                #
                # COMMIT, never roll back. A plain `SET` inside a transaction block IS
                # transactional, so a rollback silently discards the factory's own session setup.
                # Verified in round 2 (panel-readloop-r2-20260728T085541Z-ba19b8): the rollback
                # reverted search_path to `"$user", public`, after which every read raised
                # UndefinedTable -- while the test asserting IDLE still passed. A false-green
                # inside the fix for a false-green.
                #
                # A commit FAILURE is deliberately NOT recovered. Round 3 (codex, P1, reproduced)
                # showed the previous `except: rollback()` fallback then continued and returned a
                # LIVE connection whose setup had been discarded -- lane healthy, wrong schema, no
                # caller able to tell. Let it propagate to the handler below, which closes and
                # raises.
                #
                # POLICY, undocumented before round 3 (opus5): committing means this lane commits
                # work it does not own. A factory that opened a transaction intending to roll it
                # back (test-isolation fixtures do this) would have its writes committed. The
                # contract is therefore: a ReadLoop factory MUST NOT return a connection holding
                # a transaction it expects to be discarded.
                conn.commit()
            conn.autocommit = True
        except Exception:
            # RAISE rather than return. The previous `except: return conn` handed back a
            # non-autocommit connection, reinstating the exact pre-incident shape; a read failing
            # on it then leaves INERROR, which `_conn_unusable` did not recognise -- the two
            # composed into a permanent wedge in the original outage shape.
            #
            # WHERE THE RETRY ACTUALLY IS: `_refresh_conn` catches, logs, keeps the old connection,
            # and the lane retries on the next inbound read. That is true THERE and NOT in
            # `__init__`, where there is no old connection to keep -- a raise from `__init__`
            # propagates through `MemoryConsumer.__init__` and `run_memory()` dies at boot.
            #
            # BLAST RADIUS, corrected twice. Round 3 said the raise was a local retry (wrong --
            # not at `__init__`). Round 4 said it took all three lanes down as though that were new
            # (also wrong): `WriteLoop.__init__` already calls `conn_factory()` before `ReadLoop` is
            # constructed, so a boot-time DB failure already kills `MemoryConsumer` today, with or
            # without this change (opus5, round 4, Q3). The genuinely NEW boot-death path is narrow:
            # factory reachable, but commit/autocommit fails on the connection it returns. Deliberate
            # -- a service that cannot build a correct read lane should not come up half-working --
            # and compose `restart: unless-stopped` retries the boot.
            try:
                conn.close()  # do not leak the socket we are refusing to use
            except Exception:
                logger.exception("read loop failed to close a connection it could not configure")
            raise
        return conn

    def _conn_unusable(self):
        """True when the current connection cannot serve another read.

        Only meaningful AFTER a failed use. A server-reaped backend still reports
        INTRANS/closed=False until the next statement touches it (verified round 2), so this is
        NOT a valid pre-flight probe -- hoisting it to one yields a silent false negative.
        """
        if getattr(self.conn, "closed", False):
            return True
        import psycopg  # lazy, see _new_conn

        try:
            status = self.conn.info.transaction_status
        except Exception:
            # Defensive only, and DEAD for real psycopg connections: a genuinely closed connection
            # reports `.closed = True` (caught above) and its `.info.transaction_status` returns
            # UNKNOWN rather than raising (verified round 3, opus5 F3). Kept for non-psycopg
            # connection objects, which the factory contract permits.
            #
            # ATTRIBUTION CORRECTED (round 3, opus5 F3): this branch did NOT fix agy's round-2
            # fail-open finding -- adding UNKNOWN/INERROR to the status tuple below did. An earlier
            # version of this comment claimed the credit, and confidently-wrong prose about what
            # fixed what is exactly how round 2 shipped a false-green.
            logger.exception("read loop could not read connection status; treating as unusable")
            return True
        return status in (
            psycopg.pq.TransactionStatus.UNKNOWN,
            # An aborted transaction raises InFailedSqlTransaction on EVERY further statement
            # until rollback, so the lane must rebuild. Reachable whenever the lane is not
            # autocommit -- precisely what a failed `_new_conn` used to leave behind.
            psycopg.pq.TransactionStatus.INERROR,
        )

    def _refresh_conn(self, exc, entry_id):
        """Rebuild the Postgres connection after a transient failure (mirrors WriteLoop).

        Without this the lane is wedged permanently by a single reaped connection, and recall is
        the lowest-traffic lane so it is the one the database reaps first.
        """
        # Rebuild when the exception is transient OR the connection is simply unusable. Keying
        # only on classify_infra_error was wrong: the reap that actually happens in production is
        # idle_in_transaction_session_timeout -> 25P03 (IdleInTransactionSessionTimeout), which is
        # NOT a psycopg.OperationalError and so classifies "poison" — the first remediation
        # returned here and left the lane wedged with the fix installed.
        try:
            transient = classify_infra_error(exc) == "transient"
        except TypeError:
            transient = False
        if not (transient or self._conn_unusable()):
            return
        try:
            old_conn = self.conn
            new_conn = self._new_conn()
            self.conn = new_conn
            if old_conn is not new_conn and not getattr(old_conn, "closed", True):
                try:
                    old_conn.close()
                except Exception:
                    logger.exception("read loop failed to close superseded connection")
        except Exception:
            logger.exception("read loop failed to refresh connection after %s", entry_id)


class MemoryConsumer:
    def __init__(self, redis, conn_factory, *, embed, prefix=PREFIX):
        from .fetch import FetchConsumer

        self.write_loop = WriteLoop(redis, conn_factory, embed=embed, prefix=prefix, consumer="writer")
        # The FACTORY, not conn_factory() — see the ReadLoop docstring and the 2026-07-28
        # incident. Calling it here is what made recall unrecoverable.
        self.read_loop = ReadLoop(redis, conn_factory, embed=embed, prefix=prefix, consumer="reader")
        self.fetch_loop = FetchConsumer(redis, conn_factory, prefix=prefix, consumer="fetch")
        self.threads = []

    def start(self):
        if self.threads:
            return
        self.write_loop._stop.clear()
        self.read_loop._stop.clear()
        self.fetch_loop._stop.clear()
        self.threads = [
            threading.Thread(target=self.write_loop.run, daemon=True),
            threading.Thread(target=self.read_loop.run, daemon=True),
            threading.Thread(target=self.fetch_loop.run, daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def stop(self):
        self.write_loop.stop()
        self.read_loop.stop()
        self.fetch_loop.stop()
        for thread in self.threads:
            thread.join(timeout=2.0)


def memory_query(redis, query_text, k=8, *, timeout_s, prefix=PREFIX):
    cid = uuid.uuid4().hex
    reply = reply_key(cid, prefix=prefix)
    redis.xadd(
        reads_stream(prefix),
        {"cid": cid, "reply": reply, "query": query_text, "k": str(k)},
        maxlen=MAXLEN,
        approximate=True,
    )

    timeout = max(1, math.ceil(timeout_s))
    popped = redis.blpop(reply, timeout=timeout)
    if popped is None:
        return None

    _, raw = popped
    envelope = json.loads(raw)
    if envelope.get("status") != "ok":
        return None
    return envelope.get("hits")

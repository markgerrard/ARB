"""Served-hint record — bus consumer, sink, deadletter, retention purge.

Mirrors ``arb_memory.eval``: StreamConsumerLoop drains the hint-reads stream;
HintReadSink writes parent+hits; malformed entries go to hint_read_deadletter.
Retention ages out ``hint_read`` rows (hits cascade; deadletter is kept).
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime

import psycopg
import redis
from psycopg.types.json import Jsonb

from .bus import ensure_group, hint_reads_stream
from .consumer_loop import StreamConsumerLoop, sanitize_json

logger = logging.getLogger(__name__)

# G-09 / H-04: deterministic namespace for bus-tier read_id (literal, not uuid.NAMESPACE_*).
HINT_READ_NAMESPACE = uuid.UUID("2f8e6b1a-4b76-4b8e-9a2b-6f6e6a2e6a11")
HINT_READS_GROUP = "arbmem-hint-reads"


def _qualified_entry_id(prefix: str, entry_id: str) -> str:
    """Fully-qualified stream key + entry id.

    Fed into ``read_id``'s uuid5 *and* stored as the ``stream_entry_id`` column
    value (H-04) so that column is globally unique without changing either
    table's single-column ``UNIQUE (stream_entry_id)`` shape.
    """
    return f"{hint_reads_stream(prefix)}/{entry_id}"


def _bus_read_id(prefix: str, entry_id: str) -> uuid.UUID:
    """Deterministic uuid5 under HINT_READ_NAMESPACE — redelivery is idempotent."""
    return uuid.uuid5(HINT_READ_NAMESPACE, _qualified_entry_id(prefix, entry_id))


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value in ("1", "true", "True", 1):
        return True
    if value in ("0", "false", "False", 0, None, ""):
        return False
    raise ValueError(f"hint-read event invalid boolean: {value!r}")


def _parse_hint_read_event(fields) -> dict:
    """Parse the wire element ``handle_read_request`` XADDs (S4a / D-4 columns).

    Wire hits are a flat JSON array of
    ``{hint_id, rank, withheld, vector_distance, lexical_rank}``. Re-nest into
    the H-02 shape the sink reads: ``hit["hint"]["id"]`` etc., ``hit["withheld"]``
    at the outer level (ERRATA S4a: nested access is the parse concern).
    """
    outcome = fields.get("outcome")
    if not outcome:
        raise ValueError("hint-read event missing outcome")
    if outcome not in ("ok", "error"):
        raise ValueError(f"hint-read event invalid outcome: {outcome!r}")
    if not fields.get("k") and fields.get("k") != 0:
        raise ValueError("hint-read event missing k")
    if not fields.get("served_at"):
        raise ValueError("hint-read event missing served_at")

    raw_hits = fields.get("hits", "[]")
    try:
        if isinstance(raw_hits, str):
            hits_wire = json.loads(raw_hits)
        else:
            hits_wire = raw_hits
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("hint-read event invalid hits") from exc
    if not isinstance(hits_wire, list):
        raise ValueError("hint-read event hits must be a list")

    nested_hits = []
    for item in hits_wire:
        if not isinstance(item, dict):
            raise ValueError("hint-read event hit must be an object")
        if "hint_id" not in item:
            raise ValueError("hint-read event hit missing hint_id")
        if "withheld" not in item:
            raise ValueError("hint-read event hit missing withheld")
        nested_hits.append(
            {
                "hint": {
                    "id": item["hint_id"],
                    "vector_distance": item.get("vector_distance"),
                    "lexical_rank": item.get("lexical_rank"),
                },
                "withheld": bool(item["withheld"]),
            }
        )

    hit_count_raw = fields.get("hit_count")
    if hit_count_raw is None or hit_count_raw == "":
        hit_count = len(nested_hits) if outcome == "ok" else 0
    else:
        try:
            hit_count = int(hit_count_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("hint-read event invalid hit_count") from exc

    try:
        k = int(fields["k"])
    except (TypeError, ValueError) as exc:
        raise ValueError("hint-read event invalid k") from exc

    # served_at is ISO text from the producer; psycopg accepts it for timestamptz.
    served_at = fields["served_at"]
    # Validate it is parseable enough to catch garbage early.
    try:
        if isinstance(served_at, str):
            datetime.fromisoformat(served_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("hint-read event invalid served_at") from exc

    return {
        "outcome": outcome,
        "run_id": fields.get("run_id") or None,
        "seat_id": fields.get("seat_id") or None,
        "query_hmac": fields.get("query_hmac"),
        "query_text": fields.get("query_text"),
        "query_truncated": _as_bool(fields.get("query_truncated", "0")),
        "k": k,
        "hit_count": hit_count,
        "cid": fields.get("cid"),
        "served_at": served_at,
        "hits": nested_hits,
    }


class HintReadSink:
    """Parent+hits write for the bus tier (H-10).

    ``ON CONFLICT (read_id) DO NOTHING RETURNING read_id``; if no row comes back
    the entry is a clean redelivery — return ``"duplicate"`` and do not touch
    ``hint_read_hit`` (re-inserting would collide on ``PRIMARY KEY (read_id, rank)``
    and dead-letter a good receipt).
    """

    def write(self, conn, prefix, entry_id, event):
        qualified_id = _qualified_entry_id(prefix, entry_id)
        read_id = _bus_read_id(prefix, entry_id)
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO hint_read (read_id, door, outcome, run_id, seat_id, "
                "query_hmac, query_text, query_truncated, k, hit_count, cid, "
                "stream_entry_id, served_at) "
                "VALUES (%s, 'bus', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (read_id) DO NOTHING RETURNING read_id",
                (
                    read_id,
                    event["outcome"],
                    event.get("run_id"),
                    event.get("seat_id"),
                    event.get("query_hmac"),
                    event.get("query_text"),
                    event["query_truncated"],
                    event["k"],
                    event.get("hit_count", 0),
                    event.get("cid"),
                    qualified_id,
                    event["served_at"],
                ),
            ).fetchone()
            if row is None:
                # Redelivery of an already-committed entry — parent and hits exist.
                return "duplicate"
            for rank, hit in enumerate(event.get("hits", ()), start=1):
                inner = hit["hint"]  # H-02 nesting on the bus tier
                conn.execute(
                    "INSERT INTO hint_read_hit (read_id, rank, hint_id, withheld, "
                    "vector_distance, lexical_rank) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        read_id,
                        rank,
                        inner["id"],
                        hit["withheld"],
                        inner.get("vector_distance"),
                        inner.get("lexical_rank"),
                    ),
                )
        return "recorded"


def deadletter_malformed_hint_read(conn, prefix, entry_id, fields, error):
    """Mirrors ``deadletter_malformed_eval_event``, with H-04 qualified stream_entry_id."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO hint_read_deadletter (stream_entry_id, raw_entry, error) "
            "VALUES (%s, %s, %s) ON CONFLICT (stream_entry_id) DO NOTHING",
            (
                _qualified_entry_id(prefix, entry_id),
                Jsonb(sanitize_json(dict(fields, stream_entry_id=entry_id))),
                str(error),
            ),
        )


class HintReadConsumer(StreamConsumerLoop):
    def __init__(
        self,
        redis_client,
        conn_factory,
        *,
        prefix=None,
        consumer="hint-reads",
        block_ms=1000,
        sinks=None,
    ):
        from .bus import PREFIX as DEFAULT_PREFIX

        self.redis = redis_client
        self.conn_factory = conn_factory
        self.prefix = DEFAULT_PREFIX if prefix is None else prefix
        self.consumer = consumer
        self.block_ms = block_ms
        self.sinks = sinks or [HintReadSink()]
        self.stream = hint_reads_stream(self.prefix)
        self.group = HINT_READS_GROUP
        self.deadletter_table = "hint_read_deadletter"
        self._init_consumer_loop(ledger_prefix=self.prefix)
        self.thread = None
        ensure_group(self.redis, self.stream, HINT_READS_GROUP)

    def start(self):
        if self.thread is not None:
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def _ack(self, entry_id):
        try:
            self.redis.xack(self.stream, HINT_READS_GROUP, entry_id)
        except redis.RedisError:
            self._infra_this_iteration = True
            logger.exception("hint-read consumer failed to ack %s", entry_id)
            raise

    def _handle_entry(self, entry_id, fields):
        try:
            event = _parse_hint_read_event(fields)
        except (KeyError, TypeError, ValueError) as exc:
            conn = None
            try:
                conn = self.conn_factory()
                deadletter_malformed_hint_read(conn, self.prefix, entry_id, fields, exc)
            except Exception as deadletter_error:
                logger.exception("hint-read consumer failed to dead-letter %s", entry_id)
                return self._deadletter_failed(entry_id, fields, deadletter_error, None)
            finally:
                if conn is not None and not getattr(conn, "closed", True):
                    conn.close()
            self._ack(entry_id)
            return "dead-lettered"

        conn = None
        try:
            conn = self.conn_factory()
            result = None
            for sink in self.sinks:
                result = sink.write(conn, self.prefix, entry_id, event)
        except (psycopg.Error, redis.RedisError) as exc:
            logger.exception("hint-read consumer failed to handle %s", entry_id)
            return self._retry_or_exhaust(
                entry_id,
                fields,
                exc,
                lambda: self._with_fresh_conn(
                    lambda c: deadletter_malformed_hint_read(
                        c, self.prefix, entry_id, fields, exc
                    )
                ),
            )
        except Exception as exc:
            logger.exception("hint-read handler failed for %s; dead-lettering", entry_id)
            dl_conn = None
            try:
                dl_conn = self.conn_factory()
                deadletter_malformed_hint_read(
                    dl_conn, self.prefix, entry_id, fields, exc
                )
            except Exception as deadletter_error:
                logger.exception(
                    "hint-read consumer failed to dead-letter handler error %s", entry_id
                )
                return self._deadletter_failed(entry_id, fields, deadletter_error, None)
            finally:
                if dl_conn is not None and not getattr(dl_conn, "closed", True):
                    dl_conn.close()
            self._ack(entry_id)
            return "dead-lettered"
        finally:
            if conn is not None and not getattr(conn, "closed", True):
                conn.close()
        self._poison.pop(entry_id, None)
        self._ack(entry_id)
        return result


def purge_expired(conn, older_than_days: int, *, batch_size: int = 10000) -> int:
    """Delete `hint_read` rows older than the retention window.

    Mirrors ``eval.purge_expired``: batches of ``batch_size`` rows, each batch
    in its own transaction. Deletes from ``hint_read`` only — ``hint_read_hit``
    rows cascade via FK; ``hint_read_deadletter`` is never touched.
    """
    total = 0
    while True:
        with conn.transaction():
            cur = conn.execute(
                """
                DELETE FROM hint_read
                WHERE ctid IN (
                    SELECT ctid
                    FROM hint_read
                    WHERE served_at < now() - (%s * interval '1 day')
                    LIMIT %s
                )
                """,
                (older_than_days, batch_size),
            )
        deleted = cur.rowcount or 0
        total += deleted
        if deleted < batch_size:
            return total
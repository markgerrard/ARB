"""ARB Memory audit helpers."""

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone

import psycopg
import redis
from psycopg.types.json import Jsonb

from .bus import ensure_group
from .consumer_loop import StreamConsumerLoop, sanitize_json


# Env-configurable to match bus.PREFIX — so the audit consumer can be prefix-isolated (e.g. an e2e
# on its own stream, never the live `arbmem-audit` group). Was hardcoded "" (only ever exercised at
# the default prefix in unit tests); the artefact+audit e2e caught the mismatch with a prefixed AuditRun.
PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
AUDIT_MAX_PAYLOAD_BYTES = 16_384
SEQ_TTL_SECONDS = 365 * 24 * 60 * 60
MAXLEN = 1_000_000
GROUP = "arbmem-audit"
logger = logging.getLogger(__name__)


def resolve_audit_env(env=None):
    """Where audit events go: ``(url, prefix)``, ARB_AUDIT_* winning over ARB_MEMORY_*.

    The daemon has resolved audit this way since the credential split
    (``bridge.resolve_audit_redis``); the vote-emitting CLIs did not, and read
    ``ARB_MEMORY_*`` only. On a host that sets both to different buses — exactly what a
    mid-migration host does — the daemon's votes and the CLI's votes then landed on
    DIFFERENT buses. The run reconciles against a partial roster and fails at verdict
    close as ``refused_reconcile``, with nothing pointing at a split audit plane. Both
    callers must resolve identically; that is what this function is for.

    Returns the URL unvalidated (``None`` if neither var is set) so callers keep their own
    error text.
    """
    env = os.environ if env is None else env
    url = env.get("ARB_AUDIT_REDIS_URL") or env.get("ARB_MEMORY_REDIS_URL")
    prefix = env.get("ARB_AUDIT_PREFIX") or env.get("ARB_MEMORY_PREFIX") or ""
    return url, prefix


def _seq_key(run_id, *, prefix=PREFIX):
    return f"{prefix}arbmem:audit:run:{run_id}:seq"


def _canonical_payload(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def audit_stream(prefix=PREFIX):
    return f"{prefix}arbmem:audit"


def next_seq(redis, run_id, *, prefix=PREFIX):
    seq = redis.incr(_seq_key(run_id, prefix=prefix))
    redis.expire(_seq_key(run_id, prefix=prefix), SEQ_TTL_SECONDS)
    return int(seq)


def audit_content_hash(run_id, seq, source, kind, payload):
    header = f"{run_id}\0{seq}\0{source}\0{kind}\0".encode("utf-8")
    body = _canonical_payload(payload).encode("utf-8")
    return hashlib.sha256(header + body).hexdigest()


def audit_emit(redis, *, run_id, seq, source, kind, payload, ts=None, prefix=PREFIX):
    payload_json = _canonical_payload(payload)
    if len(payload_json.encode("utf-8")) > AUDIT_MAX_PAYLOAD_BYTES:
        raise ValueError("audit payload exceeds AUDIT_MAX_PAYLOAD_BYTES")

    ts = ts or datetime.now(timezone.utc).isoformat()
    content_hash = audit_content_hash(run_id, seq, source, kind, payload)
    return redis.xadd(
        audit_stream(prefix),
        {
            "run_id": run_id,
            "seq": str(seq),
            "source": source,
            "kind": kind,
            "payload": payload_json,
            "content_hash": content_hash,
            "ts": ts,
        },
        maxlen=MAXLEN,
        approximate=True,
    )


class AuditRun:
    def __init__(self, redis, run_id, *, prefix=PREFIX):
        self.redis = redis
        self.run_id = run_id
        self.prefix = prefix

    def emit(self, source, kind, payload):
        seq = next_seq(self.redis, self.run_id, prefix=self.prefix)
        return audit_emit(
            self.redis,
            run_id=self.run_id,
            seq=seq,
            source=source,
            kind=kind,
            payload=payload,
            prefix=self.prefix,
        )


class AuditSink:
    required = ["postgres"]

    def write(self, conn, event):
        raise NotImplementedError


class PostgresAuditSink(AuditSink):
    def write(self, conn, event):
        content_hash = event.get("content_hash") or audit_content_hash(
            event["run_id"],
            int(event["seq"]),
            event["source"],
            event["kind"],
            event["payload"],
        )
        event = dict(event, content_hash=content_hash)
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO audit_events
                    (run_id, seq, source, kind, ts, payload, stream_entry_id, content_hash, raw_entry)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, seq) DO NOTHING
                RETURNING id
                """,
                (
                    event["run_id"],
                    int(event["seq"]),
                    event["source"],
                    event["kind"],
                    event.get("ts"),
                    Jsonb(sanitize_json(event["payload"])),
                    event.get("stream_entry_id"),
                    content_hash,
                    Jsonb(sanitize_json(event)),
                ),
            ).fetchone()
            if row is not None:
                return "written"

            existing = conn.execute(
                "SELECT content_hash FROM audit_events WHERE run_id = %s AND seq = %s",
                (event["run_id"], int(event["seq"])),
            ).fetchone()
            if existing is not None and existing[0] == content_hash:
                return "duplicate"

            conflicting_hash = existing[0] if existing is not None else None
            conn.execute(
                """
                INSERT INTO audit_deadletter
                    (run_id, seq, source, kind, payload, content_hash, conflicting_hash, stream_entry_id)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stream_entry_id) DO NOTHING
                """,
                (
                    event["run_id"],
                    int(event["seq"]),
                    event["source"],
                    event["kind"],
                    Jsonb(sanitize_json(event["payload"])),
                    content_hash,
                    conflicting_hash,
                    event.get("stream_entry_id"),
                ),
            )
            logger.error(
                "audit event collision dead-lettered run_id=%s seq=%s content_hash=%s conflicting_hash=%s",
                event["run_id"],
                event["seq"],
                content_hash,
                conflicting_hash,
            )
            return "dead-lettered"


def _coerce_seq(fields):
    try:
        return int(fields["seq"])
    except (KeyError, TypeError, ValueError):
        return None


def deadletter_malformed_audit_event(conn, entry_id, fields, error):
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO audit_deadletter
                (run_id, seq, source, kind, payload, content_hash, raw_entry, error, stream_entry_id)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stream_entry_id) DO NOTHING
            """,
            (
                fields.get("run_id"),
                _coerce_seq(fields),
                fields.get("source"),
                fields.get("kind"),
                None,
                fields.get("content_hash"),
                Jsonb(sanitize_json(dict(fields, stream_entry_id=entry_id))),
                str(error),
                entry_id,
            ),
        )


def deadletter_duplicate_verdict(conn, entry_id, event):
    reason = "duplicate verdict for run_id"
    with conn.transaction():
        existing = conn.execute(
            "SELECT content_hash FROM audit_events WHERE run_id = %s AND kind = 'verdict' LIMIT 1",
            (event["run_id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO audit_deadletter
                (run_id, seq, source, kind, payload, content_hash, conflicting_hash, raw_entry, error, stream_entry_id)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stream_entry_id) DO NOTHING
            """,
            (
                event["run_id"],
                int(event["seq"]),
                event["source"],
                event["kind"],
                Jsonb(sanitize_json(event["payload"])),
                event["content_hash"],
                existing[0] if existing is not None else None,
                Jsonb(sanitize_json(event)),
                reason,
                entry_id,
            ),
        )
    logger.error(
        "audit duplicate verdict dead-lettered run_id=%s seq=%s content_hash=%s",
        event["run_id"],
        event["seq"],
        event["content_hash"],
    )


def _is_duplicate_verdict_violation(error):
    return (
        isinstance(error, psycopg.errors.UniqueViolation)
        and getattr(getattr(error, "diag", None), "constraint_name", None)
        == "audit_events_one_verdict"
    )


def handle_audit_event(conn, event, *, sinks):
    result = None
    for sink in sinks:
        result = sink.write(conn, event)
    return result


def _xinfo_value(group, key, default=0):
    value = group.get(key, default)
    return default if value is None else value


def audit_lag(redis, *, prefix=PREFIX):
    stream = audit_stream(prefix)
    ensure_group(redis, stream, GROUP)
    groups = redis.xinfo_groups(stream)
    group_info = next((group for group in groups if group.get("name") == GROUP), {})
    return {
        "stream": stream,
        "stream_length": redis.xlen(stream),
        "group": GROUP,
        "pending": int(_xinfo_value(group_info, "pending")),
        "lag": int(_xinfo_value(group_info, "lag")),
    }


def check_audit_health(redis, *, prefix=PREFIX, threshold=1000):
    lag = audit_lag(redis, prefix=prefix)
    return {
        "alarm": max(lag["pending"], lag["lag"]) >= threshold,
        "lag": lag,
    }


class AuditConsumer(StreamConsumerLoop):
    def __init__(self, redis, conn_factory, *, prefix=PREFIX, consumer="audit", block_ms=1000, sinks=None):
        self.redis = redis
        self.conn_factory = conn_factory
        self.prefix = prefix
        self.consumer = consumer
        self.block_ms = block_ms
        self.sinks = sinks or [PostgresAuditSink()]
        self.stream = audit_stream(prefix)
        self.group = GROUP
        self.deadletter_table = "audit_deadletter"
        self._init_consumer_loop(ledger_prefix=self.prefix)
        self.thread = None
        ensure_group(self.redis, self.stream, GROUP)

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

    @staticmethod
    def _parse_event(entry_id, fields):
        try:
            payload = json.loads(fields["payload"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("audit event has invalid payload") from exc

        event = {
            "run_id": fields["run_id"],
            "seq": int(fields["seq"]),
            "source": fields["source"],
            "kind": fields["kind"],
            "payload": payload,
            "content_hash": fields["content_hash"],
            "stream_entry_id": entry_id,
            "ts": fields["ts"],
        }
        expected_hash = audit_content_hash(
            event["run_id"],
            event["seq"],
            event["source"],
            event["kind"],
            event["payload"],
        )
        if event["content_hash"] != expected_hash:
            raise ValueError("audit event content_hash mismatch")
        return event

    def _ack(self, entry_id):
        try:
            self.redis.xack(self.stream, GROUP, entry_id)
        except redis.RedisError:
            self._infra_this_iteration = True
            logger.exception("audit consumer failed to ack entry %s", entry_id)
            raise

    def _handle_entry(self, entry_id, fields):
        try:
            event = self._parse_event(entry_id, fields)
        except (KeyError, TypeError, ValueError) as exc:
            conn = None
            try:
                conn = self.conn_factory()
                deadletter_malformed_audit_event(conn, entry_id, fields, exc)
            except Exception as deadletter_error:
                logger.exception("audit consumer failed to dead-letter malformed entry %s", entry_id)
                return self._deadletter_failed(entry_id, fields, deadletter_error, None)
            finally:
                if conn is not None and not getattr(conn, "closed", True):
                    conn.close()
            logger.exception("malformed audit entry dead-lettered %s", entry_id)
            self._ack(entry_id)
            return "dead-lettered"

        conn = None
        try:
            conn = self.conn_factory()
            result = handle_audit_event(conn, event, sinks=self.sinks)
        except psycopg.errors.UniqueViolation as exc:
            if not _is_duplicate_verdict_violation(exc):
                logger.exception("audit consumer hit retryable unique violation for entry %s", entry_id)
                return self._retry_or_exhaust(
                    entry_id,
                    fields,
                    exc,
                    lambda: self._with_fresh_conn(
                        lambda conn: deadletter_malformed_audit_event(conn, entry_id, fields, exc)
                    ),
                )
            # The insert transaction is aborted by the partial-index violation. Use a fresh
            # connection for the recoverable deadletter, then ack so this deterministic event
            # cannot poison the consumer group forever.
            logger.warning("duplicate audit verdict for entry %s; dead-lettering", entry_id)
            dl_conn = None
            try:
                dl_conn = self.conn_factory()
                deadletter_duplicate_verdict(dl_conn, entry_id, event)
            except Exception as deadletter_error:
                logger.exception("audit consumer failed to dead-letter duplicate verdict %s", entry_id)
                return self._retry_or_exhaust(
                    entry_id,
                    fields,
                    exc,
                    lambda: (_ for _ in ()).throw(deadletter_error),
                )
            finally:
                if dl_conn is not None and not getattr(dl_conn, "closed", True):
                    dl_conn.close()
            self._ack(entry_id)
            return "duplicate_verdict"
        except (psycopg.Error, redis.RedisError) as exc:
            logger.exception("audit consumer failed to handle entry %s", entry_id)
            return self._retry_or_exhaust(
                entry_id,
                fields,
                exc,
                lambda: self._with_fresh_conn(
                    lambda conn: deadletter_malformed_audit_event(conn, entry_id, fields, exc)
                ),
            )
        except Exception as exc:
            # Deterministic, non-infra handler failure (e.g. a future sink's
            # serialize/validation bug). Evidence has no silent-drop path: preserve
            # it in the deadletter (recoverable) before acking — never ack-and-drop.
            # Use a fresh connection: the handler's conn may be poisoned by the error.
            logger.exception("audit handler failed for entry %s; dead-lettering", entry_id)
            dl_conn = None
            try:
                dl_conn = self.conn_factory()
                deadletter_malformed_audit_event(dl_conn, entry_id, fields, exc)
            except Exception as deadletter_error:
                logger.exception("audit consumer failed to dead-letter handler error %s", entry_id)
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

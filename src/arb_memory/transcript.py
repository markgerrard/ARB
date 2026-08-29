"""Durable transcript trace consumer.

Mirrors eval.py: Redis stream entry id is the idempotency key; malformed
deterministic entries are dead-lettered and acked, infra failures are retried.
"""
from __future__ import annotations

import logging
import re
import threading

import psycopg
import redis
from psycopg.types.json import Jsonb

from .bus import ensure_group
from .consumer_loop import StreamConsumerLoop, sanitize_json


TRANSCRIPT_GROUP = "arbmem-transcript"
TRANSCRIPT_STREAM_BASE = "arbmem:trace"
logger = logging.getLogger(__name__)


class PostgresTranscriptSink:
    def write(self, conn, event):
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO transcript_io
                    (run_id, task_id, seat_id, orchestrator, turn_index, item_id,
                     seq, kind, tool_name, content, meta, ts, stream_entry_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stream_entry_id) DO NOTHING
                RETURNING id
                """,
                (
                    event["run_id"],
                    event["task_id"],
                    event.get("seat_id"),
                    event.get("orchestrator"),
                    event["turn_index"],
                    event["item_id"],
                    event["seq"],
                    event["kind"],
                    event.get("tool_name"),
                    event["content"],
                    Jsonb(sanitize_json(event.get("meta", {}))),
                    event["ts"],
                    event["stream_entry_id"],
                ),
            ).fetchone()
        return "written" if row is not None else "duplicate"


def deadletter_malformed_transcript_event(conn, entry_id, fields, error):
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO transcript_deadletter
                (run_id, task_id, seat_id, kind, raw_entry, error, stream_entry_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stream_entry_id) DO NOTHING
            """,
            (
                fields.get("run_id") if isinstance(fields, dict) else None,
                fields.get("task_id") if isinstance(fields, dict) else None,
                fields.get("seat_id") if isinstance(fields, dict) else None,
                fields.get("kind") if isinstance(fields, dict) else None,
                Jsonb(sanitize_json(dict(fields, stream_entry_id=entry_id) if isinstance(fields, dict) else {"raw": fields})),
                str(error),
                entry_id,
            ),
        )


class TranscriptConsumer(StreamConsumerLoop):
    def __init__(self, redis, conn_factory, *, prefix=None, consumer="transcript", block_ms=1000, sinks=None):
        self.redis = redis
        self.conn_factory = conn_factory
        self.consumer = consumer
        self.block_ms = block_ms
        self.sinks = sinks or [PostgresTranscriptSink()]
        # Effective prefix stored so repair records share the source stream's
        # namespace; previously absent entirely, so they escaped to the
        # unprefixed ledger.
        self.prefix = "" if prefix is None else prefix
        self.stream = f"{prefix}{TRANSCRIPT_STREAM_BASE}" if prefix is not None else TRANSCRIPT_STREAM_BASE
        self.group = TRANSCRIPT_GROUP
        self.deadletter_table = "transcript_deadletter"
        self._init_consumer_loop(ledger_prefix=self.prefix)
        self.thread = None
        ensure_group(self.redis, self.stream, TRANSCRIPT_GROUP)

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
        if not isinstance(fields, dict):
            raise ValueError("transcript event fields not a dict")
        for required in ("task_id", "item_id", "seq", "kind", "ts"):
            if fields.get(required) in (None, ""):
                raise ValueError(f"transcript event missing {required}")
        try:
            turn_index = int(fields.get("turn_index") or 0)
            seq = int(fields["seq"])
        except (TypeError, ValueError) as exc:
            raise ValueError("transcript event invalid numeric field") from exc
        content = fields.get("content", "")
        if not isinstance(content, str):
            raise ValueError("transcript event invalid content")
        tool_name = fields.get("tool_name") or None
        if not content and not tool_name:
            raise ValueError("transcript event empty content and tool_name")
        meta = {}
        if fields["kind"].startswith("command"):
            meta = _parse_apply_patch(content)
        return {
            "run_id": fields.get("run_id") or "",
            "task_id": fields["task_id"],
            "seat_id": fields.get("seat_id"),
            "orchestrator": fields.get("orchestrator"),
            "turn_index": turn_index,
            "item_id": fields["item_id"],
            "seq": seq,
            "kind": fields["kind"],
            "tool_name": tool_name,
            "content": content,
            "meta": meta,
            "ts": fields["ts"],
            "stream_entry_id": entry_id,
        }

    def _ack(self, entry_id):
        try:
            self.redis.xack(self.stream, TRANSCRIPT_GROUP, entry_id)
        except redis.RedisError:
            self._infra_this_iteration = True
            logger.exception("transcript consumer failed to ack %s", entry_id)
            raise

    def _handle_entry(self, entry_id, fields):
        try:
            event = self._parse_event(entry_id, fields)
        except (KeyError, TypeError, ValueError) as exc:
            conn = None
            try:
                conn = self.conn_factory()
                deadletter_malformed_transcript_event(conn, entry_id, fields, exc)
            except Exception as deadletter_error:
                logger.exception("transcript consumer failed to dead-letter %s", entry_id)
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
                result = sink.write(conn, event)
        except (psycopg.Error, redis.RedisError) as exc:
            logger.exception("transcript consumer failed to handle %s", entry_id)
            return self._retry_or_exhaust(
                entry_id,
                fields,
                exc,
                lambda: self._with_fresh_conn(
                    lambda conn: deadletter_malformed_transcript_event(conn, entry_id, fields, exc)
                ),
            )
        except Exception as exc:
            logger.exception("transcript handler failed for %s; dead-lettering", entry_id)
            dl_conn = None
            try:
                dl_conn = self.conn_factory()
                deadletter_malformed_transcript_event(dl_conn, entry_id, fields, exc)
            except Exception as deadletter_error:
                logger.exception("transcript consumer failed to dead-letter handler error %s", entry_id)
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


def _parse_apply_patch(command: str) -> dict[str, int | str]:
    if "apply_patch" not in command and "*** Begin Patch" not in command:
        return {}
    file_name = None
    added = 0
    removed = 0
    for line in command.splitlines():
        match = re.match(r"\*\*\* (?:Update|Add|Delete) File: (.+)", line)
        if match and file_name is None:
            file_name = match.group(1).strip()
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    if file_name is None:
        return {}
    return {"file": file_name, "added": added, "removed": removed}


def purge_expired(conn, older_than_days: int, *, batch_size: int = 10000) -> int:
    total = 0
    while True:
        with conn.transaction():
            cur = conn.execute(
                """
                DELETE FROM transcript_io
                WHERE ctid IN (
                    SELECT ctid
                    FROM transcript_io
                    WHERE inserted_at < now() - (%s * interval '1 day')
                    LIMIT %s
                )
                """,
                (older_than_days, batch_size),
            )
        deleted = cur.rowcount or 0
        total += deleted
        if deleted < batch_size:
            return total

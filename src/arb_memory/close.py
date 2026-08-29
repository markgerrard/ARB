"""Bus consumer for reconcile-gated audit verdict closes."""

from __future__ import annotations

import json
import logging
import os
import threading

import psycopg
import redis
from psycopg.types.json import Jsonb

from .bus import ensure_group
from .consumer_loop import StreamConsumerLoop, sanitize_json
from .run import CloseResult, close_core

PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
CLOSE_REQUEST_STREAM = "arbmem:audit:close_request"
CLOSE_RESULT_PREFIX = "arbmem:audit:close_result:"
GROUP = "arbmem-audit-close"
RESULT_TTL_SECONDS = 600
logger = logging.getLogger(__name__)


def close_request_stream(prefix=PREFIX):
    return f"{prefix}{CLOSE_REQUEST_STREAM}"


def close_result_key(request_id, prefix=PREFIX):
    return f"{prefix}{CLOSE_RESULT_PREFIX}{request_id}"


def deadletter_malformed_close_request(conn, entry_id, fields, error):
    raw_entry = (
        {
            key.decode("utf-8") if isinstance(key, bytes) else key:
            value.decode("utf-8") if isinstance(value, bytes) else value
            for key, value in fields.items()
        }
        if isinstance(fields, dict)
        else {"raw": fields}
    )
    raw_entry["stream_entry_id"] = entry_id
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO audit_close_deadletter
                (request_id, run_id, raw_entry, error, stream_entry_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (stream_entry_id) DO NOTHING
            """,
            (
                CloseConsumer._text(fields.get("request_id")) if isinstance(fields, dict) else None,
                CloseConsumer._text(fields.get("run_id")) if isinstance(fields, dict) else None,
                Jsonb(sanitize_json(raw_entry)),
                str(error),
                entry_id,
            ),
        )


class CloseConsumer(StreamConsumerLoop):
    def __init__(self, redis, conn_factory, *, prefix=PREFIX, consumer="close", block_ms=1000):
        self.redis = redis
        self.conn_factory = conn_factory
        self.prefix = prefix
        self.consumer = consumer
        self.block_ms = block_ms
        self.stream = close_request_stream(prefix)
        self.group = GROUP
        self.deadletter_table = "audit_close_deadletter"
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
    def _text(value):
        return value.decode("utf-8") if isinstance(value, bytes) else value

    @classmethod
    def _parse_request(cls, fields):
        if not isinstance(fields, dict):
            raise ValueError("close request fields not a dict")
        fields = {key: cls._text(value) for key, value in fields.items()}
        for required in ("request_id", "run_id", "verdict"):
            if not fields.get(required):
                raise ValueError(f"close request missing {required}")
        try:
            verdict = json.loads(fields["verdict"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("close request verdict is not valid JSON") from exc
        if not isinstance(verdict, dict):
            raise ValueError("close request verdict must be a JSON object")
        return {
            "request_id": fields["request_id"],
            "run_id": fields["run_id"],
            "verdict": verdict,
            "requested_by": fields.get("requested_by") or "orchestrator",
        }

    def _write_result(self, request_id, result):
        key = close_result_key(request_id, prefix=self.prefix)
        self.redis.lpush(key, json.dumps(result, separators=(",", ":")))
        self.redis.expire(key, RESULT_TTL_SECONDS)

    def _ack(self, entry_id):
        try:
            self.redis.xack(self.stream, GROUP, entry_id)
        except redis.RedisError:
            self._infra_this_iteration = True
            raise

    def _handle_entry(self, entry_id, fields):
        try:
            request = self._parse_request(fields)
        except (KeyError, TypeError, ValueError) as exc:
            request_id = (
                self._text(fields.get("request_id"))
                if isinstance(fields, dict)
                else None
            ) or entry_id
            conn = None
            try:
                conn = self.conn_factory()
                deadletter_malformed_close_request(conn, entry_id, fields, exc)
                self._write_result(request_id, {"outcome": "malformed", "exit_code": 2, "gaps": []})
            except Exception as deadletter_error:
                logger.exception("audit close consumer failed to handle malformed entry %s", entry_id)
                return self._deadletter_failed(entry_id, fields, deadletter_error, None)
            finally:
                if conn is not None and not getattr(conn, "closed", True):
                    conn.close()
            self._ack(entry_id)
            return "malformed"

        conn = None
        try:
            conn = self.conn_factory()
            result = close_core(
                conn,
                self.redis,
                request["run_id"],
                request["verdict"],
                source=request["requested_by"],
            )
            self._write_result(request["request_id"], result.as_dict())
        except (psycopg.Error, redis.RedisError) as exc:
            logger.exception("audit close consumer failed to handle entry %s", entry_id)
            return self._retry_or_exhaust(
                entry_id,
                fields,
                exc,
                lambda: self._with_fresh_conn(
                    lambda conn: deadletter_malformed_close_request(conn, entry_id, fields, exc)
                ),
                on_terminal=lambda: self._write_result(
                    request["request_id"],
                    {"outcome": "infra_exhausted", "exit_code": 3, "gaps": []},
                ),
            )
        except Exception as exc:
            logger.exception("audit close handler failed for %s; dead-lettering", entry_id)
            dl_conn = None
            try:
                dl_conn = self.conn_factory()
                deadletter_malformed_close_request(dl_conn, entry_id, fields, exc)
            except Exception as deadletter_error:
                logger.exception("audit close consumer failed to dead-letter handler error %s", entry_id)
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


AuditCloseConsumer = CloseConsumer

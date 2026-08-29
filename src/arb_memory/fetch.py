"""Credential-neutral artefact fetch-by-id over the ARB Memory bus.

Transient infrastructure faults recirculate without a result so the client observes a
timeout (which the driver refuses as class ``timeout``). Poison faults exhaust to
``infra_exhausted`` and are acknowledged. ``not_found`` is reserved for a zero-row query.
This split mirrors the audit-close consumer.

``infra_exhausted`` is a statement about the STORE: it tried and gave up. It is emitted only
by the consumer. Client-side transport faults are never it — they are ``request_unsent`` (the
XADD failed, so nothing was ever asked) or ``result_unreadable`` (the request was published
but the reply could not be read). Keeping these apart is what lets a caller tell "the store is
in trouble" from "my connection is", which are different fixes.
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid

import psycopg
import redis

from .bus import MAXLEN, ensure_group
from .consumer_loop import StreamConsumerLoop

PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
FETCH_REQUEST_STREAM = "arbmem:artefact:fetch_request"
FETCH_RESULT_PREFIX = "arbmem:artefact:fetch_result:"
GROUP = "arbmem-artefact-fetch"
FETCH_TIMEOUT_SECONDS = float(os.environ.get("ARB_MEMORY_FETCH_TIMEOUT_SECONDS", "30"))
RESULT_TTL_SECONDS = int(os.environ.get("ARB_MEMORY_FETCH_RESULT_TTL_SECONDS", "120"))
logger = logging.getLogger(__name__)


def fetch_request_stream(prefix=PREFIX):
    return f"{prefix}{FETCH_REQUEST_STREAM}"


def fetch_result_key(request_id, prefix=PREFIX):
    return f"{prefix}{FETCH_RESULT_PREFIX}{request_id}"


def memory_fetch_by_id(
    redis_client,
    artefact_id,
    *,
    version=None,
    timeout=FETCH_TIMEOUT_SECONDS,
    prefix=PREFIX,
) -> dict | None:
    """Fetch an artefact version over the bus; normalize transport failures."""
    request_id = uuid.uuid4().hex
    fields = {"request_id": request_id, "artefact_id": artefact_id}
    if version is not None:
        fields["version"] = str(version)
    # The two legs fail for different reasons and must not share an outcome. Collapsing them
    # into infra_exhausted (which ALSO names the consumer's genuine give-up, below) made one
    # word mean three states, and on 2026-08-08 that sent a caller chasing artefact-store
    # capacity for what was a client-side transport fault.
    try:
        redis_client.xadd(
            fetch_request_stream(prefix),
            fields,
            maxlen=MAXLEN,
            approximate=True,
        )
    except redis.RedisError:
        # The request never left. Nothing is serving it; retrying is safe and is the fix.
        return {"outcome": "request_unsent", "request_id": request_id}
    try:
        popped = redis_client.blpop(
            fetch_result_key(request_id, prefix),
            timeout=max(1, math.ceil(timeout)),
        )
    except redis.RedisError:
        # The request WAS published and is likely being served — we just cannot hear the
        # answer. Says nothing about the store's health, unlike infra_exhausted.
        return {"outcome": "result_unreadable", "request_id": request_id}
    if popped is None:
        return None
    try:
        envelope = json.loads(popped[1])
    except (TypeError, json.JSONDecodeError):
        return {"outcome": "malformed", "request_id": request_id}
    if not isinstance(envelope, dict):
        return {"outcome": "malformed", "request_id": request_id}
    envelope["request_id"] = request_id
    return envelope


class FetchConsumer(StreamConsumerLoop):
    def __init__(self, redis_client, conn_factory, *, prefix=PREFIX, consumer="fetch", block_ms=1000):
        self.redis = redis_client
        self.conn_factory = conn_factory
        self.prefix = prefix
        self.consumer = consumer
        self.block_ms = block_ms
        self.stream = fetch_request_stream(prefix)
        self.group = GROUP
        self._init_consumer_loop(ledger_prefix=self.prefix)
        ensure_group(self.redis, self.stream, GROUP)

    @staticmethod
    def _text(value):
        return value.decode("utf-8") if isinstance(value, bytes) else value

    @classmethod
    def _parse_request(cls, fields):
        if not isinstance(fields, dict):
            raise ValueError("fetch request fields not a dict")
        fields = {cls._text(key): cls._text(value) for key, value in fields.items()}
        for required in ("request_id", "artefact_id"):
            if not fields.get(required):
                raise ValueError(f"fetch request missing {required}")
        version = fields.get("version")
        if version is not None:
            try:
                version = int(version)
            except (TypeError, ValueError) as exc:
                raise ValueError("fetch request version is not an integer") from exc
            if version < 1:
                raise ValueError("fetch request version must be positive")
        return {
            "request_id": fields["request_id"],
            "artefact_id": fields["artefact_id"],
            "version": version,
        }

    def _write_result(self, request_id, result):
        key = fetch_result_key(request_id, prefix=self.prefix)
        self.redis.lpush(key, json.dumps(result, default=str, separators=(",", ":")))
        self.redis.expire(key, RESULT_TTL_SECONDS)

    @staticmethod
    def _row_to_result(row):
        if row is None:
            return {"outcome": "not_found"}
        if row[3] is not None:
            return {"outcome": "binary_unsupported"}
        return {
            "outcome": "ok",
            "artefact_id": row[0],
            "version": row[1],
            "content": row[2],
            "content_mime": row[4],
            "content_hash": row[5],
            "author": row[6],
            "created_at": row[7],
        }

    @staticmethod
    def _fetch(conn, request):
        if request["version"] is None:
            row = conn.execute(
                """
                SELECT artefact_id, version, content, content_bytes, content_mime,
                       content_hash, author, created_at
                FROM artefacts
                WHERE artefact_id = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (request["artefact_id"],),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT artefact_id, version, content, content_bytes, content_mime,
                       content_hash, author, created_at
                FROM artefacts
                WHERE artefact_id = %s AND version = %s
                """,
                (request["artefact_id"], request["version"]),
            ).fetchone()
        return FetchConsumer._row_to_result(row)

    def _handle_entry(self, entry_id, fields):
        try:
            request = self._parse_request(fields)
        except (TypeError, ValueError) as exc:
            logger.warning("malformed artefact fetch request %s: %s", entry_id, exc)
            request_id = (
                self._text(fields.get("request_id"))
                if isinstance(fields, dict)
                else None
            ) or entry_id
            self._write_result(request_id, {"outcome": "malformed"})
            self._ack(entry_id)
            return "malformed"

        conn = None
        try:
            conn = self.conn_factory()
            result = self._fetch(conn, request)
            self._write_result(request["request_id"], result)
        except (psycopg.Error, redis.RedisError) as exc:
            logger.exception("artefact fetch consumer failed to handle entry %s", entry_id)
            return self._retry_or_exhaust(
                entry_id,
                fields,
                exc,
                lambda: True,
                on_terminal=lambda: self._write_result(
                    request["request_id"], {"outcome": "infra_exhausted"}
                ),
            )
        finally:
            if conn is not None and not getattr(conn, "closed", True):
                conn.close()

        self._poison.pop(entry_id, None)
        self._ack(entry_id)
        return result

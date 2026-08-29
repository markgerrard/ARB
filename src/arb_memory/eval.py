"""ARB Memory eval-trace consumer. Mirrors the audit single-writer/at-least-once pattern.

run_id is required (mistake-prevention): a teed event without a non-empty run_id is
dead-lettered, never silently inserted. Idempotency key = stream_entry_id (the XADD id the
consumer reads from xreadgroup; re-presented on PEL redelivery -> ON CONFLICT catches it).
"""
import json
import logging
import threading
from collections import Counter

import psycopg
import redis
from psycopg.types.json import Jsonb

from .bus import ensure_group
from .consumer_loop import StreamConsumerLoop, sanitize_json
from .eval_config import eval_prefix, EVAL_GROUP, eval_stream

EVAL_MAX_PAYLOAD_BYTES = 16_384
logger = logging.getLogger(__name__)
SPAN_COUNTERS = Counter()


class SpanMalformed(ValueError):
    """A deterministic span event error that is safe to record and acknowledge."""


def _payload(event):
    value = event.get("payload") or {}
    return value if isinstance(value, dict) else {}


def _epoch(event):
    value = _payload(event).get("attempt_epoch")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SpanMalformed("invalid attempt_epoch")


def _timestamp(event, *, closing=False):
    data = _payload(event)
    event_ts = data.get("event_ts")
    if event_ts:
        return event_ts, "event_ts"
    return event["sent_at"], "sent_at"


def deadletter_span(conn, event, error):
    conn.execute(
        """
        INSERT INTO span_deadletter
            (run_id, task_id, event_type, raw_entry, error, stream_entry_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (stream_entry_id) DO NOTHING
        """,
        (
            event.get("run_id"), event.get("task_id"), event.get("event_type"),
            Jsonb(sanitize_json(event)), str(error), event["stream_entry_id"],
        ),
    )


def _require(data, key, event_type):
    value = data.get(key)
    if value in (None, ""):
        raise SpanMalformed(f"{event_type} missing {key}")
    return value


def _ledger(conn, event, incoming):
    run_id, task_id = event["run_id"], event["task_id"]
    conn.execute(
        """
        INSERT INTO eval_task (run_id, task_id, seat_id, orchestrator, attempt_epoch)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (run_id, task_id) DO NOTHING
        """,
        (run_id, task_id, event.get("seat_id"), event.get("orchestrator"), incoming),
    )
    row = conn.execute(
        "SELECT attempt_epoch FROM eval_task WHERE run_id=%s AND task_id=%s FOR UPDATE",
        (run_id, task_id),
    ).fetchone()
    stored = int(row[0])
    if incoming < stored:
        return False
    if incoming > stored:
        conn.execute(
            "DELETE FROM eval_tool_call WHERE run_id=%s AND task_id=%s AND attempt_epoch < %s",
            (run_id, task_id, incoming),
        )
        conn.execute(
            "DELETE FROM eval_turn WHERE run_id=%s AND task_id=%s AND attempt_epoch < %s",
            (run_id, task_id, incoming),
        )
        conn.execute(
            """
            UPDATE eval_task
            SET attempt_epoch=%s, seat_id=%s, orchestrator=%s, started_at=NULL,
                finished_at=NULL, duration_ms=NULL, turn_count=0, tool_call_count=0,
                ok=NULL, outcome='open', updated_at=now()
            WHERE run_id=%s AND task_id=%s
            """,
            (incoming, event.get("seat_id"), event.get("orchestrator"), run_id, task_id),
        )
    return True


def _start_task(conn, event, epoch):
    started_at, _basis = _timestamp(event)
    conn.execute(
        """
        UPDATE eval_task
        SET started_at=COALESCE(started_at, %s), seat_id=COALESCE(seat_id, %s),
            orchestrator=COALESCE(orchestrator, %s), updated_at=now()
        WHERE run_id=%s AND task_id=%s AND attempt_epoch <= %s
        """,
        (started_at, event.get("seat_id"), event.get("orchestrator"),
         event["run_id"], event["task_id"], epoch),
    )


def _start_turn(conn, event, epoch):
    data = _payload(event)
    turn_index = _require(data, "turn_index", event["event_type"])
    started_at, basis = _timestamp(event)
    conn.execute(
        """
        INSERT INTO eval_turn
            (run_id, task_id, seat_id, orchestrator, attempt_epoch, turn_index,
             started_at, latency_basis)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (run_id, task_id, turn_index) DO UPDATE
        SET started_at=COALESCE(eval_turn.started_at, EXCLUDED.started_at),
            seat_id=COALESCE(eval_turn.seat_id, EXCLUDED.seat_id),
            orchestrator=COALESCE(eval_turn.orchestrator, EXCLUDED.orchestrator),
            latency_basis=COALESCE(eval_turn.latency_basis, EXCLUDED.latency_basis),
            updated_at=now()
        WHERE eval_turn.attempt_epoch <= EXCLUDED.attempt_epoch
        """,
        (event["run_id"], event["task_id"], event.get("seat_id"), event.get("orchestrator"),
         epoch, int(turn_index), started_at, basis),
    )


def _start_tool(conn, event, epoch):
    data = _payload(event)
    tool_call_id = _require(data, "tool_call_id", event["event_type"])
    turn_index = _require(data, "turn_index", event["event_type"])
    started_at, basis = _timestamp(event)
    conn.execute(
        """
        INSERT INTO eval_tool_call
            (run_id, task_id, seat_id, orchestrator, attempt_epoch, turn_index,
             tool_call_id, tool_name, started_at, latency_basis, started_stream_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (run_id, task_id, tool_call_id) DO UPDATE
        SET started_at=COALESCE(eval_tool_call.started_at, EXCLUDED.started_at),
            turn_index=COALESCE(eval_tool_call.turn_index, EXCLUDED.turn_index),
            tool_name=COALESCE(eval_tool_call.tool_name, EXCLUDED.tool_name),
            seat_id=COALESCE(eval_tool_call.seat_id, EXCLUDED.seat_id),
            orchestrator=COALESCE(eval_tool_call.orchestrator, EXCLUDED.orchestrator),
            latency_basis=COALESCE(eval_tool_call.latency_basis, EXCLUDED.latency_basis),
            started_stream_id=COALESCE(eval_tool_call.started_stream_id, EXCLUDED.started_stream_id),
            updated_at=now()
        WHERE eval_tool_call.attempt_epoch <= EXCLUDED.attempt_epoch
        """,
        (event["run_id"], event["task_id"], event.get("seat_id"), event.get("orchestrator"),
         epoch, int(turn_index), tool_call_id, data.get("tool_name"), started_at, basis,
         event["stream_entry_id"]),
    )


def _increment_tool_rollups(conn, event, turn_index, epoch):
    conn.execute(
        "UPDATE eval_turn SET tool_call_count=tool_call_count+1, updated_at=now() "
        "WHERE run_id=%s AND task_id=%s AND turn_index=%s AND attempt_epoch=%s",
        (event["run_id"], event["task_id"], turn_index, epoch),
    )
    conn.execute(
        "UPDATE eval_task SET tool_call_count=tool_call_count+1, updated_at=now() "
        "WHERE run_id=%s AND task_id=%s AND attempt_epoch=%s",
        (event["run_id"], event["task_id"], epoch),
    )


def _close_tool(conn, event, epoch):
    data = _payload(event)
    tool_call_id = _require(data, "tool_call_id", event["event_type"])
    current = conn.execute(
        "SELECT outcome, turn_index, started_at FROM eval_tool_call "
        "WHERE run_id=%s AND task_id=%s AND tool_call_id=%s AND attempt_epoch=%s FOR UPDATE",
        (event["run_id"], event["task_id"], tool_call_id, epoch),
    ).fetchone()
    if current is None:
        deadletter_span(conn, event, "orphan tool finish")
        return
    if current[0] != "open":
        return
    close_at, basis = _timestamp(event, closing=True)
    outcome = "finished"
    row = conn.execute(
        """
        UPDATE eval_tool_call
        SET finished_at=%s,
            latency_ms=CASE WHEN %s::timestamptz IS NULL OR started_at IS NULL
                                  OR %s::timestamptz < started_at THEN NULL
                       ELSE EXTRACT(EPOCH FROM (%s::timestamptz - started_at))*1000 END,
            latency_basis=%s, exit_code=%s, ok=%s,
            outcome=CASE WHEN %s::timestamptz < started_at THEN 'clock_invalid' ELSE %s END,
            finished_stream_id=%s, updated_at=now()
        WHERE run_id=%s AND task_id=%s AND tool_call_id=%s AND attempt_epoch=%s AND outcome='open'
        RETURNING turn_index
        """,
        (close_at, close_at, close_at, close_at, basis, data.get("exit_code"), data.get("ok"),
         close_at, outcome, event["stream_entry_id"],
         event["run_id"], event["task_id"], tool_call_id, epoch),
    ).fetchone()
    if row:
        _increment_tool_rollups(conn, event, row[0], epoch)


def _close_turn(conn, event, epoch, *, finality=False):
    data = _payload(event)
    turn_index = _require(data, "turn_index", event["event_type"])
    params = (event["run_id"], event["task_id"], int(turn_index), epoch)
    current = conn.execute(
        "SELECT outcome, started_at, finality_evidence FROM eval_turn "
        "WHERE run_id=%s AND task_id=%s AND turn_index=%s AND attempt_epoch=%s FOR UPDATE",
        params,
    ).fetchone()
    if current is None:
        if finality and conn.execute(
            "SELECT 1 FROM eval_turn WHERE run_id=%s AND task_id=%s AND turn_index=%s",
            params[:3],
        ).fetchone() is not None:
            return
        deadletter_span(conn, event, "orphan turn finish")
        return
    prior_outcome, started_at, evidence_before = current
    if (
        event["event_type"] == "turn_finalized"
        and prior_outcome == "clock_invalid"
        and evidence_before == "retracted"
    ):
        return
    if (
        event["event_type"] != "turn_finality_retracted"
        and prior_outcome != "open"
        and not (event["event_type"] == "turn_finalized" and evidence_before == "retracted")
    ):
        return
    close_at, basis = _timestamp(event, closing=True)
    if event["event_type"] == "turn_finalized":
        latency = (data.get("event_ts"), started_at)
        outcome, close_basis, evidence = "recovered", "turn_finalized", "fd_quiescence"
    elif event["event_type"] == "turn_finality_retracted":
        latency, outcome, close_basis, evidence = None, "clock_invalid", "none", "retracted"
    else:
        close_basis = "task_finish_derived" if event.get("_derived") else (
            "turn_timeout" if event["event_type"] == "turn_timeout" else "turn_completed"
        )
        if basis == "event_ts" and data.get("turn_clock_monotonic") is not True:
            latency, outcome = None, "clock_invalid"
        elif basis == "event_ts":
            latency, outcome = (data.get("event_ts"), started_at), "finished"
        else:
            latency, outcome = (close_at, started_at), "timeout" if event["event_type"] == "turn_timeout" else "finished"
        evidence = None
    where = "run_id=%s AND task_id=%s AND turn_index=%s AND attempt_epoch=%s"
    if event["event_type"] == "turn_finalized":
        where += " AND finality_evidence IS DISTINCT FROM 'retracted'"
    if event["event_type"] != "turn_finality_retracted":
        where += " AND outcome='open'"
    row = conn.execute(
        f"""
        UPDATE eval_turn
        SET completed_at=%s,
            latency_ms=CASE WHEN %s::timestamptz IS NULL OR %s::timestamptz IS NULL THEN NULL
                       ELSE EXTRACT(EPOCH FROM (%s::timestamptz - %s::timestamptz))*1000 END,
            latency_basis=%s, outcome=%s, close_basis=%s, finality_evidence=%s,
            ok=%s, updated_at=now()
        WHERE {where}
        RETURNING turn_index
        """,
        (close_at, latency[0] if latency else None, latency[1] if latency else None,
         latency[0] if latency else None, latency[1] if latency else None, basis,
         outcome, close_basis, evidence, data.get("ok"), *params),
    ).fetchone()
    if row and prior_outcome == "open":
        conn.execute(
            "UPDATE eval_task SET turn_count=turn_count+1, updated_at=now() "
            "WHERE run_id=%s AND task_id=%s AND attempt_epoch=%s",
            (event["run_id"], event["task_id"], epoch),
        )


def _finish_task(conn, event, epoch):
    data = _payload(event)
    finished_at, _basis = _timestamp(event, closing=True)
    current = conn.execute(
        "SELECT started_at FROM eval_task WHERE run_id=%s AND task_id=%s AND attempt_epoch=%s FOR UPDATE",
        (event["run_id"], event["task_id"], epoch),
    ).fetchone()
    conn.execute(
        """
        UPDATE eval_task
        SET finished_at=%s, duration_ms=CASE WHEN started_at IS NULL THEN NULL
                    ELSE EXTRACT(EPOCH FROM (%s::timestamptz - started_at))*1000 END,
            ok=%s, outcome=CASE WHEN %s THEN 'finished' ELSE 'incomplete' END, updated_at=now()
        WHERE run_id=%s AND task_id=%s AND attempt_epoch=%s
        """,
        (finished_at, finished_at, data.get("ok"), bool(data.get("ok")),
         event["run_id"], event["task_id"], epoch),
    )
    seat_id = str(event.get("seat_id") or "").lower()
    recovery_producer = (
        seat_id in {"pi_rpc", "pi-sdk", "agy-print", "agy", "agy-tmux"}
        or seat_id.startswith(("pi-", "pi_", "agy-", "agy_"))
    )
    if recovery_producer:
        rows = conn.execute(
            "SELECT turn_index FROM eval_turn WHERE run_id=%s AND task_id=%s "
            "AND attempt_epoch=%s AND outcome='open' FOR UPDATE",
            (event["run_id"], event["task_id"], epoch),
        ).fetchall()
        for (turn_index,) in rows:
            derived = dict(event, event_type="turn_completed", _derived=True,
                           payload={"turn_index": turn_index, "ok": data.get("ok")})
            _close_turn(conn, derived, epoch)
        tools = conn.execute(
            "SELECT tool_call_id FROM eval_tool_call WHERE run_id=%s AND task_id=%s "
            "AND attempt_epoch=%s AND outcome='open' FOR UPDATE",
            (event["run_id"], event["task_id"], epoch),
        ).fetchall()
        for (tool_call_id,) in tools:
            derived = dict(event, event_type="command_finished",
                           payload={"tool_call_id": tool_call_id, "ok": data.get("ok")})
            _close_tool(conn, derived, epoch)


def project_spans(conn, event):
    incoming = _epoch(event)
    if incoming is None:
        SPAN_COUNTERS["span_skipped_no_epoch"] += 1
        return "skipped-no-epoch"
    if not _ledger(conn, event, incoming):
        return "stale"
    event_type = event["event_type"]
    if event_type == "task_started":
        _start_task(conn, event, incoming)
    elif event_type == "task_finished":
        _finish_task(conn, event, incoming)
    elif event_type == "turn_started":
        _start_turn(conn, event, incoming)
    elif event_type in {"turn_completed", "turn_timeout", "turn_finalized", "turn_finality_retracted"}:
        _close_turn(conn, event, incoming, finality=event_type.startswith("turn_final"))
    elif event_type == "command_started":
        _start_tool(conn, event, incoming)
    elif event_type == "command_finished":
        _close_tool(conn, event, incoming)
    return "projected"


class PostgresEvalSink:
    def write(self, conn, event):
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO eval_event_raw
                    (run_id, task_id, seat_id, orchestrator, event_type, schema_version,
                     sent_at, payload, stream_entry_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stream_entry_id) DO NOTHING
                RETURNING id
                """,
                (
                    event["run_id"], event["task_id"], event.get("seat_id"),
                    event.get("orchestrator"), event["event_type"],
                    event.get("schema_version", "1"), event["sent_at"],
                    Jsonb(sanitize_json(event["payload"])), event["stream_entry_id"],
                ),
            ).fetchone()
            try:
                projection = project_spans(conn, event)
            except SpanMalformed as exc:
                deadletter_span(conn, event, exc)
                projection = "deadlettered"
        if row is None and projection == "projected":
            return "duplicate"
        return projection if row is not None else "duplicate"


def deadletter_malformed_eval_event(conn, entry_id, fields, error):
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO eval_deadletter
                (run_id, task_id, seat_id, event_type, schema_version, payload, stream_entry_id, raw_entry, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stream_entry_id) DO NOTHING
            """,
            (
                fields.get("run_id"), fields.get("task_id"), fields.get("seat_id"),
                fields.get("event_type"), fields.get("schema_version"), None, entry_id,
                Jsonb(sanitize_json(dict(fields, stream_entry_id=entry_id))), str(error),
            ),
        )


class EvalConsumer(StreamConsumerLoop):
    def __init__(self, redis, conn_factory, *, prefix=None, consumer="eval", block_ms=1000, sinks=None):
        self.redis = redis
        self.conn_factory = conn_factory
        self.consumer = consumer
        self.block_ms = block_ms
        self.sinks = sinks or [PostgresEvalSink()]
        # eval_stream() reads ARB_EVAL_PREFIX; the e2e/tests pass an explicit prefix instead.
        # Kept so repair records route to THIS tenant's ledger: EvalConsumer's
        # stream shape has no `arbmem:` marker, which is what broke inference.
        # EFFECTIVE prefix, not the raw argument: when None the stream comes
        # from ARB_EVAL_PREFIX, so storing None routed repair records to the
        # unprefixed ledger while the source stream was correctly namespaced.
        self.prefix = eval_prefix() if prefix is None else prefix
        self.stream = f"{prefix}eval:events" if prefix is not None else eval_stream()
        self.group = EVAL_GROUP
        self.deadletter_table = "eval_deadletter"
        self._init_consumer_loop(ledger_prefix=self.prefix)
        self.thread = None
        ensure_group(self.redis, self.stream, EVAL_GROUP)

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
        run_id = fields.get("run_id")
        if not run_id:
            raise ValueError("eval event missing run_id")
        for required in ("task_id", "event_type", "sent_at"):
            if not fields.get(required):
                raise ValueError(f"eval event missing {required}")
        raw_payload = fields.get("payload", "{}")
        if isinstance(raw_payload, str) and len(raw_payload.encode("utf-8")) > EVAL_MAX_PAYLOAD_BYTES:
            raise ValueError("eval payload exceeds EVAL_MAX_PAYLOAD_BYTES")
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        except json.JSONDecodeError as exc:
            raise ValueError("eval event invalid payload") from exc
        return {
            "run_id": run_id,
            "task_id": fields["task_id"],
            "seat_id": fields.get("seat_id"),
            "orchestrator": fields.get("orchestrator"),
            "event_type": fields["event_type"],
            "schema_version": fields.get("schema_version", "1"),
            "sent_at": fields["sent_at"],
            "payload": payload,
            "stream_entry_id": entry_id,
        }

    def _ack(self, entry_id):
        try:
            self.redis.xack(self.stream, EVAL_GROUP, entry_id)
        except redis.RedisError:
            self._infra_this_iteration = True
            logger.exception("eval consumer failed to ack %s", entry_id)
            raise

    def _handle_entry(self, entry_id, fields):
        try:
            event = self._parse_event(entry_id, fields)
        except (KeyError, TypeError, ValueError) as exc:
            conn = None
            try:
                conn = self.conn_factory()
                deadletter_malformed_eval_event(conn, entry_id, fields, exc)
            except Exception as deadletter_error:
                logger.exception("eval consumer failed to dead-letter %s", entry_id)
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
            # Infra error: do NOT ack - let PEL redeliver (transient).
            logger.exception("eval consumer failed to handle %s", entry_id)
            return self._retry_or_exhaust(
                entry_id,
                fields,
                exc,
                lambda: self._with_fresh_conn(
                    lambda conn: deadletter_malformed_eval_event(conn, entry_id, fields, exc)
                ),
            )
        except Exception as exc:
            # Deterministic, non-infra handler failure (a future sink's validate/serialize bug).
            # Evidence has no silent-drop path: preserve in the deadletter (recoverable) before
            # acking - never ack-and-drop, and never infinite-loop on the same poison entry.
            # Mirrors AuditConsumer (audit.py:269-289). Fresh conn - the handler's may be poisoned.
            logger.exception("eval handler failed for %s; dead-lettering", entry_id)
            dl_conn = None
            try:
                dl_conn = self.conn_factory()
                deadletter_malformed_eval_event(dl_conn, entry_id, fields, exc)
            except Exception as deadletter_error:
                logger.exception("eval consumer failed to dead-letter handler error %s", entry_id)
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


def _xinfo_value(group, key, default=0):
    value = group.get(key, default)
    return default if value is None else value


def eval_lag(redis, *, prefix=None):
    stream = f"{prefix}eval:events" if prefix is not None else eval_stream()
    ensure_group(redis, stream, EVAL_GROUP)
    groups = redis.xinfo_groups(stream)
    group_info = next((group for group in groups if group.get("name") == EVAL_GROUP), {})
    return {
        "stream": stream,
        "stream_length": redis.xlen(stream),
        "group": EVAL_GROUP,
        "pending": int(_xinfo_value(group_info, "pending")),
        "lag": int(_xinfo_value(group_info, "lag")),
    }


def check_eval_health(redis, *, prefix=None, threshold=1000):
    lag = eval_lag(redis, prefix=prefix)
    return {
        "alarm": max(lag["pending"], lag["lag"]) >= threshold,
        "lag": lag,
    }


def purge_expired(conn, older_than_days: int, *, batch_size: int = 10000) -> int:
    total = 0
    while True:
        with conn.transaction():
            cur = conn.execute(
                """
                DELETE FROM eval_event_raw
                WHERE ctid IN (
                    SELECT ctid
                    FROM eval_event_raw
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

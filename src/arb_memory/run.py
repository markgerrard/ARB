from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import signal
import sys


def _redis_client():
    from arb_memory import redis_conn

    return redis_conn.from_url(os.environ["ARB_MEMORY_REDIS_URL"])


def _memory_conn():
    import psycopg

    return psycopg.connect(os.environ["ARB_MEMORY_DSN"])


def _wait_forever():
    while True:
        signal.pause()


def run_memory() -> None:
    from arb_memory.bus import MemoryConsumer
    from arb_memory.embed import embed

    consumer = MemoryConsumer(_redis_client(), _memory_conn, embed=embed)
    consumer.start()
    _wait_forever()


def run_audit() -> None:
    from arb_memory.audit import AuditConsumer

    consumer = AuditConsumer(_redis_client(), _memory_conn)
    consumer.start()
    _wait_forever()


def _eval_redis_client():
    from arb_memory import redis_conn
    from arb_memory.eval_config import eval_redis_url, eval_redis_db

    return redis_conn.from_url(eval_redis_url(), db=eval_redis_db())


def _trace_redis_client():
    from arb_memory import redis_conn

    return redis_conn.from_url(os.environ["ARB_TRACE_REDIS_URL"])


def run_eval() -> None:
    from arb_memory.eval import EvalConsumer

    consumer = EvalConsumer(_eval_redis_client(), _memory_conn)
    consumer.start()
    _wait_forever()


def run_eval_purge() -> None:
    from arb_memory.eval import purge_expired

    days = int(os.environ.get("ARB_EVAL_RETENTION_DAYS", "30"))
    with _memory_conn() as conn:
        deleted = purge_expired(conn, older_than_days=days)
    print(f"eval purge deleted {deleted} rows older_than_days={days}")


def run_transcript() -> None:
    from arb_memory.transcript import TranscriptConsumer

    consumer = TranscriptConsumer(_trace_redis_client(), _memory_conn, prefix=os.environ.get("ARB_TRACE_PREFIX", ""))
    consumer.start()
    _wait_forever()


def run_transcript_purge() -> None:
    from arb_memory.transcript import purge_expired

    days = int(os.environ.get("ARB_TRANSCRIPT_RETENTION_DAYS", "30"))
    with _memory_conn() as conn:
        deleted = purge_expired(conn, older_than_days=days)
    print(f"transcript purge deleted {deleted} rows older_than_days={days}")


def run_hint_reads() -> None:
    from arb_memory.hint_reads import HintReadConsumer

    consumer = HintReadConsumer(_redis_client(), _memory_conn)
    consumer.start()
    _wait_forever()


def run_hint_read_purge() -> None:
    from arb_memory.hint_reads import purge_expired

    days = int(os.environ.get("ARB_HINT_READ_RETENTION_DAYS", "30"))
    with _memory_conn() as conn:
        deleted = purge_expired(conn, older_than_days=days)
    print(f"hint-read purge deleted {deleted} rows older_than_days={days}")


def setup_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_event_raw (
            id              bigserial PRIMARY KEY,
            run_id          text NOT NULL,
            task_id         text NOT NULL,
            seat_id         text,
            orchestrator    text,
            event_type      text NOT NULL,
            schema_version  text NOT NULL DEFAULT '1',
            sent_at         timestamptz NOT NULL,
            payload         jsonb NOT NULL,
            stream_entry_id text NOT NULL,
            inserted_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (stream_entry_id)
        )
        """
    )
    conn.execute("ALTER TABLE eval_event_raw ADD COLUMN IF NOT EXISTS schema_version text NOT NULL DEFAULT '1'")
    conn.execute("ALTER TABLE eval_event_raw ADD COLUMN IF NOT EXISTS orchestrator text")
    conn.execute("CREATE INDEX IF NOT EXISTS eval_event_raw_run_idx ON eval_event_raw (run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS eval_event_raw_task_idx ON eval_event_raw (task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS eval_event_raw_inserted_at_idx ON eval_event_raw (inserted_at)")
    previous_autocommit = conn.autocommit
    if not previous_autocommit:
        conn.commit()
    conn.autocommit = True
    try:
        conn.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS eval_event_raw_orchestrator_task_sent_idx "
            "ON eval_event_raw (orchestrator, task_id, sent_at DESC, id DESC)"
        )
    finally:
        conn.autocommit = previous_autocommit

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_deadletter (
            id              bigserial PRIMARY KEY,
            run_id          text,
            task_id         text,
            seat_id         text,
            event_type      text,
            schema_version  text,
            payload         jsonb,
            stream_entry_id text,
            raw_entry       jsonb,
            error           text,
            ts              timestamptz NOT NULL DEFAULT now(),
            UNIQUE (stream_entry_id)
        )
        """
    )
    conn.execute("ALTER TABLE eval_deadletter ADD COLUMN IF NOT EXISTS schema_version text")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcript_io (
            id              bigserial PRIMARY KEY,
            run_id          text NOT NULL,
            task_id         text NOT NULL,
            seat_id         text,
            orchestrator    text,
            turn_index      integer NOT NULL DEFAULT 0,
            item_id         text NOT NULL,
            seq             bigint NOT NULL DEFAULT 0,
            kind            text NOT NULL,
            tool_name       text,
            content         text NOT NULL,
            meta            jsonb NOT NULL DEFAULT '{}',
            ts              timestamptz NOT NULL,
            stream_entry_id text NOT NULL,
            inserted_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (stream_entry_id)
        )
        """
    )
    conn.execute("ALTER TABLE transcript_io ADD COLUMN IF NOT EXISTS turn_index integer NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE transcript_io ADD COLUMN IF NOT EXISTS tool_name text")
    conn.execute("ALTER TABLE transcript_io ADD COLUMN IF NOT EXISTS meta jsonb NOT NULL DEFAULT '{}'")
    conn.execute("CREATE INDEX IF NOT EXISTS transcript_io_task_ts_idx ON transcript_io (task_id, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS transcript_io_ts_idx ON transcript_io (ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS transcript_io_inserted_at_idx ON transcript_io (inserted_at)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcript_deadletter (
            id              bigserial PRIMARY KEY,
            run_id          text,
            task_id         text,
            seat_id         text,
            kind            text,
            raw_entry       jsonb,
            error           text,
            stream_entry_id text,
            ts              timestamptz NOT NULL DEFAULT now(),
            UNIQUE (stream_entry_id)
        )
        """
    )
    conn.execute("ALTER TABLE transcript_deadletter ADD COLUMN IF NOT EXISTS kind text")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_tool_call (
            id                 bigserial PRIMARY KEY,
            run_id             text NOT NULL,
            task_id            text NOT NULL,
            seat_id            text,
            orchestrator       text,
            attempt_epoch      integer NOT NULL,
            turn_index         integer NOT NULL,
            tool_call_id       text NOT NULL,
            tool_name          text,
            started_at         timestamptz,
            finished_at        timestamptz,
            latency_ms         bigint,
            latency_basis      text CHECK (latency_basis IN ('sent_at', 'event_ts')),
            exit_code          integer,
            ok                 boolean,
            outcome            text NOT NULL DEFAULT 'open'
                               CHECK (outcome IN ('open', 'finished', 'timeout', 'incomplete', 'clock_invalid')),
            started_stream_id  text,
            finished_stream_id text,
            inserted_at        timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now(),
            UNIQUE (run_id, task_id, tool_call_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_turn (
            id                 bigserial PRIMARY KEY,
            run_id             text NOT NULL,
            task_id            text NOT NULL,
            seat_id            text,
            orchestrator       text,
            attempt_epoch      integer NOT NULL,
            turn_index         integer NOT NULL,
            started_at         timestamptz,
            completed_at       timestamptz,
            latency_ms         bigint,
            latency_basis      text CHECK (latency_basis IN ('sent_at', 'event_ts')),
            tool_call_count    integer NOT NULL DEFAULT 0,
            ok                 boolean,
            outcome            text NOT NULL DEFAULT 'open'
                               CHECK (outcome IN ('open', 'finished', 'timeout', 'incomplete', 'clock_invalid', 'recovered')),
            close_basis        text NOT NULL DEFAULT 'none'
                               CHECK (close_basis IN ('turn_completed', 'task_finish_derived', 'turn_timeout', 'turn_finalized', 'none')),
            finality_evidence  text CHECK (finality_evidence IN ('fd_quiescence', 'retracted')),
            inserted_at        timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now(),
            UNIQUE (run_id, task_id, turn_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_task (
            id                 bigserial PRIMARY KEY,
            run_id             text NOT NULL,
            task_id            text NOT NULL,
            seat_id            text,
            orchestrator       text,
            attempt_epoch      integer NOT NULL,
            started_at         timestamptz,
            finished_at        timestamptz,
            duration_ms        bigint,
            turn_count         integer NOT NULL DEFAULT 0,
            tool_call_count    integer NOT NULL DEFAULT 0,
            ok                 boolean,
            outcome            text NOT NULL DEFAULT 'open'
                               CHECK (outcome IN ('open', 'finished', 'timeout', 'incomplete')),
            inserted_at        timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now(),
            UNIQUE (run_id, task_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS span_deadletter (
            id               bigserial PRIMARY KEY,
            run_id           text,
            task_id          text,
            event_type       text NOT NULL,
            raw_entry        jsonb NOT NULL,
            error            text NOT NULL,
            stream_entry_id  text NOT NULL UNIQUE,
            ts               timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def run_setup_schema() -> None:
    with _memory_conn() as conn:
        setup_schema(conn)
    print("setup-schema applied: eval/transcript tables and indexes")


def visibility_grant_warning(*, visibility_role: str | None, visibility_dsn_set: bool) -> str | None:
    """Return a warning if a visibility service is configured but its grant role is unset.

    Unset ARB_VISIBILITY_GATEWAY_ROLE skips apply_visibility_grants — which fails CLOSED
    (the read-role gets no SELECT), so it is not a security hole, but the skip is silent:
    a future schema/role change would strand the visibility reader with no signal. Make it
    loud when ARB_VISIBILITY_DSN shows a visibility service is actually in play (audit OPS-V1).
    """
    if visibility_dsn_set and not visibility_role:
        return (
            "WARNING: ARB_VISIBILITY_DSN is set but ARB_VISIBILITY_GATEWAY_ROLE is not — "
            "apply_visibility_grants was SKIPPED. The visibility reader keeps only whatever "
            "grants it already has; set ARB_VISIBILITY_GATEWAY_ROLE to re-apply on schema change."
        )
    return None


def run_grants() -> None:
    import psycopg

    from arb_memory.mcp.config import mcp_role_name
    from arb_memory.mcp.grants import (
        apply_eval_grants,
        apply_gate_lane_writer_grants,
        apply_gate_reader_grants,
        apply_hint_read_consumer_grants,
        apply_hint_read_local_writer_grants,
        apply_retention_grants,
        apply_local_reader_grants,
        apply_mcp_grants,
        apply_transcript_grants,
        apply_visibility_grants,
    )

    dsn = os.environ["ARB_MEMORY_DSN"]
    with psycopg.connect(dsn) as conn:
        consumer_role = os.environ.get("ARB_EVAL_CONSUMER_ROLE") or conn.info.user
        transcript_role = os.environ.get("ARB_TRANSCRIPT_CONSUMER_ROLE") or consumer_role
        visibility_role = os.environ.get("ARB_VISIBILITY_GATEWAY_ROLE")
        local_reader_role = os.environ.get("ARB_MEMORY_LOCAL_READER_ROLE")
        vault_export_role = os.environ.get("ARB_VAULT_EXPORT_ROLE")
        retention_role = os.environ.get("ARB_RETENTION_ROLE")
        gate_reader_role = os.environ.get("ARB_GATE_READER_ROLE")
        lane_writer_role = os.environ.get("ARB_GATE_LANE_WRITER_ROLE")
        lane_writer_consumer = os.environ.get("ARB_GATE_LANE_WRITER_CONSUMER_ID")
        lane_writer_lane = os.environ.get("ARB_GATE_LANE_WRITER_LANE")
        # Per-seat writer triple: all three required together; partial set aborts.
        lane_writer_fields = (
            lane_writer_role,
            lane_writer_consumer,
            lane_writer_lane,
        )
        present = [v for v in lane_writer_fields if v]
        if present and len(present) != 3:
            raise SystemExit(
                "ARB_GATE_LANE_WRITER_ROLE, ARB_GATE_LANE_WRITER_CONSUMER_ID, and "
                "ARB_GATE_LANE_WRITER_LANE must be set together (or all unset); "
                "refusing partial lane-writer grant configuration"
            )
        mcp_role = mcp_role_name()
        apply_eval_grants(conn, consumer_role)
        apply_hint_read_consumer_grants(conn, consumer_role)
        if retention_role:
            apply_retention_grants(conn, retention_role)
        apply_transcript_grants(conn, transcript_role)
        apply_mcp_grants(conn, mcp_role)
        if visibility_role:
            apply_visibility_grants(conn, visibility_role)
        else:
            warning = visibility_grant_warning(
                visibility_role=visibility_role,
                visibility_dsn_set=bool(os.environ.get("ARB_VISIBILITY_DSN")),
            )
            if warning:
                print(warning, file=sys.stderr)
        if local_reader_role:
            apply_local_reader_grants(conn, local_reader_role)
            apply_hint_read_local_writer_grants(conn, local_reader_role)
        if vault_export_role:
            apply_local_reader_grants(conn, vault_export_role)   # no hint_read access
        if gate_reader_role:
            # Conditional: today's unrelated grant runs must not fail merely because
            # the cluster-global gate role has not yet been provisioned. When set,
            # isolation failure / missing role aborts before commit.
            apply_gate_reader_grants(conn, gate_reader_role)
        if lane_writer_role:
            # Creates no login or secret — only grants + binding for a
            # pre-provisioned per-seat role. Isolation failure aborts before commit.
            apply_gate_lane_writer_grants(
                conn,
                lane_writer_role,
                consumer_id=lane_writer_consumer,
                lane=lane_writer_lane,
            )
        conn.commit()
    print(
        f"grants applied: eval-consumer={consumer_role!r} "
        f"transcript-consumer={transcript_role!r} mcp-role={mcp_role!r} "
        f"visibility-gateway-role={visibility_role!r} "
        f"local-reader-role={local_reader_role!r} "
        f"vault-export-role={vault_export_role!r}"
        f" retention-role={retention_role!r}"
        f" gate-reader-role={gate_reader_role!r}"
        f" lane-writer-role={lane_writer_role!r}"
        f" lane-writer-consumer={lane_writer_consumer!r}"
        f" lane-writer-lane={lane_writer_lane!r}"
    )


def run_writer() -> None:
    import uvicorn

    from arb_memory import redis_conn
    from arb_memory.writer import build_writer_app

    # This is the client the deployed writer actually uses — build_writer_app's own hardened
    # fallback never fires here because we pass one in. It serves ONLY the await path, so it
    # idles between uses and its connections go stale; without the pool health check the BLPOP
    # gets a dead connection and raises (2026-08-08).
    async_redis_client = redis_conn.async_from_url(os.environ["ARB_MEMORY_REDIS_URL"])
    app = build_writer_app(
        _redis_client(),
        token=os.environ["ARB_MEMORY_WRITER_TOKEN"],
        async_redis_client=async_redis_client,
    )
    uvicorn.run(
        app,
        host=os.environ.get("ARB_MEMORY_WRITER_HOST", "0.0.0.0"),
        port=int(os.environ.get("ARB_MEMORY_WRITER_PORT", "8800")),
    )


def run_visibility() -> None:
    import uvicorn

    from arb_memory.visibility import build_visibility_app

    uvicorn.run(
        build_visibility_app(
            bus_redis_url=os.environ["ARB_BRIDGE_BUS_URL"],
            bus_prefix=os.environ.get("ARB_BRIDGE_BUS_PREFIX", ""),
            dsn=os.environ["ARB_MEMORY_DSN"],
            public_base_url=os.environ["ARB_MEMORY_MCP_PUBLIC_BASE_URL"],
        ),
        host=os.environ.get("ARB_VISIBILITY_HOST", "0.0.0.0"),
        port=int(os.environ.get("ARB_VISIBILITY_PORT", "8810")),
    )


def run_mcp() -> None:
    from arb_memory.mcp.config import load_settings, mcp_connect
    from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
    from arb_memory.mcp.server import build_server

    settings = load_settings()
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: mcp_connect(settings))
    server = build_server(settings=settings, provider=provider)
    # Bind all interfaces inside the container so cloudflared (the sole ingress) can reach the door
    # at mcp:8000 over the compose network. FastMCP defaults to 127.0.0.1 — loopback-only inside the
    # container, unreachable from another container. No host port is published (see docker-compose),
    # so 0.0.0.0 here means "all interfaces *within the container's network namespace*", not the host.
    server.settings.host = os.environ.get("ARB_MEMORY_MCP_HOST", "0.0.0.0")
    server.settings.port = int(os.environ.get("ARB_MEMORY_MCP_PORT", "8000"))
    # Streamable-HTTP transport (served at root "/", see build_server). This is the transport claude.ai's
    # current connector actually drives through OAuth (its probe is POST / -> 401 -> full OAuth flow).
    # SSE was tried but claude.ai's connector won't do SSE+OAuth discovery (GET /sse 401 -> "couldn't
    # connect", no OAuth) — SSE is deprecated/streamable-first there. The real connector blocker is the
    # Cloudflare edge 403'ing Claude/Anthropic user-agents on this hostname (UA-based, transport-agnostic),
    # fixed by a hostname-scoped CF Skip rule — NOT a server change. See [[edge-blocks-llm-dataplane]].
    server.run(transport="streamable-http")


def load_pointed_env_file() -> None:
    """Load KEY=VALUE lines from $ARB_MEMORY_LOCAL_ENV_FILE into os.environ.

    Spawning harnesses that would otherwise expose secrets on the command line
    (codex passes MCP env as a `-c` argv override, visible in ps) write them to a
    mode-600 file and pass only this pointer. Values split on the FIRST '=' only —
    DSNs carry '=' in query params. A set pointer with an unreadable file fails
    loud rather than starting a keyless server.
    """
    pointer = os.environ.get("ARB_MEMORY_LOCAL_ENV_FILE")
    if not pointer:
        return
    try:
        content = open(pointer, encoding="utf-8").read()
    except OSError as exc:
        raise RuntimeError(f"ARB_MEMORY_LOCAL_ENV_FILE points at unreadable file: {exc}") from exc
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key] = value


def run_local_read_mcp() -> None:
    from arb_memory.embed import embed
    from arb_memory.local_read_policy import local_read_dsn
    from arb_memory.mcp.local_server import build_local_server
    from arb_memory.mcp.read_tools import LocalReadSettings

    load_pointed_env_file()
    dsn = local_read_dsn(os.environ)
    server = build_local_server(LocalReadSettings(dsn=dsn), embed=embed)
    server.run(transport="stdio")


def _read_audit_close_payload(payload_file: str):
    try:
        if payload_file == "-":
            raw = sys.stdin.read()
        else:
            with open(payload_file, encoding="utf-8") as payload_stream:
                raw = payload_stream.read()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"audit-close: payload is not valid JSON: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        print("audit-close: payload must be a JSON object", file=sys.stderr)
        return None
    return payload


def _audit_stream_has_verdict(redis, run_id: str, *, prefix: str) -> bool:
    from arb_memory.audit import audit_stream

    for _entry_id, fields in redis.xrange(audit_stream(prefix), min="-", max="+"):
        entry_run_id = fields.get("run_id")
        entry_kind = fields.get("kind")
        if isinstance(entry_run_id, bytes):
            entry_run_id = entry_run_id.decode("utf-8")
        if isinstance(entry_kind, bytes):
            entry_kind = entry_kind.decode("utf-8")
        if entry_run_id == run_id and entry_kind == "verdict":
            return True
    return False


@dataclass(frozen=True)
class CloseResult:
    outcome: str
    exit_code: int
    gaps: list[str]

    def as_dict(self):
        return {
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "gaps": list(self.gaps),
        }


def _is_infrastructure_error(exc):
    import psycopg
    import redis

    return isinstance(exc, (psycopg.Error, redis.RedisError))


def close_core(conn, redis, run_id: str, payload: dict, *, source: str = "orchestrator") -> CloseResult:
    from arb_memory.audit import AuditRun, SEQ_TTL_SECONDS, _canonical_payload
    from arb_memory.panel_audit import reconcile

    result = reconcile(conn, run_id, payload, redis=redis)
    if not result["ok"]:
        return CloseResult("refused_reconcile", 4, list(result["gaps"]))

    prefix = os.environ.get("ARB_MEMORY_PREFIX", "")
    payload_hash = hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()
    claim_key = f"{prefix}arbmem:audit:run:{run_id}:verdict_close"
    claimed = redis.set(claim_key, payload_hash, ex=SEQ_TTL_SECONDS, nx=True)
    if claimed:
        try:
            AuditRun(redis, run_id, prefix=prefix).emit(source, "verdict", payload)
        except Exception as exc:
            redis.delete(claim_key)
            if _is_infrastructure_error(exc):
                raise
            return CloseResult("emit_failed", 1, [str(exc)])
        return CloseResult("emitted", 0, [])

    existing_hash = redis.get(claim_key)
    if isinstance(existing_hash, bytes):
        existing_hash = existing_hash.decode("utf-8")
    if existing_hash == payload_hash:
        if _audit_stream_has_verdict(redis, run_id, prefix=prefix):
            return CloseResult("emitted", 0, [])
        return CloseResult("orphaned", 6, [])

    return CloseResult("different_verdict", 5, [])


def _report_close_result(run_id: str, result: CloseResult) -> None:
    if result.outcome == "refused_reconcile":
        print("audit-close: verdict REFUSED — roster did not reconcile:", file=sys.stderr)
        for gap in result.gaps:
            print(f"  - {gap}", file=sys.stderr)
    elif result.outcome == "emitted" and result.exit_code == 0:
        print(f"emitted verdict run_id={run_id}")
    elif result.outcome == "emit_failed":
        detail = f": {result.gaps[0]}" if result.gaps else ""
        print(f"audit-close: verdict emit failed{detail}", file=sys.stderr)
    elif result.outcome == "different_verdict":
        print("audit-close: different verdict already closed for this run", file=sys.stderr)
    elif result.outcome == "orphaned":
        print(
            "audit-close: close-claim exists but no verdict emitted (prior crash) — "
            "DEL the claim key and re-run",
            file=sys.stderr,
        )


def run_audit_close(run_id: str, payload_file: str, *, source: str = "orchestrator") -> int:
    payload = _read_audit_close_payload(payload_file)
    if payload is None:
        return 2

    redis = _redis_client()
    conn = _memory_conn()
    try:
        result = close_core(conn, redis, run_id, payload, source=source)
    except Exception as exc:
        if not _is_infrastructure_error(exc):
            raise
        result = CloseResult("emit_failed", 1, [str(exc)])
    _report_close_result(run_id, result)
    return result.exit_code


def run_audit_close_consumer() -> None:
    from arb_memory.close import CloseConsumer

    consumer = CloseConsumer(_redis_client(), _memory_conn)
    consumer.start()
    _wait_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m arb_memory")
    services = parser.add_subparsers(dest="service", required=True)
    for service in (
        "memory",
        "audit",
        "audit-close-consumer",
        "eval",
        "eval-purge",
        "transcript",
        "transcript-purge",
        "hint-reads",
        "hint-read-purge",
        "setup-schema",
        "mcp",
        "local-read-mcp",
        "writer",
        "grants",
        "visibility",
    ):
        services.add_parser(service)
    audit_close = services.add_parser("audit-close")
    audit_close.add_argument("--run-id", required=True)
    audit_close.add_argument("--payload-file", required=True)
    audit_close.add_argument("--source", default="orchestrator")
    args = parser.parse_args(argv)

    if args.service == "audit-close":
        return run_audit_close(args.run_id, args.payload_file, source=args.source)

    handlers = {
        "memory": run_memory,
        "audit": run_audit,
        "audit-close-consumer": run_audit_close_consumer,
        "eval": run_eval,
        "eval-purge": run_eval_purge,
        "transcript": run_transcript,
        "transcript-purge": run_transcript_purge,
        "hint-reads": run_hint_reads,
        "hint-read-purge": run_hint_read_purge,
        "setup-schema": run_setup_schema,
        "mcp": run_mcp,
        "local-read-mcp": run_local_read_mcp,
        "writer": run_writer,
        "grants": run_grants,
        "visibility": run_visibility,
    }
    result = handlers[args.service]()
    return 0 if result is None else result


if __name__ == "__main__":
    raise SystemExit(main())

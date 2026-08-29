from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hmac
import inspect
import json
import logging
import os
from pathlib import Path
import re
import secrets

import anyio
import psycopg
import redis
import redis.asyncio as aioredis
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import PlainTextResponse
from starlette.responses import RedirectResponse
from starlette.responses import Response
from starlette.responses import StreamingResponse
from starlette.routing import Route

from arb_memory import redis_conn
from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import Settings
from arb_memory.mcp.config import canonical_resource
from arb_memory.mcp.login import _login_form
from arb_memory.mcp.login import _session_cookie
from arb_memory.mcp.login_factors import verify_two_factor
from arb_memory.transcript import _parse_apply_patch
from arb_memory.journey_export import NO_SNAPSHOT_ERROR


logger = logging.getLogger(__name__)

STALE_GRACE_S = 120
SSE_BACKFILL_COUNT = 200
# The orchestrator is a single long-lived task (the warm session, seat_id == orchestrator) whose
# transcript grows unbounded; short-lived seats are naturally small. Cap ONLY the orchestrator's
# PG backfill to a recent window (read at call time so it's overridable/testable). The live-tail is
# unaffected (separately maxlen-bounded), so new content still streams in full.
def _orchestrator_backfill_window() -> tuple[float, int]:
    # Tolerate a malformed/negative override rather than raising inside the SSE handler (which would
    # kill the stream); clamp to >= 0 (a negative LIMIT/interval errors in Postgres). (panel P2)
    try:
        hours = max(0.0, float(os.environ.get("ARB_VIS_BACKFILL_HOURS", "3")))
    except ValueError:
        hours = 3.0
    try:
        lines = max(0, int(os.environ.get("ARB_VIS_BACKFILL_LINES", "2000")))
    except ValueError:
        lines = 2000
    return hours, lines



SSE_BLOCK_MS = 1000
SSE_HEARTBEAT_S = 15
SSE_RE_VALIDATE_S = 60
ORCHESTRATOR_LAST_SEEN_SEED_COUNT = 5000
ORCHESTRATOR_STALE_SECS = 24 * 60 * 60
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
STATIC_DIR = Path(__file__).parent / "static"
STREAM_ID_RE = re.compile(r"^\d+-\d+$")
SEAT_RESUME_ID_RE = re.compile(r"^e=([^;]*);t=([^;]*)$")
VISIBILITY_LOGIN_CLIENT_ID = "visibility"
VISIBILITY_GLOBAL_KEY = "visibility:global"
VISIBILITY_SESSION_TTL_SECS = 8 * 3600


def _principal(conn, token, public_base_url):
    row = oauth_store.get_access_token(conn, token)
    if row is None:
        return None
    if canonical_resource(row["resource"]) != canonical_resource(public_base_url):
        return None
    return row


def mint_visibility_token(
    conn,
    *,
    public_base_url: str,
    client_id: str = "visibility",
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    token: str | None = None,
) -> str:
    token = token or f"vis-{secrets.token_urlsafe(32)}"
    oauth_store.put_access_token(
        conn,
        token=token,
        client_id=client_id,
        resource=public_base_url,
        scopes=scopes or ["memory.read"],
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(days=3650),
    )
    return token


def _visibility_login_settings(*, public_base_url: str, dsn: str) -> Settings | None:
    login_secret = os.environ.get("ARB_MEMORY_MCP_LOGIN_SECRET", "")
    totp_secret = os.environ.get("ARB_MEMORY_MCP_TOTP_SECRET", "")
    if not login_secret or not totp_secret:
        logger.warning(
            "visibility login gate disabled; ARB_MEMORY_MCP_LOGIN_SECRET and ARB_MEMORY_MCP_TOTP_SECRET are required"
        )
        return None
    return Settings(
        public_base_url=public_base_url,
        mcp_dsn=dsn,
        login_secret=login_secret,
        totp_secret=totp_secret,
    )


def _visibility_session_ttl_secs() -> int:
    try:
        return max(1, int(os.environ.get("ARB_VIS_SESSION_TTL_SECS", str(VISIBILITY_SESSION_TTL_SECS))))
    except ValueError:
        return VISIBILITY_SESSION_TTL_SECS


def _visibility_session_cookie(response, value: str, *, max_age: int) -> None:
    response.set_cookie(
        "arbmem_login",
        value,
        secure=True,
        httponly=True,
        samesite="lax",
        max_age=max_age,
    )


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else ""


def _parse_ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _aware_ts(value):
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def update_last_seen(current_value, new_sent_at):
    new_ts = _aware_ts(new_sent_at)
    if new_ts is None:
        return current_value
    current_ts = _aware_ts(current_value)
    if current_ts is None or new_ts > current_ts:
        return new_sent_at
    return current_value


def build_orchestrator_list(hash_dict, now, stale_secs) -> list[str]:
    now_ts = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    active = []
    for orchestrator, sent_at in (hash_dict or {}).items():
        seen_ts = _aware_ts(sent_at)
        if not orchestrator or seen_ts is None:
            continue
        if (now_ts - seen_ts).total_seconds() <= stale_secs:
            active.append((orchestrator, seen_ts))
    active.sort(key=lambda item: (item[1], item[0]), reverse=True)
    return [orchestrator for orchestrator, _seen_ts in active]


def tee_states(redis_client, bus_prefix, labels, now):
    """Reduce claude-tail heartbeat keys to fresh/stale/missing per label.

    Roster is CONFIGURED (ARB_VIS_EXPECTED_TEES), read via MGET of exactly
    those keys — SCAN can only enumerate keys that exist, so it can never say
    "missing" for a tee that never started (CT-1 spec §C, panel r2 codex P1);
    MGET also avoids the O(keyspace) walk (panel r2 agy).
    """
    keys = [f"{bus_prefix}tail:heartbeat:{label}" for label in labels]
    try:
        raws = redis_client.mget(keys)
    except Exception:
        logger.warning("tee heartbeat mget failed", exc_info=True)
        raws = [None] * len(keys)
    out = []
    for label, raw in zip(labels, raws):
        if not raw:
            out.append({"label": label, "state": "missing"})
            continue
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            out.append({"label": label, "state": "stale"})
            continue
        ts = _aware_ts(data.get("ts"))
        stale_after = data.get("stale_after_s")
        try:
            stale_after = int(stale_after)
        except (TypeError, ValueError):
            stale_after = 330
        state = "stale"
        if ts is not None and (now - ts).total_seconds() <= stale_after:
            state = "fresh"
        out.append({**data, "label": label, "state": state})
    return out


def _expected_tee_labels() -> list[str]:
    raw = os.environ.get("ARB_VIS_EXPECTED_TEES", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _orchestrator_stale_secs() -> int:
    try:
        return max(1, int(os.environ.get("ARB_VIS_ORCHESTRATOR_STALE_SECS", str(ORCHESTRATOR_STALE_SECS))))
    except ValueError:
        return ORCHESTRATOR_STALE_SECS


def _is_stale(state, *, now=None):
    if state.get("state") != "running":
        return False
    last_event_ts = _parse_ts(state.get("last_event_ts"))
    if last_event_ts is None:
        return False
    now = now or datetime.now(timezone.utc)
    if last_event_ts.tzinfo is None:
        last_event_ts = last_event_ts.replace(tzinfo=timezone.utc)
    return (now - last_event_ts).total_seconds() > STALE_GRACE_S


def _entry_data(entry):
    raw = entry.get("data", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def _reduce_seat(state: dict, entry: dict) -> dict:
    reduced = dict(state)
    if not entry:
        if _is_stale(reduced):
            reduced["state"] = "stale"
        return reduced

    event_type = entry.get("event_type")
    data = _entry_data(entry)
    reduced.update(
        {
            "run_id": entry.get("run_id") or reduced.get("run_id"),
            "task_id": entry.get("task_id") or reduced.get("task_id"),
            "seat_id": entry.get("seat_id") or reduced.get("seat_id"),
            "orchestrator": entry.get("orchestrator") or reduced.get("orchestrator"),
            "last_event_ts": entry.get("sent_at") or reduced.get("last_event_ts"),
            "last_event": event_type or reduced.get("last_event"),
        }
    )
    if entry.get("stalled_at"):
        reduced["stalled_at"] = entry.get("stalled_at")
    else:
        reduced.pop("stalled_at", None)
    if event_type in {"task_started", "task_continuing"}:
        reduced["state"] = "running"
    elif event_type == "task_finished":
        reduced["state"] = "failed" if data.get("ok") is False else "done"
    elif event_type == "vote":
        reduced["voted"] = True
        if "stance" in data:
            reduced["stance"] = data["stance"]
        if reduced.get("state") not in {"done", "failed"}:
            reduced["state"] = "voted"
    if _is_stale(reduced):
        reduced["state"] = "stale"
    return reduced


async def _apply_status_backfill(seat: dict, redis_client, bus_prefix: str) -> None:
    task_id = seat.get("task_id")
    if not task_id or not hasattr(redis_client, "hget"):
        seat.pop("stalled_at", None)
        return
    stalled_at = redis_client.hget(f"{bus_prefix}task:{task_id}:status", "stalled_at")
    if inspect.isawaitable(stalled_at):
        stalled_at = await stalled_at
    if stalled_at:
        seat["stalled_at"] = stalled_at
    else:
        seat.pop("stalled_at", None)


def _sse_frame(entry_id, event, data):
    payload = json.dumps(data, separators=(",", ":"))
    return f"id: {entry_id}\nevent: {event}\ndata: {payload}\n\n"


def _valid_stream_id(value):
    return value if isinstance(value, str) and STREAM_ID_RE.match(value) else None


def _seat_resume_id(events_cursor, trace_cursor):
    return f"e={_valid_stream_id(events_cursor) or '-'};t={_valid_stream_id(trace_cursor) or '-'}"


def _parse_seat_resume_id(last_event_id):
    match = SEAT_RESUME_ID_RE.match(last_event_id or "")
    if not match:
        return "$", "$"
    events_cursor, trace_cursor = match.groups()
    return _valid_stream_id(events_cursor) or "$", _valid_stream_id(trace_cursor) or "$"


def _seat_event_name(previous, current):
    if current.get("state") in {"done", "failed", "stale"}:
        return "seat_finish"
    if not previous:
        return "seat_appear"
    return "seat_update"


def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_history_limit(raw: str | None) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 50
    return max(1, min(200, value))


def _history_day_bounds(raw: str | None) -> tuple[datetime, datetime] | None:
    if not raw:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    since = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    anchor = since + timedelta(days=1) - timedelta(microseconds=1)
    return since, anchor


_COMMIT_FALLBACK_DONE = frozenset(
    {
        "agent_committed",
        "orchestrator_committed",
        "post_timeout_agent_committed",
        "post_timeout_committed",
    }
)
_COMMIT_FALLBACK_INCOMPLETE = frozenset({"steer_sent", "cancel_sent"})


def _history_seat_state(event_type: str, payload: dict) -> str:
    if event_type == "task_finished":
        return "failed" if payload.get("ok") is False else "done"
    if event_type in ("task_started", "task_continuing"):
        return "incomplete"
    if event_type in _COMMIT_FALLBACK_DONE:
        return "done"
    if event_type in _COMMIT_FALLBACK_INCOMPLETE:
        return "incomplete"
    return "unknown"


def _encode_history_cursor(anchor: datetime, last_ts: datetime, task_id: str, since: datetime | None = None) -> str:
    payload = json.dumps([_iso(anchor), _iso(last_ts), task_id, _iso(since)], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_history_cursor(cursor: str) -> tuple[datetime, datetime, str, datetime | None] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parts = json.loads(raw)
        if not isinstance(parts, list) or len(parts) not in (3, 4):
            return None
        anchor_raw, last_ts_raw, task_id = parts[:3]
        since_raw = parts[3] if len(parts) == 4 else None
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    anchor, last_ts, since = _parse_ts(anchor_raw), _parse_ts(last_ts_raw), _parse_ts(since_raw)
    if since_raw is not None and since is None:
        return None
    if anchor is None or last_ts is None or not isinstance(task_id, str) or not task_id:
        return None
    return anchor, last_ts, task_id, since


def _history_row_to_seat(row: tuple) -> dict:
    task_id, run_id, seat_id, orchestrator, event_type, sent_at, _id, payload = row
    payload = payload or {}
    return {
        "task_id": task_id,
        "run_id": run_id,
        "seat_id": seat_id,
        "orchestrator": orchestrator,
        "state": _history_seat_state(event_type, payload),
        "last_event": event_type,
        "last_event_ts": _iso(sent_at),
    }


_HISTORY_COLUMNS = "task_id, run_id, seat_id, orchestrator, event_type, sent_at, id, payload"


def _query_seats_history(conn, orchestrator_id, anchor, cursor_ts, cursor_task_id, limit, since=None):
    fetch_limit = limit + 1
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT DISTINCT ON (task_id)
                {_HISTORY_COLUMNS}
            FROM eval_event_raw
            WHERE orchestrator = %(orchestrator)s
              AND sent_at <= %(anchor)s
              AND (%(since)s::timestamptz IS NULL OR sent_at >= %(since)s::timestamptz)
            ORDER BY task_id, sent_at DESC, id DESC
        )
        SELECT {_HISTORY_COLUMNS}
        FROM latest
        WHERE %(cursor_ts)s::timestamptz IS NULL
           OR (sent_at, task_id) < (%(cursor_ts)s::timestamptz, %(cursor_task_id)s::text)
        ORDER BY sent_at DESC, task_id DESC
        LIMIT %(fetch_limit)s
        """,
        {
            "orchestrator": orchestrator_id,
            "anchor": anchor,
            "since": since,
            "cursor_ts": cursor_ts,
            "cursor_task_id": cursor_task_id,
            "fetch_limit": fetch_limit,
        },
    ).fetchall()


def _timeline_prefix(event: dict) -> str:
    return " ".join(
        part
        for part in (
            event.get("ts") or event.get("sent_at") or "",
            event.get("source") or "event",
            event.get("kind") or event.get("event_type") or "",
        )
        if part
    )


def format_timeline_summary(event: dict) -> str:
    prefix = _timeline_prefix(event)
    if event.get("source") != "transcript":
        return prefix

    kind = event.get("kind") or event.get("event_type") or ""
    meta = event.get("meta") or {}
    tool_name = event.get("tool_name") or ""
    if tool_name == "apply_patch" and meta.get("file"):
        detail = f"edited `{meta.get('file')}` +{_to_int(meta.get('added'))}/-{_to_int(meta.get('removed'))}"
    elif kind == "model_thinking":
        detail = "thinking"
    elif tool_name or kind in {"command_started", "command_finished", "command_output", "tool_call", "tool_output"}:
        detail = tool_name
    else:
        detail = event.get("content") or ""
    return " ".join(part for part in (prefix, detail) if part)


def format_timeline_event(event: dict) -> str:
    prefix = _timeline_prefix(event)
    if event.get("source") != "transcript":
        return prefix

    kind = event.get("kind") or event.get("event_type") or ""
    content = event.get("content") or ""
    meta = event.get("meta") or {}
    tool_name = event.get("tool_name") or ""
    detail = ""
    if tool_name == "apply_patch" and meta.get("file"):
        summary = f"edited `{meta.get('file')}` +{_to_int(meta.get('added'))}/-{_to_int(meta.get('removed'))}"
        detail = summary
        if content:
            detail = f"{summary}\n[dim]diff[/dim]\n{content}"
    elif kind == "model_thinking":
        detail = "[dim][thinking][/dim]"
        if content:
            detail = f"{detail}\n{content}"
    elif tool_name or kind in {"command_started", "command_finished", "command_output", "tool_call", "tool_output"}:
        detail = "\n".join(part for part in (tool_name, content) if part)
    else:
        detail = content
    return " ".join(part for part in (prefix, detail) if part)


def _transcript_event_from_row(row) -> dict:
    return {
        "source": "transcript",
        "run_id": row[0],
        "task_id": row[1],
        "seat_id": row[2],
        "orchestrator": row[3],
        "turn_index": int(row[4]),
        "item_id": row[5],
        "seq": int(row[6]),
        "kind": row[7],
        "event_type": row[7],
        "tool_name": row[8],
        "content": row[9],
        "meta": row[10] or {},
        "ts": _iso(row[11]),
        "stream_entry_id": row[12],
    }


def _transcript_event_from_fields(entry_id, fields) -> dict:
    kind = fields.get("kind")
    content = fields.get("content") or ""
    meta = {}
    if str(kind or "").startswith("command"):
        meta = _parse_apply_patch(content)
    return {
        "source": "transcript",
        "run_id": fields.get("run_id"),
        "task_id": fields.get("task_id"),
        "seat_id": fields.get("seat_id") or None,
        "orchestrator": fields.get("orchestrator") or None,
        "turn_index": _to_int(fields.get("turn_index")),
        "item_id": fields.get("item_id"),
        "seq": _to_int(fields.get("seq")),
        "kind": kind,
        "event_type": kind,
        "tool_name": fields.get("tool_name") or None,
        "content": content,
        "meta": meta,
        "ts": fields.get("ts"),
        "stream_entry_id": entry_id,
    }


_TRANSCRIPT_COLS = """run_id, task_id, seat_id, orchestrator, turn_index, item_id,
               seq, kind, tool_name, content, meta, ts, stream_entry_id"""


def _backfill_transcript(conn, task_id) -> list[dict]:
    head = conn.execute(
        "SELECT seat_id, orchestrator FROM transcript_io WHERE task_id = %s LIMIT 1",
        (task_id,),
    ).fetchone()
    # The warm orchestrator is its own orchestrator (seat_id == orchestrator); its transcript is
    # persistent, so cap it to the recent window. Seats keep their full (short) history.
    # bool(head[0]) guard: seat_id/orchestrator are nullable — a NULL==NULL row (legacy/external)
    # must NOT be mistaken for the orchestrator and silently capped. (panel: agy REQUEST-CHANGES)
    is_orchestrator = head is not None and bool(head[0]) and head[0] == head[1]
    if is_orchestrator:
        hours, lines = _orchestrator_backfill_window()
        rows = conn.execute(
            f"""
            SELECT {_TRANSCRIPT_COLS}
            FROM transcript_io
            WHERE task_id = %s AND ts >= now() - (%s * interval '1 hour')
            ORDER BY ts DESC, stream_entry_id DESC
            LIMIT %s
            """,
            (task_id, hours, lines),
        ).fetchall()
        rows = list(reversed(rows))  # back to chronological after the DESC+LIMIT recent-slice
        # stream_entry_id DESC tiebreaks equal-ts rows so the cap boundary is deterministic. (panel)
    else:
        rows = conn.execute(
            f"SELECT {_TRANSCRIPT_COLS} FROM transcript_io WHERE task_id = %s ORDER BY ts",
            (task_id,),
        ).fetchall()
    return [_transcript_event_from_row(row) for row in rows]


def _backfill_seat(conn, task_id) -> list[dict]:
    transcript_events = _backfill_transcript(conn, task_id)
    eval_rows = conn.execute(
        """
        SELECT run_id, seat_id, event_type, sent_at, payload
        FROM eval_event_raw
        WHERE task_id = %s
        ORDER BY sent_at
        """,
        (task_id,),
    ).fetchall()
    events = [
        {
            "source": "eval",
            "run_id": row[0],
            "task_id": task_id,
            "seat_id": row[1],
            "event_type": row[2],
            "ts": _iso(row[3]),
            "payload": row[4],
        }
        for row in eval_rows
    ]
    events.extend(transcript_events)
    if not eval_rows:
        return sorted(events, key=lambda event: event["ts"])

    run_id = eval_rows[0][0]
    seat_id = eval_rows[0][1]
    if not run_id or not seat_id:
        return events

    actor = "seat:" + seat_id
    vote_rows = conn.execute(
        """
        SELECT seq, kind, ts, payload
        FROM audit_events
        WHERE run_id = %s AND payload->>'actor' = %s
        ORDER BY ts
        """,
        (run_id, actor),
    ).fetchall()
    events.extend(
        {
            "source": "audit",
            "run_id": run_id,
            "task_id": task_id,
            "seat_id": seat_id,
            "seq": int(row[0]),
            "event_type": row[1],
            "ts": _iso(row[2]),
            "payload": row[3],
        }
        for row in vote_rows
    )
    return sorted(events, key=lambda event: event["ts"])


def build_visibility_app(
    *,
    bus_redis_url,
    bus_prefix,
    dsn,
    public_base_url,
    trace_redis_url=None,
    trace_prefix=None,
    re_validate_s=SSE_RE_VALIDATE_S,
) -> Starlette:
    # Socket timeouts so a black-holed bus fails fast instead of hanging a
    # worker thread indefinitely (audit VIS-2). The short timeouts are deliberate and kept;
    # the baseline adds keepalive + pool health checks on top, so a connection that died while
    # idle reconnects instead of failing the next command.
    redis_client = redis.from_url(
        bus_redis_url,
        **redis_conn.connect_kwargs(socket_connect_timeout=5, socket_timeout=5),
    )
    stream = f"{bus_prefix}events:live"
    last_seen_key = f"{bus_prefix}orchestrators:last_seen"
    expected_tees = _expected_tee_labels()
    if not expected_tees:
        logger.warning("ARB_VIS_EXPECTED_TEES is empty — tee staleness surfacing is INERT; set it to e.g. claude-tail.bridge-dev")
    trace_redis_url = trace_redis_url if trace_redis_url is not None else os.environ.get("ARB_TRACE_REDIS_URL")
    trace_prefix = trace_prefix if trace_prefix is not None else os.environ.get("ARB_TRACE_PREFIX", "")
    trace_stream = f"{trace_prefix}arbmem:trace" if trace_redis_url else None
    logger.info("visibility events stream key=%s", stream)
    last_seen_task = None
    login_settings = _visibility_login_settings(public_base_url=public_base_url, dsn=dsn)
    login_enabled = login_settings is not None

    def _authenticate_blocking(token):
        if not token:
            return None
        with psycopg.connect(dsn) as conn:
            return _principal(conn, token, public_base_url)

    async def authenticated(token):
        return await anyio.to_thread.run_sync(_authenticate_blocking, token)

    def _visibility_session_blocking(session_id):
        if not login_enabled or not session_id:
            return None
        with psycopg.connect(dsn) as conn:
            session = oauth_store.get_login_session(conn, session_id)
        if session is None or session.get("verified_at") is None:
            return None
        return session

    async def visibility_session(request: Request):
        return await anyio.to_thread.run_sync(
            _visibility_session_blocking,
            request.cookies.get("arbmem_login", ""),
        )

    async def request_authenticated(request: Request):
        session = await visibility_session(request)
        if session is not None:
            return {"kind": "session", "session_id": session["session_id"]}
        principal = await authenticated(_bearer_token(request))
        if principal is not None:
            return {"kind": "token", "principal": principal}
        return None

    def _revalidation_due_in(last_check):
        return max(0.0, re_validate_s - (asyncio.get_running_loop().time() - last_check))

    async def _auth_still_valid(request):
        return await request_authenticated(request) is not None

    def _new_visibility_login_session_blocking():
        if not login_enabled:
            return None
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=login_settings.login_ttl)
        with psycopg.connect(dsn) as conn:
            oauth_store.put_login_session(
                conn,
                session_id=session_id,
                csrf_token=csrf_token,
                authorize_state={
                    "client_id": VISIBILITY_LOGIN_CLIENT_ID,
                    "redirect_uri": "/",
                },
                expires_at=expires_at,
            )
        return session_id, csrf_token

    def _visibility_login_post_blocking(session_id, supplied_csrf, passphrase, totp):
        if not login_enabled:
            return "forbidden", 403, None
        with psycopg.connect(dsn) as conn:
            session = oauth_store.get_login_session(conn, session_id)
            if session is None:
                return "invalid session", 404, None
            if session["fail_count"] >= 5:
                return "locked", 429, None
            if (
                oauth_store.get_global_login_fail_count(
                    conn,
                    VISIBILITY_LOGIN_CLIENT_ID,
                    window_seconds=login_settings.login_ttl,
                )
                >= 5
            ):
                return "locked", 429, None
            if (
                oauth_store.get_global_login_fail_count(
                    conn,
                    VISIBILITY_GLOBAL_KEY,
                    window_seconds=login_settings.login_ttl,
                )
                >= login_settings.login_global_fail_cap
            ):
                return "locked", 429, None
            if not hmac.compare_digest(supplied_csrf, session["csrf_token"]):
                return "forbidden", 403, None
            if not verify_two_factor(passphrase, totp, settings=login_settings):
                fail_count = oauth_store.bump_fail(conn, session_id) or 0
                client_fail_count = oauth_store.bump_global_login_fail(
                    conn,
                    VISIBILITY_LOGIN_CLIENT_ID,
                    window_seconds=login_settings.login_ttl,
                )
                all_fail_count = oauth_store.bump_global_login_fail(
                    conn,
                    VISIBILITY_GLOBAL_KEY,
                    window_seconds=login_settings.login_ttl,
                )
                locked = (
                    fail_count >= 5
                    or client_fail_count >= 5
                    or all_fail_count >= login_settings.login_global_fail_cap
                )
                return ("locked", 429, None) if locked else ("forbidden", 403, None)

            now = datetime.now(timezone.utc)
            ttl_secs = _visibility_session_ttl_secs()
            verified_session_id = secrets.token_urlsafe(32)
            oauth_store.put_login_session(
                conn,
                session_id=verified_session_id,
                csrf_token=secrets.token_urlsafe(32),
                authorize_state=session["authorize_state"],
                expires_at=now + timedelta(seconds=ttl_secs),
            )
            conn.execute(
                "UPDATE mcp_auth.login_sessions SET verified_at = %s WHERE session_id = %s",
                (now, verified_session_id),
            )
            conn.execute("DELETE FROM mcp_auth.login_sessions WHERE session_id = %s", (session_id,))
            oauth_store.reset_global_login_fail(conn, VISIBILITY_LOGIN_CLIENT_ID)
            oauth_store.reset_global_login_fail(conn, VISIBILITY_GLOBAL_KEY)
        return "ok", 302, verified_session_id

    def _current_last_seen_sync(orchestrator):
        if hasattr(redis_client, "hget"):
            return redis_client.hget(last_seen_key, orchestrator)
        if hasattr(redis_client, "hgetall"):
            return (redis_client.hgetall(last_seen_key) or {}).get(orchestrator)
        return None

    def _store_last_seen_sync(orchestrator, sent_at) -> None:
        current = _current_last_seen_sync(orchestrator)
        value = update_last_seen(current, sent_at)
        if value is None or value == current:
            return
        redis_client.hset(last_seen_key, orchestrator, value)

    def _seed_orchestrator_last_seen():
        latest_stream_id = None
        latest_by_orchestrator = {}
        for entry_id, fields in redis_client.xrevrange(stream, count=ORCHESTRATOR_LAST_SEEN_SEED_COUNT):
            latest_stream_id = latest_stream_id or entry_id
            orchestrator = fields.get("orchestrator")
            sent_at = fields.get("sent_at")
            if not orchestrator or not sent_at:
                continue
            latest_by_orchestrator[orchestrator] = update_last_seen(
                latest_by_orchestrator.get(orchestrator),
                sent_at,
            )
        for orchestrator, sent_at in latest_by_orchestrator.items():
            _store_last_seen_sync(orchestrator, sent_at)
        return latest_stream_id or "$"

    async def _store_last_seen_async(client, orchestrator, sent_at) -> None:
        current = await client.hget(last_seen_key, orchestrator)
        value = update_last_seen(current, sent_at)
        if value is None or value == current:
            return
        await client.hset(last_seen_key, orchestrator, value)

    async def _orchestrator_last_seen_updater():
        while True:
            client = None
            try:
                last_id = await anyio.to_thread.run_sync(_seed_orchestrator_last_seen)
                client = aioredis.from_url(bus_redis_url, **redis_conn.connect_kwargs())
                while True:
                    rows = await client.xread({stream: last_id}, block=SSE_BLOCK_MS)
                    for _stream_name, entries in rows or []:
                        for entry_id, fields in entries:
                            last_id = entry_id
                            orchestrator = fields.get("orchestrator")
                            sent_at = fields.get("sent_at")
                            if orchestrator and sent_at:
                                await _store_last_seen_async(client, orchestrator, sent_at)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("orchestrator last-seen updater failed; reconnecting", exc_info=True)
                await asyncio.sleep(1.0)
            finally:
                if client is not None:
                    await client.aclose()

    async def _startup():
        nonlocal last_seen_task
        last_seen_task = asyncio.create_task(_orchestrator_last_seen_updater())

    async def _shutdown():
        if last_seen_task is None:
            return
        last_seen_task.cancel()
        await asyncio.gather(last_seen_task, return_exceptions=True)

    @asynccontextmanager
    async def _lifespan(app):
        await _startup()
        try:
            yield
        finally:
            await _shutdown()

    def _orchestrators_from_tail() -> list[str]:
        seen = []
        for _entry_id, fields in redis_client.xrevrange(stream, count=SSE_BACKFILL_COUNT):
            orchestrator = fields.get("orchestrator")
            if orchestrator and orchestrator not in seen:
                seen.append(orchestrator)
        return seen

    def _backfill_seat_blocking(task_id):
        with psycopg.connect(dsn) as conn:
            return _backfill_seat(conn, task_id)

    def _seats_history_blocking(orchestrator_id, anchor, cursor_ts, cursor_task_id, limit, since):
        with psycopg.connect(dsn) as conn:
            return _query_seats_history(conn, orchestrator_id, anchor, cursor_ts, cursor_task_id, limit, since)

    async def login_get(request: Request):
        if not login_enabled:
            return PlainTextResponse("login disabled", status_code=404)
        session = await anyio.to_thread.run_sync(_new_visibility_login_session_blocking)
        if session is None:
            return PlainTextResponse("login disabled", status_code=404)
        session_id, csrf_token = session
        response = _login_form(session_id, csrf_token)
        _session_cookie(response, session_id)
        return response

    async def login_post(request: Request):
        form = await request.form()
        session_id = str(form.get("session") or request.cookies.get("arbmem_login") or "")
        if not session_id:
            return PlainTextResponse("missing session", status_code=400)
        body, status_code, verified_session_id = await anyio.to_thread.run_sync(
            _visibility_login_post_blocking,
            session_id,
            str(form.get("csrf_token") or ""),
            str(form.get("passphrase") or ""),
            str(form.get("totp") or ""),
        )
        if status_code != 302:
            return PlainTextResponse(body, status_code=status_code)
        response = RedirectResponse("/", status_code=302)
        _visibility_session_cookie(response, verified_session_id, max_age=_visibility_session_ttl_secs())
        return response

    async def index(request: Request):
        if login_enabled and await request_authenticated(request) is None:
            return RedirectResponse("/login", status_code=302)
        return Response(
            (STATIC_DIR / "index.html").read_text(),
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def app_js(request: Request):
        if login_enabled and await request_authenticated(request) is None:
            return RedirectResponse("/login", status_code=302)
        return Response(
            (STATIC_DIR / "app.js").read_text(),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    async def icon_png(request: Request):
        # Browser-requested before any login; public, static, cacheable.
        name = request.url.path.rsplit("/", 1)[-1]
        if name not in ("favicon-32.png", "apple-touch-icon.png"):
            return PlainTextResponse("not found", status_code=404)
        return Response(
            (STATIC_DIR / name).read_bytes(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def journey(request: Request):
        if await request_authenticated(request) is None:
            if login_enabled and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/login", status_code=302)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return Response(
            (STATIC_DIR / "journey.html").read_text(encoding="utf-8"),
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def journey_graph(request: Request):
        if await request_authenticated(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        snapshot_dir = Path(os.environ.get("ARB_JOURNEY_SNAPSHOT_DIR", "/journey-snapshot"))
        snapshot_path = snapshot_dir / "graph.json"
        if not snapshot_path.exists():
            return JSONResponse({"error": NO_SNAPSHOT_ERROR}, status_code=503)
        return Response(
            snapshot_path.read_text(encoding="utf-8"),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    def _orchestrators_blocking() -> tuple[list[str], list[dict]]:
        # All Redis I/O for the route in one blocking body so the async handler
        # can offload it to a thread — a synchronous Redis call on the event
        # loop stalls every concurrent request and open SSE stream (audit VIS-2).
        last_seen = {}
        if hasattr(redis_client, "hgetall"):
            try:
                last_seen = redis_client.hgetall(last_seen_key) or {}
            except Exception:
                logger.warning("orchestrator last-seen hash read failed; falling back to tail scan", exc_info=True)
        if last_seen:
            now = datetime.now(timezone.utc)
            stale_secs = _orchestrator_stale_secs()
            stale = []
            for orchestrator, sent_at in last_seen.items():
                seen_ts = _aware_ts(sent_at)
                if seen_ts is None or (now - seen_ts).total_seconds() > stale_secs:
                    stale.append(orchestrator)
            if stale and hasattr(redis_client, "hdel"):
                try:
                    redis_client.hdel(last_seen_key, *stale)
                except Exception:
                    logger.warning("orchestrator last-seen stale prune failed", exc_info=True)
            result = build_orchestrator_list(last_seen, now, stale_secs)
        else:
            result = _orchestrators_from_tail()
        tees = tee_states(redis_client, bus_prefix, expected_tees, datetime.now(timezone.utc))
        return result, tees

    async def orchestrators(request: Request):
        if await request_authenticated(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            result, tees = await anyio.to_thread.run_sync(_orchestrators_blocking)
        except Exception:
            # The guarded hash path falls through to the tail scan, which can
            # still raise on a full outage — return a clean 503, never a bare
            # 500 or a hung worker (audit VIS-2).
            logger.warning("orchestrators route failed against the bus", exc_info=True)
            return JSONResponse({"error": "bus unavailable"}, status_code=503)
        return JSONResponse({"orchestrators": result, "tees": tees})

    async def sse_orchestrator(request: Request):
        if await request_authenticated(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        orchestrator_id = request.path_params["orchestrator_id"]
        last_id = request.headers.get("Last-Event-ID")

        async def events():
            nonlocal last_id
            r = aioredis.from_url(bus_redis_url, **redis_conn.connect_kwargs())
            seats = {}
            loop = asyncio.get_running_loop()
            last_heartbeat = loop.time()
            last_revalidation = loop.time()

            try:
                if not last_id:
                    for entry_id, fields in await r.xrange(stream, count=SSE_BACKFILL_COUNT):
                        if fields.get("orchestrator") != orchestrator_id:
                            continue
                        fields = dict(fields)
                        await _apply_status_backfill(fields, r, bus_prefix)
                        previous = seats.get(fields.get("task_id"))
                        current = _reduce_seat(previous or {}, fields)
                        seats[current["task_id"]] = current
                        last_id = entry_id
                        yield _sse_frame(entry_id, _seat_event_name(previous, current), current)
                    last_id = last_id or "$"

                while True:
                    if await request.is_disconnected():
                        return
                    now = loop.time()
                    if now - last_revalidation >= re_validate_s:
                        if not await _auth_still_valid(request):
                            return
                        last_revalidation = loop.time()
                    block_ms = min(SSE_BLOCK_MS, max(1, int(_revalidation_due_in(last_revalidation) * 1000)))
                    rows = await r.xread({stream: last_id}, block=block_ms)
                    if rows:
                        for _stream_name, entries in rows:
                            for entry_id, fields in entries:
                                last_id = entry_id
                                if fields.get("orchestrator") != orchestrator_id:
                                    continue
                                fields = dict(fields)
                                await _apply_status_backfill(fields, r, bus_prefix)
                                previous = seats.get(fields.get("task_id"))
                                current = _reduce_seat(previous or {}, fields)
                                seats[current["task_id"]] = current
                                yield _sse_frame(entry_id, _seat_event_name(previous, current), current)
                        continue

                    now = loop.time()
                    stale_updates = []
                    for task_id, state in list(seats.items()):
                        current = _reduce_seat(state, {})
                        if current.get("state") == "stale" and state.get("state") != "stale":
                            seats[task_id] = current
                            stale_updates.append((task_id, current))
                    for task_id, current in stale_updates:
                        yield _sse_frame(f"stale-{task_id}", "seat_finish", current)
                    if now - last_heartbeat >= SSE_HEARTBEAT_S:
                        last_heartbeat = now
                        yield ": ping\n\n"
            finally:
                await r.aclose()

        return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)

    async def sse_seat(request: Request):
        if await request_authenticated(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        task_id = request.path_params["task_id"]
        last_id = request.headers.get("Last-Event-ID")

        async def events():
            nonlocal last_id
            events_cursor, trace_cursor = _parse_seat_resume_id(last_id)
            loop = asyncio.get_running_loop()
            last_heartbeat = loop.time()
            last_revalidation = loop.time()
            queue: asyncio.Queue[tuple] = asyncio.Queue()
            tasks = []
            bus_reader = aioredis.from_url(bus_redis_url, **redis_conn.connect_kwargs())
            trace_reader = aioredis.from_url(trace_redis_url, **redis_conn.connect_kwargs()) if trace_stream is not None else None

            try:
                if not last_id:
                    backfill = await anyio.to_thread.run_sync(_backfill_seat_blocking, task_id)
                    for idx, event in enumerate(backfill, start=1):
                        yield _sse_frame(f"backfill-{idx}", "backfill", event)

                async def read_events_live(start_id):
                    current_id = start_id
                    try:
                        while True:
                            rows = await bus_reader.xread({stream: current_id}, block=SSE_BLOCK_MS)
                            for _stream_name, entries in rows or []:
                                for entry_id, fields in entries:
                                    current_id = entry_id
                                    if fields.get("task_id") != task_id:
                                        continue
                                    data = dict(fields)
                                    data["payload"] = _entry_data(fields)
                                    await queue.put(("frame", "events", entry_id, "event", data))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await queue.put(("error", "events", exc))
                    finally:
                        await bus_reader.aclose()

                async def read_trace_live(start_id):
                    if trace_stream is None or trace_reader is None:
                        return
                    current_id = start_id
                    try:
                        while True:
                            rows = await trace_reader.xread({trace_stream: current_id}, block=SSE_BLOCK_MS)
                            for _stream_name, entries in rows or []:
                                for entry_id, fields in entries:
                                    current_id = entry_id
                                    if fields.get("task_id") != task_id:
                                        continue
                                    await queue.put(
                                        (
                                            "frame",
                                            "trace",
                                            entry_id,
                                            "transcript",
                                            _transcript_event_from_fields(entry_id, fields),
                                        )
                                    )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await queue.put(("error", "trace", exc))
                    finally:
                        await trace_reader.aclose()

                tasks.append(asyncio.create_task(read_events_live(events_cursor)))
                if trace_stream is not None:
                    tasks.append(asyncio.create_task(read_trace_live(trace_cursor)))

                while True:
                    if await request.is_disconnected():
                        return
                    now = loop.time()
                    if now - last_revalidation >= re_validate_s:
                        if not await _auth_still_valid(request):
                            return
                        last_revalidation = loop.time()
                    try:
                        timeout_s = min(SSE_BLOCK_MS / 1000, _revalidation_due_in(last_revalidation))
                        item = await asyncio.wait_for(queue.get(), timeout=max(0.001, timeout_s))
                    except asyncio.TimeoutError:
                        item = None
                    if item is not None:
                        if item[0] == "error":
                            exc = item[2]
                            logger.warning(
                                "seat SSE %s reader failed; closing stream",
                                item[1],
                                exc_info=(type(exc), exc, exc.__traceback__),
                            )
                            return
                        _kind, source, entry_id, event_name, data = item
                        if source == "events":
                            events_cursor = entry_id
                        elif source == "trace":
                            trace_cursor = entry_id
                        yield _sse_frame(_seat_resume_id(events_cursor, trace_cursor), event_name, data)
                        continue

                    now = loop.time()
                    if now - last_heartbeat >= SSE_HEARTBEAT_S:
                        last_heartbeat = now
                        yield ": ping\n\n"
            finally:
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                else:
                    await bus_reader.aclose()
                    if trace_reader is not None:
                        await trace_reader.aclose()

        return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)

    async def seats_history(request: Request):
        if await request_authenticated(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        orchestrator_id = request.path_params["orchestrator_id"]
        limit = _clamp_history_limit(request.query_params.get("limit"))
        raw_cursor = request.query_params.get("cursor") or ""
        raw_date = request.query_params.get("date") or ""

        if raw_cursor:
            decoded = _decode_history_cursor(raw_cursor)
            if decoded is None:
                return JSONResponse({"error": "invalid cursor"}, status_code=400)
            anchor, cursor_ts, cursor_task_id, since = decoded
        else:
            cursor_ts, cursor_task_id = None, None
            since = None
            if raw_date:
                bounds = _history_day_bounds(raw_date)
                if bounds is None:
                    return JSONResponse({"error": "invalid date"}, status_code=400)
                since, anchor = bounds
            else:
                anchor = datetime.now(timezone.utc)

        try:
            rows = await anyio.to_thread.run_sync(
                _seats_history_blocking,
                orchestrator_id,
                anchor,
                cursor_ts,
                cursor_task_id,
                limit,
                since,
            )
        except psycopg.Error:
            logger.exception("seats_history query failed for orchestrator=%s", orchestrator_id)
            return JSONResponse({"error": "history unavailable"}, status_code=503)

        has_more = len(rows) > limit
        page = rows[:limit]
        seats = [_history_row_to_seat(row) for row in page]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _encode_history_cursor(anchor, last[5], last[0], since)
        return JSONResponse({"seats": seats, "next_cursor": next_cursor, "has_more": has_more})

    return Starlette(
        routes=[
            Route("/", index, methods=["GET"]),
            Route("/login", login_get, methods=["GET"]),
            Route("/login", login_post, methods=["POST"]),
            Route("/app.js", app_js, methods=["GET"]),
            Route("/favicon-32.png", icon_png, methods=["GET"]),
            Route("/apple-touch-icon.png", icon_png, methods=["GET"]),
            Route("/journey", journey, methods=["GET"]),
            Route("/journey/graph.json", journey_graph, methods=["GET"]),
            Route("/orchestrators", orchestrators, methods=["GET"]),
            Route("/sse/orchestrator/{orchestrator_id}", sse_orchestrator, methods=["GET"]),
            Route("/sse/seat/{task_id}", sse_seat, methods=["GET"]),
            Route("/orchestrators/{orchestrator_id}/seats/history", seats_history, methods=["GET"]),
        ],
        lifespan=_lifespan,
    )

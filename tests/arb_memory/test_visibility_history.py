from datetime import datetime, timedelta, timezone
import base64
import json
import os
import uuid

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from arb_memory.mcp import oauth_store
from arb_memory.visibility import (
    _clamp_history_limit,
    _decode_history_cursor,
    _encode_history_cursor,
    _history_row_to_seat,
    _history_seat_state,
    _query_seats_history,
    build_visibility_app,
)


RESOURCE = "https://mem.example.com"


class FakeRedis:
    def xrevrange(self, key, count=200):
        return []


def _app_client(monkeypatch, conn=None):
    # DB-less tests (e.g. the 401 auth check) short-circuit before any connection, so a
    # placeholder DSN is fine when no conn is passed; DB-backed callers come through the
    # `scratch` fixture (which skips when ARB_MEMORY_DSN is unset) and pass a real conn.
    dsn = os.environ.get("ARB_MEMORY_DSN", "postgresql://placeholder:placeholder@127.0.0.1:5432/placeholder")
    if conn is not None:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        dsn = make_conninfo(dsn, options=f"-csearch_path={schema},public")
    monkeypatch.setenv("ARB_MEMORY_DSN", dsn)
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: FakeRedis())
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=dsn,
        public_base_url=RESOURCE,
    )
    return TestClient(app), app


def _put_access_token(conn):
    token = f"access-{uuid.uuid4().hex}"
    oauth_store.put_access_token(
        conn,
        token=token,
        client_id=f"client-{uuid.uuid4().hex}",
        resource=RESOURCE,
        scopes=["memory.read"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    return token


def _seed_event(conn, *, orchestrator, task_id, run_id, seat_id, event_type, sent_at, payload=None):
    conn.execute(
        """
        INSERT INTO eval_event_raw
            (run_id, task_id, seat_id, orchestrator, event_type, sent_at, payload, stream_entry_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            task_id,
            seat_id,
            orchestrator,
            event_type,
            sent_at,
            Jsonb(payload or {}),
            f"{uuid.uuid4().hex}-0",
        ),
    )


def test_history_limit_non_numeric_defaults_to_50():
    assert _clamp_history_limit(None) == 50
    assert _clamp_history_limit("") == 50
    assert _clamp_history_limit("abc") == 50


def test_history_limit_out_of_range_clamps_to_1_and_200():
    assert _clamp_history_limit("0") == 1
    assert _clamp_history_limit("-5") == 1
    assert _clamp_history_limit("500") == 200
    assert _clamp_history_limit("75") == 75


def test_history_state_task_finished_ok_true_is_done():
    assert _history_seat_state("task_finished", {"ok": True}) == "done"
    assert _history_seat_state("task_finished", {}) == "done"


def test_history_state_task_finished_ok_false_is_failed():
    assert _history_seat_state("task_finished", {"ok": False}) == "failed"


def test_history_state_task_started_and_continuing_is_incomplete():
    assert _history_seat_state("task_started", {}) == "incomplete"
    assert _history_seat_state("task_continuing", {}) == "incomplete"


def test_history_state_unrecognized_event_type_is_unknown():
    assert _history_seat_state("some_future_event", {}) == "unknown"


def test_history_state_commit_events_are_a_crash_edge_done_fallback():
    for event_type in (
        "agent_committed",
        "orchestrator_committed",
        "post_timeout_agent_committed",
        "post_timeout_committed",
    ):
        assert _history_seat_state(event_type, {}) == "done"


def test_history_state_steer_and_cancel_are_a_crash_edge_incomplete_fallback():
    assert _history_seat_state("steer_sent", {}) == "incomplete"
    assert _history_seat_state("cancel_sent", {}) == "incomplete"


def test_history_cursor_round_trips_anchor_ts_and_task_id():
    anchor = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    last_ts = datetime(2026, 6, 30, 11, 0, 0, tzinfo=timezone.utc)
    since = datetime(2026, 6, 30, 0, 0, 0, tzinfo=timezone.utc)
    cursor = _encode_history_cursor(anchor, last_ts, "task-123")
    assert _decode_history_cursor(cursor) == (anchor, last_ts, "task-123", None)
    cursor = _encode_history_cursor(anchor, last_ts, "task-123", since)
    assert _decode_history_cursor(cursor) == (anchor, last_ts, "task-123", since)


def test_history_cursor_decodes_legacy_three_item_shape():
    anchor = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    last_ts = datetime(2026, 6, 30, 11, 0, 0, tzinfo=timezone.utc)
    legacy = base64.urlsafe_b64encode(
        json.dumps([anchor.isoformat(), last_ts.isoformat(), "task-legacy"]).encode()
    ).decode("ascii").rstrip("=")

    assert _decode_history_cursor(legacy) == (anchor, last_ts, "task-legacy", None)


def test_history_cursor_bad_base64_is_malformed():
    assert _decode_history_cursor("!!!not-base64!!!") is None


def test_history_cursor_bad_json_is_malformed():
    garbage = base64.urlsafe_b64encode(b"not json").decode("ascii").rstrip("=")
    assert _decode_history_cursor(garbage) is None


def test_history_cursor_wrong_shape_is_malformed():
    two_tuple = base64.urlsafe_b64encode(
        json.dumps(["2026-06-30T12:00:00+00:00", "2026-06-30T11:00:00+00:00"]).encode()
    ).decode("ascii").rstrip("=")
    non_string_task_id = base64.urlsafe_b64encode(
        json.dumps(["2026-06-30T12:00:00+00:00", "2026-06-30T11:00:00+00:00", 123]).encode()
    ).decode("ascii").rstrip("=")
    assert _decode_history_cursor(two_tuple) is None
    assert _decode_history_cursor(non_string_task_id) is None


def test_history_row_to_seat_field_shape_matches_live_reducer_keys():
    sent_at = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    row = ("task-1", "run-1", "seat-1", "orch-1", "task_finished", sent_at, 42, {"ok": True})
    seat = _history_row_to_seat(row)
    assert set(seat.keys()) == {
        "task_id",
        "run_id",
        "seat_id",
        "orchestrator",
        "state",
        "last_event",
        "last_event_ts",
    }
    assert seat["task_id"] == "task-1"
    assert seat["state"] == "done"
    assert seat["last_event"] == "task_finished"
    assert seat["last_event_ts"] == sent_at.isoformat()
    assert "voted" not in seat and "stance" not in seat
    assert "model" not in seat and "engine_model" not in seat


def test_history_pagination_walks_all_seats_no_skip_no_repeat(scratch):
    orch = f"orch-{uuid.uuid4().hex}"
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    seeded = []
    for i in range(23):
        task_id = f"task-{i}-{uuid.uuid4().hex}"
        seeded.append(task_id)
        _seed_event(
            scratch,
            orchestrator=orch,
            task_id=task_id,
            run_id=f"run-{i}",
            seat_id=f"seat-{i}",
            event_type="task_finished",
            sent_at=base + timedelta(minutes=i),
            payload={"ok": True},
        )
    tie_a, tie_b = f"tie-a-{uuid.uuid4().hex}", f"tie-b-{uuid.uuid4().hex}"
    tie_ts = base + timedelta(minutes=100)
    _seed_event(
        scratch,
        orchestrator=orch,
        task_id=tie_a,
        run_id="run-tie-a",
        seat_id="seat-tie-a",
        event_type="task_finished",
        sent_at=tie_ts,
        payload={"ok": True},
    )
    _seed_event(
        scratch,
        orchestrator=orch,
        task_id=tie_b,
        run_id="run-tie-b",
        seat_id="seat-tie-b",
        event_type="task_finished",
        sent_at=tie_ts,
        payload={"ok": True},
    )
    seeded += [tie_a, tie_b]

    anchor = datetime.now(timezone.utc)
    seen = []
    cursor_ts, cursor_task_id = None, None
    limit = 5
    for _ in range(20):
        rows = _query_seats_history(scratch, orch, anchor, cursor_ts, cursor_task_id, limit)
        page, has_more = rows[:limit], len(rows) > limit
        if not page:
            break
        seen.extend(row[0] for row in page)
        cursor_ts, cursor_task_id = page[-1][5], page[-1][0]
        if not has_more:
            break

    assert sorted(seen) == sorted(seeded)
    assert len(seen) == len(set(seen))


def test_history_pagination_anchor_is_stable_against_concurrent_writes(scratch):
    orch = f"orch-{uuid.uuid4().hex}"
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    task_ids = [f"task-{i}-{uuid.uuid4().hex}" for i in range(6)]
    for i, task_id in enumerate(task_ids):
        _seed_event(
            scratch,
            orchestrator=orch,
            task_id=task_id,
            run_id=f"run-{i}",
            seat_id=f"seat-{i}",
            event_type="task_finished",
            sent_at=base + timedelta(minutes=i),
            payload={"ok": True},
        )

    anchor = datetime.now(timezone.utc)
    limit = 3
    page1 = _query_seats_history(scratch, orch, anchor, None, None, limit)
    assert len(page1) == limit + 1
    page1_ids = [row[0] for row in page1[:limit]]
    cursor_ts, cursor_task_id = page1[limit - 1][5], page1[limit - 1][0]

    _seed_event(
        scratch,
        orchestrator=orch,
        task_id=task_ids[-1],
        run_id="run-5",
        seat_id="seat-5",
        event_type="task_finished",
        sent_at=datetime.now(timezone.utc),
        payload={"ok": True},
    )

    page2 = _query_seats_history(scratch, orch, anchor, cursor_ts, cursor_task_id, limit)
    page2_ids = [row[0] for row in page2[:limit]]
    assert not (set(page1_ids) & set(page2_ids))
    assert task_ids[-1] not in page2_ids
    assert set(page2_ids) <= set(task_ids) - set(page1_ids)


def test_history_first_page_null_cursor_does_not_raise(scratch):
    orch = f"orch-{uuid.uuid4().hex}"
    _seed_event(
        scratch,
        orchestrator=orch,
        task_id="t1",
        run_id="r1",
        seat_id="s1",
        event_type="task_finished",
        sent_at=datetime.now(timezone.utc),
        payload={"ok": True},
    )
    rows = _query_seats_history(scratch, orch, datetime.now(timezone.utc), None, None, 50)
    assert len(rows) == 1


def test_history_scoped_to_one_orchestrator(scratch):
    orch_a, orch_b = f"orch-a-{uuid.uuid4().hex}", f"orch-b-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    _seed_event(
        scratch,
        orchestrator=orch_a,
        task_id="ta",
        run_id="ra",
        seat_id="sa",
        event_type="task_finished",
        sent_at=now,
        payload={"ok": True},
    )
    _seed_event(
        scratch,
        orchestrator=orch_b,
        task_id="tb",
        run_id="rb",
        seat_id="sb",
        event_type="task_finished",
        sent_at=now,
        payload={"ok": True},
    )
    rows = _query_seats_history(scratch, orch_a, datetime.now(timezone.utc), None, None, 50)
    assert [row[0] for row in rows] == ["ta"]


def test_history_unauthorized_returns_401(monkeypatch):
    client, _ = _app_client(monkeypatch)
    r = client.get("/orchestrators/orch-1/seats/history")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_history_empty_orchestrator_returns_200_empty(scratch, monkeypatch):
    token = _put_access_token(scratch)
    client, _ = _app_client(monkeypatch, scratch)
    r = client.get(
        f"/orchestrators/no-such-orch-{uuid.uuid4().hex}/seats/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json() == {"seats": [], "next_cursor": None, "has_more": False}


def test_history_malformed_cursor_returns_400(scratch, monkeypatch):
    token = _put_access_token(scratch)
    client, _ = _app_client(monkeypatch, scratch)
    r = client.get(
        "/orchestrators/orch-1/seats/history?cursor=!!!not-valid!!!",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert r.json() == {"error": "invalid cursor"}


def test_history_psql_error_returns_503(scratch, monkeypatch):
    token = _put_access_token(scratch)
    client, _ = _app_client(monkeypatch, scratch)

    def boom(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr("arb_memory.visibility._query_seats_history", boom)
    r = client.get("/orchestrators/orch-1/seats/history", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    assert r.json() == {"error": "history unavailable"}
    assert "Traceback" not in r.text


def test_history_votes_are_a_dead_branch(scratch, monkeypatch):
    orch = f"orch-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    _seed_event(
        scratch,
        orchestrator=orch,
        task_id="voted-seat",
        run_id="run-v",
        seat_id="seat-v",
        event_type="task_started",
        sent_at=now - timedelta(minutes=1),
    )
    _seed_event(
        scratch,
        orchestrator=orch,
        task_id="voted-seat",
        run_id="run-v",
        seat_id="seat-v",
        event_type="task_finished",
        sent_at=now,
        payload={"ok": True},
    )
    token = _put_access_token(scratch)
    client, _ = _app_client(monkeypatch, scratch)
    r = client.get(f"/orchestrators/{orch}/seats/history", headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    assert body["seats"][0]["state"] == "done"
    assert "voted" not in body["seats"][0] and "stance" not in body["seats"][0]


def test_history_field_shape_matches_live_reducer_keys(scratch, monkeypatch):
    orch = f"orch-{uuid.uuid4().hex}"
    _seed_event(
        scratch,
        orchestrator=orch,
        task_id="t1",
        run_id="r1",
        seat_id="s1",
        event_type="task_finished",
        sent_at=datetime.now(timezone.utc),
        payload={"ok": True},
    )
    token = _put_access_token(scratch)
    client, _ = _app_client(monkeypatch, scratch)
    r = client.get(f"/orchestrators/{orch}/seats/history", headers={"Authorization": f"Bearer {token}"})
    assert set(r.json()["seats"][0].keys()) == {
        "task_id",
        "run_id",
        "seat_id",
        "orchestrator",
        "state",
        "last_event",
        "last_event_ts",
    }


def test_history_pagination_via_http_walks_pages(scratch, monkeypatch):
    orch = f"orch-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    for i in range(7):
        _seed_event(
            scratch,
            orchestrator=orch,
            task_id=f"t{i}",
            run_id=f"r{i}",
            seat_id=f"s{i}",
            event_type="task_finished",
            sent_at=now + timedelta(minutes=i),
            payload={"ok": True},
        )
    token = _put_access_token(scratch)
    client, _ = _app_client(monkeypatch, scratch)
    r1 = client.get(f"/orchestrators/{orch}/seats/history?limit=3", headers={"Authorization": f"Bearer {token}"})
    body1 = r1.json()
    assert len(body1["seats"]) == 3 and body1["has_more"] is True and body1["next_cursor"]
    r2 = client.get(
        f"/orchestrators/{orch}/seats/history?limit=3&cursor={body1['next_cursor']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    body2 = r2.json()
    seen1 = {s["task_id"] for s in body1["seats"]}
    seen2 = {s["task_id"] for s in body2["seats"]}
    assert len(seen1 | seen2) == len(seen1) + len(seen2)


def test_history_date_filter_limits_to_utc_day_boundaries_and_paginates(scratch, monkeypatch):
    orch = f"orch-{uuid.uuid4().hex}"
    day = datetime(2026, 7, 4, tzinfo=timezone.utc)
    _seed_event(
        scratch,
        orchestrator=orch,
        task_id="before-day",
        run_id="run-before",
        seat_id="seat-before",
        event_type="task_finished",
        sent_at=day - timedelta(microseconds=1),
        payload={"ok": True},
    )
    for idx, sent_at in enumerate(
        [
            day,
            day + timedelta(hours=12),
            day + timedelta(hours=23, minutes=59, seconds=59, microseconds=999999),
        ]
    ):
        _seed_event(
            scratch,
            orchestrator=orch,
            task_id=f"day-seat-{idx}",
            run_id=f"run-day-{idx}",
            seat_id=f"seat-day-{idx}",
            event_type="task_finished",
            sent_at=sent_at,
            payload={"ok": True},
        )
    _seed_event(
        scratch,
        orchestrator=orch,
        task_id="next-day",
        run_id="run-next",
        seat_id="seat-next",
        event_type="task_finished",
        sent_at=day + timedelta(days=1),
        payload={"ok": True},
    )

    token = _put_access_token(scratch)
    client, _ = _app_client(monkeypatch, scratch)
    r1 = client.get(
        f"/orchestrators/{orch}/seats/history?date=2026-07-04&limit=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200
    body1 = r1.json()
    assert [seat["task_id"] for seat in body1["seats"]] == ["day-seat-2", "day-seat-1"]
    assert body1["has_more"] is True
    assert body1["next_cursor"]

    decoded = _decode_history_cursor(body1["next_cursor"])
    assert decoded[0] == day + timedelta(days=1) - timedelta(microseconds=1)
    assert decoded[3] == day

    r2 = client.get(
        f"/orchestrators/{orch}/seats/history?cursor={body1['next_cursor']}&limit=2&date=2026-07-05",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert [seat["task_id"] for seat in body2["seats"]] == ["day-seat-0"]
    assert body2["has_more"] is False
    assert body2["next_cursor"] is None


def test_history_bad_date_returns_400(scratch, monkeypatch):
    token = _put_access_token(scratch)
    client, _ = _app_client(monkeypatch, scratch)
    r = client.get(
        "/orchestrators/orch-1/seats/history?date=2026-99-99",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 400
    assert r.json() == {"error": "invalid date"}


def test_history_legacy_cursor_via_http_still_works(scratch, monkeypatch):
    orch = f"orch-{uuid.uuid4().hex}"
    base = datetime(2026, 7, 4, tzinfo=timezone.utc)
    for idx in range(3):
        _seed_event(
            scratch,
            orchestrator=orch,
            task_id=f"legacy-{idx}",
            run_id=f"run-legacy-{idx}",
            seat_id=f"seat-legacy-{idx}",
            event_type="task_finished",
            sent_at=base + timedelta(minutes=idx),
            payload={"ok": True},
        )
    anchor = base + timedelta(days=1)
    legacy_cursor = base64.urlsafe_b64encode(
        json.dumps([anchor.isoformat(), (base + timedelta(minutes=1)).isoformat(), "legacy-1"]).encode()
    ).decode("ascii").rstrip("=")
    token = _put_access_token(scratch)
    client, _ = _app_client(monkeypatch, scratch)

    r = client.get(
        f"/orchestrators/{orch}/seats/history?cursor={legacy_cursor}&limit=5",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200
    assert [seat["task_id"] for seat in r.json()["seats"]] == ["legacy-0"]

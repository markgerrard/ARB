from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any, Callable

from redis.exceptions import RedisError

from .identity import Identity, cold_identity, has_done_marker, parse_marker
from .lifecycle import Lifecycle
from .mapper import DriftError, Event, map_line
from .offset import OffsetStore, offset_key
from ..bridge import build_eval_record
from ..tool_call_id import canonical_tool_call_id
from ..visibility_tee import live_tee, trace_tee


MODEL_EVENTS = {"model_text", "model_thinking"}
LIVE_AND_TRACE_EVENTS = {
    "task_started", "task_finished", "command_started", "command_output", "command_finished",
    "turn_started", "turn_completed",
}
LIVE_ONLY_EVENTS = {"task_continuing", "drift_error"}
TOOL_EDGE_EVENTS = {"command_started", "command_finished", "command_output"}
TURN_INDEXED_EVENTS = {"command_started", "command_finished", "command_output", "turn_started", "turn_completed"}
logger = logging.getLogger("agent_redis_bridge.claude_tail.tailer")


class _DriftThresholdExceeded(RuntimeError):
    pass


class TranscriptTailer:
    CLAUDE_TAIL_ATTEMPT_EPOCH = 1
    live_maxlen = 10000
    live_ttl = 7 * 24 * 60 * 60
    trace_maxlen = 10000
    drift_threshold = 10
    poll_budget_lines = 500
    poll_budget_secs = 2.0
    continuing_interval_s = 30.0

    def __init__(
        self,
        path: str,
        identity: Identity,
        offset_store: OffsetStore,
        live_redis: Any,
        trace_redis: Any,
        prefix: str,
        redactor: Callable[[str], str],
        eval_redis: Any | None = None,
        eval_stream: str | None = None,
        trace_prefix: str = "",
        cold_agent_id: str | None = None,
        cold_session_id: str | None = None,
        identity_locked: bool = False,
    ) -> None:
        self.path = path
        self.identity = identity
        self.offset_store = offset_store
        self.live_redis = live_redis
        self.trace_redis = trace_redis
        self.eval_redis = eval_redis
        self.eval_stream = eval_stream
        self.prefix = prefix
        self.trace_prefix = trace_prefix
        self.redactor = redactor
        self._cold_agent_id = cold_agent_id
        self._cold_session_id = cold_session_id
        self._identity_locked = identity_locked
        self._identity_resolved = identity_locked or cold_agent_id is None or cold_session_id is None
        self.lifecycle = Lifecycle()
        self.drift_count = 0
        self.turn_index = 0
        self.logical_turn_index = 0
        self._turn_open = False
        self._turn_started_ts: str | None = None
        self._turn_start_offset: int | None = None
        self._last_causal_ts: str | None = None
        self._turn_clock_ok = True
        self._turn_prev_ts: str | None = None
        self._cursor_inode: int | None = None
        self._cursor_offset: int | None = None
        self._seq = 0
        self._has_started = False
        self._finished_emitted = False
        self._last_live_at = 0.0
        self._first_user_seen = False
        self.first_user_marker: dict[str, str] | None = None
        self.completed = False
        self.at_eof = False
        self.progressed = False
        self.emit_failing = False
        self.skipped_lines = 0
        self.claude_tail_missing_ts = 0
        self._last_line_ts: str | None = None

    _TURN_STATE_FIELDS = (
        "logical_turn_index", "_turn_open", "_turn_started_ts", "_last_causal_ts",
        "_turn_clock_ok", "_turn_prev_ts", "_turn_start_offset",
    )

    def _snapshot_turn_state(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self._TURN_STATE_FIELDS}

    def _restore_turn_state(self, snap: dict[str, Any]) -> None:
        for field, value in snap.items():
            setattr(self, field, value)

    def _commit(self, key: str, offset: int, ordinal: int, st_ino: int) -> None:
        """v5: the SOLE writer of the persisted offset AND the in-memory continuity cursor. Binding
        them here makes clause-1 (cursor == store.offset) impossible to violate — no exit path can
        advance one without the other. `_cursor_offset`/`_cursor_inode` have NO OTHER assignment site
        (enforced by the sole-writer census, Task 8 Step 7c)."""
        self.offset_store.store(key, offset, ordinal)
        self._cursor_inode = st_ino
        self._cursor_offset = offset

    def poll(self) -> int:
        started_at = time.monotonic()
        stat = os.stat(self.path)
        st_ino = stat.st_ino
        key = offset_key(self.path, st_ino)
        pos = self.offset_store.load(key)
        offset = pos.offset
        self.logical_turn_index = 0 if offset == 0 else pos.turn_index
        if offset > stat.st_size:
            offset = 0
            self.logical_turn_index = 0
            self._commit(key, 0, 0, st_ino)
            if self._turn_open:
                self._turn_clock_ok = False
                self._turn_open = False
        if self._turn_open and (self._cursor_inode != st_ino or self._cursor_offset != offset):
            self._turn_clock_ok = False
            self._turn_open = False

        emitted = 0
        lines = 0
        new_offset = offset
        self.at_eof = False
        emit_failed = False
        committed = False
        poll_start_turn_state = self._snapshot_turn_state()
        try:
            with open(self.path, "rb") as fh:
                fh.seek(offset)
                while True:
                    if lines >= self.poll_budget_lines or (
                        lines > 0 and time.monotonic() - started_at >= self.poll_budget_secs
                    ):
                        break
                    line = fh.readline()
                    if not line or not line.endswith(b"\n"):
                        self.at_eof = True
                        break
                    line_start = new_offset
                    new_offset = fh.tell()
                    lines += 1
                    turn_state_at_line_start = self._snapshot_turn_state()
                    ordinal_at_line_start = turn_state_at_line_start["logical_turn_index"]
                    try:
                        events, obj, is_human_user = self._parse_line(line)
                    except DriftError as exc:
                        self._ensure_identity_resolved()
                        self.drift_count += 1
                        if self._turn_open:
                            self._turn_clock_ok = False
                        try:
                            self._emit_drift_error(exc)
                        except RedisError:
                            raise
                        except Exception:
                            self.emit_failing = True
                            self.drift_count -= 1
                            self._restore_turn_state(turn_state_at_line_start)
                            if line_start != offset:
                                self._commit(key, line_start, ordinal_at_line_start, st_ino)
                                committed = True
                            self.progressed = line_start != offset
                            raise
                        if self.drift_count > self.drift_threshold:
                            self._commit(key, new_offset, self.logical_turn_index, st_ino)
                            committed = True
                            self.progressed = True
                            raise _DriftThresholdExceeded("drift threshold exceeded") from exc
                        emitted += 1
                        continue
                    except RedisError:
                        raise
                    except Exception:
                        self.skipped_lines += 1
                        if self._turn_open:
                            self._turn_clock_ok = False
                        logger.warning("skipping unparseable claude transcript line",
                                       extra={"transcript_path": self.path, "line_offset": line_start})
                        continue
                    try:
                        emitted += self._emit_events(events, obj, is_human_user, line_start)
                    except RedisError:
                        raise
                    except Exception:
                        emit_failed = True
                        self.emit_failing = True
                        self._restore_turn_state(turn_state_at_line_start)
                        if line_start != offset:
                            self._commit(key, line_start, ordinal_at_line_start, st_ino)
                            committed = True
                        self.progressed = line_start != offset
                        raise
            if new_offset != offset:
                self._commit(key, new_offset, self.logical_turn_index, st_ino)
                committed = True
                self.progressed = True
            else:
                self.progressed = False
                if emitted == 0:
                    self._maybe_emit_continuing()
        except Exception:
            if not committed:
                self._restore_turn_state(poll_start_turn_state)
            raise
        if not emit_failed:
            self.emit_failing = False
        return emitted

    def finish(self, *, ok: bool = True) -> Event | None:
        if self._finished_emitted:
            return None
        self._finished_emitted = True
        event = self.lifecycle.finished(ok=ok)
        self._route_event(event)
        return event

    def _parse_line(self, line: bytes):
        obj = json.loads(line.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("transcript line is not a JSON object")
        self._capture_first_user_marker(obj)
        self._check_done_marker(obj)
        out_of_turn = bool(obj.get("isMeta") or obj.get("isSidechain"))
        is_human_user = obj.get("type") == "user" and bool(obj.get("promptId")) and not out_of_turn
        if not out_of_turn and not is_human_user and obj.get("type") in ("user", "assistant"):
            ts = obj.get("timestamp")
            if isinstance(ts, str) and ts:
                self._last_causal_ts = ts
        try:
            events = map_line(obj)
        except DriftError as exc:
            exc.obj = obj
            raise
        return events, obj, is_human_user

    def _emit_events(self, events, obj, is_human_user, line_start=None) -> int:
        self._ensure_identity_resolved()
        emitted = 0
        if is_human_user:
            emitted += self._close_and_open_turn(obj, line_start)
        elif self._turn_open and obj.get("type") in ("user", "assistant") and not (obj.get("isMeta") or obj.get("isSidechain")):
            self._observe_clock(obj)
        if events and obj.get("type") == "assistant":
            self.turn_index += 1
        line_ts = obj.get("timestamp")
        self._last_line_ts = line_ts
        line_uuid = obj.get("uuid")
        for event in events:
            self._stamp_event_ts(event, line_ts)
            self._stamp_uuid(event, line_uuid)
            started = self.lifecycle.started()
            if started is not None:
                self._has_started = True
                self._stamp_event_ts(started, line_ts)
                self._stamp_uuid(started, line_uuid)
                self._route_event(started)
                emitted += 1
            self._route_event(event)
            emitted += 1
        return emitted

    def _observe_clock(self, obj) -> None:
        ts = obj.get("timestamp")
        if not (isinstance(ts, str) and ts):
            self._turn_clock_ok = False
            return
        if self._turn_prev_ts is not None and ts < self._turn_prev_ts:
            self._turn_clock_ok = False
        self._turn_prev_ts = ts

    def _stamp_event_ts(self, event, line_ts) -> None:
        data = event.setdefault("data", {})
        if isinstance(line_ts, str) and line_ts:
            data["event_ts"] = line_ts
        elif event["event_type"] in LIVE_AND_TRACE_EVENTS or event["event_type"] in LIVE_ONLY_EVENTS:
            self.claude_tail_missing_ts += 1

    def _stamp_uuid(self, event, line_uuid) -> None:
        if isinstance(line_uuid, str) and line_uuid:
            event.setdefault("data", {})["uuid"] = line_uuid

    def _close_and_open_turn(self, obj, line_start=None) -> int:
        emitted = 0
        line_ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
        if self._turn_open:
            completed = {"event_type": "turn_completed", "data": {
                "turn_completed": True,
                "turn_clock_monotonic": bool(self._turn_clock_ok),
            }}
            if self._last_causal_ts:
                completed["data"]["event_ts"] = self._last_causal_ts
            else:
                self.claude_tail_missing_ts += 1
            self._route_event(completed)
            emitted += 1
        self.logical_turn_index += 1
        self._turn_open = True
        self._turn_started_ts = line_ts
        self._turn_start_offset = line_start if isinstance(line_start, int) else None
        self._last_causal_ts = line_ts
        self._turn_clock_ok = bool(line_ts)
        self._turn_prev_ts = line_ts
        started = {"event_type": "turn_started", "data": {}}
        if line_ts:
            started["data"]["turn_started_ts"] = line_ts
            started["data"]["event_ts"] = line_ts
        else:
            self.claude_tail_missing_ts += 1
        self._route_event(started)
        emitted += 1
        return emitted

    def _capture_first_user_marker(self, obj: dict[str, Any]) -> None:
        if self._first_user_seen or obj.get("type") != "user":
            return
        self._first_user_seen = True
        content = (obj.get("message") or {}).get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    text = block["text"]
                    break
        self.first_user_marker = parse_marker(text) if text else None
        if text and self._is_cold():
            self._resolve_cold_identity(text)

    def _check_done_marker(self, obj: dict[str, Any]) -> None:
        if self.completed or not self._is_cold() or obj.get("type") != "assistant":
            return
        content = (obj.get("message") or {}).get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(block["text"] for block in content
                         if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
        if any(has_done_marker(text) for text in texts):
            self.completed = True

    def _is_cold(self) -> bool:
        return self._cold_agent_id is not None and self._cold_session_id is not None

    def _resolve_cold_identity(self, marker_text: str) -> None:
        if self._identity_locked:
            if self.first_user_marker and self.first_user_marker.get("run_id"):
                self.identity = replace(self.identity, run_id=self.first_user_marker["run_id"])
            return
        if self._cold_agent_id is None or self._cold_session_id is None:
            return
        self.identity = cold_identity(self._cold_agent_id, self._cold_session_id, marker_text)
        self._identity_resolved = True

    def _ensure_identity_resolved(self) -> None:
        if not self._identity_resolved:
            self._resolve_cold_identity("")

    def _emit_drift_error(self, exc: DriftError) -> None:
        event = {"event_type": "drift_error", "data": {"message": str(exc), "count": self.drift_count, "kind": "drift_error"}}
        line_ts = getattr(exc, "obj", {}).get("timestamp") if isinstance(getattr(exc, "obj", None), dict) else None
        if isinstance(line_ts, str) and line_ts:
            event["data"]["event_ts"] = line_ts
        self._route_event(event)

    def _maybe_emit_continuing(self) -> int:
        if not self._has_started or not self.identity.run_id:
            return 0
        if time.monotonic() - self._last_live_at < self.continuing_interval_s:
            return 0
        data = {"task_id": self.identity.task_id, "kind": "task_continuing"}
        if isinstance(self._last_line_ts, str) and self._last_line_ts:
            data["event_ts"] = self._last_line_ts
        self._emit_live({"event_type": "task_continuing", "data": data})
        return 1

    def _route_event(self, event: Event) -> None:
        event_type = event["event_type"]
        data = self._redact_value(dict(event.get("data") or {}))
        routed = {"event_type": event_type, "data": self._enrich_data(event_type, data)}
        if event_type in MODEL_EVENTS:
            self._emit_trace(routed)
        elif event_type in LIVE_AND_TRACE_EVENTS:
            self._emit_live(routed)
            self._emit_trace(routed)
            self._emit_eval(routed)
        elif event_type in LIVE_ONLY_EVENTS:
            self._emit_live(routed)
            self._emit_eval(routed)
        else:
            self._emit_trace(routed)

    def _enrich_data(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        data.setdefault("kind", event_type)
        data.setdefault("seq", self._seq)
        data.setdefault("item_id", self._item_id(event_type, data))
        data.setdefault("attempt_epoch", self.CLAUDE_TAIL_ATTEMPT_EPOCH)
        if event_type in TOOL_EDGE_EVENTS:
            cid = canonical_tool_call_id(data)
            if cid:
                data.setdefault("tool_call_id", cid)
        return data

    def _item_id(self, event_type: str, data: dict[str, Any]) -> str:
        tool_use_id = data.get("tool_use_id")
        if isinstance(tool_use_id, str) and tool_use_id:
            return f"{tool_use_id}:{event_type}"
        return f"{self.identity.task_id}:{self.turn_index}:{self._seq}:{event_type}"

    def _emit_live(self, event: Event) -> None:
        live_tee(self.live_redis, self.prefix, run_id=self.identity.run_id, task_id=self.identity.task_id,
                 seat_id=self.identity.seat_id, orchestrator=self.identity.orchestrator, event_type=event["event_type"],
                 sent_at=_now(), data=event["data"], maxlen=self.live_maxlen, ttl=self.live_ttl)
        self._last_live_at = time.monotonic()

    def _emit_trace(self, event: Event) -> None:
        trace_tee(self.trace_redis, self.trace_prefix, run_id=self.identity.run_id, task_id=self.identity.task_id,
                  seat_id=self.identity.seat_id, orchestrator=self.identity.orchestrator, event=event["event_type"],
                  turn_index=self.turn_index, data=event["data"], seq=int(event["data"].get("seq") or 0),
                  maxlen=self.trace_maxlen, redactor=self.redactor)

    def _emit_eval(self, event: Event) -> None:
        if self.eval_redis is None or not self.eval_stream:
            return
        data = event["data"]
        if event["event_type"] in TURN_INDEXED_EVENTS:
            data = dict(data)
            data["turn_index"] = self.logical_turn_index
        record = build_eval_record(run_id=self.identity.run_id, task_id=self.identity.task_id,
                                   seat_id=self.identity.seat_id, orchestrator=self.identity.orchestrator,
                                   event=event["event_type"], sent_at=_now(), data=data)
        if record is None:
            return
        try:
            self.eval_redis.xadd(self.eval_stream, record)
        except Exception:
            logger.exception("Claude tail eval tee failed for task %s event %s", self.identity.task_id, event["event_type"])

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redactor(value)
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
        return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

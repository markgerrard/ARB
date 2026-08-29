from __future__ import annotations

from dataclasses import dataclass, replace
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable

from redis.exceptions import RedisError

from .identity import Identity, cold_identity, warm_identity
from .offset import OffsetStore
from .tailer import TranscriptTailer
from ..bridge import build_eval_record, resolve_eval_redis
from ..redact import redact
from .finality import FinalityManager


IDLE_FINISH_SECS = 300.0
DEFAULT_REGISTRY_KEY = "claude:registry"
DEFAULT_COLD_DIR = "~/.claude/tasks"
DEFAULT_COLD_MAX_AGE_SECS = 6 * 60 * 60
logger = logging.getLogger("agent_redis_bridge.claude_tail.service")


def _utc_iso(epoch_seconds: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


@dataclass
class _TailerSpec:
    key: str
    path: str
    identity: Identity
    cold_agent_id: str | None = None
    cold_session_id: str | None = None
    identity_locked: bool = False
    draining: bool = False


@dataclass
class _TailerState:
    tailer: TranscriptTailer
    last_activity: float
    finished: bool = False
    finish_size: int = -1  # file size when idle-finished; resume if the file grows beyond it
    failing: bool = False


class Service:
    def __init__(
        self,
        *,
        redis: Any,
        prefix: str,
        live_redis: Any | None = None,
        trace_redis: Any | None = None,
        eval_redis: Any | None = None,
        eval_stream: str | None = None,
        registry_path: str | None = None,
        registry_key: str = DEFAULT_REGISTRY_KEY,
        cold_dir: str = DEFAULT_COLD_DIR,
        cold_max_age_secs: float = DEFAULT_COLD_MAX_AGE_SECS,
        trace_prefix: str = "",
        offset_store: OffsetStore | None = None,
        redactor: Callable[[str], str] = redact,
        tailer_cls: type[TranscriptTailer] = TranscriptTailer,
        time_func: Callable[[], float] = time.monotonic,
        wall_time_func: Callable[[], float] = time.time,
        idle_finish_secs: float = IDLE_FINISH_SECS,
        tick_deadline_secs: float = 30.0,
        draining_ttl_secs: int = 604800,
        heartbeat_label: str | None = None,
        stale_after_s: int = 330,
        finality_manager: FinalityManager | None = None,
        fd_probe: Callable[[int], list] | None = None,
        finality_horizon_secs: float = 900.0,
        finality_abandon_secs: float = 300.0,
    ) -> None:
        self.redis = redis
        self.live_redis = live_redis or redis
        self.trace_redis = trace_redis or redis
        self.eval_redis = eval_redis
        self.eval_stream = eval_stream
        self.prefix = prefix
        self.trace_prefix = trace_prefix
        self.registry_path = registry_path
        self.registry_key = registry_key
        self.cold_dir = Path(cold_dir).expanduser()
        self.cold_max_age_secs = cold_max_age_secs
        self.offset_store = offset_store or OffsetStore(redis, prefix)
        self.redactor = redactor
        self.tailer_cls = tailer_cls
        self.time_func = time_func
        self.wall_time_func = wall_time_func
        self.idle_finish_secs = idle_finish_secs
        self._tailers: dict[str, _TailerState] = {}
        self.tick_deadline_secs = tick_deadline_secs
        self.draining_ttl_secs = draining_ttl_secs
        self.heartbeat_label = heartbeat_label
        self.stale_after_s = stale_after_s
        self._last_heartbeat_at = float("-inf")
        self._last_heartbeat_fields: dict | None = None
        self._started_at_iso = _utc_iso(self.wall_time_func())
        self._last_emit_wall: float | None = None
        self._skipped_lines_retired = 0
        self._cursor: str | None = None
        self.finality = finality_manager or FinalityManager(
            redis,
            prefix,
            fd_probe=fd_probe,
            emit=self._emit_finality,
            horizon_secs=finality_horizon_secs,
            abandon_secs=finality_abandon_secs,
            time_func=self.time_func,
            wall_time_func=self.wall_time_func,
        )

    def tick(self) -> int:
        self._tick_heartbeat()
        self.finality.rearm()
        now = self.time_func()
        specs = self._discover_specs()
        live_keys = set(specs)

        for key in list(self._tailers):
            state = self._tailers[key]
            if key in live_keys:
                continue
            if self.finality.is_held(key):
                continue
            if key.startswith("warm:") and not state.finished:
                # Deregister observed mid-life: persist the draining record
                # (fallback) and keep polling — next tick rediscovers via the
                # record (spec §A Finish paths, warm deregister).
                self._persist_draining_record(key, state)
                live_keys.add(key)
                specs[key] = _TailerSpec(key=key, path=state.tailer.path, identity=state.tailer.identity, draining=True)
                continue
            self._finish_once(state)
            del self._tailers[key]

        for key, spec in specs.items():
            existing = self._tailers.get(key)
            if existing is None or self._resumed_after_finish(existing, spec):
                state = _TailerState(self._new_tailer(spec), now)
                self._tailers[key] = state
                if key.startswith("cold:") and self._cold_seat_completed(state):
                    sidecar_path = self._cold_sidecar_path(state)
                    if sidecar_path is not None:
                        self.finality.startup_renominate(key, state.tailer, str(sidecar_path))

        polled = 0
        order = sorted(live_keys)
        if self._cursor in order:
            i = order.index(self._cursor)
            order = order[i:] + order[:i]
        deadline = self.time_func() + self.tick_deadline_secs
        self._cursor = None
        for key in order:
            if self.time_func() >= deadline:
                # Shared tick deadline (spec §A): resume HERE next tick.
                self._cursor = key
                break
            state = self._tailers.get(key)
            if state is None or state.finished:
                continue
            try:
                emitted = state.tailer.poll()
            except FileNotFoundError:
                logger.warning("claude transcript vanished; finishing tailer", extra={"tailer_key": key})
                self._finish_once(state)
                self._delete_draining_record(key)
                del self._tailers[key]
                continue
            except RedisError:
                raise  # infra crashes the process (spec §A)
            except OSError:
                # Non-ENOENT filesystem failure (PermissionError, EIO):
                # sticky-failing, visible via the heartbeat, no offset move
                # (spec §A, panel r3 codex P1).
                logger.exception("claude transcript tailer filesystem failure", extra={"tailer_key": key})
                state.failing = True
                continue
            except Exception:
                # Emit-stage propagation (prefix already committed by the
                # tailer) or another per-tailer bug: sticky-failing (spec §A).
                logger.exception("claude transcript tailer failed", extra={"tailer_key": key})
                state.failing = True
                continue
            state.failing = bool(getattr(state.tailer, "emit_failing", False))
            polled += 1
            if emitted > 0:
                self._last_emit_wall = self.wall_time_func()
            now = self.time_func()
            at_eof = bool(getattr(state.tailer, "at_eof", True))
            if emitted or getattr(state.tailer, "progressed", False):
                # Offset progress IS activity (spec §A Finish paths, panel r3
                # agy P1). Synthetic task_continuing does not reach here —
                # poll() returns it as emitted only when nothing was read, and
                # then progressed is False and emitted counts it... see test
                # test_idle_finish_activity_sources for the pinned rule.
                state.last_activity = now
            spec = specs.get(key)
            if spec is not None and spec.draining:
                self._refresh_draining_ttl(key)
                if at_eof:
                    self._finish_once(state)
                    self._delete_draining_record(key)
                    del self._tailers[key]
                    continue
            if getattr(state.tailer, "completed", False):
                if at_eof:
                    self._finish_once(state)
                continue
            if key.startswith("cold:") and self._cold_seat_completed(state):
                if at_eof:
                    sidecar_path = self._cold_sidecar_path(state)
                    if sidecar_path is not None and self.finality.nominate(
                        key, state.tailer, str(sidecar_path), now=now
                    ):
                        continue
                    self._finish_once(state)
                    if not self.finality.is_held(key):
                        self._delete_cold_seat_files(state)
                continue
            if (
                key.startswith("cold:")
                and at_eof
                and now - state.last_activity >= self.idle_finish_secs
            ):
                self._finish_once(state)
        self.finality.tick(now=now)
        self._tick_heartbeat(end=True)
        return polled

    def _tick_heartbeat(self, *, end: bool = False) -> None:
        if not self.heartbeat_label:
            return
        now = self.time_func()
        live = [s for s in self._tailers.values() if not s.finished]
        fields = {
            "tailers": len(live),
            "failing_tailers": sum(1 for s in live if getattr(s, "failing", False)),
            # Monotonic per-process (spec §C; plan panel, agy P1): retired
            # tailers' counts are accumulated in _finish_once, so the sum
            # never decreases when a tailer finishes.
            "skipped_lines": self._skipped_lines_retired
            + sum(int(getattr(s.tailer, "skipped_lines", 0)) for s in live),
            "last_emit_at": _utc_iso(self._last_emit_wall) if self._last_emit_wall is not None else None,
        }
        # Throttle to one write per 10s — EXCEPT an end-beat whose state
        # changed since the last write: the start-beat runs before this
        # tick's polls, so without this exemption a state change would stay
        # invisible for a full extra throttle window and the persisted
        # payload would be stale-at-write (plan panel, agy P0-2 + grok P1 +
        # cold-Opus P1-2). Bounded by real state changes, not tick rate.
        changed = fields != self._last_heartbeat_fields
        if now - self._last_heartbeat_at < 10.0 and not (end and changed):
            return
        payload = json.dumps(
            {
                "ts": _utc_iso(self.wall_time_func()),
                "pid": os.getpid(),
                "started_at": self._started_at_iso,
                **fields,
                "stale_after_s": int(self.stale_after_s),
            },
            separators=(",", ":"),
        )
        # RedisError propagates — heartbeat write failure is infra (spec §C).
        self.live_redis.set(f"{self.prefix}tail:heartbeat:{self.heartbeat_label}", payload, ex=604800)
        self._last_heartbeat_at = now
        self._last_heartbeat_fields = fields

    def _discover_specs(self) -> dict[str, _TailerSpec]:
        specs: dict[str, _TailerSpec] = {}
        for record in self._read_registry():
            session_id = str(record.get("session_id") or "")
            transcript_path = str(record.get("transcript_path") or "")
            if not session_id or not transcript_path:
                continue
            transcript_exists = self._warm_transcript_exists(transcript_path)
            if transcript_exists is not True:
                if transcript_exists is False:
                    self._prune_missing_warm_registry_record(record)
                continue
            identity = _warm_identity_from_record(record)
            specs[f"warm:{session_id}"] = _TailerSpec(
                key=f"warm:{session_id}",
                path=transcript_path,
                identity=identity,
            )

        for dkey in list(self.redis.scan_iter(match=f"{self.prefix}claude:draining:*")):
            if isinstance(dkey, bytes):
                dkey = dkey.decode()
            session_id = dkey.removeprefix(f"{self.prefix}claude:draining:")
            wkey = f"warm:{session_id}"
            if wkey in specs:
                # Flap supersede (spec §A): the live registry record wins;
                # drop the draining record without a finish.
                self.redis.delete(dkey)
                continue
            raw = self.redis.get(dkey)
            if raw is None:
                continue
            try:
                record = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                logger.warning("dropping malformed draining record", extra={"session_id": session_id})
                self.redis.delete(dkey)
                continue
            transcript_path = str(record.get("transcript_path") or "")
            exists = self._warm_transcript_exists(transcript_path)
            if exists is False:
                # Prune (spec §A record lifecycle): the FileNotFound finish of
                # a live tailer deletes the record; this arm covers the
                # restart case where no tailer exists — delete silently.
                if wkey not in self._tailers:
                    self.redis.delete(dkey)
                    continue
                # a live tailer will hit FileNotFoundError and finish+delete
            elif exists is None:
                continue  # transient stat failure: keep the record, retry next
                # tick. NOTE (documented residual, plan panel codex #3): a
                # PERSISTENT stat failure at discovery leaves the session dark
                # with only the existing warning log + the tailers-count
                # discriminator — the spec's coverage table explicitly accepts
                # discovery-level darkness under that discriminator; do not
                # add heartbeat schema fields for it.
            identity = _warm_identity_from_record(record)
            specs[wkey] = _TailerSpec(key=wkey, path=transcript_path, identity=identity, draining=True)

        if self.cold_dir.exists():
            for path in sorted(self.cold_dir.glob("*.output")):
                if not self._is_recent(path):
                    continue
                agent_id = path.name.removesuffix(".output")
                if not agent_id:
                    continue
                session_id = agent_id
                identity = cold_identity(
                    agent_id,
                    session_id,
                    "",
                    project=os.environ.get("ARB_CLAUDE_TAIL_PROJECT", ""),
                    workspace=os.environ.get("ARB_CLAUDE_TAIL_WORKSPACE", ""),
                )
                identity_locked = False
                sidecar = self._read_cold_sidecar(path.parent / f"{agent_id}.arb-tail.json")
                if sidecar is not None:
                    orchestrator = sidecar.get("orchestrator")
                    if isinstance(orchestrator, str):
                        identity = replace(identity, orchestrator=orchestrator)
                    identity_locked = True
                specs[f"cold:{path}"] = _TailerSpec(
                    key=f"cold:{path}",
                    path=str(path),
                    identity=identity,
                    cold_agent_id=agent_id,
                    cold_session_id=session_id,
                    identity_locked=identity_locked,
                )
        return specs

    def _draining_key(self, session_id: str) -> str:
        return f"{self.prefix}claude:draining:{session_id}"

    def _persist_draining_record(self, key: str, state: _TailerState) -> None:
        # Fallback path — the SessionEnd hook normally wrote this already
        # (spec §A, panel r6 codex P1). UNCONDITIONAL: never gated on the
        # previous tick's at_eof (spec §A, panel r5 cold-Opus P2-1).
        session_id = key.removeprefix("warm:")
        ident = state.tailer.identity
        record = {
            "session_id": session_id,
            "transcript_path": state.tailer.path,
            "seat_id": getattr(ident, "seat_id", "") or "",
            "run_id": getattr(ident, "run_id", "") or "",
        }
        self.redis.set(self._draining_key(session_id), json.dumps(record, separators=(",", ":")), ex=self.draining_ttl_secs)

    def _delete_draining_record(self, key: str) -> None:
        if key.startswith("warm:"):
            self.redis.delete(self._draining_key(key.removeprefix("warm:")))

    def _refresh_draining_ttl(self, key: str) -> None:
        session_id = key.removeprefix("warm:")
        raw = self.redis.get(self._draining_key(session_id))
        if raw is not None:
            self.redis.set(self._draining_key(session_id), raw, ex=self.draining_ttl_secs)

    @staticmethod
    def _read_cold_sidecar(path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _read_registry(self) -> list[dict[str, Any]]:
        raw: str | bytes | None
        if self.registry_path:
            path = Path(self.registry_path)
            if not path.exists():
                return []
            raw = path.read_text(encoding="utf-8")
            return _registry_records_from_json(raw)
        hash_records = self._read_redis_hash_registry()
        if hash_records:
            return hash_records
        raw = self.redis.get(f"{self.prefix}{self.registry_key}") if hasattr(self.redis, "get") else None
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode()
        return _registry_records_from_json(raw)

    def _read_redis_hash_registry(self) -> list[dict[str, Any]]:
        if not hasattr(self.redis, "hgetall"):
            return []
        fields = self.redis.hgetall(f"{self.prefix}{self.registry_key}") or {}
        records: list[dict[str, Any]] = []
        for field, raw in fields.items():
            if isinstance(field, bytes):
                field = field.decode()
            if isinstance(raw, bytes):
                raw = raw.decode()
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                logger.warning("ignoring malformed Claude tail registry field", extra={"session_id": str(field)})
                continue
            if isinstance(data, dict):
                data = dict(data)
                data.setdefault("session_id", str(field))
                data["_registry_field"] = str(field)
                records.append(data)
        return records

    def _warm_transcript_exists(self, transcript_path: str) -> bool | None:
        try:
            os.stat(transcript_path)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            logger.warning("could not check Claude transcript path", extra={"transcript_path": transcript_path})
            return None

    def _prune_missing_warm_registry_record(self, record: dict[str, Any]) -> None:
        if self.registry_path:
            return
        field = record.get("_registry_field")
        if not field or not hasattr(self.redis, "hdel"):
            return
        try:
            self.redis.hdel(f"{self.prefix}{self.registry_key}", field)
        except RedisError:
            raise
        except Exception:
            logger.warning("failed to prune stale Claude registry field", extra={"session_id": str(field)})

    def _new_tailer(self, spec: _TailerSpec) -> TranscriptTailer:
        return self.tailer_cls(
            spec.path,
            spec.identity,
            self.offset_store,
            live_redis=self.live_redis,
            trace_redis=self.trace_redis,
            prefix=self.prefix,
            redactor=self.redactor,
            eval_redis=self.eval_redis,
            eval_stream=self.eval_stream,
            trace_prefix=self.trace_prefix,
            cold_agent_id=spec.cold_agent_id,
            cold_session_id=spec.cold_session_id,
            identity_locked=spec.identity_locked,
        )

    def _finish_once(self, state: _TailerState) -> None:
        if state.finished:
            return
        state.tailer.finish(ok=True)
        self._skipped_lines_retired += int(getattr(state.tailer, "skipped_lines", 0))
        state.finished = True
        try:
            state.finish_size = os.stat(state.tailer.path).st_size
        except OSError:
            state.finish_size = -1

    @staticmethod
    def _cold_seat_completed(state: _TailerState) -> bool:
        sidecar_path = Service._cold_sidecar_path(state)
        if sidecar_path is None:
            return False
        data = Service._read_cold_sidecar(sidecar_path)
        return bool(data is not None and data.get("completed") is True)

    @staticmethod
    def _cold_sidecar_path(state: _TailerState) -> Path | None:
        output_path = Path(state.tailer.path)
        if output_path.suffix != ".output":
            return None
        return output_path.parent / f"{output_path.stem}.arb-tail.json"

    def _delete_cold_seat_files(self, state: _TailerState) -> None:
        if self.finality.is_held(self._tailer_key_for_state(state)):
            return
        output_path = Path(state.tailer.path)
        sidecar_path = self._cold_sidecar_path(state)
        for target in (output_path, sidecar_path):
            if target is None:
                continue
            try:
                target.unlink()
            except OSError:
                pass

    def _tailer_key_for_state(self, state: _TailerState) -> str:
        for key, candidate in self._tailers.items():
            if candidate is state:
                return key
        return ""

    def _emit_finality(self, event_type: str, record: dict[str, Any]) -> None:
        if self.eval_redis is None or not self.eval_stream:
            return
        data = {
            "turn_index": int(record["turn_index"]),
            "attempt_epoch": 1,
            "event_ts": record.get("event_ts"),
            "turn_started_ts": record.get("turn_started_ts"),
        }
        if event_type == "turn_finalized":
            data.update({
                "finality_evidence": "fd_quiescence",
                "observed_inode": int(record["target_inode"]),
                "observed_size": int(record["observed_size"]),
            })
        else:
            data["finality_evidence"] = "retracted"
        identity = record
        event = build_eval_record(
            run_id=identity["run_id"], task_id=identity["task_id"],
            seat_id=identity.get("seat_id"), orchestrator=identity.get("orchestrator"),
            event=event_type, sent_at=_utc_iso(self.wall_time_func()), data=data,
        )
        if event is not None:
            self.eval_redis.xadd(self.eval_stream, event)

    def _resumed_after_finish(self, state: _TailerState, spec: "_TailerSpec") -> bool:
        # A finished seat whose file has grown past the finish point is alive again (e.g. a
        # cold-Opus that idle-finished mid-think and is now writing its report). finish_size < 0
        # means we never recorded a size (finish-on-deregister); those are not resumed here.
        if not state.finished or state.finish_size < 0:
            return False
        try:
            return os.stat(spec.path).st_size > state.finish_size
        except OSError:
            return False

    def _is_recent(self, path: Path) -> bool:
        try:
            age = self.wall_time_func() - path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age <= self.cold_max_age_secs


def _registry_records_from_json(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning("ignoring malformed Claude tail registry")
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        sessions = data.get("sessions")
        if isinstance(sessions, list):
            return [item for item in sessions if isinstance(item, dict)]
        return [dict(value, session_id=key) for key, value in data.items() if isinstance(value, dict)]
    return []


def _warm_identity_from_record(record: dict[str, Any]) -> Identity:
    session_id = str(record.get("session_id") or "")
    project = record.get("project")
    workspace = record.get("workspace")
    if isinstance(project, str) and project and isinstance(workspace, str) and workspace:
        return warm_identity(session_id, project, workspace)
    seat_id = str(record.get("seat_id") or f"claude-{session_id}")
    run_id = str(record.get("run_id") or session_id)
    return Identity(run_id=run_id, task_id=session_id, seat_id=seat_id, orchestrator=seat_id)


def build_service_from_env(**overrides) -> Service:
    redis_client = _agent_redis_from_env()
    live_redis, trace_redis = _live_trace_redis_from_env(redis_client)
    eval_redis, eval_stream = _eval_redis_from_env()
    prefix = os.environ.get("AGENT_REDIS_PREFIX", "agent_scratch:")
    trace_prefix = os.environ.get("ARB_TRACE_PREFIX", "")
    TranscriptTailer.poll_budget_lines = int(os.environ.get("ARB_CLAUDE_TAIL_POLL_BUDGET_LINES", str(TranscriptTailer.poll_budget_lines)))
    TranscriptTailer.poll_budget_secs = float(os.environ.get("ARB_CLAUDE_TAIL_POLL_BUDGET_SECS", str(TranscriptTailer.poll_budget_secs)))
    kwargs = dict(
        redis=redis_client,
        live_redis=live_redis,
        trace_redis=trace_redis,
        eval_redis=eval_redis,
        eval_stream=eval_stream,
        prefix=prefix,
        trace_prefix=trace_prefix,
        registry_path=os.environ.get("ARB_CLAUDE_TAIL_REGISTRY_PATH"),
        registry_key=os.environ.get("ARB_CLAUDE_TAIL_REGISTRY_KEY", DEFAULT_REGISTRY_KEY),
        cold_dir=os.environ.get("ARB_CLAUDE_TAIL_COLD_DIR", DEFAULT_COLD_DIR),
        cold_max_age_secs=float(os.environ.get("ARB_CLAUDE_TAIL_MAX_AGE_SECS", str(DEFAULT_COLD_MAX_AGE_SECS))),
        idle_finish_secs=float(os.environ.get("ARB_CLAUDE_TAIL_IDLE_FINISH_SECS", str(IDLE_FINISH_SECS))),
        finality_horizon_secs=float(os.environ.get("ARB_TAIL_FINALITY_HORIZON_SECS", "900")),
        finality_abandon_secs=float(os.environ.get("ARB_TAIL_FINALITY_ABANDON_SECS", "300")),
        heartbeat_label=os.environ.get("ARB_CLAUDE_TAIL_HEARTBEAT_LABEL") or None,
        tick_deadline_secs=float(os.environ.get("ARB_CLAUDE_TAIL_TICK_DEADLINE_SECS", "30")),
    )
    kwargs.update(overrides)
    return Service(**kwargs)


def _live_trace_redis_from_env(fallback):
    live_url = os.environ.get("ARB_LIVE_REDIS_URL")
    trace_url = os.environ.get("ARB_TRACE_REDIS_URL")
    if live_url and trace_url:
        return _redis_from_url(live_url), _redis_from_url(trace_url)
    if live_url:
        logger.warning("ARB_TRACE_REDIS_URL is unset; using ARB_LIVE_REDIS_URL for both Claude live and trace sinks")
        client = _redis_from_url(live_url)
        return client, client
    if trace_url:
        logger.warning("ARB_LIVE_REDIS_URL is unset; using ARB_TRACE_REDIS_URL for both Claude live and trace sinks")
        client = _redis_from_url(trace_url)
        return client, client
    return fallback, fallback


def _eval_redis_from_env():
    eval_url, eval_db, eval_prefix = resolve_eval_redis({})
    if not eval_url:
        logger.warning("ARB_EVAL_REDIS_URL is unset; Claude tail eval tee is disabled")
        return None, None
    return _redis_from_url(eval_url, db=eval_db), f"{eval_prefix}eval:events"


def _agent_redis_from_env():
    url = os.environ.get("AGENT_REDIS_URL")
    if url:
        return _redis_from_url(url)
    return _redis_from_parts(
        host=os.environ["AGENT_REDIS_HOST"],
        port=int(os.environ["AGENT_REDIS_PORT"]),
        db=int(os.environ["AGENT_REDIS_DB"]),
        username=os.environ.get("AGENT_REDIS_USER") or None,
        password=os.environ.get("AGENT_REDIS_PASSWORD") or None,
        ssl=os.environ.get("AGENT_REDIS_TLS", "0") in {"1", "true", "True", "yes"},
    )


def _redis_from_url(url: str, **kwargs):
    import redis

    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_keepalive=True,
        health_check_interval=30,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
        **kwargs,
    )


def _redis_from_parts(**kwargs):
    import redis

    return redis.Redis(
        decode_responses=True,
        socket_keepalive=True,
        health_check_interval=30,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
        **kwargs,
    )

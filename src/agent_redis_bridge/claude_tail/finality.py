"""Durable terminal-turn finalization for cold Claude tail sessions.

The module deliberately receives raw filesystem observations (stat results and
fd lists).  The zero-write-open decision, digest verification, durable
watch-before-emit ordering, and re-arm policy live here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, NamedTuple


WATCH_PREFIX = "claude:finality:watch:"
RETRACTED_TTL = 7 * 24 * 60 * 60
DEFAULT_HORIZON_SECS = 900.0
DEFAULT_ABANDON_SECS = 300.0


class FdInfo(NamedTuple):
    fd: int
    mode: str = ""
    inode: int | None = None
    path: str | None = None


class CandidateState(str, Enum):
    NOMINATING = "nominating"
    QUIESCING = "quiescing"
    CONFIRMING = "confirming"
    WATCHED = "watched"


class FinalityWatchStore:
    def __init__(self, redis: Any, prefix: str = "") -> None:
        self.redis = redis
        self.prefix = prefix

    def key(self, target_path: str, target_inode: int, turn_index: int) -> str:
        return (
            f"{self.prefix}{WATCH_PREFIX}{target_path}|{int(target_inode)}:{int(turn_index)}"
        )

    def put(self, record: dict[str, Any], *, ex: int | None = None) -> str:
        key = self.key(record["target_path"], record["target_inode"], record["turn_index"])
        self.redis.set(key, json.dumps(record, separators=(",", ":")), ex=ex)
        return key

    def get(self, key: str) -> dict[str, Any] | None:
        raw = self.redis.get(key)
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def delete(self, key: str) -> None:
        self.redis.delete(key)

    def mark_retracted(self, record: dict[str, Any]) -> str:
        record = dict(record)
        record["status"] = "retracted"
        return self.put(record, ex=RETRACTED_TTL)

    def scan(self) -> Iterable[tuple[str, dict[str, Any]]]:
        match = f"{self.prefix}{WATCH_PREFIX}*"
        for key in self.redis.scan_iter(match=match):
            if isinstance(key, bytes):
                key = key.decode()
            record = self.get(key)
            if record is not None:
                yield key, record


def _default_fd_probe(inode: int) -> list[FdInfo]:
    try:
        completed = subprocess.run(
            ["lsof", "-nP", "-F", "fian", "-a", "-d", "0-2147483647"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        warning_noise = all(
            line.lower().startswith(("lsof: warning", "lsof: can't stat"))
            for line in completed.stderr.splitlines()
            if line.strip()
        )
        if completed.returncode not in (0, 1) or (
            completed.returncode == 1 and completed.stderr and not warning_noise
        ):
            raise RuntimeError("fd probe failed")
        output = completed.stdout
    except FileNotFoundError:
        if sys.platform.startswith("linux"):
            return _proc_fd_probe(inode)
        raise RuntimeError("fd probe failed")
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("fd probe failed")
    current: dict[str, Any] = {}
    result: list[FdInfo] = []
    for line in output.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "f":
            if current.get("inode") == inode and current.get("mode"):
                result.append(FdInfo(current.get("fd", -1), current["mode"], inode))
            current = {"fd": int(value) if value.isdigit() else -1}
        elif tag == "i":
            try:
                current["inode"] = int(value)
            except ValueError:
                current["inode"] = None
        elif tag == "a":
            current["mode"] = value
    if current.get("inode") == inode and current.get("mode"):
        result.append(FdInfo(current.get("fd", -1), current["mode"], inode))
    return result


def _proc_fd_probe(inode: int) -> list[FdInfo]:
    result: list[FdInfo] = []
    try:
        proc_entries = os.listdir("/proc")
    except OSError as exc:
        raise RuntimeError("fd probe failed") from exc
    for pid in proc_entries:
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError as exc:
            raise RuntimeError("fd probe failed") from exc
        for fd in fds:
            if not fd.isdigit():
                continue
            fd_path = f"{fd_dir}/{fd}"
            try:
                if os.stat(fd_path).st_ino != inode:
                    continue
                flags = 0
                for line in Path(f"/proc/{pid}/fdinfo/{fd}").read_text().splitlines():
                    if line.startswith("flags:"):
                        flags = int(line.split()[1], 8)
                        break
                access = flags & 0o3
                mode = "r" if access == 0 else "w" if access == 1 else "u"
                result.append(FdInfo(int(fd), mode, inode))
            except OSError as exc:
                raise RuntimeError("fd probe failed") from exc
            except ValueError:
                continue
    return result


def _has_write_open(fds: Iterable[FdInfo]) -> bool:
    for fd in fds:
        if isinstance(fd, dict):
            mode = fd.get("mode", "")
        else:
            mode = getattr(fd, "mode", "")
        if mode and any(flag in str(mode).lower() for flag in ("w", "u", "a")):
            return True
    return False


def _digest(path: str, start: int, end: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        handle.seek(start)
        remaining = end - start
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    if remaining:
        raise ValueError("target shrank during digest")
    return digest.hexdigest()


def _clean_eof(path: str, start: int) -> bool:
    with open(path, "rb") as handle:
        handle.seek(start)
        data = handle.read()
    if not data or not data.endswith(b"\n"):
        return False
    for line in data.splitlines():
        json.loads(line.decode("utf-8"))
    return True


@dataclass
class _Candidate:
    service_key: str
    tailer: Any
    sidecar_path: str
    created_at: float
    state: CandidateState = CandidateState.NOMINATING
    target_path: str | None = None
    target_inode: int | None = None
    observed_size: int | None = None
    digest: str | None = None


class FinalityManager:
    def __init__(
        self,
        redis: Any,
        prefix: str,
        *,
        fd_probe: Callable[[int], list[FdInfo]] | None = None,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        horizon_secs: float = DEFAULT_HORIZON_SECS,
        abandon_secs: float = DEFAULT_ABANDON_SECS,
        time_func: Callable[[], float] | None = None,
        wall_time_func: Callable[[], float] | None = None,
    ) -> None:
        self.store = FinalityWatchStore(redis, prefix)
        self.fd_probe = fd_probe or _default_fd_probe
        self.emit = emit
        self.horizon_secs = horizon_secs
        self.abandon_secs = abandon_secs
        self.time_func = time_func or time.monotonic
        self.wall_time_func = wall_time_func or time.time
        self.candidates: dict[tuple[str, int], _Candidate] = {}
        self.held_keys: set[str] = set()
        # Abandon is sticky for this service lifetime so the completion path
        # can fall through to teardown. It is intentionally not in Redis:
        # startup re-nomination is defined by the SPEC's watch/retracted
        # markers and may retry after a manager restart.
        self._abandoned: set[tuple[str, int]] = set()
        # Confirm-stage failures are retryable because a dirty EOF can be a
        # transient mid-write observation.  Keep the first nomination time so
        # repeated retries of one service/turn still share the D4 abandon
        # deadline instead of resetting it on every release.
        self._first_seen: dict[tuple[str, int], float] = {}
        self._rearmed = False

    def is_held(self, service_key: str) -> bool:
        return service_key in self.held_keys

    def nominate(self, service_key: str, tailer: Any, sidecar_path: str, *, now: float | None = None) -> bool:
        if not service_key.startswith("cold:"):
            return False
        try:
            sidecar = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return False
        if not isinstance(sidecar, dict) or sidecar.get("completed") is not True:
            return False
        if not bool(getattr(tailer, "_turn_open", False)):
            return False
        if not bool(getattr(tailer, "_has_started", False)):
            return False
        if bool(getattr(tailer, "completed", False)):
            return False
        if not bool(getattr(tailer, "_turn_clock_ok", False)):
            return False
        if not isinstance(getattr(tailer, "_turn_start_offset", None), int):
            return False
        if not getattr(tailer, "_turn_started_ts", None) or not getattr(tailer, "_last_causal_ts", None):
            return False
        turn_index = int(getattr(tailer, "logical_turn_index", 0))
        if turn_index < 1:
            return False
        candidate_key = (service_key, turn_index)
        if candidate_key in self._abandoned:
            return False
        if self._find_terminal(tailer, turn_index):
            return False
        if candidate_key in self.candidates:
            return True
        created_at = self._first_seen.setdefault(
            candidate_key, self.time_func() if now is None else now
        )
        self.candidates[candidate_key] = _Candidate(
            service_key=service_key,
            tailer=tailer,
            sidecar_path=sidecar_path,
            created_at=created_at,
        )
        self.held_keys.add(service_key)
        return True

    def _find_terminal(self, tailer: Any, turn_index: int) -> bool:
        run_id = getattr(getattr(tailer, "identity", None), "run_id", "")
        task_id = getattr(getattr(tailer, "identity", None), "task_id", "")
        for _key, record in self.store.scan():
            if record.get("run_id") == run_id and record.get("task_id") == task_id and record.get("turn_index") == turn_index:
                return record.get("status") in {"watched", "closing", "retracted"}
        return False

    def _has_terminal_for_task(self, tailer: Any) -> bool:
        run_id = getattr(getattr(tailer, "identity", None), "run_id", "")
        task_id = getattr(getattr(tailer, "identity", None), "task_id", "")
        return any(
            record.get("run_id") == run_id
            and record.get("task_id") == task_id
            and record.get("status") in {"watched", "closing", "retracted"}
            for _key, record in self.store.scan()
        )

    def _candidate_record(self, candidate: _Candidate, stat: os.stat_result) -> dict[str, Any]:
        tailer = candidate.tailer
        identity = tailer.identity
        now = self.wall_time_func()
        return {
            "run_id": identity.run_id,
            "task_id": identity.task_id,
            "seat_id": identity.seat_id,
            "orchestrator": identity.orchestrator,
            "turn_index": int(tailer.logical_turn_index),
            "target_path": candidate.target_path,
            "target_inode": int(stat.st_ino),
            "turn_start_offset": int(tailer._turn_start_offset),
            "observed_size": int(stat.st_size),
            "digest": candidate.digest,
            "event_ts": tailer._last_causal_ts,
            "turn_started_ts": tailer._turn_started_ts,
            "status": "watched",
            "finalized_at": now,
            "horizon_end": now + self.horizon_secs,
            "service_key": candidate.service_key,
            "output_path": tailer.path,
            "sidecar_path": candidate.sidecar_path,
        }

    def tick(self, *, now: float | None = None, wall_now: float | None = None) -> None:
        current = self.time_func() if now is None else now
        current_wall = self.wall_time_func() if wall_now is None else wall_now
        for candidate_key, candidate in list(self.candidates.items()):
            if candidate.state == CandidateState.WATCHED:
                continue
            if current - candidate.created_at >= self.abandon_secs:
                self._release(candidate_key, abandoned=True)
                continue
            if candidate.state == CandidateState.NOMINATING:
                candidate.state = CandidateState.QUIESCING
                continue
            try:
                target = os.path.realpath(candidate.tailer.path)
                stat = os.stat(target)
                fds = self.fd_probe(int(stat.st_ino))
            except (OSError, RuntimeError, subprocess.SubprocessError):
                continue
            if _has_write_open(fds):
                continue
            if candidate.state == CandidateState.QUIESCING:
                candidate.target_path = target
                candidate.target_inode = int(stat.st_ino)
                candidate.observed_size = int(stat.st_size)
                candidate.state = CandidateState.CONFIRMING
                continue
            try:
                if candidate.target_inode != stat.st_ino or candidate.observed_size != stat.st_size:
                    candidate.state = CandidateState.QUIESCING
                    continue
                if not _clean_eof(target, int(candidate.tailer._turn_start_offset)):
                    self._release(candidate_key)
                    continue
                candidate.digest = _digest(
                    target, int(candidate.tailer._turn_start_offset), int(stat.st_size)
                )
                record = self._candidate_record(candidate, stat)
                try:
                    self.store.put(record)
                except RuntimeError:
                    # A failed durable write must retain the teardown hold and
                    # must never be followed by a synthetic emit.
                    continue
                candidate.state = CandidateState.WATCHED
                self._first_seen.pop(candidate_key, None)
                self.held_keys.add(candidate.service_key)
                if self.emit is not None:
                    self.emit("turn_finalized", record)
            except (OSError, ValueError):
                self._release(candidate_key)

        self._close_expired(current_wall)

    def startup_renominate(self, service_key: str, tailer: Any, sidecar_path: str) -> bool:
        """Rebuild only continuity state from byte zero after a service restart.

        This is candidacy evidence: it emits no capture events and never writes
        the offset store.  The cursor is aligned in memory so the next normal
        poll can continue from its persisted position.
        """
        if not service_key.startswith("cold:") or self._has_terminal_for_task(tailer):
            return False
        try:
            target = os.path.realpath(tailer.path)
            stat = os.stat(target)
            with open(target, "rb") as handle:
                data = handle.read()
            if not data.endswith(b"\n"):
                return False
            turn_index = 0
            turn_start = None
            turn_started_ts = None
            last_ts = None
            previous_ts = None
            clock_ok = True
            for offset, line in self._lines_with_offsets(data):
                obj = json.loads(line.decode("utf-8"))
                out_of_turn = bool(obj.get("isMeta") or obj.get("isSidechain"))
                human = obj.get("type") == "user" and bool(obj.get("promptId")) and not out_of_turn
                if human:
                    turn_index += 1
                    turn_start = offset
                    turn_started_ts = obj.get("timestamp")
                    previous_ts = turn_started_ts
                elif not out_of_turn and obj.get("type") in ("user", "assistant"):
                    ts = obj.get("timestamp")
                    if turn_index and not isinstance(ts, str):
                        clock_ok = False
                    if isinstance(ts, str) and previous_ts is not None and ts < previous_ts:
                        clock_ok = False
                    if isinstance(ts, str):
                        previous_ts = ts
                        last_ts = ts
            if turn_index < 1 or turn_start is None or not clock_ok or not turn_started_ts or not previous_ts:
                return False
            tailer.logical_turn_index = turn_index
            tailer._turn_open = True
            tailer._has_started = True
            tailer._turn_clock_ok = True
            tailer._turn_start_offset = turn_start
            tailer._turn_started_ts = turn_started_ts
            tailer._last_causal_ts = last_ts or turn_started_ts
            tailer._cursor_inode = stat.st_ino
            tailer._cursor_offset = stat.st_size
            tailer.at_eof = True
            return self.nominate(service_key, tailer, sidecar_path)
        except (OSError, ValueError, UnicodeDecodeError):
            return False

    @staticmethod
    def _lines_with_offsets(data: bytes):
        offset = 0
        for line in data.splitlines(keepends=True):
            yield offset, line.rstrip(b"\n")
            offset += len(line)

    def _close_expired(self, current: float) -> None:
        for key, record in list(self.store.scan()):
            if record.get("status") != "watched":
                continue
            expired = current >= float(record.get("horizon_end", current + 1))
            if self._validate_watch(key, record, require_quiescence=expired) is not True:
                continue
            if not expired:
                continue
            closing = dict(record, status="closing")
            self.store.put(closing)
            self._unlink_artifacts(record)
            self.store.delete(key)
            self.held_keys.discard(record.get("service_key", ""))

    @staticmethod
    def _appended_same_turn(record: dict[str, Any]) -> bool | None:
        try:
            with open(record["target_path"], "rb") as handle:
                handle.seek(int(record["observed_size"]))
                data = handle.read()
            for line in data.splitlines():
                obj = json.loads(line.decode("utf-8"))
                if (
                    obj.get("type") == "user"
                    and bool(obj.get("promptId"))
                    and not obj.get("isMeta")
                    and not obj.get("isSidechain")
                ):
                    # A later human prompt is the boundary of the finalized turn;
                    # its assistant records are outside the protected turn.
                    return False
                if obj.get("type") == "assistant":
                    return True
                if obj.get("type") == "user" and not obj.get("promptId"):
                    return True
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return False

    def rearm(self) -> None:
        if self._rearmed:
            return
        self._rearmed = True
        for key, record in list(self.store.scan()):
            status = record.get("status")
            if status == "retracted":
                continue
            if status == "closing":
                self._unlink_artifacts(record)
                self.store.delete(key)
                self.held_keys.discard(record.get("service_key", ""))
                continue
            self.held_keys.add(record.get("service_key", ""))
            valid = self._validate_watch(key, record)
            if valid is True:
                if self.emit is not None:
                    self.emit("turn_finalized", record)

    def _validate_watch(
        self, key: str, record: dict[str, Any], *, require_quiescence: bool = False
    ) -> bool | None:
        """Return True for valid, False for a definitive mutation, None to defer."""
        try:
            stat = os.stat(record["target_path"])
        except FileNotFoundError:
            self._retract(key, record)
            return False
        except (OSError, ValueError):
            return None
        if stat.st_ino != int(record["target_inode"]) or stat.st_size < int(record["observed_size"]):
            self._retract(key, record)
            return False
        try:
            valid_digest = (
                _digest(record["target_path"], int(record["turn_start_offset"]), int(record["observed_size"]))
                == record["digest"]
            )
        except FileNotFoundError:
            self._retract(key, record)
            return False
        except ValueError:
            self._retract(key, record)
            return False
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None
        if not valid_digest:
            self._retract(key, record)
            return False
        if stat.st_size > int(record["observed_size"]):
            appended = self._appended_same_turn(record)
            if appended is None:
                return None
            if appended:
                self._retract(key, record)
                return False
        if require_quiescence:
            try:
                if _has_write_open(self.fd_probe(int(stat.st_ino))):
                    return None
            except (OSError, RuntimeError, subprocess.SubprocessError):
                return None
        return True

    def _retract(self, key: str, record: dict[str, Any]) -> None:
        self.store.mark_retracted(record)
        self._unlink_artifacts(record)
        self.held_keys.discard(record.get("service_key", ""))
        if self.emit is not None:
            self.emit("turn_finality_retracted", record)

    @staticmethod
    def _unlink_artifacts(record: dict[str, Any]) -> None:
        for name in ("output_path", "sidecar_path"):
            path = record.get(name)
            if not path:
                continue
            try:
                Path(path).unlink()
            except OSError:
                pass

    def _release(self, candidate_key: tuple[str, int], *, abandoned: bool = False) -> None:
        candidate = self.candidates.pop(candidate_key, None)
        if candidate is not None:
            self.held_keys.discard(candidate.service_key)
            if abandoned:
                self._abandoned.add(candidate_key)
                self._first_seen.pop(candidate_key, None)

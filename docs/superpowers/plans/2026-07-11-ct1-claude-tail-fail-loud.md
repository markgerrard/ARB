# CT-1 claude-tail fail-loud — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The orchestrator visibility tee can never be silently dark — infra failures crash the process (KeepAlive revives), hangs are killed by a watchdog, and liveness is legible on the live bus via an output-liveness heartbeat surfaced by the gateway.

**Architecture:** Four legs per the spec `docs/superpowers/specs/2026-07-11-ct1-claude-tail-fail-loud-design.md` (v1.6 FINAL — the authority for every behavior here): (A) `RedisError` crashes / parse errors skip / offset corruption self-heals, with chunked budget-bounded polls and `at_eof`-gated finish paths backed by durable draining records; (B) a lock-free watchdog thread that `os._exit(86)`s a stalled main loop; (C) a heartbeat key on the live bus written by the main loop, read by the gateway via a configured expected-labels MGET; (D) `RotatingFileHandler` logging.

**Tech Stack:** Python 3.14, redis-py, pytest (hermetic fakes — no live bus in tests), Starlette (gateway), vanilla JS (gateway UI).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-11-ct1-claude-tail-fail-loud-design.md` v1.6. If plan and spec conflict, the spec wins — STOP and report, do not improvise.
- All work in the assigned worktree only. Run tests with the repo venv: `PYTHONPATH=src .venv/bin/python -m pytest ...` from the worktree root, and verify `module.__file__` points into YOUR worktree if anything looks stale (worktree-editable-install shadowing is a known trap).
- Targeted test files ONLY (`tests/claude_tail/`, `tests/test_visibility*.py` where named). NEVER run the full suite.
- Log records about transcript lines carry **path + offset only — never line bytes** (transcript lines are conversation content).
- The watchdog thread must never call into the `logging` framework.
- Heartbeat payload fields are structural only; any future free-text field must route through `redact()`.
- Env var names are load-bearing (gateway and plist depend on them): `ARB_CLAUDE_TAIL_WATCHDOG_SECS`, `ARB_CLAUDE_TAIL_POLL_BUDGET_LINES`, `ARB_CLAUDE_TAIL_POLL_BUDGET_SECS`, `ARB_CLAUDE_TAIL_TICK_DEADLINE_SECS`, `ARB_CLAUDE_TAIL_HEARTBEAT_LABEL`, `ARB_CLAUDE_TAIL_LOG_FILE`, `ARB_VIS_EXPECTED_TEES`.
- Commit after every green task with the exact message given; do not push.

## Shared interface contract (all tasks)

- `TranscriptTailer.poll() -> int` (emitted count, unchanged signature). After every call it sets: `self.at_eof: bool` (no more complete lines were available this call), `self.progressed: bool` (offset advanced this call), `self.emit_failing: bool` (sticky: an emit-stage failure propagated; cleared by a clean poll), `self.skipped_lines: int` (cumulative parse-stage skips since construction).
- Class attrs on `TranscriptTailer`: `poll_budget_lines = 500`, `poll_budget_secs = 2.0`.
- `Watchdog(threshold_secs, tick_interval_secs, *, wake_secs=15.0, time_func=time.monotonic, write_func=os.write, exit_func=os._exit)` with `.effective_threshold: float`, `.mark_tick()`, `.check()`, `.start()`.
- `Service.__init__` gains kwargs (all keyword-only, defaulted): `heartbeat_label: str | None = None`, `stale_after_s: int = 330`, `tick_deadline_secs: float = 30.0`, `draining_ttl_secs: int = 604800`.
- Draining record key: `f"{prefix}claude:draining:{session_id}"` on the LOCAL agent redis (`Service.redis`); value = the registry record JSON.
- Heartbeat key: `f"{prefix}tail:heartbeat:{label}"` on the LIVE bus (`Service.live_redis`); value = 8-field JSON; TTL 604800.
- Hook helper: `claude_tail_hooks.common.copy_redis_record_to_draining(client, session_id, *, ttl_secs=604800) -> bool`.
- Gateway helper: `arb_memory.visibility.tee_states(redis_client, bus_prefix, labels, now) -> list[dict]` (module level, unit-testable).

---

### Task 1: OffsetStore corruption self-heal

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/offset.py`
- Test: `tests/claude_tail/test_offset.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `OffsetStore.get(key)` returns 0 (and resets the stored value to `"0"`) instead of raising when the stored value is corrupt. `RedisError` from the client still propagates.

- [ ] **Step 1: Write the failing tests** (append to `tests/claude_tail/test_offset.py`)

```python
def test_get_self_heals_corrupt_offset_to_zero():
    redis = FakeRedis()
    store = OffsetStore(redis, "p:")
    redis.set("p:claude:offset:k", "not-an-int")

    assert store.get("k") == 0
    assert redis.get("p:claude:offset:k") == "0"


def test_get_self_heals_none_like_corruption():
    class WeirdRedis(FakeRedis):
        def get(self, key):
            return ["not", "a", "str"]

    store = OffsetStore(WeirdRedis(), "p:")
    assert store.get("k") == 0
```

(`FakeRedis` already exists in this test file; reuse it. If its `set` signature differs, match it.)

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_offset.py -v`
Expected: the two new tests FAIL with `ValueError` / `TypeError`.

- [ ] **Step 3: Implement** — replace `OffsetStore.get` in `src/agent_redis_bridge/claude_tail/offset.py`:

```python
import logging

logger = logging.getLogger("agent_redis_bridge.claude_tail.offset")


    def get(self, key: str) -> int:
        value = self.redis.get(self._redis_key(key))
        if value is None:
            return 0
        if isinstance(value, bytes):
            value = value.decode()
        try:
            return int(value)
        except (TypeError, ValueError):
            # A corrupt stored offset must not become a permanent per-tailer
            # failure loop (spec §A, panel r1 codex): reset to 0 — the tailer
            # re-reads from the top, at-least-once. RedisError from the reset
            # propagates (infra crashes).
            logger.warning("corrupt claude-tail offset; resetting to 0", extra={"offset_key": key})
            self.redis.set(self._redis_key(key), "0")
            return 0
```

(`import logging` goes at module top with the existing imports; the `logger =` line below them.)

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_offset.py -v`
Expected: ALL tests in the file PASS (pre-existing ones included).

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/offset.py tests/claude_tail/test_offset.py
git commit -m "fix(claude-tail): offset corruption self-heals to 0 instead of raising forever (CT-1 A)"
```

---

### Task 2: Watchdog module

**Files:**
- Create: `src/agent_redis_bridge/claude_tail/watchdog.py`
- Test: `tests/claude_tail/test_watchdog.py` (new file)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `Watchdog` per the shared contract. Task 9 wires it into `main()`/`run_loop`.

- [ ] **Step 1: Write the failing tests** — create `tests/claude_tail/test_watchdog.py`:

```python
from agent_redis_bridge.claude_tail.watchdog import LAST_GASP, Watchdog


class Harness:
    def __init__(self, start=1000.0):
        self.now = start
        self.writes = []
        self.exits = []

    def time(self):
        return self.now

    def write(self, fd, data):
        self.writes.append((fd, data))

    def exit(self, code):
        self.exits.append(code)


def _watchdog(h, threshold=300.0, interval=1.0):
    return Watchdog(threshold, interval, time_func=h.time, write_func=h.write, exit_func=h.exit)


def test_no_fire_below_threshold():
    h = Harness()
    wd = _watchdog(h)
    h.now += 299.0
    wd.check()
    assert h.exits == []


def test_fires_past_threshold_with_raw_write_then_exit_86():
    h = Harness()
    wd = _watchdog(h)
    h.now += 301.0
    wd.check()
    assert h.exits == [86]
    assert h.writes == [(2, LAST_GASP)]


def test_no_fire_in_first_window_with_zero_ticks():
    # init pins last_tick to construction time (spec §B, panel r3 grok):
    # a fresh daemon must not immediately-fire before its first tick.
    h = Harness()
    wd = _watchdog(h)
    h.now += 100.0
    wd.check()
    assert h.exits == []


def test_mark_tick_resets_the_clock():
    h = Harness()
    wd = _watchdog(h)
    h.now += 250.0
    wd.mark_tick()
    h.now += 250.0
    wd.check()
    assert h.exits == []


def test_effective_threshold_floor_vs_long_interval():
    # interval 360s with configured 300s must NOT false-fire a healthy
    # sleeping daemon (spec §B, panel r3 agy): floor = 3*interval + 60.
    h = Harness()
    wd = Watchdog(300.0, 360.0, time_func=h.time, write_func=h.write, exit_func=h.exit)
    assert wd.effective_threshold == 3 * 360.0 + 60.0
    h.now += 1000.0
    wd.check()
    assert h.exits == []
    h.now += 200.0  # total 1200 > 1140
    wd.check()
    assert h.exits == [86]


def test_exit_runs_even_if_write_raises():
    # EBADF on a detached stderr must not kill the watchdog before exit
    # (spec §B, panel r2 agy).
    h = Harness()

    def bad_write(fd, data):
        raise OSError(9, "EBADF")

    wd = Watchdog(300.0, 1.0, time_func=h.time, write_func=bad_write, exit_func=h.exit)
    h.now += 301.0
    wd.check()
    assert h.exits == [86]


def test_check_never_touches_logging(monkeypatch):
    # The last gasp must not enter the logging framework (spec §B, panel r1
    # cold-Opus: a main thread hung holding a handler lock would deadlock us).
    import logging

    def boom(*args, **kwargs):
        raise AssertionError("watchdog called into logging")

    for name in ("info", "warning", "error", "exception", "critical", "debug", "log"):
        monkeypatch.setattr(logging.Logger, name, boom)
    h = Harness()
    wd = _watchdog(h)
    h.now += 301.0
    wd.check()
    assert h.exits == [86]
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_watchdog.py -v`
Expected: FAIL with `ModuleNotFoundError: ... watchdog`.

- [ ] **Step 3: Implement** — create `src/agent_redis_bridge/claude_tail/watchdog.py`:

```python
from __future__ import annotations

import os
import threading
import time

LAST_GASP = b"[claude-tail watchdog] main loop stalled past threshold; os._exit(86)\n"


class Watchdog:
    """Crash a stalled main loop.

    The check path does time arithmetic, one raw fd write, and exit — no
    Redis, no locks, and NEVER the logging framework: if the main thread is
    hung inside a logging write holding the handler lock (a live candidate
    for the 2026-07-06 incident), a logging call here would deadlock behind
    the same lock and the process would never exit (spec §B).
    """

    def __init__(
        self,
        threshold_secs: float,
        tick_interval_secs: float,
        *,
        wake_secs: float = 15.0,
        time_func=time.monotonic,
        write_func=os.write,
        exit_func=os._exit,
    ) -> None:
        floor = 3.0 * float(tick_interval_secs) + 60.0
        self.effective_threshold = max(float(threshold_secs), floor)
        if self.effective_threshold > float(threshold_secs):
            # Startup log line when the floor raises the threshold (spec §B;
            # plan panel, agy P1). Init runs on the MAIN thread before
            # start() — the no-logging rule applies to check(), not here.
            import logging

            logging.getLogger("agent_redis_bridge.claude_tail.watchdog").warning(
                "watchdog threshold raised to %.0fs (floor 3*interval+60 over configured %.0fs)",
                self.effective_threshold,
                float(threshold_secs),
            )
        self.wake_secs = wake_secs
        self._time = time_func
        self._write = write_func
        self._exit = exit_func
        # Init pin (spec §B): last_tick starts NOW, so a fresh daemon gets a
        # full threshold window before its first completed tick.
        self._last_tick = time_func()

    def mark_tick(self) -> None:
        self._last_tick = self._time()

    def check(self) -> None:
        if self._time() - self._last_tick > self.effective_threshold:
            try:
                self._write(2, LAST_GASP)
            except Exception:
                pass  # a dead stderr must not stop the exit (spec §B)
            self._exit(86)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="claude-tail-watchdog", daemon=True)
        thread.start()
        return thread

    def _loop(self) -> None:  # pragma: no cover - thin sleep loop over check()
        while True:
            time.sleep(self.wake_secs)
            self.check()
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_watchdog.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/watchdog.py tests/claude_tail/test_watchdog.py
git commit -m "feat(claude-tail): lock-free watchdog thread, raw last gasp + os._exit(86) (CT-1 B)"
```

---

### Task 3: Tailer — stage-split classification, chunked budgeted polls, at_eof, prefix commit

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/tailer.py`
- Test: `tests/claude_tail/test_tailer.py`

**Interfaces:**
- Consumes: Task 1's self-healing `OffsetStore` (no code change needed here).
- Produces: `poll()` per the shared contract (`at_eof`, `progressed`, `emit_failing`, `skipped_lines`, budgets). Tasks 4-6 rely on these attributes.

- [ ] **Step 1: Write the failing tests** (append to `tests/claude_tail/test_tailer.py`)

```python
from redis.exceptions import ConnectionError as RedisConnectionError

from agent_redis_bridge.claude_tail.mapper import DriftError


def _tailer(transcript, redis, **kwargs):
    identity = warm_identity("sess", "bridge", "dev")
    store = OffsetStore(redis, "p:")
    return TranscriptTailer(
        str(transcript), identity, store,
        live_redis=redis, trace_redis=redis,
        prefix="agent_scratch:", redactor=lambda s: s, **kwargs,
    )


def _offset_value(redis, transcript):
    import os as _os
    key = offset_key(str(transcript), _os.stat(transcript).st_ino)
    return int(redis.get(f"p:claude:offset:{key}") or 0)


def test_non_dict_json_lines_are_skipped_and_counted(tmp_path, caplog):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        'null\n[]\n"str"\n'
        + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        + "\n",
        encoding="utf-8",
    )
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)

    emitted = tailer.poll()

    assert tailer.skipped_lines == 3
    assert emitted > 0  # the good line still emitted
    assert tailer.at_eof is True
    # skip log records carry path+offset ONLY, never the line bytes (GLM G1)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "null" not in joined and '"str"' not in joined
    # next poll does not re-read the skipped lines
    assert tailer.poll() == 0
    assert tailer.skipped_lines == 3


def test_invalid_json_line_skipped_offset_advances(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("{not json}\n", encoding="utf-8")
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)

    tailer.poll()

    assert tailer.skipped_lines == 1
    assert _offset_value(redis, transcript) == transcript.stat().st_size


def test_emit_stage_redis_error_propagates_without_offset_advance(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})

    class InfraFailRedis(FakeRedis):
        def xadd(self, key, fields, **kwargs):
            raise RedisConnectionError("broken pipe")

    redis = InfraFailRedis()
    tailer = _tailer(transcript, redis)

    with pytest.raises(RedisConnectionError):
        tailer.poll()
    assert _offset_value(redis, transcript) == 0  # infra never advances offsets
    assert tailer.skipped_lines == 0  # NOT classified as a data error
    assert tailer.emit_failing is False  # infra-crash path, NOT the code-bug
    # path — this assertion is what makes deny-proof 1 reddable (plan panel,
    # grok P1 + cold-Opus P2-1: without it, a single-line fixture cannot
    # distinguish the arms because line_start == offset commits nothing).


def test_emit_stage_code_bug_prefix_commits_and_marks_failing(tmp_path):
    # Lines 0..N-1 emit clean, line N's emit raises a non-RedisError: offset
    # commits through N-1 (prefix commit, panel r3 cold-Opus P1), the failure
    # propagates, emit_failing is sticky (spec §A).
    transcript = tmp_path / "s.jsonl"
    good = {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
    _write_jsonl(transcript, good, good, good)
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)

    calls = {"n": 0}
    real_route = tailer._route_event

    def flaky_route(event):
        calls["n"] += 1
        if calls["n"] >= 4:  # first line emits task_started + model event = 2-3 calls
            raise AttributeError("injected emit bug")
        return real_route(event)

    tailer._route_event = flaky_route

    with pytest.raises(AttributeError):
        tailer.poll()

    assert tailer.emit_failing is True
    committed = _offset_value(redis, transcript)
    line_len = len(json.dumps(good, separators=(",", ":")) + "\n")
    assert committed % line_len == 0 and 0 < committed < transcript.stat().st_size

    # a later clean poll clears the sticky flag
    tailer._route_event = real_route
    tailer.poll()
    assert tailer.emit_failing is False


def test_line_budget_chunks_and_commits_per_chunk(tmp_path):
    transcript = tmp_path / "s.jsonl"
    good = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}
    _write_jsonl(transcript, *([good] * 7))
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)
    tailer.poll_budget_lines = 3

    tailer.poll()
    assert tailer.at_eof is False
    assert tailer.progressed is True
    first = _offset_value(redis, transcript)
    assert 0 < first < transcript.stat().st_size

    tailer.poll()
    second = _offset_value(redis, transcript)
    assert second > first  # monotonic progress per chunk (deny-proof hinge 5b)

    tailer.poll()
    assert tailer.at_eof is True
    assert _offset_value(redis, transcript) == transcript.stat().st_size


def test_wall_clock_budget_finishes_current_line_then_returns(tmp_path, monkeypatch):
    transcript = tmp_path / "s.jsonl"
    good = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}
    _write_jsonl(transcript, *([good] * 5))
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)
    tailer.poll_budget_secs = 0.0  # every loop-top check is already expired

    tailer.poll()

    # exactly one complete line processed (budget checked between lines; a
    # started line always finishes — offsets are line-granular, spec §A)
    line_len = len(json.dumps(good, separators=(",", ":")) + "\n")
    assert _offset_value(redis, transcript) == line_len
    assert tailer.at_eof is False


def test_partial_trailing_line_counts_as_eof(tmp_path):
    # spec §A (panel r4 grok pin): a torn final line must not block at_eof.
    transcript = tmp_path / "s.jsonl"
    good = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}})
    transcript.write_text(good + "\n" + '{"type": "assis', encoding="utf-8")
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)

    tailer.poll()

    assert tailer.at_eof is True
    # offset stops at the end of the last COMPLETE line
    assert _offset_value(redis, transcript) == len(good) + 1


def test_drift_error_keeps_dedicated_arm_not_parse_skip(tmp_path):
    # spec §A (panel r3 grok P1 + r4 agy P2): unknown line types still emit
    # drift_error, still count toward the threshold, are NOT skipped.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "mystery_type_zz", "message": {}})
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)

    tailer.poll()

    assert tailer.skipped_lines == 0
    assert tailer.drift_count == 1
    assert "drift_error" in _event_types(redis)
```

Note: `test_drift_error_keeps_dedicated_arm_not_parse_skip` depends on `map_line` raising `DriftError` for unknown types — check `mapper.py::map_line` first; if an unknown type maps to `[]` instead of raising, pick a payload shape that DOES raise (e.g. `{"type": "assistant", "message": {"content": "not-a-list"}}` per `mapper.py`'s "message.content is not a list" DriftError) and adjust.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_tailer.py -v -k "skipped or budget or prefix or partial or drift_error_keeps or propagates_without"`
Expected: new tests FAIL (`AttributeError: ... skipped_lines`, offset mismatches, etc.).

- [ ] **Step 3: Implement** — in `src/agent_redis_bridge/claude_tail/tailer.py`:

3a. Add to the module imports:

```python
from redis.exceptions import RedisError
```

3b. Add to `__init__` (after `self.completed = False`):

```python
        # CT-1 poll contract (spec §A): set on every poll() call.
        self.at_eof = False
        self.progressed = False
        self.emit_failing = False
        self.skipped_lines = 0
```

3c. Add class attributes next to `drift_threshold`:

```python
    poll_budget_lines = 500
    poll_budget_secs = 2.0
```

3d. Replace `poll()` and `_process_line()` with the stage-split, budgeted versions:

```python
    def poll(self) -> int:
        started_at = time.monotonic()
        stat = os.stat(self.path)
        key = offset_key(self.path, stat.st_ino)
        offset = self.offset_store.get(key)
        if offset > stat.st_size:
            self.offset_store.commit(key, 0)
            offset = 0

        emitted = 0
        lines = 0
        new_offset = offset
        self.at_eof = False
        emit_failed = False
        with open(self.path, "rb") as fh:
            fh.seek(offset)
            while True:
                # Budget check between lines (spec §A): a started line always
                # finishes its events — offsets are line-granular — so
                # checking here IS the "between event emissions" rule's only
                # implementable boundary (the spec's own worst-case statement,
                # "overshoot = one line's residual fan-out", is exactly this
                # check's semantics). The wall-clock branch is guarded with
                # `lines > 0` so a tiny/zero budget still makes at least one
                # line of progress per poll — zero-progress polls would starve
                # forever (plan panel, agy P0 + cold-Opus P1-1).
                if lines >= self.poll_budget_lines or (
                    lines > 0 and time.monotonic() - started_at >= self.poll_budget_secs
                ):
                    break
                line = fh.readline()
                if not line or not line.endswith(b"\n"):
                    # EOF, or a torn trailing partial line: both count as
                    # at_eof (spec §A, panel r4 grok pin) — no complete lines
                    # remain to read this call.
                    self.at_eof = True
                    break
                line_start = new_offset
                new_offset = fh.tell()
                lines += 1

                # ---- parse/map stage: ANY failure skips the line ----
                try:
                    events, obj = self._parse_line(line)
                except DriftError as exc:
                    # Dedicated arm ORDERED BEFORE the generic skip (spec §A,
                    # panel r4 agy P2). Drift emission is EMIT-stage: RedisError
                    # propagates (infra crashes); a non-Redis bug during the
                    # drift emit gets the same prefix-commit + sticky-failing
                    # treatment as any emit-stage bug (plan panel, codex P1 —
                    # otherwise it replays the whole chunk under a
                    # non-failing heartbeat).
                    self._ensure_identity_resolved()
                    self.drift_count += 1
                    try:
                        self._emit_drift_error(exc)
                    except RedisError:
                        raise
                    except Exception:
                        self.emit_failing = True
                        if line_start != offset:
                            self.offset_store.commit(key, line_start)
                        self.progressed = line_start != offset
                        raise
                    if self.drift_count > self.drift_threshold:
                        self.offset_store.commit(key, new_offset)
                        self.progressed = True
                        raise _DriftThresholdExceeded("drift threshold exceeded") from exc
                    emitted += 1
                    continue
                except RedisError:
                    raise
                except Exception:
                    self.skipped_lines += 1
                    logger.warning(
                        "skipping unparseable claude transcript line",
                        extra={"transcript_path": self.path, "line_offset": line_start},
                    )
                    continue

                # ---- emit stage ----
                try:
                    emitted += self._emit_events(events, obj)
                except RedisError:
                    # Infra: never advance past unemitted events; the process
                    # crashes via run_loop (spec §A). Lines already read this
                    # chunk are replayed on respawn — at-least-once.
                    raise
                except Exception:
                    # Code bug in the emit path: commit through the PREVIOUS
                    # (last fully-emitted) line so re-attempts are bounded to
                    # this single line (prefix commit, spec §A, panel r3
                    # cold-Opus P1), mark sticky-failing, propagate.
                    emit_failed = True
                    self.emit_failing = True
                    if line_start != offset:
                        self.offset_store.commit(key, line_start)
                    self.progressed = line_start != offset
                    raise

        if new_offset != offset:
            self.offset_store.commit(key, new_offset)
            self.progressed = True
        else:
            self.progressed = False
            if emitted == 0:
                emitted += self._maybe_emit_continuing()
        if not emit_failed:
            self.emit_failing = False
        return emitted

    def _parse_line(self, line: bytes):
        obj = json.loads(line.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("transcript line is not a JSON object")
        self._capture_first_user_marker(obj)
        self._check_done_marker(obj)
        events = map_line(obj)  # may raise DriftError — handled by caller
        return events, obj

    def _emit_events(self, events, obj) -> int:
        self._ensure_identity_resolved()
        if events and obj.get("type") == "assistant":
            self.turn_index += 1
        emitted = 0
        for event in events:
            started = self.lifecycle.started()
            if started is not None:
                self._has_started = True
                self._route_event(started)
                emitted += 1
            self._route_event(event)
            emitted += 1
        return emitted
```

Delete the old `_process_line` method (its body is now split across `_parse_line`/`_emit_events`; the DriftError handling moved into `poll()`).

Note: `emit_failing` on the DriftError path — a `RedisError` from `_emit_drift_error` propagates via the outer `except RedisError: raise` INSIDE the DriftError arm? No: it propagates out of the DriftError arm directly to `poll()`'s caller (there is no enclosing try). That is correct — infra crashes.

- [ ] **Step 4: Run to verify pass, including all pre-existing tailer tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_tailer.py -v`
Expected: ALL PASS. Pre-existing tests are the regression net for the restructure — if any fail, the restructure broke semantics; fix the implementation, not the old tests (exception: a pre-existing test that pins `_process_line` by name may be updated to the new seam, preserving its assertion).

- [ ] **Step 5: Deny-proofs (throwaway — do NOT commit the mutations)**

1. Comment out the `except RedisError: raise` arm in the emit stage → `test_emit_stage_redis_error_propagates_without_offset_advance` must go RED at the `emit_failing is False` assertion (the code-bug arm sets it True). Restore.
2. Change the prefix commit to commit `new_offset` instead of `line_start` → `test_emit_stage_code_bug_prefix_commits_and_marks_failing` must go RED. Restore.
3. Swap the DriftError arm below the generic `except Exception` → `test_drift_error_keeps_dedicated_arm_not_parse_skip` must go RED. Restore.

Run after restoring: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_tailer.py -q` → all green. Report each deny-proof result (red seen: yes/no) in your task report.

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/tailer.py tests/claude_tail/test_tailer.py
git commit -m "feat(claude-tail): stage-split error classification + chunked budgeted polls with at_eof and prefix commit (CT-1 A)"
```

---

### Task 4: run_loop + Service — RedisError crashes, OSError/emit failures mark sticky failing

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/__main__.py`
- Modify: `src/agent_redis_bridge/claude_tail/service.py`
- Test: `tests/claude_tail/test_service.py`

**Interfaces:**
- Consumes: Task 3's `emit_failing` attribute.
- Produces: `run_loop(service, *, interval, sleep_func, max_ticks, watchdog=None)` re-raises `RedisError`, calls `watchdog.mark_tick()` after every tick; `_TailerState.failing: bool`; `Service.tick` re-raises `RedisError` from any tailer, marks `failing` on `OSError`/other exceptions and on `emit_failing`. Task 8's heartbeat counts `failing`.

- [ ] **Step 1: Write the failing tests** (append to `tests/claude_tail/test_service.py`; extend the existing `FakeTailer` minimally as shown)

First, add these attributes to the existing `FakeTailer.__init__` body (keeps all existing tests green — defaults preserve old behavior):

```python
        self.at_eof = True
        self.progressed = False
        self.emit_failing = False
        self.skipped_lines = 0
        self.poll_exc = None
```

and change `FakeTailer.poll` to:

```python
    def poll(self):
        self.poll_count += 1
        if self.poll_exc is not None:
            raise self.poll_exc
        return 0
```

Then the tests:

```python
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError


def _service_with_one_warm(tmp_path, redis=None):
    redis = redis or FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps([{"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}]), encoding="utf-8")
    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), tailer_cls=FakeTailer)
    return service, transcript, registry


def test_tick_reraises_redis_error_from_tailer(tmp_path):
    service, transcript, registry = _service_with_one_warm(tmp_path)
    service.tick()  # creates the tailer
    FakeTailer.instances[-1].poll_exc = RedisConnectionError("broken pipe")
    with pytest.raises(RedisConnectionError):
        service.tick()


def test_tick_marks_tailer_failing_on_oserror_and_clears_on_clean_poll(tmp_path):
    service, transcript, registry = _service_with_one_warm(tmp_path)
    service.tick()
    tailer = FakeTailer.instances[-1]
    tailer.poll_exc = PermissionError("no read")
    service.tick()
    state = next(iter(service._tailers.values()))
    assert state.failing is True
    tailer.poll_exc = None
    service.tick()
    assert state.failing is False


def test_tick_marks_failing_from_emit_failing_attr(tmp_path):
    service, transcript, registry = _service_with_one_warm(tmp_path)
    service.tick()
    FakeTailer.instances[-1].emit_failing = True
    service.tick()
    assert next(iter(service._tailers.values())).failing is True


def test_run_loop_reraises_redis_error_and_swallows_others():
    class Boom:
        def __init__(self, exc):
            self.exc = exc
            self.ticks = 0

        def tick(self):
            self.ticks += 1
            raise self.exc

    swallowed = Boom(ValueError("x"))
    run_loop(swallowed, interval=0, sleep_func=lambda _s: None, max_ticks=3)
    assert swallowed.ticks == 3

    fatal = Boom(RedisConnectionError("bus down"))
    with pytest.raises(RedisConnectionError):
        run_loop(fatal, interval=0, sleep_func=lambda _s: None, max_ticks=3)
    assert fatal.ticks == 1  # exited on the FIRST infra failure


def test_run_loop_marks_watchdog_every_tick():
    class Ticker:
        def tick(self):
            return 0

    class FakeWatchdog:
        def __init__(self):
            self.marks = 0

        def mark_tick(self):
            self.marks += 1

    wd = FakeWatchdog()
    run_loop(Ticker(), interval=0, sleep_func=lambda _s: None, max_ticks=4, watchdog=wd)
    assert wd.marks == 4
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py -v -k "reraises or failing or watchdog"`
Expected: FAIL (RedisError swallowed; no `failing` attr; `run_loop` has no `watchdog` kwarg).

- [ ] **Step 3: Implement**

3a. `src/agent_redis_bridge/claude_tail/__main__.py` — full new `run_loop` (add `from redis.exceptions import RedisError` at module top):

```python
def run_loop(service, *, interval: float, sleep_func: Callable[[float], None] = time.sleep, max_ticks: int | None = None, watchdog=None) -> None:
    import logging

    log = logging.getLogger("agent_redis_bridge.claude_tail.service")
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        try:
            service.tick()
        except RedisError:
            # Infra: crash-fast (spec §A). launchd KeepAlive is the retry;
            # normal propagation exits through the interpreter, which flushes
            # logging handlers.
            log.exception("claude tail tick failed on the bus; exiting for KeepAlive respawn")
            raise
        except Exception:
            log.exception("Claude tail service tick failed")
        if watchdog is not None:
            # After every completed tick, success or handled failure — the
            # loop is alive either way (spec §B).
            watchdog.mark_tick()
        ticks += 1
        sleep_func(interval)
```

3b. `src/agent_redis_bridge/claude_tail/service.py`:
- Add `from redis.exceptions import RedisError` to module imports.
- Add `failing: bool = False` to the `_TailerState` dataclass.
- In `tick()`'s per-tailer poll block, replace the two existing `except` arms with:

```python
            try:
                emitted = state.tailer.poll()
            except FileNotFoundError:
                logger.warning("claude transcript vanished; finishing tailer", extra={"tailer_key": key})
                self._finish_once(state)
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
```

- In `_prune_missing_warm_registry_record`, split the swallow:

```python
        try:
            self.redis.hdel(f"{self.prefix}{self.registry_key}", field)
        except RedisError:
            raise
        except Exception:
            logger.warning("failed to prune stale Claude registry field", extra={"session_id": str(field)})
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py -v`
Expected: ALL PASS (old + new).

- [ ] **Step 5: Deny-proofs (throwaway)**

1. Delete the `except RedisError: raise` arm in `tick()` → `test_tick_reraises_redis_error_from_tailer` RED. Restore.
2. Restore the blanket `except Exception: log; continue` in `run_loop` (remove the RedisError arm) → `test_run_loop_reraises_redis_error_and_swallows_others` RED (binds the two layers — spec §E test 3, panel r3 grok). Restore.

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py -q` → green. Report deny-proof results.

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/__main__.py src/agent_redis_bridge/claude_tail/service.py tests/claude_tail/test_service.py
git commit -m "feat(claude-tail): RedisError crashes through run_loop; per-tailer failures sticky-visible (CT-1 A)"
```

---

### Task 5: Service — tick deadline, round-robin cursor, at_eof-gated finishes, idle-finish activity

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/service.py`
- Test: `tests/claude_tail/test_service.py`

**Interfaces:**
- Consumes: Task 3's `at_eof`/`progressed`; Task 4's per-tailer handling.
- Produces: `Service.__init__` kwarg `tick_deadline_secs: float = 30.0`; `Service._cursor`; every finish path gated on `at_eof`; `last_activity` bumps on `progressed or emitted`. Task 6 adds the draining branch into this structure.

- [ ] **Step 1: Write the failing tests** (append to `tests/claude_tail/test_service.py`)

```python
def _write_cold(tmp_path, name="agent1", lines=1):
    cold_dir = tmp_path / "cold"
    cold_dir.mkdir(exist_ok=True)
    out = cold_dir / f"{name}.output"
    out.write_text("x\n" * lines, encoding="utf-8")
    return cold_dir, out


def test_cold_sidecar_completed_does_not_finish_before_eof(tmp_path):
    # spec §A Finish paths (panel r3 convergent P0): a budget-limited poll +
    # completed:true must NOT finish+delete an undrained transcript.
    # ORDERING MATTERS (plan panel, grok P0): create the tailer and put it
    # mid-backlog BEFORE the sidecar flips to completed — FakeTailer defaults
    # at_eof=True, so a pre-completed sidecar would (correctly!) finish on
    # the very first tick and the test would fail against right behavior.
    cold_dir, out = _write_cold(tmp_path)
    (tmp_path / "empty.json").write_text("[]", encoding="utf-8")
    service = Service(redis=FakeRedis(), prefix="agent_scratch:", registry_path=str(tmp_path / "empty.json"), cold_dir=str(cold_dir), tailer_cls=FakeTailer)

    service.tick()  # creates tailer (no sidecar yet)
    tailer = FakeTailer.instances[-1]
    tailer.at_eof = False  # budget-limited chunk, backlog remains
    (cold_dir / "agent1.arb-tail.json").write_text(json.dumps({"completed": True}), encoding="utf-8")
    service.tick()
    assert out.exists()  # NOT deleted
    state = next(iter(service._tailers.values()))
    assert state.finished is False

    tailer.at_eof = True
    service.tick()
    assert not out.exists()  # drained -> finish + delete


def test_inband_completed_gates_on_eof(tmp_path):
    cold_dir, out = _write_cold(tmp_path)
    service = Service(redis=FakeRedis(), prefix="agent_scratch:", registry_path=str(tmp_path / "empty.json"), cold_dir=str(cold_dir), tailer_cls=FakeTailer)
    service.tick()
    tailer = FakeTailer.instances[-1]
    tailer.completed = True
    tailer.at_eof = False
    service.tick()
    assert next(iter(service._tailers.values())).finished is False
    tailer.at_eof = True
    service.tick()
    assert next(iter(service._tailers.values())).finished is True


def test_idle_finish_requires_eof_and_offset_progress_counts_as_activity(tmp_path):
    # spec §A Finish paths (panel r3 agy P1): a zero-event multi-chunk
    # catch-up must not idle-finish mid-file.
    cold_dir, out = _write_cold(tmp_path)
    clock = {"now": 0.0}
    service = Service(
        redis=FakeRedis(), prefix="agent_scratch:", registry_path=str(tmp_path / "empty.json"),
        cold_dir=str(cold_dir), tailer_cls=FakeTailer, idle_finish_secs=10.0,
        time_func=lambda: clock["now"],
    )
    service.tick()
    tailer = FakeTailer.instances[-1]

    tailer.at_eof = False
    tailer.progressed = True  # catching up through event-less lines
    clock["now"] = 100.0
    service.tick()
    assert next(iter(service._tailers.values())).finished is False  # progress = activity

    tailer.progressed = False
    tailer.at_eof = False
    clock["now"] = 200.0
    service.tick()
    assert next(iter(service._tailers.values())).finished is False  # idle but NOT at_eof

    tailer.at_eof = True
    clock["now"] = 300.0
    service.tick()
    assert next(iter(service._tailers.values())).finished is True  # at_eof + idle window


def test_tick_deadline_round_robin_no_starvation(tmp_path):
    # spec §A budgets (panel r3 codex P0 + grok P2): the tick returns at the
    # deadline and resumes with the NEXT tailer — every tailer progresses
    # across ticks.
    cold_dir, _ = _write_cold(tmp_path, "a")
    _write_cold(tmp_path, "b")
    _write_cold(tmp_path, "c")
    clock = {"now": 0.0}

    class SlowTailer(FakeTailer):
        def poll(self):
            clock["now"] += 10.0  # each poll consumes 10s
            return super().poll()

    FakeTailer.instances.clear()  # SlowTailer shares the class-level list —
    # clear so the [-3:] slice below is unambiguous (plan panel, grok P2)
    service = Service(
        redis=FakeRedis(), prefix="agent_scratch:", registry_path=str(tmp_path / "empty.json"),
        cold_dir=str(cold_dir), tailer_cls=SlowTailer, tick_deadline_secs=15.0,
        time_func=lambda: clock["now"],
    )
    service.tick()  # tailers created; deadline cuts polling short
    service.tick()
    service.tick()
    counts = sorted(t.poll_count for t in SlowTailer.instances[-3:])
    assert counts[0] >= 1  # nobody starved after three ticks
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py -v -k "gates_on_eof or idle_finish_requires or round_robin or before_eof"`
Expected: FAIL (finishes fire regardless of `at_eof`; no `tick_deadline_secs` kwarg).

- [ ] **Step 3: Implement** — in `service.py`:

3a. `__init__`: add keyword args `tick_deadline_secs: float = 30.0` (store as `self.tick_deadline_secs`) and initialize `self._cursor: str | None = None`.

3b. Replace the polling half of `tick()` (everything from `polled = 0` down) with:

```python
        polled = 0
        order = sorted(live_keys)
        if self._cursor in order:
            i = order.index(self._cursor)
            order = order[i:] + order[:i]
        deadline = self.time_func() + self.tick_deadline_secs
        self._cursor = None
        for idx, key in enumerate(order):
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
                del self._tailers[key]
                continue
            except RedisError:
                raise
            except OSError:
                logger.exception("claude transcript tailer filesystem failure", extra={"tailer_key": key})
                state.failing = True
                continue
            except Exception:
                logger.exception("claude transcript tailer failed", extra={"tailer_key": key})
                state.failing = True
                continue
            state.failing = bool(getattr(state.tailer, "emit_failing", False))
            polled += 1
            now = self.time_func()
            at_eof = bool(getattr(state.tailer, "at_eof", True))
            if emitted or getattr(state.tailer, "progressed", False):
                # Offset progress IS activity (spec §A Finish paths, panel r3
                # agy P1). Synthetic task_continuing does not reach here —
                # poll() returns it as emitted only when nothing was read, and
                # then progressed is False and emitted counts it... see test
                # test_idle_finish_activity_sources for the pinned rule.
                state.last_activity = now
            if getattr(state.tailer, "completed", False):
                if at_eof:
                    self._finish_once(state)
                continue
            if key.startswith("cold:") and self._cold_seat_completed(state):
                if at_eof:
                    self._finish_once(state)
                    self._delete_cold_seat_files(state)
                continue
            if (
                key.startswith("cold:")
                and at_eof
                and now - state.last_activity >= self.idle_finish_secs
            ):
                self._finish_once(state)
        return polled
```

IMPORTANT subtlety (`task_continuing` vs activity — spec §A, panel r4 cold-Opus P2): the real tailer emits `task_continuing` only when NO bytes were read (`progressed is False`), but it returns `emitted == 1` for it, which would bump `last_activity` via the `emitted or ...` condition. Fix at the source of truth: in `tailer.py` (one-line change, include in THIS task), `_maybe_emit_continuing`'s return value must not count as activity — have `poll()` track it separately:

```python
        else:
            self.progressed = False
            if emitted == 0:
                self._maybe_emit_continuing()  # liveness ping only — NOT activity, NOT counted in emitted
```

The pre-existing test this breaks is `tests/claude_tail/test_tailer.py::test_quiet_poll_emits_task_continuing_after_start` (search for `task_continuing` in the file): change its `assert count == 1` to `assert count == 0` and keep/strengthen its event assertion via `_event_types(redis)` (the ping must still be EMITTED — only the return-value accounting changes; plan panel, cold-Opus P2-2 named this edit explicitly so no worker has to infer it). Add the pinning test:

```python
def test_idle_finish_activity_sources(tmp_path):
    # task_continuing must NOT defeat idle-finish (spec §A, panel r4 cold-Opus P2)
    cold_dir, out = _write_cold(tmp_path)
    clock = {"now": 0.0}
    service = Service(
        redis=FakeRedis(), prefix="agent_scratch:", registry_path=str(tmp_path / "empty.json"),
        cold_dir=str(cold_dir), tailer_cls=FakeTailer, idle_finish_secs=10.0,
        time_func=lambda: clock["now"],
    )
    service.tick()
    tailer = FakeTailer.instances[-1]
    tailer.at_eof = True
    tailer.progressed = False
    # FakeTailer.poll returns 0 — a real tailer emitting ONLY task_continuing
    # also reports emitted 0 after this task's tailer change.
    clock["now"] = 50.0
    service.tick()
    assert next(iter(service._tailers.values())).finished is True
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py tests/claude_tail/test_tailer.py -v`
Expected: ALL PASS (both files — the tailer change is covered by existing continuing-interval tests; update them per the note above if they pinned the return-value counting).

- [ ] **Step 5: Deny-proof (throwaway)**

Remove the `at_eof` gate from the cold-sidecar branch → `test_cold_sidecar_completed_does_not_finish_before_eof` RED (spec §E 5d hinge). Restore; re-run green. Report result.

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/service.py src/agent_redis_bridge/claude_tail/tailer.py tests/claude_tail/test_service.py tests/claude_tail/test_tailer.py
git commit -m "feat(claude-tail): at_eof-gated finishes, tick deadline with RR cursor, progress-as-activity (CT-1 A)"
```

---

### Task 6: Service — durable draining records

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/service.py`
- Test: `tests/claude_tail/test_service.py`

**Interfaces:**
- Consumes: Tasks 3-5 (`at_eof`, finish gating, tick structure).
- Produces: `_TailerSpec.draining: bool = False`; draining key `f"{prefix}claude:draining:{session_id}"`; `Service.__init__` kwarg `draining_ttl_secs: int = 604800`; discovery union + prune; deletion on ANY terminal finish; flap supersede. Task 7's hook writes the same key shape.

- [ ] **Step 1: Extend fakes + write the failing tests**

`FakeRedis` in `test_service.py` needs: `delete`, `scan_iter`, TTL-aware `set`. Extend it:

```python
class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.ttls.pop(key, None)

    def scan_iter(self, match=None):
        import fnmatch
        for key in list(self.values):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    def hgetall(self, key):
        return {}
```

Tests (spec §E 5i):

```python
DRAIN_KEY = "agent_scratch:claude:draining:s1"


def _registry_with(tmp_path, transcript):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps([{"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}]), encoding="utf-8")
    return registry


def test_deregister_mid_backlog_writes_record_and_keeps_draining(tmp_path):
    # 5i(a): registry record removed mid-backlog -> draining record written,
    # tailer REMAINS polled, drains to EOF across ticks, finishes, record gone.
    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    registry = _registry_with(tmp_path, transcript)
    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), tailer_cls=FakeTailer)
    service.tick()
    tailer = FakeTailer.instances[-1]
    tailer.at_eof = False  # backlog remains

    registry.write_text("[]", encoding="utf-8")  # deregister
    service.tick()
    assert redis.get(DRAIN_KEY) is not None  # unconditional fallback write
    assert tailer.poll_count >= 2  # still polled

    service.tick()
    assert tailer.poll_count >= 3  # rediscovered via the record, still polled

    tailer.at_eof = True
    service.tick()
    assert redis.get(DRAIN_KEY) is None  # deleted at the at_eof finish
    assert not service._tailers  # drained and gone


def test_restart_rediscovers_from_draining_record(tmp_path):
    # 5i(b): fresh service (restart) rediscovers via the record and resumes.
    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    record = {"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=604800)
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")

    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), tailer_cls=FakeTailer)
    service.tick()
    tailer = FakeTailer.instances[-1]
    assert tailer.path == str(transcript)

    tailer.at_eof = True
    service.tick()
    assert redis.get(DRAIN_KEY) is None
    assert any(t.finished for t in [tailer])


def test_reregister_during_drain_supersedes(tmp_path):
    # 5i(c): flap -> single tailer, draining record dropped, no finish.
    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    record = {"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=604800)
    registry = _registry_with(tmp_path, transcript)  # registry HAS the session

    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), tailer_cls=FakeTailer)
    service.tick()
    assert redis.get(DRAIN_KEY) is None  # registry supersedes; record dropped
    assert len([t for t in FakeTailer.instances if t.path == str(transcript)]) == 1
    assert not next(iter(service._tailers.values())).finished


def test_transcript_deleted_mid_drain_prunes_record_one_finish(tmp_path):
    # 5i(d): vanished transcript -> record pruned, exactly ONE finish, no
    # per-tick tailer re-creation churn (panel r5 convergent).
    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    record = {"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=604800)
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), tailer_cls=FakeTailer)
    service.tick()  # draining tailer exists
    FakeTailer.instances[-1].at_eof = False  # STILL DRAINING — without this
    # the at_eof finish deletes the record on tick 1 and the FileNotFound
    # path is never exercised (plan panel, cold-Opus P1-3: vacuous test whose
    # deny-proof could not go red).
    n_tailers = len(FakeTailer.instances)

    transcript.unlink()
    FakeTailer.instances[-1].poll_exc = FileNotFoundError()
    service.tick()
    assert redis.get(DRAIN_KEY) is None  # deleted on the FileNotFound finish

    service.tick()
    service.tick()
    assert len(FakeTailer.instances) == n_tailers  # no re-creation churn


def test_restart_with_missing_transcript_deletes_record_silently(tmp_path):
    # plan panel, codex P1: the restart path (record exists, transcript gone,
    # NO in-memory tailer) must delete the record without creating a tailer,
    # without a finish (no lifecycle ever started in this process), without
    # churn or crash. Spec §A prune bullet, restart parenthetical.
    redis = FakeRedis()
    record = {"session_id": "s1", "transcript_path": str(tmp_path / "gone.jsonl"), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=604800)
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    FakeTailer.instances.clear()
    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), tailer_cls=FakeTailer)

    service.tick()
    assert redis.get(DRAIN_KEY) is None  # pruned
    assert FakeTailer.instances == []  # no tailer ever created
    service.tick()  # no churn, no crash
    assert FakeTailer.instances == []


def test_draining_ttl_refreshed_each_tick(tmp_path):
    redis = FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    record = {"session_id": "s1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"}
    redis.set(DRAIN_KEY, json.dumps(record), ex=10)  # stale TTL
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    service = Service(redis=redis, prefix="agent_scratch:", registry_path=str(registry), tailer_cls=FakeTailer)
    FakeTailer.instances.clear()
    service.tick()
    FakeTailer.instances[-1].at_eof = False
    service.tick()
    assert redis.ttls[DRAIN_KEY] == 604800  # refreshed while draining
```

Note on 5i(e) (final-burst race): with the daemon-side unconditional write, the race window is the HOOK's — covered in Task 7's test. The daemon-side unconditionality is already pinned by `test_deregister_mid_backlog_writes_record_and_keeps_draining` (no `at_eof` condition on the write).

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py -v -k "draining or deregister or rediscovers or supersedes or mid_drain"`
Expected: FAIL.

- [ ] **Step 3: Implement** — in `service.py`:

3a. `_TailerSpec` gains `draining: bool = False`. `__init__` gains `draining_ttl_secs: int = 604800` (store on self).

3b. Helpers:

```python
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
```

(All these ops raise `RedisError` through — spec §A names the draining ops in the infra-crash enumeration; do NOT wrap them in try/except.)

3c. In `_discover_specs`, after the warm-registry loop and BEFORE the cold loop, add the draining union:

```python
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
```

3d. In `tick()`'s key-drop loop, replace the unconditional finish for warm keys:

```python
        for key in list(self._tailers):
            state = self._tailers[key]
            if key in live_keys:
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
```

3e. In the polling loop (Task 5's structure): after computing `at_eof`, add the draining branches —

```python
            spec = specs.get(key)
            if spec is not None and spec.draining:
                self._refresh_draining_ttl(key)
                if at_eof:
                    self._finish_once(state)
                    self._delete_draining_record(key)
                    del self._tailers[key]
                    continue
```

and in the `except FileNotFoundError:` arm add `self._delete_draining_record(key)` right after `self._finish_once(state)`.

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Deny-proofs (throwaway)**

1. Make the draining state memory-only (skip `_persist_draining_record`'s redis write) → `test_restart_rediscovers_from_draining_record`... that test seeds the key directly, so instead: `test_deregister_mid_backlog_writes_record_and_keeps_draining` RED at the `redis.get(DRAIN_KEY) is not None` assert. Restore.
2. Scope record deletion back to at_eof-only (remove `_delete_draining_record` from the FileNotFound arm AND the no-tailer prune) → `test_transcript_deleted_mid_drain_prunes_record_one_finish` RED. Restore.

Re-run green; report results.

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/service.py tests/claude_tail/test_service.py
git commit -m "feat(claude-tail): durable draining records — union into discovery, delete on any terminal finish (CT-1 A)"
```

---

### Task 7: SessionEnd hook — write-then-delete handoff

**Files:**
- Modify: `scripts/claude_tail_hooks/common.py`
- Modify: `scripts/claude_tail_hooks/session_end.py`
- Test: `tests/claude_tail/test_hooks.py`

**Interfaces:**
- Consumes: the draining key shape from Task 6 (`{prefix}claude:draining:{session_id}`).
- Produces: `common.copy_redis_record_to_draining(client, session_id, *, ttl_secs=604800) -> bool`; `session_end` calls it BEFORE `remove_redis_record` (redis-registry mode only; file-registry mode relies on the daemon fallback — document with a comment).

- [ ] **Step 1: Write the failing test** (append to `tests/claude_tail/test_hooks.py` — inspect its existing fake-redis/session_end fixtures first and reuse their idioms; the test below assumes a `FakeRedis`-style client with `hset/hget/hdel/set/get`; adapt names to the file's existing fake)

```python
def test_session_end_writes_draining_record_before_removing_registry(monkeypatch):
    # spec §A (panel r6 codex P1): hook-side write-then-delete closes the
    # crash window between registry removal and the daemon's next tick.
    from claude_tail_hooks import common, session_end

    class Client:
        def __init__(self):
            self.hashes = {"agent_scratch:claude:registry": {"s1": json.dumps({"session_id": "s1", "transcript_path": "/tmp/t.jsonl"})}}
            self.values = {}
            self.ops = []

        def hget(self, key, field):
            self.ops.append(("hget", key, field))
            return self.hashes.get(key, {}).get(field)

        def hdel(self, key, field):
            self.ops.append(("hdel", key, field))
            self.hashes.get(key, {}).pop(field, None)

        def set(self, key, value, ex=None):
            self.ops.append(("set", key))
            self.values[key] = value

    client = Client()
    monkeypatch.delenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("AGENT_REDIS_URL", "redis://ignored")
    monkeypatch.setattr(common, "redis_client", lambda: client)
    monkeypatch.setattr(session_end, "redis_client", lambda: client)

    rc = session_end.main([json.dumps({"session_id": "s1"})])

    assert rc == 0
    assert client.values["agent_scratch:claude:draining:s1"]  # record written
    assert "s1" not in client.hashes["agent_scratch:claude:registry"]  # then removed
    set_idx = client.ops.index(("set", "agent_scratch:claude:draining:s1"))
    hdel_idx = client.ops.index(("hdel", "agent_scratch:claude:registry", "s1"))
    assert set_idx < hdel_idx  # WRITE-THEN-DELETE ordering is the whole point
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src:scripts .venv/bin/python -m pytest tests/claude_tail/test_hooks.py -v -k draining`
(Match the PYTHONPATH the existing hook tests use — check the top of `test_hooks.py`/`conftest` for how `claude_tail_hooks` is imported and mirror it.)
Expected: FAIL (no draining record written).

- [ ] **Step 3: Implement**

3a. `scripts/claude_tail_hooks/common.py` — add:

```python
def draining_redis_key(session_id: str, prefix: str | None = None) -> str:
    return f"{prefix if prefix is not None else os.environ.get('AGENT_REDIS_PREFIX', 'agent_scratch:')}claude:draining:{session_id}"


def copy_redis_record_to_draining(client, session_id: str, *, ttl_secs: int = 604800) -> bool:
    """Copy the registry record to the durable draining key.

    Called by session_end BEFORE remove_redis_record: if the daemon dies in
    the window between registry removal and its next tick, the fresh process
    rediscovers the transcript from this record (CT-1 spec §A, panel r6). A
    crash between the copy and the removal leaves both records — the daemon's
    flap rule (registry supersedes) makes that safe.
    """
    raw = client.hget(registry_redis_key(), session_id)
    if raw is None:
        return False
    if isinstance(raw, bytes):
        raw = raw.decode()
    client.set(draining_redis_key(session_id), raw, ex=ttl_secs)
    return True
```

3b. `scripts/claude_tail_hooks/session_end.py` — import `copy_redis_record_to_draining` alongside the existing imports and change the redis branch of `_main`:

```python
        client = redis_client()
        if client is None:
            raise ValueError("ARB_CLAUDE_TAIL_REGISTRY_PATH or AGENT_REDIS_URL is required")
        copy_redis_record_to_draining(client, session_id)
        remove_redis_record(client, session_id)
```

(File-registry mode is unchanged: no draining store exists there; the daemon-side fallback in Task 6 covers it. Leave a one-line comment saying so.)

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src:scripts .venv/bin/python -m pytest tests/claude_tail/test_hooks.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Deny-proof (throwaway)**

Revert the hook to delete-only (drop the `copy_redis_record_to_draining` call) → the ordering test RED (spec §E 5i(f) hinge). Restore; re-run green; report.

- [ ] **Step 6: Commit**

```bash
git add scripts/claude_tail_hooks/common.py scripts/claude_tail_hooks/session_end.py tests/claude_tail/test_hooks.py
git commit -m "feat(claude-tail): SessionEnd hook write-then-delete draining handoff (CT-1 A, panel r6)"
```

---

### Task 8: Service heartbeat writer

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/service.py`
- Test: `tests/claude_tail/test_service.py`

**Interfaces:**
- Consumes: Task 4's `failing` state; Task 3's `skipped_lines`.
- Produces: `Service.__init__` kwargs `heartbeat_label: str | None = None`, `stale_after_s: int = 330`; heartbeat SET at tick start AND end, throttled to one write per 10s, key `f"{prefix}tail:heartbeat:{label}"` on `live_redis`, TTL 604800, 8-field JSON. Task 10's gateway reads this exact shape.

- [ ] **Step 1: Write the failing tests**

```python
def _heartbeats(redis):
    return [(k, v) for (k, v) in redis.values.items() if "tail:heartbeat:" in k]


def test_heartbeat_written_with_eight_fields_and_ttl(tmp_path):
    redis = FakeRedis()
    live = FakeRedis()
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    clock = {"now": 0.0}
    service = Service(
        redis=redis, live_redis=live, prefix="agent_scratch:",
        registry_path=str(registry), tailer_cls=FakeTailer,
        heartbeat_label="claude-tail.test", stale_after_s=330,
        time_func=lambda: clock["now"], wall_time_func=lambda: 1000.0,
    )
    service.tick()

    key = "agent_scratch:tail:heartbeat:claude-tail.test"
    payload = json.loads(live.values[key])
    assert set(payload) == {"ts", "pid", "started_at", "tailers", "failing_tailers", "skipped_lines", "last_emit_at", "stale_after_s"}
    assert payload["stale_after_s"] == 330
    assert payload["tailers"] == 0
    assert live.ttls[key] == 604800


def test_heartbeat_throttled_to_ten_seconds(tmp_path):
    redis, live = FakeRedis(), FakeRedis()
    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    clock = {"now": 0.0}
    service = Service(
        redis=redis, live_redis=live, prefix="agent_scratch:", registry_path=str(registry),
        tailer_cls=FakeTailer, heartbeat_label="claude-tail.test",
        time_func=lambda: clock["now"], wall_time_func=lambda: 1000.0,
    )

    class CountingLive(FakeRedis):
        pass

    writes = []
    orig_set = live.set
    live.set = lambda k, v, ex=None: (writes.append(k), orig_set(k, v, ex=ex))

    service.tick()  # start+end within 0s -> exactly one write
    assert len([w for w in writes if "heartbeat" in w]) == 1
    clock["now"] = 5.0
    service.tick()  # still inside the 10s throttle
    assert len([w for w in writes if "heartbeat" in w]) == 1
    clock["now"] = 11.0
    service.tick()
    assert len([w for w in writes if "heartbeat" in w]) == 2


def test_heartbeat_counts_failing_and_skipped(tmp_path):
    # uses the _registry_with helper the draining tests added to this file
    redis, live = FakeRedis(), FakeRedis()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x\n", encoding="utf-8")
    registry = _registry_with(tmp_path, transcript)
    clock = {"now": 0.0}
    service = Service(
        redis=redis, live_redis=live, prefix="agent_scratch:", registry_path=str(registry),
        tailer_cls=FakeTailer, heartbeat_label="claude-tail.test",
        time_func=lambda: clock["now"], wall_time_func=lambda: 1000.0,
    )
    service.tick()
    tailer = FakeTailer.instances[-1]
    tailer.emit_failing = True
    tailer.skipped_lines = 7
    clock["now"] = 11.0
    service.tick()
    payload = json.loads(live.values["agent_scratch:tail:heartbeat:claude-tail.test"])
    assert payload["failing_tailers"] == 1
    assert payload["skipped_lines"] == 7
    assert payload["tailers"] == 1


def test_heartbeat_write_failure_raises(tmp_path):
    class DeadLive(FakeRedis):
        def set(self, key, value, ex=None):
            if "heartbeat" in key:
                raise RedisConnectionError("bus down")
            super().set(key, value, ex=ex)

    registry = tmp_path / "registry.json"
    registry.write_text("[]", encoding="utf-8")
    service = Service(
        redis=FakeRedis(), live_redis=DeadLive(), prefix="agent_scratch:",
        registry_path=str(registry), tailer_cls=FakeTailer, heartbeat_label="claude-tail.test",
    )
    with pytest.raises(RedisConnectionError):
        service.tick()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py -v -k heartbeat`
Expected: FAIL (no `heartbeat_label` kwarg).

- [ ] **Step 3: Implement** — in `service.py`:

3a. `__init__` gains `heartbeat_label: str | None = None`, `stale_after_s: int = 330`; store both; also:

```python
        self._last_heartbeat_at = float("-inf")
        self._last_heartbeat_fields: dict | None = None
        self._started_at_iso = _utc_iso(self.wall_time_func())
        self._last_emit_wall: float | None = None
        self._skipped_lines_retired = 0
```

with the helper at module level:

```python
def _utc_iso(epoch_seconds: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()
```

3b. The writer (all fields STRUCTURAL — any future free-text field must route through `redact()`, spec §C / GLM G2):

```python
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
```

3c. In `tick()`: call `self._tick_heartbeat()` as the FIRST statement (start beat) and `self._tick_heartbeat(end=True)` as the LAST statement before `return polled` (end beat). In the polling loop, after a successful poll with `emitted > 0` (real events — remember `task_continuing` no longer counts in `emitted`): `self._last_emit_wall = self.wall_time_func()`.

3d. In `_finish_once`, before setting `state.finished = True`, accumulate the retired counter (monotonicity — plan panel, agy P1):

```python
        self._skipped_lines_retired += int(getattr(state.tailer, "skipped_lines", 0))
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/service.py tests/claude_tail/test_service.py
git commit -m "feat(claude-tail): output-liveness heartbeat on the live bus, start+end beats, 8-field payload (CT-1 C)"
```

---

### Task 9: main() wiring — logging rotation, watchdog start, env plumbing

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/__main__.py`
- Modify: `src/agent_redis_bridge/claude_tail/service.py` (`build_service_from_env` additions)
- Test: `tests/claude_tail/test_service.py`

**Interfaces:**
- Consumes: Tasks 2 (Watchdog), 4 (run_loop watchdog kwarg), 8 (heartbeat kwargs).
- Produces: `configure_logging(label) -> str` in `__main__.py`; `build_watchdog(interval) -> Watchdog | None`; `build_service_from_env(**overrides)` passes heartbeat/deadline/stale kwargs from env; `main()` wires everything.

- [ ] **Step 1: Write the failing tests** (append to `tests/claude_tail/test_service.py`)

```python
def test_configure_logging_creates_dir_and_rotating_handler(tmp_path, monkeypatch):
    import logging

    from agent_redis_bridge.claude_tail.__main__ import configure_logging

    target = tmp_path / "deep" / "nested" / "tail.log"
    monkeypatch.setenv("ARB_CLAUDE_TAIL_LOG_FILE", str(target))
    root = logging.getLogger("agent_redis_bridge")
    before = list(root.handlers)
    try:
        path = configure_logging("claude-tail.test")
        assert path == str(target)
        assert target.parent.is_dir()  # makedirs (spec §D, panel r3 agy P2)
        handler = [h for h in root.handlers if h not in before][-1]
        from logging.handlers import RotatingFileHandler

        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 3
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_build_watchdog_env(monkeypatch):
    from agent_redis_bridge.claude_tail.__main__ import build_watchdog

    monkeypatch.setenv("ARB_CLAUDE_TAIL_WATCHDOG_SECS", "0")
    assert build_watchdog(1.0) is None  # 0 disables (spec §B)

    monkeypatch.setenv("ARB_CLAUDE_TAIL_WATCHDOG_SECS", "300")
    wd = build_watchdog(1.0)
    assert wd is not None and wd.effective_threshold == 300.0


def test_build_service_from_env_wires_heartbeat_and_budgets(monkeypatch):
    from agent_redis_bridge.claude_tail.tailer import TranscriptTailer

    # build_service_from_env mutates the CLASS attrs (deployment singleton);
    # register them with monkeypatch so this test's 42/0.5 values are
    # restored at teardown and do not leak into other tests (plan panel,
    # agy/grok/cold-Opus P2 convergent).
    monkeypatch.setattr(TranscriptTailer, "poll_budget_lines", TranscriptTailer.poll_budget_lines)
    monkeypatch.setattr(TranscriptTailer, "poll_budget_secs", TranscriptTailer.poll_budget_secs)
    monkeypatch.setenv("AGENT_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_HEARTBEAT_LABEL", "claude-tail.envtest")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_TICK_DEADLINE_SECS", "12")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_POLL_BUDGET_LINES", "42")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_POLL_BUDGET_SECS", "0.5")
    service = build_service_from_env(stale_after_s=390)
    assert service.heartbeat_label == "claude-tail.envtest"
    assert service.tick_deadline_secs == 12.0
    assert service.stale_after_s == 390
    from agent_redis_bridge.claude_tail.tailer import TranscriptTailer

    assert TranscriptTailer.poll_budget_lines == 42
    assert TranscriptTailer.poll_budget_secs == 0.5
```

(Check how existing `build_service_from_env` tests set env — mirror their AGENT_REDIS_* setup if `AGENT_REDIS_URL` alone is not the idiom.)

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py -v -k "configure_logging or build_watchdog or wires_heartbeat"`
Expected: FAIL (imports missing).

- [ ] **Step 3: Implement**

3a. `__main__.py` — add:

```python
import logging
import logging.handlers
import socket

from .watchdog import Watchdog


def configure_logging(label: str) -> str:
    path = os.environ.get("ARB_CLAUDE_TAIL_LOG_FILE") or os.path.expanduser(f"~/Library/Logs/claude-tail/{label}.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("agent_redis_bridge")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return path


def build_watchdog(interval: float):
    raw = float(os.environ.get("ARB_CLAUDE_TAIL_WATCHDOG_SECS", "300"))
    if raw <= 0:
        return None
    return Watchdog(raw, interval)


def heartbeat_label() -> str:
    return os.environ.get("ARB_CLAUDE_TAIL_HEARTBEAT_LABEL") or f"claude-tail.{socket.gethostname()}"


def main() -> int:
    label = heartbeat_label()
    configure_logging(label)
    interval = float(os.environ.get("ARB_CLAUDE_TAIL_INTERVAL_SECS", "1.0"))
    watchdog = build_watchdog(interval)
    stale_after = int((watchdog.effective_threshold if watchdog else 300.0) + 30.0)
    service = build_service_from_env(heartbeat_label=label, stale_after_s=stale_after)
    if watchdog is not None:
        watchdog.mark_tick()
        watchdog.start()
    run_loop(service, interval=interval, watchdog=watchdog)
```

3b. `service.py::build_service_from_env` — accept `**overrides` and thread env:

```python
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
        heartbeat_label=os.environ.get("ARB_CLAUDE_TAIL_HEARTBEAT_LABEL") or None,
        tick_deadline_secs=float(os.environ.get("ARB_CLAUDE_TAIL_TICK_DEADLINE_SECS", "30")),
    )
    kwargs.update(overrides)
    return Service(**kwargs)
```

(`from .tailer import TranscriptTailer` is already imported in service.py.)

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_service.py tests/claude_tail/test_watchdog.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/__main__.py src/agent_redis_bridge/claude_tail/service.py tests/claude_tail/test_service.py
git commit -m "feat(claude-tail): main() wiring — rotating logs, watchdog start, env plumbing (CT-1 B/D)"
```

---

### Task 10: Gateway — tees in /orchestrators + UI chip + CHANGELOG

**Files:**
- Modify: `src/arb_memory/visibility.py`
- Modify: `src/arb_memory/static/app.js`
- Modify: `src/arb_memory/static/index.html`
- Modify: `CHANGELOG.md`
- Test: `tests/claude_tail/test_visibility_tee.py` (module-level reduction; the route is exercised by existing visibility tests' auth coverage)

**Interfaces:**
- Consumes: Task 8's heartbeat key + payload shape.
- Produces: module-level `tee_states(redis_client, bus_prefix, labels, now) -> list[dict]` in `visibility.py`; `/orchestrators` response gains `"tees": [...]`; env `ARB_VIS_EXPECTED_TEES` (comma-separated labels; empty → startup WARNING + `tees: []`).

- [ ] **Step 1: Write the failing tests** (append to `tests/claude_tail/test_visibility_tee.py`)

```python
from datetime import datetime, timedelta, timezone

from arb_memory.visibility import tee_states


class TeeFakeRedis:
    def __init__(self, values):
        self.values = values

    def mget(self, keys):
        return [self.values.get(k) for k in keys]


def _hb(ts, stale_after=330, **extra):
    payload = {
        "ts": ts.isoformat(), "pid": 1, "started_at": ts.isoformat(),
        "tailers": 1, "failing_tailers": 0, "skipped_lines": 0,
        "last_emit_at": None, "stale_after_s": stale_after,
    }
    payload.update(extra)
    return json.dumps(payload)


def test_tee_states_fresh_stale_missing():
    now = datetime.now(timezone.utc)
    redis = TeeFakeRedis({
        "agent_scratch:tail:heartbeat:a": _hb(now - timedelta(seconds=10)),
        "agent_scratch:tail:heartbeat:b": _hb(now - timedelta(seconds=1000)),
    })
    out = tee_states(redis, "agent_scratch:", ["a", "b", "c"], now)
    by = {t["label"]: t for t in out}
    assert by["a"]["state"] == "fresh"
    assert by["b"]["state"] == "stale"
    assert by["b"]["ts"]  # "stale since <ts>" evidence retained (spec §C)
    assert by["c"]["state"] == "missing"


def test_tee_states_staleness_uses_payload_stale_after_s():
    # spec §C (panel r2 cold-Opus P2-1): the daemon's own stale_after_s wins,
    # not a hardcoded 330.
    now = datetime.now(timezone.utc)
    redis = TeeFakeRedis({
        "agent_scratch:tail:heartbeat:slow": _hb(now - timedelta(seconds=500), stale_after=1200),
    })
    out = tee_states(redis, "agent_scratch:", ["slow"], now)
    assert out[0]["state"] == "fresh"  # 500s old but stale_after 1200


def test_tee_states_malformed_payload_is_stale_not_crash():
    now = datetime.now(timezone.utc)
    redis = TeeFakeRedis({"agent_scratch:tail:heartbeat:x": "{not json"})
    out = tee_states(redis, "agent_scratch:", ["x"], now)
    assert out[0]["state"] == "stale"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_visibility_tee.py -v -k tee_states`
Expected: FAIL (`ImportError: tee_states`).

- [ ] **Step 3: Implement**

3a. `src/arb_memory/visibility.py` — module-level function (near `build_orchestrator_list`):

```python
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
```

3b. In `build_visibility_app` (where `last_seen_key` etc. are set up), compute once and warn loudly when empty (spec §C, panel r3 grok P1):

```python
    expected_tees = _expected_tee_labels()
    if not expected_tees:
        logger.warning("ARB_VIS_EXPECTED_TEES is empty — tee staleness surfacing is INERT; set it to e.g. claude-tail.bridge-dev")
```

3c. Extend `_orchestrators_blocking` to return a tuple and the route to include tees:

```python
    def _orchestrators_blocking():
        ...existing body unchanged, but where it currently `return`s a list,
        capture it as `result` and end with:
        tees = tee_states(redis_client, bus_prefix, expected_tees, datetime.now(timezone.utc))
        return result, tees
```

(BOTH return points — the hash path and the `_orchestrators_from_tail()` fallback — must return the tuple.) And in `orchestrators()`:

```python
        result, tees = await anyio.to_thread.run_sync(_orchestrators_blocking)
        ...
        return JSONResponse({"orchestrators": result, "tees": tees})
```

3d. UI chip. In `src/arb_memory/static/index.html`, add next to the orchestrator `<select>` field (inside the same `.orchestrator-field` container):

```html
      <span id="tees-status" title="claude-tail tee heartbeat"></span>
```

with CSS in the existing `<style>` block:

```css
      #tees-status{ margin-left:8px; font-size:12px; }
      #tees-status .tee-fresh{ color:#3fb950; }
      #tees-status .tee-stale, #tees-status .tee-missing{ color:#f85149; font-weight:600; }
```

In `src/arb_memory/static/app.js`, inside `loadOrchestrators()` right after `const payload = await response.json();`:

```javascript
      renderTees(payload.tees || []);
```

and add the function near the other render helpers:

```javascript
    function renderTees(tees) {
      const el = document.getElementById("tees-status");
      if (!el) return;
      el.replaceChildren(
        ...tees.map((tee) => {
          const span = document.createElement("span");
          span.className = "tee-" + tee.state;
          if (tee.state === "fresh") {
            span.textContent = "tee ✓" + (tee.tailers === 0 ? " (0 tailers)" : "");
          } else if (tee.state === "stale") {
            span.textContent = "tee STALE since " + (tee.ts || "?");
          } else {
            span.textContent = "tee MISSING (" + tee.label + ")";
          }
          return span;
        })
      );
    }
```

3e. `CHANGELOG.md` — add at the top of the unreleased/current section:

```markdown
- CT-1: claude-tail fail-loud (spec docs/superpowers/specs/2026-07-11-ct1-claude-tail-fail-loud-design.md,
  6-round design panel). WHAT: RedisError now crashes the tee daemon (KeepAlive revives)
  while parse errors skip and offset corruption self-heals; a lock-free watchdog
  os._exit(86)s a hung loop; chunked budgeted polls with at_eof-gated finishes and
  durable draining records (SessionEnd hook hands off write-then-delete); an
  output-liveness heartbeat (tailers/failing_tailers/skipped_lines) on the live bus,
  surfaced by the visibility gateway via ARB_VIS_EXPECTED_TEES; RotatingFileHandler logs.
  WHY: the tee zombied 2026-07-06→10 (process alive, zero events, KeepAlive inert,
  discovered by a human) — every failure mode must now crash or be legible on the bus.
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_visibility_tee.py -v`
Expected: ALL PASS.
Also import-check the gateway module: `PYTHONPATH=src .venv/bin/python -c "import arb_memory.visibility as v; assert hasattr(v, 'tee_states')"`.

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/visibility.py src/arb_memory/static/app.js src/arb_memory/static/index.html tests/claude_tail/test_visibility_tee.py CHANGELOG.md
git commit -m "feat(visibility): tee heartbeat surfacing — expected-labels MGET, fresh/stale/missing chip (CT-1 C)"
```

---

## Post-implementation (orchestrator-owned, NOT part of worker tasks)

Live gate per spec §E (executed by the orchestrator after merge, before closing CT-1):
1. Kickstart the seat; heartbeat fresh within 10s with `tailers ≥ 1`; gateway chip renders the configured label.
2. Throwaway instance with black-hole `ARB_LIVE_REDIS_URL` exits nonzero within seconds.
3. Orchestrator events flow live.
4. Rotating log receives records; launchd stderr stays quiet.
Deploy notes: plist gains `ARB_CLAUDE_TAIL_HEARTBEAT_LABEL=claude-tail.bridge-dev` (bootout+bootstrap, not kickstart); gateway env gains `ARB_VIS_EXPECTED_TEES=claude-tail.bridge-dev`; one-time removal of the 54MB `/tmp/claude-tail.bridge-dev.launchd.err`.

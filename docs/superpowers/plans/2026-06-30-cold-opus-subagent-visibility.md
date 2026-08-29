# Cold-Opus Subagent Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make cold-Opus reviewer subagents (Claude Code's native Agent/Task tool, e.g.
`subagent_type: code-reviewer-report-writer`) appear as live seats in arb-watch by registering
them with `claude_tail`'s existing cold-seat discovery mechanism via two new Claude Code hooks
(`SubagentStart`/`SubagentStop`), instead of the currently-broken assumption that such a transcript
already lands in the watched directory.

**Architecture:** Two new hook scripts drop a symlink (pointing at the subagent's real transcript,
derived from the parent session's already-known transcript path) plus a small JSON sidecar into
the directory `claude_tail`'s daemon already globs every poll tick — zero daemon-*discovery*
change needed. Two small, targeted changes to the daemon (`service.py`, `tailer.py`) read that
sidecar to carry the `orchestrator` field through correctly and to finish+clean up promptly on
completion instead of waiting on the 5-minute idle-finish fallback.

**Tech Stack:** Python 3.11+, pytest, the existing `agent_redis_bridge.claude_tail` package and
`scripts/claude_tail_hooks` hook scripts.

## Global Constraints

- `seat_id` for these seats MUST stay `cold-opus-<agent_id>` (the existing `cold_identity()`
  no-marker default) — do not introduce a custom seat_id format. (Spec § "Why not a custom
  seat_id".)
- Only `orchestrator` flows through the new sidecar to the daemon's identity — never `seat_id` or
  `run_id`. (Spec § Architecture.)
- The sidecar filename is `<agent_id>.arb-tail.json`, never `<agent_id>.meta.json` (collides with
  Claude Code's own same-named file in a different directory). (Spec § Architecture, step 5.)
- `subagent_stop.py` must NEVER delete the `.output` symlink or sidecar itself — only the daemon
  does, after confirming `completed: true` on an already-polled key. (Spec § "Why not
  delete-on-stop".)
- Every new hook script is wrapped in `fail_soft` (matches `scripts/claude_tail_hooks/common.py`'s
  existing convention) — observability plumbing must never block or crash the orchestrator or
  subagent.
- `ARB_CLAUDE_TAIL_COLD_AGENT_TYPES` env var (comma-separated `agent_type` allowlist) defaults to
  `code-reviewer-report-writer` when unset.
- Reference spec: `docs/superpowers/specs/2026-06-30-cold-opus-subagent-visibility-design.md`
  (panel-reviewed twice; read it for full rationale on every design decision below).

---

### Task 1: `identity_locked` guard in `TranscriptTailer`

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/tailer.py`
- Test: `tests/claude_tail/test_tailer.py`

**Interfaces:**
- Consumes: nothing new (only existing `agent_redis_bridge.claude_tail.identity.cold_identity`,
  `Identity`).
- Produces: `TranscriptTailer.__init__` gains a new keyword-only-by-convention parameter
  `identity_locked: bool = False`. When `True` at construction, the tailer's own first-line
  identity resolution (`_resolve_cold_identity`) becomes a permanent no-op for that tailer's
  lifetime — later tasks (Task 2) rely on passing `identity_locked=True` when a sidecar is found.

- [ ] **Step 1: Write the failing test — locked identity survives a later marker line**

Add to `tests/claude_tail/test_tailer.py` (after `test_cold_identity_marker_applies_to_opening_and_later_events`,
around line 199):

```python
def test_locked_cold_identity_is_not_overridden_by_a_later_marker(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "message": {"content": "[ARB_RUN:run-1 ARB_SEAT:cold-seat-1 ARB_ORCH:warm-orch] review this"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis()
    locked_identity = Identity(run_id="sess-x", task_id="agent-1", seat_id="cold-opus-agent-1", orchestrator="claude-bridge-dev")
    tailer = TranscriptTailer(
        str(transcript),
        locked_identity,
        OffsetStore(redis, "p:"),
        live_redis=redis,
        trace_redis=redis,
        prefix="agent_scratch:",
        redactor=_redactor,
        cold_agent_id="agent-1",
        cold_session_id="sess-x",
        identity_locked=True,
    )

    tailer.poll()

    live = [fields for key, fields, _ in redis.xadds if key.endswith("events:live")]
    # The marker says run_id=run-1/seat_id=cold-seat-1/orchestrator=warm-orch -- locked identity
    # must win, not the marker.
    assert {fields["run_id"] for fields in live} == {"sess-x"}
    assert {fields["seat_id"] for fields in live} == {"cold-opus-agent-1"}
    assert {fields["orchestrator"] for fields in live} == {"claude-bridge-dev"}
```

Add `Identity` to the existing import line at the top of the file:

```python
from agent_redis_bridge.claude_tail.identity import Identity, cold_identity, warm_identity
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_tailer.py::test_locked_cold_identity_is_not_overridden_by_a_later_marker -v`
Expected: FAIL with `TypeError: TranscriptTailer.__init__() got an unexpected keyword argument 'identity_locked'`

- [ ] **Step 3: Write the regression test for the upgrade path (must still pass unlocked)**

Add directly after the test from Step 1:

```python
def test_unlocked_cold_identity_still_upgrades_past_a_leading_drop_type_line(tmp_path):
    # Round-1 spec-review bug: a transcript that opens with a DROP_TYPES line (e.g. "system")
    # resolves the empty-marker fallback via _ensure_identity_resolved() before the real first
    # user line (carrying an ARB marker) ever arrives. identity_locked=False must still let that
    # later marker line upgrade the identity, exactly as it does today.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "system", "subtype": "init"},
        {"type": "user", "message": {"content": "[ARB_RUN:run-1 ARB_SEAT:cold-seat-1 ARB_ORCH:warm-orch] review this"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis()
    fallback = cold_identity("agent-1", "session-fallback", "")
    tailer = TranscriptTailer(
        str(transcript),
        fallback,
        OffsetStore(redis, "p:"),
        live_redis=redis,
        trace_redis=redis,
        prefix="agent_scratch:",
        redactor=_redactor,
        cold_agent_id="agent-1",
        cold_session_id="session-fallback",
        identity_locked=False,
    )

    tailer.poll()

    live = [fields for key, fields, _ in redis.xadds if key.endswith("events:live")]
    assert {fields["run_id"] for fields in live} == {"run-1"}
    assert {fields["seat_id"] for fields in live} == {"cold-seat-1"}
    assert {fields["orchestrator"] for fields in live} == {"warm-orch"}
```

- [ ] **Step 4: Run both new tests to verify they fail/pass as expected pre-implementation**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_tailer.py -k "locked_cold_identity or unlocked_cold_identity" -v`
Expected: `test_locked_cold_identity_is_not_overridden_by_a_later_marker` FAILs (same TypeError as
Step 2); `test_unlocked_cold_identity_still_upgrades_past_a_leading_drop_type_line` ALSO FAILs with
the same TypeError (both pass `identity_locked=...` to a constructor that doesn't accept it yet).

- [ ] **Step 5: Implement `identity_locked` in `TranscriptTailer`**

In `src/agent_redis_bridge/claude_tail/tailer.py`, change the `__init__` signature (currently
lines 32-44) to:

```python
    def __init__(
        self,
        path: str,
        identity: Identity,
        offset_store: OffsetStore,
        live_redis: Any,
        trace_redis: Any,
        prefix: str,
        redactor: Callable[[str], str],
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
        self.prefix = prefix
        self.trace_prefix = trace_prefix
        self.redactor = redactor
        self._cold_agent_id = cold_agent_id
        self._cold_session_id = cold_session_id
        self._identity_locked = identity_locked
        self._identity_resolved = identity_locked or cold_agent_id is None or cold_session_id is None
```

(This replaces the old `self._identity_resolved = cold_agent_id is None or cold_session_id is None`
line — the new line just adds `identity_locked or` in front of the existing condition. Everything
below that line in `__init__`, `self.lifecycle = Lifecycle()` onward, is unchanged.)

Then change `_resolve_cold_identity` (currently lines 173-177) to:

```python
    def _resolve_cold_identity(self, marker_text: str) -> None:
        if self._identity_locked:
            return
        if self._cold_agent_id is None or self._cold_session_id is None:
            return
        self.identity = cold_identity(self._cold_agent_id, self._cold_session_id, marker_text)
        self._identity_resolved = True
```

(Only the new `if self._identity_locked: return` line at the top is added; everything else in the
method is unchanged.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_tailer.py -v`
Expected: all tests PASS, including both new ones.

- [ ] **Step 7: Run the full claude_tail suite to check for regressions**

Run: `.venv/bin/python -m pytest tests/claude_tail -q`
Expected: all tests PASS (this only touches `tailer.py`; `service.py` doesn't pass
`identity_locked` yet, so its tests are unaffected by this task).

- [ ] **Step 8: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/tailer.py tests/claude_tail/test_tailer.py
git commit -m "feat(claude-tail): add identity_locked guard to TranscriptTailer

Lets a discovery-time-resolved identity (e.g. from a sidecar) survive the
tailer's own first-user-line marker resolution, without breaking the
existing empty-fallback-then-marker-upgrade path for unlocked tailers."
```

---

### Task 2: `service.py` — sidecar-aware discovery + prompt completion via the active poll loop

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/service.py`
- Test: `tests/claude_tail/test_service.py`

**Interfaces:**
- Consumes: `TranscriptTailer.__init__`'s `identity_locked` parameter (Task 1).
- Produces: `_TailerSpec` gains an `identity_locked: bool = False` field. `_discover_specs()`'s
  cold branch reads an optional `<cold_dir>/<agent_id>.arb-tail.json` sidecar (created by Task 3's
  `subagent_start.py`) to override `identity.orchestrator` and set `identity_locked=True`. `tick()`
  finishes and deletes the symlink+sidecar for any live cold key whose sidecar says `completed:
  true`, immediately after that tick's poll of it (no separate "final poll" call — the poll that
  already runs every tick is the final one).

- [ ] **Step 1: Update the existing `FakeTailer` test double to accept `identity_locked`**

`tests/claude_tail/test_service.py`'s `FakeTailer.__init__` (lines 25-37) does not accept
`identity_locked` yet. Once `_new_tailer` passes it (Step 4 below), every existing test in this
file using `FakeTailer` would break with `TypeError: unexpected keyword argument 'identity_locked'`
unless this is fixed first. Change the signature to:

```python
class FakeTailer:
    instances = []

    def __init__(
        self,
        path,
        identity,
        offset_store,
        live_redis,
        trace_redis,
        prefix,
        redactor,
        trace_prefix="",
        cold_agent_id=None,
        cold_session_id=None,
        identity_locked=False,
    ):
        self.path = path
        self.identity = identity
        self.cold_agent_id = cold_agent_id
        self.cold_session_id = cold_session_id
        self.identity_locked = identity_locked
        self.live_redis = live_redis
        self.trace_redis = trace_redis
        self.prefix = prefix
        self.trace_prefix = trace_prefix
        self.poll_count = 0
        self.finished = []
        self.completed = False
        FakeTailer.instances.append(self)
```

(Only the new `identity_locked=False` parameter and the new `self.identity_locked = identity_locked`
line are added; everything else is unchanged.)

- [ ] **Step 2: Run the existing suite to confirm this alone is a no-op change**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_service.py -v`
Expected: all tests PASS (the new parameter has a default, so no existing call site breaks).

- [ ] **Step 3: Write the failing test — sidecar overrides orchestrator and locks identity**

Add to `tests/claude_tail/test_service.py`, after `test_tick_discovers_warm_and_cold_tailers_of_right_kind`:

```python
def test_cold_sidecar_overrides_orchestrator_and_locks_identity(tmp_path):
    FakeTailer.instances = []
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-7.output").write_text("", encoding="utf-8")
    _write_json(cold_dir / "agent-7.arb-tail.json", {"orchestrator": "claude-bridge-dev", "completed": False})
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
    )

    service.tick()

    cold = FakeTailer.instances[0]
    assert cold.identity.orchestrator == "claude-bridge-dev"
    assert cold.identity.seat_id == "cold-opus-agent-7"  # unchanged default, not overridden
    assert cold.identity_locked is True


def test_cold_seat_without_sidecar_keeps_existing_unlocked_behavior(tmp_path):
    FakeTailer.instances = []
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-8.output").write_text("", encoding="utf-8")
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
    )

    service.tick()

    cold = FakeTailer.instances[0]
    assert cold.identity.orchestrator == ""
    assert cold.identity_locked is False


def test_cold_seat_with_malformed_sidecar_falls_back_to_unlocked(tmp_path):
    FakeTailer.instances = []
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-9.output").write_text("", encoding="utf-8")
    (cold_dir / "agent-9.arb-tail.json").write_text("{not valid json", encoding="utf-8")
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
    )

    service.tick()

    cold = FakeTailer.instances[0]
    assert cold.identity.orchestrator == ""
    assert cold.identity_locked is False
```

- [ ] **Step 4: Run to verify the new tests fail**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_service.py -k "sidecar" -v`
Expected: `test_cold_sidecar_overrides_orchestrator_and_locks_identity` FAILs (`AssertionError:
'' != 'claude-bridge-dev'` or similar — the override doesn't happen yet); the other two
(no-sidecar, malformed-sidecar) PASS already since that's today's behavior — that's fine, they're
explicit regression guards for behavior that must NOT change.

- [ ] **Step 5: Implement sidecar-aware discovery in `service.py`**

Add `replace` to the existing `dataclasses` import (currently `from dataclasses import dataclass`
at the top):

```python
from dataclasses import dataclass, replace
```

Add `identity_locked: bool = False` to `_TailerSpec` (currently lines 24-30):

```python
@dataclass
class _TailerSpec:
    key: str
    path: str
    identity: Identity
    cold_agent_id: str | None = None
    cold_session_id: str | None = None
    identity_locked: bool = False
```

Replace the cold-discovery block inside `_discover_specs()` (currently lines 134-148):

```python
        if self.cold_dir.exists():
            for path in sorted(self.cold_dir.glob("*.output")):
                if not self._is_recent(path):
                    continue
                agent_id = path.name.removesuffix(".output")
                if not agent_id:
                    continue
                session_id = agent_id
                identity = cold_identity(agent_id, session_id, "")
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
```

Add a new static helper method right after `_discover_specs` (before `_read_registry`):

```python
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
```

Update `_new_tailer` (currently lines 189-201) to pass the new field through:

```python
    def _new_tailer(self, spec: _TailerSpec) -> TranscriptTailer:
        return self.tailer_cls(
            spec.path,
            spec.identity,
            self.offset_store,
            live_redis=self.live_redis,
            trace_redis=self.trace_redis,
            prefix=self.prefix,
            redactor=self.redactor,
            trace_prefix=self.trace_prefix,
            cold_agent_id=spec.cold_agent_id,
            cold_session_id=spec.cold_session_id,
            identity_locked=spec.identity_locked,
        )
```

- [ ] **Step 6: Run to verify the sidecar tests pass**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_service.py -k "sidecar" -v`
Expected: all three PASS.

- [ ] **Step 7: Run the full service test file to check for regressions**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_service.py -v`
Expected: all tests PASS.

- [ ] **Step 8: Commit the discovery half**

```bash
git add src/agent_redis_bridge/claude_tail/service.py tests/claude_tail/test_service.py
git commit -m "feat(claude-tail): read a cold-seat sidecar to lock orchestrator identity

_discover_specs() now checks for an optional <agent_id>.arb-tail.json
sidecar alongside a cold seat's .output file. When present and valid, its
orchestrator field overrides the default and identity_locked=True is
passed to the tailer so it can't be clobbered by marker parsing. Absent or
malformed sidecar -> byte-for-byte unchanged existing behavior."
```

- [ ] **Step 9: Write the failing test — prompt completion via sidecar, not idle-finish**

Add to `tests/claude_tail/test_service.py`, after the sidecar-discovery tests just added:

```python
def test_cold_seat_completed_sidecar_finishes_and_deletes_files_same_tick(tmp_path):
    FakeTailer.instances = []
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    output_path = cold_dir / "agent-5.output"
    sidecar_path = cold_dir / "agent-5.arb-tail.json"
    output_path.write_text("x\n", encoding="utf-8")
    _write_json(sidecar_path, {"orchestrator": "claude-bridge-dev", "completed": False})
    now = [100.0]
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
        time_func=lambda: now[0],
        idle_finish_secs=300.0,
    )
    service.tick()
    assert FakeTailer.instances[0].finished == []  # not completed yet

    # The orchestrator's SubagentStop hook would do this rewrite -- simulate it directly.
    _write_json(sidecar_path, {"orchestrator": "claude-bridge-dev", "completed": True})
    now[0] = 100.5  # well under idle_finish_secs -- must NOT need to wait for idle-finish
    service.tick()

    assert FakeTailer.instances[0].finished == [True]
    assert not output_path.exists()
    assert not sidecar_path.exists()


def test_cold_seat_without_completed_sidecar_keeps_polling_normally(tmp_path):
    FakeTailer.instances = []
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    (cold_dir / "agent-6.output").write_text("x\n", encoding="utf-8")
    _write_json(cold_dir / "agent-6.arb-tail.json", {"orchestrator": "claude-bridge-dev", "completed": False})
    service = Service(
        redis=FakeRedis(),
        prefix="agent_scratch:",
        registry_path=str(tmp_path / "missing.json"),
        cold_dir=str(cold_dir),
        tailer_cls=FakeTailer,
        idle_finish_secs=300.0,
    )

    service.tick()
    service.tick()

    assert FakeTailer.instances[0].finished == []
    assert FakeTailer.instances[0].poll_count == 2
```

- [ ] **Step 10: Run to verify the new tests fail**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_service.py -k "completed_sidecar or without_completed_sidecar" -v`
Expected: `test_cold_seat_completed_sidecar_finishes_and_deletes_files_same_tick` FAILs
(`AssertionError: [] != [True]` — nothing finishes it yet); `test_cold_seat_without_completed_sidecar_keeps_polling_normally`
PASSes already (no new behavior triggers without `completed: true`).

- [ ] **Step 11: Implement the completion check in the active poll loop**

In `tick()` (currently lines 78-118), the active-poll loop currently reads:

```python
        polled = 0
        for key in sorted(live_keys):
            state = self._tailers.get(key)
            if state is None or state.finished:
                continue
            try:
                emitted = state.tailer.poll()
            except Exception:
                logger.exception("claude transcript tailer failed", extra={"tailer_key": key})
                continue
            polled += 1
            if getattr(state.tailer, "completed", False):
                # The seat signaled its own completion (cold-Opus [ARB_SEAT_DONE]) — finish promptly
                # and accurately, rather than waiting on the idle-finish backstop.
                self._finish_once(state)
                continue
            if emitted:
                state.last_activity = now
            elif key.startswith("cold:") and now - state.last_activity >= self.idle_finish_secs:
                # Idle-finish only cold (.output) seats — their lifecycle is bounded by file
                # activity. A warm orchestrator is bounded by its SessionEnd hook (registry
                # removal, handled above); idle-finishing it would abandon a still-live session
                # (finished tailers are skipped forever) and freeze its transcript mid-run.
                self._finish_once(state)
        return polled
```

Replace it with (the only change is a new `elif key.startswith("cold:") and self._cold_seat_completed(state)`
branch inserted between the existing `tailer.completed` check and the idle-finish `elif`):

```python
        polled = 0
        for key in sorted(live_keys):
            state = self._tailers.get(key)
            if state is None or state.finished:
                continue
            try:
                emitted = state.tailer.poll()
            except Exception:
                logger.exception("claude transcript tailer failed", extra={"tailer_key": key})
                continue
            polled += 1
            if getattr(state.tailer, "completed", False):
                # The seat signaled its own completion (cold-Opus [ARB_SEAT_DONE]) — finish promptly
                # and accurately, rather than waiting on the idle-finish backstop.
                self._finish_once(state)
                continue
            if key.startswith("cold:") and self._cold_seat_completed(state):
                # SubagentStop wrote completed:true to the sidecar. The poll() just above already
                # caught the tailer up to EOF, so it's safe to finish now — no separate final poll
                # needed. Delete the symlink + sidecar so the key drops out of live_keys on the
                # NEXT tick, which then hits the existing, untouched "key not in live_keys" cleanup
                # branch above (a redundant _finish_once there is a harmless no-op).
                self._finish_once(state)
                self._delete_cold_seat_files(state)
                continue
            if emitted:
                state.last_activity = now
            elif key.startswith("cold:") and now - state.last_activity >= self.idle_finish_secs:
                # Idle-finish only cold (.output) seats — their lifecycle is bounded by file
                # activity. A warm orchestrator is bounded by its SessionEnd hook (registry
                # removal, handled above); idle-finishing it would abandon a still-live session
                # (finished tailers are skipped forever) and freeze its transcript mid-run.
                self._finish_once(state)
        return polled
```

Add two new helper methods right after `_finish_once` (currently lines 203-211):

```python
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
        output_path = Path(state.tailer.path)
        sidecar_path = self._cold_sidecar_path(state)
        for target in (output_path, sidecar_path):
            if target is None:
                continue
            try:
                target.unlink()
            except OSError:
                pass
```

(`_cold_seat_completed` and `_cold_sidecar_path` are `@staticmethod` since they only need `state`,
matching the existing `_read_cold_sidecar` static helper added in Step 5; `_delete_cold_seat_files`
is a regular method for consistency with the rest of the class, though it doesn't use `self`
beyond calling the other helpers.)

- [ ] **Step 12: Run to verify the new tests pass**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_service.py -k "completed_sidecar or without_completed_sidecar" -v`
Expected: all PASS.

- [ ] **Step 13: Run the full service test file and the full claude_tail suite**

Run: `.venv/bin/python -m pytest tests/claude_tail -v`
Expected: all tests PASS — this includes the pre-existing `test_cold_seat_completed_marker_finishes_promptly_without_idle`
test (the `[ARB_SEAT_DONE]` marker path), which must still pass unchanged since that branch wasn't
touched.

- [ ] **Step 14: Commit the completion half**

```bash
git add src/agent_redis_bridge/claude_tail/service.py tests/claude_tail/test_service.py
git commit -m "feat(claude-tail): finish + clean up a cold seat promptly on sidecar completion

tick()'s active poll loop now checks each live cold key's sidecar after
polling it. completed:true -> finish immediately (the poll that just ran
is the final one) and delete the .output symlink + sidecar, which makes
the key drop from live_keys on the next tick and hit the existing,
untouched missing-key cleanup path. Closes the round-1 spec bug where this
check was placed on the (unreachable, since the symlink is never deleted
by the hook) key-dropped branch instead."
```

---

### Task 3: `subagent_start.py` hook (+ `common.py` helpers)

**Files:**
- Modify: `scripts/claude_tail_hooks/common.py`
- Create: `scripts/claude_tail_hooks/subagent_start.py`
- Create: `tests/claude_tail/test_subagent_hooks.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (this task and Task 2 are independent of each other; both
  depend only on Task 1/the existing codebase).
- Produces (new `common.py` functions, consumed by this task and Task 4):
  - `cold_dir() -> Path` — resolves `ARB_CLAUDE_TAIL_COLD_DIR` (default `~/.claude/tasks`),
    expanded.
  - `cold_agent_types() -> set[str]` — parses `ARB_CLAUDE_TAIL_COLD_AGENT_TYPES` (comma-separated,
    default `code-reviewer-report-writer`) into a set.
  - `lookup_registry_record(session_id: str) -> dict[str, Any] | None` — finds a registry record
    by `session_id`, transparently handling both the file-registry and Redis-registry backends
    (mirrors the existing dual-path branching in `session_start.py`/`session_end.py`).
  - `write_json_atomic(path: Path, data: dict[str, Any]) -> None` — temp-file-in-same-dir +
    `os.replace`, mirroring the existing `write_registry` idiom. Used by this task to close a
    write-order race (see Step 8) and by Task 4 for consistency.

- [ ] **Step 1: Write the failing tests for the new `common.py` helpers**

Create `tests/claude_tail/test_subagent_hooks.py`:

```python
import json

from scripts.claude_tail_hooks import common, subagent_start


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_cold_dir_defaults_to_claude_tasks(monkeypatch):
    monkeypatch.delenv("ARB_CLAUDE_TAIL_COLD_DIR", raising=False)
    assert common.cold_dir() == (__import__("pathlib").Path("~/.claude/tasks").expanduser())


def test_cold_dir_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(tmp_path / "custom"))
    assert common.cold_dir() == tmp_path / "custom"


def test_cold_agent_types_defaults_to_code_reviewer_report_writer(monkeypatch):
    monkeypatch.delenv("ARB_CLAUDE_TAIL_COLD_AGENT_TYPES", raising=False)
    assert common.cold_agent_types() == {"code-reviewer-report-writer"}


def test_cold_agent_types_parses_comma_separated_override(monkeypatch):
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_AGENT_TYPES", "code-reviewer-report-writer, arb-design-panelist")
    assert common.cold_agent_types() == {"code-reviewer-report-writer", "arb-design-panelist"}


def test_lookup_registry_record_finds_match_in_file_registry(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    common.write_registry(registry, [
        {"session_id": "sess-1", "transcript_path": "/x/sess-1.jsonl", "seat_id": "claude-bridge-dev"},
        {"session_id": "sess-2", "transcript_path": "/x/sess-2.jsonl", "seat_id": "claude-other-dev"},
    ])

    record = common.lookup_registry_record("sess-2")

    assert record == {"session_id": "sess-2", "transcript_path": "/x/sess-2.jsonl", "seat_id": "claude-other-dev"}


def test_lookup_registry_record_returns_none_on_miss(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    common.write_registry(registry, [{"session_id": "sess-1", "transcript_path": "/x/sess-1.jsonl"}])

    assert common.lookup_registry_record("sess-missing") is None


def test_lookup_registry_record_uses_redis_when_no_registry_path(monkeypatch):
    class FakeRedisClient:
        def __init__(self):
            self.hashes = {}

        def hgetall(self, key):
            return self.hashes.get(key, {})

        def hset(self, key, field, value):
            self.hashes.setdefault(key, {})[field] = value

    class FakeRedisFactory:
        client = FakeRedisClient()

        @staticmethod
        def from_url(url, **kwargs):
            return FakeRedisFactory.client

    monkeypatch.delenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("AGENT_REDIS_URL", "redis://example/0")
    monkeypatch.setattr("redis.Redis", FakeRedisFactory)
    common.upsert_redis_record(FakeRedisFactory.client, {
        "session_id": "sess-9", "transcript_path": "/x/sess-9.jsonl", "seat_id": "claude-bridge-dev",
    })

    record = common.lookup_registry_record("sess-9")

    assert record["transcript_path"] == "/x/sess-9.jsonl"
```

- [ ] **Step 2: Run to verify these fail**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_subagent_hooks.py -v`
Expected: FAIL with `AttributeError: module 'scripts.claude_tail_hooks.common' has no attribute
'cold_dir'` (and similarly for the other two new functions); also a collection error for `from
scripts.claude_tail_hooks import ... subagent_start` since that module doesn't exist yet — that's
expected, all of Step 1's tests are red until Steps 3+5 land.

- [ ] **Step 3: Implement the three `common.py` helpers**

Add `DEFAULT_COLD_AGENT_TYPES` next to the existing `DEFAULT_COLD_DIR` constant (currently line 14):

```python
DEFAULT_REGISTRY_KEY = "claude:registry"
DEFAULT_COLD_DIR = "~/.claude/tasks"
DEFAULT_COLD_AGENT_TYPES = "code-reviewer-report-writer"
```

Add four new functions after `mirror_cold_outputs` (currently ends around line 195), before
`fail_soft`:

```python
def cold_dir() -> Path:
    return Path(os.environ.get("ARB_CLAUDE_TAIL_COLD_DIR", DEFAULT_COLD_DIR)).expanduser()


def cold_agent_types() -> set[str]:
    raw = os.environ.get("ARB_CLAUDE_TAIL_COLD_AGENT_TYPES", DEFAULT_COLD_AGENT_TYPES)
    return {item.strip() for item in raw.split(",") if item.strip()}


def lookup_registry_record(session_id: str) -> dict[str, Any] | None:
    path = registry_path()
    if path is not None:
        records = read_registry(path)
    else:
        client = redis_client()
        if client is None:
            return None
        records = read_redis_registry(client)
    for item in records:
        if item.get("session_id") == session_id:
            return item
    return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    # Mirrors write_registry's existing temp-file-in-same-dir + os.replace idiom (above) — a
    # reader globbing the directory never observes a partially-written file.
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)
```

Now replace the inline `cold_dir` expression inside `mirror_cold_outputs` (currently line 167,
`cold_dir = Path(os.environ.get("ARB_CLAUDE_TAIL_COLD_DIR", DEFAULT_COLD_DIR)).expanduser()`) with
a call to the new helper:

```python
    cold_dir_path = cold_dir()
    cold_dir_path.mkdir(parents=True, exist_ok=True)
```

(Rename the local variable from `cold_dir` to `cold_dir_path` here since `cold_dir` is now the
function name — update the one further use of the old local variable name later in that same
function, `target = cold_dir / source.name` → `target = cold_dir_path / source.name`, to match.)

- [ ] **Step 4: Confirm the whole test file still fails to collect (expected — `subagent_start` doesn't exist yet)**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_subagent_hooks.py -v`
Expected: a collection ERROR for the whole file (the `from scripts.claude_tail_hooks import ...
subagent_start` import at the top fails since that module doesn't exist yet). This is expected and
not yet meaningful progress — the `common.py` helpers just added can't be exercised until Step 5's
test additions and Step 8's `subagent_start.py` implementation land. Proceed to Step 5.

- [ ] **Step 5: Run the existing hook tests to confirm the `mirror_cold_outputs` refactor is a no-op**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_hooks.py -v`
Expected: all PASS, including `test_session_start_mirrors_cold_output_files` and
`test_mirror_skips_self_and_existing_real_file` (these exercise `mirror_cold_outputs`, which now
calls `cold_dir()` instead of inlining the same expression).

- [ ] **Step 6: Write the failing tests for `subagent_start.py` itself**

Add to `tests/claude_tail/test_subagent_hooks.py`, after the `common.py` helper tests:

```python
def test_subagent_start_noop_for_disallowed_agent_type(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_AGENT_TYPES", "code-reviewer-report-writer")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(tmp_path / "registry.json"))

    rc = subagent_start.main([json.dumps({
        "session_id": "sess-1", "agent_id": "agent-1", "agent_type": "Explore", "cwd": "/Users/<user>/<workspace>",
    })])

    assert rc == 0
    assert not cold_dir_path.exists()


def test_subagent_start_noop_when_parent_session_not_in_registry(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    common.write_registry(registry, [])

    rc = subagent_start.main([json.dumps({
        "session_id": "sess-missing", "agent_id": "agent-1",
        "agent_type": "code-reviewer-report-writer", "cwd": "/Users/<user>/<workspace>",
    })])

    assert rc == 0
    assert not (cold_dir_path / "agent-1.output").exists()


def test_subagent_start_creates_symlink_and_sidecar_for_allowed_type(tmp_path, monkeypatch):
    # The real Claude Code layout nests subagent transcripts under a directory matching the
    # parent session id -- a SIBLING of the flat parent .jsonl file, not a child of its parent
    # directory. Build that real on-disk layout directly (no .parent/.with_suffix reuse from the
    # production code under test), so this assertion cannot pass tautologically against a buggy
    # path formula -- this is exactly the shape of bug a round-4 implementation review caught
    # when the original version of this test built its expectation the same way the (buggy)
    # production code did.
    cold_dir_path = tmp_path / "tasks"
    registry = tmp_path / "registry.json"
    parent_transcript = tmp_path / "projects" / "-Users-mark-<workspace>" / "sess-1.jsonl"
    parent_transcript.parent.mkdir(parents=True)
    parent_transcript.write_text("", encoding="utf-8")
    real_subagent_dir = tmp_path / "projects" / "-Users-mark-<workspace>" / "sess-1" / "subagents"
    real_subagent_dir.mkdir(parents=True)
    real_subagent_transcript = real_subagent_dir / "agent-agent-1.jsonl"
    real_subagent_transcript.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    common.write_registry(registry, [{
        "session_id": "sess-1", "transcript_path": str(parent_transcript), "seat_id": "claude-bridge-dev",
    }])

    rc = subagent_start.main([json.dumps({
        "session_id": "sess-1", "agent_id": "agent-1",
        "agent_type": "code-reviewer-report-writer", "cwd": "/Users/<user>/<workspace>",
    })])

    assert rc == 0
    output_link = cold_dir_path / "agent-1.output"
    assert output_link.is_symlink()
    assert output_link.exists()  # NOT dangling -- mirrors what service._is_recent()'s stat() needs
    assert output_link.resolve() == real_subagent_transcript.resolve()
    sidecar = _read_json(cold_dir_path / "agent-1.arb-tail.json")
    assert sidecar == {"orchestrator": "claude-bridge-dev", "completed": False}


def test_subagent_start_is_idempotent(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    registry = tmp_path / "registry.json"
    parent_transcript = tmp_path / "projects" / "-Users-mark-<workspace>" / "sess-1.jsonl"
    parent_transcript.parent.mkdir(parents=True)
    parent_transcript.write_text("", encoding="utf-8")
    real_subagent_dir = tmp_path / "projects" / "-Users-mark-<workspace>" / "sess-1" / "subagents"
    real_subagent_dir.mkdir(parents=True)
    (real_subagent_dir / "agent-agent-1.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    common.write_registry(registry, [{
        "session_id": "sess-1", "transcript_path": str(parent_transcript), "seat_id": "claude-bridge-dev",
    }])
    payload = [json.dumps({
        "session_id": "sess-1", "agent_id": "agent-1",
        "agent_type": "code-reviewer-report-writer", "cwd": "/Users/<user>/<workspace>",
    })]

    assert subagent_start.main(payload) == 0
    assert subagent_start.main(payload) == 0  # must not raise on re-invocation

    output_link = cold_dir_path / "agent-1.output"
    assert output_link.is_symlink()
    assert output_link.exists()  # NOT dangling


def test_subagent_start_fails_soft_on_bad_input(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(tmp_path / "registry.json"))

    rc = subagent_start.main(["{bad-json"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "claude-tail hook error:" in captured.err
    assert "Traceback" not in captured.err
```

- [ ] **Step 7: Run to verify these fail**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_subagent_hooks.py -v`
Expected: collection ERROR (`subagent_start` module still doesn't exist).

- [ ] **Step 8: Implement `subagent_start.py` — sidecar written atomically BEFORE the symlink**

**Write-order matters (found during plan review, not the design-spec rounds): write the sidecar
first, then the symlink, never the reverse.** The daemon's discovery (`_discover_specs()`) is
triggered solely by the `.output` symlink's existence via the `*.output` glob — the sidecar is
just extra data read *if* it happens to be there at that moment. If the symlink existed before the
sidecar, a daemon tick landing in that window would create the tailer unlocked
(`identity_locked=False`, empty `orchestrator`) — and critically, `tick()`'s tailer-creation check
(`if existing is None or self._resumed_after_finish(existing, spec):` — Task 2, `service.py`'s
`tick()`) never re-creates an *already-existing* tailer just because its spec later gains sidecar
data. That seat's `orchestrator` would stay empty for its entire lifetime, not just transiently.
Writing the sidecar first (atomically, via `write_json_atomic` from Task 3 Step 3 — so a reader
never sees a half-written file either) before the symlink ever exists eliminates the race
entirely: by the time discovery can see the symlink at all, the sidecar is guaranteed complete.

Create `scripts/claude_tail_hooks/subagent_start.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path

from .common import cold_agent_types, cold_dir, fail_soft, load_hook_payload, lookup_registry_record, required_str, write_json_atomic


def _main(args: list[str] | None = None) -> int:
    payload = load_hook_payload(args)
    agent_type = payload.get("agent_type")
    if not isinstance(agent_type, str) or agent_type not in cold_agent_types():
        return 0

    session_id = required_str(payload, "session_id")
    agent_id = required_str(payload, "agent_id")

    parent = lookup_registry_record(session_id)
    if parent is None:
        return 0
    parent_transcript = parent.get("transcript_path")
    if not isinstance(parent_transcript, str) or not parent_transcript:
        return 0

    # with_suffix("") strips only the .jsonl suffix from the parent transcript's FILE path,
    # yielding the session-id directory that actually contains subagents/ (a SIBLING of the flat
    # parent .jsonl, not its parent directory -- .parent here would drop the session_id segment
    # entirely and produce a dangling symlink; found during implementation review, round 4).
    subagent_transcript = Path(parent_transcript).with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
    directory = cold_dir()

    # Sidecar first, atomically -- see the write-order note above. Discovery only ever looks for
    # the symlink, so the sidecar must already be complete by the time the symlink can be seen.
    sidecar = directory / f"{agent_id}.arb-tail.json"
    write_json_atomic(sidecar, {"orchestrator": parent.get("seat_id") or "", "completed": False})

    output_link = directory / f"{agent_id}.output"
    if output_link.is_symlink() or output_link.exists():
        output_link.unlink()
    output_link.symlink_to(subagent_transcript)
    return 0


def main(args: list[str] | None = None) -> int:
    return fail_soft("subagent_start", _main, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 9: Run to verify all the new tests pass**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_subagent_hooks.py -v`
Expected: all PASS.

- [ ] **Step 10: Run the full claude_tail suite to check for regressions**

Run: `.venv/bin/python -m pytest tests/claude_tail -v`
Expected: all PASS.

- [ ] **Step 11: Commit**

```bash
git add scripts/claude_tail_hooks/common.py scripts/claude_tail_hooks/subagent_start.py tests/claude_tail/test_subagent_hooks.py
git commit -m "feat(claude-tail): SubagentStart hook registers an allowlisted cold seat

New subagent_start.py: on an allowlisted agent_type (default
code-reviewer-report-writer, configurable via
ARB_CLAUDE_TAIL_COLD_AGENT_TYPES), looks up the parent session's already-
known transcript_path in the existing registry, derives the subagent's
real transcript path from it (no slug reimplementation), and drops a
symlink + orchestrator sidecar into the existing cold_dir -- which the
daemon's unmodified .output glob picks up on its next poll tick. Sidecar
is written atomically BEFORE the symlink (found during plan review): the
daemon's discovery is keyed solely on the symlink's existence, and an
already-created tailer is never refreshed later just because its sidecar
appears -- writing symlink-first could permanently strand a seat with no
orchestrator grouping if a poll tick landed in the gap between the two
writes.

New common.py helpers: cold_dir(), cold_agent_types(),
lookup_registry_record(), write_json_atomic() -- the latter three also
set up Task 4 (subagent_stop.py)."
```

---

### Task 4: `subagent_stop.py` hook

**Files:**
- Create: `scripts/claude_tail_hooks/subagent_stop.py`
- Test: `tests/claude_tail/test_subagent_hooks.py`

**Interfaces:**
- Consumes: `cold_dir()`, `write_json_atomic()`, `fail_soft`, `load_hook_payload`, `required_str`
  from `common.py` (Task 3 — `cold_dir()`/`write_json_atomic()` are new from that task, the rest
  already existed).
- Produces: nothing consumed by other tasks — this is the last piece of the hook pair.

- [ ] **Step 1: Write the failing tests**

Add to `tests/claude_tail/test_subagent_hooks.py`, after the `subagent_start` tests, and add
`subagent_stop` to the existing import line at the top of the file:

```python
from scripts.claude_tail_hooks import common, subagent_start, subagent_stop
```

```python
def test_subagent_stop_rewrites_completed_true_without_deleting(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    cold_dir_path.mkdir()
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    output_link = cold_dir_path / "agent-1.output"
    output_link.symlink_to(tmp_path / "does-not-need-to-exist.jsonl")
    sidecar = cold_dir_path / "agent-1.arb-tail.json"
    sidecar.write_text(json.dumps({"orchestrator": "claude-bridge-dev", "completed": False}), encoding="utf-8")

    rc = subagent_stop.main([json.dumps({"agent_id": "agent-1"})])

    assert rc == 0
    assert output_link.is_symlink()  # not deleted
    assert _read_json(sidecar) == {"orchestrator": "claude-bridge-dev", "completed": True}


def test_subagent_stop_noop_when_sidecar_missing(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    cold_dir_path.mkdir()
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))

    rc = subagent_stop.main([json.dumps({"agent_id": "agent-unknown"})])

    assert rc == 0
    assert list(cold_dir_path.iterdir()) == []


def test_subagent_stop_is_idempotent(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    cold_dir_path.mkdir()
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    sidecar = cold_dir_path / "agent-1.arb-tail.json"
    sidecar.write_text(json.dumps({"orchestrator": "claude-bridge-dev", "completed": False}), encoding="utf-8")
    payload = [json.dumps({"agent_id": "agent-1"})]

    assert subagent_stop.main(payload) == 0
    assert subagent_stop.main(payload) == 0

    assert _read_json(sidecar) == {"orchestrator": "claude-bridge-dev", "completed": True}


def test_subagent_stop_fails_soft_on_bad_input(monkeypatch, capsys):
    rc = subagent_stop.main(["{bad-json"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "claude-tail hook error:" in captured.err
    assert "Traceback" not in captured.err
```

- [ ] **Step 2: Run to verify these fail**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_subagent_hooks.py -v`
Expected: collection ERROR (`subagent_stop` module doesn't exist yet).

- [ ] **Step 3: Implement `subagent_stop.py`**

Create `scripts/claude_tail_hooks/subagent_stop.py`:

```python
from __future__ import annotations

import json
import sys

from .common import cold_dir, fail_soft, load_hook_payload, required_str, write_json_atomic


def _main(args: list[str] | None = None) -> int:
    payload = load_hook_payload(args)
    agent_id = required_str(payload, "agent_id")

    sidecar = cold_dir() / f"{agent_id}.arb-tail.json"
    if not sidecar.exists():
        return 0

    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0

    data["completed"] = True
    write_json_atomic(sidecar, data)
    return 0


def main(args: list[str] | None = None) -> int:
    return fail_soft("subagent_stop", _main, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run to verify all tests pass**

Run: `.venv/bin/python -m pytest tests/claude_tail/test_subagent_hooks.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full claude_tail suite**

Run: `.venv/bin/python -m pytest tests/claude_tail -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/claude_tail_hooks/subagent_stop.py tests/claude_tail/test_subagent_hooks.py
git commit -m "feat(claude-tail): SubagentStop hook signals completion without deleting

New subagent_stop.py: rewrites the cold seat's sidecar with
completed:true (atomically, via common.write_json_atomic). Deliberately
does not delete the .output symlink or sidecar itself -- service.py's
tick() (Task 2) does that only after confirming it has polled the seat
through to EOF, avoiding the truncation/total-loss race a delete-on-stop
design would have."
```

---

### Task 5: Full-suite regression + CHANGELOG entry

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: nothing new — this is a wrap-up/verification task after Tasks 1-4 are all committed.
- Produces: nothing consumed by other tasks.

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python -m pytest tests -q`
Expected: all PASS, no regressions outside `tests/claude_tail`.

- [ ] **Step 2: Run doc-index and doc-drift checks**

Run: `scripts/check-doc-index && scripts/check-doc-drift`
Expected: both exit 0 (this plan doesn't touch any tracked docs besides itself and the spec, both
already covered by the `docs/superpowers/{plans,specs}` collection exemption).

- [ ] **Step 3: Add the CHANGELOG entry**

Add to `CHANGELOG.md` under `## Unreleased — dev`, above the most recent existing entry:

```markdown
### feat(claude-tail): cold-Opus subagent visibility in arb-watch (2026-06-30)
- Cold-Opus reviewer subagents (Claude Code's native Agent/Task tool, e.g.
  `code-reviewer-report-writer`) never appeared as a seat in arb-watch — `claude_tail`'s cold-seat
  discovery globbed the wrong directory (the harness's actual subagent-output location had drifted
  since the prior 2026-06-28 spec).
- **Fix:** two new hooks, `SubagentStart`/`SubagentStop` (wired in `.claude/settings.local.json`,
  host-local), register/deregister each allowlisted subagent (`ARB_CLAUDE_TAIL_COLD_AGENT_TYPES`,
  default `code-reviewer-report-writer`) by symlinking its real transcript — derived from the
  already-known parent-session registry entry, not a reimplemented path-slugging algorithm — into
  the directory the daemon's existing `.output` glob already watches. Zero daemon-*discovery*
  change needed; real-time pickup within one poll tick (~1s).
- Two small, targeted daemon changes carry the `orchestrator` field through correctly
  (`identity_locked` guard on `TranscriptTailer`) and finish+clean up promptly on completion
  instead of the 5-minute idle-finish fallback (`service.py`'s `tick()` checks each live cold key's
  sidecar after polling it).
- Panel-reviewed twice (codex + agy-print + cold-Opus): round 1 found two P1 design bugs (an
  identity-overwrite-wrong-layer bug, a same-`seat_id` dedup collision in the Go frontend); round 2
  (re-reviewing the round-1 fixes) found two more — a circular/unreachable completion-check
  placement, and an identity-guard regression of an existing marker-upgrade code path — all four
  fixed, the last two independently re-verified against the real code rather than trusted from
  reviewer prose. See `docs/superpowers/specs/2026-06-30-cold-opus-subagent-visibility-design.md`.
- **Verified:** TDD red→green throughout; full `tests/claude_tail` suite + full repo suite green.
```

- [ ] **Step 4: Run check-doc-index again and the full suite one more time**

Run: `scripts/check-doc-index && .venv/bin/python -m pytest tests -q`
Expected: both clean.

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG entry for cold-Opus subagent visibility"
```

---

## Post-plan (not part of this implementation plan — operator/live-verification steps)

These are deliberately **not** plan tasks because they require a live host with a running
`claude-tail-daemon` and write access to `.claude/settings.local.json` (host-local, gitignored,
not something a worktree-isolated implementer can or should touch):

1. Wire `SubagentStart` → `python -m scripts.claude_tail_hooks.subagent_start` and `SubagentStop` →
   `python -m scripts.claude_tail_hooks.subagent_stop` into `.claude/settings.local.json`'s
   `hooks` block, mirroring the existing `SessionStart`/`SessionEnd` entries (same env vars: cd
   into the repo, `ARB_CLAUDE_TAIL_PROJECT`, `ARB_CLAUDE_TAIL_WORKSPACE`, `AGENT_REDIS_URL`,
   `AGENT_REDIS_PREFIX`, `PYTHONPATH`).
2. Restart the Claude Code session (hooks load at session start — editing the config doesn't
   hot-swap into a running session).
3. Spawn a real `code-reviewer-report-writer` subagent and confirm: a row appears in arb-watch
   within ~1-2s of spawn with the correct `orchestrator` grouping; it finishes promptly (not after
   5 minutes) on completion; its full transcript content is present in the trace (no truncated
   final lines).
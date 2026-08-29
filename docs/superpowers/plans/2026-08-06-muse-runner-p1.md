# muse_runner P1 Implementation Plan

> **STATUS: all five tasks complete** as of 2026-08-06 (`b8cfd287`). 32 tests green;
> 15 mutations across two sweeps, each caught by its named test. Boxes ticked below.
> Gates G1–G6 remain UNRUN — they need live `muse` turns and are not P1 code.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `muse_runner.py` — a cold-process / warm-session warm-orch runner for Muse Code — with the whole Muse-specific parse surface in a pure, offline-testable unit.

**Architecture:** Two units. `MuseEventMapper` turns Muse's JSONL envelopes into `TurnEvent`s and touches no process. `MuseRunner` owns session identity, spawns one `muse exec` per turn, and reaps it. Continuity is a persisted session UUID minted *before* the first process exists.

**Tech Stack:** Python 3, `asyncio.create_subprocess_exec`, `unittest` (repo convention), pytest as the runner.

**Spec:** `docs/superpowers/specs/2026-08-06-muse-runner-design.md`. **Evidence:** ARB Memory `findings-muse-code-seat-probe-20260806` v1.

## Global Constraints

- **Test invocation (non-negotiable):** `PYTHONPATH=<worktree>/src /Volumes/<workspace>/repos/ARB/.venv/bin/python -m pytest`. This worktree has no `.venv`; the main checkout's venv resolves `arb_warm_orch` from the **main checkout** and `tests/conftest.py:142` refuses the run with an import-provenance error. Green without `PYTHONPATH` describes a different tree.
- **No live `muse` call in any Task below.** P1 tasks are offline-only. Live capture and gates G1–G5 are §7 of the spec and run separately, under the 60-turn / $10 ceiling.
- **Turn outcome derives from `run.terminal.*` only.** `task.lifecycle.failed` never determines it (spec §5.2).
- **`task.lifecycle.status` never maps to `ReasoningDelta`** (spec §5.3).
- Repo test style is `unittest.TestCase` classes; follow it.
- Runner method names are fixed by `runner.py:168-226` and are duck-typed — nothing type-checks them (spec §2).

---

### Task 1: The envelope mapper — text, terminal, and the two traps

**Files:**
- Create: `src/arb_warm_orch/muse_events.py`
- Test: `tests/test_muse_events.py`

**Interfaces:**
- Produces: `MuseEventMapper.feed(envelope: dict) -> list[TurnEvent]`; `MuseEventMapper.terminal: str | None`; `MuseEventMapper.terminal_text: str`; `parse_line(line: str) -> dict | None`.
- Consumes: `TextDelta`, `ToolCallStarted`, `ToolCallCompleted`, `ReasoningDelta`, `tool_kind` from `.turn_events`.

- [x] **Step 1: Write the failing test**

```python
import unittest
from arb_warm_orch.muse_events import MuseEventMapper, parse_line
from arb_warm_orch.turn_events import TextDelta


def env(payload_type, payload, **kw):
    d = {"schema_version": 1, "payload_type": payload_type, "payload": payload}
    d.update(kw)
    return d


class TestTextAndPreamble(unittest.TestCase):
    def test_output_delta_becomes_text_delta(self):
        m = MuseEventMapper()
        out = m.feed(env("run.output.delta", {"text": "hi"}))
        self.assertEqual(out, [TextDelta(text="hi")])

    def test_preamble_line_is_skipped_not_an_error(self):
        self.assertIsNone(parse_line("muse: workspace root: /tmp/x"))

    def test_json_line_parses(self):
        self.assertEqual(parse_line('{"a":1}'), {"a": 1})


class TestTraps(unittest.TestCase):
    def test_lifecycle_failed_does_not_set_terminal(self):
        m = MuseEventMapper()
        m.feed(env("task.lifecycle.failed",
                   {"reason": "provider does not support base instructions"}))
        m.feed(env("task.lifecycle.failed", {"reason": "same again"}))
        self.assertIsNone(m.terminal)
        self.assertEqual(m.failures, [
            "provider does not support base instructions", "same again"])

    def test_terminal_comes_only_from_run_terminal(self):
        m = MuseEventMapper()
        m.feed(env("task.lifecycle.failed", {"reason": "noise"}))
        m.feed(env("run.terminal.completed", {"terminal": "completed", "text": "OK"}))
        self.assertEqual(m.terminal, "completed")
        self.assertEqual(m.terminal_text, "OK")

    def test_lifecycle_status_is_never_reasoning(self):
        m = MuseEventMapper()
        out = m.feed(env("task.lifecycle.status",
                         {"message": "opening meta model stream attempt 1/10"}))
        self.assertEqual(out, [])
```

- [x] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /Volumes/<workspace>/repos/ARB/.venv/bin/python -m pytest tests/test_muse_events.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'arb_warm_orch.muse_events'`

- [x] **Step 3: Write minimal implementation**

Create `src/arb_warm_orch/muse_events.py` with `parse_line` (returns `None` for any line not starting with `{`, and for malformed JSON) and `MuseEventMapper` with `feed`, `terminal`, `terminal_text`, `failures`. Dispatch on `payload_type`. `run.output.delta` → `[TextDelta]`. `run.terminal.*` sets `terminal`/`terminal_text` and returns `[]`. `task.lifecycle.failed` appends to `failures` and returns `[]`. `task.lifecycle.status` returns `[]`. Unknown types return `[]`.

- [x] **Step 4: Run test to verify it passes**

Expected: PASS, 6 tests.

- [x] **Step 5: Commit**

```bash
git add src/arb_warm_orch/muse_events.py tests/test_muse_events.py
git commit -m "feat(warm-orch): muse envelope mapper — text, terminal, and the two traps"
```

---

### Task 2: Tool-call correlation

**Files:**
- Modify: `src/arb_warm_orch/muse_events.py`
- Test: `tests/test_muse_events.py`

**Interfaces:**
- Produces: `ToolCallStarted` at `task.lifecycle.scheduled`, `ToolCallCompleted` at `tool.result`; `normalise_call_id(raw: str) -> str`.

**Correlation assumption, stated because it is an assumption:** `task.lifecycle.proposed` carries `task_kind` (e.g. `tool.bash`) but the stable id arrives on `task.lifecycle.scheduled` as `idempotency_key` (`"tool:call_019fd538…"`), while `tool.result` carries `call_id` (`"call_019fd538…"`). The mapper therefore strips a leading `tool:` to join them. **Gate G6 (new, added by this plan) must confirm the join against a real capture** — if `call_id` and `idempotency_key` do not correspond, `ToolCallCompleted` will not match its `ToolCallStarted` and buzz's idle deadline (`turn_events.py:60-62`) never resets.

- [x] **Step 1: Write the failing test**

```python
from arb_warm_orch.turn_events import ToolCallCompleted, ToolCallStarted
from arb_warm_orch.muse_events import normalise_call_id


class TestToolCalls(unittest.TestCase):
    def test_call_id_prefix_is_stripped(self):
        self.assertEqual(normalise_call_id("tool:call_019f"), "call_019f")
        self.assertEqual(normalise_call_id("call_019f"), "call_019f")

    def test_scheduled_emits_started_with_kind_from_proposed(self):
        m = MuseEventMapper()
        self.assertEqual(m.feed(env("task.lifecycle.proposed",
                                    {"task_id": "t1", "task_kind": "tool.bash"})), [])
        out = m.feed(env("task.lifecycle.scheduled",
                         {"task_id": "t1", "idempotency_key": "tool:call_019f"}))
        self.assertEqual(out, [ToolCallStarted(
            tool_call_id="call_019f", title="tool.bash", kind="execute")])

    def test_tool_result_outcome_maps_to_status(self):
        m = MuseEventMapper()
        ok = m.feed(env("tool.result", {"call_id": "call_a",
                                        "correlation_facts": {"outcome": "success"}}))
        bad = m.feed(env("tool.result", {"call_id": "call_b",
                                         "correlation_facts": {"outcome": "error"}}))
        self.assertEqual(ok, [ToolCallCompleted(tool_call_id="call_a", status="completed")])
        self.assertEqual(bad, [ToolCallCompleted(tool_call_id="call_b", status="failed")])

    def test_scheduled_without_proposed_still_emits(self):
        m = MuseEventMapper()
        out = m.feed(env("task.lifecycle.scheduled",
                         {"task_id": "t9", "idempotency_key": "tool:call_z"}))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].tool_call_id, "call_z")
        self.assertEqual(out[0].kind, "other")
```

- [x] **Step 2: Run to verify it fails** — Expected: FAIL, `ImportError: cannot import name 'normalise_call_id'`

- [x] **Step 3: Implement.** `normalise_call_id` strips one leading `tool:`. Track `task_kind` by `task_id` on `proposed`. On `scheduled`, emit `ToolCallStarted(tool_call_id=normalise_call_id(idempotency_key), title=task_kind or "tool", kind=_muse_kind(task_kind))`. `_muse_kind` maps `tool.bash`→`execute`, `tool.read`→`read`, `tool.write`/`tool.edit`→`edit`, `tool.glob`/`tool.grep`→`search`, else `other`. On `tool.result`, emit `ToolCallCompleted` with `status="completed"` when `outcome == "success"` else `"failed"`.

- [x] **Step 4: Run to verify it passes** — Expected: PASS, 10 tests total.

- [x] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(warm-orch): correlate muse tool calls across proposed/scheduled/result"
```

---

### Task 3: Session identity — persisted before any process exists

**Files:**
- Create: `src/arb_warm_orch/muse_runner.py`
- Test: `tests/test_muse_runner.py`

**Interfaces:**
- Produces: `MuseConfig(cwd, session_dir, model=None, reasoning_effort=None, muse_bin="muse")`; `MuseRunner(config)`; `await connect()`; `await disconnect()`; `.session_id`.

- [x] **Step 1: Write the failing test**

```python
import asyncio, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from arb_warm_orch.muse_runner import MuseConfig, MuseRunner


class TestSessionIdentity(unittest.TestCase):
    def test_connect_persists_session_id_before_any_process(self):
        with TemporaryDirectory() as d:
            cfg = MuseConfig(cwd=Path(d), session_dir=Path(d) / "s")
            r = MuseRunner(cfg)
            asyncio.run(r.connect())
            sid = r.session_id
            self.assertIsNotNone(sid)
            self.assertEqual(len(sid), 36)
            self.assertEqual(sid, sid.lower())
            self.assertTrue(r._session_id_path().exists())
            self.assertEqual(r._session_id_path().read_text().strip(), sid)

    def test_second_runner_reuses_the_persisted_id(self):
        with TemporaryDirectory() as d:
            cfg = MuseConfig(cwd=Path(d), session_dir=Path(d) / "s")
            a = MuseRunner(cfg); asyncio.run(a.connect())
            b = MuseRunner(cfg); asyncio.run(b.connect())
            self.assertEqual(a.session_id, b.session_id)

    def test_connect_is_idempotent(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(MuseConfig(cwd=Path(d), session_dir=Path(d) / "s"))
            asyncio.run(r.connect()); first = r.session_id
            asyncio.run(r.connect())
            self.assertEqual(r.session_id, first)
```

- [x] **Step 2: Run to verify it fails** — Expected: FAIL, `ModuleNotFoundError: arb_warm_orch.muse_runner`

- [x] **Step 3: Implement** `MuseConfig` dataclass and `MuseRunner` with `_session_id_path()` (`session_dir/session-id`), `_load_session_id()`, `_persist_session_id()`, and `connect()` that loads-or-mints `str(uuid.uuid4()).lower()` and persists immediately. `disconnect()` terminates any live child and leaves the id alone.

- [x] **Step 4: Run to verify it passes** — Expected: PASS, 3 tests.

- [x] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(warm-orch): muse session identity persisted at connect, not turn-end"
```

---

### Task 4: `stream_turn` — argv, spawn, parse, reap

**Files:**
- Modify: `src/arb_warm_orch/muse_runner.py`
- Test: `tests/test_muse_runner.py`

**Interfaces:**
- Produces: `MuseRunner.build_argv(prompt_file: Path) -> list[str]`; `async stream_turn(text) -> AsyncIterator[TurnEvent]`; `async turn(text) -> str`.

`build_argv` is split out precisely so argv is assertable without spawning anything.

- [x] **Step 1: Write the failing test**

```python
class TestArgv(unittest.TestCase):
    def test_argv_carries_the_required_flags(self):
        with TemporaryDirectory() as d:
            cfg = MuseConfig(cwd=Path(d), session_dir=Path(d) / "s",
                             model="muse-spark-1.2", reasoning_effort="minimal")
            r = MuseRunner(cfg); asyncio.run(r.connect())
            argv = r.build_argv(Path("/tmp/p.md"))
            self.assertEqual(argv[:2], ["muse", "exec"])
            self.assertIn("--json", argv)
            self.assertIn("--session-id", argv)
            self.assertIn(r.session_id, argv)
            self.assertEqual(argv[argv.index("--prompt-file") + 1], "/tmp/p.md")
            self.assertEqual(argv[argv.index("--model") + 1], "muse-spark-1.2")
            self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "minimal")
            self.assertEqual(argv[argv.index("--workspace") + 1], d)

    def test_argv_omits_unset_optionals_and_never_widens_blast_radius(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(MuseConfig(cwd=Path(d), session_dir=Path(d) / "s"))
            asyncio.run(r.connect())
            argv = r.build_argv(Path("/tmp/p.md"))
            self.assertNotIn("--model", argv)
            self.assertNotIn("--reasoning-effort", argv)
            for banned in ("--yolo", "--disable-sandbox", "--disable-approval",
                           "--trust-workspace", "--disable-write", "--disable-shell"):
                self.assertNotIn(banned, argv)


class TestStreamTurn(unittest.TestCase):
    def test_stream_turn_maps_a_scripted_stdout(self):
        lines = [
            "muse: workspace root: /tmp/x",
            '{"payload_type":"run.output.delta","payload":{"text":"he"}}',
            '{"payload_type":"task.lifecycle.failed","payload":{"reason":"noise"}}',
            '{"payload_type":"run.output.delta","payload":{"text":"llo"}}',
            '{"payload_type":"run.terminal.completed","payload":'
            '{"terminal":"completed","text":"hello"}}',
        ]
        with TemporaryDirectory() as d:
            r = MuseRunner(MuseConfig(cwd=Path(d), session_dir=Path(d) / "s"))
            r._spawn = _scripted_spawn(lines)          # injected, no real process
            got = asyncio.run(_drain(r.stream_turn("hi")))
            self.assertEqual([e.text for e in got if isinstance(e, TextDelta)],
                             ["he", "llo"])
            self.assertEqual(asyncio.run(r.turn("hi")), "hello")
```

Helpers in the same file:

```python
async def _drain(agen):
    return [e async for e in agen]


def _scripted_spawn(lines):
    async def _spawn(argv, cwd):
        class _P:
            returncode = 0
            async def wait(self): return 0
            def terminate(self): pass
            @property
            def stdout(self): return _lines()
        async def _lines():
            for ln in lines:
                yield ln.encode()
        return _P()
    return _spawn
```

- [x] **Step 2: Run to verify it fails** — Expected: FAIL, `AttributeError: 'MuseRunner' object has no attribute 'build_argv'`

- [x] **Step 3: Implement.** `build_argv` emits `[muse_bin, "exec", "--json", "--session-id", sid, "--prompt-file", str(p), "--workspace", str(cwd)]` plus `--model` / `--reasoning-effort` only when set. `_spawn(argv, cwd)` wraps `asyncio.create_subprocess_exec` with `stdout=PIPE`; keep it a separate attribute so tests inject. `stream_turn` writes `text` to a temp file inside `session_dir`, spawns, iterates stdout lines through `parse_line` + `MuseEventMapper.feed`, yields each event, awaits exit, then records `mapper.terminal`. `turn` drains and joins `TextDelta` only.

- [x] **Step 4: Run to verify it passes** — Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(warm-orch): muse stream_turn — one process per turn, parsed offline"
```

---

### Task 5: `interrupt` and a loud `apply_system_prompt`

**Files:**
- Modify: `src/arb_warm_orch/muse_runner.py`
- Test: `tests/test_muse_runner.py`

**Interfaces:**
- Produces: `async interrupt()`; `apply_system_prompt(text: str)`.

Spec §5.4: `acp_server.py:265` does `getattr(runner, "apply_system_prompt", None)` and no-ops when absent. Defining it is what stops Muse becoming the fourth seat that silently drops the composed prompt. Since G2 has not run, it must be **loud**, not silently accepting.

- [x] **Step 1: Write the failing test**

```python
class TestInterruptAndSystemPrompt(unittest.TestCase):
    def test_interrupt_terminates_child_and_keeps_session_id(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(MuseConfig(cwd=Path(d), session_dir=Path(d) / "s"))
            asyncio.run(r.connect()); sid = r.session_id
            killed = []
            class _P:
                returncode = None
                def terminate(self): killed.append(True)
                async def wait(self): return -15
            r._proc = _P()
            asyncio.run(r.interrupt())
            self.assertEqual(killed, [True])
            self.assertEqual(r.session_id, sid)          # NOT rotated
            self.assertIsNone(r._proc)

    def test_interrupt_with_no_child_is_a_noop(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(MuseConfig(cwd=Path(d), session_dir=Path(d) / "s"))
            asyncio.run(r.connect())
            asyncio.run(r.interrupt())                    # must not raise

    def test_apply_system_prompt_is_loud_not_a_silent_noop(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(MuseConfig(cwd=Path(d), session_dir=Path(d) / "s"))
            with self.assertRaises(NotImplementedError) as ctx:
                r.apply_system_prompt("you are a seat")
            self.assertIn("G2", str(ctx.exception))
            self.assertTrue(hasattr(r, "apply_system_prompt"))
```

- [x] **Step 2: Run to verify it fails** — Expected: FAIL, `AttributeError: ... 'interrupt'`

- [x] **Step 3: Implement.** `interrupt()` returns early when `self._proc is None`; else `terminate()`, `await wait()`, set `self._proc = None`, leave `session_id` untouched. `apply_system_prompt(text)` raises `NotImplementedError` naming gate G2 and the reason (`muse exec` has no system-prompt flag; seam reachability unresolved).

- [x] **Step 4: Run to verify it passes** — Expected: PASS.

- [x] **Step 5: Run the whole suite and commit**

```bash
PYTHONPATH=$PWD/src /Volumes/<workspace>/repos/ARB/.venv/bin/python -m pytest tests/test_muse_events.py tests/test_muse_runner.py -q
git add -A && git commit -m "feat(warm-orch): muse interrupt keeps the session id; system-prompt seam is loud"
```

---

## Self-Review

**Spec coverage:** §2.1 two-unit split → Tasks 1+3. §2.2 `stream_turn` → Task 4. §2.3 preamble → Task 1. §3 session inversion → Task 3. §4 interrupt → Task 5. §5.1 event map → Tasks 1–2. §5.2/§5.3 traps → Task 1. §5.4 system prompt → Task 5. §5.5 `ReasoningDelta` → **deferred to G3**; no payload_type is known, so no task can implement it without guessing — recorded here rather than silently dropped. §6 tiers → Task 1–5 are tier 1; tier 2 and fixtures need live capture. §7 gates → separate, not P1 code.

**Placeholder scan:** none — every step carries real code.

**Type consistency:** `MuseEventMapper.feed` returns `list[TurnEvent]` throughout; `normalise_call_id` used identically in Tasks 2 and 4; `build_argv`/`_spawn`/`_proc` names consistent across Tasks 4–5.

**New gate raised by this plan:** **G6 — does `tool.result.call_id` correspond to `task.lifecycle.scheduled.idempotency_key` minus the `tool:` prefix?** If not, tool-call pairing breaks and buzz's idle deadline never resets. Offline tests cannot settle it; it needs one live capture.

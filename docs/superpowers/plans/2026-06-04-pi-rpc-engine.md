# pi-rpc Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `pi-rpc` bridge engine wrapping `pi --mode rpc`, peer to the existing codex/gemini-acp/grok-acp/agy-print engines, with multi-model support and a policy→tools guard.

**Architecture:** A persistent subprocess engine that mirrors `src/agent_redis_bridge/engines/gemini_acp.py` (daemon reader thread → `queue.Queue`, locked `_send`), but speaks pi's JSON-lines RPC (`{"type":...}` commands, streamed events, terminal `agent_end`) read in **binary** mode (LF-only framing). Tools are fixed per-instance at spawn via `--pi-tools`, with a turn-time guard refusing non-trusted turns on full-tools instances.

**Tech Stack:** Python 3, stdlib `subprocess`/`threading`/`queue`/`json`, pytest/unittest. Authoritative spec: `docs/superpowers/specs/2026-06-04-pi-rpc-engine-design.md` (v2). pi RPC protocol: `/home/<user>/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/docs/rpc.md`.

---

## Authoritative references (read before starting)

- **Spec v2** — every design decision and the *why* behind it. This plan implements it.
- **`engines/gemini_acp.py`** — the structural template. Copy its reader-thread/queue/`_send`/`request`/`_get_message`/`stop` shape. The pi engine differs in: binary reading (§3), pi's command/event names (not ACP `session/*`), the policy guard, and no ACP handshake.
- **`engines/base.py`** — the `AgentEngine` Protocol + `TurnResult` the engine must satisfy.
- **`tests/test_gemini_acp.py`** — the unit-test pattern (FakeProcess/FakeStdin/FakeStdout, `popen_factory` injection, pre-seeding `engine.messages`).

### Key protocol facts (from rpc.md)

- Commands: one JSON object per line to stdin. Optional `id` echoed in the matching `{"type":"response","command":...,"id":...,"success":bool,"data":...}`.
- Events (no `id`): `agent_start`, `agent_end` (terminal), `turn_*`, `message_start/update/end`, `tool_execution_start/update/end`, `queue_update`, `compaction_*`, `auto_retry_*`, `extension_error`.
- `message_update.assistantMessageEvent.type` ∈ {`text_delta`, `text_end`, `toolcall_*`, `error` (reason `aborted`/`error`), ...}.
- `tool_execution_end` has `toolName`, `result`, `isError`.
- `get_last_assistant_text` → `data.text` (string or null).
- `extension_ui_request` dialog methods (block, need response): `select`/`confirm`/`input`/`editor`. Fire-and-forget (no response): `notify`/`setStatus`/`setWidget`/`setTitle`/`set_editor_text`.

---

## File structure

- **Create** `src/agent_redis_bridge/engines/pi_rpc.py` — `PiRpcEngine` (the whole engine + a module-level `normalize_pi_event` helper for progress mapping, mirroring gemini's `normalize_session_update`).
- **Modify** `src/agent_redis_bridge/bridge.py` — import, `ENGINE_TO_TOOL`, `--pi-tools` arg, `build_engine` branch.
- **Modify** `scripts/agent-dispatch` — engine→tool `case`, usage string, header comment.
- **Create** `tests/test_pi_rpc.py` — unit tests (binary framing, turn loop, prompt-fail, drain, get_last id-match + fallback, policy guard, ext-ui, steer/interrupt return types, start probe).
- **Create** `tests/test_pi_rpc_e2e.py` — opt-in live e2e (mirror `test_grok_acp_e2e.py`).
- **Create** `.env.pi-dev` example + systemd note (deploy doc).

---

## Task 1: PiRpcEngine skeleton — binary reader, framing, `_send`

**Files:**
- Create: `src/agent_redis_bridge/engines/pi_rpc.py`
- Test: `tests/test_pi_rpc.py`

- [ ] **Step 1: Write the failing framing test.** In `tests/test_pi_rpc.py`:

```python
import json
import queue
import unittest

from agent_redis_bridge.engines.pi_rpc import PiRpcEngine


class FakeStdin:
    def __init__(self): self.chunks = []
    def write(self, value): self.chunks.append(value)   # bytes
    def flush(self): pass


class FakeStdout:
    """Binary stream: yields one b'...\\n'-terminated record per line, splitting on \\n ONLY."""
    def __init__(self, messages):
        blob = b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in messages)
        self._lines = blob.splitlines(keepends=True)   # splits on \n only for bytes? NO — see note
    def __iter__(self): return iter(self._lines)


def encode_lines(messages):
    # Helper: build a raw byte blob and split the way binary readline does (on \n only).
    blob = b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in messages)
    out, buf = [], b""
    for b in blob.split(b"\n")[:-1]:
        out.append(b + b"\n")
    return out


class FakeProcess:
    def __init__(self, byte_lines=None):
        self.stdin = FakeStdin()
        self.stdout = iter(byte_lines or [])
        self.stderr = iter([])
        self.terminated = False
    def terminate(self): self.terminated = True
    def wait(self, timeout=None): return 0
    def kill(self): self.terminated = True
    def poll(self): return None


class PiFramingTest(unittest.TestCase):
    def test_reader_splits_on_lf_only_and_preserves_cr(self):
        # A JSON string value containing a bare \r must NOT split the record.
        msg = {"type": "response", "command": "get_state", "id": "x", "success": True,
               "data": {"note": "line1\rline2"}}
        line = json.dumps(msg).encode("utf-8") + b"\n"
        fake = FakeProcess([line])
        engine = PiRpcEngine(cwd="/tmp/p", model=None, popen_factory=lambda *a, **k: fake)
        engine.process = fake
        engine._read_stdout()  # drains the fake stdout into engine.messages
        got = engine.messages.get_nowait()
        self.assertEqual(got["data"]["note"], "line1\rline2")
```

> **Implementation note for the reader:** spawn with `text=False` (binary). Iterate
> `for raw in self.process.stdout:` — in **binary** mode this splits on `b"\n"` only
> (no universal-newline translation), so a bare `\r` stays inside the record. Then
> `raw = raw.rstrip(b"\n")`, strip a single trailing `b"\r"` (`if raw.endswith(b"\r"): raw = raw[:-1]`),
> `decode("utf-8", errors="replace")`, skip empties, `json.loads`, put dicts on the queue.
> A malformed line is skipped (non-fatal). Do **not** `.strip()` the whole line (that
> would eat meaningful leading/trailing whitespace inside — only strip the delimiters).

- [ ] **Step 2: Run it, verify failure** (`PiRpcEngine` undefined).
  Run: `cd /home/<user>/AgentRedisBridge && PYTHONPATH=src python -m pytest tests/test_pi_rpc.py -q`
  Expected: ImportError / fail.

- [ ] **Step 3: Implement the skeleton.** `__init__(self, *, cwd, model, pi_tools=None, command="pi", popen_factory=subprocess.Popen)` storing those + `process=None`, `messages=queue.Queue()`, `next_id=1`, `send_lock=threading.Lock()`, `active_prompt_id=None`, `healthy=True`. Implement `command_args()` → `[command, "--mode", "rpc", "--no-session"]` + (`["--tools", pi_tools]` if `pi_tools`) + (`["--model", model]` if `model`). Implement `_read_stdout()` per the note above; `_send(payload)` → `json.dumps(payload, separators=(",",":")).encode("utf-8") + b"\n"` written under lock to binary stdin; `_next_request_id()`; `_get_message(timeout)`. `start()` spawns binary Popen + reader thread (readiness probe added in Task 6).

- [ ] **Step 4: Run, verify pass.**
  Run: `PYTHONPATH=src python -m pytest tests/test_pi_rpc.py -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**
  `git add src/agent_redis_bridge/engines/pi_rpc.py tests/test_pi_rpc.py && git commit -m "feat(pi-rpc): engine skeleton + binary LF-only framing"`

---

## Task 2: `run_turn_with_progress` happy path + `normalize_pi_event`

**Files:** Modify `pi_rpc.py`, `tests/test_pi_rpc.py`

- [ ] **Step 1: Write the failing test.** Pre-seed `engine.messages` (like `test_gemini_acp.py` does) with a streamed turn:

```python
class PiTurnTest(unittest.TestCase):
    def _engine(self):
        fake = FakeProcess()
        eng = PiRpcEngine(cwd="/tmp/p", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        return eng, fake

    def test_happy_path_collects_text_and_uses_get_last(self):
        eng, fake = self._engine()
        eng.messages.put({"type": "response", "command": "prompt", "id": 1, "success": True})
        eng.messages.put({"type": "message_update",
                          "assistantMessageEvent": {"type": "text_delta", "delta": "Hel"}})
        eng.messages.put({"type": "message_update",
                          "assistantMessageEvent": {"type": "text_delta", "delta": "lo"}})
        eng.messages.put({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
                          "args": {"command": "ls"}})
        eng.messages.put({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
                          "isError": False, "result": {}})
        eng.messages.put({"type": "agent_end", "messages": []})
        eng.messages.put({"type": "response", "command": "get_last_assistant_text", "id": 2,
                          "success": True, "data": {"text": "Hello"}})
        events = []
        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted",
                                         on_event=lambda e, d: events.append((e, d)))
        self.assertTrue(res.ok)
        self.assertEqual(res.result, "Hello")
        self.assertIn(("model_text", {"delta": "Hel"}), events)
        self.assertTrue(any(e == "command_started" for e, _ in events))
        self.assertTrue(any(e == "command_finished" for e, _ in events))
        sent = [json.loads(c.decode()) for c in fake.stdin.chunks]
        self.assertEqual(sent[0]["type"], "prompt")
        self.assertEqual(sent[0]["message"], "hi")
        self.assertEqual(sent[-1]["type"], "get_last_assistant_text")
```

- [ ] **Step 2: Run, verify fail.** Expected: AttributeError / wrong result.

- [ ] **Step 3: Implement.** Add `normalize_pi_event(event) -> tuple[str, dict] | None`:
  - `message_update` + `assistantMessageEvent.type=="text_delta"` → `("model_text", {"delta": <delta>})`.
  - `tool_execution_start` → `("command_started", {"command": args.get("command") or toolName, "status": "in_progress", "exit_code": None, "tool_call_id": toolCallId, "kind": toolName})`.
  - `tool_execution_end` → `("command_finished", {..., "status": "completed"/"failed", "exit_code": 1 if isError else 0, ...})`.
  - else `None`.
  Implement the turn loop per **spec §4 steps 0–4** (drain is Task 4; here implement send-prompt → loop until `agent_end` mapping via `normalize_pi_event`, accumulating `text_delta` into `chunks`, calling `on_event`). After `agent_end`, call a helper `self._fetch_last_text(deadline)` that sends id-bearing `get_last_assistant_text` and id-matches the response (Task 5 hardens fallback; here just read `data.text`). Return `TurnResult(ok=True, result=text or "".join(chunks).strip())`. Honor `timeout` deadline (Task 4 adds poisoning).

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `feat(pi-rpc): turn loop + event normalization`

---

## Task 3: Prompt `success:false` + error/terminal exits (no-hang)

**Files:** Modify `pi_rpc.py`, `tests/test_pi_rpc.py`

- [ ] **Step 1: Failing tests.**

```python
    def test_rejected_prompt_returns_not_ok_without_agent_end(self):
        eng, fake = self._engine()
        eng.messages.put({"type": "response", "command": "prompt", "id": 1,
                          "success": False, "error": "bad prompt"})
        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)
        self.assertFalse(res.ok)
        self.assertIn("bad prompt", res.error or "")

    def test_message_error_event_ends_turn_not_ok(self):
        eng, fake = self._engine()
        eng.messages.put({"type": "response", "command": "prompt", "id": 1, "success": True})
        eng.messages.put({"type": "message_update",
                          "assistantMessageEvent": {"type": "error", "reason": "error"}})
        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)
        self.assertFalse(res.ok)
```

- [ ] **Step 2: Run, verify fail** (currently hangs to timeout / returns ok).
- [ ] **Step 3: Implement.** In the loop: track the prompt response by `id`; on `success:false` return `ok=False` immediately. Treat `message_update` with `assistantMessageEvent.type=="error"` and `auto_retry_end` with `success:false` as turn-ending → `ok=False`. Exit conditions: `agent_end` | error-event | prompt-fail | timeout.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `feat(pi-rpc): handle prompt rejection + error events (no hang)`

---

## Task 4: Queue drain on entry + timeout poisons the engine

**Files:** Modify `pi_rpc.py`, `tests/test_pi_rpc.py`

- [ ] **Step 1: Failing tests.**

```python
    def test_stale_events_drained_before_prompt(self):
        eng, fake = self._engine()
        eng.messages.put({"type": "agent_end"})       # stale leftover from a prior turn
        eng.messages.put({"type": "response", "command": "prompt", "id": 1, "success": True})
        eng.messages.put({"type": "agent_end"})
        eng.messages.put({"type": "response", "command": "get_last_assistant_text", "id": 2,
                          "success": True, "data": {"text": "ok"}})
        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)
        self.assertTrue(res.ok)
        self.assertEqual(res.result, "ok")   # not terminated by the stale agent_end

    def test_timeout_marks_engine_unhealthy(self):
        eng, fake = self._engine()
        eng.messages.put({"type": "response", "command": "prompt", "id": 1, "success": True})
        # no agent_end -> deadline hits
        res = eng.run_turn_with_progress("hi", timeout=0, policy="trusted", on_event=None)
        self.assertFalse(res.ok)
        self.assertFalse(eng.healthy)
        sent = [json.loads(c.decode()) for c in fake.stdin.chunks]
        self.assertTrue(any(s["type"] == "abort" for s in sent))
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Add a `_drain()` (`get_nowait` until `queue.Empty`) called at the top of `run_turn_with_progress` BEFORE sending the prompt. On timeout: emit `turn_timeout`, `_send({"type":"abort"})`, set `self.healthy = False`, return `ok=False`. (Pool-restart wiring is exercised in Task 8's pool test; the engine just flips the flag — add a `def is_healthy(self) -> bool: return self.healthy` accessor.)
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `feat(pi-rpc): drain stale events + poison engine on timeout`

---

## Task 5: `get_last_assistant_text` id-match + fallback chain

**Files:** Modify `pi_rpc.py`, `tests/test_pi_rpc.py`

- [ ] **Step 1: Failing tests.**

```python
    def test_get_last_ignores_late_events_and_matches_id(self):
        eng, fake = self._engine()
        eng.messages.put({"type": "response", "command": "prompt", "id": 1, "success": True})
        eng.messages.put({"type": "agent_end"})
        eng.messages.put({"type": "queue_update", "steering": [], "followUp": []})  # late noise
        eng.messages.put({"type": "response", "command": "get_last_assistant_text", "id": 2,
                          "success": True, "data": {"text": "final"}})
        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)
        self.assertEqual(res.result, "final")

    def test_empty_text_falls_back_to_placeholder(self):
        eng, fake = self._engine()
        eng.messages.put({"type": "response", "command": "prompt", "id": 1, "success": True})
        eng.messages.put({"type": "agent_end"})
        eng.messages.put({"type": "response", "command": "get_last_assistant_text", "id": 2,
                          "success": True, "data": {"text": None}})
        res = eng.run_turn_with_progress("hi", timeout=2, policy="trusted", on_event=None)
        self.assertTrue(res.ok)
        self.assertIn("completed", res.result)   # placeholder, not empty
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** `_fetch_last_text`: send `{"id":gid,"type":"get_last_assistant_text"}`; loop `_get_message` until a message with `type=="response" and id==gid` (drain/ignore others, bounded by remaining deadline); read `msg["data"]["text"]`. Result precedence in the turn: `data.text` (non-empty) → `"".join(chunks).strip()` (non-empty) → `f"pi-rpc prompt {pid} completed."`.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `feat(pi-rpc): id-matched get_last_assistant_text + fallback`

---

## Task 6: `start()` readiness probe + steer/interrupt/stop returning `str`

**Files:** Modify `pi_rpc.py`, `tests/test_pi_rpc.py`

- [ ] **Step 1: Failing tests.**

```python
    def test_start_probe_raises_when_process_dead(self):
        from agent_redis_bridge.engines.base import EngineError
        fake = FakeProcess()              # no get_state response queued
        eng = PiRpcEngine(cwd="/tmp/p", model=None, popen_factory=lambda *a, **k: fake)
        with self.assertRaises(EngineError):
            eng.start(probe_timeout=0)    # nothing answers -> EngineError

    def test_steer_and_interrupt_return_str(self):
        eng, fake = self._engine()
        eng.active_prompt_id = 7
        self.assertIsInstance(eng.steer("go"), str)
        self.assertIsInstance(eng.interrupt(), str)
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** `start(probe_timeout=10)`: spawn + reader thread, then `_send({"id":pid,"type":"get_state"})`; wait via `_get_message` for the id-matched response within `probe_timeout`; if process `poll()` is not None or no response, `raise EngineError("pi --mode rpc failed readiness probe")`. `steer(message)`: `sid=str(self._next_request_id()); _send({"id":sid,"type":"steer","message":message}); return sid`. `interrupt()`: `_send({"type":"abort"}); return str(self.active_prompt_id or "pi-rpc")`. `stop()`: terminate/wait(5)/kill — copy gemini.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `feat(pi-rpc): start() readiness probe + str-returning steer/interrupt`

---

## Task 7: Policy guard + `extension_ui_request` handling

**Files:** Modify `pi_rpc.py`, `tests/test_pi_rpc.py`

- [ ] **Step 1: Failing tests.**

```python
    def test_full_tools_instance_refuses_nontrusted_turn(self):
        eng, fake = self._engine()       # pi_tools is None -> full tools
        res = eng.run_turn_with_progress("hi", timeout=2, policy="human", on_event=None)
        self.assertFalse(res.ok)
        self.assertIn("non-trusted", (res.error or "").lower())

    def test_review_instance_serves_nontrusted_turn(self):
        fake = FakeProcess()
        eng = PiRpcEngine(cwd="/tmp/p", model=None, pi_tools="read,grep,find,ls",
                          popen_factory=lambda *a, **k: fake)
        eng.process = fake; eng.messages = queue.Queue()
        eng.messages.put({"type": "response", "command": "prompt", "id": 1, "success": True})
        eng.messages.put({"type": "agent_end"})
        eng.messages.put({"type": "response", "command": "get_last_assistant_text", "id": 2,
                          "success": True, "data": {"text": "reviewed"}})
        res = eng.run_turn_with_progress("review", timeout=2, policy="human", on_event=None)
        self.assertTrue(res.ok)

    def test_extension_ui_dialog_is_cancelled_fireforget_ignored(self):
        eng, fake = self._engine()
        eng._handle_client_message({"type": "extension_ui_request", "id": "u1", "method": "confirm",
                                    "title": "ok?"})
        eng._handle_client_message({"type": "extension_ui_request", "id": "u2", "method": "notify",
                                    "message": "hi"})
        sent = [json.loads(c.decode()) for c in fake.stdin.chunks]
        self.assertEqual(len(sent), 1)                       # only the dialog got a reply
        self.assertEqual(sent[0]["type"], "extension_ui_response")
        self.assertEqual(sent[0]["id"], "u1")
        self.assertEqual(sent[0]["confirmed"], False)
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**
  - Guard at top of `run_turn_with_progress` (after `_drain`, before prompt): `if not self.pi_tools and policy != "trusted": return TurnResult(ok=False, result="", error="non-trusted turn refused by full-tools pi instance")`. (A restricted instance — `pi_tools` set — serves any policy.)
  - `_handle_client_message(msg)`: if `msg.get("type")=="extension_ui_request"`: dialog methods (`select`/`confirm`/`input`/`editor`) → reply; `confirm` → `{"type":"extension_ui_response","id":id,"confirmed":False}`; the other three → `{"type":"extension_ui_response","id":id,"cancelled":True}`. Fire-and-forget methods → ignore (no send). Call `_handle_client_message` from the turn loop for any `extension_ui_request` event.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `feat(pi-rpc): policy guard + extension_ui dialog handling`

---

## Task 8: Pool/parallelism test (two engines, no cross-talk)

**Files:** `tests/test_pi_rpc.py` (or `tests/test_pi_rpc_pool.py`)

- [ ] **Step 1: Failing test.** Mirror `tests/test_bridge_parallelism.py`/`test_engine_pool.py`: construct two `PiRpcEngine`s with independent FakeProcesses, run a turn on each concurrently (threads), assert each returns its own result and neither sees the other's events. Assert `is_healthy()` reflects per-engine state.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3:** No new engine code expected (pool-safe by construction); if the test surfaces shared state, fix it (likely none).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `test(pi-rpc): deterministic two-engine parallelism`

---

## Task 9: Wire into bridge.py

**Files:** Modify `src/agent_redis_bridge/bridge.py`; `tests/test_pi_rpc.py` or `tests/test_bridge.py`

- [ ] **Step 1: Failing test.**

```python
    def test_build_engine_returns_pi_rpc(self):
        import argparse
        from agent_redis_bridge.bridge import build_engine, ENGINE_TO_TOOL
        from agent_redis_bridge.engines.pi_rpc import PiRpcEngine
        assert ENGINE_TO_TOOL["pi-rpc"] == "pi"
        args = argparse.Namespace(engine="pi-rpc", model="minimax/MiniMax-M3", pi_tools=None)
        eng = build_engine(args, cwd="/tmp/p")
        assert isinstance(eng, PiRpcEngine)
        assert eng.model == "minimax/MiniMax-M3"
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** In `bridge.py`: `from .engines.pi_rpc import PiRpcEngine`; add `"pi-rpc": "pi"` to `ENGINE_TO_TOOL`; in `build_parser` add `parser.add_argument("--pi-tools", default=os.environ.get("BRIDGE_PI_TOOLS"))`; in `build_engine` add `if args.engine == "pi-rpc": return PiRpcEngine(cwd=cwd, model=args.model, pi_tools=getattr(args, "pi_tools", None))`. (ctl.py auto-covered via its `ENGINE_TO_TOOL` import — no edit.)
- [ ] **Step 4: Run, verify pass** + full suite: `PYTHONPATH=src python -m pytest -q`.
- [ ] **Step 5: Commit.** `feat(pi-rpc): wire engine into bridge.py (ENGINE_TO_TOOL, --pi-tools, build_engine)`

---

## Task 10: Wire into scripts/agent-dispatch

**Files:** Modify `scripts/agent-dispatch`

- [ ] **Step 1:** Add `pi-rpc) TOOL=pi ;;` to the engine→tool `case` (~line 115, next to `grok-acp) TOOL=grok ;;`). Add `pi-rpc` to the usage string (~line 32) and the header comment (~line 2).
- [ ] **Step 2: Verify** with the existing preflight (no live model needed):
  `FROM_AGENT_ID=claude-bridge-dev BRANCH=main AGENT_ENV_FILE=.env.bridge scripts/agent-dispatch --engine pi-rpc --target-id pi-bridge-dev --dry-run-envelope "ping"` — expect a valid envelope with no "unknown engine".
- [ ] **Step 3: Commit.** `feat(pi-rpc): add pi-rpc to agent-dispatch engine map`

---

## Task 11: E2E test + deploy artifacts

**Files:** Create `tests/test_pi_rpc_e2e.py`, `.env.pi-dev`

- [ ] **Step 1:** Mirror `tests/test_grok_acp_e2e.py`: opt-in (`@external`/env-gated), `agent-dispatch --engine pi-rpc --target-id <id>` against a live pi instance; skip cleanly when none registered. Test 1: command-exec with a unique echo marker. Test 2: file-write-and-verify (only when targeting a full-tools instance).
- [ ] **Step 2:** Create `.env.pi-dev` (mirror `.env.bridge`: bus, `AGENT_PROJECT=bridge`, workdir, trusted senders) with comments showing the three launch lines (worker / `--role kimi` / `--role minimax` with the verified model ids `kimi-coding/kimi-k2-thinking`, `minimax/MiniMax-M3`) and `BRIDGE_PI_TOOLS=read,grep,find,ls` for review instances. Add a short note in the file (or README) pointing at the `agent-bridge@` systemd template.
- [ ] **Step 3: Commit.** `test(pi-rpc): opt-in e2e + .env.pi-dev deploy example`

---

## Self-review checklist (run before dispatching)

- **Spec coverage:** §3 framing→T1; §4 turn loop→T2–T5; §4 start probe + steer/interrupt→T6; §5 policy+ext-ui→T7; §5.5 parallelism→T8; §6 model→T9; §7 wiring→T9/T10; §9 tests→T1–T8,T11. ✓
- **No placeholders:** test code is concrete; implementation steps reference exact methods/spec sections.
- **Type consistency:** `pi_tools` (ctor + `--pi-tools` arg + build_engine) used consistently; `healthy`/`is_healthy()`; `normalize_pi_event`; `_fetch_last_text`; `_drain`; `_handle_client_message`.

## Notes for the implementer (codex)

- **Mirror `gemini_acp.py`** for all boilerplate (reader thread lifecycle, `_get_message`, `stop`, lock discipline) — only the binary read, pi command/event names, policy guard, and ext-ui shapes differ.
- Run `PYTHONPATH=src python -m pytest -q` after each task; the whole suite must stay green.
- Reply with the commit SHAs, per-task test counts, and any deviations from this plan.

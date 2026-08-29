# agent-sdk bridge engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** A mutation-capable `agent-sdk` bridge engine wrapping `claude-agent-sdk` (`ClaudeSDKClient`) to drive M3/Kimi/GLM through the bridge, fail-closed-mediated and respawn-durable.

**Architecture:** Decompose into pure/testable units (routing+env, mediation policy, scrubbed file SessionStore, loop-thread) that the engine composes. The engine owns ONE asyncio loop on ONE thread for the client's life; all control marshalled via `run_coroutine_threadsafe`. Almost everything is unit-tested with a MOCK SDK client; one gated live test at the end.

**Tech Stack:** Python 3 (bridge `.venv`), `claude-agent-sdk` 0.2.104, `unittest`+mock, git.

> **⚠️ Correction (2026-06-21) — do not copy the `glm-5.2` ModelSpec below for a live GLM seat.** That
> spec routes GLM to z.ai's Anthropic endpoint (`/api/anthropic`); real agentic dispatches **hang** there
> (z.ai's time-to-first-token scales steeply with input size, and agent-sdk's full system prompt + tool
> schemas push every request past the dispatch timeout). The GLM judge seat runs on the **`pi-sdk`** engine
> instead (`--model zai/glm-5.2`, z.ai Coding-Plan endpoint). Also: if you ever do use this lane, the model
> code must be plain `glm-5.2`, never `glm-5.2[1m]` (z.ai 400-loops the suffix). M3/Kimi on this engine are
> unaffected. See [decisions/m3-judgment-seat.md](../../decisions/m3-judgment-seat.md) §D4 and
> [agent-role-routing.md](../../agent-role-routing.md).

## Global Constraints
- Spec: `docs/superpowers/specs/2026-06-18-agent-sdk-engine-design.md` (authoritative).
- Test cmd: `PYTHONPATH=src .venv/bin/python3 -m unittest discover -s tests` (engine tests live in `tests/`, import `agent_redis_bridge.*`).
- **Mediation gate = `can_use_tool`** (NOT the PreToolUse hook). Normative SDK options for any gated client: `permission_mode="default"`, **`allowed_tools=[]`**, **`setting_sources=[]`** — the tool ceiling is enforced ONLY inside the `can_use_tool` policy. Violating any of these silently fail-OPENs.
- Keys read by env-var name from gitignored `envs/agent-sdk-models-dev.env`; NEVER embedded/printed/committed. Logical model names only: `minimax-m3` / `kimi` / `glm-5.2`.
- `ClaudeSDKClient` has **no `steer()`**; `query()` is an async generator (consume in an async helper, never `asyncio.run(query(...))`). `can_use_tool` requires a streaming (AsyncIterable) prompt.
- One client ↔ one loop ↔ one thread, for life; respawn = new trio with `resume=<last_completed_session_id>`.
- Spike already proved (treat as given): `can_use_tool` denies a Write via `ClaudeSDKClient`; `session_store`+`resume` carries a session across a fresh client.

---

### Task 1: Model routing + slug + isolated-env builder

**Files:** Create `src/agent_redis_bridge/engines/agent_sdk_models.py`; Test `tests/test_agent_sdk_models.py`.

**Interfaces — Produces:** `ModelSpec(name, slug, base_url, model_id, auth_style, key_env, lane_env: dict)`; `MODELS: dict[str, ModelSpec]` keyed by logical name; `resolve(name) -> ModelSpec` (raises `KeyError`); `isolated_env(spec, key, base: dict) -> dict` — returns an env that SETS the selected vendor's vars and NEUTRALIZES (removes) every other `ANTHROPIC_*`/`AGENT_SDK_*` key, so the in-process SDK child can't cross-route. `SENSITIVE_PREFIXES = ("ANTHROPIC_", "AGENT_SDK_")`.

- [ ] **Step 1: failing test**
```python
# tests/test_agent_sdk_models.py
import unittest
from agent_redis_bridge.engines.agent_sdk_models import MODELS, resolve, isolated_env, SENSITIVE_PREFIXES

class ModelsTest(unittest.TestCase):
    def test_three_logical_models_with_short_slugs(self):
        self.assertEqual(set(MODELS), {"minimax-m3","kimi","glm-5.2"})
        for s in MODELS.values():
            self.assertRegex(s.slug, r"^[a-z0-9-]{1,16}$")  # derive_agent_id role pattern
    def test_glm_uses_auth_token_and_lane_env(self):
        g = resolve("glm-5.2")
        self.assertEqual(g.auth_style, "auth-token")
        self.assertIn("ANTHROPIC_DEFAULT_SONNET_MODEL", g.lane_env)
    def test_isolated_env_sets_selected_and_neutralizes_others(self):
        spec = resolve("minimax-m3")
        polluted = {"PATH":"/x","ANTHROPIC_AUTH_TOKEN":"leak","AGENT_SDK_KIMI_KEY":"leak2","ANTHROPIC_BASE_URL":"old"}
        env = isolated_env(spec, "K123", base=polluted)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.minimax.io/anthropic")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "K123")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)   # other auth style neutralized
        self.assertNotIn("AGENT_SDK_KIMI_KEY", env)      # other vendor key neutralized
        self.assertEqual(env["PATH"], "/x")              # non-sensitive preserved
```
- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement**
```python
# src/agent_redis_bridge/engines/agent_sdk_models.py
from __future__ import annotations
from dataclasses import dataclass, field

SENSITIVE_PREFIXES = ("ANTHROPIC_", "AGENT_SDK_")

@dataclass(frozen=True)
class ModelSpec:
    name: str; slug: str; base_url: str; model_id: str; key_env: str
    auth_style: str = "x-api-key"  # or "auth-token"
    lane_env: dict = field(default_factory=dict)

MODELS = {
    "minimax-m3": ModelSpec("minimax-m3","m3","https://api.minimax.io/anthropic","MiniMax-M3","AGENT_SDK_MINIMAX_KEY"),
    "kimi": ModelSpec("kimi","kimi","https://api.kimi.com/coding/","kimi-for-coding","AGENT_SDK_KIMI_KEY"),
    "glm-5.2": ModelSpec("glm-5.2","glm","https://api.z.ai/api/anthropic","","AGENT_SDK_GLM_KEY","auth-token",
        {"ANTHROPIC_DEFAULT_OPUS_MODEL":"glm-5.2[1m]","ANTHROPIC_DEFAULT_SONNET_MODEL":"glm-5.2[1m]",
         "ANTHROPIC_DEFAULT_HAIKU_MODEL":"glm-4.5-air"}),
}

def resolve(name: str) -> ModelSpec:
    return MODELS[name]

def _auth_var(auth_style: str) -> str:
    return "ANTHROPIC_AUTH_TOKEN" if auth_style == "auth-token" else "ANTHROPIC_API_KEY"

def isolated_env(spec: ModelSpec, key: str, *, base: dict) -> dict:
    # start from base minus ALL sensitive vars (neutralize cross-route), then set the selected vendor's
    env = {k: v for k, v in base.items() if not k.startswith(SENSITIVE_PREFIXES)}
    env["ANTHROPIC_BASE_URL"] = spec.base_url
    env[_auth_var(spec.auth_style)] = key
    env.update(spec.lane_env)
    return env
```
- [ ] **Step 4: run, pass.**
- [ ] **Step 5: commit** `git add src/agent_redis_bridge/engines/agent_sdk_models.py tests/test_agent_sdk_models.py && git commit -m "feat(agent-sdk): model routing table + isolated-env builder"`

---

### Task 2: Mediation policy + option preconditions

**Files:** Create `src/agent_redis_bridge/engines/agent_sdk_mediation.py`; Test `tests/test_agent_sdk_mediation.py`.

**Interfaces — Produces:** `parse_ceiling(csv: str) -> frozenset[str]` (raises `MediationError` on empty/degenerate — the readonly-gate lesson); `decide(tool_name: str, *, ceiling: frozenset[str], policy: str) -> tuple[bool, str]` returns `(allow, reason)` — `policy=="trusted"` allows tools in `ceiling`, denies tools outside; non-trusted → deny mutating tools `{Write,Edit,MultiEdit,NotebookEdit,Bash}` (but see Task 5: non-trusted mutation turns are *refused* before tools); unknown/exception → deny. `MUTATING = {...}`. `gated_option_kwargs() -> dict` returns the normative `{permission_mode:"default", allowed_tools:[], setting_sources:[]}`.

- [ ] **Step 1: failing test**
```python
# tests/test_agent_sdk_mediation.py
import unittest
from agent_redis_bridge.engines.agent_sdk_mediation import parse_ceiling, decide, gated_option_kwargs, MediationError

class MediationTest(unittest.TestCase):
    def test_ceiling_empty_refuses(self):
        with self.assertRaises(MediationError): parse_ceiling("")
        with self.assertRaises(MediationError): parse_ceiling(" , ")
    def test_trusted_allows_in_ceiling_denies_outside(self):
        c = parse_ceiling("Read,Write,Bash")
        self.assertTrue(decide("Write", ceiling=c, policy="trusted")[0])
        self.assertFalse(decide("WebFetch", ceiling=c, policy="trusted")[0])
    def test_nontrusted_denies_mutating(self):
        c = parse_ceiling("Read,Write,Bash")
        self.assertFalse(decide("Write", ceiling=c, policy="human")[0])
        self.assertTrue(decide("Read", ceiling=c, policy="human")[0])
    def test_unknown_denies(self):
        self.assertFalse(decide("Frobnicate", ceiling=parse_ceiling("Read"), policy="trusted")[0])
    def test_normative_option_kwargs(self):
        k = gated_option_kwargs()
        self.assertEqual(k["permission_mode"], "default")
        self.assertEqual(k["allowed_tools"], [])
        self.assertEqual(k["setting_sources"], [])
```
- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement**
```python
# src/agent_redis_bridge/engines/agent_sdk_mediation.py
from __future__ import annotations

class MediationError(RuntimeError): pass

MUTATING = frozenset({"Write","Edit","MultiEdit","NotebookEdit","Bash"})

def parse_ceiling(csv: str) -> frozenset[str]:
    tools = frozenset(t.strip() for t in (csv or "").split(",") if t.strip())
    if not tools:
        raise MediationError("agent-sdk tool ceiling is empty; set BRIDGE_AGENT_SDK_TOOLS")
    return tools

def decide(tool_name: str, *, ceiling: frozenset[str], policy: str) -> tuple[bool, str]:
    if tool_name not in ceiling:
        return (False, f"{tool_name} outside ceiling")
    if policy != "trusted" and tool_name in MUTATING:
        return (False, f"{tool_name} denied for non-trusted policy {policy!r}")
    return (True, "allowed")

def gated_option_kwargs() -> dict:
    # NORMATIVE: can_use_tool fires only on the "ask" path; allowed_tools/setting_sources must be empty
    # or the gate is bypassed (fail-OPEN). The ceiling lives ONLY in `decide`.
    return {"permission_mode": "default", "allowed_tools": [], "setting_sources": []}
```
- [ ] **Step 4: run, pass.**
- [ ] **Step 5: commit** `git add src/agent_redis_bridge/engines/agent_sdk_mediation.py tests/test_agent_sdk_mediation.py && git commit -m "feat(agent-sdk): fail-closed can_use_tool mediation policy + normative option preconditions"`

---

### Task 3: Scrubbed file-backed SessionStore

**Files:** Create `src/agent_redis_bridge/engines/agent_sdk_session.py`; Test `tests/test_agent_sdk_session.py`.

**Interfaces — Consumes:** the installed `claude_agent_sdk.SessionStore` protocol — verify the exact
signatures in `types.py` (~L1425-1483): both methods are **async, batch, and key-shaped** —
`async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None` and
`async def load(self, key: SessionKey) -> list[...] | None`, where `key` is a dict like
`{"project_key": ..., "session_id": ...}`. **Produces:** `FileSessionStore(root: Path, agent_id: str)`
(async; persists entries under `root/<agent_id>/<session_id>.jsonl`, keyed by `key["session_id"]`);
`ScrubbedSessionStore(inner, secrets, var_names)` — async-delegates, scrubbing `entries` on `append`
before they reach `inner`. `scrub(text, secrets, var_names)` ported from `tools/agent-sdk-probe/scrub.py`.

- [ ] **Step 1: failing test**
```python
# tests/test_agent_sdk_session.py
import asyncio, tempfile, unittest
from pathlib import Path
from agent_redis_bridge.engines.agent_sdk_session import FileSessionStore, ScrubbedSessionStore, scrub

KEY = {"project_key": "proj", "session_id": "sess1"}

class SessionStoreTest(unittest.TestCase):
    def test_scrub_redacts_value_and_var(self):
        self.assertNotIn("sk-x", scrub("k=sk-x", ["sk-x"], []))
        self.assertNotIn("AGENT_SDK_GLM_KEY", scrub("$AGENT_SDK_GLM_KEY", [], ["AGENT_SDK_GLM_KEY"]))
    def test_scrubbed_store_scrubs_on_append(self):
        seen = []
        class Fake:  # matches the real async batch protocol
            async def append(self, key, entries): seen.extend(entries)
            async def load(self, key): return None
        s = ScrubbedSessionStore(Fake(), secrets=["sk-leak"], var_names=[])
        asyncio.run(s.append(KEY, [{"text": "ran with sk-leak in args"}]))
        self.assertNotIn("sk-leak", str(seen[-1]))
    def test_file_store_roundtrip_namespaced(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                st = FileSessionStore(Path(d), "agent-sdk-x-dev-m3")
                await st.append(KEY, [{"a": 1}])
                self.assertEqual(await st.load(KEY), [{"a": 1}])
                self.assertTrue((Path(d) / "agent-sdk-x-dev-m3").is_dir())
        asyncio.run(scenario())
```
- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement** (`scrub` ported verbatim from the probe; `FileSessionStore` writes `root/<agent_id>/<session_key>.jsonl`, `append` appends a JSON line, `load` reads them; `ScrubbedSessionStore.append` runs `scrub` over `json.dumps(entry)` then re-parses, or scrubs string fields — implement to scrub the serialized form and delegate; `load` delegates). Match whatever `append`/`load` signatures the installed `SessionStore` protocol declares (verify `types.py`); adapt the test's `Fake` to the real signature.
- [ ] **Step 4: run, pass.**
- [ ] **Step 5: commit** `git add src/agent_redis_bridge/engines/agent_sdk_session.py tests/test_agent_sdk_session.py && git commit -m "feat(agent-sdk): file SessionStore + ScrubbedSessionStore decorator"`

---

### Task 4: Background loop-thread marshalling

**Files:** Create `src/agent_redis_bridge/engines/agent_sdk_loop.py`; Test `tests/test_agent_sdk_loop.py`.

**Interfaces — Produces:** `LoopThread()` — owns one asyncio loop on one daemon thread; `start()`, `submit(coro) -> concurrent.futures.Future` (via `run_coroutine_threadsafe`), `stop(timeout)` (stops loop + joins thread). Used so the sync engine API marshals every client call onto the one loop, and control calls (interrupt) are submitted independently of the in-flight turn future (lock-free).

- [ ] **Step 1: failing test**
```python
# tests/test_agent_sdk_loop.py
import asyncio, time, unittest
from agent_redis_bridge.engines.agent_sdk_loop import LoopThread

class LoopThreadTest(unittest.TestCase):
    def test_submit_runs_on_loop_from_another_thread(self):
        lt = LoopThread(); lt.start()
        try:
            self.assertEqual(lt.submit(self._echo(7)).result(timeout=5), 7)
        finally:
            lt.stop(timeout=5)
    async def _echo(self, x): return x
    def test_control_coro_runs_while_long_coro_in_flight(self):
        lt = LoopThread(); lt.start()
        try:
            slow = lt.submit(self._sleep(2))           # in-flight "turn"
            time.sleep(0.2)
            fast = lt.submit(self._echo("interrupt"))   # "control" call, must not block on slow
            self.assertEqual(fast.result(timeout=2), "interrupt")  # returns before slow finishes
            self.assertFalse(slow.done())
            slow.result(timeout=5)
        finally:
            lt.stop(timeout=5)
    async def _sleep(self, s): await asyncio.sleep(s); return "slept"
```
- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement** `LoopThread`: in `start()` create `asyncio.new_event_loop()`, run it on a daemon `threading.Thread(target=loop.run_forever)`; `submit(coro)` = `asyncio.run_coroutine_threadsafe(coro, loop)`; `stop()` = `loop.call_soon_threadsafe(loop.stop)` then `thread.join(timeout)` then `loop.close()`.
- [ ] **Step 4: run, pass** (proves control is lock-free: the fast coro completes while the slow one is still in-flight, on the same loop).
- [ ] **Step 5: commit** `git add src/agent_redis_bridge/engines/agent_sdk_loop.py tests/test_agent_sdk_loop.py && git commit -m "feat(agent-sdk): background loop-thread for async->sync marshalling"`

---

### Task 5: The engine — `AgentSdkEngine` (core)

**Files:** Create `src/agent_redis_bridge/engines/agent_sdk.py`; Test `tests/test_agent_sdk_engine.py`.

**Interfaces — Consumes:** Tasks 1-4 + `claude_agent_sdk.ClaudeSDKClient`. **Produces:** `AgentSdkEngine(cwd, model, *, tool_ceiling, key, session_root, oneshot=False, role_profile=None, ...)` implementing the `AgentEngine` protocol; `supports_continuation = not oneshot`; `consumes_role_profile = True`. The SDK client is injected via a `client_factory` param (default constructs `ClaudeSDKClient`) so unit tests pass a mock.

The engine is the integration seam; test it against a **mock client** that yields scripted messages. Split into focused, individually-tested behaviors. `_gate` MUST be `async def` (the SDK awaits `can_use_tool`). The engine exposes `healthy: bool` + `is_healthy()`.

- [ ] **Step 0 (THE fail-open guard — plan panel P0, unanimous): test the REAL options are fail-closed.**
The mock `client_factory` must CAPTURE the `ClaudeAgentOptions` the engine constructs in `start()`, and a
test must assert they are fail-closed — else a builder who writes `allowed_tools=list(ceiling)` (the
intuitive move, what the spike's `query()` did) passes every other test while silently fail-OPENing the
governed tools (the SDK bypasses `can_use_tool` for anything in `allowed_tools`/ambient
`setting_sources`/non-default `permission_mode`).
```python
def test_engine_builds_failclosed_options(self):
    captured = {}
    def factory(**kw):
        captured["opts"] = kw.get("options")
        return MagicMock()  # connect/query/receive_response mocked
    eng = AgentSdkEngine(cwd=".", model="minimax-m3", tool_ceiling="Read,Write,Bash",
                         key="K", session_root="/tmp/x", client_factory=factory)
    eng.start()
    opts = captured["opts"]
    self.assertEqual(opts.allowed_tools, [])          # ceiling must NOT be here
    self.assertEqual(opts.setting_sources, [])         # no ambient permissions.allow
    self.assertEqual(opts.permission_mode, "default")  # nothing auto-allows
    self.assertTrue(callable(opts.can_use_tool))       # the gate is wired
```
This test MUST fail if the ceiling is put on `allowed_tools` or `setting_sources` is omitted. It is the
single guarantee that the engine is fail-closed in reality, not just in the pure `decide()` function.

- [ ] **Step 1: failing test — silent-death detection + TurnResult mapping**
```python
# tests/test_agent_sdk_engine.py (excerpt)
import unittest
from unittest.mock import MagicMock
from agent_redis_bridge.engines.agent_sdk import AgentSdkEngine
# A fake client yielding messages with NO ResultMessage simulates mid-turn child death.
class _FakeMsg: ...
def _engine(messages, **kw):
    # client_factory returns a mock whose receive_response yields `messages`; build engine with it
    ...
class EngineTest(unittest.TestCase):
    def test_stream_without_result_message_is_failure_and_unhealthy(self):
        eng = _engine(messages=[])  # no ResultMessage
        r = eng.run_turn_with_progress("do x", timeout=30, policy="trusted", on_event=None)
        self.assertFalse(r.ok); self.assertIn("without result", (r.error or "").lower())
        self.assertFalse(eng.healthy)
    def test_result_message_maps_to_turnresult(self):
        eng = _engine(messages=[_result(session_id="sid1", subtype="success", text="done")])
        r = eng.run_turn_with_progress("do x", timeout=30, policy="trusted", on_event=None)
        self.assertTrue(r.ok); self.assertEqual(r.thread_id, "sid1")
    def test_nontrusted_mutation_turn_refused(self):
        eng = _engine(messages=[_result("s","success","")], tool_ceiling="Read,Write,Bash")
        # non-trusted policy: the engine refuses a mutation-capable turn up front
        r = eng.run_turn_with_progress("edit files", timeout=30, policy="human", on_event=None)
        self.assertFalse(r.ok)
    def test_tooluse_events_deduped_by_id(self):
        seen=[]; eng=_engine(messages=[_tooluse("Read","t1"),_tooluse("Read","t1"),_result("s","success","")])
        eng.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=lambda k,d: seen.append((k,d)))
        starts=[d for k,d in seen if k=="command_started"]
        self.assertEqual(len(starts), 1)  # deduped by tool_use_id
```
(Implement the `_result`/`_tooluse`/`_engine` helpers to build mock `claude_agent_sdk` message objects + inject the mock `client_factory`.)
- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement `AgentSdkEngine`** — `start()`: `LoopThread.start()`, build `ClaudeAgentOptions(**gated_option_kwargs(), can_use_tool=self._gate, cwd=self.cwd, model=spec.model_id or None, env=isolated_env(...), session_store=ScrubbedSessionStore(FileSessionStore(session_root, agent_id), [key], [spec.key_env]), system_prompt=role_profile, include_hook_events=False, resume=self._last_session_id)`, connect via `LoopThread.submit`. `_gate(tool, inp, ctx)`: `allow,reason = decide(tool, ceiling=self.ceiling, policy=self._turn_policy)`; emit a scrubbed audit event; return `PermissionResultAllow()`/`PermissionResultDeny(message=reason)`; any exception → Deny. `run_turn_with_progress`: if `policy != "trusted"` and the ceiling intersects MUTATING, **refuse** (`TurnResult(ok=False, error="non-trusted mutation turn refused")`); else under the turn lock, submit a coro that `await client.query(prompt_stream(task))` then iterates `client.receive_response()` mapping events (dedupe `ToolUseBlock` by `tool_use_id`→`command_started`; Text→`model_text`; tracking `saw_result`/`session_id`/`tool_calls`); after the stream, if not `saw_result` → `ok=False` + `self.healthy=False`; else map `ResultMessage`→`TurnResult(ok=True, result, thread_id=session_id, stop_reason, tool_calls)`. `steer`: oneshot→raise, else soft no-op (return "steer not supported mid-turn"). `interrupt`: if active turn, `LoopThread.submit(client.interrupt())`; else no-op. `stop`: cancel future, submit `client.disconnect()`, `LoopThread.stop()`, reap child. Timeout: `future.result(timeout)` raises → mark cancelled, disconnect, reap, `TurnResult(ok=False)`, `healthy=False`. Scrub ALL emitted event payloads + the stderr callback with `[key]`+`[spec.key_env]`. **Reap note:** the
CLI child has NO direct pid handle exposed — `disconnect()` is the reap path (the SDK transport closes
stdin → waits → SIGTERM → waits → SIGKILL; an atexit `_ACTIVE_CHILDREN` is the backstop). So timeout/stop
reap = submit `client.disconnect()` on the loop with a bounded outer wait; do NOT assume a pid to kill.
`can_use_tool` is `async def`.
- [ ] **Step 4: run, pass** (all mock-client behaviors green).
- [ ] **Step 5: commit** `git add src/agent_redis_bridge/engines/agent_sdk.py tests/test_agent_sdk_engine.py && git commit -m "feat(agent-sdk): AgentSdkEngine core (mediated, silent-death-safe, event-mapped)"`

---

### Task 6: Startup guard (positive + deny self-probe logic)

**Files:** Modify `src/agent_redis_bridge/engines/agent_sdk.py`; Test add to `tests/test_agent_sdk_engine.py`.

**Interfaces — Produces:** `AgentSdkEngine.assert_serveable()` — called at `start()` before the engine is usable. Asserts config preconditions (ceiling non-empty; `gated_option_kwargs` applied; `can_use_tool` wired) AND runs a **live self-probe** turn proving BOTH: (a) a ceiling tool (`Read`) is *routed through* `can_use_tool` (positive — the gate is consulted, not pre-allowed), and (b) a non-ceiling/`Write` attempt is **denied**. Raise `EngineError` (refuse to serve) on any failure. Unit-test the LOGIC with a mock client whose `_gate` invocations are recorded; the live probe is exercised in Task 8.

**Placement (plan-panel P1):** `assert_serveable()` runs inside `start()`, and the bridge's `build_engine`
+ daemon call `start()` **before `register()`/serving** — so a non-serveable engine raises and the seat
never registers (mirror how the daemon starts the engine before the inbox loop; verify in `bridge.py`).

- [ ] **Step 1: failing test** — `assert_serveable` raises if the recorded gate never saw a ceiling tool (positive case absent) or if a Write wasn't denied; passes when both hold (mock client scripts a Read tool-use → gate consulted, and a Write → Deny observed).
- [ ] **Step 2-4:** implement + pass.
- [ ] **Step 5: commit** `git commit -am "feat(agent-sdk): startup guard self-probe (positive gate-routing + write-denied), refuse-to-serve"`

---

### Task 7: Bridge integration — wiring, slug, worktree hard-guard, readonly_gate

**Files:** Modify `src/agent_redis_bridge/bridge.py` (`ENGINE_TO_TOOL`, parser choices, `build_engine`, agent-id slug, worktree guard) and `src/agent_redis_bridge/readonly_gate.py`; Test `tests/test_agent_sdk_bridge.py`.

**Interfaces — Consumes:** `AgentSdkEngine` (Task 5/6). **Produces:** `ENGINE_TO_TOOL["agent-sdk"]="agent-sdk"`; a `build_engine` branch constructing `AgentSdkEngine(cwd, model=args.model, tool_ceiling=os.environ.get("BRIDGE_AGENT_SDK_TOOLS"), key=load_key(spec), session_root=..., oneshot=args.agent_sdk_oneshot, role_profile=...)` using the Task-1 slug for the agent-id role; a **worktree hard-guard** in the dispatch path that refuses a trusted mutation turn for an `agent-sdk` stateful seat unless `worktree_spec is not None`; `readonly_gate.py` recognizes `agent-sdk`.

- [ ] **Step 1: failing tests**
```python
# tests/test_agent_sdk_bridge.py
import unittest
from agent_redis_bridge.bridge import ENGINE_TO_TOOL
class BridgeWiringTest(unittest.TestCase):
    def test_engine_to_tool_registered(self):
        self.assertEqual(ENGINE_TO_TOOL["agent-sdk"], "agent-sdk")
    def test_agent_id_uses_short_slug(self):
        # build_engine / derive path yields agent-sdk-<project>-<workspace>-<slug> with slug in {m3,kimi,glm}
        ...
    def test_worktree_guard_refuses_trusted_mutation_without_worktree(self):
        # the guard returns refuse when stateful agent-sdk + trusted + worktree_spec is None
        ...
```
- [ ] **Step 2-4:** implement (mirror the `pi-sdk` build_engine branch + the worktree-spec check that already exists in `process_request`) + pass. Add `--agent-sdk-oneshot` to the parser.
- [ ] **Step 5: commit** `git add -A && git commit -m "feat(agent-sdk): bridge wiring + agent-id slug + worktree hard-guard + readonly_gate branch"`

---

### Task 8: Full suite + GATED live integration

- [ ] **Step 1: full unit suite green** — `PYTHONPATH=src .venv/bin/python3 -m unittest discover -s tests` → all green (incl. existing 324 + the new agent-sdk tests).
- [ ] **Step 2: install + register a stateful M3 seat** (ephemeral, localhost db12) — `--engine agent-sdk --model minimax-m3 --project agentredisbridge --workspace dev` with `BRIDGE_AGENT_SDK_TOOLS=Read,Grep,Glob,Write,Edit,Bash`, env sourced from `envs/agent-sdk-models-dev.env`. Confirm `[bridge] agent-sdk-agentredisbridge-dev-m3 online` + the startup guard passed.
- [ ] **Step 3: live mutation on a worktree** — dispatch a small implement-this task to a pre-created worktree from a trusted sender; confirm the model wrote code via mediated Write calls, the completion gate committed it, and (secret-free) the audit events show allow/deny decisions. Repeat the non-trusted refusal.
- [ ] **Step 4: respawn-resume** — mid-session, kill the `claude` CLI child; confirm the turn fails clean (not a silent PASS), the engine respawns a fresh client and `resume`s the session from the file SessionStore, and continuity holds.
- [ ] **Step 5: one-shot mode** — `--agent-sdk-oneshot` against M3; confirm a stateless turn works and `steer()` raises.
- [ ] **Step 6: secret-free + commit results** — grep keys across logs/results; record outcomes; orchestrator records the engine as shipped (green-light decisions are the user's).

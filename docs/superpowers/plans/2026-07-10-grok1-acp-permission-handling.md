# GROK-1 ACP Permission Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make grok-acp answer ACP permission asks spec-correctly (fail-closed for non-trusted senders), bound deny-loops with a budget, wire real pool quarantine, and make session reuse safe via per-dispatch rotation + sessionId-gated asks.

**Architecture:** All changes live in `src/agent_redis_bridge/engines/grok_acp.py` plus a new shared helper module `engines/_acp.py` (extracted from cursor_acp). The decision core is `_respond_to_client_request(message, *, policy, on_event)`: authorization comes ONLY from the explicitly threaded `policy` (turn loop passes the active policy; `request()` always passes `None` ⇒ unconditional deny). Health is affirmative: `healthy=False` at prompt start, set `True` only on a clean, uninterrupted terminal response. Non-retiring engines rotate `session/new` per dispatch; any ask whose `sessionId` isn't current is denied regardless of policy.

**Tech Stack:** Python 3.12 (repo `.venv`), stdlib only, `unittest`-style tests run under pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-10-grok1-acp-permission-handling-design.md` (v1.3 CLOSED, commit `434070b`). Section references (D1, D2, D3a, D3b, V2–V7) point there.

## Global Constraints

- **Hermetic tests (V6):** CI has no `grok` binary. Every test drives the engine with fake process/queue fixtures. Never spawn `grok`.
- **Zero behavior change for no-ask turns on default (retire-ON) engines (V4a):** existing `tests/test_grok_acp.py` stream assertions must keep passing (the ONLY permitted fixture change is adding `poll()` to `FakeProcess`; the ONLY permitted expectation change is Task 2's removal of the invalid `"approved"` reply shape, which is the bug being fixed).
- **Env knobs, shared with codex, exact names:** `BRIDGE_APPROVAL_DENY_BUDGET` (default `10`), `BRIDGE_APPROVAL_GRACE_S` (default `10`). Do not invent new env vars.
- **Build pin 1 (spec header):** flip `self.session_id` only AFTER a successful `session/new` response.
- **Build pin 2 (spec header):** a JSON-RPC-**error** prompt response counts as UNCLEAN for health marking (engine stays `healthy=False`).
- **Test scope (operator rule):** run TARGETED test files only — `tests/test_grok_acp.py`, `tests/test_grok_approvals.py`, `tests/test_cursor_acp.py`, `tests/test_grok_retire.py` — NEVER the full suite. Command prefix from repo root: `.venv/bin/python -m pytest`.
- **Commits:** one per task, conventional-commit style, exact messages given per task.
- Cursor behavior is UNCHANGED (constraint 5): `tests/test_cursor_acp.py` passes untouched after Task 1.
- **gemini_acp.py and kimi_code_acp.py are OUT OF SCOPE.** Do not edit them even though they contain similar code.

## File Structure

- Create `src/agent_redis_bridge/engines/_acp.py` — shared ACP option-selection helper (`_DENY_MARKERS`, `_select_allow_option`). One responsibility: pick the allow option from a `session/request_permission` payload.
- Modify `src/agent_redis_bridge/engines/cursor_acp.py` — delete its local copies, import from `_acp` (names stay importable from the `cursor_acp` namespace).
- Modify `src/agent_redis_bridge/engines/grok_acp.py` — everything else (D2, D3, D3a, D3b).
- Modify `tests/test_grok_acp.py` — `FakeProcess.poll()` only.
- Create `tests/test_grok_approvals.py` — all new behavior tests (mirrors `tests/test_codex_approvals.py` naming).
- Modify `docs/BACKLOG.md`, `skills/using-agent-bridge/SKILL.md`, `CHANGELOG.md` — Task 10.

---

### Task 1: Shared `_select_allow_option` helper (`engines/_acp.py`) — spec D1

**Files:**
- Create: `src/agent_redis_bridge/engines/_acp.py`
- Modify: `src/agent_redis_bridge/engines/cursor_acp.py` (delete local `_DENY_MARKERS` at ~line 525 and `_select_allow_option` at ~lines 542-566; add import)
- Test: `tests/test_grok_approvals.py` (new file, first test)

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `_acp._select_allow_option(params: dict[str, Any]) -> str | None` and `_acp._DENY_MARKERS: tuple[str, ...]`. Later tasks import `_select_allow_option` in grok_acp. `cursor_acp._select_allow_option` must still resolve (tests import it from there).

- [ ] **Step 1: Write the failing test**

Create `tests/test_grok_approvals.py`:

```python
import json
import queue
import unittest

from agent_redis_bridge.engines._acp import _select_allow_option


ASK_OPTIONS = [
    {"optionId": "allow-edits-session", "name": "Always allow edits", "kind": "allow_always"},
    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
]


class SelectAllowOptionTest(unittest.TestCase):
    def test_prefers_allow_once_over_allow_always(self) -> None:
        self.assertEqual(_select_allow_option({"options": ASK_OPTIONS}), "allow-once")

    def test_no_allow_kind_and_no_allow_substring_returns_none(self) -> None:
        # V3(b) fixture discipline: reject-only optionIds with NO "allow" substring,
        # otherwise the substring fallback defeats the case (cold-Opus r1).
        options = [{"optionId": "deny-once", "name": "Deny", "kind": "reject_once"}]
        self.assertIsNone(_select_allow_option({"options": options}))

    def test_cursor_namespace_still_resolves(self) -> None:
        from agent_redis_bridge.engines.cursor_acp import _select_allow_option as cursor_select
        self.assertIs(cursor_select, _select_allow_option)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'agent_redis_bridge.engines._acp'`

- [ ] **Step 3: Create `src/agent_redis_bridge/engines/_acp.py`**

Move the code VERBATIM from `cursor_acp.py` (copy the exact `_DENY_MARKERS` tuple you find at ~line 525 — do not retype it from memory — and the exact `_select_allow_option` function at ~lines 542-566, including its docstring):

```python
"""Shared ACP client helpers (Agent Client Protocol).

Extracted from cursor_acp so grok_acp (and any future ACP engine) reuses the
panel-reviewed allow-option selection instead of forking it. Behavior must stay
byte-identical to the cursor original (GROK-1 design v1.3, D1).
"""
from __future__ import annotations

from typing import Any

_DENY_MARKERS = (  # <-- REPLACE with the exact tuple deleted from cursor_acp.py
    ...
)


def _select_allow_option(params: dict[str, Any]) -> str | None:
    ...  # <-- REPLACE with the exact function body deleted from cursor_acp.py
```

- [ ] **Step 4: Point cursor_acp at the shared module**

In `src/agent_redis_bridge/engines/cursor_acp.py`: delete the local `_DENY_MARKERS` and `_select_allow_option` definitions, and add to the import block at the top:

```python
from ._acp import _DENY_MARKERS, _select_allow_option
```

(If `ruff`/linting complains `_DENY_MARKERS` is unused in cursor_acp, keep the import anyway — it preserves the public-ish namespace — and add `# noqa: F401`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py tests/test_cursor_acp.py -v`
Expected: ALL PASS (cursor tests unchanged and green proves constraint 5).

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/engines/_acp.py src/agent_redis_bridge/engines/cursor_acp.py tests/test_grok_approvals.py
git commit -m "feat(grok1): extract _select_allow_option into shared engines/_acp (D1)"
```

---

### Task 2: Explicit policy threading + trusted allow + fail-closed floor — spec D2

**Files:**
- Modify: `src/agent_redis_bridge/engines/grok_acp.py` (`set_session_mode_for_policy` ~lines 210-249, `_handle_client_message` ~lines 300-330, `_respond_to_client_request` ~lines 332-359, `request()` call site ~line 289, turn-loop call site ~line 203)
- Modify: `tests/test_grok_acp.py` ONLY if an existing test pinned the old `{"outcome": {"outcome": "approved"}}` reply (that invalid shape is the root-cause bug; update such an assertion to the new `selected`/`cancelled` shapes and note it in the commit body)
- Test: `tests/test_grok_approvals.py`

**Interfaces:**
- Consumes: `_acp._select_allow_option` (Task 1).
- Produces: `_respond_to_client_request(self, message: dict, *, policy: str | None = None, on_event: ProgressCallback | None = None) -> None` and `_handle_client_message(self, message: dict, *, on_event, chunks: list[str], tool_titles: dict[str, str], policy: str | None = None) -> None`. Tasks 3-7 extend the same methods. `self._policy` and `self._auto_approve_permissions` NO LONGER EXIST anywhere in the file.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_grok_approvals.py` (module-level helpers first — later tasks reuse them):

```python
from agent_redis_bridge.engines.grok_acp import GrokAcpEngine


class FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, value: str) -> None:
        self.lines.append(value)

    def flush(self) -> None:
        pass


class FakeStdout:
    def __init__(self, messages: list[dict]) -> None:
        self.lines = [json.dumps(m) + "\n" for m in messages]

    def __iter__(self):
        return iter(self.lines)


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout([])
        self.stderr = FakeStdout([])
        self.terminated = False
        self.exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 0

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        self.terminated = True
        self.exit_code = -9


def make_engine(*, retire: bool = True) -> GrokAcpEngine:
    engine = GrokAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: FakeProcess())
    engine.retire_after_turn = retire
    engine.process = FakeProcess()  # type: ignore[assignment]
    engine.session_id = "sess-1"
    engine.messages = queue.Queue()
    return engine


def ask(rid: int, session_id: str = "sess-1", title: str = "write file") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": {"toolCallId": f"tc-{rid}", "kind": "edit", "title": title},
            "options": list(ASK_OPTIONS),
        },
    }


def sent(engine: GrokAcpEngine) -> list[dict]:
    return [json.loads(line) for line in engine.process.stdin.lines]


class TrustedAllowTest(unittest.TestCase):
    def test_trusted_ask_answered_with_selected_offered_allow_once(self) -> None:
        engine = make_engine()
        engine._respond_to_client_request(ask(0), policy="trusted", on_event=None)
        reply = sent(engine)[-1]
        self.assertEqual(reply["id"], 0)
        self.assertEqual(reply["result"], {"outcome": {"outcome": "selected", "optionId": "allow-once"}})

    def test_trusted_with_no_allow_option_cancels_fail_closed_floor(self) -> None:
        engine = make_engine()
        message = ask(0)
        message["params"]["options"] = [{"optionId": "deny-once", "name": "Deny", "kind": "reject_once"}]
        with self.assertLogs("agent_redis_bridge.engines.grok_acp", level="WARNING"):
            engine._respond_to_client_request(message, policy="trusted", on_event=None)
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})

    def test_trusted_with_malformed_params_cancels(self) -> None:
        engine = make_engine()
        message = ask(0)
        message["params"] = None
        engine._respond_to_client_request(message, policy="trusted", on_event=None)
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})

    def test_stale_decision_state_is_gone(self) -> None:
        # D2: the responder decides ONLY on the threaded policy. The old engine
        # state flags must not exist at all (grok r1 P2-1 / cold-Opus r1 P2).
        engine = make_engine()
        engine.set_session_mode_for_policy("trusted")  # may try set_mode; fake queue is empty -> mode set fails soft
        self.assertFalse(hasattr(engine, "_auto_approve_permissions"))
        self.assertFalse(hasattr(engine, "_policy"))

    def test_unknown_method_still_gets_32601(self) -> None:
        engine = make_engine()
        engine._respond_to_client_request(
            {"jsonrpc": "2.0", "id": 7, "method": "xai/some_new_thing", "params": {}},
            policy="trusted",
            on_event=None,
        )
        reply = sent(engine)[-1]
        self.assertEqual(reply["error"]["code"], -32601)
```

Note on the mode-set call in `test_stale_decision_state_is_gone`: `set_session_mode_for_policy` loops over candidate modes and each `request()` times out against the empty fake queue. Keep the fake-queue path fast by constructing the engine normally — `request()`'s per-mode timeout is 12s × 3 candidates, which is too slow for a unit test. So in that test, monkeypatch the mode call away:

```python
    def test_stale_decision_state_is_gone(self) -> None:
        engine = make_engine()
        engine.request = lambda *a, **k: {}  # type: ignore[method-assign]  # mode RPC not under test
        engine.set_session_mode_for_policy("trusted")
        self.assertFalse(hasattr(engine, "_auto_approve_permissions"))
        self.assertFalse(hasattr(engine, "_policy"))
```

(Use this monkeypatched version, not the first sketch.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py -v`
Expected: `TrustedAllowTest` methods FAIL — the current code replies `{"outcome": {"outcome": "approved"}}` (or `cancelled` since `_auto_approve_permissions` is unset via `getattr`), and `_respond_to_client_request` does not accept `policy=`/`on_event=` kwargs (TypeError).

- [ ] **Step 3: Implement**

In `src/agent_redis_bridge/engines/grok_acp.py`:

(a) Add the import at the top, next to the existing `.base` import:

```python
from ._acp import _select_allow_option
```

(b) In `set_session_mode_for_policy`, DELETE these two lines (~225-227):

```python
        # Remember policy for permission decisions
        self._policy = policy
        self._auto_approve_permissions = (policy == "trusted")
```

(c) Change `_handle_client_message` signature and pass-through (~line 300):

```python
    def _handle_client_message(
        self,
        message: dict[str, Any],
        *,
        on_event: ProgressCallback | None,
        chunks: list[str],
        tool_titles: dict[str, str],
        policy: str | None = None,
    ) -> None:
        if "id" in message and isinstance(message.get("method"), str):
            self._respond_to_client_request(message, policy=policy, on_event=on_event)
            return
        ...  # rest unchanged
```

(d) In the turn loop (~line 203), thread the active policy:

```python
            self._handle_client_message(
                message, on_event=on_event, chunks=chunks, tool_titles=tool_titles, policy=policy
            )
```

`request()`'s call site (~line 289) stays EXACTLY as it is — it thereby passes `policy=None` by default, which IS the design's inter-turn unconditional deny (D2). Add a comment there:

```python
            # policy=None => _respond_to_client_request denies any ask unconditionally
            # (fail-closed floor: no authorizing turn in scope; GROK-1 D2).
            self._handle_client_message(message, on_event=None, chunks=[], tool_titles={})
```

(e) Rewrite `_respond_to_client_request` (~lines 332-359):

```python
    def _respond_to_client_request(
        self,
        message: dict[str, Any],
        *,
        policy: str | None = None,
        on_event: ProgressCallback | None = None,
    ) -> None:
        """Answer a server-initiated request. Authorization comes ONLY from the
        threaded ``policy`` (GROK-1 v1.3 D2): the turn loop passes the active
        turn's policy; ``request()`` passes None, which always denies."""
        method = message.get("method")
        request_id = message.get("id")
        raw_params = message.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

        if method == "session/request_permission":
            if policy == "trusted":
                option_id = _select_allow_option(params)
                if option_id is not None:
                    logger.debug(
                        f"[grok-acp] trusted allow: {self._ask_title(params)!r} via option {option_id!r}"
                    )
                    self._send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
                    })
                    return
                logger.warning(
                    "[grok-acp] permission ask offered no allow option (or params malformed); "
                    "cancelled despite trusted policy (fail-closed floor)"
                )
                self._send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                })
                return
            self._deny_ask(request_id, params, policy=policy, on_event=on_event)
            return

        # Unknown client methods: JSON-RPC -32601 (probe run D: error => no execution).
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"client method not supported in grok-acp: {method}"},
        })

    @staticmethod
    def _ask_title(params: dict[str, Any]) -> str | None:
        tool_call = params.get("toolCall")
        if isinstance(tool_call, dict) and isinstance(tool_call.get("title"), str):
            return tool_call["title"]
        return None

    def _deny_ask(
        self,
        request_id: Any,
        params: dict[str, Any],
        *,
        policy: str | None,
        on_event: ProgressCallback | None,
    ) -> None:
        # Task 3 fills in budget counting + the command_denied event. For now: deny.
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"outcome": {"outcome": "cancelled"}},
        })
        if policy is None:
            logger.warning(
                "[grok-acp] permission ask outside an authorizing turn; denied fail-closed"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py tests/test_grok_acp.py -v`
Expected: new tests PASS. If any `tests/test_grok_acp.py` test fails, inspect it: a failure asserting the old `"approved"` reply shape is the bug being fixed — update that assertion to the new shape. Any OTHER failure means you broke the stream contract — fix your change, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/engines/grok_acp.py tests/test_grok_approvals.py tests/test_grok_acp.py
git commit -m "fix(grok1): spec-correct ACP permission replies with explicit policy threading (D2)

Root cause of GROK-1: 'approved' is not a valid ACP outcome; grok treats
it as non-acceptance and the turn dies. Trusted now answers selected+
offered-optionId via the shared picker; authorization comes only from
the threaded policy (request() waits deny unconditionally); the stale
_policy/_auto_approve_permissions flags are deleted."
```

---

### Task 3: Deny path — budget counting + `command_denied` event — spec D2/D3

**Files:**
- Modify: `src/agent_redis_bridge/engines/grok_acp.py` (`__init__` ~lines 39-68, `_deny_ask` from Task 2)
- Test: `tests/test_grok_approvals.py`

**Interfaces:**
- Consumes: Task 2's `_deny_ask` seam.
- Produces: engine attrs `self.deny_budget: int`, `self.approval_grace_s: float`, `self._deny_count: int`, `self._last_denied_title: str | None`. Event name `command_denied` with payload keys `command, turn_id, item_id, kind, seq, deny_count, deny_budget`. Task 6 reads `_deny_count`/`_last_denied_title`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_grok_approvals.py`:

```python
class DenyPathTest(unittest.TestCase):
    def test_non_trusted_ask_denied_with_event_and_count(self) -> None:
        engine = make_engine()
        engine.active_prompt_id = 2
        events: list[tuple[str, dict]] = []
        engine._respond_to_client_request(
            ask(0, title="rm -rf /"), policy="human", on_event=lambda n, d: events.append((n, d))
        )
        reply = sent(engine)[-1]
        self.assertEqual(reply["id"], 0)
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(engine._deny_count, 1)
        self.assertEqual(engine._last_denied_title, "rm -rf /")
        name, data = events[-1]
        self.assertEqual(name, "command_denied")
        self.assertEqual(data["command"], "rm -rf /")
        self.assertEqual(data["kind"], "command_denied")
        self.assertEqual(data["turn_id"], "2")
        self.assertEqual(data["item_id"], "tc-0")
        self.assertEqual(data["deny_count"], 1)
        self.assertEqual(data["deny_budget"], engine.deny_budget)
        self.assertIsInstance(data["seq"], int)

    def test_non_trusted_deny_without_callback_still_counts(self) -> None:
        # V3(d): budget accounting must not depend on observability wiring.
        engine = make_engine()
        with self.assertLogs("agent_redis_bridge.engines.grok_acp", level="WARNING"):
            engine._respond_to_client_request(ask(0), policy="human", on_event=None)
        self.assertEqual(engine._deny_count, 1)

    def test_policy_none_denies_without_budget_count_or_event(self) -> None:
        # V3(e)/V4(c): inter-turn ask => unconditional deny, unbudgeted, no event.
        engine = make_engine()
        events: list[tuple[str, dict]] = []
        with self.assertLogs("agent_redis_bridge.engines.grok_acp", level="WARNING"):
            engine._respond_to_client_request(ask(0), policy=None, on_event=lambda n, d: events.append((n, d)))
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(engine._deny_count, 0)
        self.assertEqual(events, [])

    def test_deny_budget_env_default_is_ten(self) -> None:
        engine = make_engine()
        self.assertEqual(engine.deny_budget, 10)
        self.assertEqual(engine.approval_grace_s, 10.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py::DenyPathTest -v`
Expected: FAIL — `AttributeError` on `deny_budget`/`_deny_count`.

- [ ] **Step 3: Implement**

(a) In `GrokAcpEngine.__init__`, after the `self._progress_seq = 0` line, add:

```python
        # GROK-1 D2/D3: per-turn approval deny budget (env names shared with codex).
        self.deny_budget = int(os.environ.get("BRIDGE_APPROVAL_DENY_BUDGET", "10"))
        self.approval_grace_s = float(os.environ.get("BRIDGE_APPROVAL_GRACE_S", "10"))
        self._deny_count = 0
        self._last_denied_title: str | None = None
```

(b) Replace Task 2's `_deny_ask` body:

```python
    def _deny_ask(
        self,
        request_id: Any,
        params: dict[str, Any],
        *,
        policy: str | None,
        on_event: ProgressCallback | None,
    ) -> None:
        title = self._ask_title(params)
        if policy is not None:
            # In-turn denies are budget-counted regardless of on_event (V3d);
            # inter-turn (policy=None) denies are timeout-bounded anomalies (D2).
            self._deny_count += 1
            self._last_denied_title = title
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"outcome": {"outcome": "cancelled"}},
        })
        if policy is None:
            logger.warning(
                "[grok-acp] permission ask outside an authorizing turn; denied fail-closed"
            )
            return
        if on_event is None:
            logger.warning(
                f"[grok-acp] denied permission ask (no progress callback): {title!r} "
                f"deny_count={self._deny_count}/{self.deny_budget}"
            )
            return
        tool_call = params.get("toolCall")
        tool_call_id = tool_call.get("toolCallId") if isinstance(tool_call, dict) else None
        turn_id = str(self.active_prompt_id)
        on_event(
            "command_denied",
            {
                "command": title,
                "turn_id": turn_id,
                "item_id": tool_call_id if isinstance(tool_call_id, str) else f"{turn_id}:approval",
                "kind": "command_denied",
                "seq": self._next_progress_seq(),
                "deny_count": self._deny_count,
                "deny_budget": self.deny_budget,
            },
        )
```

**Deny-proof discipline (V2):** these tests assert the reply CONTENTS on the fake stdin — if a future edit deletes the `session/request_permission` arm, the ask falls through to `-32601` and every assertion here goes red. Do not weaken them to "a reply was sent".

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py tests/test_grok_acp.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/engines/grok_acp.py tests/test_grok_approvals.py
git commit -m "feat(grok1): fail-closed deny path with budget counting and command_denied event (D2/D3)"
```

---

### Task 4: sessionId gate on asks — spec D3b (gate half)

**Files:**
- Modify: `src/agent_redis_bridge/engines/grok_acp.py` (`_respond_to_client_request` from Task 2)
- Test: `tests/test_grok_approvals.py`

**Interfaces:**
- Consumes: Tasks 2-3.
- Produces: the gate rule later tasks rely on — an ask whose `params.sessionId != self.session_id` is denied via the `policy=None` path (unbudgeted, no event) even when `policy="trusted"`.

- [ ] **Step 1: Write the failing tests**

```python
class SessionIdGateTest(unittest.TestCase):
    def test_stale_session_ask_denied_even_under_trusted(self) -> None:
        engine = make_engine()  # session_id == "sess-1"
        events: list[tuple[str, dict]] = []
        with self.assertLogs("agent_redis_bridge.engines.grok_acp", level="WARNING"):
            engine._respond_to_client_request(
                ask(0, session_id="sess-OLD"), policy="trusted",
                on_event=lambda n, d: events.append((n, d)),
            )
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(engine._deny_count, 0)   # unbudgeted: not this turn's ask
        self.assertEqual(events, [])              # no event mis-attributed to the turn

    def test_missing_session_id_fails_the_gate(self) -> None:
        engine = make_engine()
        message = ask(0)
        del message["params"]["sessionId"]
        engine._respond_to_client_request(message, policy="trusted", on_event=None)
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})

    def test_current_session_ask_still_allowed_under_trusted(self) -> None:
        engine = make_engine()
        engine._respond_to_client_request(ask(0, session_id="sess-1"), policy="trusted", on_event=None)
        reply = sent(engine)[-1]
        self.assertEqual(reply["result"]["outcome"]["outcome"], "selected")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py::SessionIdGateTest -v`
Expected: `test_stale_session_ask_denied_even_under_trusted` and `test_missing_session_id_fails_the_gate` FAIL (currently answered `selected`).

- [ ] **Step 3: Implement**

In `_respond_to_client_request`, insert the gate as the FIRST check inside the `session/request_permission` branch (before the `policy == "trusted"` check):

```python
        if method == "session/request_permission":
            ask_session = params.get("sessionId")
            if ask_session != self.session_id:
                # GROK-1 v1.3 D3b: a stale ask from an abandoned session is
                # structurally unauthorizable — deny regardless of policy.
                logger.warning(
                    f"[grok-acp] permission ask for non-current session {ask_session!r} "
                    f"(current {self.session_id!r}); denied fail-closed"
                )
                self._send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                })
                return
            ...  # existing trusted / deny logic unchanged below
```

(Malformed params ⇒ `params == {}` ⇒ `ask_session is None != "sess-1"` ⇒ gate denies. The equality check is fail-closed for missing AND mismatched ids, as codex r4 verified.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py tests/test_grok_acp.py -v`
Expected: ALL PASS. NOTE: `TrustedAllowTest.test_trusted_with_malformed_params_cancels` now passes via the gate instead of the floor — that is fine (both are `cancelled`).

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/engines/grok_acp.py tests/test_grok_approvals.py
git commit -m "feat(grok1): deny permission asks from non-current sessions unconditionally (D3b gate)"
```

---

### Task 5: Health machinery — `is_healthy()`, affirmative clean-completion, dead-child exits — spec D3a

**Files:**
- Modify: `src/agent_redis_bridge/engines/grok_acp.py` (`__init__`, `run_turn_with_progress` ~lines 146-208, `interrupt` ~line 256, `_read_stdout` ~lines 374-386)
- Modify: `tests/test_grok_acp.py` (`FakeProcess` gains `poll()`; nothing else)
- Test: `tests/test_grok_approvals.py`

**Interfaces:**
- Consumes: Tasks 2-4.
- Produces: `self.healthy: bool` (init `True`), `self._interrupted: bool`, `is_healthy(self) -> bool`, `_process_exited(self) -> bool`. `EnginePool.release()` quarantines via `is_healthy()` (`engine_pool.py:128`) — Task 8 proves it. Rule: `healthy=False` at prompt start; set `True` ONLY on a cleanly-received, non-error, uninterrupted terminal response (build pins 1-2 in Global Constraints).

- [ ] **Step 1: Add `poll()` to the OLD fixture**

In `tests/test_grok_acp.py`, add to its `FakeProcess` class (after `__init__`):

```python
    def poll(self) -> int | None:
        return 0 if self.terminated else None
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_grok_approvals.py`:

```python
def _turn_messages(*mid: dict) -> list[dict]:
    """set_mode response (id 1), then mid-turn messages, then prompt response (id 2)."""
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        *mid,
        {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}},
    ]


def run_turn(engine: GrokAcpEngine, messages: list[dict], *, policy: str = "trusted", timeout: int = 2):
    for message in messages:
        engine.messages.put(message)
    events: list[tuple[str, dict]] = []
    result = engine.run_turn_with_progress(
        "task", timeout=timeout, policy=policy, on_event=lambda n, d: events.append((n, d))
    )
    return result, events


class HealthTest(unittest.TestCase):
    def test_clean_end_turn_marks_healthy(self) -> None:
        engine = make_engine()
        result, _ = run_turn(engine, _turn_messages())
        self.assertTrue(result.ok)
        self.assertTrue(engine.is_healthy())

    def test_error_prompt_response_is_unclean(self) -> None:
        # Build pin 2: a JSON-RPC-error prompt response leaves the engine unhealthy.
        engine = make_engine()
        result, _ = run_turn(engine, [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "boom"}},
        ])
        self.assertFalse(result.ok)
        self.assertFalse(engine.is_healthy())

    def test_raised_engine_error_leaves_unhealthy(self) -> None:
        # V7(c2): a non-dict result raises EngineError on a LIVE child; the
        # affirmative marking (False at prompt start) must survive the raise.
        from agent_redis_bridge.engines.base import EngineError
        engine = make_engine()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": "not-a-dict"})
        with self.assertRaises(EngineError):
            engine.run_turn_with_progress("task", timeout=2, policy="trusted", on_event=None)
        self.assertFalse(engine.is_healthy())

    def test_dead_child_mid_turn_returns_promptly_and_unhealthy(self) -> None:
        # V7(c): no message ever arrives and the child is dead -> exit, not timeout-spin.
        engine = make_engine()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.process.exit_code = 1  # dead before the prompt resolves
        result, _ = run_turn(engine, [], timeout=30)
        self.assertFalse(result.ok)
        self.assertIn("exited", result.error)
        self.assertFalse(engine.is_healthy())

    def test_reader_death_marks_unhealthy(self) -> None:
        engine = make_engine()

        class ExplodingStdout:
            def __iter__(self):
                raise RuntimeError("pipe torn")

        engine.process.stdout = ExplodingStdout()
        engine._read_stdout()
        self.assertFalse(engine.healthy)

    def test_external_interrupt_makes_turn_unclean(self) -> None:
        engine = make_engine()
        for message in [{"jsonrpc": "2.0", "id": 1, "result": {}}]:
            engine.messages.put(message)
        # interrupt() called mid-turn (e.g. bridge cancel); response still arrives.
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "cancelled"}})
        engine.session_id = "sess-1"

        def on_event(name, data):
            if name == "turn_started":
                engine.interrupt()

        result = engine.run_turn_with_progress("task", timeout=2, policy="trusted", on_event=on_event)
        self.assertFalse(result.ok)
        self.assertFalse(engine.is_healthy())
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py::HealthTest -v`
Expected: FAIL — no `is_healthy` attribute; dead-child case runs to the 30s timeout (kill it if needed and treat as fail).

- [ ] **Step 4: Implement**

(a) `__init__` additions (next to Task 3's block):

```python
        # GROK-1 D3a: affirmative health. False during a turn; True ONLY after a
        # cleanly-received, non-error, uninterrupted terminal response.
        self.healthy = True
        self._interrupted = False
        self._turns_served = 0
```

(b) New methods (place after `interrupt()`):

```python
    def is_healthy(self) -> bool:
        # reader_thread liveness matters: a dead reader with a live child is a
        # DEAF engine — recycling it poisons the next task (cursor CUR-2 parity).
        return (
            self.healthy
            and self.process is not None
            and self.process.poll() is None
            and (self.reader_thread is None or self.reader_thread.is_alive())
        )

    def _process_exited(self) -> bool:
        return self.process is not None and self.process.poll() is not None
```

(c) `interrupt()` — first line of the method body:

```python
        self._interrupted = True
```

(d) `_read_stdout` — wrap the loop (cursor parity):

```python
    def _read_stdout(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    self.messages.put(value)
        except Exception:  # noqa: BLE001 - a dead reader must mark the engine, not die silently
            logger.exception("grok stdout reader died; marking engine unhealthy")
            self.healthy = False
```

(e) `run_turn_with_progress` — at the top, BEFORE `set_session_mode_for_policy`:

```python
        if self.session_id is None:
            raise EngineError("ACP session not started")

        # D3a affirmative marking: unhealthy until this turn PROVES a clean end.
        self.healthy = False
        self._interrupted = False
        self.set_session_mode_for_policy(policy)
        self._deny_count = 0
        self._last_denied_title = None
```

then after `self.active_prompt_id = prompt_id`, add:

```python
        self._turns_served += 1
```

(f) In the turn loop's `if message is None:` branch:

```python
            if message is None:
                if self._process_exited():
                    self.active_prompt_id = None
                    return TurnResult(
                        ok=False,
                        result="".join(chunks).strip(),
                        error="grok process exited unexpectedly",
                    )
                continue
```

(g) In the terminal-response branch (`message.get("id") == prompt_id and "method" not in message`): the `"error" in message` early-return stays as-is (healthy remains False — build pin 2). In the success path, immediately before `self.active_prompt_id = None` / the final `return TurnResult(...)`:

```python
                if not self._interrupted:
                    # Clean, uninterrupted terminal response: the ONLY place
                    # healthy flips back to True (D3a).
                    self.healthy = True
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py tests/test_grok_acp.py -v`
Expected: ALL PASS (old streaming tests unaffected: their turns end cleanly, healthy=True restored).

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/engines/grok_acp.py tests/test_grok_approvals.py tests/test_grok_acp.py
git commit -m "feat(grok1): is_healthy + affirmative clean-completion marking + dead-child exits (D3a)"
```

---

### Task 6: Deny-budget exhaustion — interrupt + bounded grace drain — spec D3

**Files:**
- Modify: `src/agent_redis_bridge/engines/grok_acp.py` (turn loop + new `_exhaust_deny_budget`)
- Test: `tests/test_grok_approvals.py`

**Interfaces:**
- Consumes: Tasks 3 (`_deny_count`, `deny_budget`, `approval_grace_s`, `_last_denied_title`) and 5 (`_interrupted`, `_process_exited`, healthy rules).
- Produces: `_exhaust_deny_budget(self, *, deadline: float, prompt_id: int, policy: str, on_event, chunks: list[str], tool_titles: dict) -> TurnResult`. Both exit arms leave `healthy == False` (an interrupted turn is UNCLEAN — spec D3 step 3, codex r2).

- [ ] **Step 1: Write the failing tests**

```python
class BudgetExhaustionTest(unittest.TestCase):
    def _engine(self) -> GrokAcpEngine:
        engine = make_engine()
        engine.deny_budget = 2
        engine.approval_grace_s = 0.3
        return engine

    def test_grace_success_returns_legible_error_and_unclean(self) -> None:
        engine = self._engine()
        messages = [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            ask(0, title="w1"), ask(1, title="w2"), ask(2, title="w3"),  # 3rd exceeds budget 2
            {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "cancelled"}},
        ]
        result, events = run_turn(engine, messages, policy="human", timeout=5)
        self.assertFalse(result.ok)
        self.assertIn("deny budget exhausted (3 denials)", result.error)
        self.assertIn("w3", result.error)
        wire = sent(engine)
        denials = [m for m in wire if m.get("result", {}).get("outcome", {}).get("outcome") == "cancelled"]
        self.assertEqual(len(denials), 3)  # the exceeding ask is still ANSWERED (D1 holds)
        self.assertTrue(any(m.get("method") == "session/cancel" for m in wire))
        counts = [d["deny_count"] for n, d in events if n == "command_denied"]
        self.assertEqual(counts, [1, 2, 3])
        self.assertFalse(engine.is_healthy())  # grace success is STILL an unclean end

    def test_grace_expiry_returns_within_bound_and_unhealthy(self) -> None:
        import time
        engine = self._engine()
        messages = [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            ask(0), ask(1), ask(2),
            # no prompt response ever
        ]
        start = time.monotonic()
        result, _ = run_turn(engine, messages, policy="human", timeout=30)
        elapsed = time.monotonic() - start
        self.assertFalse(result.ok)
        self.assertIn("deny budget exhausted", result.error)
        self.assertLess(elapsed, 5)  # grace bound (0.3s) not the 30s turn timeout
        self.assertFalse(engine.is_healthy())

    def test_asks_during_grace_are_still_answered(self) -> None:
        engine = self._engine()
        messages = [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            ask(0), ask(1), ask(2),
            ask(3, title="late"),  # arrives during the grace drain
            {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "cancelled"}},
        ]
        result, _ = run_turn(engine, messages, policy="human", timeout=5)
        wire = sent(engine)
        denials = [m for m in wire if m.get("result", {}).get("outcome", {}).get("outcome") == "cancelled"]
        self.assertEqual(len(denials), 4)  # answer-everything holds through the drain
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py::BudgetExhaustionTest -v`
Expected: FAIL — no budget check exists; the turn ends normally on the prompt response with `ok=False` (stopReason cancelled) but WITHOUT the budget error text / `session/cancel`.

- [ ] **Step 3: Implement**

(a) In the turn loop, immediately after the `self._handle_client_message(...)` call:

```python
            if self._deny_count > self.deny_budget:
                return self._exhaust_deny_budget(
                    deadline=deadline,
                    prompt_id=prompt_id,
                    policy=policy,
                    on_event=on_event,
                    chunks=chunks,
                    tool_titles=tool_titles,
                )
```

(b) New method (place after `run_turn_with_progress`):

```python
    def _exhaust_deny_budget(
        self,
        *,
        deadline: float,
        prompt_id: int,
        policy: str,
        on_event: ProgressCallback | None,
        chunks: list[str],
        tool_titles: dict[str, str],
    ) -> TurnResult:
        """D3: the model is deny-looping instead of concluding. Interrupt, drain
        for a bounded grace answering every ask per D2 throughout, return legibly.
        Both arms leave healthy=False: an interrupted turn is an UNCLEAN end and
        the session is never reused (v1.3 D3b)."""
        error = (
            f"approval deny budget exhausted ({self._deny_count} denials); "
            f"last: {self._last_denied_title or '<unknown tool>'}"
        )
        try:
            self.interrupt()
        except EngineError:
            pass  # dead engine can't be interrupted; drain below settles the exit
        grace_deadline = time.monotonic() + max(
            0.0, min(self.approval_grace_s, deadline - time.monotonic())
        )
        while time.monotonic() < grace_deadline:
            message = self._get_message(
                timeout=max(0.05, min(0.5, grace_deadline - time.monotonic()))
            )
            if message is None:
                if self._process_exited():
                    break
                continue
            if message.get("id") == prompt_id and "method" not in message:
                self.active_prompt_id = None
                return TurnResult(ok=False, result="".join(chunks).strip(), error=error)
            self._handle_client_message(
                message, on_event=on_event, chunks=chunks, tool_titles=tool_titles, policy=policy
            )
        self.active_prompt_id = None
        return TurnResult(ok=False, result="".join(chunks).strip(), error=error)
```

(`healthy` is already `False` — set at prompt start; this path never sets it `True`, so both arms quarantine. `self._interrupted` is set by `interrupt()`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py tests/test_grok_acp.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/engines/grok_acp.py tests/test_grok_approvals.py
git commit -m "feat(grok1): deny-budget exhaustion with interrupt + bounded grace drain (D3)"
```

---

### Task 7: Per-dispatch session rotation for non-retiring engines — spec D3b (rotation half)

**Files:**
- Modify: `src/agent_redis_bridge/engines/grok_acp.py` (`run_turn_with_progress` top, new `_rotate_session_if_reused`)
- Test: `tests/test_grok_approvals.py`

**Interfaces:**
- Consumes: Task 5 (`_turns_served`, healthy rules), existing `request()`.
- Produces: `_rotate_session_if_reused(self) -> None` — no-op on retiring engines or before the first turn; otherwise `session/new` + adopt new id. Raises `EngineError` and leaves `healthy=False` on failure. `self.session_id` flips ONLY after a successful response (build pin 1).

- [ ] **Step 1: Write the failing tests**

```python
class SessionRotationTest(unittest.TestCase):
    def test_default_retire_engine_never_rotates(self) -> None:
        engine = make_engine(retire=True)
        engine._turns_served = 1
        result, _ = run_turn(engine, _turn_messages())
        self.assertTrue(result.ok)
        methods = [m.get("method") for m in sent(engine)]
        self.assertNotIn("session/new", methods)  # V4a: retire-ON stream unchanged

    def test_non_retiring_second_turn_rotates_before_prompt(self) -> None:
        engine = make_engine(retire=False)
        engine._turns_served = 1
        messages = [
            {"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "sess-2"}},  # rotation session/new
            {"jsonrpc": "2.0", "id": 2, "result": {}},                        # set_mode
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},  # prompt
        ]
        result, _ = run_turn(engine, messages)
        self.assertTrue(result.ok)
        methods = [m.get("method") for m in sent(engine)]
        self.assertEqual(methods.index("session/new") + 1, methods.index("session/set_mode"))
        self.assertLess(methods.index("session/new"), methods.index("session/prompt"))
        self.assertEqual(engine.session_id, "sess-2")
        prompt = next(m for m in sent(engine) if m.get("method") == "session/prompt")
        self.assertEqual(prompt["params"]["sessionId"], "sess-2")

    def test_non_retiring_first_turn_does_not_rotate(self) -> None:
        engine = make_engine(retire=False)
        engine._turns_served = 0
        result, _ = run_turn(engine, _turn_messages())
        self.assertTrue(result.ok)
        self.assertNotIn("session/new", [m.get("method") for m in sent(engine)])

    def test_failed_rotation_quarantines_and_never_reuses_old_session(self) -> None:
        from agent_redis_bridge.engines.base import EngineError
        engine = make_engine(retire=False)
        engine._turns_served = 1
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "no"}})
        with self.assertRaises(EngineError):
            engine.run_turn_with_progress("task", timeout=2, policy="trusted", on_event=None)
        self.assertFalse(engine.is_healthy())
        self.assertEqual(engine.session_id, "sess-1")  # build pin 1: flip only on success
        self.assertNotIn("session/prompt", [m.get("method") for m in sent(engine)])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py::SessionRotationTest -v`
Expected: `test_non_retiring_second_turn_rotates_before_prompt` and `test_failed_rotation_quarantines...` FAIL (no rotation exists; the id-numbering also mismatches).

- [ ] **Step 3: Implement**

(a) In `run_turn_with_progress`, insert rotation between the affirmative marking and `set_session_mode_for_policy` (rotation must precede mode-set so the mode applies to the NEW session):

```python
        self.healthy = False
        self._interrupted = False
        self._rotate_session_if_reused()
        self.set_session_mode_for_policy(policy)
```

(b) New method:

```python
    def _rotate_session_if_reused(self) -> None:
        """v1.3 D3b: a non-retiring engine keeps its warm process but NEVER reuses
        a session across dispatches — fresh session/new per dispatch (probe run H:
        isolated context; asks carry the new sessionId, so the D3b gate correlates).
        self.session_id flips ONLY after a successful response (build pin 1); on
        failure the engine is quarantined rather than falling back to the old
        session (fail-closed)."""
        if self.retire_after_turn or self._turns_served == 0:
            return
        old_session = self.session_id
        try:
            response = self.request(
                "session/new",
                {"cwd": self.cwd, "mcpServers": local_memory_mcp_servers()},
                timeout=30,
            )
        except EngineError as exc:
            self.healthy = False
            raise EngineError(f"session rotation failed; engine quarantined: {exc}") from exc
        new_session = response.get("sessionId")
        if not isinstance(new_session, str):
            self.healthy = False
            raise EngineError("session rotation returned no sessionId; engine quarantined")
        self.session_id = new_session
        logger.info(f"[grok-acp] rotated session {old_session!r} -> {new_session!r} (fresh context per dispatch)")
```

(Any ask arriving during the rotation's `request()` wait is denied via `policy=None`, and the old sessionId stays current until the flip — the composition cold-Opus r4 verified fail-closed.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py tests/test_grok_acp.py -v`
Expected: ALL PASS (default-retire engines don't rotate, so the old stream tests are untouched).

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/engines/grok_acp.py tests/test_grok_approvals.py
git commit -m "feat(grok1): per-dispatch session rotation on non-retiring engines (D3b)"
```

---

### Task 8: Real EnginePool quarantine proof — spec V7 arms (b)/(d), retire-pinned

**Files:**
- Test: `tests/test_grok_approvals.py` (tests only; no source change expected — if a test fails, the bug is in Tasks 5-7)

**Interfaces:**
- Consumes: `EnginePool` from `agent_redis_bridge.engine_pool` (`EnginePool(factory, max_size)`, `.acquire(task_id)`, `.release(task_id)`, `._idle`), `GrokAcpEngine.is_healthy()` (Task 5).
- Produces: nothing new — an executable proof that the pool actually honors grok health.

- [ ] **Step 1: Write the tests**

```python
class PoolQuarantineTest(unittest.TestCase):
    """V7 pool arms MUST pin retire_after_turn=False: otherwise release()'s
    retire branch stops the engine without consulting is_healthy() and the
    assertion passes vacuously (cold-Opus r3)."""

    def _pooled_engine(self) -> tuple:
        from agent_redis_bridge.engine_pool import EnginePool
        engine = make_engine(retire=False)
        engine.reader_thread = None  # no real reader in fixtures
        pool = EnginePool(lambda: engine, max_size=1)
        acquired = pool.acquire("task-1")
        self.assertIs(acquired, engine)
        return pool, engine

    def test_unhealthy_engine_is_stopped_not_reidled(self) -> None:
        pool, engine = self._pooled_engine()
        engine.healthy = False  # as left by grace expiry / dead child / raise path
        self.assertFalse(engine.is_healthy())
        pool.release("task-1")
        self.assertNotIn(engine, pool._idle)          # never re-idled
        self.assertTrue(engine.process.terminated)     # stop() was called

    def test_healthy_engine_is_reidled_no_over_revocation(self) -> None:
        pool, engine = self._pooled_engine()
        engine.healthy = True  # as left by a clean end_turn
        self.assertTrue(engine.is_healthy())
        pool.release("task-1")
        self.assertIn(engine, pool._idle)
        self.assertFalse(engine.process.terminated)
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py::PoolQuarantineTest -v`
Expected: PASS if Tasks 5-7 are correct (`release()` consults the now-existing `is_healthy()`). If `test_unhealthy_engine_is_stopped_not_reidled` fails, `is_healthy()` is missing or wrong — fix Task 5's code, do NOT touch `engine_pool.py`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_grok_approvals.py
git commit -m "test(grok1): prove EnginePool stops unhealthy grok engines and re-idles clean ones (V7 b/d)"
```

---

### Task 9: Inertness sweep + full targeted run — spec V4

**Files:**
- Test: `tests/test_grok_approvals.py`

**Interfaces:**
- Consumes: everything above.
- Produces: pinned no-ask stream + unknown-method behavior in BOTH wait loops.

- [ ] **Step 1: Write the tests**

```python
class InertnessTest(unittest.TestCase):
    def test_no_ask_trusted_turn_stream_is_unchanged(self) -> None:
        # V4(a): default engine, no asks => exactly the pre-change wire sequence.
        engine = make_engine()
        result, events = run_turn(engine, _turn_messages())
        self.assertTrue(result.ok)
        self.assertTrue(engine.is_healthy())
        methods = [m.get("method") for m in sent(engine)]
        self.assertEqual(methods, ["session/set_mode", "session/prompt"])
        self.assertNotIn("command_denied", [n for n, _ in events])

    def test_unknown_method_in_turn_loop_gets_32601(self) -> None:
        engine = make_engine()
        unknown = {"jsonrpc": "2.0", "id": 9, "method": "xai/mystery", "params": {}}
        result, _ = run_turn(engine, _turn_messages(unknown))
        self.assertTrue(result.ok)
        reply = next(m for m in sent(engine) if m.get("id") == 9 and "error" in m)
        self.assertEqual(reply["error"]["code"], -32601)

    def test_unknown_method_in_request_wait_gets_32601(self) -> None:
        engine = make_engine()
        engine.messages.put({"jsonrpc": "2.0", "id": 9, "method": "xai/mystery", "params": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        engine.request("some/method", {}, timeout=2)
        reply = next(m for m in sent(engine) if m.get("id") == 9 and "error" in m)
        self.assertEqual(reply["error"]["code"], -32601)

    def test_ask_in_request_wait_denied_unconditionally(self) -> None:
        # V4(c): inter-turn ask => deny + log, unbudgeted (policy=None path).
        engine = make_engine()
        engine.messages.put(ask(9))
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        with self.assertLogs("agent_redis_bridge.engines.grok_acp", level="WARNING"):
            engine.request("some/method", {}, timeout=2)
        reply = next(m for m in sent(engine) if m.get("id") == 9)
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(engine._deny_count, 0)
```

- [ ] **Step 2: Run the full targeted set**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py tests/test_grok_acp.py tests/test_cursor_acp.py tests/test_grok_retire.py -v`
Expected: ALL PASS. This is the merge-gate test command — record its output for the review.

- [ ] **Step 3: Commit**

```bash
git add tests/test_grok_approvals.py
git commit -m "test(grok1): inertness sweep — no-ask streams unchanged, -32601 both loops, inter-turn deny (V4)"
```

---

### Task 10: Documentation — CHANGELOG, BACKLOG, skill table

**Files:**
- Modify: `CHANGELOG.md` (new entry at top, matching the file's existing entry format)
- Modify: `docs/BACKLOG.md` § GROK-1 (lines ~572-596)
- Modify: `skills/using-agent-bridge/SKILL.md` (the grok-acp row of the engine-specific diagnostics table)

**Interfaces:** none — prose only. Do NOT mark grok as implementor-viable anywhere yet: that claim is gated on the V5 live gate, which the ORCHESTRATOR runs after merge (not part of this plan).

- [ ] **Step 1: CHANGELOG entry**

Add at the top of `CHANGELOG.md`, matching the existing entry style:

```markdown
## 2026-07-10 — grok-acp: spec-correct permission answers (GROK-1)

**What:** grok-acp now answers `session/request_permission` with the ACP-spec
`selected`+offered-optionId shape (trusted) or `cancelled` (everything else),
decided ONLY by an explicitly threaded per-turn policy; adds a per-turn deny
budget (`BRIDGE_APPROVAL_DENY_BUDGET`, shared with codex) with an interrupt +
bounded grace exit; `is_healthy()` so the engine pool actually quarantines
wedged grok engines; and, for non-retiring seats, per-dispatch `session/new`
rotation with sessionId-gated asks. Shared `_select_allow_option` moved to
`engines/_acp.py` (cursor imports it; behavior unchanged).

**Why:** GROK-1 — the old reply `{"outcome": "approved"}` is not a valid ACP
outcome; grok treated every permission-requiring operation as rejected and the
turn died. The probe artifact (docs/superpowers/probes/2026-07-10-grok1-v1-probe/)
pinned the root cause (controlled A/B, runs A vs B) and refuted the dead-worker
theory. Design: docs/superpowers/specs/2026-07-10-grok1-acp-permission-handling-design.md
(v1.3, 4-round panel, round-4 unanimous).
```

- [ ] **Step 2: BACKLOG § GROK-1 rewrite**

Replace the whole `## GROK-1 …` section body (keep the heading) with:

```markdown
**STATUS: designed (v1.3, panel-closed unanimous) + implemented; AWAITING V5 live
gate before grok brief rules relax.** Design:
`docs/superpowers/specs/2026-07-10-grok1-acp-permission-handling-design.md`.

**Root cause (probe-verified 2026-07-10, controlled A/B):** the adapter answered
`session/request_permission` with `{"outcome": {"outcome": "approved"}}` — not a
valid ACP outcome — so grok treated the reply as non-acceptance: the operation
never executed and the turn died. The `worker quit with fatal: ... 
Auth(AuthorizationRequired)` stderr line is BENIGN (present on successful runs);
the original dead-worker attribution here was wrong. Out-of-cwd READS never ask
at all. No bypass layer exists (`--always-approve`, yolo mode, `allow_always`
grants — all probed inert over ACP). Evidence:
`docs/superpowers/probes/2026-07-10-grok1-v1-probe/` (runs A–I).

**Remaining to close GROK-1:** orchestrator-run V5 live gate (restart
grok-bridge-dev onto the new code; out-of-cwd write task must show the
trusted-allow callback fired + file written + `end_turn`), then relax the
cwd-only/inline grok brief rules in `skills/using-agent-bridge` and correct ARB
Memory `art-d893502c280b1740`. V5b (opt-out isolation gate) only if an opt-out
seat is ever stood up.
```

- [ ] **Step 3: Skill table row update**

In `skills/using-agent-bridge/SKILL.md`, find the grok-acp row in the engine-specific diagnostics table (the one describing `stopReason=cancelled` with causes (a) auth decay / (b) dead permission worker). Rewrite cause (b) in place — keep cause (a) auth-decay verbatim:

```markdown
(b) **Invalid permission reply (GROK-1, root-caused 2026-07-10):** pre-fix bridges
answered permission asks with a non-ACP `"approved"` outcome, so ANY
permission-requiring op (out-of-cwd writes; NOT reads — reads never ask) was
treated as rejected and the turn died. FIXED in `engines/grok_acp.py` (spec-correct
`selected`+optionId; deny budget; sessionId gate). Until the V5 live gate passes on
a restarted seat, keep grok briefs cwd-only with inline replies; after it passes,
out-of-cwd writes are expected to work on trusted dispatches.
```

- [ ] **Step 4: Run the targeted tests one final time and commit**

Run: `.venv/bin/python -m pytest tests/test_grok_approvals.py tests/test_grok_acp.py tests/test_cursor_acp.py tests/test_grok_retire.py -q`
Expected: ALL PASS.

```bash
git add CHANGELOG.md docs/BACKLOG.md skills/using-agent-bridge/SKILL.md
git commit -m "docs(grok1): changelog + BACKLOG root-cause correction + skill failure-shape update"
```

---

## Post-merge orchestrator gates (NOT for the implementing engineer)

These are the spec's remaining verification obligations; they need live seats and fleet-restart discipline, so the warm orchestrator runs them after review + merge:

1. **Implementation review panel** (codex-sol + pi-GLM certify; grok + cold-Opus contribute) against the spec's V-obligations.
2. **V5 live gate:** check running tasks, restart `grok-bridge-dev` onto the new SHA, dispatch a trusted out-of-cwd write task; assert callback-fired evidence (trusted-allow DEBUG record/event) + file content + `end_turn`; control arm = read-only brief unchanged.
3. **Three-store correction on green gate:** relax the skill's grok rules (Task 10 wrote the interim wording), correct ARB Memory `art-d893502c280b1740`, update local memories (`manual-seats-promoted-launchd`, `pi-sdk-glm-wedge-root-cause`, `grok1-design-closed-v13`).
4. **V5b isolation gate** — only if/when an opt-out grok seat is stood up; re-run per grok binary upgrade.
```

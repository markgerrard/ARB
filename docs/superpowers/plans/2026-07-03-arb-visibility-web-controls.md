# ARB Visibility web — Go-parity controls (filters + transcript toggles) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

Authored against `docs/superpowers/specs/2026-07-03-arb-visibility-web-controls-spec.md` (the
already-corrected version, including its §3.4 `isTruncatableOutput` fix), which is itself
authoritative over `docs/superpowers/specs/2026-07-03-arb-visibility-web-controls-design.md`
(three remediation rounds) wherever they differ. Author only — no implementation happened while
writing this plan. The current `src/arb_memory/static/app.js`/`index.html` were read and diffed
against every line citation in the spec before this plan was written; all citations matched byte
for byte, including a `grep` confirming exactly seven existing sites clear or replace
`timelineEl`'s content (five `timelineEl.textContent = ""` sites + two
`timelineEl.textContent = "[error] ..."` sites) — the codebase has not moved since the spec was
written.

## Goal

Port six of the Go TUI's (`tools/arb-watch-go`) keybinding-driven controls to the ARB Visibility
web page as click-driven UI: a status filter, a dynamic agent filter, and four transcript-display
toggles (Timestamps, Labels, Expand output, Full width) — plus the transcript-buffer/rerender
mechanism Go-parity for the toggles requires. No backend changes.

## Architecture

Two new UI zones, both additive to the existing two-pane (`aside#seat-panel` / `section#timeline`)
layout: a full-width filter bar between `<header>` and `<main>` (status + agent dropdowns, a
shown/total count), and a toggle strip as the first child of `<section>`, above `#timeline` (four
`aria-pressed` buttons). Everything is client-side in the same two files as the prior stage
(`src/arb_memory/static/index.html`, `src/arb_memory/static/app.js`); `src/arb_memory/visibility.py`
is read-only reference and is never modified. `app.js` keeps its existing shape — an IIFE exporting
pure, `require()`-able functions for Node contract tests, plus an `init()` closure holding DOM
references and mutable client state. This plan adds five new pure functions (`agentOf`,
`visibleSeats`, `deriveAgentOptions`, `isTruncatableOutput`, `collapseOutput`), two private
persistence helpers, a new `transcriptBuffer` array + `rerenderTranscript()` mechanism so toggling
Timestamps/Labels/Expand-output retroactively re-renders already-displayed transcript lines (not
just future ones — the single largest scope item in this feature, per the design's remediation),
and a `fullWidth` guard threaded through the existing `shouldAutoFetchHistoryPage` pagination
predicate so hiding the sidebar can't trigger a history-fetch burst.

## Tech Stack

Vanilla JS (no framework, no bundler, no `package.json`), CSS custom properties, Starlette/Python
backend (unchanged, untouched). Tests: Python `pytest` driving
`subprocess.run([node, "-e", script])` against `app.js` (the existing harness pattern) plus plain
Python string assertions against `index.html`. All new tests append to the one existing test file,
`tests/arb_memory/test_visibility_web_contract.py` (18 tests today, all passing) — no new test
file, no new framework.

## Global Constraints

Values below are pinned exactly per the spec; no task should re-derive them.

1. **The corrected `isTruncatableOutput` — verbatim, do not revert to a kind-only check:**
   ```js
   function isTruncatableOutput(data) {
     if (!data) {
       return false;
     }
     if (data.tool_name === "apply_patch" && data.meta && data.meta.file) {
       return false; // apply_patch always renders <details>-wrapped HTML (formatTimelineEvent checks
                     // tool_name BEFORE kind — app.js:178), regardless of what `kind` is set to.
     }
     return data.kind === "command_output" || data.kind === "command_finished" || data.kind === "tool_output";
   }
   ```
   This was originally shipped kind-only (no `apply_patch` check) and the spec panel found it wrong
   unanimously, against the actual committed fixture at
   `tests/arb_memory/test_visibility_web_contract.py:311-320` (`kind: "command_finished"` **and**
   `tool_name: "apply_patch"` set together on one real entry). The exclusion check must fire on
   `tool_name`/`meta.file` **independent of and before** the `kind` match.
2. **The 20-entry `module.exports` list (15 existing + 5 new), exact final order:**
   ```js
   module.exports = {
     authHeaders,
     appendTimelineFrame,
     escapeHtml,
     formatTimelineEvent,
     formatTimelineFrame,
     isRealEventId,
     parseFrames,
     reduceSeat,
     streamSSE,
     ageLabel,
     utcDayMonth,
     seatAgeLabel,
     isStaleHistoryGen,
     isScrolledNearBottom,
     shouldAutoFetchHistoryPage,
     agentOf,
     visibleSeats,
     deriveAgentOptions,
     collapseOutput,
     isTruncatableOutput,
   };
   ```
   Note `collapseOutput` is listed **before** `isTruncatableOutput` here even though
   `isTruncatableOutput` is built first in task order below (Task 4 before Task 5) — both are
   standard `function` declarations (hoisted), so textual/declaration order has no effect on
   behavior; Task 5 re-orders the exports block to put `collapseOutput` before `isTruncatableOutput`
   to match this exact final list. The contract test itself sorts both sides before comparing, so
   this reordering is cosmetic fidelity to the spec, not a behavioral requirement.
3. **The 8-value status filter list, exact order:** `all, running, incomplete, done, failed, voted,
   stale, unknown`. `unknown` is a real, reachable value (`_history_seat_state`'s unhandled-event-type
   fallback and `renderSeats`'s own `seat.state || "unknown"` fallback both produce it) and must be
   filterable, not just displayable.
4. **Seven transcript-buffer-clearing sites, not five** — a `grep` for the literal
   `timelineEl.textContent = ""` pattern alone finds only five; two more sites REPLACE (not clear
   to empty) the timeline with an error message and were missed by an earlier five-site pass:
   | # | Function | Current line | Existing code |
   |---|---|---|---|
   | 1 | `clearToken`'s click handler | 403 | `timelineEl.textContent = "";` |
   | 2 | `setSeatSource` | 469 | `timelineEl.textContent = "";` |
   | 3 | `selectSeat` | 569 | `timelineEl.textContent = "";` |
   | 4 | `openOrchestrator` | 602 | `timelineEl.textContent = "";` |
   | 5 | `loadOrchestrators`'s 401/403 branch | 644 | `timelineEl.textContent = "";` |
   | 6 | `startOrchestratorStream`'s non-auth error branch | 621 | `timelineEl.textContent = "[error] " + frame.data.message + "\n";` |
   | 7 | `loadOrchestrators`'s `!response.ok` branch | 651 | `timelineEl.textContent = "[error] /orchestrators " + response.status + "\n";` |
   The rule going forward is "every site that clears OR REPLACES `timelineEl`'s content, without
   exception" — this table is today's concrete instantiation of that rule, not a substitute for it.
   Task 14 re-`grep`s the real, current file for **both** patterns before editing, to catch an
   eighth site if the codebase moved since this table was built.
5. **The `.dim` CSS class is new** (`​.dim{color:var(--ink-500)}`) — zero matches for `.dim` in the
   current stylesheet (verified). It is its own class, not a reuse of an existing selector, even
   though it matches the muted-ink color already used inline elsewhere.
6. **The two-level flex layout replacing both `calc(100vh - 73px)` rules** (outer `body` column +
   inner `section` column):
   ```css
   html, body{ height:100%; }
   body{
     background:var(--paper); color:var(--ink-700); font-family:var(--serif);
     -webkit-font-smoothing:antialiased; line-height:1.4;
     display:flex; flex-direction:column;
   }
   main{
     display:grid; grid-template-columns:minmax(260px,34%) 1fr;
     flex:1 1 auto; min-height:0;
   }
   section{
     min-width:0; background:var(--paper); border-radius:8px;
     display:flex; flex-direction:column;
   }
   #timeline{
     flex:1 1 auto; min-height:0; overflow:auto;
     margin:0; padding:14px 18px; white-space:pre-wrap; font-family:var(--mono); color:var(--ink-700);
   }
   ```
   Both `min-height:calc(100vh - 73px)` occurrences are removed entirely. The
   `@media (max-width:720px)` block is carried forward **verbatim, unchanged**.
7. **`collapseOutput`'s `MAX_COLLAPSED_OUTPUT_LINES = 6`** (verbatim from Go's
   `maxCollapsedOutputLines`) — a module-scope `const`, **not exported**.
8. **The six `localStorage` keys and their read-defaults**, all read once at `init()`, written on
   every change, guarded against a throwing/disabled `localStorage`:
   | Key | Default | Backing variable |
   |---|---|---|
   | `visStatusFilter` | `"all"` | `statusFilter` |
   | `visAgentFilter` | `"all"` | `agentFilter` |
   | `visShowTimestamps` | off (`false`) | `showTimestamps` |
   | `visShowLabels` | off (`false`) | `showLabels` |
   | `visExpanded` | off (`false`) | `expanded` |
   | `visFullWidth` | off (`false`) | `fullWidth` |
   `Clear` (`clearToken`'s handler) must **not** touch any of these six keys or their backing
   variables — it only resets auth/session/seat state, exactly as it does today.
9. **Only Timestamps, Labels, and Expand-output call `rerenderTranscript()`.** Full-width never
   does — it only flips its own `aria-pressed` state and `seatPanelEl.hidden`.
10. **`appendTimelineFrame` gains a third `options` parameter** (an arity change on an existing
    exported function) — the call site must build it from the **current** toggle-state variables
    at call time, not a hardcoded `{ escapeContent: true }`.

## File Structure

| File | Responsibility |
|---|---|
| `src/arb_memory/static/index.html` | `<style>` additions (`#filter-bar`, `#transcript-toolbar`, `.dim`, layout restructure) + new `#filter-bar`/`#transcript-toolbar` markup |
| `src/arb_memory/static/app.js` | Five new pure functions, two private persistence helpers, `rerenderTranscript()`, the transcript buffer, all new `init()` wiring |
| `tests/arb_memory/test_visibility_web_contract.py` | Extended (not replaced) — new tests appended, four existing tests updated with spec-given expected values, no new file |

No new files are created by this plan.

## Ordering

Pure functions and existing-test updates first (Tasks 1–9) — fast, isolated, no DOM harness needed,
and none of them depend on real `index.html` markup existing. Then the stateful `init()` wiring
(Tasks 10–16), verified via the existing `_DOM_HARNESS_JS` fake-DOM harness — this harness's
`document.getElementById` auto-creates any element on demand, so these tasks and their tests run
correctly **before** the real HTML markup exists; do not be alarmed mid-plan that `index.html`
doesn't yet have `#status-filter`/`#toggle-timestamps`/etc. Then the HTML/CSS string-assertion
tasks last (Tasks 17–19) — declarative, lowest regression risk, and the point at which the real
markup catches up to what the JS has been driving against a fake DOM all along. Task 20 re-runs the
full suite as a final gate. Task 21 is the required manual browser check.

**IMPORTANT (plan-panel fix, codex's catch): do NOT open the real page in an actual browser between
Tasks 10 and 18.** The fake-DOM harness's auto-vivifying `getElementById` is what keeps the
AUTOMATED TESTS green throughout Tasks 10–16 — it does not mean the real page works. A genuine
browser's `document.getElementById("status-filter")` returns `null` until Task 17 adds that element
to `index.html`; any subsequent `.addEventListener`/`.value` access on that `null` throws inside
`init()`, likely breaking page initialization entirely (not just the new controls — potentially the
already-shipped live/history feature too, since it's the same `init()` function). This is expected
and does not indicate a bug in Tasks 10–16 themselves — verification for this whole span is the
automated test suite only (`pytest`, per each task's own Step 2/4), never a manual page load. The
required manual browser check (Task 21) is intentionally the LAST task, after Task 17-18 lands the
real markup and Task 19 lands the CSS — that is the only point in this plan a live browser check is
meaningful.

## Test command (run after every task that touches `app.js` or `index.html`)

```bash
PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
```

---

## Task 1 — `agentOf`: seat-id → agent label, ported from `reduce.go:111-134`

**Files:**
- Modify: `src/arb_memory/static/app.js` (new function before `function init() {`, currently `app.js:333`; `module.exports` block, currently `app.js:691-709`)
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Produces: `agentOf(seatId: string) => string` — module-scope, pure, exported. Used by Task 2
  (`visibleSeats`) and Task 3 (`deriveAgentOptions`).

- [ ] **Step 1: Write the failing test**
  ```python
  def test_appjs_agent_of_ported_cases():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the agentOf ported-cases contract test")
      script = textwrap.dedent(
          f"""
          const {{ agentOf }} = require({json.dumps(str(APP_JS))});
          console.log(JSON.stringify({{
            coldOpus: agentOf("cold-opus-1"),
            empty: agentOf(""),
            codex: agentOf("codex-1"),
            agy: agentOf("agy-1"),
            pi: agentOf("pi-1"),
            gemini: agentOf("gemini-1"),
            cursor: agentOf("cursor-1"),
            grok: agentOf("grok-1"),
            kimi: agentOf("kimi-1"),
            claude: agentOf("claude-1"),
            unknownLowercased: agentOf("Foo-bar-1"),
          }}));
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      assert json.loads(completed.stdout) == {
          "coldOpus": "opus",
          "empty": "?",
          "codex": "codex",
          "agy": "agy",
          "pi": "pi",
          "gemini": "gemini",
          "cursor": "cursor",
          "grok": "grok",
          "kimi": "kimi",
          "claude": "claude",
          "unknownLowercased": "foo",
      }
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k agent_of
  ```
  Expected: FAIL — `agentOf` is not exported (`TypeError: agentOf is not a function` or `undefined`).

- [ ] **Step 3: Write minimal implementation**
  Insert directly before `function init() {` (`app.js:333`):
  ```js
  function agentOf(seatId) {
    const lower = String(seatId || "").toLowerCase();
    if (lower.startsWith("cold-opus-")) {
      return "opus";
    }
    const head = lower.split("-")[0];
    if (!head) {
      return "?";
    }
    const labels = {
      codex: "codex", agy: "agy", pi: "pi", gemini: "gemini",
      cursor: "cursor", grok: "grok", kimi: "kimi", claude: "claude",
    };
    return labels[head] || head;
  }
  ```
  Add `agentOf,` at the end of the `module.exports` object (`app.js:691-709`).

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: 19 passed (18 existing + this one). `test_appjs_exports_full_contract_surface` is
  expected to now be **RED** (16 exported names vs. its still-15-name expected list) — this is
  known, planned collateral breakage; it stays red through Task 5 and is fixed in Task 6. Confirm
  via `-k agent_of` that only the new test passes, and via the full run that
  `test_appjs_exports_full_contract_surface` is the only failure.

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): port agentOf from Go for the agent filter"
  ```

---

## Task 2 — `visibleSeats`: status/agent filtering

**Files:**
- Modify: `src/arb_memory/static/app.js`
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Consumes: `agentOf(seatId) => string` (Task 1).
- Produces: `visibleSeats(seatMap: Record<string,object>, statusFilter: string, agentFilter: string) => object[]`
  — module-scope, pure, exported, unsorted. Used by Task 11 (`renderSeats`).

- [ ] **Step 1: Write the failing test**
  ```python
  def test_appjs_visible_seats_filters_by_status_and_agent():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the visibleSeats contract test")
      script = textwrap.dedent(
          f"""
          const {{ visibleSeats }} = require({json.dumps(str(APP_JS))});
          const seats = {{
            "t-1": {{ task_id: "t-1", seat_id: "codex-1", state: "running" }},
            "t-2": {{ task_id: "t-2", seat_id: "codex-2", state: "done" }},
            "t-3": {{ task_id: "t-3", seat_id: "agy-1", state: "failed" }},
            "t-4": {{ task_id: "t-4", seat_id: "Foo-1" }},
          }};
          console.log(JSON.stringify({{
            all: visibleSeats(seats, "all", "all").map((s) => s.task_id).sort(),
            running: visibleSeats(seats, "running", "all").map((s) => s.task_id),
            unknownStatus: visibleSeats(seats, "unknown", "all").map((s) => s.task_id),
            codexAgent: visibleSeats(seats, "all", "codex").map((s) => s.task_id).sort(),
          }}));
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert result["all"] == ["t-1", "t-2", "t-3", "t-4"]
      assert result["running"] == ["t-1"]
      assert result["unknownStatus"] == ["t-4"]
      assert result["codexAgent"] == ["t-1", "t-2"]
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k visible_seats
  ```
  Expected: FAIL — `visibleSeats` not exported.

- [ ] **Step 3: Write minimal implementation**
  Insert directly after `agentOf`'s closing brace:
  ```js
  function visibleSeats(seatMap, statusFilter, agentFilter) {
    return Object.values(seatMap).filter((seat) => {
      const state = seat.state || "unknown";
      if (statusFilter !== "all" && state !== statusFilter) {
        return false;
      }
      const agent = agentOf(seat.seat_id || "");
      if (agentFilter !== "all" && agent !== agentFilter) {
        return false;
      }
      return true;
    });
  }
  ```
  Add `visibleSeats,` at the end of `module.exports`.

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: 20 passed, `test_appjs_exports_full_contract_surface` still the sole known failure.

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): add visibleSeats status/agent filtering"
  ```

---

## Task 3 — `deriveAgentOptions`: dynamic agent-dropdown derivation

**Files:**
- Modify: `src/arb_memory/static/app.js`
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Consumes: `agentOf(seatId) => string` (Task 1).
- Produces: `deriveAgentOptions(seats: Record<string,object>) => string[]` — module-scope, pure,
  exported. Used by Task 11 (`updateAgentFilterOptions`).

- [ ] **Step 1: Write the failing test**
  ```python
  def test_appjs_derive_agent_options_dedupes_sorts_and_prepends_all():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the deriveAgentOptions contract test")
      script = textwrap.dedent(
          f"""
          const {{ deriveAgentOptions }} = require({json.dumps(str(APP_JS))});
          const seats = {{
            "t-1": {{ seat_id: "codex-1" }},
            "t-2": {{ seat_id: "codex-2" }},
            "t-3": {{ seat_id: "agy-1" }},
            "t-4": {{ seat_id: "Foo-1" }},
          }};
          console.log(JSON.stringify(deriveAgentOptions(seats)));
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      assert json.loads(completed.stdout) == ["all", "agy", "codex", "foo"]
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k derive_agent_options
  ```
  Expected: FAIL — `deriveAgentOptions` not exported.

- [ ] **Step 3: Write minimal implementation**
  Insert directly after `visibleSeats`'s closing brace:
  ```js
  function deriveAgentOptions(seats) {
    const set = new Set();
    Object.values(seats).forEach((seat) => {
      set.add(agentOf(seat.seat_id || ""));
    });
    return ["all", ...Array.from(set).sort()];
  }
  ```
  Add `deriveAgentOptions,` at the end of `module.exports`.

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: 21 passed, `test_appjs_exports_full_contract_surface` still the sole known failure.

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): add deriveAgentOptions for the dynamic agent filter"
  ```

---

## Task 4 — `isTruncatableOutput`: corrected field-scope check

**Files:**
- Modify: `src/arb_memory/static/app.js`
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Produces: `isTruncatableOutput(data: object) => boolean` — module-scope, pure, exported. Used by
  Task 5 (`collapseOutput`).

- [ ] **Step 1: Write the failing test** (the spec panel's load-bearing case)
  ```python
  def test_appjs_is_truncatable_output_field_scope():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the isTruncatableOutput field-scope contract test")
      script = textwrap.dedent(
          f"""
          const {{ isTruncatableOutput }} = require({json.dumps(str(APP_JS))});
          console.log(JSON.stringify({{
            commandOutput: isTruncatableOutput({{ kind: "command_output" }}),
            commandFinished: isTruncatableOutput({{ kind: "command_finished" }}),
            toolOutput: isTruncatableOutput({{ kind: "tool_output" }}),
            applyPatchWithMatchingKind: isTruncatableOutput({{
              kind: "command_finished", tool_name: "apply_patch", meta: {{ file: "x.py" }},
            }}),
            applyPatchKindLiteral: isTruncatableOutput({{ kind: "apply_patch" }}),
            applyPatchNoKind: isTruncatableOutput({{ tool_name: "apply_patch", meta: {{ file: "x" }} }}),
            applyPatchNoMetaFile: isTruncatableOutput({{ kind: "command_output", tool_name: "apply_patch", meta: {{}} }}),
            nullData: isTruncatableOutput(null),
            emptyData: isTruncatableOutput({{}}),
          }}));
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert result["commandOutput"] is True
      assert result["commandFinished"] is True
      assert result["toolOutput"] is True
      assert result["applyPatchWithMatchingKind"] is False
      assert result["applyPatchKindLiteral"] is False
      assert result["applyPatchNoKind"] is False
      assert result["applyPatchNoMetaFile"] is True
      assert result["nullData"] is False
      assert result["emptyData"] is False
  ```
  `applyPatchWithMatchingKind` is the exact fixture shape from
  `tests/arb_memory/test_visibility_web_contract.py:311-320` — a naive kind-only check would return
  `True` here; this assertion is what the spec panel's fix exists to prove.

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k is_truncatable_output
  ```
  Expected: FAIL — `isTruncatableOutput` not exported.

- [ ] **Step 3: Write minimal implementation**
  Insert directly after `deriveAgentOptions`'s closing brace (verbatim from Global Constraint 1):
  ```js
  function isTruncatableOutput(data) {
    if (!data) {
      return false;
    }
    if (data.tool_name === "apply_patch" && data.meta && data.meta.file) {
      return false; // apply_patch always renders <details>-wrapped HTML (formatTimelineEvent checks
                    // tool_name BEFORE kind — app.js:178), regardless of what `kind` is set to.
    }
    return data.kind === "command_output" || data.kind === "command_finished" || data.kind === "tool_output";
  }
  ```
  Add `isTruncatableOutput,` at the end of `module.exports`.

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: 22 passed, `test_appjs_exports_full_contract_surface` still the sole known failure.

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): add corrected isTruncatableOutput (apply_patch exclusion)"
  ```

---

## Task 5 — `collapseOutput`: line-6 truncation on the rendered string

**Files:**
- Modify: `src/arb_memory/static/app.js`
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Consumes: `isTruncatableOutput(data) => boolean` (Task 4).
- Produces: `collapseOutput(data: object, rendered: string, expanded: boolean) => string` —
  module-scope, pure, exported. Used by Task 9 (`appendTimelineFrame`) and Task 12
  (`rerenderTranscript`).

- [ ] **Step 1: Write the failing tests**
  ```python
  def test_appjs_collapse_output_truncates_long_plain_text_output():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the collapseOutput truncation contract test")
      script = textwrap.dedent(
          f"""
          const {{ collapseOutput }} = require({json.dumps(str(APP_JS))});
          const data = {{ kind: "command_output" }};
          const nineLines = ["l1","l2","l3","l4","l5","l6","l7","l8","l9"].join("\\n");
          const shortLines = "one\\ntwo";
          console.log(JSON.stringify({{
            truncated: collapseOutput(data, nineLines, false),
            expanded: collapseOutput(data, nineLines, true),
            short: collapseOutput(data, shortLines, false),
          }}));
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert result["truncated"] == (
          "l1\nl2\nl3\nl4\nl5\nl6\n"
          '<span class="dim">… +3 line(s) — click Expand output to see more</span>'
      )
      assert result["expanded"] == "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9"
      assert result["short"] == "one\ntwo"


  def test_appjs_collapse_output_excludes_apply_patch_and_model_thinking():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the collapseOutput exclusion contract test")
      script = textwrap.dedent(
          f"""
          const {{ collapseOutput }} = require({json.dumps(str(APP_JS))});
          const applyPatchData = {{
            kind: "command_finished", tool_name: "apply_patch", meta: {{ file: "foo.py" }},
          }};
          const applyPatchRendered = [
            "edited `foo.py` +3/-1",
            "<details><summary>diff</summary>",
            "line1", "line2", "line3", "line4", "line5", "line6",
            "</details>",
          ].join("\\n");
          const thinkingData = {{ kind: "model_thinking" }};
          const thinkingRendered = [
            "<details><summary>thinking</summary>",
            "line1", "line2", "line3", "line4", "line5", "line6",
            "</details>",
          ].join("\\n");
          console.log(JSON.stringify({{
            applyPatch: collapseOutput(applyPatchData, applyPatchRendered, false),
            thinking: collapseOutput(thinkingData, thinkingRendered, false),
          }}));
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      apply_patch_rendered = "\n".join([
          "edited `foo.py` +3/-1",
          "<details><summary>diff</summary>",
          "line1", "line2", "line3", "line4", "line5", "line6",
          "</details>",
      ])
      thinking_rendered = "\n".join([
          "<details><summary>thinking</summary>",
          "line1", "line2", "line3", "line4", "line5", "line6",
          "</details>",
      ])
      assert result["applyPatch"] == apply_patch_rendered
      assert result["thinking"] == thinking_rendered
  ```
  Both fixtures in the second test are 9 lines (would truncate if `isTruncatableOutput` were
  wrong); asserting the FULL string is returned unchanged proves neither can ever have its
  `<details>` tag cut mid-tag.

- [ ] **Step 2: Run tests to verify they fail**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k collapse_output
  ```
  Expected: FAIL — `collapseOutput` not exported.

- [ ] **Step 3: Write minimal implementation**
  Insert directly after `isTruncatableOutput`'s closing brace:
  ```js
  const MAX_COLLAPSED_OUTPUT_LINES = 6; // verbatim from Go's maxCollapsedOutputLines

  function collapseOutput(data, rendered, expanded) {
    if (expanded || !isTruncatableOutput(data)) {
      return rendered;
    }
    const lines = rendered.split("\n");
    if (lines.length <= MAX_COLLAPSED_OUTPUT_LINES) {
      return rendered;
    }
    const hidden = lines.length - MAX_COLLAPSED_OUTPUT_LINES;
    const kept = lines.slice(0, MAX_COLLAPSED_OUTPUT_LINES).join("\n");
    return kept + "\n" + '<span class="dim">… +' + hidden + " line(s) — click Expand output to see more</span>";
  }
  ```
  In `module.exports`, insert `collapseOutput,` **immediately before** the `isTruncatableOutput,`
  line already added by Task 4 (matching Global Constraint 2's exact final order), not after it.

- [ ] **Step 4: Run tests to verify they pass**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: 24 passed. `test_appjs_exports_full_contract_surface` is now the **only** failing test
  in the file (all 5 new pure functions exported, exports test not yet updated) — the next task
  fixes this.

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): add collapseOutput line-6 truncation"
  ```

---

## Task 6 — Existing-test update: `test_appjs_exports_full_contract_surface` (20 entries)

**Files:**
- Modify: `tests/arb_memory/test_visibility_web_contract.py` only — no production code change (all
  5 exports already landed in Tasks 1–5).

**Interfaces:** none new.

- [ ] **Step 1: Update the test's expected list** (spec §5.2 item 9, copied verbatim — do not
  re-derive)
  Replace the `expected = sorted([...])` block in `test_appjs_exports_full_contract_surface`:
  ```python
  expected = sorted([
      "authHeaders", "appendTimelineFrame", "escapeHtml", "formatTimelineEvent",
      "formatTimelineFrame", "isRealEventId", "parseFrames", "reduceSeat", "streamSSE",
      "ageLabel", "utcDayMonth", "seatAgeLabel", "isStaleHistoryGen",
      "isScrolledNearBottom", "shouldAutoFetchHistoryPage",
      "agentOf", "visibleSeats", "deriveAgentOptions", "collapseOutput", "isTruncatableOutput",
  ])
  ```

- [ ] **Step 2: Run the full file**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 24 tests green — this was the last known-red test from Tasks 1–5.

- [ ] **Step 3: Commit**
  ```bash
  git add tests/arb_memory/test_visibility_web_contract.py
  git commit -m "test(arb-visibility-web): update export-surface contract to 20 entries"
  ```

---

## Task 7 — `shouldAutoFetchHistoryPage`: `fullWidth` guard (function only, not the real call site)

**Files:**
- Modify: `src/arb_memory/static/app.js` (`shouldAutoFetchHistoryPage`, currently `app.js:326-331`)
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Changes: `shouldAutoFetchHistoryPage({seatSource, historyHasMore, historyLoading, historyCursor,
  scrollHeight, clientHeight})` → adds `fullWidth` key to the same destructured parameter. The real
  production call site (`applyHistoryPage`) is wired in Task 16, once the closure variable
  `fullWidth` exists (Task 10).

- [ ] **Step 1: Write the failing test update** (spec §5.2 item 10, copied verbatim)
  Replace `test_appjs_viewport_fill_fallback_triggers_additional_fetch`'s body:
  ```python
  def test_appjs_viewport_fill_fallback_triggers_additional_fetch():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the viewport-fill fallback contract test")
      script = textwrap.dedent(
          f"""
          const {{ shouldAutoFetchHistoryPage }} = require({json.dumps(str(APP_JS))});
          const base = {{
            seatSource: "history", historyHasMore: true, historyLoading: false,
            historyCursor: "cur-1", scrollHeight: 100, clientHeight: 400, fullWidth: false,
          }};
          console.log(JSON.stringify({{
            fitsViewport: shouldAutoFetchHistoryPage(base),
            overflowsViewport: shouldAutoFetchHistoryPage({{...base, scrollHeight: 800}}),
            stillLoading: shouldAutoFetchHistoryPage({{...base, historyLoading: true}}),
            liveMode: shouldAutoFetchHistoryPage({{...base, seatSource: "live"}}),
            noMorePages: shouldAutoFetchHistoryPage({{...base, historyHasMore: false}}),
            noCursor: shouldAutoFetchHistoryPage({{...base, historyCursor: null}}),
            fullWidthHidesFetch: shouldAutoFetchHistoryPage({{...base, fullWidth: true}}),
          }}));
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      assert json.loads(completed.stdout) == {
          "fitsViewport": True,
          "overflowsViewport": False,
          "stillLoading": False,
          "liveMode": False,
          "noMorePages": False,
          "noCursor": False,
          "fullWidthHidesFetch": False,
      }
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k viewport_fill
  ```
  Expected: FAIL — `fullWidthHidesFetch` comes back `True` (current function ignores `fullWidth`
  entirely and only checks `scrollHeight <= clientHeight`, which is `true` here).

- [ ] **Step 3: Write minimal implementation**
  Replace `shouldAutoFetchHistoryPage` (`app.js:326-331`):
  ```js
  function shouldAutoFetchHistoryPage({ seatSource, historyHasMore, historyLoading, historyCursor, scrollHeight, clientHeight, fullWidth }) {
    if (seatSource !== "history" || !historyHasMore || historyLoading || !historyCursor || fullWidth) {
      return false;
    }
    return scrollHeight <= clientHeight;
  }
  ```

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 24 tests still green (25th assertion added to the same test function, not a new
  test — total test count unchanged at 24).

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): add fullWidth guard to shouldAutoFetchHistoryPage"
  ```

---

## Task 8 — `timelinePrefix`: conditional Timestamps/Labels construction

**Files:**
- Modify: `src/arb_memory/static/app.js` (`timelinePrefix`, currently `app.js:148-155`)
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Changes: `timelinePrefix(data, options)` — private, not exported (unchanged export status).
  `options.showTimestamps`/`options.showLabels` are new keys read off the same `options` object
  `formatTimelineEvent`/`formatTimelineFrame` already thread through unchanged.

- [ ] **Step 1: Write the failing test update** (spec §5.2 item 7, copied verbatim)
  In `test_appjs_formats_transcript_timeline_kinds`, the `samples.map(formatTimelineFrame)` call is
  unchanged (still no `options` argument). Replace only the final assertion:
  ```python
  assert json.loads(completed.stdout) == [
      "hello ‹redacted›",
      "<details><summary>thinking</summary>\nchecking plan\n</details>",
      "edited `foo.py` +3/-1\n<details><summary>diff</summary>\npatch\n</details>",
      "bash\n$ pytest\npassed",
      "Read",
  ]
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k formats_transcript_timeline_kinds
  ```
  Expected: FAIL — current output still has the `ts source kind` prefix on every line (e.g.
  `"2026-06-25T10:00:00+00:00 transcript model_text hello ‹redacted›"`), since `timelinePrefix`
  today always includes it unconditionally.

- [ ] **Step 3: Write minimal implementation**
  Replace `timelinePrefix` (`app.js:148-155`):
  ```js
  function timelinePrefix(data, options) {
    const escapeContent = options && options.escapeContent;
    const showTimestamps = options && options.showTimestamps;
    const showLabels = options && options.showLabels;
    const esc = (value) => (escapeContent ? escapeHtml(value) : value);
    return [
      showTimestamps ? esc(data.ts || data.sent_at || "") : "",
      showLabels ? esc(data.source || "event") : "",
      showLabels ? esc(data.kind || data.event_type || "") : "",
    ].filter(Boolean).join(" ");
  }
  ```
  Since the test calls `formatTimelineFrame` with no `options` argument, `options` is `undefined`
  for every sample, so `showTimestamps`/`showLabels` are both falsy and the prefix is always `""`
  — matching the new expected output above.

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  **Expected: 23 passed, 1 known-red — NOT all 24 green (plan-panel fix, cold-Opus's catch).**
  `test_appjs_appends_transcript_details_as_html` also calls `formatTimelineFrame` (via
  `appendTimelineFrame`) and its OWN expected values (unchanged until Task 9's Step 1) still
  assume the old always-on `ts source kind` prefix this step just made conditional — so it goes
  red here, the same way the Tasks 1-5 export-surface test is knowingly red until all 5 new
  functions land. This is expected and will be fixed by Task 9's Step 1, not a sign this step's
  implementation is wrong.

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): make timelinePrefix's timestamp/label conditional on options"
  ```

---

## Task 9 — `appendTimelineFrame`: new `options` param, escape-by-default, `collapseOutput` wedge

**Files:**
- Modify: `src/arb_memory/static/app.js` (`appendTimelineFrame`, currently `app.js:209-216`)
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Changes: `appendTimelineFrame(timelineEl, frame)` → `appendTimelineFrame(timelineEl, frame,
  options)`. This is an **arity change** on an existing exported function — every production call
  site must be updated (Task 13 updates the one real call site inside `selectSeat`).
- Consumes: `collapseOutput(data, rendered, expanded)` (Task 5).

- [ ] **Step 1: Write the failing test update** (spec §5.2 item 8, copied verbatim)
  In `test_appjs_appends_transcript_details_as_html`, both `appendTimelineFrame(timeline, {...})`
  calls gain an explicit 3rd argument:
  ```python
  appendTimelineFrame(timeline, {
    event: "transcript",
    data: {
      source: "transcript",
      ts: "2026-06-25T10:00:01+00:00<img>",
      kind: "model_thinking",
      content: "checking <plan> ‹redacted›"
    }
  }, { escapeContent: true });
  appendTimelineFrame(timeline, {
    event: "transcript",
    data: {
      source: "transcript",
      ts: "2026-06-25T10:00:02+00:00",
      kind: "command_finished<script>",
      tool_name: "apply_patch",
      content: "patch <body>",
      meta: {file: "<script>alert(1)</script>", added: "3<", removed: "1>"}
    }
  }, { escapeContent: true });
  ```
  Replace the final assertion:
  ```python
  assert json.loads(completed.stdout) == {
      "textContent": "",
      "calls": [
          [
              "beforeend",
              "<details><summary>thinking</summary>\nchecking &lt;plan&gt; ‹redacted›\n</details><br><br>",
          ],
          [
              "beforeend",
              "edited `&lt;script&gt;alert(1)&lt;/script&gt;` +3/-1\n<details><summary>diff</summary>\npatch &lt;body&gt;\n</details><br><br>",
          ],
      ],
  }
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k appends_transcript_details
  ```
  **Expected: FAIL, cleanly** (plan-panel fix, cold-Opus's catch — this step's description was
  previously hedgy/uncertain about why; it's now unambiguous). This test has been red since Task
  8 landed (Task 8's own Step 4 now correctly documents this as an expected, known-red state, not
  a surprise) — its expected values still assumed the old always-on prefix `timelinePrefix` used
  to produce. This step's Step 1 edit brings the test's EXPECTED values in line with Task 8's
  already-shipped prefix change; running it now (before Step 3's actual `appendTimelineFrame`
  rewrite) should still fail, but for a DIFFERENT reason than before Step 1 — the prefix-dropping
  is now correctly expected, but the 3rd-argument arity change and `collapseOutput` wedge this
  step's Step 3 adds haven't landed yet. Do not skip Step 3 on the assumption Step 1 alone made it
  pass — the arity change and truncation wedge are still required by the Global Constraints and by
  Task 16's reliance on `collapseOutput` running inside this function.

- [ ] **Step 3: Write minimal implementation**
  Replace `appendTimelineFrame` (`app.js:209-216`):
  ```js
  function appendTimelineFrame(timelineEl, frame, options) {
    // Escape-by-default (spec-panel hardening): if a future call site omits `options` or forgets
    // `escapeContent`, this must still escape — an opt-OUT model (explicit `escapeContent: false`
    // required to skip escaping) is the safe default for a function that inserts content via
    // `insertAdjacentHTML`.
    const effectiveOptions = Object.assign(
      { escapeContent: true },
      options,
      { escapeContent: options && options.escapeContent === false ? false : true }
    );
    if (frame && frame.data && frame.data.source === "transcript" && timelineEl.insertAdjacentHTML) {
      const rendered = formatTimelineFrame(frame, effectiveOptions);
      const collapsed = collapseOutput(frame.data, rendered, Boolean(effectiveOptions.expanded));
      timelineEl.insertAdjacentHTML("beforeend", collapsed + "<br><br>");
    } else {
      timelineEl.textContent += formatTimelineFrame(frame, effectiveOptions) + "\n\n";
    }
    timelineEl.scrollTop = timelineEl.scrollHeight;
  }
  ```

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 24 tests green. This completes all four spec-mandated existing-test updates
  (§5.2 items 7, 8, 9, 10 — Tasks 8, 9, 6, 7 respectively).

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): appendTimelineFrame threads options + wedges collapseOutput"
  ```

---

## Task 10 — `init()` scaffolding: persistence helpers, DOM refs, closure state, initial DOM sync

**Files:**
- Modify: `src/arb_memory/static/app.js` (`init()`, currently `app.js:333-689`)
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Produces (private, module scope, not exported): `readPersisted(key, fallback)`,
  `writePersisted(key, value)`.
- Produces (closure-scope, inside `init()`): DOM refs `statusFilterEl`, `agentFilterEl`,
  `filterCountEl`, `toggleTimestampsButton`, `toggleLabelsButton`, `toggleExpandButton`,
  `toggleFullWidthButton`; state vars `statusFilter`, `agentFilter`, `showTimestamps`,
  `showLabels`, `expanded`, `fullWidth`, `transcriptBuffer`. All consumed by every task from here
  on (11–16).
- This task adds a **new DOM-harness test constant** (`_DOM_HARNESS_JS`) copied from the existing
  test file verbatim — it already exists there (added by the prior stage's Task 9); this plan does
  not redefine it, it reuses it as-is.

This task references DOM element ids (`#status-filter`, `#toggle-timestamps`, etc.) that do not
exist in the real `index.html` yet (Tasks 17–18 add them). This is intentional and safe: the
`_DOM_HARNESS_JS` fake DOM's `getElementById` auto-creates any element on first request, so
`document.getElementById("status-filter")` returns a working `FakeElement` regardless. Do not add
the real HTML markup early to "fix" this — the Ordering section above explains why HTML comes last.

- [ ] **Step 1: Write the failing test**
  ```python
  def test_appjs_initial_toggle_and_filter_state_reflects_persistence():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the initial-state persistence contract test")
      script = _DOM_HARNESS_JS + textwrap.dedent(
          f"""
          const dom = makeDom();
          global.fetch = () => Promise.resolve({{
            ok: true, status: 200, json: () => Promise.resolve({{ orchestrators: [] }}),
          }});
          require({json.dumps(str(APP_JS))});
          dom.ready()();

          (async () => {{
            await flush(5);
            console.log(JSON.stringify({{
              statusFilterValue: dom.get("status-filter").value,
              timestampsPressed: dom.get("toggle-timestamps").getAttribute("aria-pressed"),
              labelsPressed: dom.get("toggle-labels").getAttribute("aria-pressed"),
              expandPressed: dom.get("toggle-expand").getAttribute("aria-pressed"),
              fullWidthPressed: dom.get("toggle-fullwidth").getAttribute("aria-pressed"),
              seatPanelHidden: dom.get("seat-panel").hidden,
            }}));
          }})();
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert result["statusFilterValue"] == "all"
      assert result["timestampsPressed"] == "false"
      assert result["labelsPressed"] == "false"
      assert result["expandPressed"] == "false"
      assert result["fullWidthPressed"] == "false"
      assert result["seatPanelHidden"] is False
  ```
  This is a plan-added regression test (not in the spec's own enumerated list) covering the initial
  DOM-sync block from spec §3.8 — included because it is real, load-bearing behavior with no other
  dedicated test.

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k initial_toggle_and_filter_state
  ```
  Expected: FAIL — `dom.get("status-filter").value` is `""` (freshly auto-created `FakeElement`,
  nothing sets it yet); `getAttribute("aria-pressed")` on the toggle buttons returns `undefined`
  (never set).

- [ ] **Step 3: Write minimal implementation**
  Add persistence helpers directly after `collapseOutput`'s closing brace (module scope, still
  before `function init() {`):
  ```js
  function readPersisted(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : raw;
    } catch (_err) {
      return fallback;
    }
  }

  function writePersisted(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (_err) {
      // best-effort — privacy mode / disabled storage; page stays fully functional without it
    }
  }
  ```
  Inside `init()`, add the 7 new DOM refs directly after the existing `const historyStatusEl = ...`
  line (`app.js:343`):
  ```js
  const statusFilterEl = document.getElementById("status-filter");
  const agentFilterEl = document.getElementById("agent-filter");
  const filterCountEl = document.getElementById("filter-count");
  const toggleTimestampsButton = document.getElementById("toggle-timestamps");
  const toggleLabelsButton = document.getElementById("toggle-labels");
  const toggleExpandButton = document.getElementById("toggle-expand");
  const toggleFullWidthButton = document.getElementById("toggle-fullwidth");
  ```
  Add the 7 new closure state variables directly after the existing `let historyStatusText = "";`
  line (`app.js:354`):
  ```js
  let statusFilter = readPersisted("visStatusFilter", "all");
  let agentFilter = readPersisted("visAgentFilter", "all");
  let showTimestamps = readPersisted("visShowTimestamps", "false") === "true";
  let showLabels = readPersisted("visShowLabels", "false") === "true";
  let expanded = readPersisted("visExpanded", "false") === "true";
  let fullWidth = readPersisted("visFullWidth", "false") === "true";
  let transcriptBuffer = [];
  ```
  Add the initial DOM sync directly before `if (tokenInput.value) { loadOrchestrators(); }`
  (`app.js:686-688`, the last lines of `init()`):
  ```js
  statusFilterEl.value = statusFilter;
  toggleTimestampsButton.setAttribute("aria-pressed", String(showTimestamps));
  toggleLabelsButton.setAttribute("aria-pressed", String(showLabels));
  toggleExpandButton.setAttribute("aria-pressed", String(expanded));
  toggleFullWidthButton.setAttribute("aria-pressed", String(fullWidth));
  seatPanelEl.hidden = fullWidth;
  ```

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 25 tests green (the 24 from before + this one) — confirms this scaffolding change
  doesn't break any of the 3 prior DOM-harness tests either.

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): add filter/toggle persistence, DOM refs, and initial sync"
  ```

---

## Task 11 — `renderSeats()`: filtering, count badge, agent-dropdown reconciliation

**Files:**
- Modify: `src/arb_memory/static/app.js` (`renderSeats`, currently `app.js:407-443`)
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Consumes: `visibleSeats(seatMap, statusFilter, agentFilter)` (Task 2), `deriveAgentOptions(seats)`
  (Task 3), `statusFilter`/`agentFilter`/`filterCountEl`/`agentFilterEl` (Task 10).
- Produces (closure-scope): `updateAgentFilterOptions()`, `lastAgentOptionsKey` (module-level `let`
  inside `init()`'s scope, declared right above the function).

- [ ] **Step 1: Write the failing test**
  ```python
  def test_appjs_render_seats_filters_and_updates_count_and_agent_options():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the renderSeats filtering contract test")
      script = _DOM_HARNESS_JS + textwrap.dedent(
          f"""
          const dom = makeDom();
          let orchestratorSse = null;
          global.fetch = (url, opts) => {{
            if (url === "/orchestrators") {{
              return Promise.resolve({{
                ok: true, status: 200,
                json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
              }});
            }}
            if (url.startsWith("/sse/orchestrator/")) {{
              orchestratorSse = makeSseResponse(opts.signal);
              return Promise.resolve(orchestratorSse);
            }}
            return Promise.reject(new Error("unexpected fetch " + url));
          }};

          require({json.dumps(str(APP_JS))});
          dom.ready()();

          (async () => {{
            await flush(10);
            orchestratorSse.push(
              "id: 1-0\\nevent: seat_appear\\ndata: " +
              JSON.stringify({{
                task_id: "seat-a", seat_id: "codex-1", run_id: "run-1",
                state: "running", last_event: "task_started",
                last_event_ts: new Date().toISOString(),
              }}) + "\\n\\n"
            );
            orchestratorSse.push(
              "id: 2-0\\nevent: seat_appear\\ndata: " +
              JSON.stringify({{
                task_id: "seat-b", seat_id: "agy-1", run_id: "run-2",
                state: "done", last_event: "task_finished",
                last_event_ts: new Date().toISOString(),
              }}) + "\\n\\n"
            );
            await flush(10);
            const countAll = dom.get("filter-count").textContent;
            const agentOptionsAll = dom.get("agent-filter").children.map((o) => o.value);

            console.log(JSON.stringify({{ countAll, agentOptionsAll }}));
          }})();
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert result["countAll"] == "2/2"
      assert result["agentOptionsAll"] == ["all", "agy", "codex"]
  ```
  Plan-added (spec's own test list has no dedicated integration test for `renderSeats`'s filtering
  wiring — it relies on the Task 2/3 pure-function tests for correctness of the underlying logic).
  **Scoped to the DEFAULT (`"all"`/`"all"`) filter state only** — plan-panel fix (cold-Opus's
  catch): this task builds `renderSeats()`/`updateAgentFilterOptions()` but the `#status-filter`/
  `#agent-filter` `change` event listeners that actually SET `statusFilter`/`agentFilter` from a
  simulated user interaction aren't added until Task 12. A test here dispatching a `change` event
  and expecting the result to narrow would silently fail (no listener registered yet) — that
  exact scenario is covered instead by Task 12's already-existing combined test, extended with the
  count/seats-shown assertions (see Task 12 below), which is the task that actually wires the
  listener.

- [ ] **Step 1b: Write the failing test for the empty-seats persistence guard**

  **Plan-panel fix (cursor spike's second, independent pass — a real catch): the
  `options.length > 1` guard's own rationale (above) said to "confirm this with a dedicated
  assertion," but no such assertion previously existed in this task's actual test code — only in
  prose. This step is that missing test, written out concretely.**
  ```python
  def test_appjs_update_agent_filter_options_preserves_persisted_filter_before_seats_load():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the empty-seats agent-filter guard test")
      script = _DOM_HARNESS_JS + textwrap.dedent(
          f"""
          const dom = makeDom();
          global.localStorage._store.visAgentFilter = "codex";
          global.fetch = () => Promise.resolve({{
            ok: true, status: 200, json: () => Promise.resolve({{ orchestrators: [] }}),
          }});
          require({json.dumps(str(APP_JS))});
          dom.ready()();

          (async () => {{
            await flush(10);
            console.log(JSON.stringify({{
              agentOptionsWithNoSeats: dom.get("agent-filter").children.map((o) => o.value),
              persistedAgentFilter: global.localStorage._store.visAgentFilter,
            }}));
          }})();
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert result["agentOptionsWithNoSeats"] == ["all"]
      assert result["persistedAgentFilter"] == "codex"
  ```
  This exercises the exact scenario the guard exists for: `seats` is empty (no orchestrator ever
  selected, matching page load before any SSE frame), `deriveAgentOptions({})` returns `["all"]`
  only, and a pre-set `localStorage` value of `"codex"` (simulating a prior session's preference)
  must survive — `options.length > 1` is `false` (only `"all"` exists), so the reset branch never
  fires and the persisted value stays untouched in storage.

- [ ] **Step 2: Run both new tests to verify they fail**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k "render_seats_filters or preserves_persisted_filter_before_seats_load"
  ```
  Expected: FAIL — `filter-count`'s `textContent` stays `""` (current `renderSeats` never sets
  it); `agent-filter`'s children stay empty (never populated); the second test fails because
  `renderSeats`/`updateAgentFilterOptions` don't exist yet at all, so `dom.get("agent-filter")`
  never gets populated and `localStorage` is never touched either way (a vacuous, not
  meaningfully-red, failure at this point — Step 4 is where it becomes a real proof).

- [ ] **Step 3: Write minimal implementation**
  Replace `renderSeats` (`app.js:407-443`) — only the input array and the three lines after
  `seatsEl.replaceChildren` change; seat-row markup is byte-identical to today:
  ```js
  function renderSeats() {
    const total = Object.keys(seats).length;
    const filtered = visibleSeats(seats, statusFilter, agentFilter);
    const orderedSeats = filtered.slice().sort((a, b) => {
      return String(b.last_event_ts || "").localeCompare(String(a.last_event_ts || ""));
    });
    seatsEl.replaceChildren(
      ...orderedSeats.map((seat) => {
        const li = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.taskId = seat.task_id;
        button.setAttribute("aria-current", seat.task_id === selectedTaskId ? "true" : "false");
        button.innerHTML = [
          '<span class="seat-id"></span>',
          '<span class="state-cell"><span class="pulse-dot" aria-hidden="true"></span><span class="badge"></span></span>',
          '<span class="age"></span>',
          '<span class="run-id"></span>',
          '<span class="last-event"></span>',
        ].join("");
        button.querySelector(".seat-id").textContent = seat.seat_id || seat.task_id || "unknown";
        const stateCell = button.querySelector(".state-cell");
        const badge = stateCell.querySelector(".badge");
        badge.textContent = seat.state || "unknown";
        badge.dataset.state = seat.state || "unknown";
        stateCell.classList.toggle("is-running", seat.state === "running");
        if (seat.voted && seat.stance) {
          badge.textContent += " (" + seat.stance + ")";
        }
        button.querySelector(".age").textContent = seatAgeLabel(seat.last_event_ts, seatSource, Date.now());
        button.querySelector(".run-id").textContent = seat.run_id || "";
        button.querySelector(".last-event").textContent = seat.last_event || "";
        button.addEventListener("click", () => selectSeat(seat.task_id));
        li.appendChild(button);
        return li;
      })
    );
    historyStatusEl.textContent = historyStatusLine();
    filterCountEl.textContent = filtered.length + "/" + total;
    updateAgentFilterOptions();
  }

  let lastAgentOptionsKey = null;

  function updateAgentFilterOptions() {
    const options = deriveAgentOptions(seats);
    const optionsKey = options.join(" ");
    if (optionsKey !== lastAgentOptionsKey) {
      lastAgentOptionsKey = optionsKey;
      agentFilterEl.replaceChildren(
        ...options.map((value) => {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = value;
          return option;
        })
      );
    }
    if (options.includes(agentFilter)) {
      agentFilterEl.value = agentFilter;
    } else if (options.length > 1) {
      // Only reset+persist when there's real seat data to validate against — see note below.
      agentFilter = "all";
      agentFilterEl.value = "all";
      writePersisted("visAgentFilter", "all");
    }
    // else: options === ["all"] only (no seats loaded yet) — leave agentFilter untouched.
  }
  ```
  The `lastAgentOptionsKey` guard exists because `renderSeats()` runs on every SSE frame, history
  page, mode toggle, and the existing 2s age-tick timer — without it, `agentFilterEl`'s children
  would be torn down and rebuilt every 2 seconds even when unchanged, which on some browsers closes
  an operator's currently-open dropdown mid-click.

  **`options.length > 1` guard (plan-panel fix, cold-Opus's second catch — distinct from the
  `lastAgentOptionsKey` guard above):** `openOrchestrator` (Task 15) calls `renderSeats()`
  immediately after clearing `seats` to empty, before any real SSE frame arrives —
  `deriveAgentOptions({})` returns exactly `["all"]` at that moment. Without this guard, a
  persisted `agentFilter` like `"codex"` (from a prior session) would be found "not in options"
  (the only option is `"all"`) and get **immediately overwritten to `"all"` in `localStorage`**,
  on every single page load, moments before the real SSE stream proves the preference was valid —
  silently destroying it every time, defeating the entire point of persisting it. Verified against
  the current `app.js:610` call site directly. Step 1b below is the dedicated test proving this.

- [ ] **Step 4: Run both tests to verify they pass**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 27 tests green (26 + this task's second, Step-1b test).

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): renderSeats filters, updates count badge, reconciles agent options"
  ```

---

## Task 12 — `rerenderTranscript()` + filter-select and toggle-button click handlers

**Files:**
- Modify: `src/arb_memory/static/app.js` (new code inserted directly after `clearToken`'s
  `addEventListener` block, currently ending `app.js:405`, and before `function renderSeats() {`)
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
- Consumes: `formatTimelineFrame`, `collapseOutput` (module scope); `transcriptBuffer`,
  `showTimestamps`, `showLabels`, `expanded`, `fullWidth`, `statusFilter`, `agentFilter` (Task 10).
- Produces (closure-scope): `rerenderTranscript()`. Called by 3 of the 4 toggle handlers here, and
  again in Task 13/14's tests.

- [ ] **Step 1: Write the failing test**
  ```python
  def test_appjs_filter_and_toggle_click_handlers_update_state_and_rerender():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the filter/toggle click-handler contract test")
      script = _DOM_HARNESS_JS + textwrap.dedent(
          f"""
          const dom = makeDom();
          let orchestratorSse = null;
          global.fetch = (url, opts) => {{
            if (url === "/orchestrators") {{
              return Promise.resolve({{
                ok: true, status: 200,
                json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
              }});
            }}
            if (url.startsWith("/sse/orchestrator/")) {{
              orchestratorSse = makeSseResponse(opts.signal);
              return Promise.resolve(orchestratorSse);
            }}
            return Promise.reject(new Error("unexpected fetch " + url));
          }};
          require({json.dumps(str(APP_JS))});
          dom.ready()();

          (async () => {{
            await flush(10);
            orchestratorSse.push(
              "id: 1-0\\nevent: seat_appear\\ndata: " +
              JSON.stringify({{
                task_id: "seat-a", seat_id: "codex-1", run_id: "run-1",
                state: "running", last_event: "task_started",
                last_event_ts: new Date().toISOString(),
              }}) + "\\n\\n"
            );
            orchestratorSse.push(
              "id: 2-0\\nevent: seat_appear\\ndata: " +
              JSON.stringify({{
                task_id: "seat-b", seat_id: "agy-1", run_id: "run-2",
                state: "done", last_event: "task_finished",
                last_event_ts: new Date().toISOString(),
              }}) + "\\n\\n"
            );
            await flush(10);

            // Plan-panel addition (cold-Opus's catch): this is where the filter-narrowing behavior
            // originally (mis)placed in Task 11 actually belongs — Task 11 builds renderSeats()'s
            // filtering logic, but only THIS task's click/change handlers make a dispatched
            // "change" event on #status-filter actually update `statusFilter` and re-render.
            dom.get("status-filter").value = "running";
            dom.get("status-filter").dispatchEvent("change");
            const countRunning = dom.get("filter-count").textContent;
            const seatsShown = seatTaskIds(dom);

            dom.get("toggle-timestamps").dispatchEvent("click");
            dom.get("toggle-labels").dispatchEvent("click");
            dom.get("toggle-expand").dispatchEvent("click");
            dom.get("toggle-fullwidth").dispatchEvent("click");

            console.log(JSON.stringify({{
              countRunning, seatsShown,
              persistedStatus: global.localStorage._store.visStatusFilter,
              timestampsPressed: dom.get("toggle-timestamps").getAttribute("aria-pressed"),
              labelsPressed: dom.get("toggle-labels").getAttribute("aria-pressed"),
              expandPressed: dom.get("toggle-expand").getAttribute("aria-pressed"),
              fullWidthPressed: dom.get("toggle-fullwidth").getAttribute("aria-pressed"),
              seatPanelHidden: dom.get("seat-panel").hidden,
            }}));
          }})();
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert result["countRunning"] == "1/2"
      assert result["seatsShown"] == ["seat-a"]
      assert result["persistedStatus"] == "running"
      assert result["timestampsPressed"] == "true"
      assert result["labelsPressed"] == "true"
      assert result["expandPressed"] == "true"
      assert result["fullWidthPressed"] == "true"
      assert result["seatPanelHidden"] is True
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k filter_and_toggle_click_handlers
  ```
  Expected: FAIL — `dispatchEvent("click"/"change")` on these auto-created `FakeElement`s calls no
  listeners at all (none registered yet), so every field in the result stays at its harness default
  (`undefined`/`""`/`false`).

- [ ] **Step 3: Write minimal implementation**
  Insert directly after `clearToken`'s `addEventListener` block (`app.js:405`) and before
  `function renderSeats() {`:
  ```js
  function rerenderTranscript() {
    timelineEl.replaceChildren();
    for (const frame of transcriptBuffer) {
      const rendered = formatTimelineFrame(frame, { escapeContent: true, showTimestamps, showLabels, expanded });
      const collapsed = collapseOutput(frame.data, rendered, expanded);
      timelineEl.insertAdjacentHTML("beforeend", collapsed + "<br><br>");
    }
    timelineEl.scrollTop = timelineEl.scrollHeight;
  }

  statusFilterEl.addEventListener("change", () => {
    statusFilter = statusFilterEl.value;
    writePersisted("visStatusFilter", statusFilter);
    renderSeats();
  });

  agentFilterEl.addEventListener("change", () => {
    agentFilter = agentFilterEl.value;
    writePersisted("visAgentFilter", agentFilter);
    renderSeats();
  });

  toggleTimestampsButton.addEventListener("click", () => {
    showTimestamps = !showTimestamps;
    toggleTimestampsButton.setAttribute("aria-pressed", String(showTimestamps));
    writePersisted("visShowTimestamps", String(showTimestamps));
    rerenderTranscript();
  });

  toggleLabelsButton.addEventListener("click", () => {
    showLabels = !showLabels;
    toggleLabelsButton.setAttribute("aria-pressed", String(showLabels));
    writePersisted("visShowLabels", String(showLabels));
    rerenderTranscript();
  });

  toggleExpandButton.addEventListener("click", () => {
    expanded = !expanded;
    toggleExpandButton.setAttribute("aria-pressed", String(expanded));
    writePersisted("visExpanded", String(expanded));
    rerenderTranscript();
  });

  toggleFullWidthButton.addEventListener("click", () => {
    fullWidth = !fullWidth;
    toggleFullWidthButton.setAttribute("aria-pressed", String(fullWidth));
    writePersisted("visFullWidth", String(fullWidth));
    seatPanelEl.hidden = fullWidth;
  });
  ```
  Note: `rerenderTranscript()` calling `timelineEl.insertAdjacentHTML(...)` on an empty
  `transcriptBuffer` (as in this test) never actually reaches that line — the `for` loop body
  never executes — so this task does not yet require the fake DOM's `insertAdjacentHTML` support;
  that lands in Task 13, which is the first task to exercise a non-empty buffer.

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 28 tests green (bumped by 1 — Task 11 gained a second test, plan-panel fix).

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): wire filter/toggle click handlers and rerenderTranscript"
  ```

---

## Task 13 — Transcript-buffer push site + live-append options-threading (spec test 11)

**Files:**
- Modify: `src/arb_memory/static/app.js` (`selectSeat`, currently `app.js:564-586`)
- Modify: `tests/arb_memory/test_visibility_web_contract.py` (extend the shared `_DOM_HARNESS_JS`
  constant with `insertAdjacentHTML` support, then add the new test)

**Interfaces:**
- Changes: `selectSeat`'s SSE callback now pushes `source === "transcript"` frames onto
  `transcriptBuffer` and calls `appendTimelineFrame` with the current toggle-state `options` object
  instead of no 3rd argument.

- [ ] **Step 1: Extend the shared DOM harness** (required infrastructure — `rerenderTranscript()`
  unconditionally calls `timelineEl.insertAdjacentHTML(...)`, which `FakeElement` does not yet
  implement). In `_DOM_HARNESS_JS`'s `FakeElement` class, add the method and make `replaceChildren()`
  (no-args form) also reset `textContent`:
  ```js
  replaceChildren(...nodes) {
    this.children = nodes;
    if (nodes.length && typeof nodes[0].value === "string") {
      this.value = nodes[0].value;
    } else if (!nodes.length) {
      this.value = "";
      this.textContent = "";
    }
  }
  set innerHTML(html) {
    this.children = [];
    const stack = [this];
    const tokenRe = /<\/?span\s+class="([^"]+)"[^>]*>|<\/span>/g;
    let match;
    while ((match = tokenRe.exec(html))) {
      const full = match[0];
      const cls = match[1];
      if (full.startsWith("</")) {
        stack.pop();
      } else {
        const child = new FakeElement("span");
        child.classList.add(cls);
        stack[stack.length - 1].children.push(child);
        stack.push(child);
      }
    }
  }
  insertAdjacentHTML(where, html) {
    // Minimal model: this fake DOM has no real HTML parser, so treat `textContent` as an opaque
    // raw-HTML/text accumulation buffer — tests only assert on substring presence/absence, never
    // on real DOM structure for the timeline pane.
    this.textContent += html;
  }
  ```
  (`replaceChildren`/`innerHTML` are shown for context/location — only add the new
  `insertAdjacentHTML` method and the two added lines inside `replaceChildren`'s
  `else if (!nodes.length)` branch; do not otherwise change `innerHTML`.) Verify this change alone
  doesn't break anything yet:
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 27 tests still green (neither existing test calls `insertAdjacentHTML` or relies on
  `replaceChildren()`'s no-args form clearing `textContent`).

- [ ] **Step 2: Write the failing test** (spec §5.3 item 11)
  ```python
  def test_appjs_transcript_buffer_rerenders_on_toggle():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the transcript buffer/rerender contract test")
      script = _DOM_HARNESS_JS + textwrap.dedent(
          f"""
          const dom = makeDom();
          let orchestratorSse = null;
          let seatSse = null;
          global.fetch = (url, opts) => {{
            if (url === "/orchestrators") {{
              return Promise.resolve({{
                ok: true, status: 200,
                json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
              }});
            }}
            if (url.startsWith("/sse/orchestrator/")) {{
              orchestratorSse = makeSseResponse(opts.signal);
              return Promise.resolve(orchestratorSse);
            }}
            if (url.startsWith("/sse/seat/")) {{
              seatSse = makeSseResponse(opts.signal);
              return Promise.resolve(seatSse);
            }}
            return Promise.reject(new Error("unexpected fetch " + url));
          }};

          require({json.dumps(str(APP_JS))});
          dom.ready()();

          function pushTranscript(id, ts, text) {{
            seatSse.push(
              "id: " + id + "\\nevent: transcript\\ndata: " +
              JSON.stringify({{ source: "transcript", ts: ts, kind: "model_text", content: text }}) +
              "\\n\\n"
            );
          }}

          (async () => {{
            await flush(10);
            orchestratorSse.push(
              "id: 1-0\\nevent: seat_appear\\ndata: " +
              JSON.stringify({{
                task_id: "seat-a", seat_id: "codex-1", run_id: "run-1",
                state: "running", last_event: "task_started",
                last_event_ts: new Date().toISOString(),
              }}) + "\\n\\n"
            );
            await flush(10);
            dom.get("seats").children[0].children[0].dispatchEvent("click");
            await flush(10);

            pushTranscript("1-0", "2026-06-25T10:00:00+00:00", "first line");
            pushTranscript("2-0", "2026-06-25T10:00:01+00:00", "second line");
            await flush(10);
            const beforeToggle = dom.get("timeline").textContent;

            dom.get("toggle-timestamps").dispatchEvent("click");
            const afterToggle = dom.get("timeline").textContent;

            pushTranscript("3-0", "2026-06-25T10:00:02+00:00", "third line");
            await flush(10);
            const afterNewFrame = dom.get("timeline").textContent;

            console.log(JSON.stringify({{ beforeToggle, afterToggle, afterNewFrame }}));
          }})();
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert "2026-06-25T10:00:00" not in result["beforeToggle"]
      assert "2026-06-25T10:00:01" not in result["beforeToggle"]
      assert "first line" in result["beforeToggle"]
      assert "second line" in result["beforeToggle"]
      assert "2026-06-25T10:00:00" in result["afterToggle"]
      assert "2026-06-25T10:00:01" in result["afterToggle"]
      assert "2026-06-25T10:00:02" in result["afterNewFrame"]
  ```
  The middle two assertions (`afterToggle` contains both earlier timestamps) are the core proof —
  `rerenderTranscript()` retroactively re-renders already-displayed content, not just future
  appends. The last assertion proves the live-append path also threads current toggle state
  (§3.7's options-threading fix), not stale pre-toggle options.

- [ ] **Step 3: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k transcript_buffer_rerenders
  ```
  Expected: FAIL — `afterToggle` doesn't contain the timestamps, because `transcriptBuffer` is
  never populated (current `selectSeat` never pushes to it) so `rerenderTranscript()`'s loop body
  never runs.

- [ ] **Step 4: Write minimal implementation**
  Replace `selectSeat` (`app.js:564-586`):
  ```js
  function selectSeat(taskId) {
    if (!taskId || taskId === selectedTaskId) {
      return;
    }
    selectedTaskId = taskId;
    timelineEl.textContent = "";
    if (stopSeatStream) {
      stopSeatStream();
    }
    stopSeatStream = streamSSE("/sse/seat/" + encodeURIComponent(taskId), (frame) => {
      if (frame.event === "error") {
        const status = frame.data && frame.data.status;
        if (status === 401 || status === 403) {
          showAuthBanner();
        } else {
          timelineEl.textContent += "[error] " + frame.data.message + "\n";
        }
        return;
      }
      if (frame.data && frame.data.source === "transcript") {
        transcriptBuffer.push(frame);
      }
      appendTimelineFrame(timelineEl, frame, { escapeContent: true, showTimestamps, showLabels, expanded });
    });
    renderSeats();
  }
  ```
  (This does **not** yet add `transcriptBuffer.length = 0;` next to `timelineEl.textContent = "";`
  above — that is one of the seven clearing sites, added as a batch in Task 14.)

- [ ] **Step 5: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 29 tests green (bumped by 1 — Task 11 gained a second test, plan-panel fix).

- [ ] **Step 6: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): push transcript frames to a buffer and thread live toggle state"
  ```

---

## Task 14 — The seven transcript-buffer-clearing sites (grep-verified, spec test 12)

**Files:**
- Modify: `src/arb_memory/static/app.js` (`clearToken`, `setSeatSource`, `selectSeat`,
  `openOrchestrator`, `loadOrchestrators`, `startOrchestratorStream`)
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:** none new — every one of the seven sites gets one added line,
`transcriptBuffer.length = 0;`, next to its existing clear-or-replace of `timelineEl`.

- [ ] **Step 1: Grep-verify the current site count before editing** (do not trust Global
  Constraint 4's table blindly — confirm it against the real file as it stands right now, per the
  spec's own remediation: an earlier five-site pass undercounted by grepping only one pattern)
  ```bash
  grep -n 'timelineEl.textContent = ""' src/arb_memory/static/app.js
  grep -n 'timelineEl.textContent = "\[error' src/arb_memory/static/app.js
  ```
  Expected: 5 matches from the first command, 2 from the second — 7 total. If the count differs
  (e.g. an eighth site from unrelated changes since this plan was written), treat every match as a
  clearing site requiring the same one-line addition below, and note the discrepancy when reporting
  this task's completion.

- [ ] **Step 2: Write the failing test** (spec §5.3 item 12)
  ```python
  def test_appjs_transcript_buffer_cleared_on_seat_and_orchestrator_switch():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the transcript-buffer-clearing regression test")
      script = _DOM_HARNESS_JS + textwrap.dedent(
          f"""
          const dom = makeDom();
          let orchestratorSse = null;
          let seatSse = null;
          global.fetch = (url, opts) => {{
            if (url === "/orchestrators") {{
              return Promise.resolve({{
                ok: true, status: 200,
                json: () => Promise.resolve({{ orchestrators: ["orch-1", "orch-2"] }}),
              }});
            }}
            if (url.startsWith("/sse/orchestrator/")) {{
              orchestratorSse = makeSseResponse(opts.signal);
              return Promise.resolve(orchestratorSse);
            }}
            if (url.startsWith("/sse/seat/")) {{
              seatSse = makeSseResponse(opts.signal);
              return Promise.resolve(seatSse);
            }}
            return Promise.reject(new Error("unexpected fetch " + url));
          }};

          function pushSeat(taskId, seatId) {{
            orchestratorSse.push(
              "id: 1-0\\nevent: seat_appear\\ndata: " +
              JSON.stringify({{
                task_id: taskId, seat_id: seatId, run_id: "run-" + taskId,
                state: "running", last_event: "task_started",
                last_event_ts: new Date().toISOString(),
              }}) + "\\n\\n"
            );
          }}

          function pushTranscript(text) {{
            seatSse.push(
              "id: 1-0\\nevent: transcript\\ndata: " +
              JSON.stringify({{ source: "transcript", kind: "model_text", content: text }}) + "\\n\\n"
            );
          }}

          function selectSeatByTaskId(taskId) {{
            const ids = seatTaskIds(dom);
            const idx = ids.indexOf(taskId);
            dom.get("seats").children[idx].children[0].dispatchEvent("click");
          }}

          require({json.dumps(str(APP_JS))});
          dom.ready()();

          (async () => {{
            await flush(10);
            pushSeat("seat-a", "codex-1");
            await flush(10);
            selectSeatByTaskId("seat-a");
            await flush(10);
            pushTranscript("seat-a-content");
            await flush(10);

            pushSeat("seat-b", "codex-2");
            await flush(10);
            selectSeatByTaskId("seat-b");
            await flush(10);
            pushTranscript("seat-b-content");
            await flush(10);

            dom.get("toggle-timestamps").dispatchEvent("click");
            dom.get("toggle-timestamps").dispatchEvent("click");
            const afterSeatSwitch = dom.get("timeline").textContent;

            dom.get("orchestrator").value = "orch-2";
            dom.get("orchestrator").dispatchEvent("change");
            await flush(10);
            pushSeat("seat-c", "codex-3");
            await flush(10);
            selectSeatByTaskId("seat-c");
            await flush(10);
            pushTranscript("seat-c-content");
            await flush(10);

            dom.get("toggle-timestamps").dispatchEvent("click");
            dom.get("toggle-timestamps").dispatchEvent("click");
            const afterOrchestratorSwitch = dom.get("timeline").textContent;

            dom.get("clear-token").dispatchEvent("click");
            dom.get("toggle-timestamps").dispatchEvent("click");
            const afterClear = dom.get("timeline").textContent;

            console.log(JSON.stringify({{ afterSeatSwitch, afterOrchestratorSwitch, afterClear }}));
          }})();
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert "seat-b-content" in result["afterSeatSwitch"]
      assert "seat-a-content" not in result["afterSeatSwitch"]
      assert "seat-c-content" in result["afterOrchestratorSwitch"]
      assert "seat-b-content" not in result["afterOrchestratorSwitch"]
      assert "seat-a-content" not in result["afterOrchestratorSwitch"]
      assert result["afterClear"] == ""
  ```

- [ ] **Step 3: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k transcript_buffer_cleared
  ```
  Expected: FAIL — `afterSeatSwitch` still contains `"seat-a-content"` (the buffer is never
  cleared on seat switch today, so the forced rerender replays it alongside seat-b's content).

- [ ] **Step 4: Write minimal implementation** — add `transcriptBuffer.length = 0;` at each of the
  seven sites, next to the existing clear/replace line:
  1. `clearToken`'s click handler (`app.js:403`):
     ```js
     Object.keys(seats).forEach((key) => delete seats[key]);
     selectedTaskId = "";
     selectedOrchestratorId = "";
     historyGen += 1;
     seatSource = "live";
     historyCursor = null;
     historyHasMore = false;
     historyLoading = false;
     historyStatusText = "";
     modeLiveButton.disabled = true;
     modeHistoryButton.disabled = true;
     updateModeButtons();
     localStorage.removeItem("token");
     localStorage.token = "";
     tokenInput.value = "";
     orchestratorSelect.replaceChildren();
     seatsEl.replaceChildren();
     timelineEl.textContent = "";
     transcriptBuffer.length = 0;
     hideAuthBanner();
     ```
  2. `setSeatSource` (`app.js:469`):
     ```js
     selectedTaskId = "";
     Object.keys(seats).forEach((key) => delete seats[key]);
     timelineEl.textContent = "";
     transcriptBuffer.length = 0;
     ```
  3. `selectSeat` (`app.js:569`, inside the function already modified in Task 13):
     ```js
     selectedTaskId = taskId;
     timelineEl.textContent = "";
     transcriptBuffer.length = 0;
     ```
  4. `openOrchestrator` (`app.js:602`):
     ```js
     Object.keys(seats).forEach((key) => delete seats[key]);
     selectedTaskId = "";
     timelineEl.textContent = "";
     transcriptBuffer.length = 0;
     ```
  5. `startOrchestratorStream`'s non-auth error branch (`app.js:621`):
     ```js
     timelineEl.textContent = "[error] " + frame.data.message + "\n";
     transcriptBuffer.length = 0;
     ```
  6. `loadOrchestrators`'s 401/403 branch (`app.js:644`):
     ```js
     showAuthBanner();
     orchestratorSelect.replaceChildren();
     seatsEl.replaceChildren();
     timelineEl.textContent = "";
     transcriptBuffer.length = 0;
     return;
     ```
  7. `loadOrchestrators`'s `!response.ok` branch (`app.js:651`):
     ```js
     orchestratorSelect.replaceChildren();
     seatsEl.replaceChildren();
     timelineEl.textContent = "[error] /orchestrators " + response.status + "\n";
     transcriptBuffer.length = 0;
     return;
     ```

- [ ] **Step 5: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 30 tests green (bumped by 1 — Task 11 gained a second test, plan-panel fix).

- [ ] **Step 6: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "fix(arb-visibility-web): clear the transcript buffer at all seven timeline-clearing sites"
  ```

---

## Task 15 — `openOrchestrator`: filter-reset-on-switch

**Files:**
- Modify: `src/arb_memory/static/app.js` (`openOrchestrator`, currently `app.js:588-612`)
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:** none new — behavior change on an existing closure function.

- [ ] **Step 1: Write the failing test**
  ```python
  def test_appjs_open_orchestrator_preserves_filters_on_first_call_resets_on_switch():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the openOrchestrator filter-reset contract test")
      script = _DOM_HARNESS_JS + textwrap.dedent(
          f"""
          const dom = makeDom();
          global.localStorage._store.visStatusFilter = "running";
          let orchestratorSse = null;
          global.fetch = (url, opts) => {{
            if (url === "/orchestrators") {{
              return Promise.resolve({{
                ok: true, status: 200,
                json: () => Promise.resolve({{ orchestrators: ["orch-1", "orch-2"] }}),
              }});
            }}
            if (url.startsWith("/sse/orchestrator/")) {{
              orchestratorSse = makeSseResponse(opts.signal);
              return Promise.resolve(orchestratorSse);
            }}
            return Promise.reject(new Error("unexpected fetch " + url));
          }};

          require({json.dumps(str(APP_JS))});
          dom.ready()();

          (async () => {{
            await flush(10);
            const afterFirstOpen = dom.get("status-filter").value;

            dom.get("orchestrator").value = "orch-2";
            dom.get("orchestrator").dispatchEvent("change");
            await flush(10);
            const afterSwitch = dom.get("status-filter").value;
            const persistedAfterSwitch = global.localStorage._store.visStatusFilter;

            console.log(JSON.stringify({{ afterFirstOpen, afterSwitch, persistedAfterSwitch }}));
          }})();
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert result["afterFirstOpen"] == "running"
      assert result["afterSwitch"] == "all"
      assert result["persistedAfterSwitch"] == "all"
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k open_orchestrator_preserves_filters
  ```
  Expected: FAIL — `afterSwitch` stays `"running"` (current `openOrchestrator` never resets
  filters at all).

- [ ] **Step 3: Write minimal implementation**
  Replace `openOrchestrator` (`app.js:588-612`):
  ```js
  function openOrchestrator(orchestratorId) {
    const isOrchestratorSwitch = selectedOrchestratorId !== "" && selectedOrchestratorId !== orchestratorId;
    selectedOrchestratorId = orchestratorId;
    historyGen += 1;
    seatSource = "live";
    historyCursor = null;
    historyHasMore = false;
    historyLoading = false;
    historyStatusText = "";
    updateModeButtons();
    modeLiveButton.disabled = false;
    modeHistoryButton.disabled = false;

    if (isOrchestratorSwitch) {
      statusFilter = "all";
      agentFilter = "all";
      writePersisted("visStatusFilter", "all");
      writePersisted("visAgentFilter", "all");
      statusFilterEl.value = "all";
    }

    Object.keys(seats).forEach((key) => delete seats[key]);
    selectedTaskId = "";
    timelineEl.textContent = "";
    transcriptBuffer.length = 0;
    if (stopOrchestratorStream) {
      stopOrchestratorStream();
    }
    if (stopSeatStream) {
      stopSeatStream();
      stopSeatStream = null;
    }
    renderSeats(); // repopulates/reconciles #agent-filter via updateAgentFilterOptions
    stopOrchestratorStream = startOrchestratorStream(orchestratorId);
  }
  ```
  `isOrchestratorSwitch` is computed **before** `selectedOrchestratorId` is reassigned: true only
  when an orchestrator was already selected and it's a different one. On the very first call
  (`selectedOrchestratorId === ""`), it's `false` — filters stay exactly as `readPersisted` set
  them. `agentFilterEl.value` is not set directly on a switch — `renderSeats()`'s
  `updateAgentFilterOptions()` call at the end reconciles it to `"all"` since that's always in the
  derived option list.

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 31 tests green (bumped by 1 — Task 11 gained a second test, plan-panel fix).

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): reset status/agent filters on a genuine orchestrator switch"
  ```

---

## Task 16 — `shouldAutoFetchHistoryPage` real call-site wiring (spec test 13)

**Files:**
- Modify: `src/arb_memory/static/app.js` (`applyHistoryPage`, currently `app.js:534-562`)
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:** none new — `applyHistoryPage`'s existing `shouldAutoFetchHistoryPage(...)` call
site gains the `fullWidth` key (the function itself already accepts it, from Task 7).

- [ ] **Step 1: Write the failing test** (spec §5.3 item 13)
  ```python
  def test_appjs_full_width_hides_seat_panel_and_blocks_fetch_burst():
      node = _node()
      if node is None:
          raise AssertionError("node is required for the full-width fetch-suppression contract test")
      script = _DOM_HARNESS_JS + textwrap.dedent(
          f"""
          const dom = makeDom();
          dom.get("seat-panel").scrollHeight = 100;
          dom.get("seat-panel").clientHeight = 400;
          let orchestratorSse = null;
          const historyUrls = [];
          global.fetch = (url, opts) => {{
            if (url === "/orchestrators") {{
              return Promise.resolve({{
                ok: true, status: 200,
                json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
              }});
            }}
            if (url.startsWith("/sse/orchestrator/")) {{
              orchestratorSse = makeSseResponse(opts.signal);
              return Promise.resolve(orchestratorSse);
            }}
            if (url.startsWith("/orchestrators/orch-1/seats/history")) {{
              historyUrls.push(url);
              return Promise.resolve({{
                ok: true, status: 200,
                json: () => Promise.resolve({{
                  seats: [
                    {{ task_id: "seat-a", seat_id: "hist-a", run_id: "run-a", state: "done", last_event: "task_finished", last_event_ts: "2026-06-25T12:00:00Z" }},
                  ],
                  next_cursor: "cur-2",
                  has_more: true,
                }}),
              }});
            }}
            return Promise.reject(new Error("unexpected fetch " + url));
          }};

          require({json.dumps(str(APP_JS))});
          dom.ready()();

          (async () => {{
            await flush(10);
            dom.get("toggle-fullwidth").dispatchEvent("click");
            const hiddenAfterToggleOn = dom.get("seat-panel").hidden;

            dom.get("mode-history").dispatchEvent("click");
            await flush(10);
            const historyUrlCountWhileFullWidth = historyUrls.length;

            dom.get("toggle-fullwidth").dispatchEvent("click");
            const hiddenAfterToggleOff = dom.get("seat-panel").hidden;

            console.log(JSON.stringify({{
              hiddenAfterToggleOn, historyUrlCountWhileFullWidth, hiddenAfterToggleOff,
            }}));
          }})();
          """
      )
      completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
      result = json.loads(completed.stdout)
      assert result["hiddenAfterToggleOn"] is True
      assert result["historyUrlCountWhileFullWidth"] == 1
      assert result["hiddenAfterToggleOff"] is False
  ```
  `scrollHeight:100 <= clientHeight:400` is the exact "fits viewport" shape that would otherwise
  auto-fetch a second page (per Task 7's pure-function proof) — this test proves the real call
  site actually suppresses it when `fullWidth` is on, not just that the guard function works in
  isolation.

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k full_width_hides_seat_panel
  ```
  Expected: FAIL — `historyUrlCountWhileFullWidth` comes back `2` (the second page auto-fetches
  because `applyHistoryPage`'s call site doesn't pass `fullWidth` yet, so the destructured
  parameter is `undefined`/falsy regardless of the button's actual state).

- [ ] **Step 3: Write minimal implementation**
  In `applyHistoryPage` (`app.js:534-562`), update the `shouldAutoFetchHistoryPage(...)` call:
  ```js
  if (
    shouldAutoFetchHistoryPage({
      seatSource,
      historyHasMore,
      historyLoading,
      historyCursor,
      scrollHeight: seatPanelEl.scrollHeight,
      clientHeight: seatPanelEl.clientHeight,
      fullWidth,
    })
  ) {
    fetchHistoryPage({ append: true });
  }
  ```

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 32 tests green (bumped by 1 — Task 11 gained a second test, plan-panel fix). This
  completes every spec-mandated JS/DOM-harness test (spec §5.1–§5.3 items 1–13); only the
  HTML/CSS string-assertion tests (§5.4) remain.

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/app.js tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): wire fullWidth into applyHistoryPage's auto-fetch guard"
  ```

---

## Task 17 — `index.html`: `#filter-bar` markup + CSS

**Files:**
- Modify: `src/arb_memory/static/index.html`
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:** none — declarative markup/CSS only.

- [ ] **Step 1: Write the failing test** (spec §5.4 item 14)
  ```python
  def test_index_html_filter_bar_markup_and_css():
      html = INDEX_HTML.read_text()
      assert '<div id="filter-bar">' in html
      assert '<select id="status-filter">' in html
      for value in ("all", "running", "incomplete", "done", "failed", "voted", "stale", "unknown"):
          assert f'<option value="{value}">{value}</option>' in html
      status_order = [
          html.index(f'<option value="{v}">{v}</option>')
          for v in ("all", "running", "incomplete", "done", "failed", "voted", "stale", "unknown")
      ]
      assert status_order == sorted(status_order)
      assert '<select id="agent-filter">' in html
      assert '<option value="all">all</option>' in html
      assert '<span id="filter-count">' in html
      for selector in (
          "#filter-bar{",
          "#filter-bar label{",
          "#filter-bar select{",
          "#filter-bar select:focus{",
          "#filter-count{",
      ):
          assert selector in html, f"missing selector/rule: {selector!r}"
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k filter_bar_markup
  ```
  Expected: FAIL — `#filter-bar` does not exist in `index.html` yet.

- [ ] **Step 3: Write minimal implementation**
  Add the CSS block directly after the existing `.segmented button[aria-pressed="true"]{...}` rule
  (`index.html:71-73`), before `main{...}`:
  ```css
  #filter-bar{
    align-items:center; background:var(--paper-sunk); border-bottom:1px solid var(--line-200);
    display:flex; flex-wrap:wrap; gap:16px; padding:10px 18px;
  }
  #filter-bar label{
    align-items:center; color:var(--ink-500); display:inline-flex; gap:6px;
    font-family:var(--mono); font-size:.6875rem; letter-spacing:.06em; text-transform:uppercase;
  }
  #filter-bar select{
    background:var(--card); border:1px solid var(--line-300); border-radius:5px;
    color:var(--ink-900); font-family:var(--mono); font-size:.8125rem; padding:6px 10px;
  }
  #filter-bar select:focus{
    background:var(--card); border-color:var(--clay-600); box-shadow:0 0 0 3px var(--clay-100);
    outline:none;
  }
  #filter-count{
    color:var(--ink-500); font-family:var(--mono); font-size:.75rem; margin-left:auto;
  }
  ```
  Add the markup between `<div id="auth-banner"...>...</div>` and `<main id="app">`
  (`index.html:155-156`):
  ```html
  <div id="filter-bar">
    <label>
      Status
      <select id="status-filter">
        <option value="all">all</option>
        <option value="running">running</option>
        <option value="incomplete">incomplete</option>
        <option value="done">done</option>
        <option value="failed">failed</option>
        <option value="voted">voted</option>
        <option value="stale">stale</option>
        <option value="unknown">unknown</option>
      </select>
    </label>
    <label>
      Agent
      <select id="agent-filter">
        <option value="all">all</option>
      </select>
    </label>
    <span id="filter-count"></span>
  </div>
  ```

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 33 tests green (bumped by 1 — Task 11 gained a second test, plan-panel fix).

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/index.html tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): add #filter-bar markup and CSS"
  ```

---

## Task 18 — `index.html`: `#transcript-toolbar` markup + `.dim` CSS

**Files:**
- Modify: `src/arb_memory/static/index.html`
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:** none — declarative markup/CSS only.

- [ ] **Step 1: Write the failing test** (spec §5.4 item 15)
  ```python
  def test_index_html_transcript_toolbar_markup_and_css():
      html = INDEX_HTML.read_text()
      toolbar_idx = html.index('<div id="transcript-toolbar">')
      timeline_idx = html.index('<pre id="timeline">')
      assert toolbar_idx < timeline_idx
      assert html.index("<section>") < toolbar_idx < html.index("</section>")
      for button_id, label in (
          ("toggle-timestamps", "Timestamps"),
          ("toggle-labels", "Labels"),
          ("toggle-expand", "Expand output"),
          ("toggle-fullwidth", "Full width"),
      ):
          assert f'<button id="{button_id}" type="button" aria-pressed="false">{label}</button>' in html
          assert f'id="{button_id}" type="button" aria-pressed="false" disabled' not in html
      for selector in (
          "#transcript-toolbar{",
          "#transcript-toolbar button{",
          '#transcript-toolbar button[aria-pressed="true"]{',
          ".dim{",
      ):
          assert selector in html, f"missing selector/rule: {selector!r}"
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k transcript_toolbar_markup
  ```
  Expected: FAIL — `#transcript-toolbar` does not exist in `index.html` yet.

- [ ] **Step 3: Write minimal implementation**
  Add the CSS blocks directly after `#timeline details summary{...}` (`index.html:123`), before
  `#auth-banner{...}`:
  ```css
  #transcript-toolbar{
    border-bottom:1px solid var(--line-200); display:flex; flex-wrap:wrap; gap:8px; padding:10px 14px;
  }
  #transcript-toolbar button{
    font-size:.75rem; padding:6px 10px;
  }
  #transcript-toolbar button[aria-pressed="true"]{
    background:var(--clay-100); border-color:var(--clay-600); color:var(--clay-700);
  }
  .dim{ color:var(--ink-500); }
  ```
  Replace the `<section>` block (`index.html:161-163`):
  ```html
  <section>
    <div id="transcript-toolbar">
      <button id="toggle-timestamps" type="button" aria-pressed="false">Timestamps</button>
      <button id="toggle-labels" type="button" aria-pressed="false">Labels</button>
      <button id="toggle-expand" type="button" aria-pressed="false">Expand output</button>
      <button id="toggle-fullwidth" type="button" aria-pressed="false">Full width</button>
    </div>
    <pre id="timeline"></pre>
  </section>
  ```

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 34 tests green (bumped by 1 — Task 11 gained a second test, plan-panel fix).

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/index.html tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): add #transcript-toolbar markup and .dim CSS"
  ```

---

## Task 19 — `index.html`: CSS layout restructuring (removes both `calc(100vh - 73px)` rules)

**Files:**
- Modify: `src/arb_memory/static/index.html`
- Test: `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:** none — declarative CSS only.

- [ ] **Step 1: Write the failing test** (spec §5.4 item 16)
  ```python
  def test_index_html_css_layout_restructure():
      html = INDEX_HTML.read_text()
      assert "calc(100vh - 73px)" not in html
      assert "html, body{ height:100%; }" in html
      assert html.count("flex:1 1 auto; min-height:0;") >= 2
      assert html.count("display:flex; flex-direction:column;") >= 2
  ```

- [ ] **Step 2: Run test to verify it fails**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k css_layout_restructure
  ```
  Expected: FAIL — both `calc(100vh - 73px)` occurrences are still present.

- [ ] **Step 3: Write minimal implementation**
  Add a new rule directly after the existing `html,body{margin:0}` (`index.html:29`):
  ```css
  html, body{ height:100%; }
  ```
  Replace `body{...}` (`index.html:30-33`):
  ```css
  body{
    background:var(--paper); color:var(--ink-700); font-family:var(--serif);
    -webkit-font-smoothing:antialiased; line-height:1.4;
    display:flex; flex-direction:column;
  }
  ```
  Replace `main{...}` (`index.html:75`):
  ```css
  main{ display:grid; grid-template-columns:minmax(260px,34%) 1fr; flex:1 1 auto; min-height:0; }
  ```
  Replace `section{...}` (`index.html:118`):
  ```css
  section{
    min-width:0; background:var(--paper); border-radius:8px;
    display:flex; flex-direction:column;
  }
  ```
  Replace `#timeline{...}` (`index.html:119-122`):
  ```css
  #timeline{
    flex:1 1 auto; min-height:0; overflow:auto;
    margin:0; padding:14px 18px; white-space:pre-wrap; font-family:var(--mono); color:var(--ink-700);
  }
  ```
  Leave `@media (max-width:720px){...}` (`index.html:132-135`) **completely unchanged**.

- [ ] **Step 4: Run test to verify it passes**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all 35 tests green (bumped by 1 — Task 11 gained a second test, plan-panel fix) —
  every task-level test in this plan now passes.

- [ ] **Step 5: Commit**
  ```bash
  git add src/arb_memory/static/index.html tests/arb_memory/test_visibility_web_contract.py
  git commit -m "feat(arb-visibility-web): replace calc(100vh - 73px) with two-level flex layout"
  ```

---

## Task 20 — Full regression run

**Files:** none (verification only)

- [ ] **Step 1: Run the full contract test file once more as a final gate**
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
  ```
  Expected: all tests green — the 18 pre-existing tests (4 updated with new expected values in
  Tasks 6/7/8/9, 14 untouched), plus every new test added across Tasks 1–5 (pure functions), 10–12
  (scaffolding/renderSeats/toggle wiring), 13–16 (buffer/rerender/clearing/full-width DOM-harness
  tests), and 17–19 (HTML/CSS).

- [ ] **Step 2: Run the broader gateway test suite once**, to confirm `visibility.py`'s own test
  file (and anything else under `tests/arb_memory/`) was never disturbed — this plan never touches
  the backend:
  ```bash
  PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest tests/arb_memory/ -q
  ```

- [ ] **Step 3:** No commit — this task is a verification checkpoint, not a code change.

---

## Task 21 — Manual browser verification (Playwright, required — not automated)

**Files:** none (manual QA only)

The prior stage's manual-verification task was skipped ("browser tooling unavailable"). This time,
use Playwright directly — do not repeat that gap.

- [ ] **Step 1: Rebuild the local dev container** so the static file changes actually reach the
  running page. `visibility` shares the `arb-memory:phase3` image tag with `memory`/`audit`/`mcp` —
  only `memory` carries a `build:` block, so a `visibility`-scoped rebuild is a silent no-op; you
  must rebuild `memory` and force-recreate `visibility`:
  ```bash
  docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml build memory
  docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up -d --force-recreate visibility
  ```

- [ ] **Step 2: Load `http://localhost:8810` in Playwright** with a valid bearer token entered (the
  same local Postgres/Redis-backed `visibility` service described in
  `deploy/docker-compose.local.yml` — it points at the host's real fleet data via
  `host.docker.internal`, so real seats should be visible once an orchestrator is selected).
  Capture a snapshot/screenshot after initial load; confirm no console errors.

- [ ] **Step 3: Status filter** — select each of the 8 values in `#status-filter` in turn; for each,
  confirm `#filter-count`'s `X/Y` updates and only seats matching that status (or all, for
  `"all"`) appear in the sidebar. Confirm `unknown` is selectable and (if any seat currently lacks
  a `state`) filters correctly.

- [ ] **Step 4: Agent filter** — confirm `#agent-filter` populates with the distinct agent labels
  actually present in the loaded seats (plus `"all"`); select one, confirm the sidebar narrows;
  switch orchestrators and confirm the dropdown's option list updates to the new orchestrator's
  agents (not stale from the previous one).

- [ ] **Step 5: Timestamps / Labels** — select a seat with existing transcript history, confirm
  lines render with no timestamp/label prefix by default; click Timestamps, confirm the
  **already-displayed** lines immediately gain timestamps (not just new ones arriving after);
  click Labels, same proof for source/kind labels; click both off again, confirm the prefix
  disappears from already-displayed lines too.

- [ ] **Step 6: Expand output** — find or produce a tool-output entry long enough to truncate (>6
  lines, plain-text kind); confirm it shows the first 6 lines plus the `.dim`-styled "click Expand
  output to see more" hint; click Expand output, confirm the full content appears immediately for
  that already-displayed entry; click it off again, confirm it re-truncates. Separately confirm an
  `apply_patch` `<details>` entry is never truncated by this feature regardless of the toggle.

- [ ] **Step 7: Full width** — click Full width, confirm the sidebar (`aside#seat-panel`)
  disappears and the transcript pane takes the full row width; while in History mode with more
  pages available, confirm no additional history page fetch fires as a direct/immediate result of
  the toggle (watch the network tab); click Full width again, confirm the sidebar reappears and
  scroll-triggered pagination still works normally.

- [ ] **Step 8: Clear preserves preferences** — set a non-default filter/toggle combination, click
  Clear, re-enter a valid token; confirm the previously-set filter/toggle state is restored exactly
  (proving `Clear` never touched the six persisted keys), and confirm the previously-open stream
  does not silently repopulate stale seats (the prior stage's own regression, still closed).

- [ ] **Step 9: Console check** — review the browser console/network tab across every step above
  for any error the string-assertion and Node-subprocess tests couldn't have surfaced. Treat any
  finding as a fix within this same feature's scope, not a new design round.

Report the outcome of this checklist (pass/fail per step, and any bug found) before considering
this feature done.

# Authoring brief: ARB Visibility web — Go-parity controls — implementation PLAN

Author the task-by-task **implementation plan** (TDD). Author only — do not implement. Turn the
remediated spec into a plan a codex TDD worker can execute step by step.

**REQUIRED SUB-SKILL for this brief:** follow `superpowers:writing-plans` discipline exactly —
plan header (Goal/Architecture/Tech Stack/Global Constraints), bite-sized 2-5 minute steps
(write failing test → run red → minimal impl → run green → commit), complete code in every step,
exact file paths, an Interfaces block per task.

## Inputs (read; do NOT read any other plan draft)

- **The remediated spec (the contract):** `docs/superpowers/specs/2026-07-03-arb-visibility-web-controls-spec.md`
  — read ALL of it. This spec was itself corrected after its own panel review found a P0 in
  `isTruncatableOutput` (§3.4) — the version in the file NOW is the corrected one (with an explicit
  `tool_name === "apply_patch"` exclusion). Build against exactly what's there; do not "simplify"
  §3.4 back toward a kind-only check.
- The design (context, three remediation rounds; the spec is authoritative over it):
  `docs/superpowers/specs/2026-07-03-arb-visibility-web-controls-design.md`.
- Real code under change: `src/arb_memory/static/app.js`, `src/arb_memory/static/index.html`.
- Read-only reference: `tools/arb-watch-go/reduce.go`, `tools/arb-watch-go/model.go`.
- The existing test file/harness to extend: `tests/arb_memory/test_visibility_web_contract.py`
  (currently 18 tests, all passing, from the prior merged feature — new tests append here, no new
  file). `tests/fixtures/sse_web_orchestrator.txt` if referenced.

## The plan must have (writing-plans discipline)

1. **Header:** goal, architecture (2-3 sentences: filter bar + toggle strip + transcript buffer,
   all client-side, backend untouched), tech stack, and a **Global Constraints** block with the
   spec's exact values: the corrected `isTruncatableOutput` function body verbatim (the
   `apply_patch`-exclusion version, not kind-only), the 20-entry `module.exports` list, the 8-value
   status filter list (including `unknown`), the seven transcript-buffer-clearing sites (not five),
   the `.dim` CSS class (new), the two-level flex layout replacing both `calc(100vh - 73px)` rules.
2. **File structure:** the two files changed, one line each — no new files.
3. **Bite-sized TDD tasks.** Order matters: land the pure functions first (`agentOf`,
   `visibleSeats`, `deriveAgentOptions`, `isTruncatableOutput`, `collapseOutput` — each independently
   testable via the existing Node-subprocess harness), then the four existing-test updates (spec
   §5.2, exact new expected values already given — do NOT let a task re-derive these, copy them
   verbatim from the spec), then the DOM/CSS layer (filter bar + toggle strip markup, the CSS
   layout restructuring), then the stateful wiring (persistence, `openOrchestrator`'s filter-reset,
   the transcript buffer + `rerenderTranscript()` + the seven clearing sites, the `fullWidth` guard
   on `shouldAutoFetchHistoryPage`). Show the ACTUAL test code and impl for every load-bearing task
   — especially `isTruncatableOutput` (include the spec's own load-bearing test case: both `kind`
   and `tool_name: "apply_patch"` set together must return `false`) and the transcript-buffer
   rerender-on-toggle test (proving already-displayed lines change, not just future ones).
4. **A dedicated task for the seven buffer-clearing sites** — since the spec's own remediation
   found two of these missed by an earlier five-site pass (a `grep` for one exact string pattern
   undercounted the actual rule), the plan should have the TDD worker `grep` the real, current
   `app.js` for BOTH `timelineEl.textContent = ""` AND `timelineEl.textContent = "[error]` at build
   time (not trust a possibly-stale count) to confirm exactly seven sites before editing, catching
   an eighth site if the codebase has changed since this spec was written.
5. **Ordering:** pure functions and existing-test updates first (fast, isolated, no DOM harness
   needed) — then DOM-harness tests (buffer/rerender, full-width fetch-suppression) — then
   HTML/CSS string-assertion tests last (declarative, lowest regression risk). Each task ends with
   an independently testable deliverable + a commit.
6. **Test commands:** `PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m pytest
   tests/arb_memory/test_visibility_web_contract.py -q` after every task; a final full-suite run
   (`tests/arb_memory/ -q`) as the last verification task.
7. **A final manual-verification task** (Playwright, not the "browser tooling unavailable" gap from
   the prior feature — this time explicitly instruct: load the local dev container
   (`http://localhost:8810`, rebuild if needed), click through every new control (status filter,
   agent filter narrowing/reconciling, all four toggles including confirming Timestamps/Labels
   actually change ALREADY-displayed lines not just future ones, Full-width hiding the sidebar and
   suppressing history pagination, Clear not resetting persisted preferences), check the browser
   console for errors throughout.

## Output

Write the plan to the path given in your dispatch task line (absolute, inside
`docs/superpowers/plans/`, following sibling naming — `2026-07-03-arb-visibility-web-controls.md`).
Complete, buildable, no placeholders. Final message: a 5-8 line summary + the file path + the 2-3
plan decisions you're least sure are right.

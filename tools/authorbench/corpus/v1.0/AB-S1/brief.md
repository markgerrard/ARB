# Authoring brief: ARB Visibility web page — restyle + history parity — implementation SPEC

Author the **implementation spec** for this feature. Author only — no implementation, no
task-by-task plan (that's the next stage). Turn the (already panel-reviewed and remediated) design
into a precise, buildable contract.

## Inputs (read these; do NOT read any other spec/plan *draft*)

- **The remediated design:** `docs/superpowers/specs/2026-07-03-arb-visibility-web-design.md` —
  read ALL of it, especially the final **"## Warm-orchestrator remediation (post design-panel)"**
  section, which OVERRIDES earlier text where they differ. In particular: the `reduceSeat`
  no-op rationale is corrected (wiring it in would freeze every seat's displayed state — do not
  ship the "harmless no-op" framing anywhere); `streamSSE` gets a real, scoped fix (structured
  `{message, status}` error events, a `fatal` flag that stops the reconnect loop on a 401/403 —
  this is now IN scope, the earlier "streamSSE unchanged" line is superseded); dark-mode media
  query is now in scope (not a deferred risk); UTC-based `dd/mm` formatting is required; an age
  re-render timer is required; mode-toggle must clear `selectedTaskId` + stop the seat stream;
  history pagination needs a viewport-fill fallback in addition to the scroll listener. The
  "ACKNOWLEDGED — explicitly deferred" section lists what is deliberately NOT changing (no-new-hues
  badges stay as designed; orchestrator-row segregation is not replicated).
- The real code under change: `src/arb_memory/static/index.html`, `src/arb_memory/static/app.js`.
- The backend it talks to (read-only reference, not modified — confirm the spec's assumptions
  against it, do not propose backend changes): `src/arb_memory/visibility.py` — `_reduce_seat`
  (exact output field shape/names), `_bearer_token`/`_principal`/`authenticated` (the auth gate),
  `seats_history`/`_history_row_to_seat`/`_history_seat_state` (the history endpoint's exact
  response shape and state-mapping), the SSE routes' exact 401 behavior.
- The styling reference (the "door page"): `src/arb_memory/mcp/login.py` — full `<style>` block,
  exact CSS custom properties. Copy tokens verbatim; do not introduce close-but-different values.
- The companion, already-shipped, panel-reviewed Go TUI this design explicitly ports mechanisms
  from: `tools/arb-watch-go/model.go` + `reduce.go` — `toggleHistoryMode`, `maybeFetchNextHistoryPage`,
  the `historyGen`/`historyPageMsg`/`historyErrMsg` gen-check pattern, `streamFatalEvent`/`s.fatal`
  (the exact mechanism the remediated `streamSSE` fix mirrors), `seatAge`/`ageLabel`'s UTC `dd/mm`
  rule, the 2s age-tick timer.
- The existing test harness this feature's tests must fit: `tests/arb_memory/test_visibility_web_contract.py`
  — read it in full. It spawns `node` as a subprocess via Python's `subprocess`, `require()`s
  `app.js` directly (which exports via a `typeof module !== "undefined"` CommonJS block at the
  bottom of the file), and asserts JS output matches Python `_reduce_seat` output over a shared SSE
  fixture (`tests/fixtures/sse_web_orchestrator.txt`). There is no npm/package.json/JS test
  framework in this repo — new tests for new `app.js` functions (age formatting, the historyGen
  guard helpers, the corrected `streamSSE` error shape) must follow this exact
  Python-spawns-Node-requires-the-file pattern, not introduce a new JS test runner.
- **Do NOT read** any file matching `*-spec-*draft*.md` or `*-plan-*.md` — other authors' drafts;
  author independently.

## The spec must pin (be exact and buildable)

1. **CSS — exact values, not "use the door tokens."** The full `:root` custom-property block
   (light + the new dark-mode media-query override with exact swapped values), every selector this
   design touches (`body`, `header`, `label`, `input`/`select`, buttons incl. the new segmented
   Live/History toggle's active-state styling, `aside`/`#seats li`/`aria-current`, `section`/
   `#timeline`/`<details><summary>`, the new `#auth-banner`, the new age-column styling incl. the
   `running`-state pulse `@keyframes` + `prefers-reduced-motion` override, the state-badge table
   exactly as the design specifies — no new hues, `failed` gets clay, `stale` gets muted ink-400).
   Exact border-radii (8px pane-level, 5px control-level), exact padding, exact `box-shadow`.
2. **HTML structure changes.** The new `#auth-banner` element (attributes, initial `hidden` state),
   the new Live/History segmented control markup (two buttons? a `role="tablist"`? pin the exact
   markup), where the age column goes in each seat row's markup, the `"· history"` status line
   placement in the sidebar.
3. **`app.js` — every new/changed function, named and signed exactly:**
   - `streamSSE` changes: the exact new error-event shape (`{ event: "error", data: { message,
     status } }`), the `fatal` flag / stop-condition logic (4xx → set `stopped = true` after
     emitting, do not re-enter the loop; non-4xx keeps existing backoff/retry unchanged), and how a
     `streamSSE` consumer (the orchestrator stream, the seat stream) is told a stream died fatally
     vs. is still retrying (does the caller need a new callback parameter, or is checking
     `data.status` on the emitted error event sufficient? Decide and pin it).
   - The historyGen state machine: exact new client-state variables (`seatSource`, `historyCursor`,
     `historyHasMore`, `historyLoading`, `historyGen`) and the exact bump/guard logic for: toggle-to-
     history, toggle-to-live (stop+restart the orchestrator SSE with no `Last-Event-ID`), orchestrator
     switch while in history mode, and the stale-fetch discard check on every history fetch's
     resolution.
   - The history fetch function(s): exact signature, exact URL construction against
     `GET /orchestrators/{id}/seats/history` with `limit`/`cursor` params, exact handling of
     `next_cursor`/`has_more` from the response.
   - Pagination: the scroll-listener threshold and condition, AND the viewport-fill fallback (pin
     the exact check — e.g. compare `scrollHeight` to `clientHeight` after each page render; loop
     auto-fetching while content doesn't overflow and `historyHasMore` is true).
   - Age formatting: `ageLabel(seconds)` (identical to the Go `ageLabel`, already exists as a
     pattern to port) plus the UTC `dd/mm` branch (exact: `getUTCDate()`/`getUTCMonth()`, zero-padded,
     `dd/mm` order, applies only when `seatSource === "history"` and age `>= 86400` seconds).
   - The age re-render timer: exact interval, what it re-renders (age text only, or triggers a full
     `renderSeats()`), interaction with `prefers-reduced-motion` (timer keeps running regardless;
     only the pulse *animation* is disabled by the media query, per the design).
   - `reduceSeat`: the EXACT corrected guard-comment text to place above it in `app.js` (the spec
     should quote the exact comment verbatim so the plan/implementation stage doesn't improvise
     wording that regresses to the "no-op" framing). No call sites change — it stays exported,
     unwired, present for the contract test only.
   - Mode-toggle/orchestrator-switch cleanup: exact statement that `selectedTaskId` is cleared and
     any active seat-transcript stream is stopped on every mode toggle and orchestrator switch (not
     just "clear seats" — spell out the seat-stream/selectedTaskId cleanup explicitly since this was
     a real gap the design's remediation caught).
   - Auth banner show/hide: exact trigger condition (`status === 401 || status === 403` from the
     `/orchestrators` fetch OR a `streamSSE` fatal error event), exact show/hide DOM manipulation,
     and confirm the non-auth error paths (network failure, 5xx, malformed history cursor) keep
     their existing inline-timeline-text behavior unchanged.
4. **Interfaces block:** every new/changed function's exact name, parameters, and return
   shape/behavior — this is what a plan-stage task-splitter and an implementer rely on. Include
   which functions must be added to the existing `module.exports` block (for contract-testability)
   alongside the already-exported set (`parseFrames`, `reduceSeat`, `isRealEventId`,
   `formatTimelineEvent`, `formatTimelineFrame`, `appendTimelineFrame`, `escapeHtml`, `authHeaders`,
   `streamSSE`).
5. **The complete test list** (name + one-line intent each), following the existing
   Python-spawns-Node contract-test pattern. Cover at minimum: the existing contract test must still
   pass unmodified (or note precisely if its fixture/assertions need updating for the `streamSSE`
   error-shape change); a new test asserting a 401/403 `streamSSE` response stops retrying (does not
   re-issue a `fetch` after the fatal event — this needs a way to assert "no further calls," e.g. a
   mock `fetch` counting invocations over a bounded wait); a test for the UTC `dd/mm` formatter
   against a fixed timestamp near a UTC-day boundary (proving it does NOT shift with local
   timezone); a test for the historyGen guard discarding a stale-generation fetch result; a
   pagination test for the viewport-fill fallback triggering additional fetches when content doesn't
   overflow; a test that `reduceSeat` remains present/exported and the existing parity assertion
   still holds byte-identical.
6. Exact test commands (mirror the existing suite's invocation:
   `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q`
   plus wherever the plan stage adds new test file(s) under the same directory).

## Output

Write the spec to the path given in your dispatch task line (absolute, inside
`docs/superpowers/specs/`, following the naming convention of sibling files —
`2026-07-03-arb-visibility-web-spec.md`). Tight and decided — no placeholders, no "the plan will
decide this." Final message: a 5-8 line summary + the file path + the 2-3 spec decisions you're
least sure are right.

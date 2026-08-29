# ARB Visibility web page — restyle + history parity — implementation plan

Authored against `docs/superpowers/specs/2026-07-03-arb-visibility-web-spec.md` AS REMEDIATED by its
final `## Warm-orchestrator remediation (post spec-panel)` section (authoritative over the raw body
above it). Author only — no implementation happened while writing this plan.

## Goal

Restyle the ARB Visibility operator page to the "door page" aesthetic, add live/history seat-view
parity with the already-shipped Go TUI, and fix a real pre-existing bug (`streamSSE` never stops
retrying after a 4xx, and `Clear` never actually disconnects the SSE streams it appears to reset) —
all within the two existing client files, backend untouched.

## Architecture

Two static files change: `src/arb_memory/static/index.html` (structure + `<style>` block) and
`src/arb_memory/static/app.js` (all client logic). The backend
(`src/arb_memory/visibility.py`) is read-only reference — its routes, auth, SSE framing, and the
`GET /orchestrators/{id}/seats/history` endpoint already exist and are correct; nothing there
changes. `app.js` keeps its existing shape: an IIFE exporting a handful of pure, `require()`-able
functions for Node-based contract tests, plus an `init()` closure (registered on
`DOMContentLoaded`) holding all DOM references and mutable client state (`seats`, `seatSource`,
`historyGen`, …). This plan adds six new pure functions to the exported surface (age formatting,
the history-generation guard, and the two pagination-trigger predicates), rewrites `streamSSE` to
stop retrying on a 4xx, rewrites `Clear`/`loadOrchestrators`/`openOrchestrator`/`selectSeat` to
route 401/403 through a new auth banner instead of dumping text into the timeline, adds a
live/history state machine (`setSeatSource`, `fetchHistoryPage`, `applyHistoryPage`) that
wholesale-replaces the single `seats` map (no second history map), and restyles every CSS rule
against the token set already used by the door login page (`src/arb_memory/mcp/login.py`).

## Tech stack

Vanilla JS (no framework, no bundler, no `package.json`), CSS custom properties, Starlette/Python
backend (unchanged). Tests: Python `pytest` driving `subprocess.run([node, "-e", script])` against
`app.js` (the existing harness pattern — no new JS test runner) plus plain-Python string assertions
against `index.html`'s served text for the CSS/HTML tasks. All new tests are appended to the one
existing test file, `tests/arb_memory/test_visibility_web_contract.py` — no new test file.

## Global Constraints

Values and mechanisms below are pinned exactly per the remediated spec; no task should re-derive
them.

1. **The 11 `:root` custom properties, light block (verbatim from `login.py:41-49`):**
   ```css
   :root{
     --paper:#FAF8F4; --paper-sunk:#F3EFE8; --card:#FFFFFF;
     --ink-900:#1F1D1A; --ink-700:#45413B; --ink-500:#6E6960; --ink-400:#918B80;
     --line-200:#E7E2D8; --line-300:#D9D3C7;
     --clay-600:#9E4A2E; --clay-700:#823A22; --clay-100:#EFE0D6;
     --serif:'Newsreader',Georgia,'Times New Roman',serif;
     --mono:'IBM Plex Mono',ui-monospace,'SF Mono',Menlo,monospace;
     --ease:cubic-bezier(.4,0,.2,1);
   }
   ```
   **Dark block** (`@media (prefers-color-scheme: dark)`, an inverted mapping within the same token
   names — NOT a straight copy):
   ```css
   @media (prefers-color-scheme: dark) {
     :root{
       --paper:#1F1D1A; --paper-sunk:#28251F; --card:#322E27;
       --ink-900:#FAF8F4; --ink-700:#D9D3C7; --ink-500:#A8A299; --ink-400:#6E6960;
       --line-200:#3A362E; --line-300:#4A453B;
       --clay-600:#C97350; --clay-700:#E0916B; --clay-100:#3D2A20;
     }
   }
   ```
   **`--ink-500` is `#A8A299` in the dark block, NOT `#918B80`** — this is the WCAG-AA contrast fix
   from the spec's post-spec-panel remediation item 6 (the pre-remediation value failed AA against
   `--card:#322E27`). Do not use the pre-remediation value from anywhere else in this repo's history.
2. **Door-page font pair** (verbatim `<link>` pair, from `login.py:37-39`), placed in `index.html`'s
   `<head>`:
   ```html
   <link rel="preconnect" href="https://fonts.googleapis.com">
   <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
   <link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
   ```
3. **`streamSSE`'s 4xx-range fatal-stop condition is `status >= 400 && status < 500`** — any 4xx,
   not a narrower "401/403 only" condition (matches the Go sibling's
   `resp.StatusCode >= 400 && resp.StatusCode < 500`, `tools/arb-watch-go/sse.go:126`). The
   auth-banner trigger is a **narrower** subset of this broader stop condition:
   `status === 401 || status === 403`. A 404/429 stops retrying but does not show the banner.
4. **The historyGen guard has two check-points on every history fetch**, not one: once immediately
   after `fetch()` resolves, and again after `response.json()` resolves. Both use the same
   `isStaleHistoryGen(requestGen, currentGen)` pure function.
5. **The exact `module.exports` list — 15 entries (9 existing + 6 new):**
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
   };
   ```
6. **The `reduceSeat` guard comment — exact text, quoted here in full, use verbatim, do not
   paraphrase** (this is the empirically-verified wording from the spec's post-spec-panel
   remediation item 2, which supersedes the earlier "freeze at first state" framing from the
   design/spec body):
   ```js
   // Do NOT call from the live path. It expects raw redis fields (event_type/sent_at); a live
   // frame is already reduced server-side and carries last_event/last_event_ts instead — none of
   // this function's event_type branches would ever fire, and state would never be set at all
   // (verified: feeding it real already-reduced frames leaves `state` absent from every output,
   // not frozen at a prior value — the seat would render "unknown" forever). Kept unwired, exported
   // only for the JS/Python parity contract test (test_appjs_parser_and_reducer_match_visibility_
   // reducer), which asserts this function stays byte-identical to Python's _reduce_seat over a
   // shared fixture.
   ```
7. **Function scope split (spec post-spec-panel remediation item 9):** the six new pure functions
   (`ageLabel`, `utcDayMonth`, `seatAgeLabel`, `isStaleHistoryGen`, `isScrolledNearBottom`,
   `shouldAutoFetchHistoryPage`) live at IIFE module scope (so `module.exports` can reach them).
   Everything else new (`setSeatSource`, `fetchHistoryPage`, `applyHistoryPage`,
   `updateModeButtons`, `showAuthBanner`, `hideAuthBanner`, `startOrchestratorStream`, the scroll
   listener, the age-tick `setInterval`) lives inside `init()` — they close over `seats`,
   `historyGen`, and the DOM element references.
8. **`init()` gains five new `const` DOM references** (spec item 8): `modeLiveButton`,
   `modeHistoryButton`, `authBannerEl`, `seatPanelEl`, `historyStatusEl` — declared alongside the
   existing `tokenInput`/`clearToken`/`orchestratorSelect`/`seatsEl`/`timelineEl` block.
9. **`Clear` is a full client reset, not just DOM/localStorage** (spec's final, post-spec-panel
   remediation item 1 — a genuine behavior change to existing code): stop both SSE stream handles,
   clear `seats`, reset `selectedTaskId`/`selectedOrchestratorId`, bump `historyGen`, reset
   `seatSource` to `"live"`, clear `historyCursor`/`historyHasMore`/`historyLoading`/
   `historyStatusText`, disable both mode buttons — **in that order**, before the existing
   DOM-clear/banner-hide steps. A regression test must prove the old bug (a still-open SSE stream
   silently repopulating the sidebar after Clear) is actually closed, not just that new fields get
   zeroed.
10. **`shouldAutoFetchHistoryPage` takes a `historyCursor` param and must treat a falsy cursor as
    "don't auto-fetch"** (spec item 7) — without this, a `has_more:true` page with a null
    `next_cursor` would re-request page 1 forever. Threaded through both call sites (the viewport-fill
    check in `applyHistoryPage` and the scroll listener's early-return guard).
11. **No new test file, no npm/package.json, no JS test framework.** Every new test is appended to
    `tests/arb_memory/test_visibility_web_contract.py`, reusing its exact
    `subprocess.run([node, "-e", script])` pattern for JS-level assertions, and plain
    `Path.read_text()` string assertions for the HTML/CSS tasks (there is no visual-regression
    tooling in this repo).

## File structure

| File | Responsibility |
|---|---|
| `src/arb_memory/static/index.html` | Page shell: `<head>` fonts + `<style>` (all CSS in Global Constraints §1 and below), `<body>` markup (header, mode toggle, auth banner, two-pane layout) |
| `src/arb_memory/static/app.js` | All client logic: SSE parsing/streaming, age formatting, live/history state machine, rendering, auth-banner wiring |
| `tests/arb_memory/test_visibility_web_contract.py` | Extended (not replaced) with new JS-contract tests + new plain-Python HTML/CSS string-assertion tests |

No new files are created by this plan.

## Ordering

`app.js` logic changes first (Tasks 1–9) — these carry real regression risk and are tested.
`index.html`'s structure and CSS (Tasks 10–11) are declarative and comparatively low-risk; doing
them after the JS is stable means the restyle doesn't need to be re-verified against a still-moving
render path. Task 12 re-runs the full test file once as a final regression gate. Task 13 is the
required manual browser check.

## Test command (run after every task that touches `app.js` or `index.html`)

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
```

---

## Task 1 — `streamSSE`: structured error + 4xx fatal-stop

**Files:** `src/arb_memory/static/app.js` (lines 210–268 today), `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
```js
function streamSSE(url, onEvent): () => void
```
Unchanged signature. Error events become `{ event: "error", data: { message: string, status: number | null } }`. After any 4xx (`status >= 400 && status < 500`), `connect()` returns and never calls `onEvent` again — no new callback parameter, a consumer infers "dead" from silence after the fatal error event.

**Steps**

1. Add `import os` to the test file's imports (needed by Task 2's TZ-sensitive test; harmless to add now) — one-line edit at the top of `tests/arb_memory/test_visibility_web_contract.py`:
   ```python
   import json
   import os
   import shutil
   import subprocess
   import textwrap
   from pathlib import Path
   ```

2. Add a shared JS flush-helper constant near the top of the test file (below the existing `FIXTURE` constant), used by this task's two new tests to get a deterministic, wall-clock-free wait instead of a real `sleep`:
   ```python
   _FLUSH_JS = """
   function flush(times) {
     return new Promise((resolve) => {
       let remaining = times;
       function step() {
         if (remaining <= 0) { resolve(); return; }
         remaining -= 1;
         setImmediate(step);
       }
       step();
     });
   }
   """
   ```

3. Write the two failing tests, appended at the end of the test file:
   ```python
   def test_appjs_streamsse_stops_retrying_after_4xx():
       node = _node()
       if node is None:
           raise AssertionError("node is required for the streamSSE fatal-4xx contract test")
       script = _FLUSH_JS + textwrap.dedent(
           f"""
           global.localStorage = {{ getItem: () => null, token: "" }};
           global.setTimeout = (fn) => setImmediate(fn);
           const {{ streamSSE }} = require({json.dumps(str(APP_JS))});

           let fetchCalls = 0;
           global.fetch = () => {{
             fetchCalls += 1;
             return Promise.resolve({{ ok: false, status: 401 }});
           }};

           const events = [];
           streamSSE("http://example/sse", (frame) => events.push(frame));

           flush(20).then(() => {{
             console.log(JSON.stringify({{ fetchCalls, events }}));
           }});
           """
       )
       completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
       result = json.loads(completed.stdout)
       assert result["fetchCalls"] == 1
       assert result["events"] == [
           {"event": "error", "data": {"message": "SSE 401", "status": 401}}
       ]


   def test_appjs_streamsse_keeps_retrying_after_5xx():
       node = _node()
       if node is None:
           raise AssertionError("node is required for the streamSSE non-fatal-5xx contract test")
       script = _FLUSH_JS + textwrap.dedent(
           f"""
           global.localStorage = {{ getItem: () => null, token: "" }};
           global.setTimeout = (fn) => setImmediate(fn);
           const {{ streamSSE }} = require({json.dumps(str(APP_JS))});

           let fetchCalls = 0;
           global.fetch = () => {{
             fetchCalls += 1;
             return Promise.resolve({{ ok: false, status: 500 }});
           }};

           streamSSE("http://example/sse", () => {{}});

           flush(20).then(() => {{
             console.log(JSON.stringify({{ fetchCalls }}));
           }});
           """
       )
       completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
       result = json.loads(completed.stdout)
       assert result["fetchCalls"] > 1
   ```
   Both tests set `global.localStorage` (Task's remediation item 3 — plain `node -e` has no
   `localStorage`, and `authHeaders()` calls `localStorage.getItem(...)` unconditionally inside
   `streamSSE`'s `connect()`; without this stub both tests throw a `TypeError` inside an
   un-awaited async function before the mocked `fetch` is ever reached) and override
   `global.setTimeout` to fire via `setImmediate` (remediation item 4 — makes the reconnect
   backoff deterministic and wall-clock-free; a broken, still-looping implementation reveals itself
   within the 20-tick `flush`, not after a real 500ms–5s wait).

4. Run red:
   ```bash
   PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k streamsse
   ```
   Fails: current `streamSSE` throws an unstructured `Error("SSE " + status)`, emits
   `{event:"error", data:{message}}` (no `status` key), and falls through to the reconnect loop
   unconditionally — the 4xx test's `fetchCalls == 1` assertion fails (it keeps retrying) and the
   event-shape assertion fails (`status` missing).

5. Replace `app.js:210-268`'s `streamSSE` with:
   ```js
   function streamSSE(url, onEvent) {
     let lastId = "";
     let stopped = false;
     let reconnectMs = 500;
     const decoder = new TextDecoder();
     const abortController = new AbortController();

     async function connect() {
       while (!stopped) {
         const headers = authHeaders();
         if (isRealEventId(lastId)) {
           headers["Last-Event-ID"] = lastId;
         }
         try {
           const response = await fetch(url, { headers, signal: abortController.signal });
           if (!response.ok) {
             const httpError = new Error("SSE " + response.status);
             httpError.status = response.status;
             throw httpError;
           }
           reconnectMs = 500;
           const reader = response.body.getReader();
           let buffer = "";
           while (!stopped) {
             const chunk = await reader.read();
             if (chunk.done) {
               break;
             }
             buffer += decoder.decode(chunk.value, { stream: true });
             const parsed = parseFrames(buffer);
             buffer = parsed.tail;
             for (const frame of parsed.frames) {
               if (isRealEventId(frame.id)) {
                 lastId = frame.id;
               }
               let data = frame.data;
               try {
                 data = JSON.parse(frame.data || "{}");
               } catch (_err) {
                 data = frame.data;
               }
               onEvent(Object.assign({}, frame, { data }));
             }
           }
         } catch (err) {
           if (stopped || err.name === "AbortError") {
             return;
           }
           const status = typeof err.status === "number" ? err.status : null;
           onEvent({ event: "error", data: { message: err.message, status } });
           if (status !== null && status >= 400 && status < 500) {
             stopped = true;
             return; // do not re-enter the reconnect loop; no wasted backoff wait either
           }
         }
         await new Promise((resolve) => setTimeout(resolve, reconnectMs));
         reconnectMs = Math.min(reconnectMs * 2, 5000);
       }
     }

     connect();
     return function stop() {
       stopped = true;
       abortController.abort();
     };
   }
   ```

6. Run green:
   ```bash
   PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
   ```
   All existing tests (1–4) plus the two new ones pass.

7. Commit: `fix(arb-visibility-web): streamSSE stops retrying after a 4xx instead of hammering the gateway forever`

---

## Task 2 — Age formatting: `ageLabel` / `utcDayMonth` / `seatAgeLabel`

**Files:** `src/arb_memory/static/app.js`, `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
```js
function ageLabel(seconds: number): string
function utcDayMonth(lastEventTs: string): string | null
function seatAgeLabel(lastEventTs: string, seatSource: "live" | "history", nowMs?: number): string
```

**Steps**

1. Write the failing test, appended to the test file (needs the `import os` added in Task 1 step 1):
   ```python
   def test_appjs_utc_dd_mm_formatter_ignores_local_timezone():
       node = _node()
       if node is None:
           raise AssertionError("node is required for the UTC dd/mm formatter contract test")
       script = textwrap.dedent(
           f"""
           const {{ seatAgeLabel }} = require({json.dumps(str(APP_JS))});
           const result = seatAgeLabel(
             "2026-06-25T23:50:00Z",
             "history",
             Date.parse("2026-06-27T00:00:00Z")
           );
           console.log(JSON.stringify({{ result }}));
           """
       )
       env = dict(os.environ)
       env["TZ"] = "Pacific/Kiritimati"  # UTC+14 — a naive local-time formatter shifts the date forward
       completed = subprocess.run(
           [node, "-e", script], check=True, text=True, capture_output=True, env=env
       )
       assert json.loads(completed.stdout)["result"] == "25/06"
   ```

2. Run red: `import error / NameError` — `seatAgeLabel` doesn't exist yet.

3. Implement, inserted at IIFE module scope directly after the new `streamSSE` (Task 1), before
   `init()`:
   ```js
   function ageLabel(seconds) {
     if (seconds < 60) return Math.floor(seconds) + "s";
     if (seconds < 3600) return Math.floor(seconds / 60) + "m";
     return Math.floor(seconds / 3600) + "h";
   }

   function utcDayMonth(lastEventTs) {
     const ts = parseTs(lastEventTs);
     if (ts === null) {
       return null;
     }
     const date = new Date(ts);
     const dd = String(date.getUTCDate()).padStart(2, "0");
     const mm = String(date.getUTCMonth() + 1).padStart(2, "0");
     return dd + "/" + mm;
   }

   // No year component — eval_event_raw (the table the history endpoint reads) was created
   // 2026-06-23/25; at most ~10 days of queryable history exist as of this design. Revisit if this
   // table holds more than ~11 months of history.
   function seatAgeLabel(lastEventTs, seatSource, nowMs) {
     const ts = parseTs(lastEventTs);
     if (ts === null) {
       return "—";
     }
     const secs = ((nowMs == null ? Date.now() : nowMs) - ts) / 1000;
     if (seatSource === "history" && secs >= 86400) {
       const dm = utcDayMonth(lastEventTs);
       if (dm !== null) {
         return dm;
       }
     }
     return ageLabel(secs);
   }
   ```
   `ageLabel` is byte-identical in logic to `tools/arb-watch-go/reduce.go:192-201`'s Go sibling.
   `utcDayMonth` uses `getUTCDate()`/`getUTCMonth()` explicitly, never `getDate()`/`getMonth()` — a
   naive local-time formatter would shift the displayed day depending on the operator's UTC offset.
   `parseTs` is the existing private helper (`app.js:65-71`), reused unchanged.

4. Run green.

5. Commit: `feat(arb-visibility-web): UTC-based age formatting (ageLabel/utcDayMonth/seatAgeLabel), ported from the Go TUI`

---

## Task 3 — History pure-function guards: `isStaleHistoryGen` / `isScrolledNearBottom` / `shouldAutoFetchHistoryPage`

**Files:** `src/arb_memory/static/app.js`, `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:**
```js
function isStaleHistoryGen(requestGen: number, currentGen: number): boolean
function isScrolledNearBottom(scrollTop: number, clientHeight: number, scrollHeight: number, threshold: number): boolean
function shouldAutoFetchHistoryPage(opts: {
  seatSource: "live" | "history", historyHasMore: boolean, historyLoading: boolean,
  historyCursor: string | null, scrollHeight: number, clientHeight: number,
}): boolean
```

**Steps**

1. Write the three failing tests:
   ```python
   def test_appjs_history_gen_guard_discards_stale_fetch():
       node = _node()
       if node is None:
           raise AssertionError("node is required for the history-gen guard contract test")
       script = textwrap.dedent(
           f"""
           const {{ isStaleHistoryGen }} = require({json.dumps(str(APP_JS))});
           console.log(JSON.stringify({{
             stale: isStaleHistoryGen(1, 2),
             current: isStaleHistoryGen(2, 2),
           }}));
           """
       )
       completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
       assert json.loads(completed.stdout) == {"stale": True, "current": False}


   def test_appjs_scroll_threshold_near_bottom():
       node = _node()
       if node is None:
           raise AssertionError("node is required for the scroll-threshold contract test")
       script = textwrap.dedent(
           f"""
           const {{ isScrolledNearBottom }} = require({json.dumps(str(APP_JS))});
           console.log(JSON.stringify({{
             atThreshold: isScrolledNearBottom(360, 400, 800, 40),
             aboveThreshold: isScrolledNearBottom(300, 400, 800, 40),
           }}));
           """
       )
       completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
       assert json.loads(completed.stdout) == {"atThreshold": True, "aboveThreshold": False}


   def test_appjs_viewport_fill_fallback_triggers_additional_fetch():
       node = _node()
       if node is None:
           raise AssertionError("node is required for the viewport-fill fallback contract test")
       script = textwrap.dedent(
           f"""
           const {{ shouldAutoFetchHistoryPage }} = require({json.dumps(str(APP_JS))});
           const base = {{
             seatSource: "history", historyHasMore: true, historyLoading: false,
             historyCursor: "cur-1", scrollHeight: 100, clientHeight: 400,
           }};
           console.log(JSON.stringify({{
             fitsViewport: shouldAutoFetchHistoryPage(base),
             overflowsViewport: shouldAutoFetchHistoryPage({{...base, scrollHeight: 800}}),
             stillLoading: shouldAutoFetchHistoryPage({{...base, historyLoading: true}}),
             liveMode: shouldAutoFetchHistoryPage({{...base, seatSource: "live"}}),
             noMorePages: shouldAutoFetchHistoryPage({{...base, historyHasMore: false}}),
             noCursor: shouldAutoFetchHistoryPage({{...base, historyCursor: null}}),
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
       }
   ```
   The `noCursor` case is the spec's post-spec-panel remediation item 7: without a `historyCursor`
   guard, a `has_more:true` page carrying a null/empty `next_cursor` would cause a re-fetch of page
   1 (no cursor param sent), duplicating rows.

2. Run red: `NameError` — none of the three functions exist yet.

3. Implement, inserted at module scope after Task 2's age functions:
   ```js
   function isStaleHistoryGen(requestGen, currentGen) {
     return requestGen !== currentGen;
   }

   function isScrolledNearBottom(scrollTop, clientHeight, scrollHeight, threshold) {
     return scrollTop + clientHeight >= scrollHeight - threshold;
   }

   function shouldAutoFetchHistoryPage({ seatSource, historyHasMore, historyLoading, historyCursor, scrollHeight, clientHeight }) {
     if (seatSource !== "history" || !historyHasMore || historyLoading || !historyCursor) {
       return false;
     }
     return scrollHeight <= clientHeight; // content doesn't overflow -> normal scrolling can't fire
   }
   ```

4. Run green.

5. Commit: `feat(arb-visibility-web): history pagination guards (gen-staleness, scroll-threshold, viewport-fill fallback)`

---

## Task 4 — `module.exports`: full 15-entry contract surface

**Files:** `src/arb_memory/static/app.js` (lines 395–407 today), `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:** no new function; the exports block itself is the interface under test.

**Steps**

1. Write the failing test:
   ```python
   def test_appjs_exports_full_contract_surface():
       node = _node()
       if node is None:
           raise AssertionError("node is required for the exports contract test")
       script = textwrap.dedent(
           f"""
           const mod = require({json.dumps(str(APP_JS))});
           const names = Object.keys(mod).sort();
           const types = {{}};
           for (const name of names) {{ types[name] = typeof mod[name]; }}
           console.log(JSON.stringify({{ names, types }}));
           """
       )
       completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
       result = json.loads(completed.stdout)
       expected = sorted([
           "authHeaders", "appendTimelineFrame", "escapeHtml", "formatTimelineEvent",
           "formatTimelineFrame", "isRealEventId", "parseFrames", "reduceSeat", "streamSSE",
           "ageLabel", "utcDayMonth", "seatAgeLabel", "isStaleHistoryGen",
           "isScrolledNearBottom", "shouldAutoFetchHistoryPage",
       ])
       assert result["names"] == expected
       assert all(t == "function" for t in result["types"].values())
   ```
   The `typeof ... === "function"` assertion (spec's post-spec-panel remediation item 10) guards
   against a future edit accidentally exporting `undefined` or a non-function value under the
   right key — `Object.keys(...).sort()` matching alone wouldn't catch that.

2. Run red: current export list has only 9 entries — `names` mismatch.

3. Replace `app.js:395-407`'s `module.exports` block with:
   ```js
   if (typeof module !== "undefined" && module.exports) {
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
     };
   }
   ```

4. Run green.

5. Commit: `feat(arb-visibility-web): export the six new pure functions alongside the existing nine`

---

## Task 5 — `reduceSeat` guard comment (empirically-verified wording)

**Files:** `src/arb_memory/static/app.js` (line 84 today), `tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:** none new — `reduceSeat`'s signature/behavior is unchanged; only the comment above it
changes.

**Steps**

1. Write the failing test (a cheap, exact-text guard against a future edit silently reverting to
   the earlier, factually-wrong "no-op, kept for symmetry" phrasing or paraphrasing this comment):
   ```python
   def test_appjs_reduce_seat_guard_comment_states_verified_mechanism():
       text = APP_JS.read_text()
       assert (
           "// Do NOT call from the live path. It expects raw redis fields (event_type/sent_at); a live\n"
           "// frame is already reduced server-side and carries last_event/last_event_ts instead — none of\n"
           "// this function's event_type branches would ever fire, and state would never be set at all\n"
           "// (verified: feeding it real already-reduced frames leaves `state` absent from every output,\n"
           '// not frozen at a prior value — the seat would render "unknown" forever). Kept unwired, exported\n'
           "// only for the JS/Python parity contract test (test_appjs_parser_and_reducer_match_visibility_\n"
           "// reducer), which asserts this function stays byte-identical to Python's _reduce_seat over a\n"
           "// shared fixture.\n"
           "function reduceSeat(state, entry) {"
       ) in text
   ```

2. Run red: the comment doesn't exist yet (current code has no comment above `reduceSeat` at all).

3. Insert directly above `app.js:84`'s `function reduceSeat(state, entry) {`:
   ```js
   // Do NOT call from the live path. It expects raw redis fields (event_type/sent_at); a live
   // frame is already reduced server-side and carries last_event/last_event_ts instead — none of
   // this function's event_type branches would ever fire, and state would never be set at all
   // (verified: feeding it real already-reduced frames leaves `state` absent from every output,
   // not frozen at a prior value — the seat would render "unknown" forever). Kept unwired, exported
   // only for the JS/Python parity contract test (test_appjs_parser_and_reducer_match_visibility_
   // reducer), which asserts this function stays byte-identical to Python's _reduce_seat over a
   // shared fixture.
   function reduceSeat(state, entry) {
   ```
   Do not touch `reduceSeat`'s body — `test_appjs_parser_and_reducer_match_visibility_reducer` must
   keep passing unmodified.

4. Run green — including re-running the whole file to confirm test 1
   (`test_appjs_parser_and_reducer_match_visibility_reducer`) is untouched:
   ```bash
   PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
   ```

5. Commit: `docs(arb-visibility-web): correct the reduceSeat guard comment to the empirically-verified mechanism`

---

## Task 6 — Auth banner + `loadOrchestrators` / `openOrchestrator` / `startOrchestratorStream` / `selectSeat` rewrites

**Files:** `src/arb_memory/static/app.js` (`init()`, lines 270–393 today)

**Interfaces:**
```js
function showAuthBanner(): void
function hideAuthBanner(): void
async function loadOrchestrators(): Promise<void>
function openOrchestrator(orchestratorId: string): void
function startOrchestratorStream(orchestratorId: string): () => void
function selectSeat(taskId: string): void
```
All five DOM-closure functions live inside `init()` (Global Constraint 7). This task adds the five
new `const` DOM references (Global Constraint 8) and rewrites the live-path functions to route
401/403 through the banner instead of writing inline `[error]` text.

**This task has no isolated automated test** — these functions are closures inside `init()`, not
exported, and `init()` never runs under the plain-`node -e` harness (it only fires on
`DOMContentLoaded`, which the existing tests never dispatch). Its correctness is verified two ways:
(a) the DOM-harness regression test built in Task 9 (Clear fix) exercises this exact code path —
token entry → `loadOrchestrators` → `openOrchestrator` → `startOrchestratorStream` — as a
prerequisite for that test's own scenario, so a broken wiring here fails Task 9's test with a real
`ReferenceError`/`TypeError`, not silently; (b) the mandatory manual browser check (Task 13). This
is a deliberate, narrower automated-test footprint than the pure-function tasks above — consistent
with the spec's own scope (there is no browser-testing tooling in this repo to drive `init()`
directly in isolation).

**Steps**

1. Add the five new `const` DOM references to `init()`'s existing declaration block
   (`app.js:271-275`):
   ```js
   function init() {
     const tokenInput = document.getElementById("token");
     const clearToken = document.getElementById("clear-token");
     const orchestratorSelect = document.getElementById("orchestrator");
     const seatsEl = document.getElementById("seats");
     const timelineEl = document.getElementById("timeline");
     const modeLiveButton = document.getElementById("mode-live");
     const modeHistoryButton = document.getElementById("mode-history");
     const authBannerEl = document.getElementById("auth-banner");
     const seatPanelEl = document.getElementById("seat-panel");
     const historyStatusEl = document.getElementById("history-status");
     const seats = {};
     let selectedTaskId = "";
     let selectedOrchestratorId = "";
     let stopOrchestratorStream = null;
     let stopSeatStream = null;
     let seatSource = "live";
     let historyCursor = null;
     let historyHasMore = false;
     let historyLoading = false;
     let historyGen = 0;
     let historyStatusText = "";
   ```
   (`selectedOrchestratorId`, `seatSource`, `historyCursor`, `historyHasMore`, `historyLoading`,
   `historyGen`, `historyStatusText` are the new client-state variables from spec §3.3 — Task 7
   wires the history-specific ones; this task only needs `selectedOrchestratorId` and `seatSource`.)

2. Add `showAuthBanner`/`hideAuthBanner`, anywhere inside `init()` before their first use (e.g.
   directly after the declaration block):
   ```js
   function showAuthBanner() {
     authBannerEl.hidden = false;
   }
   function hideAuthBanner() {
     authBannerEl.hidden = true;
   }
   ```

3. Replace `openOrchestrator` (`app.js:340-361`) with a version that also resets history/mode state
   on every orchestrator switch and starts its stream via the new `startOrchestratorStream` helper:
   ```js
   function openOrchestrator(orchestratorId) {
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

     Object.keys(seats).forEach((key) => delete seats[key]);
     selectedTaskId = "";
     timelineEl.textContent = "";
     if (stopOrchestratorStream) {
       stopOrchestratorStream();
     }
     if (stopSeatStream) {
       stopSeatStream();
       stopSeatStream = null;
     }
     renderSeats();
     stopOrchestratorStream = startOrchestratorStream(orchestratorId);
   }

   function startOrchestratorStream(orchestratorId) {
     return streamSSE("/sse/orchestrator/" + encodeURIComponent(orchestratorId), (frame) => {
       if (frame.event === "error") {
         const status = frame.data && frame.data.status;
         if (status === 401 || status === 403) {
           showAuthBanner();
         } else {
           timelineEl.textContent = "[error] " + frame.data.message + "\n";
         }
         return;
       }
       if (seatSource !== "live") {
         return; // history mode is showing fetched pages; drop live frames silently (model.go:269)
       }
       const seat = frame.data;
       seats[seat.task_id] = seat;
       renderSeats();
     });
   }
   ```
   `openOrchestrator` references `updateModeButtons()` (Task 7) and `renderSeats()` (Task 8) — both
   are hoisted `function` declarations inside the same `init()` scope, so forward-reference order is
   fine regardless of which task's edit lands first in the file; land Tasks 6–8 in the same working
   session before running the Task 9 regression test.

4. Replace `loadOrchestrators` (`app.js:363-387`):
   ```js
   async function loadOrchestrators() {
     if (stopOrchestratorStream) {
       stopOrchestratorStream();
       stopOrchestratorStream = null;
     }
     const response = await fetch("/orchestrators", { headers: authHeaders() });
     if (response.status === 401 || response.status === 403) {
       showAuthBanner();
       orchestratorSelect.replaceChildren();
       seatsEl.replaceChildren();
       timelineEl.textContent = "";
       return;
     }
     hideAuthBanner();
     if (!response.ok) {
       orchestratorSelect.replaceChildren();
       seatsEl.replaceChildren();
       timelineEl.textContent = "[error] /orchestrators " + response.status + "\n";
       return;
     }
     const payload = await response.json();
     orchestratorSelect.replaceChildren(
       ...(payload.orchestrators || []).map((id) => {
         const option = document.createElement("option");
         option.value = id;
         option.textContent = id;
         return option;
       })
     );
     modeLiveButton.disabled = !orchestratorSelect.value;
     modeHistoryButton.disabled = !orchestratorSelect.value;
     if (orchestratorSelect.value) {
       openOrchestrator(orchestratorSelect.value);
     }
   }
   ```

5. Replace `selectSeat` (`app.js:321-338`) — only the error branch changes (banner vs. inline text):
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
       appendTimelineFrame(timelineEl, frame);
     });
     renderSeats();
   }
   ```

6. No isolated red/green here (see note above) — proceed to Task 7, then verify both together via
   Task 9.

7. Commit (fold into one commit with Task 7 and Task 8's wiring, or commit now with a clear message
   if working incrementally): `feat(arb-visibility-web): auth banner + live-path rewrites (loadOrchestrators/openOrchestrator/startOrchestratorStream/selectSeat)`

---

## Task 7 — History state machine: `setSeatSource` / `fetchHistoryPage` / `applyHistoryPage` / scroll listener / age-tick timer

**Files:** `src/arb_memory/static/app.js` (`init()`, continuing from Task 6)

**Interfaces:**
```js
function setSeatSource(nextSource: "live" | "history"): void
function updateModeButtons(): void
function fetchHistoryPage({ append: boolean }): void
function applyHistoryPage(payload: { seats: object[], next_cursor: string | null, has_more: boolean }, append: boolean): void
```

**Steps** (no isolated automated test — same rationale as Task 6; verified collectively by Task 9's
harness and the Task 13 manual check)

1. Add `updateModeButtons` and the mode-toggle click listeners, inside `init()`:
   ```js
   function updateModeButtons() {
     modeLiveButton.setAttribute("aria-pressed", seatSource === "live" ? "true" : "false");
     modeHistoryButton.setAttribute("aria-pressed", seatSource === "history" ? "true" : "false");
   }

   modeLiveButton.addEventListener("click", () => setSeatSource("live"));
   modeHistoryButton.addEventListener("click", () => setSeatSource("history"));
   ```

2. Add `setSeatSource`:
   ```js
   function setSeatSource(nextSource) {
     if (nextSource === seatSource) {
       return; // clicking the already-active segment is a no-op
     }
     historyGen += 1;
     if (stopSeatStream) {
       stopSeatStream();
       stopSeatStream = null;
     }
     selectedTaskId = "";
     Object.keys(seats).forEach((key) => delete seats[key]);
     timelineEl.textContent = "";

     seatSource = nextSource;
     historyCursor = null;
     historyHasMore = false;
     historyStatusText = "";
     updateModeButtons();

     if (nextSource === "history") {
       historyLoading = true;
       renderSeats();
       fetchHistoryPage({ append: false });
       return;
     }

     historyLoading = false;
     renderSeats();
     if (stopOrchestratorStream) {
       stopOrchestratorStream();
     }
     stopOrchestratorStream = startOrchestratorStream(selectedOrchestratorId);
   }
   ```

3. Add `fetchHistoryPage` and `applyHistoryPage`:
   ```js
   function fetchHistoryPage({ append }) {
     const gen = historyGen;
     const cursorParam = append && historyCursor ? "?cursor=" + encodeURIComponent(historyCursor) : "";
     const url = "/orchestrators/" + encodeURIComponent(selectedOrchestratorId) + "/seats/history" + cursorParam;
     historyLoading = true;
     renderSeats();
     fetch(url, { headers: authHeaders() })
       .then((response) => {
         if (isStaleHistoryGen(gen, historyGen)) {
           return null; // mode/orchestrator changed since this fetch was issued — discard
         }
         if (response.status === 401 || response.status === 403) {
           showAuthBanner();
           historyLoading = false;
           historyStatusText = "";
           renderSeats();
           return null;
         }
         if (!response.ok) {
           historyLoading = false;
           historyStatusText = "history unavailable";
           renderSeats();
           return null;
         }
         return response.json();
       })
       .then((payload) => {
         if (payload === null || isStaleHistoryGen(gen, historyGen)) {
           return; // discard: either already handled above, or went stale during the .json() await
         }
         applyHistoryPage(payload, append);
       })
       .catch(() => {
         if (isStaleHistoryGen(gen, historyGen)) {
           return;
         }
         historyLoading = false;
         historyStatusText = "history unavailable";
         renderSeats();
       });
   }

   function applyHistoryPage(payload, append) {
     historyLoading = false;
     historyStatusText = "";
     const rows = Array.isArray(payload.seats) ? payload.seats : [];
     if (!append) {
       Object.keys(seats).forEach((key) => delete seats[key]);
     }
     for (const seat of rows) {
       if (append && Object.prototype.hasOwnProperty.call(seats, seat.task_id)) {
         continue; // de-dup: matches Go's appendHistoryPage skip-if-exists (model.go:894-899)
       }
       seats[seat.task_id] = seat;
     }
     historyCursor = payload.next_cursor || null;
     historyHasMore = Boolean(payload.has_more);
     renderSeats();
     if (
       shouldAutoFetchHistoryPage({
         seatSource,
         historyHasMore,
         historyLoading,
         historyCursor,
         scrollHeight: seatPanelEl.scrollHeight,
         clientHeight: seatPanelEl.clientHeight,
       })
     ) {
       fetchHistoryPage({ append: true });
     }
   }
   ```
   `limit` is never sent — both this client and the Go client rely on the backend's default clamp
   of 50 (`_clamp_history_limit`, `visibility.py:205-210`).

4. Add the scroll listener and the age re-render timer, near the end of `init()` (before the final
   `if (tokenInput.value) { loadOrchestrators(); }` line):
   ```js
   const HISTORY_SCROLL_THRESHOLD_PX = 40;
   seatPanelEl.addEventListener("scroll", () => {
     if (seatSource !== "history" || !historyHasMore || historyLoading || !historyCursor) {
       return;
     }
     if (isScrolledNearBottom(seatPanelEl.scrollTop, seatPanelEl.clientHeight, seatPanelEl.scrollHeight, HISTORY_SCROLL_THRESHOLD_PX)) {
       fetchHistoryPage({ append: true });
     }
   });

   const AGE_TICK_MS = 2000; // matches the Go sibling's 2s tick (tickCmd, model.go:1146-1150)
   setInterval(() => {
     renderSeats();
   }, AGE_TICK_MS);
   ```
   The scroll listener's early-return also checks `!historyCursor` (spec post-spec-panel remediation
   item 7's second call site), matching `shouldAutoFetchHistoryPage`'s own guard.

5. No isolated red/green — proceed to Task 8, then Task 9's regression test exercises this together
   with Task 6's wiring.

6. Commit: `feat(arb-visibility-web): live/history state machine (setSeatSource, keyset-paginated fetchHistoryPage/applyHistoryPage, scroll + viewport-fill triggers, age re-render tick)`

---

## Task 8 — `renderSeats()` rewrite: age column, state-cell/badge/pulse-dot, history-status line

**Files:** `src/arb_memory/static/app.js` (`renderSeats`, `app.js:295-319` today)

**Interfaces:**
```js
function renderSeats(): void
function historyStatusLine(): string
```

**Steps** (no isolated automated test in this task — verified by Task 9's harness, which asserts on
`renderSeats()`'s DOM output directly, and the Task 13 manual check)

1. Replace `renderSeats` (`app.js:295-319`) with:
   ```js
   function renderSeats() {
     const orderedSeats = Object.values(seats).sort((a, b) => {
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
   }

   function historyStatusLine() {
     if (seatSource !== "history") {
       return "";
     }
     if (historyLoading) {
       return "· history — loading…";
     }
     if (historyStatusText) {
       return "· history — " + historyStatusText;
     }
     return "· history";
   }
   ```
   `voted`/`stance` rendering is live-only in practice — `_history_row_to_seat`
   (`visibility.py:255-266`) never sets those fields, so the branch is structurally unreachable for
   a history-mode row; this is not a parity bug to chase.

2. No isolated red/green — this is the last of the three `init()`-wiring tasks; run the full
   existing test file once now purely as a non-regression check (all tests 1–11 from Tasks 1–5
   should still be green, since nothing exported changed):
   ```bash
   PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
   ```

3. Commit: `feat(arb-visibility-web): renderSeats age column + state-cell/badge/pulse-dot + history-status line`

---

## Task 9 — `Clear` full reset + regression test (the load-bearing DOM-harness test)

**Files:** `src/arb_memory/static/app.js` (`clearToken`'s click listener, `app.js:286-293` today),
`tests/arb_memory/test_visibility_web_contract.py`

**Interfaces:** no new function — `clearToken`'s existing click listener gains behavior.

This is the one task in this plan that needs a full DOM/fetch/stream mock, because it's the only
place the brief requires proof that a **pre-existing runtime bug involving asynchronous stream
delivery** is actually closed, not just that new fields get zeroed. The harness built here is
deliberately minimal — just enough surface for `init()` to run without throwing and for one live
SSE stream to be driven under test control.

**Steps**

1. Add the shared DOM-harness JS as a new Python constant near the top of the test file (below
   `_FLUSH_JS` from Task 1):
   ```python
   _DOM_HARNESS_JS = """
   class FakeClassList {
     constructor() { this._set = new Set(); }
     add(c) { this._set.add(c); }
     remove(c) { this._set.delete(c); }
     toggle(c, force) {
       if (force === true) { this._set.add(c); return true; }
       if (force === false) { this._set.delete(c); return false; }
       if (this._set.has(c)) { this._set.delete(c); return false; }
       this._set.add(c); return true;
     }
     contains(c) { return this._set.has(c); }
   }

   class FakeElement {
     constructor(tag) {
       this.tagName = (tag || "div").toUpperCase();
       this._listeners = {};
       this._attrs = {};
       this.dataset = {};
       this.classList = new FakeClassList();
       this.children = [];
       this.textContent = "";
       this.value = "";
       this.hidden = false;
       this.disabled = false;
       this.scrollTop = 0;
       this.scrollHeight = 0;
       this.clientHeight = 0;
     }
     addEventListener(type, fn) {
       (this._listeners[type] = this._listeners[type] || []).push(fn);
     }
     dispatchEvent(type, evt) {
       (this._listeners[type] || []).forEach((fn) => fn(evt || {}));
     }
     setAttribute(name, value) { this._attrs[name] = String(value); }
     getAttribute(name) { return this._attrs[name]; }
     appendChild(child) { this.children.push(child); return child; }
     replaceChildren(...nodes) {
       this.children = nodes;
       if (nodes.length && typeof nodes[0].value === "string") {
         this.value = nodes[0].value;
       } else if (!nodes.length) {
         this.value = "";
       }
     }
     set innerHTML(html) {
       // Minimal parser: the only innerHTML producer in app.js is renderSeats()'s fixed array of
       // `<span class="...">` fragments. Extract class names in order and materialize one child
       // FakeElement per span so button.querySelector(".foo") below can find them.
       this.children = [];
       const re = /<span class="([^"]+)"[^>]*>/g;
       let match;
       while ((match = re.exec(html))) {
         const child = new FakeElement("span");
         child.classList.add(match[1]);
         this.children.push(child);
       }
     }
     querySelector(selector) {
       const cls = selector.replace(".", "");
       const direct = this.children.find((c) => c.classList.contains(cls));
       if (direct) return direct;
       for (const child of this.children) {
         const nested = child.querySelector ? child.querySelector(selector) : null;
         if (nested) return nested;
       }
       return null;
     }
   }

   function flush(times) {
     return new Promise((resolve) => {
       let remaining = times;
       function step() {
         if (remaining <= 0) { resolve(); return; }
         remaining -= 1;
         setImmediate(step);
       }
       step();
     });
   }

   function makeDom() {
     const elements = {};
     const get = (id) => elements[id] || (elements[id] = new FakeElement());
     let domReadyCallback = null;
     global.document = {
       getElementById: get,
       createElement: (tag) => new FakeElement(tag),
       addEventListener: (type, fn) => {
         if (type === "DOMContentLoaded") domReadyCallback = fn;
       },
     };
     global.window = {};
     global.localStorage = {
       _store: { token: "test-token-123" },
       getItem(key) {
         return Object.prototype.hasOwnProperty.call(this._store, key) ? this._store[key] : null;
       },
       setItem(key, value) { this._store[key] = value; },
       removeItem(key) { delete this._store[key]; },
       token: "",
     };
     global.setInterval = () => 0; // age-tick timer would otherwise keep the Node process alive
     return { elements, get, ready: () => domReadyCallback };
   }

   function makeSseResponse(signal) {
     let resolveNext = null;
     let rejectNext = null;
     const pending = [];
     signal.addEventListener("abort", () => {
       if (rejectNext) {
         const rj = rejectNext;
         resolveNext = null;
         rejectNext = null;
         const err = new Error("aborted");
         err.name = "AbortError";
         rj(err);
       }
     });
     const encoder = new TextEncoder();
     return {
       ok: true,
       status: 200,
       body: {
         getReader() {
           return {
             read() {
               return new Promise((resolve, reject) => {
                 if (pending.length) {
                   resolve(pending.shift());
                 } else {
                   resolveNext = resolve;
                   rejectNext = reject;
                 }
               });
             },
           };
         },
       },
       push(text) {
         const chunk = { done: false, value: encoder.encode(text) };
         if (resolveNext) {
           const r = resolveNext;
           resolveNext = null;
           rejectNext = null;
           r(chunk);
         } else {
           pending.push(chunk);
         }
       },
     };
   }
   """
   ```

2. Write the failing regression test, appended to the test file:
   ```python
   def test_appjs_clear_token_stops_streams_and_prevents_repopulation():
       node = _node()
       if node is None:
           raise AssertionError("node is required for the Clear full-reset regression test")
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
           const ready = dom.ready();
           ready();

           (async () => {{
             await flush(10); // let loadOrchestrators()/openOrchestrator()/streamSSE's connect() settle
             const seatsBefore1 = dom.get("seats").children.length;

             orchestratorSse.push(
               "id: 1-0\\nevent: seat_appear\\ndata: " +
               JSON.stringify({{
                 task_id: "seat-a", seat_id: "codex-1", run_id: "run-1",
                 state: "running", last_event: "task_started",
                 last_event_ts: new Date().toISOString(),
               }}) + "\\n\\n"
             );
             await flush(10);
             const seatsAfterFrame = dom.get("seats").children.length;

             dom.get("clear-token")._listeners.click[0]();
             const seatsRightAfterClear = dom.get("seats").children.length;

             orchestratorSse.push(
               "id: 2-0\\nevent: seat_appear\\ndata: " +
               JSON.stringify({{
                 task_id: "seat-b", seat_id: "codex-2", run_id: "run-2",
                 state: "running", last_event: "task_started",
                 last_event_ts: new Date().toISOString(),
               }}) + "\\n\\n"
             );
             await flush(10);
             const seatsAfterPostClearFrame = dom.get("seats").children.length;

             console.log(JSON.stringify({{
               seatsBefore1, seatsAfterFrame, seatsRightAfterClear, seatsAfterPostClearFrame,
             }}));
           }})();
           """
       )
       completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
       result = json.loads(completed.stdout)
       assert result["seatsBefore1"] == 0
       assert result["seatsAfterFrame"] == 1
       assert result["seatsRightAfterClear"] == 0
       assert result["seatsAfterPostClearFrame"] == 0
   ```
   The last assertion is the actual regression proof: against today's `clearToken` handler (which
   never calls `stopOrchestratorStream()`), the already-open SSE connection's `abortController` is
   never aborted, so the pending `reader.read()` — parked waiting for the next chunk — is still
   live; pushing a frame after Clear resolves that pending read and `startOrchestratorStream`'s
   callback writes it straight into `seats`, making `seatsAfterPostClearFrame` come back `1`, not
   `0`. Against the fixed handler, `stopOrchestratorStream()` calls `abortController.abort()`
   synchronously, which (via the `signal.addEventListener("abort", ...)` wiring in
   `makeSseResponse`, mirroring real Fetch-API abort semantics) rejects that pending read with an
   `AbortError` — `streamSSE`'s existing `if (stopped || err.name === "AbortError") { return; }`
   branch fires, `connect()` exits for good, and the later `.push()` has nothing left listening to
   consume it.

3. Run red:
   ```bash
   PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q -k clear_token
   ```
   Fails at `seatsAfterPostClearFrame == 0` (comes back `1`) against the current `clearToken`
   handler, which only clears `localStorage`/DOM and never calls `stopOrchestratorStream`.

4. Replace `clearToken`'s click listener (`app.js:286-293`) with the full reset, in the exact order
   from Global Constraint 9:
   ```js
   clearToken.addEventListener("click", () => {
     if (stopOrchestratorStream) {
       stopOrchestratorStream();
       stopOrchestratorStream = null;
     }
     if (stopSeatStream) {
       stopSeatStream();
       stopSeatStream = null;
     }
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
     hideAuthBanner();
   });
   ```
   (`updateModeButtons()` is added here beyond the spec's literal field list — resetting
   `seatSource` to `"live"` without also updating the buttons' `aria-pressed` state would leave a
   stale "History" segment visually selected after a Clear performed while in history mode; this is
   a small, low-risk consistency fix following the same pattern `setSeatSource`/`openOrchestrator`
   already use whenever `seatSource` changes.)

5. Run green:
   ```bash
   PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
   ```
   All 12 tests so far (1–4 existing, 5 new pure-function/export tests from Tasks 1–4, the guard
   comment test from Task 5, and this Clear regression test) pass. This is also the first point in
   this plan where Tasks 6–8's `init()` wiring gets exercised end-to-end — if any DOM reference or
   function reference from those tasks were missing or misnamed, this test fails with a real
   `ReferenceError`/`TypeError` rather than silently.

6. Commit: `fix(arb-visibility-web): Clear stops both SSE streams and resets all history/mode state, closing the "Clear doesn't disconnect anything" bug`

---

## Task 10 — `index.html`: body structure (mode toggle, auth banner, seat-panel id, history-status)

**Files:** `src/arb_memory/static/index.html`, `tests/arb_memory/test_visibility_web_contract.py`

**Steps**

1. Write the failing tests (plain Python string assertions — no Node needed, this is markup, not
   JS):
   ```python
   INDEX_HTML = ROOT / "src" / "arb_memory" / "static" / "index.html"


   def test_index_html_head_gets_the_door_page_font_link_pair():
       html = INDEX_HTML.read_text()
       assert '<link rel="preconnect" href="https://fonts.googleapis.com">' in html
       assert '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' in html
       assert (
           '<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;'
           '6..72,500;6..72,600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">'
       ) in html


   def test_index_html_body_has_mode_toggle_auth_banner_and_history_status():
       html = INDEX_HTML.read_text()
       assert "<h1>ARB · Visibility</h1>" in html
       assert '<div class="segmented" role="group" aria-label="Seat source">' in html
       assert '<button id="mode-live" type="button" aria-pressed="true" disabled>Live</button>' in html
       assert '<button id="mode-history" type="button" aria-pressed="false" disabled>History</button>' in html
       assert '<div id="auth-banner" hidden role="alert">' in html
       assert 'Unauthorized — check your token and try again.' in html
       assert '<aside id="seat-panel">' in html
       assert '<div id="history-status" aria-live="polite"></div>' in html
   ```

2. Run red: current `index.html` has none of the new markup (`h1` still says just "ARB
   Visibility", no mode toggle, no auth banner, no font links, `aside` has no `id`).

3. Replace `index.html`'s `<head>` (add font links after the existing `<meta viewport>` tag, before
   `<title>` or `<style>` — either position is fine since order between `<link>`/`<title>` doesn't
   matter) and the full `<body>` (`index.html:120-143` today):
   ```html
   <link rel="preconnect" href="https://fonts.googleapis.com">
   <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
   <link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
   ```
   ```html
   <body>
     <header>
       <h1>ARB · Visibility</h1>
       <label>
         Token
         <input id="token" type="password" autocomplete="off">
       </label>
       <label>
         Orchestrator
         <select id="orchestrator"></select>
       </label>
       <div class="segmented" role="group" aria-label="Seat source">
         <button id="mode-live" type="button" aria-pressed="true" disabled>Live</button>
         <button id="mode-history" type="button" aria-pressed="false" disabled>History</button>
       </div>
       <button id="clear-token" type="button">Clear</button>
     </header>
     <div id="auth-banner" hidden role="alert">Unauthorized — check your token and try again.</div>
     <main id="app">
       <aside id="seat-panel">
         <ul id="seats"></ul>
         <div id="history-status" aria-live="polite"></div>
       </aside>
       <section>
         <pre id="timeline"></pre>
       </section>
     </main>
     <script src="/app.js"></script>
   </body>
   ```
   The mode toggle uses `role="group"` + `aria-pressed` toggle buttons, not `role="tablist"` — there
   is exactly one panel (the seat list) whose data source changes, which the toggle-button-group
   pattern matches; `tablist`/`aria-selected` would misrepresent it. `#seat-panel`'s `id` moves onto
   the `<aside>` (the scrolling container, `overflow:auto` per Task 11's CSS), not onto `<ul
   id="seats">` — it's the scroll-listener/viewport-fill target from Task 7.

4. Run green.

5. Commit: `feat(arb-visibility-web): door-page fonts + mode-toggle/auth-banner/seat-panel/history-status markup`

---

## Task 11 — CSS restyle (tokens, base, header, inputs, buttons/segmented, sidebar/badges, timeline, auth-banner, responsive)

**Files:** `src/arb_memory/static/index.html` (`<style>` block, lines 7–118 today), `tests/arb_memory/test_visibility_web_contract.py`

This is declarative CSS with no logic branches, so it's authored as one task per the brief's
allowance, but still needs a concrete, automated verification step (there's no visual-regression
tooling in this repo) — a Python test asserting the stylesheet contains the exact tokens/selectors
pinned by the spec.

**Steps**

1. Write the failing tests:
   ```python
   def test_index_html_css_root_tokens_light_and_dark():
       html = INDEX_HTML.read_text()
       assert "--paper:#FAF8F4; --paper-sunk:#F3EFE8; --card:#FFFFFF;" in html
       assert "--ink-900:#1F1D1A; --ink-700:#45413B; --ink-500:#6E6960; --ink-400:#918B80;" in html
       assert "--clay-600:#9E4A2E; --clay-700:#823A22; --clay-100:#EFE0D6;" in html
       assert "@media (prefers-color-scheme: dark)" in html
       assert "--ink-500:#A8A299;" in html  # corrected dark-mode WCAG-AA value, NOT #918B80
       assert "--paper:#1F1D1A; --paper-sunk:#28251F; --card:#322E27;" in html
       assert "--clay-600:#C97350; --clay-700:#E0916B; --clay-100:#3D2A20;" in html
       assert "color-scheme: light dark" not in html  # dropped per the design's fixed-theme decision


   def test_index_html_css_key_selectors_present():
       html = INDEX_HTML.read_text()
       for selector in (
           ".segmented", ".segmented button", '.segmented button[aria-pressed="true"]',
           "#auth-banner", "#auth-banner[hidden]",
           ".state-cell", ".badge", '.badge[data-state="stale"]', '.badge[data-state="failed"]',
           ".pulse-dot", "@keyframes seat-pulse", "@media (prefers-reduced-motion: reduce)",
           "#history-status", "#history-status:empty",
           "grid-template-columns: minmax(140px,1fr) minmax(220px,2fr) auto auto auto",
       ):
           assert selector in html, f"missing selector/rule: {selector!r}"
   ```

2. Run red: none of the new selectors exist; the current stylesheet still uses `Canvas`/
   `CanvasText`/`Highlight` system colors and `color-scheme: light dark`.

3. Replace `index.html`'s entire `<style>` block (`index.html:7-118` today) with:
   ```css
   :root{
     --paper:#FAF8F4; --paper-sunk:#F3EFE8; --card:#FFFFFF;
     --ink-900:#1F1D1A; --ink-700:#45413B; --ink-500:#6E6960; --ink-400:#918B80;
     --line-200:#E7E2D8; --line-300:#D9D3C7;
     --clay-600:#9E4A2E; --clay-700:#823A22; --clay-100:#EFE0D6;
     --serif:'Newsreader',Georgia,'Times New Roman',serif;
     --mono:'IBM Plex Mono',ui-monospace,'SF Mono',Menlo,monospace;
     --ease:cubic-bezier(.4,0,.2,1);
   }
   @media (prefers-color-scheme: dark) {
     :root{
       --paper:#1F1D1A; --paper-sunk:#28251F; --card:#322E27;
       --ink-900:#FAF8F4; --ink-700:#D9D3C7; --ink-500:#A8A299; --ink-400:#6E6960;
       --line-200:#3A362E; --line-300:#4A453B;
       --clay-600:#C97350; --clay-700:#E0916B; --clay-100:#3D2A20;
     }
   }
   *{box-sizing:border-box}
   html,body{margin:0}
   body{
     background:var(--paper); color:var(--ink-700); font-family:var(--serif);
     -webkit-font-smoothing:antialiased; line-height:1.4;
   }
   header{
     align-items:end; background:var(--paper-sunk); border-bottom:1px solid var(--line-200);
     display:grid; gap:12px;
     grid-template-columns: minmax(140px,1fr) minmax(220px,2fr) auto auto auto;
     padding:14px 18px;
   }
   h1{
     font-family:var(--serif); font-weight:600; font-size:1.25rem; color:var(--ink-900);
     letter-spacing:-.01em; margin:0;
   }
   label{
     font-family:var(--mono); font-size:.6875rem; text-transform:uppercase;
     letter-spacing:.1em; color:var(--ink-500); display:grid; gap:4px;
   }
   input#token, select#orchestrator{
     font-family:var(--mono); font-size:.95rem; color:var(--ink-900);
     background:var(--paper-sunk); border:1px solid var(--line-300);
     border-radius:5px; padding:11px 13px; width:100%;
     transition:border-color 120ms var(--ease), background-color 120ms var(--ease), box-shadow 120ms var(--ease);
   }
   input#token:focus, select#orchestrator:focus{
     outline:none; border-color:var(--clay-600); background:var(--card);
     box-shadow:0 0 0 3px var(--clay-100);
   }
   button{
     font-family:var(--mono); font-size:.8125rem; text-transform:uppercase;
     letter-spacing:.1em; font-weight:500; border-radius:5px; padding:11px 14px;
     cursor:pointer; background:var(--card); border:1px solid var(--line-300); color:var(--ink-700);
     transition:background-color 120ms var(--ease), border-color 120ms var(--ease);
   }
   button:hover{background:var(--paper-sunk)}
   button:focus-visible{outline:2px solid var(--clay-600); outline-offset:2px}
   button:disabled{opacity:.5; cursor:not-allowed}

   .segmented{display:inline-flex; border:1px solid var(--line-300); border-radius:5px; overflow:hidden}
   .segmented button{border:0; border-radius:0; padding:11px 14px}
   .segmented button + button{border-left:1px solid var(--line-300)}
   .segmented button[aria-pressed="true"]{
     background:var(--clay-100); border-color:var(--clay-600); color:var(--clay-700);
   }

   main{ display:grid; grid-template-columns:minmax(260px,34%) 1fr; min-height:calc(100vh - 73px); }
   aside#seat-panel{
     background:var(--card); border-right:1px solid var(--line-200); overflow:auto;
     border-radius:8px;
   }
   #seats{ list-style:none; margin:0; padding:0; }
   #seats li{ border-bottom:1px solid var(--line-200); }
   #seats button{
     background:transparent; border:0; border-radius:0; color:inherit; cursor:pointer;
     display:grid; grid-template-columns:1fr auto auto; column-gap:8px; row-gap:4px;
     padding:12px 14px; text-align:left; width:100%;
   }
   #seats button[aria-current="true"]{ background:var(--paper-sunk); }

   .seat-id{ font-family:var(--mono); font-weight:500; color:var(--ink-900); }
   .run-id{ font-family:var(--mono); color:var(--ink-500); font-size:.75rem; }
   .last-event{ font-family:var(--mono); color:var(--ink-500); font-size:.75rem; grid-column: 2 / span 2; }
   .age{ font-family:var(--mono); color:var(--ink-500); font-size:.75rem; text-align:right; }

   #history-status{
     font-family:var(--mono); font-size:.6875rem; text-transform:uppercase; letter-spacing:.06em;
     color:var(--ink-500); padding:10px 14px; border-top:1px solid var(--line-200);
   }
   #history-status:empty{ display:none; }

   .state-cell{ display:inline-flex; align-items:center; gap:5px; }
   .badge{
     font-family:var(--mono); font-size:.6875rem; text-transform:uppercase; letter-spacing:.04em;
     border:1px solid var(--ink-500); color:var(--ink-700); background:var(--paper-sunk);
     border-radius:999px; padding:1px 8px;
   }
   .badge[data-state="stale"]{ color:var(--ink-400); }
   .badge[data-state="failed"]{ border-color:var(--clay-700); color:var(--clay-700); }

   .pulse-dot{ width:6px; height:6px; border-radius:50%; background:var(--clay-600); display:none; }
   .state-cell.is-running .pulse-dot{
     display:inline-block; animation:seat-pulse 1.6s var(--ease) infinite;
   }
   @keyframes seat-pulse{ 0%,100%{opacity:1} 50%{opacity:.25} }
   @media (prefers-reduced-motion: reduce){
     .state-cell.is-running .pulse-dot{ animation:none; opacity:1; }
   }

   section{ min-width:0; background:var(--paper); border-radius:8px; }
   #timeline{
     margin:0; min-height:calc(100vh - 73px); overflow:auto; padding:14px 18px;
     white-space:pre-wrap; font-family:var(--mono); color:var(--ink-700);
   }
   #timeline details summary{ color:var(--ink-500); cursor:pointer; }

   #auth-banner{
     background:var(--clay-100); border-bottom:1px solid var(--clay-600); color:var(--clay-700);
     font-family:var(--mono); text-transform:uppercase; letter-spacing:.06em; font-size:.75rem;
     padding:10px 18px;
   }
   #auth-banner[hidden]{ display:none; }

   @media (max-width: 720px) {
     header, main { grid-template-columns: 1fr; }
     aside#seat-panel { border-right:0; max-height:42vh; }
   }
   ```
   Same `720px` breakpoint and stacking behavior as today — restyle only, no layout-behavior change.

4. Run green:
   ```bash
   PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
   ```

5. Commit: `style(arb-visibility-web): restyle to the door-page token set (light + corrected-contrast dark mode), no layout behavior change`

---

## Task 12 — Full regression run

**Files:** none (verification only)

**Steps**

1. Run the full contract test file once more as a final gate before the manual check:
   ```bash
   PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
   ```
   Expect all tests green: the 4 pre-existing tests (untouched), plus every new test added across
   Tasks 1–5, 9, 10, 11 (streamSSE 4xx/5xx, UTC formatter, history-gen/scroll/viewport-fill guards,
   exports surface, guard-comment text, Clear regression, HTML markup, CSS tokens/selectors).

2. Also run the broader gateway test suite once, to confirm nothing in `visibility.py`'s own test
   file was disturbed (it shouldn't be — this plan never touches `visibility.py`):
   ```bash
   PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/ -q
   ```

3. No commit — this task is a verification checkpoint, not a code change.

---

## Task 13 — Manual browser verification (required, not automated)

**Files:** none (manual QA only)

There is no browser-testing tooling in this repo (per the design's and spec's own Risks/Testing
sections), so this step cannot be scripted — it is the "real browser check" both documents insist
on and could not perform themselves (their diagnosis was curl/static-reading only). Do not skip
this and assume the restyle/JS "looks right" is sufficient; a genuine pre-existing bug
(`streamSSE`'s retry-forever behavior) was already found by static reading alone in this same
feature, which is a reason for care here, not a reason to skip the live check.

Against the local dev container (`http://localhost:8810`, already running):

1. Load the page fresh with empty `localStorage` — the restyle renders correctly with no token
   entered (empty state, no console errors, door-page tokens visible: paper background, serif
   `<h1>`, mono labels).
2. Enter a valid Bearer token → orchestrator dropdown populates; mode-toggle buttons become enabled.
3. Select an orchestrator → seat sidebar populates from the live SSE stream; age column shows
   relative ages ticking up every ~2s without a page refresh.
4. Click a seat → transcript pane streams; tool calls/patches/thinking blocks render with the new
   mono/serif typography.
5. Toggle to History → sidebar clears, shows "· history — loading…", then populates from the
   history endpoint; `dd/mm` dates show for any seat older than 24h (note in the follow-up report if
   the available orchestrator's history is all recent and this branch couldn't be exercised).
6. Scroll to the bottom of the history list (with `has_more: true`) → next page loads and appends
   without duplicating or losing rows. If the first page doesn't fill the viewport, confirm the
   viewport-fill fallback auto-loads additional pages without any scroll input.
7. Toggle back to Live → sidebar clears and repopulates from a fresh SSE backfill; confirm it keeps
   updating as new events arrive (not frozen).
8. Enter a deliberately wrong token → the auth banner appears (not a silent JSON dump in the
   timeline). Click Clear, then re-enter a valid token → banner clears, page recovers, and a
   previously-open stream does not silently repopulate stale data (the Task 9 regression, confirmed
   live).
9. Check the browser console/network tab throughout steps 1–8 for any client-side error the
   curl-only diagnosis couldn't have surfaced. If one turns up, treat it as a fix within this same
   feature's scope (per the design's own framing), not a surprise requiring a new design round.

Report the outcome of this checklist (pass/fail per step, and any bug found) before considering this
feature done — a plan step, not an assumption.

---

## Warm-orchestrator remediation (post plan-panel)

Certifying quorum for this round: codex (**FIX_BEFORE_BUILD**) and pi-GLM (**BUILD_READY_WITH_NITS**,
but its "nits" section contains a confirmed P0 — see below). **agy-print is a named absent vote**:
two dispatch attempts both failed (first attempt completed a real review but the bridge's
completion-gate flagged uncommitted scratch files left in the repo from its own verification work,
cleaned up by the orchestrator; second attempt failed with a genuine engine-level timeout). Per
the "don't silently shrink the panel" discipline, this is flagged explicitly rather than silently
treated as a full quorum. Cold-Opus contributed as a non-certifying reviewer (**FIX_BEFORE_BUILD**),
same quorum-swap reason as the design/spec panels (this plan's author is a cold-Sonnet subagent,
same Claude lineage as cold-Opus).

Despite the thin certifying count, the findings below are treated as decisive: **both P0s were
independently found by all three responding reviewers** (codex, pi-GLM, cold-Opus — one certifying
pair plus the non-certifying contributor, three fully independent read-throughs), **and two of the
three verified them by directly executing the plan's own test code**, not just reading it. I then
independently re-verified both myself by running the exact code in question and observing the
failure — this is about as strong a confirmation as a finding can get before the actual TDD
implementation happens. **This section is authoritative where it differs from the tasks above.**

### DECIDED — must apply before/during TDD implementation

1. **Task 9's `_DOM_HARNESS_JS` `FakeElement.innerHTML` parser must build a nested tree, not a flat
   sibling list.** All three reviewers found this independently; I ran it myself against the exact
   plan HTML and confirmed: `stateCell.children.length === 0` and
   `stateCell.querySelector(".badge")` returns `null`, because the current regex
   (`/<span class="([^"]+)"[^>]*>/g`) matches every `<span class="...">` in the string — including
   the ones nested inside `state-cell` — and pushes them all as flat siblings of `button`, not as
   children of `state-cell`. Task 8's real `renderSeats()` code does
   `stateCell.querySelector(".badge").textContent = ...`, which throws `TypeError` the moment the
   harness renders a single seat. This crashes the flagship regression test — the one proving the
   real, pre-existing `Clear`-doesn't-disconnect bug is fixed — before it ever reaches its own
   assertions, in BOTH the red and green runs. **This is a test-harness bug, not a product-code
   bug** — a real browser's `innerHTML` nests correctly; only the fake parser is unfaithful.

   Fix: replace the `set innerHTML(html)` method in `_DOM_HARNESS_JS`'s `FakeElement` class with a
   stack-based tokenizer that tracks open/close tags (verified working, byte-for-byte, by running it
   against the exact Task 8 markup array and confirming `stateCell.querySelector(".badge")` finds
   the badge, sets its `textContent`, and the flat top-level siblings — `seat-id`, `age`, `run-id`,
   `last-event` — remain directly reachable from `button` as before):
   ```js
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
   ```
   Add a positive assertion inside the Task 9 test (before the Clear-specific assertions) that
   `dom.get("seats").children[0]` actually has a discoverable `.badge` with the seat's real state
   text — so a future harness regression of this exact kind fails loud immediately, not by chasing
   an unrelated-looking assertion failure three steps later.

2. **Tasks 1's tests 5 and 6 (and the plan's own described "red" step) must not hang.** All three
   reviewers found this; I independently reproduced it by running the plan's exact test code
   (`global.setTimeout = (fn) => setImmediate(fn)`, no `stop()` call, no Python-side timeout)
   against both the current (broken) `streamSSE` and a scratch 5xx-retry scenario — in both cases
   the Node subprocess printed its `console.log` output correctly but **never exited on its own**
   (confirmed by watching the process stay alive for 15+ seconds and force-killing it — exit code
   137). Mechanism: the broken/non-4xx retry loop keeps rescheduling itself via the overridden
   `setTimeout` → `setImmediate` forever; a live `setImmediate` callback keeps Node's event loop
   permanently non-empty, so the process never terminates on its own even after `console.log` runs.
   `subprocess.run([node, "-e", script], check=True, capture_output=True)` has no `timeout=`
   parameter (matching the existing harness's pattern, which never needed one before this task) —
   this means Task 1's own prescribed "run red" step (against today's genuinely-broken code) hangs
   pytest indefinitely, not "fails the assertion" as the plan's Step 4 describes. **Task 12's final
   full-suite run would hang forever** if test 6 (5xx-keeps-retrying) doesn't explicitly stop its
   stream, since a 5xx is never fatal even in the FIXED implementation — this is not just a red-step
   problem, it's a green-step problem for test 6 specifically.

   Fix, two parts:
   - In test 6's script (5xx-keeps-retrying), capture `const stop = streamSSE(url, onEvent);`, and
     after the `flush(20)` window (but before the final `console.log`), call `stop();` — this lets
     the process exit cleanly regardless of which implementation (broken or fixed) is under test,
     since both retry forever on a 5xx.
   - Add `timeout=10` (seconds) to **both** new `subprocess.run(...)` calls in Task 1 (tests 5 and
     6), as a safety net independent of the `stop()` fix — so if a future regression reintroduces
     unbounded retrying with no `stop()` anywhere in a test, pytest raises a clear
     `subprocess.TimeoutExpired` after 10s instead of hanging the whole suite/CI indefinitely. Note
     in the task text that the CURRENT (pre-fix) code's "red" run for test 5 will hit this same
     10s timeout rather than a clean assertion failure — that's expected and correct, not a
     mistake in the test.

3. **Add one automated DOM-harness test exercising the history-toggle path end-to-end.** Codex found
   this and cold-Opus independently corroborated it: today, Tasks 6-8's entire live/history state
   machine (`setSeatSource`, `fetchHistoryPage`, `applyHistoryPage`, the scroll listener, the
   viewport-fill fallback, mode-toggle button wiring) has **zero** automated coverage beyond
   isolated pure-function unit tests (`shouldAutoFetchHistoryPage` etc., tested in isolation, not as
   wired into `init()`) — Task 9's harness only ever exercises the live/`Clear` path, never clicks
   `#mode-history`, never mocks `GET /orchestrators/{id}/seats/history`. Since history-mode parity is
   literally half of this feature's stated purpose ("restyle + history parity"), leaving it entirely
   unverified until the manual browser check (Task 13) is a real gap — this also directly confirms
   the plan author's own self-flagged uncertain decision #1 ("a reviewer could reasonably want the
   same harness extended to history-toggle integration too").

   Fix: extend Task 9 (once its harness parser is fixed per item 1) with a second scenario in the
   same test file — reusing `makeDom()`/`FakeElement`/`flush()` — that: loads orchestrators, clicks
   `#mode-history`, mocks a `fetch` response for `/orchestrators/{id}/seats/history` returning a
   small page with `has_more:true` and a real `next_cursor`, asserts the sidebar populates from that
   response (not from any live frame) and `#history-status`'s text reflects the loading→loaded
   transition, then drives one pagination trigger (either a mocked `scroll` event via
   `seatPanelEl.dispatchEvent("scroll")` or a direct viewport-fill check by setting
   `clientHeight > scrollHeight` before the first page resolves) and asserts a second page's rows
   get appended without duplicating the first page's `task_id`s.

4. **One additional CSS assertion, hardening Task 11 against a reverted dark-mode contrast fix.**
   Cold-Opus's P2-1: the existing test list checks `--ink-500:#A8A299;` is present, but doesn't
   positively rule out the pre-remediation value `#918B80` also appearing somewhere in the dark
   block (e.g. from a bad merge or a copy-paste of the wrong constant). Mirror the plan's existing
   negative check pattern (`assert "color-scheme: light dark" not in html`): add
   `assert html.count("--ink-500:#A8A299;") == 1` (or equivalent) to Task 11's CSS test, confirming
   the corrected value appears exactly once and nowhere is the old, WCAG-failing value reintroduced.

### ACKNOWLEDGED — verified sound, no change needed

- **The abort-triggers-rejection mock in `makeSseResponse`** (the plan author's own flagged
  uncertain decision #2) — codex independently ran a real Node HTTP SSE response with a pending
  `reader.read()` against a real `AbortController.abort()` and confirmed it rejects with a genuine
  `AbortError`, matching the mock's assumption exactly. The mock is faithful; no change needed.
- **Adding `updateModeButtons()` to the `Clear` handler beyond the spec's literal field list** (the
  plan author's own flagged uncertain decision #3) — cold-Opus confirmed this is sound and low-risk:
  without it, resetting `seatSource` to `"live"` during Clear would leave a stale "History" segment
  visually `aria-pressed`. Keep as specified.
- **Hoisting/ordering across Tasks 6-8** — all three reviewers confirmed `openOrchestrator`,
  `updateModeButtons`, `renderSeats`, `setSeatSource`, and `startOrchestratorStream` are all
  `function` declarations in one `init()` scope, so forward references are safe regardless of task
  order, and since `init()` only runs on `DOMContentLoaded` (never dispatched by the Tasks 1-5 pure
  function tests), landing Task 6 alone cannot regress the already-passing suite. No change needed.
- **`historyCursor` threading** — confirmed present and consistent at both call sites (the
  `applyHistoryPage` options object and the scroll listener's early-return guard). Cold-Opus also
  caught that the spec's own pre-remediation *body* text (§3.5.1/§3.6) omits `historyCursor` from
  its illustrative code — an internal spec inconsistency between its body and its own remediation
  section — but the plan correctly followed the authoritative remediation item 7, not the stale
  body text. No plan change needed; noted as a good catch, not a defect in this plan.
- **Global-constraint byte-consistency** (the 15-entry export list, the corrected dark
  `--ink-500:#A8A299`, the full `reduceSeat` guard comment text) — confirmed identical, byte-for-byte,
  across the Global Constraints block, every implementing task, and every asserting test.

### Still open for the codex TDD implementer

The manual browser check (Task 13) remains required regardless of the above — it is the only place
the actual visual restyle (dark-mode contrast beyond the one WCAG value already fixed, the seat-row
grid layout's real rendered appearance, the age-tick's visible ticking) gets checked at all, since
this repo has no visual-regression tooling. Do not skip it or treat green automated tests as
sufficient on their own.

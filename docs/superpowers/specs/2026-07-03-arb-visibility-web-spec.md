# ARB Visibility web page — restyle + history parity — implementation spec

Authoritative source: `docs/superpowers/specs/2026-07-03-arb-visibility-web-design.md`, its
**"Warm-orchestrator remediation (post design-panel)"** section governing wherever it differs from
the design's earlier body text. This spec turns that (panel-reviewed, remediated) design into an
exact, buildable contract for two files only:

- `src/arb_memory/static/index.html`
- `src/arb_memory/static/app.js`

Backend (`src/arb_memory/visibility.py`) is **read-only reference** — not modified, contract
confirmed against the current code (line references below point at the code actually read, not
assumed). Nothing here is a placeholder; the plan stage should not need to re-decide anything in
this document.

---

## 1. CSS — exact values

### 1.1 `:root` custom properties (light + dark)

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
    --ink-900:#FAF8F4; --ink-700:#D9D3C7; --ink-500:#918B80; --ink-400:#6E6960;
    --line-200:#3A362E; --line-300:#4A453B;
    --clay-600:#C97350; --clay-700:#E0916B; --clay-100:#3D2A20;
  }
}
*{box-sizing:border-box}
html,body{margin:0}
```

Values copied verbatim from `login.py:41-49` for the light block. The dark block is an **inverted
mapping within the same token names** (per remediation item 4) — paper/ink swap roles; `line-200`/
`line-300` get new darker greys (the light hex values would be invisible as borders on a dark
background, so reusing them verbatim is not viable here — this is the one sub-decision in this
block that isn't a straight verbatim copy, flagged in the summary); `clay-600`/`clay-700` are
lightened (`#C97350`/`#E0916B`) because the original clay hexes are too dark to read against a
`#1F1D1A` background — same hue family, adjusted for dark-background contrast, not a new hue.
`clay-100` becomes a dark tinted fill (`#3D2A20`) instead of a light tint, for the same reason.

### 1.2 Fonts

Identical `<link>` pair to `login.py:37-39` (Google Fonts `preconnect` ×2 + the combined
`Newsreader` + `IBM Plex Mono` stylesheet link), placed in `index.html`'s `<head>`.

### 1.3 Base

```css
body{
  background:var(--paper); color:var(--ink-700); font-family:var(--serif);
  -webkit-font-smoothing:antialiased; line-height:1.4;
}
```

(Drops `color-scheme: light dark` and all `Canvas`/`CanvasText`/`Highlight` system-color usage from
the current `index.html:8-9,15-16,20,42,47,57,66,82,89,93` — replaced by the tokens above and the
dark-mode media query in 1.1. This is the flagged "no auto-invert" deviation from the design's Risks
section, now mitigated by 1.1's explicit dark override.)

### 1.4 Header

```css
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
```

`grid-template-columns` gains two `auto` tracks versus today's `minmax(140px,1fr) minmax(220px,2fr)
auto` (`index.html:23`) — one for the new mode-toggle, one for `Clear` (title | token | orchestrator
| mode-toggle | clear). `<h1>` text becomes `ARB · Visibility` (brand mark, not the 3.25rem login
splash size — this is a dense dashboard header per the design's explicit deviation note).

### 1.5 Inputs / select

```css
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
```

Copied verbatim from `login.py:82-92`'s `input.t` treatment, applied to both `input#token` and
`select#orchestrator` (native `<select>` can't take the `input.t::placeholder` letter-spacing trick,
so that one line of `login.py:88` is not ported — everything else is identical). This is the
**only** place `box-shadow` is used anywhere in this design (per design body §"Design tokens" — no
floating `.card` box-shadow exists on this page since `aside`/`section` are flush dashboard panes,
not a centered card; see 1.7).

### 1.6 Buttons (secondary + segmented mode toggle)

```css
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
```

`:focus-visible` rule copied verbatim from `login.py:102`. The base `button` rule is the new
**secondary** variant (design body §"Buttons": `background:var(--card); border:1px solid
var(--line-300); color:var(--ink-700)`) applied to `#clear-token` and both segmented-toggle buttons
— `login.py`'s primary (`background:var(--ink-900); color:var(--paper)`) button style is **not**
used anywhere on this page (no true "submit" action exists here). `.segmented button[aria-pressed=
"true"]` is the one other place clay appears besides `failed` (the active-mode selection state).

### 1.7 Sidebar / seat rows

```css
main{ display:grid; grid-template-columns:minmax(260px,34%) 1fr; min-height:calc(100vh - 73px); }
aside#seat-panel{
  background:var(--card); border-right:1px solid var(--line-200); overflow:auto;
  border-radius:8px; /* only the top-left/bottom-left corners read visually; panel is otherwise
                          flush to header/viewport edges — pinned per the door-page radius split
                          (8px pane-level / 5px control-level) even though most of it is a no-op */
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
```

Grid layout: 3 columns (`1fr auto auto`), 5 children in DOM order (`seat-id`, `state-cell`, `age`,
`run-id`, `last-event`) → row 1 auto-places `seat-id | state-cell | age`; row 2 auto-places
`run-id` into column 1, and `last-event` is explicitly `grid-column: 2 / span 2` so it fills the
remaining width of row 2 rather than leaving column 3 empty under `age`. `aria-current="true"` keeps
`--paper-sunk` (not clay — clay stays reserved for `failed`/active-toggle per the design's restraint
principle).

### 1.8 State badges (exact — no new hues)

```css
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
```

Matches the design's table exactly: `done`/`incomplete`/`unknown`/`voted` all render as the plain
`.badge` rule (no override needed — that IS the neutral treatment); `stale` gets the `ink-400`
override; `failed` gets the `clay-700` border/text override; `running` gets the plain badge **plus**
`state-cell.is-running`'s pulsing dot. `prefers-reduced-motion` disables only the `animation`
(dot stays visible, static, `opacity:1`) — the JS age-tick timer that drives re-renders is
unaffected by this media query (see §3.6).

### 1.9 Timeline pane

```css
section{ min-width:0; background:var(--paper); border-radius:8px; /* see 1.7 note on flush panels */ }
#timeline{
  margin:0; min-height:calc(100vh - 73px); overflow:auto; padding:14px 18px;
  white-space:pre-wrap; font-family:var(--mono); color:var(--ink-700);
}
#timeline details summary{ color:var(--ink-500); cursor:pointer; }
```

### 1.10 Auth banner (new)

```css
#auth-banner{
  background:var(--clay-100); border-bottom:1px solid var(--clay-600); color:var(--clay-700);
  font-family:var(--mono); text-transform:uppercase; letter-spacing:.06em; font-size:.75rem;
  padding:10px 18px;
}
#auth-banner[hidden]{ display:none; }
```

### 1.11 Responsive (unchanged breakpoint, unchanged behavior)

```css
@media (max-width: 720px) {
  header, main { grid-template-columns: 1fr; }
  aside#seat-panel { border-right:0; max-height:42vh; }
}
```

Same `720px` breakpoint and stacking behavior as `index.html:108-117` today — restyle only.

---

## 2. HTML structure changes

Full new `<body>` (head gains the two Google Fonts `<link>` tags per §1.2; `<style>` block is
§1's CSS in full):

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

Decisions pinned:

- **Mode toggle markup: a `role="group"` wrapper around two `aria-pressed` toggle buttons, not
  `role="tablist"`.** Tabs imply separate tab-panels; here there is exactly one panel (the seat
  list) whose *data source* changes — the ARIA toggle-button-group pattern (`aria-pressed`) matches
  that semantics; `tablist`/`aria-selected` would misrepresent it. Both buttons start `disabled`
  (matching the design's "disabled until an orchestrator is selected"); JS removes `disabled` from
  both once `orchestratorSelect.value` is truthy.
- `#auth-banner` sits directly under `<header>`, outside `<main>`, `hidden` by default, `role=
  "alert"` (assistive tech announces it when un-hidden without needing a live-region poll).
- `#seats` gains an `id="seat-panel"` on its **parent** `<aside>` (not on `<ul>`) — this is the
  scroll-listener/viewport-fill target (§3.5), since `<aside>` is the scrolling container
  (`overflow:auto` in CSS), not the `<ul>` itself.
- `#history-status` is a new sibling of `#seats` inside `<aside>`, the sidebar's bottom status line
  (§3.6 for its exact content rule).
- Each seat row's button markup (rendered by `renderSeats()`, not static HTML) is, in DOM order:
  `seat-id`, `state-cell` (wrapping `pulse-dot` + `badge`), `age`, `run-id`, `last-event` — see
  §1.7/§1.8 for the CSS this maps to and §3.7 for the exact `renderSeats()` rewrite.

---

## 3. `app.js` — every new/changed function

### 3.1 `streamSSE` — structured error + 4xx fatal-stop

**Verified against the running code** (`app.js:210-268` today): on `!response.ok`, `streamSSE`
throws `Error("SSE " + status)`, the `catch` emits `{event:"error", data:{message}}`, then falls
through unconditionally to the backoff `await`/loop — nothing ever sets `stopped = true`. A revoked
token hammers the gateway with a fresh 4xx every 500ms→5s forever. Fixed exactly per the
already-shipped, panel-reviewed Go pattern (`streamFatalEvent`/`s.fatal`, `tools/arb-watch-go/
sse.go:126-133`, `model.go:118,258-263,279-281`):

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

Exact pins:

- Error event shape: `{ event: "error", data: { message: string, status: number | null } }`.
  `status` is `null` for a network failure (no HTTP response at all — `fetch` rejects before a
  status exists) or an `AbortError` never reaches `onEvent` at all (existing early-return, unchanged).
- **Fatal-stop condition is any 4xx (400–499), not just 401/403** — this matches the Go source's
  `resp.StatusCode >= 400 && resp.StatusCode < 500` exactly (`sse.go:126`), not a narrower "auth
  only" condition. A 404 (bad orchestrator id) or 429 also stops retrying; it just doesn't trigger
  the auth banner (see §3.8 — banner condition is the narrower `401 || 403` subset of this broader
  stop condition).
- Non-4xx failures (network blip, 5xx, `status === null`) fall through unchanged: existing
  500ms→5s doubling backoff, unlimited retries, no behavior change.
- **Decided: no new callback parameter on `streamSSE`.** A consumer learns "this stream is dead,
  stop expecting more" implicitly — no further `onEvent` calls ever arrive after a fatal 4xx's
  error event, because `connect()` returns. A consumer that wants to distinguish fatal-vs-retrying
  inspects `data.status` on the error event it already receives (`status !== null && status >= 400
  && status < 500` ⇒ fatal, no more events coming). This mirrors Go's approach of not exposing
  `.fatal` to any caller outside `Update`'s own frame-handling switch. `stop()`'s signature is
  unchanged.

### 3.2 Age formatting

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

- `ageLabel(seconds)` is byte-identical in logic to `tools/arb-watch-go/reduce.go:192-201`'s
  `ageLabel` (already given verbatim in the design body — ported unchanged).
- `utcDayMonth` uses `getUTCDate()`/`getUTCMonth()` explicitly — **not** `getDate()`/`getMonth()` —
  per remediation item 5 (a naive local-time formatter shifts the displayed day near midnight
  depending on the operator's UTC offset; the Go sibling formats from UTC, `model.go:1176,1457`).
  Returns `null` (not a placeholder string) when `lastEventTs` doesn't parse, so the caller
  (`seatAgeLabel`) can fall back cleanly.
- `seatAgeLabel(lastEventTs, seatSource, nowMs)` is the actual per-row entry point `renderSeats()`
  calls: relative age by default, `dd/mm` **only** when `seatSource === "history"` **and** age `>=
  86400` seconds — matching `model.go`'s `if ok && m.seatSource == "history" && ageSecs >= 24*3600`
  exactly (`model.go:1455`). The `nowMs` parameter is threaded through explicitly (not read from
  `Date.now()` internally) so the age-render timer (§3.6) and tests can supply a fixed clock.
  Unparseable/missing `last_event_ts` renders `"—"` (matches Go's `seatAge`'s `"—"` fallback,
  `model.go:1186`).
- `parseTs` is the existing private helper (`app.js:65-71`, unchanged) — reused, not duplicated.

### 3.3 History state — new client-state variables

Declared alongside the existing `seats`/`selectedTaskId`/`stopOrchestratorStream`/`stopSeatStream`
inside `init()`:

```js
let selectedOrchestratorId = "";
let seatSource = "live";           // "live" | "history"
let historyCursor = null;
let historyHasMore = false;
let historyLoading = false;
let historyGen = 0;
let historyStatusText = "";        // "" | "history unavailable" — transient fetch-failure text
```

`selectedOrchestratorId` is new (today `openOrchestrator`'s parameter is purely local — needed here
so `setSeatSource("live")` can restart the orchestrator SSE stream against the right id without a
parameter needing to be threaded through the mode-toggle click handlers).

### 3.4 The historyGen guard — pure, testable

```js
function isStaleHistoryGen(requestGen, currentGen) {
  return requestGen !== currentGen;
}
```

Every history fetch closes over `historyGen`'s value at issue time (`requestGen`) and checks it
against the live `historyGen` at each resolution point via this one function — matching
`model.go`'s `if msg.gen != m.historyGen { return m, nil }` (`model.go:319,333`), single comparison
covering all three trigger sources (mode toggle, orchestrator switch, scroll-triggered pagination)
uniformly, chosen over a per-fetch `AbortController` for the reason the design gives: one mechanism
already has to exist for the render gate; a second, independent cancellation mechanism doing the
same job is not needed.

### 3.5 `setSeatSource` — the toggle-to-history / toggle-to-live transition

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

function updateModeButtons() {
  modeLiveButton.setAttribute("aria-pressed", seatSource === "live" ? "true" : "false");
  modeHistoryButton.setAttribute("aria-pressed", seatSource === "history" ? "true" : "false");
}
```

Named `setSeatSource(nextSource)`, not `toggleHistoryMode()` — the design's segmented control is
two distinct buttons ("Live" / "History"), not a single toggle, so each button's click handler
calls `setSeatSource("live")` / `setSeatSource("history")` directly rather than flipping a boolean.
Behavior it implements, pinned exactly:

- **Toggle to History:** bump `historyGen` first (invalidates anything in flight), clear `seats`,
  clear `selectedTaskId` **and stop any active seat-transcript stream** (remediation item 7 — a
  currently-open seat transcript that isn't in the new history view must not keep streaming), set
  `historyLoading = true`, render (sidebar shows "loading…" via `#history-status`, §3.6), fetch
  page 1 (`cursor` omitted — see §3.5.1). **The orchestrator SSE connection is NOT stopped** — see
  §3.7's `renderSeats`/orchestrator-stream-callback gating.
- **Toggle to Live:** bump `historyGen`, clear `seats`/`selectedTaskId`/seat-stream identically,
  then **restart** the orchestrator SSE connection from scratch (`stopOrchestratorStream()` then a
  fresh `startOrchestratorStream(selectedOrchestratorId)` call with no `Last-Event-ID` — a brand new
  `streamSSE` handle, not a resume) — matches `model.go`'s `toggleHistoryMode`'s live branch
  (`model.go:817-819`) exactly: no cursor reconciliation, rely on the gateway's own
  `SSE_BACKFILL_COUNT`-bounded fresh backfill (`visibility.py:32,571-580`).
- **Orchestrator switch while in History mode:** handled in `openOrchestrator` (§3.9), not here —
  an orchestrator switch always resets `seatSource` to `"live"` regardless of prior mode.

#### 3.5.1 History fetch function

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
      scrollHeight: seatPanelEl.scrollHeight,
      clientHeight: seatPanelEl.clientHeight,
    })
  ) {
    fetchHistoryPage({ append: true });
  }
}
```

Exact pins:

- **URL construction:** `GET /orchestrators/{id}/seats/history`, `cursor` query param included only
  on an append fetch with a non-null `historyCursor`; **`limit` is never sent** — both this client
  and the already-shipped Go client (`sse.go:67-76`, no `limit` param) rely on the backend's default
  clamp of 50 (`_clamp_history_limit`, `visibility.py:205-210`) rather than pinning a client-side
  page size, keeping one fewer independently-tunable knob.
- `next_cursor`/`has_more` are read directly off the JSON payload's top-level keys (matches
  `visibility.py:781`'s response shape `{"seats": [...], "next_cursor": ..., "has_more": ...}`
  exactly) — `historyCursor = payload.next_cursor || null` (an absent/`null` `next_cursor` when
  `has_more` is false correctly clears the cursor).
- **Stale-fetch guard applied at both await points** — once right after `fetch()` resolves (covers
  a mode/orchestrator change that happened while the HTTP round-trip was in flight) and again after
  `response.json()` resolves (covers a change during the body-parse await) — per the design's "on
  every history fetch's resolution" requirement and Cold-Opus's emphasis that this must be checked
  at each async boundary, not just once.
- **401/403 → auth banner, do not write `historyStatusText`.** Any other non-ok status (400
  malformed cursor, 5xx, or a network-level `catch`) → `historyStatusText = "history unavailable"`,
  existing `seats` are left untouched (not cleared) so previously-loaded rows stay visible, and
  `historyLoading` clears so a subsequent scroll or toggle can retry — matches the design's error
  table exactly (malformed cursor is "treated like a fetch failure," not specially distinguished).
- **Viewport-fill fallback fires only from `applyHistoryPage`**, i.e. immediately after a page
  successfully loads — not from the periodic age-render tick (§3.6) or from live SSE frames, both
  of which also call `renderSeats()` but must not trigger extra fetches.

### 3.6 Pagination — scroll listener + viewport-fill fallback

```js
function isScrolledNearBottom(scrollTop, clientHeight, scrollHeight, threshold) {
  return scrollTop + clientHeight >= scrollHeight - threshold;
}

function shouldAutoFetchHistoryPage({ seatSource, historyHasMore, historyLoading, scrollHeight, clientHeight }) {
  if (seatSource !== "history" || !historyHasMore || historyLoading) {
    return false;
  }
  return scrollHeight <= clientHeight; // content doesn't overflow -> normal scrolling can't fire
}

const HISTORY_SCROLL_THRESHOLD_PX = 40;

seatPanelEl.addEventListener("scroll", () => {
  if (seatSource !== "history" || !historyHasMore || historyLoading) {
    return;
  }
  if (isScrolledNearBottom(seatPanelEl.scrollTop, seatPanelEl.clientHeight, seatPanelEl.scrollHeight, HISTORY_SCROLL_THRESHOLD_PX)) {
    fetchHistoryPage({ append: true });
  }
});
```

- Scroll-listener threshold: `scrollTop + clientHeight >= scrollHeight - 40`, attached to
  `#seat-panel` (the `<aside>`, the actual scrolling container — not `#seats`, which has no
  `overflow` of its own).
- Viewport-fill fallback: called from `applyHistoryPage` (§3.5.1) after every page render — if the
  page's content still doesn't overflow the panel's visible height **and** `historyHasMore` is true,
  auto-fetch the next page; this repeats (each `applyHistoryPage` call re-checks) until either the
  content overflows (handing off to the scroll listener above) or `historyHasMore` is false. This
  closes the gap the scroll listener alone can't: with `has_more: true` but a short first page, no
  scrollbar ever appears and no `scroll` event ever fires.

### 3.7 Age re-render timer

```js
const AGE_TICK_MS = 2000; // matches the Go sibling's 2s tick (tickCmd, model.go:1146-1150)
setInterval(() => {
  renderSeats();
}, AGE_TICK_MS);
```

Started once, unconditionally, at the end of `init()`. Pinned decisions:

- **2s interval**, matching the Go TUI's tick exactly (not a new, independently-chosen cadence).
- Triggers a **full `renderSeats()`**, not an age-text-only patch — `renderSeats()` already
  rebuilds the whole sidebar from `Object.values(seats)` on every SSE frame today
  (`app.js:295-319`); reusing the same function for the tick avoids a second, narrower rendering
  path that could drift from the frame-driven one. Seat counts on this page are small (a fleet's
  live seats), so the cost of a full re-render every 2s is negligible.
- Runs **regardless of `prefers-reduced-motion`** — that media query only disables the CSS pulse
  *animation* (§1.8); the age text itself must keep updating for a quiet orchestrator's numeric age
  not to visibly freeze (this was the design's own acceptance-test gap the remediation caught —
  without this timer, "age column shows relative ages ticking up" cannot pass, since `renderSeats()`
  otherwise only runs on incoming frames/selection).

### 3.8 Auth banner

```js
function showAuthBanner() {
  authBannerEl.hidden = false;
}
function hideAuthBanner() {
  authBannerEl.hidden = true;
}
```

Trigger condition (one rule, two call sites — remediation item 2's "one trigger condition, one code
path, both surfaces"):

- **`/orchestrators` fetch** (`loadOrchestrators`, §3.9): `response.status === 401 || response.status
  === 403`.
- **Any `streamSSE` error event** (orchestrator stream §3.9, seat stream §3.10, history fetch
  §3.5.1): `frame.data.status === 401 || frame.data.status === 403`.

Non-auth error paths keep their existing inline-timeline-text behavior **unchanged**, and do
**not** also show the banner:

- Network failure / 5xx on `/orchestrators`: unchanged `timelineEl.textContent = "[error]
  /orchestrators " + response.status + "\n"` (only for the non-401/403 branch — see exact code in
  §3.9).
- Non-4xx or non-auth-4xx SSE errors (network blip, 5xx, a fatal-but-non-auth 4xx like 404/429):
  unchanged inline `"[error] " + frame.data.message + "\n"` write, banner not shown.
- Malformed/expired history cursor (400): treated as a generic fetch failure (§3.5.1), no banner.

`hideAuthBanner()` is called at the **top** of the 401/403 branch's else-path in `loadOrchestrators`
(i.e., any non-401/403 result — including success — clears a stale banner from a previous failed
attempt); it is also called from `clearToken`'s click handler (§3.9) so clearing the token resets
the banner along with everything else.

### 3.9 `loadOrchestrators` / `openOrchestrator` / `clearToken` — full rewrites

```js
clearToken.addEventListener("click", () => {
  localStorage.removeItem("token");
  localStorage.token = "";
  tokenInput.value = "";
  orchestratorSelect.replaceChildren();
  seatsEl.replaceChildren();
  timelineEl.textContent = "";
  hideAuthBanner();
});

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

Exact pins:

- **401/403 on `/orchestrators`:** banner shown, sidebar/dropdown/timeline cleared, **no** inline
  `[error]` text written (the banner replaces it for this one failure mode). Any other non-ok
  status: existing inline text write, unchanged, no banner.
- `openOrchestrator` now **also resets history state and mode to `"live"`** on every orchestrator
  switch (bumps `historyGen`, clears `historyCursor`/`historyHasMore`/`historyLoading`, forces
  `seatSource = "live"`) — "an orchestrator switch behaves exactly as it does today, plus resetting
  the mode" per the design. This is on top of the existing `stopOrchestratorStream`/`stopSeatStream`
  cleanup (`app.js:344-350`, unchanged) and the existing `selectedTaskId = ""` clear — both already
  present today, now also explicitly covering the seat-stream/`selectedTaskId` pairing per
  remediation item 7.
- **`openOrchestrator`'s live-frame handler gates its only write behind `seatSource === "live"`** —
  matches `model.go:269` (`if m.seatSource == "live" { m.upsertSeat(...) }`) exactly. The
  orchestrator SSE connection itself is never stopped by a mode toggle; only this write-site guard
  changes behavior.
- Mode-toggle buttons are enabled the moment `orchestratorSelect.value` is truthy (both in
  `loadOrchestrators`, right after populating the dropdown, and unconditionally in
  `openOrchestrator`, since reaching it implies an orchestrator is selected).

### 3.10 `selectSeat` — seat-transcript stream, same auth-banner treatment

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

Only the error-branch changes (banner vs. inline text, matching §3.9's orchestrator-stream
treatment) — the rest of `selectSeat` is unchanged from `app.js:321-338`.

### 3.11 `renderSeats()` — age column + state-cell + history-status line

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

Pinned:

- `voted`/`stance` rendering (`" (" + stance + ")"` appended to the badge text) is unchanged from
  today's absence — this is new in the sense that the design's badge table calls it out, but no
  prior code rendered it either; adding it here is the first time it's rendered at all. **Live-only**
  per remediation item 3: `_history_row_to_seat` (`visibility.py:255-266`) never sets `voted`/
  `stance`, so this branch is structurally unreachable for a history-mode row — not a parity bug to
  chase.
- **`#history-status` content resolution** (`historyStatusLine()`): the design's prose is read
  together with the Go reference it says to match (`renderFilterBar`, `model.go:1379-1389`) — Go
  shows `"· history"` unconditionally whenever `seatSource === "history"` (regardless of loading/
  more-pages state), with loading/error surfaced as a **separate** status message. This spec pins
  that reading (persistent `"· history"` marker + an appended loading/error suffix) as the decided
  behavior — this is one of the two decisions in this spec I'm least certain about (see summary).

---

## 4. Interfaces block

| Function | Signature | Returns / behavior |
|---|---|---|
| `streamSSE` (changed) | `streamSSE(url, onEvent)` | unchanged signature; error events now `{event:"error", data:{message, status}}`; stops permanently (no more `onEvent` calls) after any 4xx |
| `ageLabel` (new, ported) | `ageLabel(seconds: number) => string` | `"Ns"` / `"Nm"` / `"Nh"`, identical to Go's `ageLabel` |
| `utcDayMonth` (new) | `utcDayMonth(lastEventTs: string) => string \| null` | zero-padded `"dd/mm"` from UTC fields, or `null` if unparseable |
| `seatAgeLabel` (new) | `seatAgeLabel(lastEventTs: string, seatSource: "live"\|"history", nowMs?: number) => string` | relative age, or `dd/mm` when `seatSource==="history"` and age `>=86400`s; `"—"` if unparseable |
| `isStaleHistoryGen` (new) | `isStaleHistoryGen(requestGen: number, currentGen: number) => boolean` | `true` iff the fetch that captured `requestGen` should discard its result |
| `isScrolledNearBottom` (new) | `isScrolledNearBottom(scrollTop, clientHeight, scrollHeight, threshold) => boolean` | pure scroll-threshold check |
| `shouldAutoFetchHistoryPage` (new) | `shouldAutoFetchHistoryPage({seatSource, historyHasMore, historyLoading, scrollHeight, clientHeight}) => boolean` | viewport-fill-fallback decision |
| `reduceSeat` (unchanged) | `reduceSeat(state, entry)` | unchanged; see guard comment below — stays unwired |

### `reduceSeat` guard comment — exact text to place above it in `app.js`

```js
// Do NOT call from the live path — it expects raw redis fields (event_type/sent_at) that a live
// frame, already reduced server-side, does not carry; wiring it in would freeze every seat at its
// first state. Kept unwired, exported only for the JS/Python parity contract test
// (test_appjs_parser_and_reducer_match_visibility_reducer), which asserts this function stays
// byte-identical to Python's _reduce_seat over a shared fixture.
```

This is the corrected rationale per remediation item 1 — verified directly against
`visibility.py:129-160` (`_reduce_seat`'s SSE output carries `last_event`/`last_event_ts`, never
`event_type`/`sent_at`) and `app.js:84-120` (every one of `reduceSeat`'s branches keys off
`entry.event_type`). **Do not ship the earlier "no-op, kept for symmetry" phrasing anywhere** — it
is factually wrong (wiring it in is an active regression, not a no-op) and this exact comment text
is what prevents a future reader from re-deriving the wrong framing.

### `module.exports` — full new list

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

The first nine are the already-exported set (`app.js:396-406`), signatures/behavior unchanged
except `streamSSE` per §3.1. The six new entries are additive.

---

## 5. Complete test list

All new tests follow the existing harness's exact pattern: Python `subprocess.run([node, "-e",
script], ...)`, `require()`-ing `app.js` directly, no new JS test runner, no `package.json`. File:
`tests/arb_memory/test_visibility_web_contract.py` (new tests appended to this file — no new test
file needed since it's the same module under test and the same harness pattern).

1. **`test_appjs_parser_and_reducer_match_visibility_reducer`** (existing, `app.js:37-72` behavior
   unchanged) — must still pass **unmodified**; `reduceSeat`/`parseFrames` are untouched by this
   spec.
2. **`test_appjs_real_last_event_id_filter`** (existing) — unmodified, unaffected.
3. **`test_appjs_formats_transcript_timeline_kinds`** (existing) — unmodified, unaffected.
4. **`test_appjs_appends_transcript_details_as_html`** (existing) — unmodified, unaffected.
5. **`test_appjs_streamsse_stops_retrying_after_4xx`** (new) — mock `global.fetch` to always
   resolve `{ ok: false, status: 401 }`; call `streamSSE(url, onEvent)`; wait ~600ms (longer than
   the 500ms base backoff, so a still-looping implementation would have re-invoked `fetch` by
   then); assert `onEvent` was called exactly once with `{event:"error", data:{message, status:
   401}}` and the mock `fetch` was invoked exactly once (proves no re-issued request after the
   fatal event).
6. **`test_appjs_streamsse_keeps_retrying_after_5xx`** (new) — mock `global.fetch` to always
   resolve `{ ok: false, status: 500 }`; wait ~600ms; assert `fetch` was invoked **more than once**
   (proves the 4xx fix didn't regress the existing non-fatal backoff/retry behavior).
7. **`test_appjs_utc_dd_mm_formatter_ignores_local_timezone`** (new) — spawn the Node subprocess
   with `env={**os.environ, "TZ": "Pacific/Kiritimati"}` (UTC+14, chosen so a naive local-time
   formatter shifts the date forward); call `seatAgeLabel("2026-06-25T23:50:00Z", "history",
   Date.parse("2026-06-27T00:00:00Z"))`; assert the result is `"25/06"` (the UTC date), not
   `"26/06"` (what `getDate()`/`getMonth()` under `+14:00` would produce).
8. **`test_appjs_history_gen_guard_discards_stale_fetch`** (new) — `isStaleHistoryGen(1, 2) ===
   true`; `isStaleHistoryGen(2, 2) === false`.
9. **`test_appjs_viewport_fill_fallback_triggers_additional_fetch`** (new) —
   `shouldAutoFetchHistoryPage({seatSource:"history", historyHasMore:true, historyLoading:false,
   scrollHeight:100, clientHeight:400}) === true` (content doesn't overflow → auto-fetch);
   `=== false` when `scrollHeight > clientHeight` (content overflows → scroll listener takes over),
   when `historyLoading === true`, when `seatSource === "live"`, and when `historyHasMore ===
   false`.
10. **`test_appjs_scroll_threshold_near_bottom`** (new) — `isScrolledNearBottom(360, 400, 800, 40)
    === true` (`360+400=760 >= 800-40=760`); `isScrolledNearBottom(300, 400, 800, 40) === false`.
11. **`test_appjs_exports_full_contract_surface`** (new) — `Object.keys(require(app.js)).sort()`
    equals the exact sorted §4 export list (guards against a future edit silently dropping
    `reduceSeat` or one of the new exports from `module.exports`).

### Test commands

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
```

Same single invocation as today (`tests/arb_memory/test_visibility_web_contract.py` is the only
file touched — no new test file, no new fixture file needed beyond the existing
`tests/fixtures/sse_web_orchestrator.txt`, which is untouched since `reduceSeat`/`parseFrames`
don't change).

---

## 6. What does NOT change (confirmed against the running code)

- Auth model: Bearer token via `Authorization` header (`app.js:122-125`, `visibility.py:91-93`
  `_bearer_token`), `localStorage`-persisted, `Clear` semantics (§3.9 keeps the exact existing
  behavior, adding only `hideAuthBanner()`).
- SSE hand-rolled parser over `fetch` (`parseFrames`, `isRealEventId`) — unchanged; `EventSource`
  still can't send the `Authorization` header, so this approach stays.
- Backend routes/contracts — `/`, `/app.js`, `/orchestrators`, `/sse/orchestrator/{id}`,
  `/sse/seat/{task_id}`, `/orchestrators/{id}/seats/history` all confirmed correct as read
  (`visibility.py:783-792`) — nothing in `visibility.py` is modified by this spec.
- `reduceSeat`'s exported signature/behavior, and the parity contract test that pins it against
  `_reduce_seat` — both unchanged (only the comment above it in `app.js` changes).
- The 500ms→5s doubling backoff for non-4xx SSE failures — unchanged.

---

## Spec decisions flagged as least certain

1. **The full dark-mode hex palette (§1.1) is my own construction beyond the design doc's four
   illustrative example values.** The design explicitly says "e.g." for only 4 of the 11 tokens
   (`--paper`, `--paper-sunk`, `--ink-900`, `--ink-700`) — I had to invent `--card`, `--line-200`,
   `--line-300`, and lighten `--clay-600`/`--clay-700`/`--clay-100` for dark-background contrast,
   none of which the design or `login.py` specifies. These are internally coherent but genuinely new
   hex values, not verbatim-copied tokens, which cuts slightly against the "copy tokens verbatim"
   instruction (unavoidable for a light→dark contrast inversion, but worth a design-eyes check
   before the plan stage builds against it).
2. **The `#history-status` line's exact content rule (§3.11)** resolves an ambiguity in the design's
   prose (which reads as "loading…" replacing the marker vs. Go's actual behavior of a persistent
   "· history" marker plus a separate status message) by deferring to the Go source
   (`model.go:1379-1389`) rather than the more literal English reading of the design doc. I believe
   this is the right call per the brief's own instruction to treat the Go mechanisms as the
   authoritative reference, but it is a judgment call between two documents that don't say quite the
   same thing.
3. **The seat-row grid layout (§1.7) is a genuinely new 3-column/2-row structure** (`seat-id | state-
   cell | age` on row 1, `run-id | last-event` spanning row 2) that the design doc doesn't pin at
   this level of CSS-grid-track detail — it says "age column goes in each row" but not which row/
   column. My placement is a reasonable reading of "restyle, not rebuild" applied to a genuinely new
   column, but it's my invention, not extracted from any of the read-first sources.

## Warm-orchestrator remediation (post spec-panel)

Four independent reviews: certifying quorum codex (**FIX_BEFORE_PLAN**), agy-print + pi-GLM (both
**PLAN_READY_WITH_NITS**), plus cold-Opus as a non-certifying contributor (**PLAN_READY_WITH_NITS**) —
non-certifying for the same quorum-swap reason as the design panel (this spec's author is a
cold-Sonnet subagent, same Claude lineage as cold-Opus). Every finding below was independently
verified against the real code — several by direct execution, not just reading — before acceptance;
one finding was rejected as factually wrong after verification, and a direct disagreement between
two certifying reviewers about what the Go source actually does was resolved by reading the cited
lines myself. **This section is authoritative where it differs from the body above.**

### DECIDED — must apply at plan stage

1. **`Clear` must become a full client reset, not just a DOM/localStorage clear.** Codex (certifying)
   found this, confirmed directly against `app.js:286-293`: today's handler — and the spec's §3.9
   rewrite, unchanged in this respect — only clears `localStorage`, DOM, and (new) the auth banner.
   It never stops either SSE stream. Concrete failure: an operator with an open orchestrator
   connection presses Clear; the UI blanks, but the already-negotiated SSE request (its
   `Authorization` header was sent at connect time, not re-checked) keeps delivering frames, and the
   very next one silently repopulates the sidebar — Clear does not actually disconnect anything.
   Fix: `clearToken`'s handler must also, in this order: stop `stopOrchestratorStream`/
   `stopSeatStream` (null both handles), delete all keys from `seats`, reset `selectedTaskId` and
   `selectedOrchestratorId` to `""`, bump `historyGen`, reset `seatSource = "live"`, clear
   `historyCursor`/`historyHasMore`/`historyLoading`/`historyStatusText`, disable both mode buttons
   (`modeLiveButton.disabled = modeHistoryButton.disabled = true`), then clear the DOM and hide the
   banner as already specified.

2. **The `reduceSeat` guard comment must state the EMPIRICALLY VERIFIED mechanism — not "freeze at
   first state."** This one comment's accuracy was disputed three ways across the two panels (the
   original design-stage phrasing said "freeze at first state"; pi-GLM's spec-panel review said
   "not a freeze — would go stale for running seats, unchanged for terminal seats"; cold-Opus's
   spec-panel review defended the original "freeze" framing as accurate). Rather than adjudicate by
   argument, I ran it: `node -e` with the real, exported `reduceSeat`, fed three consecutive
   already-reduced frames matching the exact shape `_reduce_seat` emits (`running` → `running` →
   `done`, real `task_id`/`run_id`/`seat_id`/`orchestrator`/`last_event_ts`/`last_event`/`state`
   fields, no `event_type`/`sent_at`). Result, every time: **`state` (and `last_event`,
   `last_event_ts`) are never present on the output object at all** — not frozen at a real value,
   not eventually forced to `"stale"`. Mechanism: `entryData()` returns `{}` for a frame with no
   `.data` key (confirmed at `app.js:53-61`), no `eventType` branch ever fires (there is no
   `event_type` key to match), and critically `state` is never assigned "running" through this path
   in the first place, so `isStale`'s `state.state !== "running"` guard (`app.js:74`) means the
   stale-fallback can never fire either. A seat wired through this path would render `"unknown"`
   (via `renderSeats`'s `seat.state || "unknown"` fallback) **forever**, from the very first frame
   onward — worse than "frozen at a real state," and not "goes stale for running seats" either,
   since state can never become `"running"` via this path to begin with. Replace the guard comment
   (spec §4) with:
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

3. **Tests 5 and 6 need a `localStorage` stub or they crash before exercising the code under
   test.** Cold-Opus found this; confirmed by direct execution: `streamSSE` → `connect()` →
   `authHeaders()` (`app.js:122-124`) calls `localStorage.getItem(...)` unconditionally. Plain
   `node -e` (the exact harness `test_visibility_web_contract.py` already uses) has no
   `localStorage` — calling `streamSSE` throws `TypeError: Cannot read properties of undefined
   (reading 'getItem')` inside the un-awaited `connect()`, which becomes an unhandled rejection
   before the mocked `fetch` is ever reached. As specified, tests 5/6 fail against a **correct**
   implementation, not just a broken one. Fix: both tests' Node scripts must set
   `global.localStorage = { getItem: () => null, token: "" }` (or equivalent) before calling
   `streamSSE`, in addition to mocking `global.fetch`.

4. **Tests 5 and 6 must not depend on real wall-clock timing.** Three independent reviewers
   converged here (agy P1, codex P2, pi-GLM's P1-2 self-downgraded to P2) — the ~600ms wait against
   a 500ms base backoff is workable but needlessly fragile on a loaded CI runner. Fix, per agy's and
   codex's suggested direction: in the test's Node script, override `global.setTimeout` to invoke
   its callback via `setImmediate` instead of respecting the real delay (e.g. `global.setTimeout =
   (fn) => setImmediate(fn);`) before calling `streamSSE`. This makes a reconnect attempt (if the
   implementation is broken and still loops) fire on the next microtask instead of 500ms later, so
   the test can assert on `fetch`'s call count after a short, fixed `setImmediate`-chain flush
   instead of a real-time wait — deterministic, no wall-clock margin to tune, zero new dependencies
   (matches the existing harness's plain-Node, no-framework pattern).

5. **The `#history-status` section's own citation of the Go source is wrong; the decision is not.**
   agy (certifying) and pi-GLM (certifying) directly disagreed about what `model.go:1379-1389`
   actually does — I read it myself to settle it. `renderFilterBar` shows `"· history"` as a
   **persistent, unconditional** marker whenever `m.seatSource == "history"`, entirely separate from
   `m.status` (a different field, set independently for loading/error text elsewhere in the Go
   model). Go does **not** combine them into one line — agy is right, pi-GLM is wrong on this
   specific factual question. The spec's own §3.11 prose ("this spec pins that reading... deferring
   to the Go source") is therefore an inaccurate citation. **The decision itself stands unchanged**
   — one combined `#history-status` line (persistent marker + appended loading/error suffix) is a
   reasonable, deliberate simplification for a page that only has one status-line element to begin
   with, not a literal port of Go's two-widget layout. Fix: reword §3.11's justification from "this
   spec pins that reading" to something like: *"Go keeps the persistent `'· history'` marker and
   loading/error status in two separate widgets (`renderFilterBar` vs. `m.status`); this page has
   only one status-line element, so this spec deliberately combines them rather than porting Go's
   two-widget split — a simplification, not a literal port."*

6. **Dark-mode `--ink-500` fails WCAG AA contrast against `--card`.** pi-GLM found this with real
   luminance math: `--card:#322E27` vs `--ink-500:#918B80` (used for `.run-id`/`.last-event`/`.age`
   text) computes to roughly 3.3:1 — below the 4.5:1 AA threshold for normal text (light mode's
   equivalent, `--card:#FFFFFF` vs `--ink-500:#6E6960`, passes at ~5.6:1). Fix: lighten the
   dark-mode `--ink-500` to approximately `#A8A299` (≈4.6:1 against `#322E27`), replacing the value
   in spec §1.1's dark media-query block. This is a correction to the spec's own already-flagged
   "invented, unaudited" dark palette (item 1 in its least-certain-decisions list) — not a new
   finding about a decided value, but a concrete fix to one of the invented ones.

7. **`shouldAutoFetchHistoryPage` needs a `historyCursor` truthy guard.** agy found this: without
   it, a hypothetical `has_more:true` response carrying a null/empty `next_cursor` would cause the
   client to re-fetch page 1 (no cursor param sent) instead of stopping or erroring, duplicating
   rows. Matches the Go client's own defensive check (`m.historyCursor != ""`). Fix: add
   `historyCursor` to `shouldAutoFetchHistoryPage`'s parameter object and its condition
   (`if (seatSource !== "history" || !historyHasMore || historyLoading || !historyCursor) { return
   false; }`), threading `historyCursor` through the two call sites in §3.5.1/§3.6.

8. **`init()` must explicitly declare the five new DOM element references.** agy and cold-Opus
   independently found the same gap: `modeLiveButton`, `modeHistoryButton`, `authBannerEl`,
   `seatPanelEl`, `historyStatusEl` are used throughout §3 but never shown being queried. Fix: add
   `const modeLiveButton = document.getElementById("mode-live");` (and the equivalent four lines)
   to `init()`'s existing declaration block, alongside `tokenInput`/`clearToken`/
   `orchestratorSelect`/`seatsEl`/`timelineEl`.

9. **Function scope (module-level vs. inside `init()`) needs one pinning sentence.** cold-Opus's
   finding: the six new pure functions (`ageLabel`, `utcDayMonth`, `seatAgeLabel`,
   `isStaleHistoryGen`, `isScrolledNearBottom`, `shouldAutoFetchHistoryPage`) must live at IIFE
   module scope (to reach `module.exports`), while `setSeatSource`/`fetchHistoryPage`/
   `applyHistoryPage`/`updateModeButtons`/`showAuthBanner`/`hideAuthBanner`/
   `startOrchestratorStream`/the scroll listener/the age-tick `setInterval` must live inside `init()`
   (they close over `seats`, `historyGen`, and the DOM element references from item 8). Derivable
   but not stated; the plan stage should place them accordingly without re-deriving this split.

10. **Test 11 (`exports_full_contract_surface`) should assert each export is a function, not just
    that the key exists.** agy's finding: `Object.keys(...).sort()` matching the expected list
    would still pass if some future edit accidentally exported `undefined` or a non-function value
    under the right key. Fix: additionally assert `typeof exports[name] === "function"` for every
    name in the list.

### ACKNOWLEDGED — explicitly deferred, not silently dropped

- **The invented dark-mode palette's remaining 6 values** (beyond the one WCAG failure fixed in
  item 6) are non-blocking per all three reviewers who checked them (agy: contrast-compliant;
  pi-GLM: didn't re-check beyond `--ink-500`; cold-Opus: "luminance directions are sane... a
  design-eyes check is the right disposition"). The spec's own flagged uncertainty stands: a visual
  check before/alongside implementation is still warranted, just not a plan-blocking requirement.
- **Viewport-fill auto-fetch's theoretical non-termination edge** (cold-Opus P2-4: a page of
  all-duplicate `task_id`s under `has_more:true` could loop) — low severity given keyset pagination
  guarantees `next_cursor` advances from the last real row (`visibility.py:778-780`), making an
  all-duplicate page implausible in practice. Optional hardening (only auto-fetch if the last page
  actually added rows) noted for the plan stage to adopt at its discretion, not required.
- **`#history-status`'s `aria-live="polite"` re-announcing identical text every 2s tick**
  (cold-Opus P2-5) — minor, loading windows are brief. Optional: skip the DOM write when the
  computed status line is unchanged from the previous render. Not required.

### Verified and REJECTED (do not re-litigate)

- **pi-GLM's counter-characterization of the `reduceSeat` mechanism** ("not a freeze — would set
  `state` to `\"stale\"` for running seats via the `isStale` fallback, leaving terminal seats
  unchanged") is **not what happens**, per the direct execution in item 2 above: `state` never
  becomes `"running"` through this path in the first place, so the `isStale` fallback (which
  requires `state.state === "running"`) can never fire either. Superseded by the empirically-verified
  wording in item 2 — no further action beyond adopting that wording.

# ARB Visibility web — Go-parity controls (filters + transcript toggles) — implementation spec

Authoritative source: `docs/superpowers/specs/2026-07-03-arb-visibility-web-controls-design.md`,
its **third design re-panel** section governing wherever it differs from earlier body text or
earlier remediation rounds (explicitly: the truncation-pipeline position and the `.dim` hint are
the third round's text, not round 2's). This spec turns that (three-times-remediated) design into
an exact, buildable contract for the same two files as the prior stage:

- `src/arb_memory/static/index.html`
- `src/arb_memory/static/app.js`

Backend (`src/arb_memory/visibility.py`) — read-only reference, not modified. Go
(`tools/arb-watch-go/reduce.go`, `model.go`) — read-only reference, the source every ported
function is checked against below with exact line citations. Nothing here is a placeholder.

---

## 1. CSS — exact new/changed rules

### 1.1 Layout restructuring (replaces both `calc(100vh - 73px)` rules)

Current `index.html` has two hardcoded offsets (`main{min-height:calc(100vh - 73px)}`,
`#timeline{min-height:calc(100vh - 73px)}`) that assumed only `<header>` consumes vertical space.
Adding `#filter-bar` and `#transcript-toolbar` breaks that assumption. Fix — outer flex column
(`body`) + inner flex column (`section`), two levels, both pinned:

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

Pins:

- `html, body{height:100%}` is new — required so `body`'s flex column has a definite height to
  distribute (`header`/`#auth-banner`/`#filter-bar` take natural height, `main` takes the rest).
- `main`'s `min-height:calc(100vh - 73px)` is **removed**, replaced by `flex:1 1 auto; min-height:0`
  — the outer level (remediation item 9).
- `section`'s `display:flex; flex-direction:column` is **new** — the inner level (remediation item
  20) — so `#transcript-toolbar` (first child) takes its natural height and `#timeline` (second
  child) gets the remaining space via `flex:1 1 auto; min-height:0; overflow:auto`.
- `#timeline`'s `min-height:calc(100vh - 73px)` is **removed** entirely, replaced by the flex rule
  above.
- `aside#seat-panel` is **unchanged** (`background:var(--card); border-right:1px solid
  var(--line-200); overflow:auto; border-radius:8px`) — it already scrolls independently and
  stretches to `main`'s row height via CSS Grid's default `align-items:stretch`, which now has a
  definite height to stretch against because `main` does.
- `@media (max-width:720px){ header, main { grid-template-columns:1fr; } aside#seat-panel {
  border-right:0; max-height:42vh; } }` — **unchanged, verbatim**. Single-column stacking still
  works: `body`'s flex column doesn't care how many grid columns `main` has.

### 1.2 `#filter-bar` (new)

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

Placed as a full-width sibling row (not nested in `aside`), matching the design's layout diagram
(`[ Filters: Status[all ▾] Agent[all ▾]  7/23 seats ]` spans the full width above the two-pane
grid). Visually related to but **not literally the same CSS rule** as `input#token,
select#orchestrator` — that rule is `width:100%` (correct for a header grid cell) which would make
these two selects stretch to fill a flex row; `#filter-bar select` is a new, narrower rule using
the same mono/ink/border/radius language. `#filter-count` uses `margin-left:auto` to push the
X/Y badge to the row's right edge.

### 1.3 `#transcript-toolbar` (new)

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
```

The four toggle buttons already inherit the base `button{...}` secondary style (background
`--card`, border `--line-300`, `--ink-700` text) from the existing global `button` rule — only
size and the pressed-state override are new. **Not** wrapped in `.segmented` — that class is for
mutually-exclusive single-selection groups (Live/History); these four toggles are independent
booleans, so each gets its own `aria-pressed` state with no shared pill border.

### 1.4 `.dim` (new)

```css
.dim{ color:var(--ink-500); }
```

Pinned as **new** per remediation's third-round correction — zero matches for `.dim` in the
current stylesheet (verified). Matches the muted-ink color already used inline for `#timeline
details summary` and other selectors, but is its own class, not a reuse.

---

## 2. HTML structure changes

Full new `<body>` (unchanged parts of `<style>` from §1 above omitted for brevity — every rule not
listed in §1 is untouched from the current file):

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
  <main id="app">
    <aside id="seat-panel">
      <ul id="seats"></ul>
      <div id="history-status" aria-live="polite"></div>
    </aside>
    <section>
      <div id="transcript-toolbar">
        <button id="toggle-timestamps" type="button" aria-pressed="false">Timestamps</button>
        <button id="toggle-labels" type="button" aria-pressed="false">Labels</button>
        <button id="toggle-expand" type="button" aria-pressed="false">Expand output</button>
        <button id="toggle-fullwidth" type="button" aria-pressed="false">Full width</button>
      </div>
      <pre id="timeline"></pre>
    </section>
  </main>
  <script src="/app.js"></script>
</body>
```

Decisions pinned:

- **`#filter-bar` sits between `#auth-banner` and `<main>`** — auth failures stay the topmost
  interrupt (unchanged position), filters are the next thing down, full width, above the two-pane
  grid (matches the design's ASCII layout diagram).
- **`#status-filter`'s 8 `<option>` values, exact order:** `all, running, incomplete, done, failed,
  voted, stale, unknown` — per brief item 1 and the design's corrected top-level table. `value` and
  visible text are identical strings for every option (matches the existing `select#orchestrator`
  population pattern of `option.value = option.textContent = id`).
- **`#agent-filter` ships with a single static `<option value="all">all</option>`** — the dynamic
  agent options are populated/reconciled by JS (`deriveAgentOptions`, §3.4) on the first
  `renderSeats()` call and every one thereafter; the static markup is just the pre-JS fallback.
- **`#filter-count` starts empty** — first non-empty content comes from the first `renderSeats()`
  call (existing `X/Y` population, §3.7).
- **All four toolbar buttons default `aria-pressed="false"`, none `disabled`** — per the design's
  ACKNOWLEDGED note, none of the four needs an active seat/orchestrator to be meaningful (Full
  width works with or without a selected seat; the other three simply have nothing yet to affect
  against an empty `#timeline`).
- **`#transcript-toolbar` is the first child of `<section>`**, `<pre id="timeline">` the second —
  required for §1.1's inner flex column to size correctly.

---

## 3. `app.js` — every new/changed function

### 3.1 `agentOf(seatId)` — new, ported from `reduce.go:111-134`

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

Exact pins, verified against `reduce.go:111-134`:

- Lowercase the **whole** `seatId` first (`lower`), check the `cold-opus-` prefix against that →
  `"opus"`.
- Otherwise, take the first `-`-delimited segment of the **already-lowercased** string. Go computes
  this as `strings.ToLower(strings.SplitN(seatID, "-", 2)[0])` — re-lowercasing a second time from
  the original string, but this is provably identical to lowercasing once then splitting (lowercase
  is a pure per-character transform that doesn't move `-` characters), so `lower.split("-")[0]` is
  the correct, idiomatic-JS equivalent — this exact JS-vs-Go mechanism choice is explicitly marked
  non-blocking in the design's ACKNOWLEDGED section ("both produce identical results for every real
  seat-id shape... the spec stage picks the idiomatic JS equivalent").
- Empty `head` (empty `seatId`, or a `seatId` starting with `-`) → `"?"`.
- 8-entry label map, each key mapping to itself (`codex/agy/pi/gemini/cursor/grok/kimi/claude`).
- **Unknown prefix → the lowercased segment itself (`head`), not the original-case string** — the
  corrected behavior (remediation item 22): e.g. `"Foo-bar-1"` → `"foo"`, not `"Foo"`. `head` is
  already lowercased before this fallback, so `return labels[head] || head;` is correct as written
  — no separate re-lowercasing needed at the return site.
- Module-scope (not inside `init()`) — a pure function, exported.

### 3.2 `visibleSeats(seatMap, statusFilter, agentFilter)` — new

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

Parameter named `seatMap`, not `seats` (spec-panel nit, pi-GLM) — the `init()` closure already has a
variable named `seats`; naming this parameter identically would shadow it within the function body,
harmless today (the function is pure and never references the closure) but a needless readability
trap for a future edit. All call sites (§3.13, §5.1) pass the closure's `seats` object positionally
— the argument's own name at the call site is unaffected by this rename.

Exact pins:

- **Filters on the raw `state` field** (`seat.state || "unknown"`, the same fallback
  `renderSeats()`'s badge already uses) — **explicitly does not** reclassify a stale-but-`running`
  seat the way Go's `effectiveState`/`visibleSeats` does. This is a stated non-goal (design's final
  ACKNOWLEDGED section), not an oversight: implementing client-side staleness reclassification tied
  to the age-tick timer is a separate, larger feature nothing in this round's brief asked for.
- **Returns an unsorted, filtered array** — `renderSeats()` (§3.7) applies the existing
  `last_event_ts`-descending sort to this narrowed set afterward. Filter-then-sort and
  sort-then-filter produce the same order since the sort is stable, but the design states the order
  explicitly so this isn't left to be re-derived.
- No `dedupSeatRuns`/orchestrator-vs-run-seat split (Go's `visibleSeats` does both) — out of scope,
  matching the design's stated scope boundary; this is a pure render-time status/agent narrowing
  only.
- Module-scope, pure, exported.

### 3.3 `deriveAgentOptions(seats)` — new

```js
function deriveAgentOptions(seats) {
  const set = new Set();
  Object.values(seats).forEach((seat) => {
    set.add(agentOf(seat.seat_id || ""));
  });
  return ["all", ...Array.from(set).sort()];
}
```

Dedup + sort + prepend `"all"` — byte-for-byte the same derivation as Go's `agentCycle()`
(`model.go:1294-1305`), computed over **every currently-loaded seat** (not the already-filtered
set) so the dropdown always offers every agent actually present, regardless of the current filter
selection. Module-scope, pure, exported.

### 3.4 `isTruncatableOutput(data)` — CORRECTED after spec-panel review (kind-only was wrong)

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

**This section originally shipped a kind-only version (no `apply_patch` check at all) — the spec
panel found it wrong, unanimously (3 of 4 reviewers independently, the 4th's more lenient read
traced to verifying a different, deliberately-mismatched test fixture instead of the real one).
Corrected here; do not revert to the kind-only version described in an earlier draft of this
section.**

The kind-only version assumed `formatTimelineEvent`'s three plain-text kinds
(`command_output`/`command_finished`/`tool_output`) and its `apply_patch` branch
(`tool_name === "apply_patch"`) are mutually exclusive categories on any real `data` object. **They
are not.** `formatTimelineEvent`'s branching is an `if`/`else if` chain that checks `tool_name`
**first** (`app.js:178`: `if (toolName === "apply_patch" && meta.file)`), before ever looking at
`kind`. So an entry with **both** `tool_name: "apply_patch"` and `kind: "command_finished"` renders
via the `apply_patch`/`<details>` branch — but a kind-only `isTruncatableOutput` would still return
`true` for it (its `kind` matches one of the three plain-text values), telling `collapseOutput` to
truncate the RENDERED string, which is actually `<details>`-wrapped HTML for this entry. This isn't
a hypothetical edge case: it's the exact, already-committed shape of the existing test fixture at
`tests/arb_memory/test_visibility_web_contract.py:311-320` (`"kind": "command_finished",
"tool_name": "apply_patch", ...`) — a kind-only check would misclassify this real, currently-used
fixture and truncate its `<details>` block mid-tag, dropping the closing `</details>` — precisely
the HTML-cutting hazard three design panel rounds believed they'd eliminated. The corrected function
above explicitly excludes anything matching the SAME condition `formatTimelineEvent` uses to decide
the apply_patch branch fires — mirroring the real branch priority instead of assuming the two checks
never overlap. `model_thinking` still needs no explicit exclusion (its `kind` was never one of the
three truncatable values, so the kind-only check already excluded it correctly — only the
`apply_patch`/`tool_name` overlap was the actual gap).

**This also corrects an inaccurate premise in the design's third-round remediation** ("the remaining
truncatable kinds produce plain text... no HTML structure to cut through" — this is only true once
`isTruncatableOutput` itself carries the explicit `apply_patch` exclusion above; kind-narrowing alone
does not guarantee it, since `kind` and `tool_name` are independent fields that can co-occur). The
design doc has been updated with a pointer to this correction.

### 3.5 `collapseOutput(data, rendered, expanded)` — new, ported from `model.go:1076-1087`

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

Exact pins:

- **Operates on the already-rendered/escaped `rendered` string**, never on `data.content` — this is
  the final, correct pipeline position (round 3, reverting round 2's ill-fated pre-formatting
  detour). `data` is read only by `isTruncatableOutput(data)` inside this function — never
  mutated, never written back to.
- Short-circuit shape matches Go exactly: `expanded || !isTruncatableOutput(data)` → return
  `rendered` unmodified. Only when truncatable AND not expanded AND the split has more than 6 lines
  does the hint get appended.
- **The hint span is appended AFTER the string is fully rendered/escaped** — it is never itself
  passed through `escapeHtml`, so `<span class="dim">` renders as live markup, not literal escaped
  text (the second of round 3's two identified defects, now structurally impossible since this
  function runs strictly after `formatTimelineFrame` returns).
- Exact hint text, verbatim: `"… +N line(s) — click Expand output to see more"` where `N` is
  `hidden` — wrapped in `<span class="dim">…</span>`.
- `MAX_COLLAPSED_OUTPUT_LINES = 6` is a module-scope `const`, **not exported** (no test needs to
  read the constant directly; behavior is asserted via a 7+-line fixture).
- Module-scope, pure except for reading the module const, exported.

### 3.6 `timelinePrefix(data, options)` — changed (private, not exported — unchanged export status)

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

Exact pins (remediation item 7):

- Three conditional array slots, in order: timestamp (gated on `showTimestamps`), source label and
  kind label (both gated on `showLabels`) — each slot is `""` when its toggle is off **or** when
  the underlying value itself is empty (matches today's `.filter(Boolean).join(" ")` behavior,
  which already drops empty strings).
- `options` may be `undefined` (existing call sites, and the four-existing-tests case) — every read
  goes through `options && options.X`, matching the existing `options && options.escapeContent`
  guard already at this line — never throws.
- Both toggles off (or `options` undefined) → prefix is `""`, i.e. the transcript line is just the
  detail content with no leading space (since `formatTimelineEvent`'s final
  `[prefix, detail].filter(Boolean).join(" ")` drops an empty prefix entirely).
- `formatTimelineEvent`/`formatTimelineFrame` (`app.js:167`, `200`) — **no signature or body
  change** beyond what already flows through unchanged: they already pass `options` straight to
  `timelinePrefix`. The new `showTimestamps`/`showLabels` keys are just additional keys on the same
  `options` object these functions already thread through — nothing else in either function
  changes.

### 3.7 `appendTimelineFrame(timelineEl, frame, options)` — changed signature (new 3rd parameter)

```js
function appendTimelineFrame(timelineEl, frame, options) {
  // Escape-by-default (spec-panel hardening, codex's catch): if a future call site omits `options`
  // or forgets `escapeContent`, this must still escape — an opt-OUT model (explicit
  // `escapeContent: false` required to skip escaping) is the safe default for a function that
  // inserts content via `insertAdjacentHTML`. The one existing production call site (§3.9/§3.11)
  // always passes `escapeContent: true` explicitly; this default only protects a call site that
  // doesn't.
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

Exact pins:

- **New third parameter, `options`** — the caller (inside `init()`, `selectSeat`'s SSE callback,
  §3.9) now builds this object from the **current toggle-state variables** at call time
  (`{ escapeContent: true, showTimestamps, showLabels, expanded }`), not the previously-hardcoded
  `{ escapeContent: true }`. This is what makes a frame arriving right after a toggle click render
  consistently with what `rerenderTranscript()` just produced (remediation item 15's third-round
  correction: "unchanged" described the append *mechanism* only, never the options object).
- **The plain-text branch** (`frame.data.source !== "transcript"`, e.g. an error/other event) also
  now receives `options` instead of no argument — harmless, since `formatTimelineFrame`'s
  non-transcript branch (`"[" + event + "] " + formatJson(data)`) never reads `options` at all, so
  behavior there is unchanged either way; passing it through is just for call-site uniformity.
- **The truncation step (`collapseOutput`) runs only in the HTML-insertion (transcript) branch**,
  immediately after `formatTimelineFrame` returns and before `insertAdjacentHTML` — a new step
  wedged between the two, not a modification to either existing function's internals (remediation
  item 8, unchanged position across all three panel rounds).
- `timelineEl.insertAdjacentHTML`/`textContent +=`/`scrollTop` mechanism is otherwise byte-identical
  to today.

### 3.8 New closure state, DOM references, and persistence helpers (all inside `init()` unless noted)

**Persistence helpers — module scope, not exported** (remediation item 11):

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

**New DOM element references, added to `init()`'s existing declaration block** (alongside
`tokenInput`/`clearToken`/.../`historyStatusEl`):

```js
const statusFilterEl = document.getElementById("status-filter");
const agentFilterEl = document.getElementById("agent-filter");
const filterCountEl = document.getElementById("filter-count");
const toggleTimestampsButton = document.getElementById("toggle-timestamps");
const toggleLabelsButton = document.getElementById("toggle-labels");
const toggleExpandButton = document.getElementById("toggle-expand");
const toggleFullWidthButton = document.getElementById("toggle-fullwidth");
```

**New closure state variables, added alongside the existing `let selectedTaskId = "";` block:**

```js
let statusFilter = readPersisted("visStatusFilter", "all");
let agentFilter = readPersisted("visAgentFilter", "all");
let showTimestamps = readPersisted("visShowTimestamps", "false") === "true";
let showLabels = readPersisted("visShowLabels", "false") === "true";
let expanded = readPersisted("visExpanded", "false") === "true";
let fullWidth = readPersisted("visFullWidth", "false") === "true";
let transcriptBuffer = [];
```

**Per-key read-default table** (remediation item 11, spelled out per key):

| `localStorage` key | Read expression | Default | Backing variable |
|---|---|---|---|
| `visStatusFilter` | `readPersisted("visStatusFilter", "all")` | `"all"` | `statusFilter` |
| `visAgentFilter` | `readPersisted("visAgentFilter", "all")` | `"all"` | `agentFilter` |
| `visShowTimestamps` | `readPersisted(...) === "true"` | off (`false`) | `showTimestamps` |
| `visShowLabels` | `readPersisted(...) === "true"` | off (`false`) | `showLabels` |
| `visExpanded` | `readPersisted(...) === "true"` | off (`false`) | `expanded` |
| `visFullWidth` | `readPersisted(...) === "true"` | off (`false`) | `fullWidth` |

**Initial DOM sync, at the end of `init()`'s setup (before the first `loadOrchestrators()` call):**

```js
statusFilterEl.value = statusFilter;
toggleTimestampsButton.setAttribute("aria-pressed", String(showTimestamps));
toggleLabelsButton.setAttribute("aria-pressed", String(showLabels));
toggleExpandButton.setAttribute("aria-pressed", String(expanded));
toggleFullWidthButton.setAttribute("aria-pressed", String(fullWidth));
seatPanelEl.hidden = fullWidth;
```

(`agentFilterEl.value` is **not** set here — it's reconciled by `updateAgentFilterOptions()`, §3.10,
which the first `renderSeats()` call already triggers once seats start loading.)

### 3.9 Filter-select and toggle-button click handlers (inside `init()`)

```js
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

Exact pins:

- **Only Timestamps/Labels/Expand-output call `rerenderTranscript()`** (§3.10). **Full-width never
  does** — it only flips its own `aria-pressed` state and `seatPanelEl.hidden`, matching
  remediation item 18 exactly (the reconciliation of the first remediation round's self-contradicting
  "all four rerender" / "Full-width doesn't need it" statements).
- **`seatPanelEl.hidden = fullWidth`** is the entire full-width mechanism — no new CSS class, no
  `display:none` rule to write: `<aside id="seat-panel">` has no author CSS setting `display`, so
  the browser's built-in `[hidden]{display:none}` UA rule applies automatically, identical in
  spirit to how `#auth-banner`'s existing `hidden` attribute already works on this page. A hidden
  `<aside>` reports `scrollHeight === clientHeight === 0`, closing off the scroll-listener trigger
  path structurally (§3.11 covers the other trigger path).
- Every write pairs a state flip with its own `writePersisted` call — no batching, no debounce
  (matches the existing `token` key's "write on every change" pattern).

### 3.10 `rerenderTranscript()` — new, inside `init()`

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
```

Exact pins:

- **Clears `#timeline` via `timelineEl.replaceChildren()`** (no arguments — empties all children),
  **not** `textContent = ""` — matching how transcript content was actually inserted
  (`insertAdjacentHTML`, i.e. HTML, not plain text), per remediation item 17.
- **Re-maps every buffered frame through the exact same two-step pipeline as the live-append path**
  (§3.7's transcript branch): `formatTimelineFrame(frame, options)` then `collapseOutput(frame.data,
  rendered, expanded)`. Both call sites build their `options` object the same way
  (`{ escapeContent: true, showTimestamps, showLabels, expanded }`) from the same closure variables
  — this identity is what the design means by "the SAME formatTimelineFrame(frame, options) +
  truncation pipeline used by the live-append path" (not a shared helper function; two call sites
  running identical logic, kept in sync by this spec pinning both explicitly).
- **Buffer entries are whole `frame` objects**, not `frame.data` — `formatTimelineFrame(frame,
  options)` needs `frame.event` for its non-transcript fallback branch (moot for buffered entries,
  since only `source === "transcript"` frames are ever buffered, per §3.11 — but the buffer stores
  the whole object so this function's signature never has to special-case a buffered vs. live
  frame).
- Only called from the three toggle handlers in §3.9 — never from `appendTimelineFrame` itself,
  never from the age-tick timer, never automatically on frame arrival.

### 3.11 Transcript buffer — push site and the seven clearing sites (corrected from five — spec-panel fix, cold-Opus)

**Push site — `selectSeat`'s SSE callback** (`app.js:573-584` today), changed:

```js
function selectSeat(taskId) {
  if (!taskId || taskId === selectedTaskId) {
    return;
  }
  selectedTaskId = taskId;
  timelineEl.textContent = "";
  transcriptBuffer.length = 0; // (1) clearing site
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

- **Only `source === "transcript"` frames are pushed** — mirrors the exact condition
  `appendTimelineFrame` already branches on to decide HTML-insertion vs. plain-text-append (remediation
  item 14). Error frames and other non-transcript SSE events are appended live but never buffered.
- Push happens **before** the (unchanged-mechanism) `appendTimelineFrame` call — order doesn't
  matter functionally (buffer and live-DOM-append are independent side effects on the same frame),
  written in this order for readability.

**The seven clearing sites — every existing site that CLEARS OR REPLACES `timelineEl`'s content,
not just the five `timelineEl.textContent = ""` occurrences:**

The design's item 16 rule ("cleared at every site that clears `timelineEl`, without exception") was
instantiated against a `grep` for the literal `timelineEl.textContent = ""` pattern — which finds
sites that clear to EMPTY, but misses two sites that REPLACE the timeline's content with an error
message (`timelineEl.textContent = "[error] ..."`, an assignment, not the `+=` append the
seat-stream's own error branch uses at `app.js:668` above — that append-onto-existing-content site
is fine as-is and needs no buffer-clear, since it doesn't wipe anything). Both missed sites matter
for the same reason the original five do: if either fires while old transcript content is buffered,
a LATER toggle click's `rerenderTranscript()` would call `timelineEl.replaceChildren()` and silently
overwrite the displayed error message with stale buffered frames — undoing the error display the
code just went out of its way to show.

| # | Function | Line (current `app.js`) | Change |
|---|---|---|---|
| 1 | `clearToken`'s click handler | `403` | add `transcriptBuffer.length = 0;` next to the existing `timelineEl.textContent = "";` |
| 2 | `setSeatSource` | `469` | same |
| 3 | `selectSeat` | `569` | same (shown inline above) |
| 4 | `openOrchestrator` | `602` | same |
| 5 | `loadOrchestrators`'s 401/403 branch | `644` | same |
| 6 | `startOrchestratorStream`'s non-auth error branch | `621` | add `transcriptBuffer.length = 0;` next to `timelineEl.textContent = "[error] " + frame.data.message + "\n";` |
| 7 | `loadOrchestrators`'s `!response.ok` (non-401/403) branch | `651` | add `transcriptBuffer.length = 0;` next to `timelineEl.textContent = "[error] /orchestrators " + response.status + "\n";` |

**Rule, not an enumerated list going forward:** the transcript buffer is cleared at every site that
clears OR REPLACES `timelineEl`'s content, without exception, present or future (remediation item
16, corrected scope) — this table is the concrete instantiation of that rule against the code as it
exists today, not a substitute for it. The rule's original wording ("every site that clears
`timelineEl`") already implied sites 6-7 — they were missed only because the verification method
(a grep for one specific string pattern) undercounted the rule's own intent, not because the rule
itself was wrong.

### 3.12 `openOrchestrator` — filter-reset-on-switch, buffer clear

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
  renderSeats(); // repopulates/reconciles #agent-filter via updateAgentFilterOptions (§3.10)
  stopOrchestratorStream = startOrchestratorStream(orchestratorId);
}
```

Exact pins:

- **`isOrchestratorSwitch` is computed BEFORE `selectedOrchestratorId` is reassigned** — the check
  is `selectedOrchestratorId !== "" && selectedOrchestratorId !== orchestratorId`: true only when
  an orchestrator was already selected AND it's a different one. On the very first call after a
  page load (`selectedOrchestratorId === ""`), this is `false` — filters are left exactly as
  `readPersisted` set them at `init()`, never reset.
- **On a genuine switch, `statusFilter`/`agentFilter` reset to `"all"`**, persisted immediately, and
  `statusFilterEl.value` is set directly (its options are static). `agentFilterEl.value` is **not**
  set directly here — `renderSeats()` at the end of this function calls
  `updateAgentFilterOptions()` (§3.10), which always includes `"all"` in its derived option list, so
  the reconciliation there correctly selects it.
- Everything else in this function is unchanged from today except the added
  `transcriptBuffer.length = 0;` line (clearing site #4, §3.11).

### 3.13 `renderSeats()` — filtering, count badge, agent-dropdown reconciliation

```js
function renderSeats() {
  const total = Object.keys(seats).length;
  const filtered = visibleSeats(seats, statusFilter, agentFilter);
  const orderedSeats = filtered.slice().sort((a, b) => {
    return String(b.last_event_ts || "").localeCompare(String(a.last_event_ts || ""));
  });
  seatsEl.replaceChildren(
    ...orderedSeats.map((seat) => {
      // unchanged from today — same 5-span button markup, same field assignments
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

let lastAgentOptionsKey = null; // spec-panel fix (cold-Opus) — see note below

function updateAgentFilterOptions() {
  const options = deriveAgentOptions(seats);
  const optionsKey = options.join(" ");
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
    // Only reset+persist when there's real seat data to validate against (see note below).
    agentFilter = "all";
    agentFilterEl.value = "all";
    writePersisted("visAgentFilter", "all");
  }
  // else: options === ["all"] only (no seats loaded yet) — leave agentFilter and its persisted
  // value untouched; see the empty-seats-window note below.
}
```

**`lastAgentOptionsKey` guard is a spec-panel addition (cold-Opus's catch), not in the original
draft:** `renderSeats()` — and therefore `updateAgentFilterOptions()` — runs on every SSE frame,
history page, mode toggle, AND the existing 2s age-tick timer (`setInterval`, unconditional,
regardless of whether any seat actually changed). Without this guard, `agentFilterEl.replaceChildren(...)`
would tear down and rebuild the `<select>`'s children every 2 seconds even when the derived option
list is identical — which, on some browsers, closes an operator's currently-open dropdown mid-click.
The guard skips the DOM rebuild (but still runs the fallback-to-`"all"` reconciliation below it,
which is cheap and doesn't touch the DOM unless the selected value actually needs to change) when
the derived option list hasn't changed since the last call, compared via a cheap joined-string key
(agent labels never contain a NUL byte, so ` `-joining is a safe, collision-free comparison
key).

**The `options.length > 1` guard on the reset branch is a SECOND, distinct spec-panel fix
(cold-Opus's second catch on this same function, from the plan-panel round) — do not conflate it
with the `lastAgentOptionsKey` guard above; they close two different bugs.** `openOrchestrator`
(already-existing code, `app.js:610` today) calls `renderSeats()` — and therefore
`updateAgentFilterOptions()` — **immediately after clearing `seats` to empty**, before
`startOrchestratorStream` has delivered a single real frame. At that moment
`deriveAgentOptions({})` returns exactly `["all"]`. Without this guard, if an operator had a
persisted `agentFilter` of, say, `"codex"` from a prior session, `options.includes("codex")` is
`false` (the only option is `"all"`) — the original `else` branch fired unconditionally,
**immediately overwriting the persisted `"codex"` preference with `"all"` in `localStorage`**,
moments before the real SSE stream would have delivered codex seats and proven the preference was
perfectly valid. This would silently destroy a persisted agent-filter choice on **every single page
load / orchestrator entry**, defeating the entire point of persisting it. The `options.length > 1`
guard defers the reset until there's actually more than just the trivial `"all"` option to
validate against — i.e., until real seat data has loaded at least once.

**Mandatory test (plan-panel fix, cursor spike's second, independent pass — this was described
here in prose but never concretized as an actual test until that catch):** call
`updateAgentFilterOptions()` (or trigger it via `renderSeats()`) with `seats = {}` and
`localStorage`'s persisted `visAgentFilter` pre-set to a non-`"all"` value (e.g. `"codex"`); assert
the persisted value is unchanged afterward. A test that never exercises the empty-`seats` case
does not actually prove this guard works — it would pass identically whether or not the
`options.length > 1` check exists, since the guard's fix is invisible except in that exact
window.

Exact pins:

- **`total`** = `Object.keys(seats).length` (every currently-loaded seat, no filters, no dedup —
  the design's scope explicitly excludes Go's `dedupSeatRuns`/`distinctSeatTotal` complexity).
  **`filtered`** = `visibleSeats(seats, statusFilter, agentFilter).length`. Badge text: `filtered +
  "/" + total`, e.g. `"7/23"`.
- **Filter-then-sort**: `visibleSeats` narrows first (unsorted), then the existing
  `last_event_ts`-descending `.sort()` runs on the narrowed array (`.slice()` first since
  `visibleSeats` already returns a fresh array from `.filter()`, but `.slice()` is cheap insurance
  against future callers passing a shared reference — `.sort()` mutates in place).
- **`updateAgentFilterOptions()` runs on every `renderSeats()` call** — every SSE frame, history
  page, mode toggle, and age-tick — matching the design's "recomputed on every seat-list change."
  If the currently-selected `agentFilter` is no longer present in the freshly-derived option list
  (its seats scrolled out / were replaced), it silently falls back to `"all"`, persists that
  fallback, and updates the `<select>` — never leaves a phantom selected value with no matching
  rows.
- Seat-row markup (button structure, field assignments) is **completely unchanged** from today —
  only the input array (`orderedSeats`, now filtered) and the three lines after `seatsEl.replaceChildren`
  are new.

### 3.14 `shouldAutoFetchHistoryPage` — `fullWidth` guard (remediation item 2)

```js
function shouldAutoFetchHistoryPage({ seatSource, historyHasMore, historyLoading, historyCursor, scrollHeight, clientHeight, fullWidth }) {
  if (seatSource !== "history" || !historyHasMore || historyLoading || !historyCursor || fullWidth) {
    return false;
  }
  return scrollHeight <= clientHeight;
}
```

Exact pins:

- **New parameter key: `fullWidth`**, read from the same object-destructuring parameter as the
  existing `seatSource`/`historyHasMore`/`historyLoading`/`historyCursor`/`scrollHeight`/
  `clientHeight` keys — added to the short-circuit `if`, not the final `scrollHeight <=
  clientHeight` line (matches the existing guard style: any disqualifying condition returns `false`
  immediately).
- **Call site** (`applyHistoryPage`, unchanged otherwise) passes `fullWidth` from the same closure
  scope as the other toggle-state variables:
  ```js
  shouldAutoFetchHistoryPage({
    seatSource, historyHasMore, historyLoading, historyCursor,
    scrollHeight: seatPanelEl.scrollHeight, clientHeight: seatPanelEl.clientHeight,
    fullWidth,
  })
  ```
- **The scroll listener itself (`seatPanelEl.addEventListener("scroll", ...)`) is unchanged** — it
  doesn't call `shouldAutoFetchHistoryPage` (it inlines its own equivalent condition) and needs no
  `fullWidth` check: a `hidden` `<aside>` structurally cannot emit a `scroll` event, closing that
  trigger path for free. Only the viewport-fill fallback (a plain function call, not a DOM event) needed
  the explicit gate, since hiding the element does not stop a JS function from being called.

---

## 4. Interfaces block

### New exported pure functions

| Function | Signature | Returns |
|---|---|---|
| `agentOf` | `agentOf(seatId: string) => string` | agent label (`"opus"`, one of 8 known labels, lowercased unknown prefix, or `"?"`) |
| `visibleSeats` | `visibleSeats(seats: Record<string, object>, statusFilter: string, agentFilter: string) => object[]` | unsorted array of seat objects passing both filters |
| `deriveAgentOptions` | `deriveAgentOptions(seats: Record<string, object>) => string[]` | `["all", ...sortedDistinctAgents]` |
| `isTruncatableOutput` | `isTruncatableOutput(data: object) => boolean` | `true` iff `data.kind` is `command_output`/`command_finished`/`tool_output` |
| `collapseOutput` | `collapseOutput(data: object, rendered: string, expanded: boolean) => string` | `rendered` unchanged, or truncated to 6 lines + a `.dim`-wrapped hint |

### Changed exported functions

| Function | Old signature | New signature | What changed |
|---|---|---|---|
| `appendTimelineFrame` | `appendTimelineFrame(timelineEl, frame)` | `appendTimelineFrame(timelineEl, frame, options)` | new 3rd param threaded to `formatTimelineFrame` and (transcript branch only) `collapseOutput` |
| `shouldAutoFetchHistoryPage` | `({seatSource, historyHasMore, historyLoading, historyCursor, scrollHeight, clientHeight})` | adds `fullWidth` key | returns `false` whenever `fullWidth` is true |

`formatTimelineEvent`/`formatTimelineFrame` — **signatures unchanged**; behavior changes only via
`timelinePrefix`'s new conditional logic (§3.6), reached through the same `options` parameter these
functions already accept and pass through.

### New non-exported (private) functions/helpers

| Function | Scope | Purpose |
|---|---|---|
| `readPersisted(key, fallback)` | module | guarded `localStorage.getItem`, defaults on throw/absence |
| `writePersisted(key, value)` | module | guarded `localStorage.setItem`, swallows throw |
| `rerenderTranscript()` | inside `init()` | re-renders the whole buffered transcript through the current toggle state |
| `updateAgentFilterOptions()` | inside `init()` | repopulates `#agent-filter`, reconciles selected value with fallback-to-`"all"` |

### `module.exports` — full new list (15 existing + 5 new = 20)

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

The first 15 entries are byte-identical to today's list and order (`app.js:692-708`); the 5 new
entries are appended at the end, in the same order they're introduced in this spec (§3.1-3.5).

---

## 5. Complete test list

All new tests follow the existing harness's exact pattern: Python `subprocess.run([node, "-e",
script], ...)`, `require()`-ing `app.js` directly (or, for the buffer/rerender and CSS-selector
tests, the existing `_DOM_HARNESS_JS` fake-DOM harness already used by
`test_appjs_clear_token_stops_streams_and_prevents_repopulation` and
`test_appjs_history_toggle_fetches_and_paginates_without_live_rows_or_duplicates`). File:
`tests/arb_memory/test_visibility_web_contract.py` — all new tests appended, no new test file.

### 5.1 New pure-function tests

1. **`test_appjs_agent_of_ported_cases`** — `agentOf` full case list per the design's Testing
   section: `agentOf("cold-opus-1") === "opus"`; `agentOf("") === "?"`; each of the 8 known labels
   (`agentOf("codex-1") === "codex"`, ... `agentOf("claude-1") === "claude"`); an unknown prefix
   lowercased, `agentOf("Foo-bar-1") === "foo"` (not `"Foo"`).
2. **`test_appjs_visible_seats_filters_by_status_and_agent`** — build a small `seats` object (4-5
   entries spanning `running`/`done`/`failed`/missing-`state` and `codex-*`/`agy-*`/unknown-prefix
   `seat_id`s); assert `visibleSeats(seats, "all", "all")` returns all of them; `visibleSeats(seats,
   "running", "all")` returns only the running one; a seat with no `state` key is included only
   when `statusFilter === "unknown"`; `visibleSeats(seats, "all", "codex")` returns only
   `codex`-prefixed seats via `agentOf`.
3. **`test_appjs_derive_agent_options_dedupes_sorts_and_prepends_all`** — seats with `seat_id`s
   `["codex-1", "codex-2", "agy-1", "Foo-1"]`; assert `deriveAgentOptions(seats) === ["all", "agy",
   "codex", "foo"]` (deduped, sorted, `"all"` first, unknown prefix lowercased).
4. **`test_appjs_collapse_output_truncates_long_plain_text_output`** — `data = {kind:
   "command_output"}`, `rendered` = 9 lines joined by `\n`; `collapseOutput(data, rendered, false)`
   returns the first 6 lines + `\n<span class="dim">… +3 line(s) — click Expand output to see
   more</span>`; `collapseOutput(data, rendered, true)` (expanded) returns `rendered` unchanged;
   `collapseOutput({kind: "command_output"}, "one\ntwo", false)` (≤6 lines) returns it unchanged.
5. **`test_appjs_collapse_output_excludes_apply_patch_and_model_thinking`** — `data = {kind:
   "command_finished", tool_name: "apply_patch", meta: {file: "x.py"}}` (the REAL fixture shape,
   matching `test_visibility_web_contract.py:311-320` exactly — both `kind` and `tool_name` set
   together) with a 9-line `rendered` string containing a `<details>...</details>` block; assert
   `collapseOutput(data, rendered, false) === rendered` (untouched — `isTruncatableOutput` returns
   `false` for this `data` because of the explicit `tool_name === "apply_patch"` exclusion in §3.4,
   checked BEFORE the `kind` match, **not** because "its check is kind-only" — that framing was the
   bug the spec panel caught; a naive kind-only check would have returned `true` for this exact
   fixture and this assertion would have failed). Same assertion for `data = {kind: "model_thinking"}`
   (no `tool_name`) with a `<details>`-wrapped 9-line `rendered` string — this one WAS already
   correctly excluded by the kind-only check alone, since `"model_thinking"` was never one of the
   three truncatable kind values. Proves the corrected scope structurally can't cut through a
   `<details>` tag for either excluded case.
6. **`test_appjs_is_truncatable_output_field_scope`** — `isTruncatableOutput({kind:
   "command_output"}) === true`; same for `"command_finished"`/`"tool_output"`; **the load-bearing
   case, added by the spec panel's fix:** `isTruncatableOutput({kind: "command_finished", tool_name:
   "apply_patch", meta: {file: "x.py"}}) === false` — both fields set together (the real fixture
   shape), proving the exclusion check fires even though `kind` alone would match; `false` for
   `{kind: "apply_patch"}` (this `kind` value never occurs in practice but must not accidentally
   match); `isTruncatableOutput({tool_name: "apply_patch", meta: {file: "x"}}) === false` (no `kind`
   set — the exclusion fires on `tool_name` alone, independent of whether `kind` also matches);
   `isTruncatableOutput({kind: "command_output", tool_name: "apply_patch", meta: {}}) === true` —
   `tool_name === "apply_patch"` **without** `meta.file` does NOT trigger the exclusion (matches
   `formatTimelineEvent`'s own `meta.file`-truthy requirement exactly), so a `kind`-matching entry
   with an incomplete apply_patch tag still counts as truncatable; `isTruncatableOutput(null) ===
   false`; `isTruncatableOutput({}) === false`.

### 5.2 Updated existing tests (exact new expected values)

7. **`test_appjs_formats_transcript_timeline_kinds`** — the test's `samples.map(formatTimelineFrame)`
   call is **unchanged** (still no `options` argument — `Array.prototype.map` actually passes
   `(element, index, array)`, so `formatTimelineFrame`'s `options` parameter receives the numeric
   index, but every property read on it — `options.showTimestamps`/`showLabels`/`escapeContent` —
   resolves to `undefined` on a boxed `Number`, so this quirk is harmless and pre-existing). With
   both new toggles defaulting off, the prefix (timestamp + source + kind) is dropped from every
   sample. New expected array:
   ```python
   assert json.loads(completed.stdout) == [
       "hello ‹redacted›",
       "<details><summary>thinking</summary>\nchecking plan\n</details>",
       "edited `foo.py` +3/-1\n<details><summary>diff</summary>\npatch\n</details>",
       "bash\n$ pytest\npassed",
       "Read",
   ]
   ```
8. **`test_appjs_appends_transcript_details_as_html`** — both `appendTimelineFrame(timeline, {...})`
   calls gain an explicit 3rd argument, `{ escapeContent: true }` (matching the new signature; the
   test doesn't exercise the toggle-state keys, only escaping, so `showTimestamps`/`showLabels`/
   `expanded` are omitted — all falsy, matching the off-by-default behavior). With the prefix
   dropped (same reasoning as test 7) and no truncation applying (both fixtures are short and/or
   fall outside the narrowed `isTruncatableOutput` scope — the second sample's `kind` is
   `"command_finished<script>"`, not `"command_finished"`, so it doesn't match regardless), the
   `<br><br>`-suffixed HTML calls lose their leading timestamp/label text:
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
9. **`test_appjs_exports_full_contract_surface`** — expected names list gains the 5 new exports
   (still sorted, still asserting `all(t == "function" for t in result["types"].values())`):
   ```python
   expected = sorted([
       "authHeaders", "appendTimelineFrame", "escapeHtml", "formatTimelineEvent",
       "formatTimelineFrame", "isRealEventId", "parseFrames", "reduceSeat", "streamSSE",
       "ageLabel", "utcDayMonth", "seatAgeLabel", "isStaleHistoryGen",
       "isScrolledNearBottom", "shouldAutoFetchHistoryPage",
       "agentOf", "visibleSeats", "deriveAgentOptions", "collapseOutput", "isTruncatableOutput",
   ])
   ```
10. **`test_appjs_viewport_fill_fallback_triggers_additional_fetch`** — `base` gains
    `fullWidth: False`; every existing case's expected value is unchanged (a `False` `fullWidth`
    changes nothing). Add one new case proving the guard fires:
    ```python
    base = {
      "seatSource": "history", "historyHasMore": True, "historyLoading": False,
      "historyCursor": "cur-1", "scrollHeight": 100, "clientHeight": 400, "fullWidth": False,
    }
    # ...existing cases unchanged, plus:
    "fullWidthHidesFetch": shouldAutoFetchHistoryPage({**base, "fullWidth": True}),
    ```
    ```python
    assert result["fullWidthHidesFetch"] is False
    # ...existing assertions unchanged (fitsViewport True, overflowsViewport False, etc.)
    ```

### 5.3 New DOM-harness (`_DOM_HARNESS_JS`) tests

11. **`test_appjs_transcript_buffer_rerenders_on_toggle`** — using the existing DOM harness
    (`makeDom`, `makeSseResponse`, `flush`): open an orchestrator, select a seat, push 2-3
    `transcript`-source SSE frames with distinct `ts`/`source`/`kind` values (Timestamps/Labels
    both off, the default), assert the initial `timelineEl` HTML contains no timestamp/label text;
    dispatch a `click` on `#toggle-timestamps`; assert `timelineEl`'s content **now** contains the
    `ts` value for **every** already-displayed frame (not just frames arriving after the click) —
    this is the core proof that `rerenderTranscript()` retroactively re-renders existing content,
    not just future appends. Push one more frame after the toggle; assert it also shows its
    timestamp (proves the live-append options-threading fix, §3.7/§3.9).
12. **`test_appjs_transcript_buffer_cleared_on_seat_and_orchestrator_switch`** — push transcript
    frames for seat A, toggle Timestamps on (buffer now has content that would render with
    timestamps), switch to seat B (`selectSeat`), toggle Timestamps off then on again (forces a
    `rerenderTranscript()`); assert seat A's old buffered frames never reappear in seat B's
    timeline. Repeat the same shape for an orchestrator switch (`openOrchestrator`) and for `Clear`
    (`clearToken`) — one assertion per site is enough to prove the buffer (not just the DOM) was
    actually emptied, since a stale buffer would only become visible on the next
    `rerenderTranscript()` call, which this test forces.
13. **`test_appjs_full_width_hides_seat_panel_and_blocks_fetch_burst`** — toggle mode to history
    with a `has_more: true`, short-first-page fixture (reusing the existing history-pagination
    fixture shape from `test_appjs_history_toggle_fetches_and_paginates_without_live_rows_or_duplicates`);
    click `#toggle-fullwidth`; assert `dom.get("seat-panel").hidden === true`; assert no additional
    `historyUrls` fetch was issued as a direct result of the full-width toggle (proving
    `shouldAutoFetchHistoryPage`'s new guard actually suppresses the burst-fetch this remediation
    item exists to close) — then click `#toggle-fullwidth` again and assert `hidden === false`.

### 5.4 New `index.html` string-assertion tests

14. **`test_index_html_filter_bar_markup_and_css`** — assert `'<div id="filter-bar">'`,
    `'<select id="status-filter">'`, all 8 `<option value="...">...</option>` status strings in the
    exact order given in §2, `'<select id="agent-filter">'`, `'<span id="filter-count">'`, and the
    `#filter-bar`/`#filter-bar select`/`#filter-count` CSS selectors from §1.2 are present.
15. **`test_index_html_transcript_toolbar_markup_and_css`** — assert `'<div id="transcript-toolbar">'`
    is immediately followed (modulo whitespace) by `'<pre id="timeline">'` as the next sibling-ish
    element inside `<section>`; all 4 buttons present with `aria-pressed="false"` and no `disabled`
    attribute; `#transcript-toolbar`/`#transcript-toolbar button[aria-pressed="true"]`/`.dim`
    selectors present.
16. **`test_index_html_css_layout_restructure`** — assert `"calc(100vh - 73px)"` does **not** appear
    anywhere in the file (both prior occurrences removed); assert `"html, body{ height:100%; }"`
    (or equivalent whitespace-normalized match), `"flex:1 1 auto; min-height:0;"` appears at least
    twice (once for `main`, once for `#timeline`), and `"display:flex; flex-direction:column;"`
    appears for both `body` and `section`.

### Test commands

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/arb_memory/test_visibility_web_contract.py -q
```

Same single invocation as every prior stage — one file, no new fixtures, no new framework.

---

## 6. What does NOT change

- Backend (`visibility.py`) — untouched; every filter/toggle is 100% client-side.
- Auth model, SSE mechanics, live/history state machine (`setSeatSource`/`fetchHistoryPage`/
  `applyHistoryPage`/`historyGen`) — untouched except the five buffer-clearing insertions (§3.11)
  and `openOrchestrator`'s filter-reset addition (§3.12), neither of which touches the history/live
  transition logic itself.
- `reduceSeat`, `parseFrames`, `streamSSE`, `authHeaders`, `escapeHtml`, age-formatting functions —
  byte-identical to today.
- No keyboard shortcuts, no copy-transcript/copy-line-range feature — explicit scope decisions
  carried over unchanged from the design.
- The token/auth architecture question (passphrase+TOTP browser login) — out of scope, untouched.
- `agentOf`'s exact prefix-extraction mechanism vs. Go's `SplitN` — both produce identical results
  for every real seat-id shape in this codebase (design's own ACKNOWLEDGED note); no further
  reconciliation needed.
- No buffer size limit on `transcriptBuffer` — matches Go's own unbounded `m.transcript` retention
  for a seat's whole runtime; bounded in practice by the existing per-seat clear-on-switch behavior.

---

## Spec decisions flagged as least certain

1. **RESOLVED by spec-panel review — `isTruncatableOutput`'s scope (§3.4) was neither of the two
   readings originally weighed here.** The original spec draft picked reading (b) below (pure
   omission, no `apply_patch` awareness at all) — the spec panel found this wrong, unanimously
   (3 of 4 reviewers independently, verified against the actual, already-committed test fixture at
   `test_visibility_web_contract.py:311-320`): `kind` and `tool_name` are independent fields that
   CAN co-occur on a real entry (that exact fixture has both `kind: "command_finished"` and
   `tool_name: "apply_patch"` set together), so pure kind-based omission doesn't actually exclude
   apply_patch — it just fails to notice apply_patch entries that happen to also carry a
   kind value in the truncatable set. The actual correct answer was a third option neither (a) nor
   (b) below anticipated: an **explicit exclusion check** on `tool_name === "apply_patch" &&
   meta.file`, mirroring `formatTimelineEvent`'s own branch-priority condition exactly (see §3.4's
   current text). Original framing preserved below for the historical record of what was debated;
   §3.4 itself is the corrected, final version.
   - The design's "Scope" prose could be read two ways: (a) the ported function is a byte-for-byte
     match of Go's two-part OR-check, and some *other*, undescribed guard in the pipeline excludes
     `apply_patch` from actually being truncated; or (b) the ported function itself simply omits
     that branch, making the exclusion structural. Neither is what §3.4 now does.
2. **`appendTimelineFrame` gaining a third `options` parameter, rather than some other mechanism for
   threading current toggle-state into the live-append path.** The design says the live-append call
   site "must build its options object from the CURRENT toggle-state variables" but doesn't specify
   *how* that object reaches `appendTimelineFrame` itself — I chose the most direct route (a new
   parameter) over alternatives like a module-level mutable options object or a closure-returning
   factory. This changes an existing exported function's arity, which is exactly the kind of change
   that breaks call sites silently if a future edit forgets the third argument (mitigated here by
   the existing-tests-must-be-updated list in §5.2, but a plan-stage implementer should be aware
   this is an arity change, not just a body change).
3. **All of §1.2/§1.3's CSS (the `#filter-bar`/`#transcript-toolbar` selectors and property values)
   and the seven new DOM element `id`s (`status-filter`, `agent-filter`, `filter-count`,
   `toggle-timestamps`, `toggle-labels`, `toggle-expand`, `toggle-fullwidth`) are my own invention** —
   the design pins the *behavior* and *general visual treatment* ("secondary button + `aria-pressed`,
   matching what's shipped") but not literal selector names or exact CSS property values for these
   two new zones. They're internally consistent with the existing token set and I believe
   defensible, but — like the prior stage's own flagged dark-palette invention — worth a quick
   visual/naming check before the plan stage treats these exact strings as unchangeable.

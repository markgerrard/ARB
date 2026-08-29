# ARB Visibility web page — restyle + history parity — design

## Purpose & scope

The ARB Visibility gateway (`src/arb_memory/visibility.py`) serves a small operator page
(`src/arb_memory/static/index.html` + `app.js`) for watching bridge seats: paste a Bearer token,
pick an orchestrator, watch seats populate live, click one to stream its transcript. Reported
symptom was "doesn't work at all"; backend investigation (curl against the live prod gateway)
found the backend is correct and current — `/`, `/app.js`, `/orchestrators`, both SSE routes all
work, and the served static files are byte-identical to the repo. The one **confirmed** gap:
`index.html`/`app.js` never got the history endpoint added 2026-07-03
(`GET /orchestrators/{orchestrator_id}/seats/history`) — the companion Go TUI (`tools/arb-watch-go`)
already has live/history parity; the web page does not.

This design is one cohesive change covering all three asks together, because they touch the same
files and the same render path:

1. **Full visual restyle** to the "door page" aesthetic (`src/arb_memory/mcp/login.py`'s `<style>`
   block) — paper background, ink text scale, one clay accent, serif/mono pairing.
2. **History-view parity** with the Go TUI: a live/history toggle, keyset-paginated fetch,
   scroll-to-load-more, `dd/mm` age formatting for old seats.
3. **Whatever the live path needs fixed**, if a real browser check turns up a client-side bug the
   curl-only diagnosis couldn't see (see Risks).

Explicitly **not** in scope (confirmed correct, unchanged): the auth model (Bearer token via
`Authorization` header, `localStorage`-persisted), the hand-rolled SSE-over-`fetch` parser
(`EventSource` can't send custom headers, so this stays), and all backend routes/contracts.

## What's genuinely new vs. restyled

| Area | Restyled only | New |
|---|---|---|
| Header (title, token input, orchestrator select, clear button) | Yes — door tokens, layout unchanged | Auth-failure banner (new element) |
| Seat sidebar (`#seats`) | Yes — card/list restyle | Age column (client didn't render age at all before); live/history mode; status-neutral badges |
| Timeline/transcript pane | Yes — typography, spacing | No new behavior |
| Live/history toggle | — | New: header control + client state machine |
| History fetch + pagination | — | New: `fetch` to the existing endpoint, scroll-triggered "load more", generation-guarded |
| `reduceSeat` wiring | — | **No change** — see decision below |
| Backend (`visibility.py`) | — | **No change** — routes, auth, SSE, and the history endpoint all already exist and are correct per the diagnosis |

## Read-first grounding that shapes this design

- `_reduce_seat` (backend) computes final `state` **before** the SSE frame is sent — the client
  always receives an already-reduced seat object with a non-empty `state` field on every
  `seat_appear`/`seat_update`/`seat_finish` event. `app.js`'s `reduceSeat` re-derives the same thing
  client-side but `openOrchestrator` stores `frame.data` directly and never calls it — so it isn't
  actually dead in the sense of "unreachable and untested." `tests/arb_memory/test_visibility_web_contract.py::test_appjs_parser_and_reducer_match_visibility_reducer`
  imports `reduceSeat` from `app.js` via Node and asserts it produces **byte-identical** output to
  the Python `_reduce_seat` over a shared fixture — it's a **parity contract test**, not a UI code
  path. Deleting `reduceSeat` breaks that test; wiring it into `openOrchestrator` would apply it to
  data that's already fully reduced, so it's provably a no-op on every real frame today.
- `tools/arb-watch-go/model.go` (the shipped, panel-reviewed Go history feature — see
  `docs/superpowers/specs/2026-07-03-arb-watch-history-design.md`'s "Warm-orchestrator remediation"
  section) settled on **single-map-replace**, not a second parallel `historySeats` map, after two
  panel rounds flagged that a second map creates readers-of-the-wrong-map bugs. `model.go`'s
  `toggleHistoryMode()` confirms this: toggling to history leaves the live SSE connection running
  but its frames are dropped (`if m.seatSource == "live" { m.upsertSeat(...) }` guards the only
  write site) while `m.seats` is wholesale-replaced by fetched pages; toggling back to live clears
  `m.seats` and **restarts** the orchestrator SSE stream from scratch (fresh backfill, no resume
  cursor) rather than trying to reconcile a stale cursor. This design ports that exact mechanism to
  JS rather than re-deriving a JS-native scheme, because it's already been through two review
  rounds against exactly these race conditions.
- The web page **does not display seat age at all today** (`renderSeats()` renders seat-id, state
  badge, run-id, last-event — no age). Porting the `dd/mm`-after-24h rule requires adding an age
  column, which is new UI, not a restyle of an existing one.
- `eval_event_raw` (the table the history endpoint queries) was created 2026-06-23/25 — about 10
  days of history exist as of this writing. See the age-format decision below.

## Design tokens (copied verbatim from `login.py`)

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

Same Google Fonts `<link>` pair (`Newsreader` + `IBM Plex Mono`, `preconnect` for both hosts).
**No new hues are introduced** (see the status-badge decision below) — this is a deliberate
constraint, not an oversight: the door page's restraint principle is "clay accent only for focus
rings and the primary action," and a dashboard with 7 seat states is exactly the kind of surface
that tempts a maintainer into a rainbow status-color system that would clash with that principle.
This design resolves that tension by keeping every state badge neutral (mono, ink-500 border,
ink-700 text, `--paper-sunk` fill) except the one state that's actually actionable
(`failed`, clay-700) and one motion-based (not color-based) signal for `running` (see below).

**Explicit deviation, called out rather than silently done:** the door page is a single centered
card; this page is a dashboard (header + two-pane body). Adapted structure, not copied wholesale:

- `body` — `background: var(--paper)`, drop `color-scheme: light dark` and the `Canvas`/`CanvasText`
  system-color usage entirely. The door page is a fixed light theme; matching its *feel* means
  matching that it doesn't auto-invert either. (This is one of the two decisions flagged as least
  certain — see the summary.)
- `header` — becomes a `--paper-sunk` bar, `border-bottom: 1px solid var(--line-200)`, the existing
  CSS grid layout kept (title, token, orchestrator, mode toggle, clear), each `label` text becomes
  the mono uppercase eyebrow style (`font-family:var(--mono); font-size:.6875rem;
  text-transform:uppercase; letter-spacing:.1em; color:var(--ink-500)`) already used for `label` in
  `login.py`. The `<h1>` becomes serif, `color:var(--ink-900)`, no longer trying to look like body
  text — a small brand mark ("ARB · Visibility"), not the 3.25rem login splash size (this is a
  dense dashboard header, not a hero).
- `input#token`, `select#orchestrator` — take `login.py`'s `input.t` treatment verbatim:
  `font-family:var(--mono); background:var(--paper-sunk); border:1px solid var(--line-300);
  border-radius:5px; padding:11px 13px`, focus state `border-color:var(--clay-600);
  background:var(--card); box-shadow:0 0 0 3px var(--clay-100)`. `select` gets the same box
  treatment (borders/radius/padding/focus ring); native `<select>` can't take the mono placeholder
  letter-spacing trick but keeps the same font/color/border family so it doesn't look foreign next
  to the token input.
- Buttons (`Clear`, the new mode toggle) — the door page's primary button style
  (`background:var(--ink-900); color:var(--paper)`) is reserved for a true primary action; there
  isn't one on this page (nothing here is "the" submit). Buttons instead get a **secondary** variant
  not present in `login.py` but consistent with its restraint: `background:var(--card);
  border:1px solid var(--line-300); color:var(--ink-700)`, same mono-uppercase label, hover
  `background:var(--paper-sunk)`, `:focus-visible { outline: 2px solid var(--clay-600);
  outline-offset: 2px }` (copied verbatim from login.py's focus-visible rule). The **active** state
  of the live/history toggle (whichever mode is selected) uses `background:var(--clay-100);
  border-color:var(--clay-600); color:var(--clay-700)` — this is the one place besides `failed`
  that clay shows up, and it's a selection state, which is squarely "the primary action" territory
  the door page reserves clay for.
- `aside` (seat sidebar) — `background:var(--card)`, `border-right:1px solid var(--line-200)`, each
  `#seats li` gets `border-bottom:1px solid var(--line-200)`; the selected seat
  (`aria-current="true"`) gets `background:var(--paper-sunk)` (not `Highlight`/clay — reserving clay
  for the failed/active-toggle cases keeps it meaningful when it does appear).
- `section`/`#timeline` — `background:var(--paper)`, transcript text `font-family:var(--mono)`
  (this is log/code content, matches the door page's mono-for-technical-content convention already
  visible in its `input.t`), `color:var(--ink-700)`, `<details><summary>` (diff/thinking
  disclosures already emitted by `formatTimelineEvent`) get `color:var(--ink-500)` for the summary
  label so collapsed blocks read as secondary until expanded.
- Card padding/radii: 8px radius for pane-level containers (aside/section borders), 5px for
  interactive controls (input/select/button) — matching `login.py`'s `.card` (8px) vs `input.t`/
  `button` (5px) split exactly.

### State badges (no new hues)

| state | rendering |
|---|---|
| `done` | neutral badge (ink-500 border, ink-700 text, paper-sunk fill) |
| `incomplete` | neutral badge (same as `done`) — mirrors the Go TUI's already-settled "historical, not alive" semantics for this exact state (see the arb-watch-history design's remediation notes) |
| `unknown` | neutral badge |
| `stale` | neutral badge, but `color:var(--ink-400)` (muted further) — "gone quiet," not alarming |
| `voted` | neutral badge; `stance` (if present) appended in parens, still neutral text |
| `running` | neutral badge **plus** a small pulsing dot in `--clay-600` to its left (CSS `@keyframes` opacity pulse, ~1.6s, `prefers-reduced-motion` disables the animation but keeps the static dot) — motion is the "alive" signal, not a new hue |
| `failed` | badge border/text in `--clay-700` — the one state that gets a color, because it's the one state an operator needs to notice first |

## Layout

Two-pane body is unchanged structurally (`aside` sidebar + `section` timeline, same
`grid-template-columns: minmax(260px, 34%) 1fr`, same `@media (max-width: 720px)` stacking) — the
brief's "restyle, not rebuild" instruction applies most directly here. Header gains one control:

```
[ ARB · Visibility ]   [ Token input ]   [ Orchestrator ▾ ]   [ Live | History ]   [ Clear ]
```

The mode toggle is a two-segment button group (`Live` / `History`), not a checkbox or a single
toggle button with changing text — a segmented control makes the *current* mode legible at a glance
(the active segment gets the clay-selected treatment above), which a single button whose label
flips between "Live"/"History" doesn't. It's disabled (both segments dimmed, non-interactive) until
an orchestrator is selected, matching that `h` is only live in the Go TUI once inside `viewSeats`.

### Auth-failure banner (new)

Today: a bad token produces a silent JSON `401` written into the timeline `<pre>` as plain text
(`"[error] /orchestrators 401"` or `"[error] {message}"` from the SSE error frame) — easy to miss,
inconsistent with the door page's clean failure handling (`login.py`'s `login_post` returns a
distinct `403`/`429` page rather than dumping a stack trace). This design adds a dismissible banner
directly under the header, shown when `/orchestrators` or either SSE stream returns 401:

```html
<div id="auth-banner" hidden role="alert">
  Unauthorized — check your token and try again.
</div>
```

Styled `background:var(--clay-100); border:1px solid var(--clay-600); color:var(--clay-700);
font-family:var(--mono); text-transform:uppercase; letter-spacing:.06em; font-size:.75rem`
— matches the clay-for-attention convention above (this is the same semantic category as the
`failed` badge: something needs the operator's attention). Shown/hidden by JS, not a fixed part of
the timeline text stream; existing timeline-text error writes for non-auth errors (network failure,
5xx) are unchanged — the banner is specifically for "your token is the problem," which is the one
failure mode the door page already has a clean convention for (a rejected credential, not a system
error).

### Token input

Stays a raw paste box (`type="password"`, unchanged `localStorage` persistence, unchanged `Clear`
semantics) — restyled per the tokens above but not replaced with a friendlier flow. There is no
underlying credential-exchange flow to build a nicer entry around (a `vis-` token is a long-lived
opaque bearer string minted out-of-band by an operator, not a user password with a login endpoint
behind it), so a nicer input widget would be decoration with no new capability. Adding one is
explicitly rejected as scope creep here (YAGNI) — the auth-failure banner above is the meaningful UX
gap for this control, not the input's appearance.

## History UI

### Toggle placement & behavior

The `Live`/`History` segmented control in the header (above). Mirrors the Go TUI's `h` key
conceptually, but as a mouse-driven page, a labeled, always-visible toggle beats a keybinding an
operator has to discover.

### State machine (single-map-replace, ported from the reviewed Go design)

`seats` stays the **one** object it is today (keyed by `task_id`); no second `historySeats` map.
New client state: `seatSource` (`"live" | "history"`), `historyCursor`, `historyHasMore`,
`historyLoading`, `historyGen` (an integer, bumped on every mode toggle *and* every orchestrator
switch).

- **Toggle to History:** bump `historyGen`. The orchestrator SSE connection is **not** stopped —
  its `onEvent` callback gates its only write (`seats[seat.task_id] = seat`) behind
  `seatSource === "live"`, so incoming live frames are silently dropped while in history mode
  (matching `model.go`'s `if m.seatSource == "live" { m.upsertSeat(...) }` guard exactly). Clear
  `seats`, set `historyLoading = true`, fetch page 1 (`cursor` omitted), render a "loading
  history…" state in the sidebar. On response (guarded by `historyGen` — see below): replace
  `seats` wholesale from the page, set `historyCursor`/`historyHasMore` from the response,
  `historyLoading = false`, `renderSeats()`.
- **Toggle to Live:** bump `historyGen`. Clear `seats`. **Restart** the orchestrator SSE connection
  from scratch (stop the existing `streamSSE` handle, call `openOrchestrator`'s stream-start again
  with no `Last-Event-ID`) rather than trying to resume a stale cursor — this reproduces the Go
  side's fresh-backfill choice exactly, and for the same reason: reconciling "what happened while
  we were in history mode" against a resumed cursor is unnecessary complexity when the gateway
  already does a full `SSE_BACKFILL_COUNT`-bounded backfill on every fresh connect.
- **Orchestrator switch (via the `<select>`) while in History mode:** bump `historyGen`, reset
  `seatSource` to `"live"` (a new orchestrator has no reason to inherit the previous one's history
  view), clear `seats`, start a fresh live SSE connection — i.e., an orchestrator switch behaves
  exactly as it does today, plus resetting the mode.
- **Stale-fetch guard:** every history fetch closes over the `historyGen` value active when it was
  issued. On resolution (success or error), compare to the *current* `historyGen`; a mismatch means
  the mode was toggled or the orchestrator changed since — discard the result as a no-op. This is
  the same mechanism `model.go`'s `historyPageMsg`/`historyErrMsg` handlers use
  (`if msg.gen != m.historyGen { return m, nil }`), chosen over a per-fetch `AbortController`
  because it needs to cover three distinct trigger sources (mode toggle, orchestrator switch, and
  scroll-triggered pagination) uniformly with one comparison, rather than wiring cancellation
  through three different call sites. An `AbortController` is not wrong here, just a second,
  independent mechanism doing the same job as the counter that already has to exist for the render
  gate — one mechanism for one job.

### Pagination ("more available, scroll to load")

A scroll listener on the sidebar (`aside`, or a wrapper if the header ever grows a sticky part):
when `scrollTop + clientHeight >= scrollHeight - <threshold>` (threshold ~40px, so it fires
slightly before the literal bottom) **and** `seatSource === "history"` **and** `historyHasMore`
**and** `!historyLoading` — set `historyLoading = true`, fetch the next page with
`cursor = historyCursor`, and on success **append** the returned seats into the existing `seats`
object (not replace) plus update `historyCursor`/`historyHasMore`, guarded by the same
`historyGen` check as above.

No dedicated "load more" button and no page-number UI — matches the Go TUI's infinite-scroll
pattern (reusing the down-arrow/page-down handling there) and is the natural mouse/scroll
equivalent. A small `"· history"` status line at the bottom of the sidebar shows
`"loading…"` while `historyLoading` is true, or nothing when `historyHasMore` is false (matching
the Go filter bar's `"· history"` marker so history mode is visually obvious even scrolled away
from the toggle).

Because the web page always re-renders the sidebar from `Object.values(seats)` sorted by
`last_event_ts` descending (existing `renderSeats()` behavior, unchanged), appending an older page
into `seats` requires **no separate ordering array** — newly-appended (older) rows simply sort to
the bottom on the next render. This is a real simplification versus the Go TUI, which needs
`historyOrder`/`seatOrder` arrays because its viewport renders a windowed slice of a fixed-height
terminal pane; the browser sidebar is a normal scrolling DOM list with no such windowing, so the
existing sort-on-render is sufv cient on its own.

### History row visual difference

A history-mode seat row is visually identical to a live one **except**:
- the age column may show a `dd/mm` date instead of a relative age (below), and
- the sidebar's bottom status strip shows `"· history"` while in this mode.

No separate visual treatment (dimming, a border, an icon) marks individual rows as
"historical" — the mode-level toggle state and the `"· history"` marker are the single source of
truth for "what am I looking at," matching the Go TUI's choice not to re-signal mode per-row.

## Age formatting

New column in each seat row, computed client-side from `last_event_ts` (present on every seat
object from both the live-reduce and history-row shapes — same field name, per the brief).

```js
function ageLabel(seconds) {
  if (seconds < 60) return Math.floor(seconds) + "s";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m";
  return Math.floor(seconds / 3600) + "h";
}
```

— identical to `tools/arb-watch-go/reduce.go`'s `ageLabel`. For a **history-mode** row whose age is
`>= 24h` (`86400` seconds), render `dd/mm` (zero-padded day/month, e.g. `"03/07"`) from
`last_event_ts` instead of the relative string — matches `model.go`'s
`if ok && m.seatSource == "history" && ageSecs >= 24*3600 { ageLbl = ts.Format("02/01") }` exactly,
including that this substitution **only applies in history mode** (a live seat is never 24h stale
by definition — `STALE_GRACE_S` is 120 seconds — so the branch is dead in live mode by
construction, same as the Go side).

**No year component.** `eval_event_raw` — the table the history endpoint reads — was created
2026-06-23/25; as of this design there is at most ~10 days of queryable history, nowhere near a
year. Adding a year field now would be speculative complexity with nothing to validate it against.
Deferred, with a code comment at the `dd/mm` formatter noting the assumption ("no year component —
revisit if this table holds more than ~11 months of history") so it isn't silently forgotten if the
gateway is still running a year from now.

## State/reduce parity — `reduceSeat` decision

**Keep `reduceSeat` unwired; do not delete it; do not call it from `openOrchestrator`.**

The backend (`_reduce_seat` in `visibility.py`) already computes final seat state before emitting
each SSE frame — every frame's `data` is a complete, already-reduced seat object. `openOrchestrator`
storing `frame.data` directly (`seats[seat.task_id] = seat`) already produces the correct shape;
calling `reduceSeat(seats[seat.task_id] || {}, seat)` on top of that would be reducing
already-reduced data, which is provably a no-op for every real frame (the function's own logic only
changes fields when `entry` carries an `event_type`/`data` payload distinct from what's already
merged in — and here `entry` *is* the full state, so every field it would set, it's setting to the
value it already has). Wiring it in would add a call that changes nothing at runtime, purely "for
symmetry" — exactly the YAGNI case the brief calls out.

It is **not**, however, truly dead code: `tests/arb_memory/test_visibility_web_contract.py::test_appjs_parser_and_reducer_match_visibility_reducer`
imports `reduceSeat` via Node and asserts it stays byte-identical to Python's `_reduce_seat` over a
shared fixture. That's a real, valuable parity guard against the two implementations silently
drifting — it just guards a function that isn't (and shouldn't be) on the live-mode hot path.
Recommendation for the plan stage: add a one-line comment above `reduceSeat` in `app.js` stating
this explicitly ("kept for the JS/Python parity contract test; not called by the live SSE path,
which already receives fully-reduced state from the backend"), so a future reader doesn't
"helpfully" wire it in or delete it without knowing why.

## What does NOT change

- Auth model: Bearer token via `Authorization` header, `localStorage`-persisted, `Clear` button
  semantics.
- SSE frame-parsing approach: hand-rolled parser over `fetch` (`parseFrames`, `streamSSE`'s
  backoff/reconnect loop) — required because `EventSource` cannot send custom headers. The client
  already backs off (500ms → doubling → capped 5s) and retries on stream failure; this design does
  not touch that logic unless a real-browser check (see Risks) turns up an actual bug in it.
- Backend routes and contracts — `/`, `/app.js`, `/orchestrators`, both SSE routes, and the history
  endpoint all already exist, are correct, and need no changes. This is a client-only change.
- The exported contract-tested functions in `app.js` (`parseFrames`, `reduceSeat`, `isRealEventId`,
  `formatTimelineEvent`, `formatTimelineFrame`, `appendTimelineFrame`, `escapeHtml`,
  `authHeaders`, `streamSSE`) keep their existing signatures and behavior — new functions (age
  formatting, history-fetch, the generation guard) are additive, ideally exported alongside them in
  the same `module.exports` block so the plan stage can contract-test them the same way.

## Error handling summary

| condition | current behavior | new behavior |
|---|---|---|
| Bad/missing token on `/orchestrators` | JSON `401` dumped as plain text into the timeline | Auth banner shown; sidebar/timeline cleared as today |
| Bad token on orchestrator SSE | `[error] {message}` written into timeline | Auth banner shown (401 case); non-auth SSE errors keep the existing inline `[error]` text — this is a genuine stream problem, not a credentials problem |
| Bad token on seat SSE | Same as above | Same treatment |
| SSE disconnect (network blip, gateway restart) | Client backs off (500ms→5s) and retries automatically | **Unchanged** — no evidence this is broken; the diagnosis didn't touch it and a real backoff loop already exists |
| History fetch fails (network/5xx) | N/A (doesn't exist yet) | Status text in the sidebar ("history unavailable"), existing loaded rows stay visible, `historyLoading` clears so scrolling/toggling again can retry |
| History fetch 401/403 | N/A | Auth banner (same as live 401) |
| Malformed/expired history cursor (400 from the endpoint) | N/A | Treated like a fetch failure — status text, no crash; this shouldn't happen in normal use since the client only ever passes back a cursor it just received |

## Testing (manual click-through for the next stage)

1. Load the page fresh (empty `localStorage`) — restyle renders correctly with no token entered
   (empty state, no console errors).
2. Enter a valid Bearer token → orchestrator dropdown populates.
3. Select an orchestrator → seat sidebar populates from the live SSE stream; age column shows
   relative ages ticking up.
4. Click a seat → transcript pane streams; tool calls/patches/thinking blocks render with the new
   typography.
5. Toggle to History → sidebar clears, shows "loading…", then populates from the history endpoint;
   `dd/mm` dates show for any seat older than 24h (may require test data older than 24h to actually
   exercise this branch — call this out if the available orchestrator's history is all recent).
6. Scroll to the bottom of the history list (with `has_more: true`) → next page loads and appends
   without duplicating or losing rows.
7. Toggle back to Live → sidebar clears and repopulates from a fresh SSE backfill; confirm it still
   updates as new events arrive (not frozen).
8. Enter a deliberately wrong token → auth banner appears (not a silent JSON dump); Clear, then
   re-enter a valid token → banner clears and the page recovers.
9. **Before assuming any of the existing live-mode JS is correct**, load the page in a real browser
   and check the console/network tab — the backend diagnosis was curl-only and could not rule out a
   client-side bug that only manifests in an actual browser (see Risks). If one turns up, it's a
   fix, not a re-design — this design's restyle/history work doesn't depend on the live path being
   bug-free, but the plan stage should not skip this check and assume it is.

## Risks / open questions

- **A real browser-only bug may exist in the live path that curl couldn't surface.** The backend
  diagnosis explicitly could not get an in-browser console/network trace this session. This design
  assumes the live path works as read (it's plausible given the code), but the plan stage must
  verify this empirically before/alongside implementing the restyle — an actual bug found there
  should be fixed alongside this work, not treated as a surprise later.
- **Dropping `color-scheme: light dark` / system colors in favor of the door page's fixed light
  theme** is a real behavior change for anyone currently viewing this page with a dark OS theme (it
  will no longer auto-invert). This matches the door page's own choice, but it's the one visual
  decision here without a fallback if an operator specifically wants dark mode back.
- **The state-badge "no new hues" approach** (neutral badges + clay only for `failed` + a motion
  cue for `running`) is a stronger restraint reading of the door aesthetic than was strictly
  necessary — a reasonable reviewer could argue a dashboard is exactly the kind of page that's
  allowed a small, distinct status-color set even if the door page itself never needed one. Flagged
  as the design choice least likely to survive a design review unchanged.

## Warm-orchestrator remediation (post design-panel)

Four independent reviews: certifying quorum codex + agy-print + pi-GLM (all **PLAN_READY_WITH_NITS**),
plus cold-Opus as a non-certifying contributor (**FIX_BEFORE_PLAN**) — non-certifying because cold-Opus
shares Claude lineage with this design's cold-Sonnet author, per the authoring-rotation quorum-swap rule.
Cold-Opus's two P1s are treated as decisive despite being non-certifying: one is a unique catch the
certifying quorum missed, the other converges with two certifying reviewers' independent findings. Both
were verified directly against the running code before acceptance, not taken on the panel's word. **This
section is authoritative where it differs from the body above.**

### DECIDED — must apply at spec/plan stage

1. **The `reduceSeat`-no-op rationale is corrected, not the decision.** Cold-Opus verified (and I
   independently re-verified against `visibility.py:129–160` + `app.js:84–120`) that `_reduce_seat`'s
   SSE output has **no** `event_type`/`sent_at` keys — it emits `last_event`/`last_event_ts` instead.
   Every one of `reduceSeat`'s branches keys off `entry.event_type`; fed an already-reduced frame, all
   of them silently miss, and `reduced.last_event_ts`/`last_event` fall back to the *previous* value.
   Wiring `reduceSeat` into the live path would **freeze every seat at its first-seen state** — an
   active regression, not a harmless no-op. The decision stands (keep unwired, keep exported for the
   contract test) but the plan-stage guard comment must say *why* in these terms: "do NOT call from the
   live path — it expects raw redis fields (`event_type`/`sent_at`) that a live frame, already reduced
   server-side, does not carry; wiring it in would freeze every seat at its first state." Do not ship
   the original "no-op, kept for symmetry" phrasing anywhere.

2. **`streamSSE` gets a real, scoped fix: distinguish a 4xx from a transient failure and stop
   retrying on it.** This drops the earlier "`streamSSE` stays unchanged" constraint for this one
   behavior — three independent reviewers (agy, pi-GLM, cold-Opus) converged on the same root defect
   from different angles, and pi-GLM's framing is the one to build from: verified directly
   (`app.js:210–261`) that on `!response.ok`, `streamSSE` throws `Error("SSE " + status)`, the `catch`
   emits an `error` event, then **falls through to the reconnect `await`/loop** — nothing sets
   `stopped = true`. A bad/expired/revoked token on either SSE endpoint does not fail once; it hammers
   the gateway with a fresh 401 every 500ms→5s **forever**. This is a real, pre-existing bug the
   curl-only diagnosis could not see, found by static code reading alone (never needed a live browser).
   Fix, mirroring the already-shipped, panel-reviewed Go TUI pattern (`streamFatalEvent`/`s.fatal` at
   `model.go:259–262`):
   - On `!response.ok`, the error event becomes `{ event: "error", data: { message, status:
     response.status } }` — a structured status, not a string to regex-parse.
   - When `status` is in the 4xx range (401 and 403 both — a revoked/expired token can surface as
     either; do not special-case only 401), set a `fatal` flag on that `streamSSE` handle and **do not
     re-enter the reconnect loop** — `stopped = true` after emitting the event, same effect as the Go
     side's "channel close that follows won't reconnect."
   - The auth banner triggers off `data.status === 401 || data.status === 403` on any of: the
     `/orchestrators` fetch (already a clean one-shot status today — `app.js:369`), or either SSE
     stream's now-structured error event. One trigger condition, one code path, both surfaces — the
     earlier plan to special-case "`/orchestrators` is one-shot, SSE loops" is no longer needed once
     the SSE side stops looping on a 4xx.
   - Non-4xx SSE failures (network blip, 5xx) keep the existing inline `[error]` timeline text and
     keep retrying — this fix narrows to "stop hammering on a rejected credential," it does not touch
     the transient-failure backoff/reconnect behavior at all.
   - This is a genuine, in-scope fix per this design's own opening framing ("whatever the live path
     needs fixed, if a real browser check turns up a client-side bug the curl-only diagnosis couldn't
     see") — it just turned out static reading was enough to find one, so the plan stage's live-browser
     check (still required, see Risks) is on top of this fix, not a precondition for it.

3. **Vote metadata (`voted`/`stance`) is explicitly live-only.** Confirmed by three independent
   reviewers (codex, agy, cold-Opus): `_history_seat_state` (`visibility.py:224–233`) has no branch for
   a vote event and `_history_row_to_seat` (`visibility.py:255–266`) never emits `voted`/`stance` —
   history rows structurally cannot carry them. Add one explicit sentence to "History row visual
   difference": *"Vote metadata is live-only; history rows deliberately omit `voted`/`stance`, so the
   `stance`-in-parens badge affordance only ever appears in live mode for a seat that has actually
   voted — this is not a parity bug to chase."*

4. **Dark-mode mitigation is now IN scope, not a deferred risk.** Three of four reviewers (agy,
   pi-GLM, cold-Opus) independently proposed the identical, cheap fix: a
   `@media (prefers-color-scheme: dark)` block swapping the paper/ink token values (e.g.
   `--paper:#1F1D1A; --paper-sunk:#28251F; --ink-900:#FAF8F4; --ink-700:#D9D3C7` — an inverted mapping
   within the same token names, not new colors) for operators with a dark OS theme. ~10-15 lines. This
   replaces the earlier "flagged as an open risk with no fallback" framing — the fallback is now
   specified and in scope for the plan.

5. **`dd/mm` age formatting must use UTC, not local time.** Two independent reviewers (agy,
   cold-Opus) flagged the same real bug risk: the Go sibling formats from a UTC-parsed timestamp
   (`model.go:1176,1457`); a naive JS `new Date(...).getDate()/.getMonth()` renders in the browser's
   local timezone, which can shift the displayed day by one near midnight depending on the operator's
   offset from UTC. Spec/plan must use `getUTCDate()`/`getUTCMonth()` explicitly in the `dd/mm`
   formatter.

6. **An age re-render timer is a real, missing requirement, not an implementation detail.** Cold-Opus
   caught that this design's own acceptance test 3 ("age column shows relative ages ticking up")
   cannot pass as specified — `renderSeats()` only runs on incoming SSE frames and seat selection; for
   a quiet orchestrator, the age text would freeze between events (state transitions to `stale` still
   work, since the *backend* pushes a `stale` frame after grace — only the numeric age freezes). Add a
   periodic re-render (`setInterval`, ~1–2s, matching the Go sibling's 2s tick) to the plan's scope.
   Interacts with the `running` pulse/`prefers-reduced-motion` handling already specified — both are
   driven by the same re-render tick, but `prefers-reduced-motion` only silences the pulse animation,
   not the age-text update.

7. **Toggling mode must explicitly clear `selectedTaskId` and stop the seat-transcript SSE stream.**
   Cold-Opus's "missed entirely": the design says "clear `seats`" on every mode/orchestrator transition
   but doesn't say what happens to a currently-selected seat's transcript stream if that seat isn't in
   the new view — today's `selectSeat` behavior would leave an orphaned stream open against a seat no
   longer in `seats`. Make explicit: every mode toggle and orchestrator switch also clears
   `selectedTaskId` and stops any active `stopSeatStream` handle, same as switching orchestrators
   already does today (`openOrchestrator`'s existing `if (stopSeatStream) { stopSeatStream(); }`).

8. **History pagination needs a viewport-fill fallback, not just a scroll listener.** Cold-Opus's
   "missed entirely": the scroll-triggered fetch (`scrollTop + clientHeight >= scrollHeight -
   threshold`) can never fire if page 1's rows don't overflow the sidebar's visible height — no
   scrollbar, no scroll event, `has_more: true` pages become unreachable. Spec/plan must add a
   fallback: after each page loads, if the sidebar's content height does not exceed its viewport height
   AND `historyHasMore` is true, auto-fetch the next page (repeat until either the content overflows,
   enabling normal scroll-to-load, or `historyHasMore` is false).

### ACKNOWLEDGED — explicitly deferred, not silently dropped

- **No-new-hues status badges** (codex, pi-GLM, cold-Opus all independently flagged this as a
  legitimate, non-blocking disagreement — the strongest three-way convergence in this panel on a pure
  judgment call). Verdict: **keep the design as written for the plan stage**, but this is the design
  decision most likely to be revisited after first real use — flagged for the operator to reassess
  once the restyled page is live, not decided away by panel vote. Rationale for keeping it as-is now:
  the "no new hues" restraint is a real, coherent aesthetic principle (matching the door page), the
  page still surfaces full state text (nothing is lost, only de-emphasized), and a reviewer-majority
  preference is an input to the human decision-maker, not an authority to override a decided design
  choice on a subjective call. If it doesn't hold up in practice, the fix is cheap and additive later
  (muted variants within the existing ink/paper/clay palette, not new hues) — not a redesign.
- **Orchestrator-row segregation** (agy's "missed entirely" — the Go TUI sinks `claude-*` orchestrator
  rows to the bottom of the list; the web sidebar doesn't). Genuinely low-value to replicate for this
  page's scale and audience (YAGNI) — document as an accepted Go-vs-web behavior difference, not a gap
  to close.
- **History/live state vocabularies are disjoint sets** (pi-GLM's "missed entirely": `running`/
  `stale`/`voted` only ever appear live; `incomplete` only ever appears in history). Not a bug — this
  matches the Go TUI's already-settled semantics exactly — but add one clarifying sentence to the state
  badge table so a future reader isn't puzzled why both exist without ever co-occurring for the same
  mode.

### Still open for the plan stage (unchanged from the design's own Risks section)

The **real-browser empirical check** (console/network trace before assuming any existing live-mode JS
is correct) remains required — this panel found the `streamSSE` retry-loop bug through static reading
alone, which is a good sign the diagnosis direction was right, but does not substitute for the actual
browser check. If the plan-stage browser check turns up a *further* client-side bug beyond the one
remediated above, fix it alongside the restyle/history work (per this design's original framing) — it
does not block starting the restyle or history tasks in parallel.

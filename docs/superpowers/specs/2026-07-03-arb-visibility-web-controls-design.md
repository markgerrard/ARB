# ARB Visibility web page — Go-parity controls (filters + transcript toggles) — design

## Purpose & scope

The ARB Visibility web page (`src/arb_memory/static/index.html` + `app.js`) recently shipped a
restyle + live/history parity with the companion Go TUI (`tools/arb-watch-go`,
`docs/superpowers/specs/2026-07-03-arb-visibility-web-design.md`). The Go TUI has a set of
keybinding-driven controls the web page never got. This design ports the ones that translate to a
mouse-driven page:

| Go key | Feature | Ported? |
|---|---|---|
| `s` | Status filter (cycle `all→running→incomplete→done→failed→voted→stale→unknown`) | Yes |
| `a` | Agent filter (cycle through agents present in the current seat list) | Yes |
| `t` | Toggle timestamps in transcript | Yes |
| `l` | Toggle source/kind labels in transcript | Yes |
| `e` | Toggle expand/truncate tool output | Yes |
| `f` | Toggle full-width transcript (hide seat sidebar) | Yes |
| `h` / `m` | History toggle / fleet menu | Already shipped (mode toggle / orchestrator dropdown) |
| `c` / `^C` | Copy transcript / copy line range | Not ported — browsers already do native text-select+copy |
| `←→` | Switch keyboard focus between panes | Not ported — no keyboard-focus concept needed on a mouse-driven page |
| `q` | Quit | N/A — it's a browser tab |

No backend changes. No new files. Everything is additive to `app.js`/`index.html`, following the
same Node-subprocess contract-test pattern already established.

**Explicit divergence from Go, decided during brainstorming:** no keyboard shortcuts are added —
clickable buttons/dropdowns only. This is a mouse-driven page; wiring `t`/`l`/`e`/`f`/`s`/`a` as
global key handlers would add focus-management complexity (avoiding conflicts with typing in the
Token field) for a page that already has a working point-and-click interaction model.

## Layout

Two new UI zones, both between/around the existing header and panes — the two-pane
`aside#seat-panel` / `section#timeline` structure is otherwise unchanged:

```
[ Header: title | token | orchestrator | mode toggle | clear ]
[ Filters: Status[all ▾]  Agent[all ▾]   7/23 seats ]
+----------------------+------------------------------------+
| seat list            | [Timestamps][Labels][Expand output] |
| (aside#seat-panel)    | [Full width]                        |
|                      |--------------------------------------|
|                      | transcript (section > #timeline)     |
+----------------------+------------------------------------+
```

- **Filter bar** (`#filter-bar`, new element between `<header>` and `<main>`): status dropdown,
  agent dropdown, a live `shown/total` count. Mirrors Go's own `renderFilterBar` — a dedicated,
  always-visible zone above the seat list, not folded into the already-busy header.
- **Toggle strip** (`#transcript-toolbar`, new element as the first child of `<section>`, above
  `#timeline`): four pressed-state buttons — Timestamps, Labels, Expand output, Full width. Same
  secondary-button + `aria-pressed` visual pattern already used for the Live/History segmented
  toggle — no new component, consistent with what's shipped.

## Filter bar

### Status filter

Fixed dropdown, values `all, running, incomplete, done, failed, voted, stale, unknown` — **the
`unknown` value was missing from the original design** (see remediation item 5): both
`_history_seat_state` (`visibility.py:224-233`, any unhandled event type) and `renderSeats`'s own
existing `seat.state || "unknown"` fallback can genuinely produce it, so it must be filterable, not
just displayable. Filtering narrows `Object.values(seats)` before `renderSeats()` maps it to DOM
rows — the underlying `seats` object is untouched; this is a pure render-time narrowing, identical
in spirit to Go's `visibleSeats()`. Filter-then-sort ordering: filter narrows the set first, *then*
the existing `last_event_ts`-descending sort applies to the narrowed set (equivalent to sorting
first since the sort is stable, but stated explicitly so the spec author doesn't have to guess).

### Agent filter

**Dynamic** dropdown — recomputed on every seat-list change (new SSE frame, history page load,
mode toggle) from whichever agent prefixes actually appear in the *currently loaded* seats. **This
original text was WRONG** — `agentOf` does not exist anywhere in `app.js` today (verified: zero
matches). It exists only in Go (`tools/arb-watch-go/reduce.go:111-134`) and must be **ported as a
new function**, not treated as already-available — see "Warm-orchestrator remediation" item 1. Once
ported, derivation is deduplicated, sorted, `all` prepended — byte-for-byte the same derivation as
Go's `agentCycle()`. If the currently selected agent value is no longer present after a
re-derivation (e.g. its seats scrolled out), the filter silently falls back to `all` rather than
showing a phantom selected option with no matching rows.

### Shown/total count

A small `X/Y` badge next to the dropdowns (e.g. "7/23"), `Y` = total loaded seat count, `X` = count
after both filters apply. Purely informational, no interaction.

**Filters apply identically in Live and History mode, and are NOT reset by a mode toggle** — same
as Go, confirmed during brainstorming. They're pure view-state over whatever `seats` currently
holds, independent of *how* those seats got there.

**Filters ARE reset to `all`/`all` on a manual orchestrator switch** — verified against Go's
`enterSeats` (`model.go:852-867`), which explicitly resets `statusFilter`/`agentFilter` to
`filterAll` alongside the history-state reset it already does on switch. A different orchestrator's
seats have no reason to inherit the previous one's filter selection — same rationale Go already
applied.

**Resolved conflict with persistence (a real interaction the brainstorm surfaced, not present in
Go):** `openOrchestrator(orchestratorId)` (`app.js:588` today) runs on **every** entry, including
the automatic one right after a persisted token auto-populates the orchestrator dropdown on page
load — Go has no equivalent "page load with a remembered session" concept, so this distinction
didn't need resolving there. If `openOrchestrator` unconditionally reset filters every call, the
localStorage-persisted values would be silently discarded the instant the page loads, defeating the
persistence design above. Resolution: `openOrchestrator` only resets `statusFilter`/`agentFilter`
to `all` when `selectedOrchestratorId` was **already set to a different, non-empty value** before
this call (i.e. a genuine mid-session switch) — checked *before* the existing
`selectedOrchestratorId = orchestratorId` assignment. On the very first call after a page load
(`selectedOrchestratorId === ""`), the persisted filter values from `localStorage` are read and
kept untouched instead.

## Toggle strip

Four buttons, same visual treatment as the existing mode-toggle segment (secondary button style,
`aria-pressed` state, clay-tinted when active). **All four default to OFF**, matching Go's
zero-value defaults — an explicit, discussed tradeoff: the web page *today* shows timestamps and
source/kind labels unconditionally (no toggle exists yet), so shipping with both off is a visible
density change for existing users on the same day this ships. Chosen deliberately for Go parity
over preserving today's incidental always-on behavior.

### Timestamps / Labels

Both apply only to transcript-pane rendering, via `timelinePrefix(data, options)` (existing
function, `app.js:148-155`) — currently **always** includes `ts`/`sent_at` and `source`/`kind`
unconditionally. This becomes conditional on two new `options` keys, `showTimestamps`/`showLabels`,
threaded through the same `options` object `formatTimelineEvent`/`formatTimelineFrame` already
accept (no new parameter — these functions already take an `options` argument for
`escapeContent`; the two new toggles are additional keys on that same object).

**Exact conditional construction (remediation item 7 — the original text left this implicit):**
```
prefix parts, in order, each included only if its toggle is on:
  options.showTimestamps → (data.ts || data.sent_at || "")   [empty string omitted, as today]
  options.showLabels     → (data.source || "event")          [empty string omitted, as today]
  options.showLabels     → (data.kind || data.event_type || "")
parts joined with " ", filtered to drop empties (same .filter(Boolean).join(" ") as today's
unconditional version) — both toggles off → prefix is "" (just the detail content remains).
```
Both `formatTimelineEvent` and `formatTimelineFrame` currently accept `options` but existing call
sites don't always pass one — `appendTimelineFrame`'s plain-text branch calls
`formatTimelineFrame(frame)` with **no** options argument at all (`app.js:213`); `options` is then
`undefined`, and every `options.showTimestamps`/`showLabels` read must short-circuit through
`options &&` guards (matching the existing `options && options.escapeContent` pattern at
`app.js:149`) so this doesn't throw — it correctly resolves to falsy → today's *new* off-by-default
behavior. See Migration note below, and remediation item 3 for which *existing* tests this changes.

### Expand output

**New behavior** — no truncation logic exists in the web client today. Ports Go's
`collapseOutput`/`isTruncatableOutput` — **final resolution below, after three panel rounds each
found one remaining defect in the pipeline position. This is the authoritative version; the
`collapseOutput`/`isTruncatableOutput` names and `(data, rendered, expanded)` signature in the "Data
flow" section below are correct and were right from the start — round 2's detour away from them,
below, was unwinding a problem round 1 had already independently solved.**

**Scope (settled in round 1, unchanged since):**
- `isTruncatableOutput(data)` checks, exactly matching Go's two separate field accesses — do not
  conflate them: `data.kind` is one of `"command_output"`, `"command_finished"`, `"tool_output"`
  **OR** (`data.tool_name === "apply_patch"` **AND** `data.meta?.file` is truthy). Note this is
  `tool_name`, not `kind` — `apply_patch` is never itself a `kind` value.
- **The `apply_patch` branch is excluded from this feature's truncation entirely** — it's already
  natively collapsible via its own `<details>` disclosure (unchanged from today), so a second
  truncation layer on top would be redundant. Expand-output's truncation therefore applies **only**
  to the plain-text kinds (`command_output`/`command_finished`/`tool_output`) — narrower than Go's
  scope, deliberately.

**Pipeline (final, third-round resolution — `collapseOutput(data, rendered, expanded)` operates on
the RENDERED string, matching Go's own signature exactly, not on raw `data.content` beforehand):**
`appendTimelineFrame`/`rerenderTranscript()` call `formatTimelineFrame(frame, options)` completely
unchanged — no pre-truncation, no mutation of `data`/`frame` at any point, ever (this is what
resolves the destructive-mutation bug two rounds of review kept circling: since nothing is ever
truncated *before* storage or *before* formatting, the buffer always holds the pristine original,
and toggling Expand-output back on later trivially shows the full content again — there was never
anything to lose). AFTER `formatTimelineFrame` returns its rendered string: if `isTruncatableOutput(data)`
and `!expanded`, split the RETURNED string on `\n`; if more than `maxCollapsedOutputLines = 6`
(verbatim from Go) lines, keep the first 6, append a hint — `"… +N line(s) — click Expand output to
see more"` wrapped in a **new** `<span class="dim">…</span>` (this class does not exist in the
current stylesheet — it's added by this feature, `.dim{color:var(--ink-500)}`, matching the same
muted-ink color already applied inline to `#timeline details summary` and several other selectors,
not "reused" from an existing class as an earlier draft of this section wrongly implied).

This is safe for exactly the reason `apply_patch`'s exclusion (above) makes it safe: the narrowed
truncatable kinds produce **plain text** in `formatTimelineEvent`'s `detail` assembly
(`[toolName, content].filter(Boolean).join("\n")`, `app.js:192-193`) — no `<details>` tags, no HTML
structure to cut through. Splitting the escaped/rendered string by `\n` is line-count-identical to
splitting the raw content (HTML-escaping never introduces or removes newline characters), so no
truncation boundary can land mid-entity, and the hint span — appended AFTER the string is fully
formatted and escaped — is never itself subject to `escapeHtml`, so it renders as styled markup, not
literal escaped text. Both defects that motivated round 2's move to pre-formatting truncation (the
destructive mutation, and — the thing round 2 didn't anticipate — the hint being escaped away) are
resolved by simply not moving it in the first place, once `apply_patch`'s narrower-scope exclusion
(already decided in round 1) is properly accounted for.

When the Expand-output toggle is on, or the entry isn't in the narrowed truncatable set above, no
truncation step runs at all — the rendered string passes through unmodified, same short-circuit
shape as Go's `expanded || !isTruncatableOutput(data)`.

### Full width

Hides `aside#seat-panel` entirely (`display:none` or equivalent), `section` (the transcript pane)
takes the full two-pane grid width. Click again to restore the two-pane layout. Matches Go's binary
hide/show exactly (confirmed during brainstorming — not a "shrink to a slim strip" middle ground).

**This original claim was WRONG — see "Warm-orchestrator remediation" item 2 below.** A hidden
element can't receive `scroll` events, but the prior feature's viewport-fill *fallback* fires from
a plain JS function call (`applyHistoryPage`), not a DOM event, and does NOT check visibility —
four independent reviewers confirmed this would trigger a rapid burst-fetch of every remaining
history page while full-width is on, not a pause.

## Persistence

All six values persist to `localStorage`, same pattern as the existing `token` key (confirmed
during brainstorming): `visStatusFilter`, `visAgentFilter`, `visShowTimestamps`, `visShowLabels`,
`visExpanded`, `visFullWidth` (naming convention: `vis` + PascalCase field, avoiding collision with
the existing bare `token` key). Read once at `init()`, written on every change. An operator's
preferred view survives a page reload — matches both the existing token-persistence UX and Go's own
within-session state retention (Go doesn't persist across process restarts, but the web page
persisting across reloads is the closest analogous "don't lose my view" behavior for a page that
reloads far more often than a terminal session restarts).

## Data flow / functions touched

- `renderSeats()` (`app.js:407` today) gains a filter step before mapping to DOM rows: compute
  `visibleSeats(seats, statusFilter, agentFilter)` (new, exported, pure — mirrors Go's
  `visibleSeats()` naming) → sorted/filtered array → existing map-to-DOM logic unchanged.
- A new pure function `deriveAgentOptions(seats)` (exported) — dedup+sort+prepend-`all`, the dynamic
  agent-dropdown population, callable independently of `renderSeats()` for testability.
- `formatTimelineEvent`/`formatTimelineFrame` (`app.js:167`, `200`) — `options` gains
  `showTimestamps`/`showLabels`/`expanded` keys; `timelinePrefix` and the new collapse-output logic
  read them off the same `options` object already threaded through.
- `appendTimelineFrame` (`app.js:209`) — currently hardcodes `{ escapeContent: true }` for the
  HTML-insertion path and no options for the plain-text path; both call sites need the current
  toggle state merged into whatever options object they build.
- New: `collapseOutput(data, rendered, expanded)` and `isTruncatableOutput(data)` — direct ports of
  the Go functions of the same name, exported for contract testing.
- New: `#filter-bar` and `#transcript-toolbar` markup in `index.html`, plus their CSS (matching the
  door-page token set — secondary-button treatment for the toggle strip, matching the existing
  `select#orchestrator` treatment for the two new dropdowns).

## What does NOT change

- Backend (`visibility.py`) — untouched. Filters/toggles are 100% client-side.
- The auth model, SSE mechanics, live/history state machine (`setSeatSource`/`fetchHistoryPage`/
  `applyHistoryPage`/`historyGen`) from the prior feature — untouched.
- No keyboard shortcuts (explicit scope decision).
- No copy-transcript / copy-line-range feature (explicit scope decision — native browser
  text-select+copy covers this need).
- The token/auth architecture question raised during this conversation (whether the visibility
  gateway should gain a passphrase+TOTP browser login on top of its existing bearer-token model,
  matching the ARB Memory MCP door) is **explicitly out of scope for this design** — flagged as a
  separate, future design topic, not decided or touched here.

## Testing

Same harness as every prior stage: new pure functions (`agentOf`, `visibleSeats`,
`deriveAgentOptions`, `collapseOutput`, `isTruncatableOutput`) get Node-subprocess contract tests
appended to `tests/arb_memory/test_visibility_web_contract.py`; new HTML/CSS gets plain-Python
string assertions against `index.html`. No new test file, no new framework, no backend test changes.

`agentOf` test cases specifically (per remediation item 21): `"cold-opus-1"` → `"opus"`, `""` →
`"?"`, each of the 8 known labels (`codex`/`agy`/`pi`/`gemini`/`cursor`/`grok`/`kimi`/`claude`), and
an unknown prefix (e.g. `"Foo-bar-1"`) → the **lowercased** prefix (`"foo"`, per item 22's
correction) — not the original-case string.

## Migration note (the one non-obvious risk)

Today's transcript lines always show `ts + source/kind`. After this ships, with Timestamps/Labels
defaulting off, every transcript line gets **shorter** by default — this is intentional (Go parity,
confirmed above) but is the one visible behavior change on ship day for anyone already using this
page. No mitigation planned beyond this design doc calling it out explicitly; an operator who wants
today's density back clicks two buttons once and (per the persistence design above) never has to
again.

## Warm-orchestrator remediation (post design-panel)

Four independent reviews: certifying quorum codex + agy-print (both **FIX_BEFORE_SPEC**) + pi-GLM
(**SPEC_READY_WITH_NITS**, but its own top finding is an unambiguous P0), plus cold-Opus as a
non-certifying contributor (**FIX_BEFORE_SPEC**) — non-certifying because this design's author is
the warm-orchestrator Claude session itself, same lineage as cold-Opus, per the quorum-swap rule.
Every finding below was verified directly against the running code before acceptance — several
(the missing `agentOf`, the transcript-buffer gap) by direct execution/grep, not just review
consensus. This section is authoritative where it differs from the body above; the two inline
corrections already made above (agent filter, status filter, expand-output) are part of this same
remediation pass.

### DECIDED — must apply at spec/plan stage

1. **`agentOf` is a new function to port, not an existing helper** (already corrected inline
   above). Port verbatim from `tools/arb-watch-go/reduce.go:111-134` — prefix-extraction from
   `seat_id` + a label map (`codex`→`codex`, `agy`→`agy`, `pi`→`pi`, `gemini`→`gemini`,
   `cursor`→`cursor`, `grok`→`grok`, `kimi`→`kimi`, `claude`→`claude`, `cold-opus-*`→`opus` special
   case, unknown prefix → the prefix itself, empty → `"?"`). Export it alongside the other new pure
   functions for contract testing.

2. **Full-width must gate BOTH the scroll listener AND the viewport-fill fallback, not just the
   scroll listener.** Four independent reviewers (codex, agy-print, pi-GLM, cold-Opus) converged on
   the same defect from different angles — the most certain finding in this panel. Verified: a
   `display:none` `#seat-panel` reports `scrollHeight === clientHeight === 0`, so
   `shouldAutoFetchHistoryPage`'s `scrollHeight <= clientHeight` check is unconditionally true
   whenever full-width is on and `historyHasMore`/`historyCursor` allow it — `applyHistoryPage`'s
   viewport-fill call (a plain JS call, not a DOM event, so hiding the element does NOT stop it)
   would chain through every remaining history page in a rapid burst the instant full-width is
   toggled on during an in-progress paginated load, or the instant a page lands while full-width is
   already on. This is not "pagination pauses" as originally claimed — it's the opposite, an
   unbounded-until-`has_more`-is-false fetch burst. Fix: thread a `fullWidth` (or equivalent
   "panel is hidden") check into `shouldAutoFetchHistoryPage`'s condition, alongside the existing
   `seatSource`/`historyHasMore`/`historyLoading`/`historyCursor` guards, so it returns `false`
   whenever the panel isn't actually visible — closing both trigger paths (scroll event genuinely
   can't fire on a hidden element; the explicit gate stops the viewport-fill call from ever
   firing) with one condition.

3. **Three existing contract tests need explicit, planned updates — not silent breakage.**
   Cold-Opus's precise citations: `test_appjs_formats_transcript_timeline_kinds`
   (`test_visibility_web_contract.py:353-359`) and `test_appjs_appends_transcript_details_as_html`
   (`:402-414`) both assert the ts/label prefix is present when calling
   `formatTimelineEvent`/`formatTimelineFrame` **without** passing the new toggle options — once
   those default to off, the asserted prefixes are wrong and both tests go red. Separately,
   `test_appjs_exports_full_contract_surface` (`:579-601`) pins the **exact** 15-entry export set
   byte-for-byte — landing `agentOf`, `visibleSeats`, `deriveAgentOptions`, `collapseOutput`, and
   `isTruncatableOutput` (5 new exports, 15→20) breaks it by construction unless updated in the same
   change. The spec/plan must call out updating all three as explicit, planned tasks (with their new
   expected values), not treat them as pre-existing tests that "just keep passing."

4. **`collapseOutput`'s truncation must not risk cutting inside HTML tags** (already corrected
   inline above, cold-Opus's unique catch, confirmed by no other reviewer but well-evidenced and
   clearly correct on inspection). Narrowed to the plain-text-only truncatable kinds; `apply_patch`
   (already `<details>`-wrapped, already natively collapsible) is explicitly excluded from this
   feature's truncation layer rather than risking a second, conflicting collapse mechanism.

5. **`unknown` added to the status filter's fixed value list** (already corrected inline above).
   Two independent reviewers (codex, agy-print) confirmed real event-type gaps in
   `_history_seat_state` and the existing `seat.state || "unknown"` render fallback both produce
   this value in practice — it must be filterable.

6. **Exact `isTruncatableOutput` field-access spec** (already corrected inline above, pi-GLM's
   precise catch): the `kind` check and the `apply_patch`/`tool_name`/`meta.file` check read
   *different* fields — a spec/plan author must not conflate `data.kind === "apply_patch"` (wrong;
   this value never occurs) with `data.tool_name === "apply_patch"` (correct).

7. **Exact `timelinePrefix` conditional construction spec** (already corrected inline above,
   pi-GLM's catch) — the four toggle-combination cases and the empty-options short-circuit are now
   spelled out precisely rather than left as "conditional on two new keys."

8. **Exact pipeline position for truncation** (already corrected inline above, pi-GLM's catch):
   between `formatTimelineFrame`'s return and `appendTimelineFrame`'s DOM insertion — a new step,
   not a modification to either existing function's internals. **This item was correct from round 1
   onward and never needed fixing** — round 2's rewrite of the "Expand output" section
   contradicted THIS item, not the other way around; the third re-panel round reverted "Expand
   output" back to match this item's original position, not vice versa.

9. **Layout height math must be explicitly revised, not left as an implicit gap.** Three
   independent reviewers (codex, agy-print, cold-Opus) confirmed `main{min-height:calc(100vh -
   73px)}` and `#timeline{min-height:calc(100vh - 73px)}` (`index.html`, current) both assume only
   the header consumes vertical space — adding `#filter-bar` (between header and `main`) and
   `#transcript-toolbar` (first child of `section`, above `#timeline`) without updating these breaks
   the intended viewport-fit/pane-scroll model, making the page taller than the viewport. Fix: the
   spec must replace the two hardcoded `calc(100vh - 73px)` magic numbers with a layout that doesn't
   need a fixed pixel offset — e.g. restructure the page as an outer flex column (`html, body {
   height:100%; } body { display:flex; flex-direction:column; }`) where `header`, `#filter-bar` are
   fixed-height siblings and `main` takes `flex:1 1 auto; min-height:0` (letting its own two-pane
   grid, and `#transcript-toolbar` + `#timeline` within the right pane, size to the remaining space
   naturally) — carrying the existing `@media (max-width:720px)` stacking behavior forward
   unchanged in spirit (single column, no fixed-height assumption either).

10. **Clear must not touch the six persisted filter/toggle localStorage keys.** Codex's finding,
    refined by pi-GLM: explicit rule — `clearToken`'s handler clears auth/session/seat state only
    (as already specified in the prior feature); it must NOT reset `visStatusFilter`,
    `visAgentFilter`, `visShowTimestamps`, `visShowLabels`, `visExpanded`, or `visFullWidth`, either
    in `localStorage` or in the in-memory variables backing them. A Clear followed by re-entering
    the same (or a different) token should restore the operator's saved view preferences exactly as
    a plain page reload would.

11. **`localStorage` read-defaults and unavailable-storage behavior must be explicit.** Every read
    is `localStorage.getItem(key) || <default>` (e.g. `"all"` for the two filters, `"false"`/absent
    → off for the four toggles) — spelled out per-key in the spec, not left implicit. If
    `localStorage` throws (privacy mode / disabled), reads fall back to defaults and writes are
    best-effort (wrapped, swallowed) — the page remains fully functional without persistence, same
    fragility class as the existing `token` key already has today (not a new risk, just extending
    an existing one to six more keys, worth being explicit about once since the surface area grew).

12. **The transcript-buffer / re-render architecture gap (found independently by agy-print and
    confirmed by direct execution — `grep` finds zero client-side transcript buffer anywhere in
    `app.js`) is DECIDED to be built, not accepted as a limitation.** `appendTimelineFrame` is
    pure DOM-append (`insertAdjacentHTML`/`textContent +=`) with no retained array of raw frame
    data — so toggling Timestamps/Labels/Expand-output would, if simply wired into the append path
    as originally scoped, only affect lines arriving **after** the toggle changes, leaving every
    already-displayed line stale. Go's actual behavior is the opposite: toggling `t`/`l`/`e`
    retroactively re-renders the *entire* visible transcript, because Go always re-derives display
    from a retained `m.transcript []map[string]any` buffer (`styledBlock`/`plainBlock` read from it
    on every render, never append pre-rendered strings). Given the explicit stated goal of this
    feature round was Go feature parity, and a toggle that visibly does nothing to existing content
    would read as broken rather than as a documented limitation, this design adds the equivalent
    buffer — **exact shape and wiring corrected in the second re-panel round below (item 13); the
    original text here understated it as storing only `frame.data`, which is wrong.** This is the
    single largest scope addition in this remediation pass — flagged explicitly as a decision, not
    slipped in silently, because it changes "wire four toggle buttons" into "wire four toggle
    buttons plus a transcript-buffer/re-render mechanism" for Timestamps/Labels/Expand-output
    specifically (Full-width does **not** call `rerenderTranscript()` — it's a pure layout toggle
    with no transcript-content dependency, only updating its own pressed-state and the panel's
    `display`).

### ACKNOWLEDGED — explicitly deferred, not silently dropped

- **Clear immediately followed by manually switching to a *different* orchestrator** can't be
  distinguished, by the `selectedOrchestratorId === ""` check alone, from "first call after a page
  load" — both look identical, so filters would be kept rather than reset in that specific sequence
  (pi-GLM's finding). Accepted as low-impact: status/agent filter values are generic across
  orchestrators, not orchestrator-specific data, so keeping them in this one edge-case sequence is
  an imprecision, not a functional bug. No fix planned.
- **`incomplete` is a permanently-empty filter value in Live mode** (pi-GLM) — expected, not a bug;
  `incomplete` only ever exists on history rows per the prior feature's own settled state-mapping
  design. No change needed.
- **Whether toolbar buttons disable when no seat/transcript is active** (pi-GLM's "missed
  entirely") — not required. Full-width is orchestrator-scoped (works with or without a selected
  seat); Timestamps/Labels/Expand-output don't crash against an empty `#timeline`, they simply have
  nothing yet to affect.

### Still open for the spec stage (superseded by the second re-panel round below)

None of the above defer further open questions to the spec stage beyond normal task-breakdown — all
twelve DECIDED items above are precise enough to spec directly against. **This was true of the
remediation's intent, but two of the twelve items (4/8 on truncation order, and 12 on the buffer
shape) contained internal contradictions or gaps a second panel round found — see below, which is
now authoritative over this section where they differ.**

## Second design re-panel — remediation of the remediation

A **second** panel round (same certifying quorum: codex + agy-print FIX_BEFORE_SPEC, pi-GLM +
cold-Opus-non-certifying both SPEC_READY_WITH_NITS) was run specifically to verify the first
remediation was correct — not to re-litigate settled decisions. It found the first remediation's
two largest, most complex items (the truncation pipeline, the transcript buffer) each had one real
remaining defect, both now fixed inline above (item 4/"Expand output" section, and item 12 above).
This subsection covers everything else the second round found.

### DECIDED — must apply

13. **The transcript buffer stores the WHOLE incoming `frame` object, not just `frame.data`.**
    Two independent reviewers (agy-print, pi-GLM) and cold-Opus (non-certifying) all independently
    caught the same gap: `formatTimelineFrame(frame, options)` reads `frame.event` directly
    (`app.js:201`, `const event = (frame && frame.event) || "message";`) for its non-transcript
    fallback branch (`"[" + event + "] " + formatJson(data)`). If the buffer stored only
    `frame.data` (as the first remediation round said), `rerenderTranscript()` would lose
    `frame.event` for every non-transcript-source frame and silently mislabel it `"message"` on
    every re-render — a real, confirmed regression. Fix: `selectSeat`'s SSE callback pushes the
    **entire `frame`** onto the buffer (the exact same object it already passes to
    `appendTimelineFrame` today — no reconstruction needed), and `rerenderTranscript()` maps each
    buffered frame through `formatTimelineFrame(frame, options)` unchanged.
14. **Only transcript-source frames are buffered.** `appendTimelineFrame` already branches on
    `frame.data && frame.data.source === "transcript"` (`app.js:210`) to decide HTML-insertion vs.
    plain-text-append; the buffer-push uses this same check — only `source === "transcript"` frames
    go into the buffer. Error frames and other non-transcript SSE events are appended live (as
    today) but never buffered, since they're not something a toggle-driven rerender should
    reproduce (pi-GLM's "missed entirely" — a natural consequence of "mirror what's already passed
    to `appendTimelineFrame`," made explicit).
15. **The live-append path is UNCHANGED and runs in parallel with the buffer — it is not replaced.**
    pi-GLM's ambiguity catch: a spec author reading the first remediation could reasonably infer
    either "replace the append with buffer-push + rerender on every frame" (matching Go's
    architecture more literally, but a bigger behavioral change) or "keep the append, add a
    buffer-push alongside it for later toggle-driven rerenders only" (what was actually intended).
    Stated explicitly now: `appendTimelineFrame` keeps appending directly to the DOM for every new
    incoming frame exactly as it does today; the buffer-push is an ADDITIONAL side effect on the
    same frame, purely to make a later `rerenderTranscript()` possible. A new frame is never
    double-rendered (once by the live append, once by a rerender) because `rerenderTranscript()`
    only runs in response to a toggle click, not on every frame.

    **Correction (third re-panel round, pi-GLM's catch): "unchanged" describes the MECHANISM only,
    not the options passed.** `appendTimelineFrame`'s call site must build its options object from
    the CURRENT toggle-state variables (`showTimestamps`/`showLabels`/`expanded`, alongside the
    existing `escapeContent: true`), not the hardcoded options-with-no-toggle-keys the original
    phrasing implied. Without this, a frame arriving right after a toggle click would render with
    stale (pre-toggle) settings — inconsistent with the just-completed `rerenderTranscript()` pass
    covering every earlier frame. Both the live-append call site and `rerenderTranscript()` read
    the same current toggle-state variables at call time; they simply do so at different moments
    (on every frame vs. only on a toggle click).
16. **The transcript buffer must be cleared at EVERY existing `timelineEl.textContent = ""` site —
    stated as a rule, not an enumerated list.** Cold-Opus's finding, verified directly: `grep -n
    'timelineEl.textContent = ""' app.js` returns **five** matches, not the two the first
    remediation named. In order: `clearToken`'s click handler (Clear), `setSeatSource` (the
    live/history mode toggle), `selectSeat` (switching which seat's transcript is shown),
    `openOrchestrator` (switching orchestrators), and `loadOrchestrators`'s 401/403 branch (a token
    going bad mid-session). Naming specific functions risks missing one (as the first remediation
    round just demonstrated) — the rule going forward: **the transcript buffer is cleared at every
    site that clears `timelineEl`, without exception, present or future.** A display-mode toggle or
    orchestrator switch that resets the visible timeline but leaves stale frames in the buffer would
    let a later `rerenderTranscript()` replay content that no longer belongs to what's on screen.
17. **`rerenderTranscript()`'s clearing mechanism must match how content was originally inserted** —
    `timelineEl.replaceChildren()` (equivalent to clearing `innerHTML`), not `textContent = ""`,
    since transcript-source content is inserted via `insertAdjacentHTML` (HTML), not
    `textContent +=` (plain text) — clearing with `textContent = ""` before re-inserting HTML would
    be inconsistent with how the content actually got there (pi-GLM P2-3).
18. **State explicitly which toggles call `rerenderTranscript()` and which don't** (codex P2): only
    Timestamps, Labels, and Expand-output trigger a rerender. Full-width only updates its own
    pressed-state and the seat-panel's `display` — it never touches transcript content and never
    calls `rerenderTranscript()`. (The first remediation said both "all four toggles rerender" and
    "Full-width doesn't need it" without reconciling the two statements — this item is the
    reconciliation.)
19. **A fourth existing test needs a planned update, for a different reason than the other three.**
    Two independent reviewers (codex, pi-GLM) both caught this: `test_appjs_viewport_fill_fallback_
    triggers_additional_fetch` (`test_visibility_web_contract.py:547-576`) directly exercises
    `shouldAutoFetchHistoryPage`, which gains the `fullWidth` guard from remediation item 2. The
    existing test's fixture objects omit `fullWidth`, so it still passes (`undefined` is falsy,
    correctly defaulting to "panel visible") — but passing doesn't mean the new guard is actually
    *tested*. Fix: add a `fullWidth: true` case to this test, asserting the function returns `false`
    even when every other condition (`historyHasMore`, `historyCursor`, etc.) would otherwise return
    `true` — a positive proof the guard works, not just that nothing broke (cold-Opus's phrasing:
    "the item-2 fix has no positive deny-proof test").
20. **The inner flex layout for `<section>` needs its own spec, not just the outer `body`-level
    one.** pi-GLM's catch: remediation item 9 specifies the OUTER flex column (`body` →
    `header`/`#filter-bar`/`main`), but `<section>` (the right pane) now also contains a
    fixed-height `#transcript-toolbar` above the scrolling `#timeline` — `<section>` needs its own
    `display:flex; flex-direction:column`, with `#timeline{flex:1 1 auto; min-height:0;
    overflow:auto}` so the toolbar takes its natural height and the timeline still scrolls
    correctly within the remaining space.
21. **`agentOf` must be listed explicitly in the Testing section**, not only in the remediation
    text (codex P2 — doc-completeness). Test cases: `cold-opus-*` → `opus`, empty string → `?`,
    each of the 8 known labels, and an unknown prefix passing through lowercased (per remediation
    item 1's correction below).
22. **`agentOf`'s unknown-prefix fallback returns the LOWERCASED prefix, not the original-case
    string.** agy-print's catch, verified against Go: `head := strings.ToLower(strings.SplitN(seatID,
    "-", 2)[0])` — `head` is already lowercased before both the label-map lookup AND the final
    `return head` fallback. The first remediation's "unknown prefix → the prefix itself" was
    ambiguous about case; corrected to "the lowercased prefix," matching Go exactly (e.g. seat id
    `"Foo-bar-1"` → agent value `"foo"`, not `"Foo"`).

### ACKNOWLEDGED — non-blocking, judged on the merits

- **No buffer size limit** (pi-GLM "missed entirely") — Go retains its full `m.transcript` for a
  seat's whole runtime too, so an unbounded buffer is Go-parity behavior, not a new risk. Bounded
  in practice by the existing per-seat clear-on-switch behavior (item 16). No limit added.
- **`agentOf`'s prefix-extraction mechanism** (codex P2-1, `SplitN(seatID, "-", 2)[0]` vs. a JS
  `seatId.split("-")[0]`) — both produce identical results for every real seat-id shape in this
  codebase; non-blocking, the spec stage picks the idiomatic JS equivalent.

Both certifying FIX_BEFORE_SPEC verdicts from this second round are resolved by the fixes above
(items 13-22, plus the inline truncation-pipeline correction in "Expand output" and the buffer-shape
correction in item 12). **This turned out to be premature — a third round found the truncation fix
itself introduced two new P0-severity bugs. See below, now genuinely final.**

## Third design re-panel — the truncation pipeline, resolved for good

A **third** panel round (codex + agy-print FIX_BEFORE_SPEC, pi-GLM SPEC_READY_WITH_NITS with an
unambiguous P1, cold-Opus non-certifying FIX_BEFORE_SPEC — 4/4 unanimous on the core finding) found
that round 2's truncation-pipeline fix, while correctly identifying a real problem (HTML-cutting
risk), introduced two of its own:

1. **Destructive mutation** (agy-print): truncating `data.content` and storing the truncated result
   would permanently lose the original full content — toggling Expand-output back on later could
   never recover it.
2. **The hint span gets escaped away** (agy-print, codex, pi-GLM, cold-Opus — all four): a
   `<span class="dim">` baked into `data.content` *before* `formatTimelineEvent` runs is subject to
   that same function's `escapeHtml()` call, rendering as literal `&lt;span...&gt;` text, not styled
   markup.

The actual resolution (now applied inline in the "Expand output" section and item 8 above, both
already corrected): **revert to post-formatting truncation** — the position round 2 moved *away*
from. Round 2's move was solving a real problem (cutting inside `<details>` tags) that round 1's
own decision (excluding `apply_patch`/`model_thinking` from truncation scope, since those are the
only kinds producing `<details>` tags) had *already* eliminated. Once that's accounted for, the
remaining truncatable kinds produce plain text with no HTML structure to cut through, so truncating
the rendered/escaped string is exactly as safe as Go's own `collapseOutput(data, rendered,
expanded)` — and this signature (present in the "Data flow" section throughout, never actually
wrong) is the final, correct one.

### Other third-round fixes (all applied inline above)

- **Wrong cross-reference** ("see remediation item 14" pointed at the buffering item, not
  truncation — cold-Opus) — removed along with the section it was in during the truncation
  rewrite.
- **`.dim` CSS class doesn't exist** (cold-Opus, verified: zero matches in `index.html`) — corrected
  to state it's a **new** class this feature adds (`.dim{color:var(--ink-500)}`), not a reuse of an
  existing one.
- **Live-append needs the current toggle state merged into its own options object**, not just
  `rerenderTranscript()` (pi-GLM) — item 15 corrected: "unchanged" was about the append *mechanism*
  only; the *options* passed must reflect current toggle state so a frame arriving right after a
  toggle click doesn't render with stale settings.
- **Stale top-level table** (still showing the pre-`unknown` status cycle) and **stale Testing
  section** (not actually listing `agentOf`, despite item 21 saying it should be) — both were
  remediation items *describing* a fix without the referenced section ever being edited; both are
  now actually edited, not just described.

### ACKNOWLEDGED — pi-GLM's staleness/`effectiveState` observation (out of scope, not silently dropped)

Go's `visibleSeats` filters on `effectiveState`, which reclassifies a long-quiet `running` seat as
`stale` for filtering purposes even before the next real event confirms it. The web page has no
`effectiveState` equivalent today — it renders whatever `state` the backend last sent, with no
client-side staleness reclassification at all. Implementing one would be a genuinely separate,
larger feature (client-side staleness detection tied to the age-tick timer) that nothing in this
round's brief asked for. This design's `visibleSeats` filters on the raw `state` value only,
matching what `renderSeats` already displays — not a bug, a scope boundary, stated explicitly so a
future reader doesn't wonder why a visibly-quiet `running` seat still matches a `running` filter.

This is genuinely the final word on the truncation mechanism — three rounds, each finding exactly
one remaining defect in a shrinking scope, converging on a design that (per round 3's unanimous
verification) matches Go's actual, already-correct function signature. No further re-panel round is
planned for this specific mechanism.

## Post-design correction (found at the spec stage, not a fourth design panel)

**One premise above was still incomplete.** "The remaining truncatable kinds produce plain text
with no HTML structure to cut through" (this section, above) assumed `isTruncatableOutput`'s
kind-based narrowing is *sufficient* on its own to exclude `apply_patch`. It is not: `kind` and
`tool_name` are independent fields on the same real data object, and the spec-panel stage found
(verified against the actual, already-committed test fixture at
`test_visibility_web_contract.py:311-320`) that a genuine `apply_patch` entry can carry
`kind: "command_finished"` — one of the three "narrowed" values — simultaneously. `isTruncatableOutput`
must explicitly exclude `tool_name === "apply_patch" && meta.file`, not just narrow by `kind`, to
actually guarantee no `<details>`-wrapped content is ever truncated. See the implementation spec's
§3.4 for the corrected function and full reasoning. The design's *decision* (apply_patch is never
truncated) was always right; only the *mechanism* for enforcing it — kind-narrowing alone — was
incomplete. Recorded here so a future reader of this design doc isn't misled by the "final word"
framing two paragraphs up into thinking kind-narrowing alone was ever sufficient.

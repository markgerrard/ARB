# arb-watch historical seat view — design draft

## Purpose & scope

`arb-watch`'s seat pane is live-only and in-memory (`m.seats` in
`tools/arb-watch-go/model.go`, fed by `/sse/orchestrator/{id}` in
`src/arb_memory/visibility.py`). On relaunch it starts empty; the only
"history" it ever sees is the gateway's own SSE backfill
(`SSE_BACKFILL_COUNT = 200`, itself bounded by the `events:live` stream's
MAXLEN). Seats older than that are gone from the UI even though every one of
them has a durable row in `eval_event_raw`.

This design adds:

1. A read-only gateway endpoint that lists seats for one orchestrator,
   reconstructed from `eval_event_raw`, newest-activity-first, keyset-paginated.
2. A keybinding in the Go TUI that toggles the seat pane's data source
   between the live SSE reducer and pages fetched from that endpoint —
   same pane, same render path, same identity key (`task_id`).

## Non-goals

- No new persistence, no seat-roster summary/materialized table. Query
  directly against `eval_event_raw` with one new index.
- No server-side status/agent filtering on the history endpoint — the TUI
  already filters client-side (`visibleSeats`, `statusFilter`/`agentFilter`);
  reuse it uniformly across both sources rather than building filtering twice.
- No cross-orchestrator history browsing. History is scoped to one
  orchestrator, exactly like the live `/sse/orchestrator/{id}` route already is.
- No attempt to reconstruct `model`/`engine_model` for historical rows —
  `EVAL_ALLOWLIST` (`src/agent_redis_bridge/eval_tee.py`) never captured them,
  so they aren't in `eval_event_raw.payload`. History rows simply omit them;
  the header already renders that field via `fallback(...)`, so it degrades
  to blank, not broken.
- No transcript history browsing here — drill-in already reads
  `eval_event_raw`/`transcript_io`/`audit_events` via `_backfill_seat`
  (`sse_seat`, `src/arb_memory/visibility.py:342`) and needs no change; a
  historical seat row drills in exactly like a live one (both key by `task_id`).
- No attempt to preserve scroll position across a live↔history toggle
  (see "toggle-back semantics" below) — reset-to-top is simpler and is called
  out as a deliberate simplification, not an oversight.

## Architecture

```
eval_event_raw (Postgres)            events:live (Valkey stream, MAXLEN-bounded)
        │                                      │
        │ GET /orchestrators/{id}/seats/history│ /sse/orchestrator/{id}
        ▼                                      ▼
  history endpoint (new)                orchestrator SSE (existing)
        │                                      │
        └───────────────┬──────────────────────┘
                         ▼
              Go TUI seat pane (model.go)
        m.seats (live, continuously fed)   m.historySeats (fetched pages)
                         │
              m.seatSource selects which map
              visibleSeats()/renderSeatTable() read
                         ▼
                  SAME render path, SAME row shape
```

The orchestrator SSE stream keeps running in the background exactly as it
does today, whether or not the seat pane is currently showing history — this
matches the existing invariant that opening a seat's transcript doesn't
freeze the seat list (`enterSeat` doesn't call `orch.stop()`). History mode
adds a second, independent data source; it does not touch or pause the first.

## The endpoint

### Path & params

```
GET /orchestrators/{orchestrator_id}/seats/history?cursor=<opaque>&limit=<n>
```

Rationale: symmetric with the existing `/sse/orchestrator/{orchestrator_id}`
route — same scoping concept (one orchestrator's roster), same auth check,
easy to reason about as "the paged version of the same list."
Rejected alternative: a flat `/seats/history?orchestrator=...` — path param
matches the existing route family instead of introducing a query-param
scoping convention the rest of the app doesn't use.

- `cursor` (optional): opaque string, omitted for the first page.
- `limit` (optional): default 50, clamped to `[1, 200]`. Non-integer or
  out-of-range values are clamped, not rejected — this is a display page
  size, not a security-relevant parameter, and failing loudly on it would
  make the TUI's own paging logic more fragile for no benefit.

### Response schema

```json
{
  "seats": [
    {
      "task_id": "…",
      "run_id": "…",
      "seat_id": "…",
      "orchestrator": "…",
      "state": "done",
      "last_event": "task_finished",
      "last_event_ts": "2026-06-30T12:00:00+00:00"
    }
  ],
  "next_cursor": "2026-06-30T12:00:00.123456+00:00_918422",
  "has_more": true
}
```

Field names (`task_id`, `run_id`, `seat_id`, `orchestrator`, `state`,
`last_event`, `last_event_ts`) are chosen to be **byte-identical** to what
`reduceSeat`/`_reduce_seat` already produce on a live seat row
(`tools/arb-watch-go/reduce.go:17`, `src/arb_memory/visibility.py:128`). The
Go TUI's rendering code (`renderSeatTable`, `runDisplay`, `seatAge`,
`effectiveState`, `agentOf`) reads seat maps by these exact keys today; a
history row decoded straight off the wire into `map[string]any` needs zero
translation to be rendered by the same functions. This is the mechanism that
satisfies "no branching" in the render path — the one intentional branch is
purely about *which map* `visibleSeats()` reads (see below), never about how
a row is displayed once selected.

### State reconstruction (server-side, one row per seat)

The query below returns exactly the **latest** `eval_event_raw` row per
`task_id`. State is derived from that single row — no fold over history is
needed, because only the last event determines final status:

| latest `event_type`              | derived `state` | rationale |
|---|---|---|
| `task_finished`, `payload.ok` is `false` | `failed` | matches live `_reduce_seat` |
| `task_finished`, otherwise               | `done`   | matches live `_reduce_seat` |
| `vote`                                    | `voted`  | matches live `_reduce_seat` |
| `task_started`, `task_continuing`         | `incomplete` | **new, historical-only** — see below |
| anything else / unrecognized              | `unknown` | forward-compat: an event vocabulary addition never crashes the endpoint |

**Decision: `incomplete`, not `running`.** A historical row whose last known
event is `task_started`/`task_continuing` is not live — the seat process is
almost certainly gone (crashed, or the bridge/host restarted before a
`task_finished` was ever teed). Rendering that as `"running"` would lie: the
live pane's `stateColors`/`ageStyle`/staleness logic all assume "running"
means "still alive, age matters." `incomplete` is a distinct, honest label.
It has no entry in `stateColors` (`model.go:70`), so it renders unstyled by
default — acceptable, flagged as an open question below (worth a color?).
**Rejected alternative:** reuse `"running"` and let `effectiveState`'s
staleness check turn it into `"stale"` — rejected because that check is
time-based (`age > 120s`) and would depend on *when the query runs*, not on
what actually happened; a seat from six months ago would flicker between
"running" and "stale" depending on clock skew in the derivation, which is
worse than just being honest that it's a distinct case.

### SQL shape

A per-seat "distinct on task_id, most recent event" query has no cheap
`LIMIT`-bounded form when the group-by key is unindexed for that access
pattern, and eagerly computing "the last event of every seat this
orchestrator ever had" is unbounded work on a long-lived orchestrator. The
design instead **over-fetches a bounded, most-recent window of raw events,
then de-duplicates within that window**:

```sql
WITH window AS (
    SELECT id, task_id, run_id, seat_id, orchestrator, event_type, sent_at, payload
    FROM eval_event_raw
    WHERE orchestrator = %(orchestrator)s
      AND (%(cursor_sent_at)s IS NULL
           OR (sent_at, id) < (%(cursor_sent_at)s, %(cursor_id)s))
    ORDER BY sent_at DESC, id DESC
    LIMIT %(window_size)s
),
latest AS (
    SELECT DISTINCT ON (task_id)
        task_id, run_id, seat_id, orchestrator, event_type, sent_at, payload
    FROM window
    ORDER BY task_id, sent_at DESC, id DESC
)
SELECT task_id, run_id, seat_id, orchestrator, event_type, sent_at, payload
FROM latest
ORDER BY sent_at DESC, id DESC
LIMIT %(limit)s;
```

- `window_size = min(limit * 8, 2000)` — a fixed over-fetch factor, hard
  capped. Cost per request is bounded by a constant regardless of how long
  the orchestrator has been running or how many total seats it has had.
- **Correctness of the dedup:** because `window` is scanned strictly in
  `(sent_at, id)` DESC order from the cursor, the first row encountered per
  `task_id` inside that window *is* that seat's true latest event among
  everything not yet returned by an earlier page — nothing newer for that
  seat exists beyond what prior pages already returned. So `DISTINCT ON`
  inside the window never under- or over-states a seat's true latest state.
- **`next_cursor` is the `(sent_at, id)` of the *last row scanned in
  `window`*, not the last row returned in `latest`.** This is the detail that
  keeps pagination correct: if cursor advanced only past the *returned*
  seats, a seat whose only event fell strictly between the last-returned row
  and the window's true boundary could be silently skipped on the next page
  (its event lives in the gap the client never asked for again). Cursoring
  off the window boundary guarantees every event is visited exactly once
  across pages, even though not every event surfaces as a distinct seat.
- **Accepted trade-off:** if very few `task_id`s dominate the window (e.g.
  one seat emitted hundreds of events, others few), a page can legitimately
  return **fewer than `limit` seats**, even down to a handful. This is
  visible to the TUI as "this page was sparse" — not a bug, just the cost of
  a single bounded query instead of a server-side expand-and-retry loop. See
  Open Questions.
- **Rejected alternative:** `SELECT DISTINCT ON (task_id) ... WHERE
  orchestrator = %s ORDER BY task_id, sent_at DESC` with no window — correct
  but its cost is proportional to the orchestrator's *entire* eval-event
  history, forever growing for a long-lived orchestrator; every page
  (including page 1) would re-scan everything. Rejected on the "must be
  bounded" requirement.
- **Rejected alternative:** a server-side loop that doubles the window until
  `limit` distinct seats are found or history is exhausted — strictly more
  correct (always returns a full page when one exists) but adds unbounded
  *iteration* count and unbounded worst-case latency on a pathological
  history (one seat with 100k events). Out of scope for a first cut; noted
  as a natural follow-up if sparse pages prove annoying in practice.

`has_more`: `true` iff `window` returned exactly `window_size` rows (i.e. the
window may have been truncated before reaching the true end of history for
this orchestrator); `false` iff `window` returned fewer rows than
`window_size` (there is nothing older left). This can occasionally cause one
extra round-trip that returns an empty-ish page right at the true boundary,
never a missed page.

### Required index

None of the three existing indexes on `eval_event_raw`
(`eval_event_raw_run_idx (run_id)`, `eval_event_raw_task_idx (task_id)`,
`eval_event_raw_inserted_at_idx (inserted_at)`) support filtering by
`orchestrator` with a `sent_at`-ordered scan — the query above would fall
back to a sequential scan + sort. Add:

```sql
CREATE INDEX IF NOT EXISTS eval_event_raw_orchestrator_sent_at_idx
    ON eval_event_raw (orchestrator, sent_at DESC, id DESC);
```

This lets Postgres satisfy the `window` CTE's `WHERE orchestrator = … ORDER
BY sent_at DESC, id DESC LIMIT …` as a single bounded index scan (no sort, no
full-table pass). Add this as a plain `ALTER`/`CREATE INDEX IF NOT EXISTS`
statement colocated with the other `eval_event_raw` DDL in
`src/arb_memory/schema.sql` and the mirrored block in `src/arb_memory/run.py`
(`setup_schema`), matching how the existing three indexes are declared
side-by-side in both places.

## The TUI toggle

### State

```go
type model struct {
    …
    seatSource    string            // "live" | "history"
    historySeats  map[string]map[string]any
    historyOrder  []string
    historyCursor string            // "" = first page; opaque, passed straight through
    historyHasMore bool
    historyLoading bool             // in-flight fetch guard (avoid duplicate requests)
}
```

`m.seats`/`m.seatOrder` (live) are untouched by any of this — they keep
being fed by the orchestrator SSE stream exactly as today, regardless of
`seatSource`. `historySeats`/`historyOrder` are a second, independent set
populated only by explicit fetches.

### Keybinding

`h` toggles `seatSource` between `"live"` and `"history"`, only meaningful in
`viewSeats`. Chosen because it's mnemonic ("history") and unused in the
current `keyBindings` table (`q ←→ c ^C t l e s a f m`).

- Switching **to** history for the first time in this orchestrator session
  (or if `historyOrder` is empty): fires the page-1 fetch
  (`cursor=""`) and shows a `"loading history…"` status until it lands.
- Switching **back to** live: no fetch, no cache invalidation — `m.seats` was
  never stale, it kept updating the whole time.
- Toggling resets `seatCursor`/`seatScroll` to `0`. **Simplification, not an
  oversight:** the two lists have different membership, so "the same visual
  row" doesn't have a stable meaning across the toggle; snapping to the top
  of whichever list is now showing is simpler and more predictable than
  trying to re-locate the previously-selected seat by `task_id` in the new
  list (which may not even contain it). Flagged as an open question — see
  below — since a user mid-review may want to stay near where they were.

### Render

`visibleSeats()` gains exactly one branch, at the top:

```go
func (m *model) visibleSeats() []string {
    order, seats := m.seatOrder, m.seats
    if m.seatSource == "history" {
        order, seats = m.historyOrder, m.historySeats
    }
    // … rest of the function unchanged, just reads `order`/`seats`
    // instead of m.seatOrder/m.seats directly
}
```

Everything downstream — `renderSeatTable`, `runDisplay`, `seatAge`,
`effectiveState`, `agentOf`, `dedupSeatRuns`, the status/agent filter cycle,
`clampSeatScroll`, `autoSelectSeat`, `enterSeat` (drill-in) — is unchanged,
because a history row and a live row are the same shape and both are keyed
by `task_id`. `renderFilterBar`/the pane header can append `· history` (or
similar) when `m.seatSource == "history"` so the mode is visually obvious —
otherwise a stale-looking historical "done" seat sitting where a live one
used to be would be confusing.

**Identity key / no double-render:** both maps key by `task_id`, and
`dedupSeatRuns` already collapses phantom duplicate rows by `(seat_id,
run_id)` regardless of source. Because `visibleSeats()` reads from exactly
one of the two maps at a time (never merged), a seat can never appear twice
across live+history simultaneously — the modes are mutually exclusive views
of the *same* pane, not two panes shown at once. Drill-in (`enterSeat`)
targets `task_id` in both modes identically, so opening a historical seat's
transcript is the existing `/sse/seat/{task_id}` path, unmodified.

### Pagination (cursor pages back in time)

Reuses `clampSeatScroll`/the existing down/pgdown handling rather than a
dedicated "next page" key: when `seatSource == "history"` and the down/pgdown
key would move the cursor past the **last currently-loaded** history row and
`historyHasMore` is true and no fetch is already in flight, dispatch a fetch
for the next page (`cursor = historyCursor`) and append the result to
`historyOrder`/`historySeats` before moving the cursor — an infinite-scroll
pattern. This reuses the seat-list navigation code path as instructed rather
than adding a second, parallel "page forward" affordance the user has to
learn. `clampSeatScroll`/`seatViewportRows` need no changes — they already
operate purely on `len(visibleSeats())`.

### Mode-switch summary

| action | live→history | history→live |
|---|---|---|
| fetch? | yes, if first time this session | no |
| `m.seats` touched? | no | no |
| cursor/scroll | reset to top | reset to top |
| SSE streams | unaffected (orch stream keeps running) | unaffected |

## Failure modes

**Gateway:**
- **psql down / connection error:** `_backfill_seat_blocking`'s pattern
  (`anyio.to_thread.run_sync` + `psycopg.connect` per request) is followed;
  wrap in `try/except psycopg.Error`, log, return `503 {"error": "history
  unavailable"}`. Never a bare 500/stack trace to the client.
- **Empty history** (orchestrator has no `eval_event_raw` rows — brand new,
  or eval consumer wasn't running during that period): `200 {"seats": [],
  "next_cursor": null, "has_more": false}`. Not an error; the TUI renders the
  existing "no seats" hint (mirrors `renderOrchestrators()`'s empty-state
  message at `model.go:1043`).
- **Malformed cursor** (fails a strict `<iso8601>_<bigint>` regex, mirroring
  `STREAM_ID_RE`'s validate-don't-trust pattern already used for SSE resume
  IDs at `visibility.py:56`): `400 {"error": "invalid cursor"}`. Never
  silently treated as "no cursor" — that would silently restart pagination
  from page 1 and could look like duplicate seats reappearing at the top of
  an in-progress scroll-back.
- **Unauthorized:** identical to every other route — `401
  {"error": "unauthorized"}` via the existing `_principal`/`authenticated` check.
- **Orchestrator with no matching seats but valid history elsewhere:** same
  as empty history, `200` with an empty list — no distinction from "orchestrator
  doesn't exist," consistent with `/orchestrators` not leaking existence
  either.

**TUI:**
- **Fetch fails (network / non-2xx):** `m.status` is set (mirrors the
  existing `errMsg` handling for `fetchOrchestratorsCmd`), the
  already-loaded history page(s) stay on screen, `historyLoading` clears so a
  retry (toggle away and back, or scroll again) isn't permanently blocked.
- **401/403 from the history endpoint:** treated like the SSE
  `streamFatalEvent` case — surfaced in `m.status`, no automatic retry (an
  auth problem won't fix itself by repeating the request).
- **Toggling to history with no orchestrator selected:** unreachable — `h`
  is only wired while `m.view == viewSeats`, which requires an orchestrator
  already chosen.

## Testing strategy

**Gateway (`src/arb_memory/`, pytest, matching the existing `test_visibility*`
style):**
- Pure unit tests for the state-derivation table above (given a fake row
  dict, assert `state`) — no DB needed.
- Cursor encode/decode round-trip + a table of malformed cursor strings → 400.
- An integration test against a real/test Postgres: seed `eval_event_raw`
  with events for several `task_id`s under one `orchestrator` at varied
  `sent_at`s (including two rows with identical `sent_at` to exercise the
  `id` tie-break), then assert: page 1 is newest-first, `next_cursor`
  advances correctly, requesting with `next_cursor` yields the remaining
  older seats with no duplicates and no gaps across pages (walk all pages,
  collect every distinct `task_id` seen, compare against the seeded set).
- A skewed-history test that seeds one `task_id` with far more than
  `window_size` events and asserts the documented "sparse page" behavior
  (fewer than `limit` returned, `has_more=true`, no crash, no duplicate/skip
  once you keep paging).
- 401 (missing/invalid token), empty-history 200, and a psql-down 503 test
  (mock the connection to raise).

**Go TUI (`tools/arb-watch-go/*_test.go`, matching `reduce_test.go`/
`model_test.go` style):**
- `visibleSeats()` branch test: with `seatSource == "history"`, confirm it
  reads `historyOrder`/`historySeats` and ignores `m.seats` (and vice versa)
  — a fabricated live seat and a fabricated history seat sharing no
  `task_id` should never both appear when only one source is active.
- `h` toggle: fires the page-1 fetch exactly once when `historyOrder` is
  empty; does not refetch on a second toggle while already populated;
  live→history→live leaves `m.seats` byte-for-byte untouched (matches
  existing "selecting a seat doesn't cancel the orchestrator stream" style
  invariant test already present at `model.go:783`).
- Infinite-scroll paging: cursor at the last loaded history row + down key +
  `historyHasMore=true` → fetch dispatched with the stored cursor, result
  appended, cursor advances into the new rows. And the inverse: `historyHasMore=false`
  → no fetch, cursor simply clamps at the end (existing `visibleSeats()` bound).
- Fetch-failure path: injected error → `m.status` set, no panic, existing
  history rows still rendered.
- Golden-style render test: a history row (state `"incomplete"`) renders via
  `renderSeatTable` with no special-casing required, matching the mechanism
  already proven by the live-row golden tests in `reduce_test.go`.

## Open questions

1. **Is `incomplete` the right label**, or should a historical
   `task_started`/`task_continuing` row instead reuse an existing state
   string (with a distinct color) to avoid introducing a state value the
   `stateColors`/status-filter cycle (`statusCycle` in `model.go:68`) don't
   know about? Today it would filter correctly (raw string match) but render
   with no color and wouldn't appear in the `s` status-filter cycle unless
   added there too. Leaning towards adding it to `statusCycle` explicitly
   rather than leaving it as an uncolored orphan value — flagging rather than
   deciding, since it changes user-facing filter behavior beyond this feature.
2. **Sparse pages under a skewed history** (one seat drowning out a whole
   window) is the design's biggest area of low confidence. It's technically
   correct and bounded, but "press the down arrow and only get 2 new rows
   for 8 window-loads" could feel broken in practice even though nothing was
   skipped. The rejected server-side expand-and-retry loop is the natural
   fix if this proves to matter; deliberately deferred here.
3. **Toggle-back scroll-position reset.** Snapping to the top on every `h`
   press is the simplest option but may be the wrong default for someone who
   toggles to history mid-review and expects to land near "where their seat
   would be." A `task_id`-based best-effort re-locate (fall back to top if
   absent) is a plausible upgrade; deliberately not built here to keep the
   toggle semantics simple and easy to reason about for a first cut.

---

## Warm-orchestrator remediation (post-panel, 2026-07-03)

The certifying panel (codex + agy + pi-GLM) returned 3× FIX_BEFORE_PLAN on this raw draft. Resolutions
below are decisive; the spec is authored against the design AS REMEDIATED here. (Panel reports are not
included in this copy.)

- **P0 — pagination skips seats + query can't run as written (codex, agy).** REPLACE the "cursor off the
  scanned window boundary" scheme with a **keyset cursor on the RETURNED, deduped rows** within a
  **fixed `now()`-anchored window**: the outer query is `SELECT … FROM (DISTINCT ON (task_id) … ORDER BY
  task_id, sent_at DESC, id DESC) latest WHERE (last_ts, task_id) < (cursor_ts, cursor_task) ORDER BY
  last_ts DESC, task_id DESC LIMIT n`; `next_cursor` encodes the **last returned deduped row's**
  `(last_ts, task_id)`, never a raw scan position. The `now()` window is fixed per pagination walk (anchor
  echoed in the cursor) so the grouped universe is stable across pages → no distinct seat is ever skipped or
  repeated. Carry `id` through `latest` (the tiebreak/`stream_entry_id` column exists) so the ORDER BY and
  cursor have their column. This is the Opus/Fable keyset scheme the panel endorsed.
- **P0 — TUI "single branch in visibleSeats" misses hard-coded `m.seats` readers (agy, codex).** Adopt the
  **single-map-replace** model (Opus): there is ONE `m.seats`/`m.seatOrder`; on `h`→history, park the live
  orch reduce (drop its `frameMsg`), REPLACE the map wholesale with fetched history pages; on `h`→live,
  clear + restart the orch stream so the live map rebuilds from backfill. No second map, so there are no
  other readers to miss. Accept the small backfill-flash on toggle-back as the simplicity cost.
- **P0/P1 — `vote → voted` is a dead history branch (all 3; pi-GLM sharpest).** `_emit_vote` tees to
  `events:live` but NOT `_tee_eval_event`, so votes never land in `eval_event_raw`. **DROP the `vote→voted`
  row** from the history state table and document the divergence: a seat that voted shows `voted` live but
  its pre-vote terminal state (`done`/`failed`) in history. `stance` is likewise absent (allowlist). A
  forward-fix (add `_tee_eval_event` to `_emit_vote`) is noted as out-of-scope for this feature.
- **P2 — `incomplete` state (all 3, validated).** KEEP `incomplete` (the panel confirmed the reasoning is
  correct — `effectiveState`'s time-based staleness is meaningless for historical timestamps). **Add it to
  `stateColors` and `statusCycle`** (`model.go`) so it renders styled and is filterable; history-mode rows
  do NOT go through `effectiveState`'s staleness reclassification.
- **P2 — failure modes / cursor parsing / auth (codex, agy).** Spec the endpoint's `503` on psql error
  (no 500-leak), `400` on malformed cursor, empty-`200` on no rows, `401` on auth; parse the opaque cursor
  tolerantly (base64url decode → `(ts, task_id)`; malformed → `400`), not via a rigid regex.

**Net:** the remediated design is the single-map-replace TUI + keyset-off-returned-rows pagination +
vote-branch-dropped + `incomplete` finished. This converges the carried Sonnet draft onto the panel-
validated choices; it is the basis for the spec.

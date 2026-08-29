# arb-watch seat history — implementation spec

Authored against `docs/superpowers/specs/2026-07-03-arb-watch-history-design.md`, with its final
"Warm-orchestrator remediation" section taken as authoritative over the raw draft above it. Where the
raw draft and the remediation conflict, this spec follows the remediation; the "Superseded" list at the
bottom names every place that matters so a plan-writer doesn't rebuild the rejected mechanism.

This is a **contract**, not a plan. No task breakdown, no file-by-file diff sequencing — that's the next
stage.

---

## 1. The endpoint

### Method, path, params

```
GET /orchestrators/{orchestrator_id}/seats/history?cursor=<opaque>&limit=<n>
```

Registered alongside the existing routes in `build_visibility_app`'s `Starlette(routes=[...])` list
(`src/arb_memory/visibility.py:643`), handler name `seats_history`.

| param | type | default | validation |
|---|---|---|---|
| `orchestrator_id` | path, string | — | none (matches `/sse/orchestrator/{orchestrator_id}` — no existence check, no leak of "orchestrator doesn't exist" vs "no history yet") |
| `cursor` | query, opaque string | omitted → first page | absent or `""` ⇒ no cursor (first page). Present ⇒ must decode (§2); decode failure ⇒ `400` |
| `limit` | query, string→int | `50` | non-numeric ⇒ default `50` (not rejected); numeric ⇒ clamped to `[1, 200]` (not rejected) |

### Auth gate

Identical to every other route: `await authenticated(_bearer_token(request))`, checked **first**,
before any param parsing (so a bad cursor from an unauthenticated caller still yields `401`, not `400`
— least information leak, matches existing route ordering).

### Response schema (200)

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
  "next_cursor": "eyJhbGciOi...",
  "has_more": true
}
```

Exactly these 7 keys per seat — **byte-identical names** to `_reduce_seat`'s live output
(`src/arb_memory/visibility.py:128`). Never emit `voted`, `stance`, `model`, `engine_model` for a
history row (Non-goals in the design; the vote branch is dropped, see §2 and §5 "Superseded"). Go's
`fallback(...)` calls on the absent fields already degrade to blank — no Go-side crash, confirmed by
reading `renderHeader`/`runDisplay`/`agentOf` (`tools/arb-watch-go/model.go:1330-1348`, `reduce.go:111`):
all use `getString`, which returns `""` for a missing key, never panics.

`next_cursor` is `null` when `has_more` is `false`.

### Error responses

| condition | status | body | notes |
|---|---|---|---|
| unauthorized (missing/invalid/wrong-resource token) | `401` | `{"error": "unauthorized"}` | same `_principal` check as every other route |
| malformed cursor (bad base64 / bad JSON / wrong shape / unparsable timestamp) | `400` | `{"error": "invalid cursor"}` | never silently treated as "no cursor" — see §2 decode |
| psql connection/query error | `503` | `{"error": "history unavailable"}` | `except psycopg.Error`, `logger.exception(...)`, never a bare 500/stack trace — mirrors the `except (psycopg.Error, redis.RedisError)` pattern already used in `eval.py`/`transcript.py`/`audit.py` |
| empty result (orchestrator has zero `eval_event_raw` rows, or a page landed exactly on the end of history) | `200` | `{"seats": [], "next_cursor": null, "has_more": false}` | not an error |
| bad `limit` (non-numeric or out of `[1,200]`) | `200` | normal response, using the clamped/defaulted limit | **not an error** — deliberately not in the error table twice; this is the one item in the brief's error list that resolves to "no error," by design |

---

## 2. The SQL, cursor, and migration

### Core correctness property (why this shape, not the raw draft's)

A per-seat "latest event" grouping (`DISTINCT ON (task_id)`) is a full grouping over every row that
could possibly be that seat's latest — you cannot know a seat's true latest event without having seen
all of that seat's rows. The raw draft's "over-fetch a bounded window, cursor off the scan boundary"
trick tried to avoid this by windowing, and the panel found it both skips seats and can't be expressed as
written (P0, both codex and agy). The remediation replaces it with the panel-endorsed **keyset-on-
returned-rows over a fixed anchor**: compute the full `latest` grouping once per request (bounded by a
frozen `sent_at <= anchor` cut, not by a row-count window), then keyset-paginate the *deduped* result.

**Accepted cost, stated explicitly (this is the trade the panel made):** the `latest` CTE's cost is
proportional to the orchestrator's total `eval_event_raw` row count up to the anchor — the same
"unbounded growth" the raw draft's own Rejected-Alternatives section warned about for the naive
`DISTINCT ON` query. That warning is now accepted, not avoided: correctness (no skipped/repeated seats)
outranks a full index-scan's cost for a orchestrator's finite lifetime of eval events. The mitigation is
indexing this so it's a single ordered index scan with no separate sort step (below) — not eliminating
the O(rows) scan, just its constant factor.

### Query (verbatim)

Params: `orchestrator` (str), `anchor` (`datetime`, tz-aware UTC), `cursor_ts` (`datetime | None`),
`cursor_task_id` (`str | None`), `fetch_limit` (`int` = `limit + 1`, over-fetch by one to detect
`has_more` without a second query).

```sql
WITH latest AS (
    SELECT DISTINCT ON (task_id)
        task_id, run_id, seat_id, orchestrator, event_type, sent_at, id, payload
    FROM eval_event_raw
    WHERE orchestrator = %(orchestrator)s
      AND sent_at <= %(anchor)s
    ORDER BY task_id, sent_at DESC, id DESC
)
SELECT task_id, run_id, seat_id, orchestrator, event_type, sent_at, id, payload
FROM latest
WHERE %(cursor_ts)s IS NULL
   OR (sent_at, task_id) < (%(cursor_ts)s, %(cursor_task_id)s)
ORDER BY sent_at DESC, task_id DESC
LIMIT %(fetch_limit)s;
```

- The inner `ORDER BY task_id, sent_at DESC, id DESC` picks each seat's true latest row (`id` — the
  `bigserial` PK — is the tiebreak when two events for the same seat share a `sent_at`; this is the
  "carry `id` through" the brief and remediation both call for).
- The outer `(sent_at, task_id) < (cursor_ts, cursor_task_id)` is a **row-value keyset comparison**:
  Postgres compares the tuples lexicographically. `task_id` is only a tiebreak for a `sent_at` tie
  between two *different* seats' latest rows — inside `latest`, `task_id` is already unique (one row
  per seat), so `(sent_at, task_id)` is a total order over the deduped set with no gaps.
  `%(cursor_ts)s IS NULL` is the "first page" branch (mirrors the existing `%(cursor_sent_at)s IS NULL`
  idiom the raw draft already used, and the general `_parse_ts`/nullable-param style already in this
  file).
- `anchor`: computed once in Python as `datetime.now(timezone.utc)` **at the start of the first page's
  request** (not SQL `now()` — a Python-side value is what gets echoed into the cursor and is trivially
  testable/mockable). Every subsequent page of the *same walk* reuses that exact value, decoded back out
  of the cursor — never recomputed. This is what makes the grouped universe stable: a write that lands
  between page 1 and page 2 (`sent_at > anchor`) is invisible to every page of this walk, so no seat can
  shift into or out of the already-partitioned keyset order mid-walk.
- **Accepted minor edge case:** if the app server's clock trails the DB inserter's clock, a row committed
  within that skew window can have `sent_at` nominally "after" the anchor and be excluded from an entire
  walk. It reappears on the next fresh walk (new `h` toggle → new anchor). Not treated as a bug.
- `has_more = len(rows) > limit`; return `rows[:limit]` as the page; if `has_more`, `next_cursor` encodes
  `(anchor, page[-1].sent_at, page[-1].task_id)` — the **last row actually returned**, which is safe now
  (unlike the raw draft's window-boundary scheme) because `latest` is a stable, complete grouping, not a
  windowed approximation.

### State reconstruction (server-side; vote branch dropped)

```python
def _history_seat_state(event_type: str, payload: dict) -> str:
    if event_type == "task_finished":
        return "failed" if payload.get("ok") is False else "done"
    if event_type in ("task_started", "task_continuing"):
        return "incomplete"
    return "unknown"
```

No `vote` branch. Confirmed from the real code: `_emit_vote` (`src/agent_redis_bridge/bridge.py:2072`)
calls `AuditRun(...).emit(...)` and `self._tee_live_event(...)` — it never calls
`self._tee_eval_event(...)`, so a `vote` event never lands in `eval_event_raw`. A seat's terminal
`eval_event_raw` row is always its `task_finished` (or, if it crashed pre-finish, its last
`task_started`/`task_continuing`) — a panel member that voted therefore reads `voted` live but
`done`/`failed` in history. This is a real, permanent divergence between the two views, not a bug to
fix here (a forward-fix — adding `_tee_eval_event` to `_emit_vote` — is out of scope).

`incomplete` (not `running`): `task_started`/`task_continuing` as a seat's *latest* row means the seat's
process almost certainly never reached `task_finished` — crashed, or host/bridge restarted first.
Labeling it `"running"` would claim liveness the data can't support, and would make the Go side's
time-based `effectiveState` staleness check depend on *when the query runs* rather than on what
happened — a seat from six months ago would flicker between "running" and "stale" as a function of query
time. `incomplete` is a distinct, honest, permanent label — panel-validated (P2, all three reviewers).

### Cursor: exact encode/decode

Opaque to the client. Base64url (no padding) of a JSON 3-tuple `[anchor_iso, last_ts_iso, task_id]`.
JSON+base64 (not a rigid regex) is what the remediation asks for ("parse tolerantly") — a bad cursor
fails at one of: base64 decode, UTF-8 decode, JSON parse, tuple-shape check, or timestamp parse, and
every one of those failure points is caught and folds to the single `400 {"error": "invalid cursor"}`
outcome. Reuses the existing `_iso`/`_parse_ts` helpers already in `visibility.py` (lines 95, 191) for
the timestamp conversions, so encode/decode round-trips through the exact same ISO-8601 formatting the
rest of the file already uses.

```python
import base64

def _encode_history_cursor(anchor: datetime, last_ts: datetime, task_id: str) -> str:
    payload = json.dumps([_iso(anchor), _iso(last_ts), task_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")

def _decode_history_cursor(cursor: str) -> tuple[datetime, datetime, str] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parts = json.loads(raw)
        anchor_raw, last_ts_raw, task_id = parts
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    anchor, last_ts = _parse_ts(anchor_raw), _parse_ts(last_ts_raw)
    if anchor is None or last_ts is None or not isinstance(task_id, str) or not task_id:
        return None
    return anchor, last_ts, task_id
```

### Required index (migration)

The query's inner CTE needs `WHERE orchestrator = … ORDER BY task_id, sent_at DESC, id DESC` satisfied
without a separate sort step. None of the three existing indexes do this, and — importantly — the raw
draft's proposed index (`orchestrator, sent_at DESC, id DESC`) does **not** either: it's ordered by
`sent_at` first, not `task_id`, so it can't feed `DISTINCT ON (task_id)`'s required order without an
explicit sort of the whole matching set. The index this query actually needs is ordered `task_id` first:

```sql
CREATE INDEX IF NOT EXISTS eval_event_raw_orchestrator_task_sent_at_idx
    ON eval_event_raw (orchestrator, task_id, sent_at DESC, id DESC);
```

This lets Postgres satisfy `latest`'s `ORDER BY task_id, sent_at DESC, id DESC` as a single ordered
Index Scan + Unique node (standard "DISTINCT ON matches index order" plan) — no Sort node, even though
the scan still visits every matching row (see the accepted-cost note above).

Add as a plain `CREATE INDEX IF NOT EXISTS` statement colocated with the other three `eval_event_raw`
indexes, in **both**:
- `src/arb_memory/schema.sql` (next to line 100, alongside `eval_event_raw_inserted_at_idx`)
- `src/arb_memory/run.py`, `setup_schema` (next to line 112, same `conn.execute("CREATE INDEX IF NOT
  EXISTS ...")` one-liner style already used there for the other three)

---

## 3. The Go changes (single-map-replace)

There is **one** `m.seats`/`m.seatOrder` — no second `historySeats`/`historyOrder` map (that was the
raw draft's design; the panel rejected it as a hard-coded-reader trap, P0, agy+codex). History mode
means: park the live stream's writes, and wholesale-replace the one map with fetched pages.

### New `model` fields

```go
seatSource     string // "live" | "history"
historyCursor  string // "" = no cursor yet fetched for the current page cursor; opaque, passed straight through
historyHasMore bool
historyLoading bool   // in-flight fetch guard
historyGen     int    // bumped on every live→history transition; stale-gen fetch responses are dropped
                       // (same staleness pattern as streamState.gen — see race guard below)
```

### New message types (`model.go`, near `frameMsg`/`errMsg`)

```go
type historyPageMsg struct {
    gen        int
    seats      []map[string]any
    nextCursor string
    hasMore    bool
    append     bool // false = first page (replace); true = subsequent page (append)
}
type historyErrMsg struct {
    gen int
    err error
}
```

### Fetch command + HTTP call (`sse.go`, mirrors `fetchOrchestrators`)

```go
type historyResponse struct {
    Seats      []map[string]any `json:"seats"`
    NextCursor *string          `json:"next_cursor"`
    HasMore    bool             `json:"has_more"`
}

func fetchSeatsHistory(baseURL, token, orchestrator, cursor string) (historyResponse, error) {
    endpoint, err := url.JoinPath(strings.TrimRight(baseURL, "/"), "orchestrators", orchestrator, "seats", "history")
    if err != nil {
        return historyResponse{}, err
    }
    if cursor != "" {
        q := url.Values{}
        q.Set("cursor", cursor)
        endpoint += "?" + q.Encode()
    }
    req, err := http.NewRequest(http.MethodGet, endpoint, nil)
    if err != nil {
        return historyResponse{}, err
    }
    req.Header.Set("Authorization", "Bearer "+token)
    resp, err := http.DefaultClient.Do(req)
    if err != nil {
        return historyResponse{}, err
    }
    defer resp.Body.Close()
    if resp.StatusCode < 200 || resp.StatusCode >= 300 {
        return historyResponse{}, fmt.Errorf("fetch seats history: %s", resp.Status)
    }
    var payload historyResponse
    if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
        return historyResponse{}, err
    }
    return payload, nil
}
```

```go
func fetchHistoryCmd(baseURL, token, orchestrator, cursor string, appendPage bool, gen int) tea.Cmd {
    return func() tea.Msg {
        resp, err := fetchSeatsHistory(baseURL, token, orchestrator, cursor)
        if err != nil {
            return historyErrMsg{gen: gen, err: err}
        }
        next := ""
        if resp.NextCursor != nil {
            next = *resp.NextCursor
        }
        return historyPageMsg{gen: gen, seats: resp.Seats, nextCursor: next, hasMore: resp.HasMore, append: appendPage}
    }
}
```

The Go client never sends `limit` — relies on the server default (50). No pagination-size control on
the client for a first cut.

### The `h` keybinding

Added to `keyBindings` (`model.go:58`) after the `a` entry: `{"h", "History"}`. Handled in `handleKey`
(`model.go:401`), only when `m.view == viewSeats`:

```go
case "h":
    if m.view == viewSeats {
        return m, m.toggleHistoryMode()
    }
```

```go
func (m *model) toggleHistoryMode() tea.Cmd {
    m.seatCursor = 0
    m.seatScroll = 0
    if m.seatSource == "history" {
        // history → live: no cached map to fall back to (single map was history's content) —
        // discard it and get a fresh backfill via a stream restart. Accepted backfill-flash cost.
        m.seatSource = "live"
        m.historyCursor = ""
        m.historyHasMore = false
        m.historyLoading = false
        m.seats = map[string]map[string]any{}
        m.seatOrder = nil
        m.status = ""
        streamURL := m.baseURL + "/sse/orchestrator/" + url.PathEscape(m.orchestrator)
        return m.startStream(m, streamOrch, streamURL, "") // realStartStream already cancels
                                                              // any live context + bumps gen,
                                                              // same as enterSeats does — no
                                                              // separate m.orch.stop() needed
    }
    // live → history: park, don't stop — the orch SSE goroutine keeps running and frameMsgs
    // keep arriving; the frameMsg handler (below) just stops applying them to m.seats while
    // seatSource == "history". This is what "SSE streams unaffected" means in practice.
    m.seatSource = "history"
    m.historyCursor = ""
    m.historyHasMore = false
    m.historyLoading = true
    m.historyGen++
    m.status = "loading history…"
    return fetchHistoryCmd(m.baseURL, m.token, m.orchestrator, "", false, m.historyGen)
}
```

**Deliberate simplification, superseding the raw draft's "skip refetch if already populated" behavior:**
because there is only one map and it gets wholesale-replaced on entry to history and wholesale-cleared on
exit, *every* live→history transition re-fetches page 1 fresh. There is no longer a cache to check. This
falls directly out of single-map-replace and is simpler to reason about than tracking whether a stale
map is still valid.

### Parking: `frameMsg` handling (`Update`, `model.go:235`)

```go
case frameMsg:
    s := m.stream(msg.which)
    if msg.gen != s.gen {
        return m, nil
    }
    if msg.f.Event == streamFatalEvent {
        s.fatal = true
        m.status = fmt.Sprintf("%s stream stopped: HTTP %s (check token / orchestrator)", msg.which, statusOf(msg.f))
        return m, nil
    }
    if isResumableID(msg.f.ID) {
        s.lastID = msg.f.ID
    }
    s.backoff = baseBackoff
    if msg.which == streamOrch {
        if m.seatSource == "live" {
            m.upsertSeat(msg.f)
            m.syncCursorToSelection()
        }
        // else: history mode — drop the frame. Still re-arm listen() below so the stream
        // doesn't stall; we just don't apply it.
    } else if m.view == viewSeats {
        m.appendTranscript(msg.f)
    }
    return m, listen(s.ch, msg.which, msg.gen)
```

Only that one `if m.seatSource == "live"` guard changes in the existing switch. Everything else in
`Update` is untouched.

### Applying history pages (new methods; NOT `upsertSeat` — no reduce, no alpha-sort)

```go
// replaceSeatsWithHistory wholesale-replaces m.seats/m.seatOrder with a first page's rows,
// preserving the server's newest-first order (do NOT sort.Strings — that's upsertSeat's
// live-map convention and would scramble history's meaningful order).
func (m *model) replaceSeatsWithHistory(seats []map[string]any) {
    m.seats = map[string]map[string]any{}
    m.seatOrder = make([]string, 0, len(seats))
    m.appendHistoryPage(seats)
}

// appendHistoryPage adds a page's rows in server order, deduping defensively on task_id
// (the SQL keyset is provably gap/dup-free across pages of one walk; this is cheap insurance
// against a client-side retry or a future server bug, not a correctness dependency).
func (m *model) appendHistoryPage(seats []map[string]any) {
    for _, seat := range seats {
        taskID := getString(seat, "task_id")
        if taskID == "" {
            continue
        }
        if _, exists := m.seats[taskID]; exists {
            continue
        }
        m.seatOrder = append(m.seatOrder, taskID)
        m.seats[taskID] = seat
    }
}
```

### `historyPageMsg`/`historyErrMsg` handling — the race guard

A history fetch is a one-shot `tea.Cmd` with no built-in staleness check (unlike the SSE streams'
`gen`-gated `listen()` loop). Without a guard, a slow page-2 fetch that lands **after** the user has
toggled back to live (or toggled history off and back on again) could splice stale history rows into a
freshly-rebuilt live `m.seats` — this is exactly the "mode-switch has no live-frame race" property the
brief calls out. `historyGen` closes it, mirroring the existing `streamState.gen` pattern:

```go
case historyPageMsg:
    if msg.gen != m.historyGen {
        return m, nil // stale — toggled away/back since this fetch started
    }
    m.historyLoading = false
    if msg.append {
        m.appendHistoryPage(msg.seats)
    } else {
        m.replaceSeatsWithHistory(msg.seats)
    }
    m.historyCursor = msg.nextCursor
    m.historyHasMore = msg.hasMore
    m.clampSeatCursor()
    return m, nil
case historyErrMsg:
    if msg.gen != m.historyGen {
        return m, nil
    }
    m.historyLoading = false
    m.status = msg.err.Error()
    return m, nil // already-loaded history rows stay on screen; retry = scroll again or toggle away/back
```

### On-scroll pagination (infinite scroll; no dedicated "next page" key)

```go
// maybeFetchNextHistoryPage fires the next page when the seat cursor has reached the last
// currently-loaded history row, more exists, and no fetch is already in flight.
func (m *model) maybeFetchNextHistoryPage() tea.Cmd {
    if m.seatSource != "history" || !m.historyHasMore || m.historyLoading {
        return nil
    }
    vis := m.visibleSeats()
    if len(vis) == 0 || m.seatCursor < len(vis)-1 {
        return nil
    }
    m.historyLoading = true
    return fetchHistoryCmd(m.baseURL, m.token, m.orchestrator, m.historyCursor, true, m.historyGen)
}
```

Wired into the two seat-list-navigation branches that move the cursor **down** (older history is further
down the list, per `visibleSeats`'s recency sort — see below): `"down"` (`model.go:452`) and
`"shift+down"/"pgdown"` (`model.go:474`). Both currently `return m, m.autoSelectSeat()`; both become:

```go
return m, tea.Batch(m.autoSelectSeat(), m.maybeFetchNextHistoryPage())
```

`"up"`/`"shift+up"/"pgup"` are unchanged — scrolling toward more-recent entries never needs older pages.

### `incomplete` in `stateColors` + `statusCycle`

```go
var statusCycle = []string{filterAll, "running", "incomplete", "done", "failed", "voted", "stale"}

var stateColors = map[string]lipgloss.Color{
    "running": "42", "incomplete": "141", "done": "244", "failed": "203", "voted": "39", "stale": "208",
}
```

(`141` — a light violet, distinct from all five existing hues — is my pick; low-confidence, purely
cosmetic, flagged in the closing summary below.)

### `effectiveState` needs **no code change** — this is a "by construction," not a branch

The design (both raw draft and remediation) requires "history rows don't get staleness-reclassified,"
and the brief lists this as something to spec. Reading `effectiveState` as it exists today
(`model.go:1073`):

```go
func effectiveState(seat map[string]any) string {
    state := getString(seat, "state")
    if state == "running" {
        if _, secs, ok := seatAge(seat); ok && secs > staleGrace.Seconds() {
            return "stale"
        }
    }
    return state
}
```

— the staleness check is gated on `state == "running"`. History rows **never** carry `state ==
"running"`: the server-side state table (§2) maps `task_started`/`task_continuing` to `"incomplete"`,
never `"running"`. And because history rows are fetched **pre-reduced** (the JSON already has a `state`
key filled in by the gateway) and applied via `replaceSeatsWithHistory`/`appendHistoryPage` — which store
the fetched map verbatim, with **no call to `reduceSeat`** — there is no code path by which a history
row's `state` field could ever read `"running"`. The guarantee holds structurally, with zero new
branching in `effectiveState` — which is the same "no branching in the render path" property the raw
draft was already claiming for the rest of the render pipeline. **No signature change, no `live bool`
parameter, no model-aware version of this function.** Verified by test (§4).

### Render / filter-bar mode indicator

`renderFilterBar` (`model.go:1241`) gains one conditional line so history mode is visually obvious:

```go
if m.seatSource == "history" {
    body += "\n" + styleFilterVal.Render("· history") + styleDim.Render(" (h)")
}
```

Nothing else in the render path changes: `renderSeatTable`, `runDisplay`, `seatAge`, `agentOf`,
`dedupSeatRuns`, `visibleSeats`'s status/agent filtering, `clampSeatScroll`, `autoSelectSeat`, `enterSeat`
all read `m.seats`/`m.seatOrder` exactly as today, because there is exactly one map and both live and
history rows share its shape.

### Recency ordering falls out for free

`visibleSeats` (`model.go:1085`) already sorts `runSeats` by ascending `seatAge` (smallest age = most
recent = highest in the list). History rows carry real historical `last_event_ts` values, so their ages
are large but their *relative* order still matches "most recently active first" — identical to the
server's `ORDER BY sent_at DESC` — so **no special-casing is needed for history's display order either**;
the existing sort produces the same order the server already returned. `replaceSeatsWithHistory`'s
insertion-order preservation is a defensive fallback for stable ordering only when ages are equal/missing,
not a hard dependency.

### Drill-in, unchanged

`enterSeat(taskID)` is keyed purely by `task_id` and starts `/sse/seat/{task_id}` regardless of which
mode selected it — confirmed unchanged, needs no edit. Toggling modes does **not** clear
`m.selectedTask`/`m.transcript`/the seat SSE stream; if the open seat isn't in the new mode's list, the
right pane just keeps showing what was already loaded until a new seat is picked. Deliberate, not an
oversight — matches the "no scroll-position preservation across modes" simplification already in the
design.

---

## 4. Test list

### Gateway (`src/arb_memory/`, pytest, `test_visibility*` style)

**Pure unit (no DB):**
1. `test_history_state_task_finished_ok_true_is_done`
2. `test_history_state_task_finished_ok_false_is_failed`
3. `test_history_state_task_started_and_continuing_is_incomplete`
4. `test_history_state_unrecognized_event_type_is_unknown` — forward-compat: a future event vocabulary
   addition degrades, never crashes
5. `test_history_cursor_round_trips_anchor_ts_and_task_id`
6. `test_history_cursor_bad_base64_is_malformed`
7. `test_history_cursor_bad_json_is_malformed`
8. `test_history_cursor_wrong_shape_is_malformed` (e.g. a 2-tuple, or a non-string `task_id`)
9. `test_history_limit_non_numeric_defaults_to_50`
10. `test_history_limit_out_of_range_clamps_to_1_and_200`

**Integration (real/test Postgres):**
11. `test_history_pagination_walks_all_seats_no_skip_no_repeat` — seed >`limit` distinct `task_id`s
    under one orchestrator at varied `sent_at`, including two rows (different seats) sharing an
    identical `sent_at` to exercise the `(sent_at, task_id)` tiebreak; walk every page via
    `next_cursor` until `has_more` is `false`; assert the union of all returned `task_id`s equals the
    seeded set with **zero duplicates**.
12. `test_history_pagination_anchor_is_stable_against_concurrent_writes` — start a walk (fetch page 1,
    capture its cursor/anchor), then insert a **new** event for a seat already surfaced on page 1
    (bumping that seat's `sent_at` to "now"), then fetch page 2 with the captured cursor; assert page 2
    is unaffected by the concurrent write (no skip, no repeat, no seat appearing out of the frozen
    order) — this is the direct regression test for the P0 the remediation fixed.
13. `test_history_votes_are_a_dead_branch` — seed a seat whose bridge lifecycle is
    `task_started → task_finished(ok=true)` with a `vote` audit-only side-effect that never reaches
    `eval_event_raw` (i.e., simply don't seed a `vote` row in `eval_event_raw` — matching real
    `_emit_vote` behavior); assert history reports `"done"`, proving the guard that a voted seat reads
    its pre-vote terminal state in history, not `"voted"`.
14. `test_history_scoped_to_one_orchestrator` — seed two orchestrators; assert cross-contamination
    never happens.
15. `test_history_empty_orchestrator_returns_200_empty`
16. `test_history_unauthorized_returns_401`
17. `test_history_psql_error_returns_503` (mock the connection/query to raise `psycopg.Error`; assert no
    stack trace leaks into the body)
18. `test_history_field_shape_matches_live_reducer_keys` — assert each seat dict's keys are exactly
    `{task_id, run_id, seat_id, orchestrator, state, last_event, last_event_ts}`; no `voted`/`stance`/
    `model`/`engine_model` ever present.
19. `test_history_index_exists_after_setup_schema` — run `setup_schema`/apply `schema.sql`, then assert
    `eval_event_raw_orchestrator_task_sent_at_idx` is present (`pg_indexes`), matching however existing
    index-presence is asserted elsewhere in this suite, or a fresh `pg_indexes` query if there's no
    existing pattern to follow.

### Go (`tools/arb-watch-go/*_test.go`, `model_test.go` style)

20. `TestToggleHistoryModeFetchesPageOneAndParksLiveFrames` — from a live view with seats populated,
    press `h`: `seatSource == "history"`, `historyLoading == true`, a fetch command was dispatched; then
    a *fabricated* `frameMsg{which: streamOrch, ...}` delivered while still in history mode must **not**
    mutate `m.seats`.
21. `TestToggleHistoryModeBackToLiveClearsAndRestartsStream` — from history mode with rows loaded, press
    `h` again: `m.seats`/`m.seatOrder` are cleared, `seatSource == "live"`, the orch stream's `gen` has
    advanced (fresh `startStream` call, not a resume), `historyCursor`/`historyHasMore`/`historyLoading`
    all reset.
22. `TestHistoryFirstPageReplacesWholesale` — deliver `historyPageMsg{append: false, gen: <current>}`
    with seats disjoint from whatever was in `m.seats` before; assert the old entries are gone entirely.
23. `TestHistoryAppendPageDedupsOnTaskID` — deliver a second `historyPageMsg{append: true}` containing
    one `task_id` already present; assert it's not duplicated in `seatOrder`, new ones are appended in
    server order.
24. `TestHistoryStaleGenResponseIsDropped` — enter history (`gen` = N), toggle back to live, then deliver
    a `historyPageMsg`/`historyErrMsg` carrying gen N: assert both are no-ops (`m.seats` unchanged,
    `historyLoading` unaffected) — the "mode-switch has no live-frame race" case.
25. `TestHistoryFetchFailureKeepsLoadedRowsAndClearsLoading` — `historyErrMsg` with a matching gen: `
    m.status` is set, already-loaded rows stay rendered, `historyLoading` clears so a retry isn't
    permanently blocked.
26. `TestIncompleteStateRendersStyledAndIsFilterable` — a seat with `state: "incomplete"`:
    `renderSeatTable`'s output contains styling from `stateColors["incomplete"]` (not the bare
    unstyled string); cycling `"s"` through `statusCycle` reaches `"incomplete"` and narrows
    `visibleSeats()` to just that seat.
27. `TestEffectiveStateNeverReclassifiesIncompleteAsStale` — a seat with `state: "incomplete"` and a
    `last_event_ts` from far in the past: `effectiveState(seat) == "incomplete"`, never `"stale"` —
    proves the by-construction guarantee (no `live bool` param needed).
28. `TestHistoryScrollTriggersNextPageAtListEnd` — cursor at the last loaded history row, `down` key,
    `historyHasMore == true`, `historyLoading == false`: a fetch command with `append: true` and the
    stored `historyCursor` is dispatched. Inverse: `historyHasMore == false` → down key clamps at the
    end, no fetch dispatched (existing `visibleSeats()` bound, no new code path).
29. `TestHistoryRowRendersWithNoSpecialCasing` — a decoded-from-JSON history-shaped `map[string]any`
    (only the 7 response keys, nothing else) renders through `renderSeatTable`/`renderHeader` with no
    panic and the same code path as a live row — proves the "byte-identical field shape needs zero
    translation" claim.
30. `TestFetchSeatsHistoryParsesResponseAndNullCursor` (in `sse_test.go`, `httptest.NewServer` style
    matching `TestFetchOrchestrators*`) — a `has_more: false, next_cursor: null` response decodes to an
    empty Go string cursor, no error.

---

## 5. Interfaces block

**Python (`src/arb_memory/visibility.py`):**

```python
def _clamp_history_limit(raw: str | None) -> int: ...
def _history_seat_state(event_type: str, payload: dict) -> str: ...
def _encode_history_cursor(anchor: datetime, last_ts: datetime, task_id: str) -> str: ...
def _decode_history_cursor(cursor: str) -> tuple[datetime, datetime, str] | None: ...
def _history_row_to_seat(row: tuple) -> dict: ...
def _query_seats_history(
    conn, orchestrator_id: str, anchor: datetime,
    cursor_ts: datetime | None, cursor_task_id: str | None, limit: int,
) -> list[tuple]: ...
def _seats_history_blocking(
    orchestrator_id: str, anchor: datetime,
    cursor_ts: datetime | None, cursor_task_id: str | None, limit: int,
) -> list[tuple]: ...
async def seats_history(request: Request) -> JSONResponse: ...
```

New route: `Route("/orchestrators/{orchestrator_id}/seats/history", seats_history, methods=["GET"])`.
New index name: `eval_event_raw_orchestrator_task_sent_at_idx`.

**SQL migration (both `schema.sql` and `run.py`):**

```sql
CREATE INDEX IF NOT EXISTS eval_event_raw_orchestrator_task_sent_at_idx
    ON eval_event_raw (orchestrator, task_id, sent_at DESC, id DESC);
```

**Go (`tools/arb-watch-go/`):**

```go
// model.go — new model fields
seatSource     string
historyCursor  string
historyHasMore bool
historyLoading bool
historyGen     int

// model.go — new message types
type historyPageMsg struct{ gen int; seats []map[string]any; nextCursor string; hasMore, append bool }
type historyErrMsg struct{ gen int; err error }

// model.go — new methods
func (m *model) toggleHistoryMode() tea.Cmd
func (m *model) replaceSeatsWithHistory(seats []map[string]any)
func (m *model) appendHistoryPage(seats []map[string]any)
func (m *model) maybeFetchNextHistoryPage() tea.Cmd

// model.go — changed data (no function signature changes)
var statusCycle = []string{filterAll, "running", "incomplete", "done", "failed", "voted", "stale"}
var stateColors = map[string]lipgloss.Color{ /* + "incomplete": "141" */ }
var keyBindings = []struct{ key, desc string }{ /* + {"h", "History"} */ }

// sse.go — new fetch function + command
type historyResponse struct {
    Seats      []map[string]any `json:"seats"`
    NextCursor *string          `json:"next_cursor"`
    HasMore    bool             `json:"has_more"`
}
func fetchSeatsHistory(baseURL, token, orchestrator, cursor string) (historyResponse, error)
func fetchHistoryCmd(baseURL, token, orchestrator, cursor string, appendPage bool, gen int) tea.Cmd
```

`effectiveState(seat map[string]any) string` — **unchanged signature**, explicitly called out because
it was the most tempting place to add a parameter and shouldn't be touched (§3).

---

## Superseded from the raw design (do not implement these)

- The window-over-fetch + "cursor off the scan boundary" SQL scheme, its `window_size` factor, and the
  "sparse page" open question — replaced wholesale by §2's fixed-anchor keyset query. There is no more
  sparse-page phenomenon: every page returns exactly `limit` distinct seats except the true last page.
- The two-map TUI design (`historySeats`/`historyOrder` as a second map, `visibleSeats()` branching
  between two maps) — replaced by single-map-replace (§3). Any reference to a "second data source"
  feeding the same render path is stale.
- The raw draft's proposed index `(orchestrator, sent_at DESC, id DESC)` — replaced by
  `(orchestrator, task_id, sent_at DESC, id DESC)` (§2), because the query shape it needs to support
  changed with the SQL fix.
- The `vote → voted` row in the state-reconstruction table — dropped (§2); a voted seat reads
  `done`/`failed` in history.
- "Skip refetch if `historyOrder` already populated" — moot under single-map-replace; every live→history
  transition fetches page 1 fresh (§3).

---

## Summary

Spec covers: the `GET /orchestrators/{id}/seats/history` endpoint (params, auth, response/error
contract), the fixed-anchor keyset SQL + opaque base64url/JSON cursor + the
`eval_event_raw_orchestrator_task_sent_at_idx` migration (both `schema.sql` and `run.py`), and the
single-map-replace Go TUI mechanism (`h` toggle, `historyGen` race guard, on-scroll pagination,
`incomplete` wired into `stateColors`/`statusCycle`, and why `effectiveState` needs zero code change). 30
named tests span pure-unit, real-Postgres integration, and Go model/render coverage. Interfaces are
pinned for a task-by-task plan to build against.

File: `/private/tmp/claude-501/-Users-mark-<workspace>/dd349cac-4a03-49b5-81a6-d6e5b8581549/scratchpad/spec-sonnet.md`

Three decisions I'm least sure are right, in descending order of worry:

1. **The new index shape** (`orchestrator, task_id, sent_at DESC, id DESC` instead of the raw draft's
   `orchestrator, sent_at DESC, id DESC`). I derived this from the query plan `DISTINCT ON (task_id)`
   needs, and it's the standard Postgres idiom for that shape — but I have not run `EXPLAIN` against it,
   and it's possible the planner still chooses a bitmap/sort plan over this index for the wrong
   parameter distribution. Worth confirming with `EXPLAIN ANALYZE` in the implementation phase before
   trusting the "single ordered scan" cost claim as fact rather than intent.
2. **Accepting the "latest is O(total orchestrator history)" cost** as the resolution to the raw draft's
   own explicitly-rejected concern, rather than treating it as still-open. I read the remediation's
   "Opus/Fable keyset scheme the panel endorsed" as endorsing this cost trade-off implicitly (correctness
   over boundedness), but the remediation text doesn't say the words "we accept unbounded scan cost" — I
   inferred it from what the corrected query shape necessarily implies. If that inference is wrong, the
   scan-cost trade-off needs its own explicit panel sign-off before implementation.
3. **`historyGen` as a new staleness mechanism** — the design/remediation never mentions a generation
   counter for history fetches; I added it because single-map-replace plus a fire-and-forget `tea.Cmd`
   fetch creates a real race (a stale page-2 response landing after a toggle-back), and the brief
   explicitly requires a race-free mode switch. I'm confident the race is real; I'm less sure
   `historyGen` (vs., say, gating on `m.seatSource == "history"` alone, which I show is insufficient for
   the toggle-away-and-back-again case) is the minimal fix versus something the original designers would
   have chosen differently.

---

## Warm-orchestrator remediation (post spec-panel, 2026-07-03)

Spec panel: codex FIX_BEFORE_PLAN, agy PLAN_READY_WITH_NITS, pi-GLM FIX_BEFORE_PLAN. Panel reports are
not included in this copy. Resolutions below are decisive; the plan is authored against the spec AS
REMEDIATED here.

- **pi-GLM P0 (worktree-commit terminal events render `unknown`) — REJECTED as a false positive
  (verified).** The claim assumes `orchestrator_committed`/`agent_committed` is the *latest* event per
  seat. It is not: `orchestrator_commit` (`bridge.py:1001`) emits the commit event, then `task_finished`
  (`bridge.py:1037`, `ok=result.ok`) fires **after** it, so `task_finished` carries the later `sent_at` and
  `DISTINCT ON (task_id) ORDER BY sent_at DESC` selects it. The existing `task_finished → done/failed`
  mapping already renders the worktree-commit path correctly. Commit events are terminal ONLY if the
  process dies between the commit and `task_finished` (a rare crash edge).
  - **Defensive fallback (P2, not the panel's P0):** map `agent_committed`, `orchestrator_committed`,
    `post_timeout_agent_committed`, `post_timeout_committed` → `done`, and `steer_sent`/`cancel_sent` →
    `incomplete`, so the rare crash-before-`task_finished` seat renders sensibly instead of `unknown`.
    These rows are otherwise superseded by `task_finished`, so the mappings only fire on the crash edge.
    (No `ok`-split needed: a *failed* commit goes through `fail()` → `task_finished ok=false → failed`;
    the commit events themselves are success-only. Verified `bridge.py:1300-1327`.)
- **codex P1 — `historyGen` race guard doesn't guard the history→live direction.** Extend the generation
  guard so a late `historyPageMsg` is dropped after a toggle BACK to live (bump `historyGen` on BOTH
  toggle directions; the fetch cmd captures the gen and its result is discarded on mismatch). Add a test:
  toggle history→live while a page fetch is in flight → the late page does not mutate the live map.
- **agy P1 — Postgres NULL keyset cursor type inference.** The first-page query passes `NULL` for
  `cursor_ts`/`cursor_task`; an untyped NULL in the row-value comparison can fail type inference. Cast
  explicitly (`%(cursor_ts)s::timestamptz`, `%(cursor_task)s::text`) or branch the SQL on first-page.
  Pin with a first-page test that exercises the real driver.
- **codex P1 — Python blocking-helper interface buildability.** Specify the history query helper as a
  concrete sync function signature returning the typed rows + `next_cursor`, called off the event loop the
  same way `_backfill_seat` is, so the plan author has an exact seam.
- **P2 — the `latest` CTE cost is bounded in practice.** `eval_event_raw` is purged by `run_eval_purge`
  (`run.py:63-69`) on `ARB_EVAL_RETENTION_DAYS`, so the anchor-window scan is bounded by retention, not
  unbounded — document this as the effective window bound (no separate lower-bound clause needed). Create
  the migration index with **`CREATE INDEX CONCURRENTLY IF NOT EXISTS`** (pi-GLM) so it doesn't block
  writes on the live prod table; note `run.py`/`schema.sql` both get it.
- **Index confirmed:** `(orchestrator, task_id, sent_at DESC, id DESC)` is correct for the
  `DISTINCT ON (task_id)` + orchestrator filter + `sent_at` sort (two of three authors and the panel agree;
  the plan includes an `EXPLAIN` check).

**Net:** the SQL/cursor/single-map-replace/race-guard core is sound (all three panelists agree); the real
work before plan is the two-direction race guard, the NULL-cursor cast, the retention-bound + CONCURRENTLY
note, and the defensive commit-event fallback. The panel's P0 was verified and rejected.

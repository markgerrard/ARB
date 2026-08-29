# ARB Visibility — Slice 4b design: agent-pane front-ends (web + TUI)

**Status:** design (autonomous run, 2026-06-25). Builds on Slice 4a (`6ae0eb0`) — the OAuth-gated SSE
gateway. 4b is the two front-ends that render the Claude-Code-style agent pane over 4a's frozen SSE contract.

## Goal
A Claude-Code-style agent pane — a live list of one orchestrator's seats; cursor/click one → its live event
timeline. Two interchangeable front-ends (web + TUI) over the SAME SSE contract, built as **two parallel tracks**.

## 4a's frozen SSE contract (both front-ends consume this — do NOT change 4a)
- `GET /orchestrators` (JSON, Bearer auth) → `{"orchestrators":[id,...]}` (picker).
- `GET /sse/orchestrator/{id}` (SSE, Bearer auth) → `seat_appear` / `seat_update` / `seat_finish` events;
  each `data` = a seat `{task_id, seat_id, orchestrator, run_id, state, last_event, last_event_ts}`.
- `GET /sse/seat/{task_id}` (SSE, Bearer auth) → backfill then live timeline events (eval + audit votes).
- Auth today = `Authorization: Bearer <token>` validated against the MCP door token table.

## Shared UX (both tracks render the same model)
Two-pane: LEFT = seat list for the selected orchestrator (seat_id, state badge running/done/failed/voted/stale,
run_id, elapsed); RIGHT = the selected seat's live timeline (turns/tool-calls/usage + votes). Select a seat
(arrow keys in TUI / click in web) → opens `/sse/seat/{task_id}`. An orchestrator picker (from `/orchestrators`).

## Track 1 — web (4b-web)
- **Served by the visibility service** as static routes (new `GET /` → HTML, `GET /app.js`) so the page is
  same-origin with the SSE endpoints (no CORS). Vanilla HTML/JS, **no build step, no framework, no new dep**.
- **Auth (the real constraint):** browser `EventSource` CANNOT set an `Authorization` header. **Decision:** use
  `fetch()` + `ReadableStream` to consume the SSE (fetch CAN set the Bearer header) — keeps header auth, no
  token-in-URL. The page obtains a token by: the user pastes/stores it (localStorage) for v1 (internal tool);
  a full browser OAuth flow is a later enhancement. (Alternative rejected: add a `?token=` query param to 4a's
  SSE — puts the token in URLs/logs.)
- Render: a `<div>` two-pane; JS reduces the SSE events into the seat list + timeline; reconnect with
  `Last-Event-ID`.

## Track 2 — TUI (4b-tui)
- A new CLI `arb-watch` (a module + console entry) that consumes the same SSE via `httpx` (already installed) +
  renders the pane in the terminal with arrow-key seat selection.
- **Dep decision (textual/rich NOT installed):** **recommend `textual`** (async, list+detail widgets,
  key handling — the right tool for an interactive pane) as a new dependency. Alternative: `rich` (live tables,
  simpler, no real interactivity) or stdlib `curses` (no dep, much more code). Recommend textual; flag for the
  panel + Mark's spec review since it adds a dep.
- Auth: a token passed via `--token`/env (the TUI is a trusted local client).
- Render: textual App with a DataTable (seats) + a log/RichLog (timeline); an httpx async SSE reader feeds both.

## Shared contract module (DRY across tracks)
A small `arb_visibility_client` (Python) with the **seat-state reducer mirroring 4a's `_reduce_seat`** + an SSE
line parser, reused by the TUI (and the E2E). The web reimplements the reducer in JS (can't share Python) but to
the SAME contract — a contract test pins both against recorded SSE fixtures so they can't drift.

## Parallel-build plan (Mark's ask: codex 4× — 2 impl + 2 review)
- Reconfigure `codex-bridge-dev` to `BRIDGE_MAX_PARALLEL=4` (plist env + bootout/bootstrap; seat idle,
  NOTIFY_INBOX=0 already set so the parallel-routing warning doesn't apply).
- Each track builds in its OWN git worktree (web, tui) so the two impl streams never collide.
- 2 slots run the two impl tracks concurrently; 2 spare slots absorb review/fix dispatches so the build doesn't
  serialize. Decorrelated certifying reviews still go to cold-Opus (in-process) + agy (separate seat) — they
  don't consume codex slots; codex's spare slots are for its contributor-review + fix turns.

## Testing
- web: a contract test (the JS reducer vs recorded SSE fixtures) + an httpx test that `GET /` serves the page +
  the auth gate on the static routes; manual visual check.
- TUI: unit-test the reducer + SSE parser (reuse the shared client); a smoke test that the App builds + ingests a
  fixture SSE stream without error; manual visual check.
- Both: an E2E that runs the real visibility service + drives a real bridge tee (mirror 4a's E2E) and asserts the
  client surfaces both seats.

## Locked decisions (3-seat panel, unanimous) + folds (v2)
1. **TUI dep = `textual`**, in an OPTIONAL extra in `pyproject.toml` (`[project.optional-dependencies]`, e.g.
   `visibility = ["textual>=...", "httpx>=..."]`) + an `arb-watch` console-script entry — core bridge installs
   don't pull it. (`httpx` is already used by 4a tests but must become a declared dep for `arb-watch`.)
2. **Web auth = fetch + ReadableStream with `Authorization: Bearer`** (NO 4a change — 4a reads the header,
   `visibility.py:39-41`; NO `?token=`). Token pasted by the user → `localStorage` for v1 (note the XSS surface;
   acceptable internal tool) with an explicit clear-token/logout. Full browser OAuth is a later enhancement.
3. **Web hosting = served by the visibility service** (same-origin, no CORS). The static shell `GET /` + `GET
   /app.js` are **PUBLIC (unauthenticated)** — they hold no secret; only `/orchestrators` + the SSE routes stay
   Bearer-gated. (Resolves the testing inconsistency: assert static routes load WITHOUT a token; data routes 401
   without one.)

### Panel-found fixes folded into v2
- **[P1] Reconnect / Last-Event-ID.** `fetch`+ReadableStream does NOT auto-resend `Last-Event-ID` (that's
  EventSource-only), so each client (web JS AND the TUI) MUST track it manually and own reconnect/backoff. CRITICAL:
  only store/resend ids matching `^\d+-\d+$` (real Redis stream ids) — the gateway also emits SYNTHETIC ids
  (`backfill-{n}`, `stale-{task}`) which, fed back into `XREAD`, raise a Redis `ResponseError` → 500. On a
  synthetic/absent last id, omit the header (forces a fresh backfill). Both client specs pin this. (Optional tiny
  4a hardening, separate low-priority follow-up: have the gateway ignore a non-`\d+-\d+` Last-Event-ID and start
  fresh — defends every client; NOT required if clients filter correctly.)
- **[P2] MIRROR `_reduce_seat`, do NOT extract it from `visibility.py`.** The web track edits `visibility.py`
  (adds the `GET /` static route); extracting the reducer would ALSO edit `visibility.py` → the two parallel
  tracks collide AND it changes frozen 4a. The TUI's Python client COPIES the reducer; a contract test against
  recorded SSE fixtures guards drift. This is what keeps the two impl tracks file-disjoint.
- **[P2] SSE behind proxies:** the gateway's/page's SSE responses should carry `Cache-Control: no-cache` +
  `X-Accel-Buffering: no` so a reverse proxy (Nginx/Cloudflare) doesn't buffer real-time frames. (Note for the
  web track; a tiny gateway header add is acceptable as it's not a contract change.)
- **[P2] JS SSE parser:** skip comment lines (`: ping`) and buffer partial frames across `fetch` chunk
  boundaries (split on `\n\n` only on complete frames). Standard SSE-over-fetch.

### Parallel-build file map (must stay disjoint — panel-confirmed with the mirror decision)
- **web track** edits: `src/arb_memory/visibility.py` (adds public `GET /` + `GET /app.js` static routes) +
  new `src/arb_memory/static/{index.html,app.js}`. Does NOT touch the reducer.
- **tui track** edits: new `src/arb_memory/watch/` module (`arb-watch` CLI, the copied reducer + SSE client) +
  `pyproject.toml` (the optional extra + console script). Does NOT touch `visibility.py`.
- Only near-overlap: both might touch `pyproject.toml` (web likely doesn't; keep the pyproject edit tui-only).
  Two worktrees off the same base; merge each independently.

## Out of scope
Write/control (read-only watch); cross-orchestrator fleet board; any 4a gateway change beyond a static-serve
route for the web page (the SSE/auth contract stays frozen).

# ARB Watch — Go/Charm Bubble Tea spike (design)

**Status:** design (2026-06-26). A **controlled comparison** spike: build a Go/Charm
(Bubble Tea + Lip Gloss + Bubbles) reimplementation of the `arb-watch` TUI **alongside** the existing Python
Textual one (`src/arb_memory/watch/`), as a pure **client** of the same gateway. Run side-by-side, judge looks
+ dev-feel with the Python version as the control. Built the same way the Python one was
(design→panel→spec→panel→plan→panel→codex-TDD→review) so the comparison is fair on workflow too.

## Why build-alongside (the framing)
`arb-watch` is a **read-only client** — it reads SSE for live + (eventually) psql for retrospective; it owns no
state. So a Go Bubble Tea watcher and the Python Textual watcher can point at the **same gateway, same
`arbmem:trace` stream, at the same time** and neither contends with the other. Nothing to migrate, no switch-off,
no decision forced. The spike commits us to nothing and yields strictly more information.

**The real question the side-by-side answers** is NOT "is Lip Gloss prettier" (that's a foregone conclusion) but
**"is it nicer *enough* to justify pulling the gateway + web backend to Go too?"** — because the TUI's looks are a
proxy for that larger decision. A standalone Go TUI talking to a Python gateway is a perfectly fine end state if
we just want a nicer watcher. The spike must let us distinguish "one nicer TUI" from "one Go backend end-to-end."

## Hard scope rails (what keeps the comparison clean)
- **Pure client. ZERO gateway logic in Go.** It hits ONLY the existing gateway HTTP/SSE endpoints. If we find
  ourselves writing stream-merging or psql in Go, we've drifted from "evaluate the TUI" into "rewrite the
  backend" — a different, larger, uncommitted decision. The server stays fixed; only the client varies.
- **Parity with the CURRENT Python version, nothing more.** Build exactly the slice Python already has, because
  that's where Lip Gloss's visual ceiling actually shows:
  - **Fleet menu** (root): list every orchestrator session (`GET /orchestrators`); select → drill in.
  - **Live seat table**: `GET /sse/orchestrator/{id}` → rows (seat · state · age · run), updating live; arrow-key
    cursor; select → drill into the seat.
  - **Transcript pane**: `GET /sse/seat/{task_id}` → Claude-Code-style render — `⏺` model text / tool call
    (`Bash(<cmd>)`, `Update(file) +N/−M`), `⎿` tool output (indented), transcript-only (no eval lifecycle noise).
  - **Age column**: `now − last_event_ts`, dim→amber(30s)→red(90s) for running seats, refreshed on a 2s tick.
  - **Seat-detail header bar**: distinct background, `seat · agent(codex/agy/pi) · model? · run · state · age`.
  - **Keys**: ↑/↓ navigate, enter drill, `m` fleet menu, `q` quit. **Known Python-only controls deliberately
    omitted from the spike** (they exist in current `arb-watch` — `app.py:66-73,329-363` — but don't move the
    looks verdict): `c` copy, `t` timestamps, `l` labels, `f` full-width. Naming them as intentional cuts (not
    "parity gaps") keeps the comparison honest (panel P2).
- **Explicitly OUT of the spike:** the merged all-seats view and seq-scrubbing. Those don't change the looks
  verdict and they're where the real surface area + rewrite cost live — building them would bias the comparison
  toward "big rewrite" instead of answering the looks question.

## Architecture
A single Go module `tools/arb-watch-go/` (module `arb-watch-go`; go 1.26; bubbletea v1.3 + lipgloss v1.1 +
bubbles v1.0 already fetched). Files, each one responsibility:

1. **`sse.go` — transport (pure client).** `fetchOrchestrators(baseURL, token) ([]string, error)` (HTTP GET);
   `streamSSE(url, token string, ch chan<- Frame, done <-chan struct{})` — opens the SSE GET with `Authorization:
   Bearer`, parses `event:`/`data:` frames (skip `:` heartbeats; 1 MiB buffer for large transcript frames),
   pushes decoded `Frame{Event string; Data map[string]any}` onto `ch` until the stream ends or `done` closes. No
   business logic.
2. **`reduce.go` — pure transforms (the testable core).** Ported 1:1 from the Python so behaviour is comparable:
   - `reduceSeat(prev, entry map[string]any) map[string]any` — mirror `watch/reducer.py reduce_seat` (running/
     done/failed/voted/stale + last_event_ts). (Frames from the gateway are often pre-reduced — use `data`
     directly when it carries `state`, else reduce. Same rule as the Python `upsert_seat`.)
   - `agentOf(seatID string) string` — first seat-id segment → codex/agy/pi/….
   - `isLifecycleNoise(frame Frame) bool` — drop non-transcript (eval/audit) events from the timeline.
   - `formatCommand(raw string) string` — strip `/bin/zsh -lc '<cmd>'` → `Bash(<cmd>)`.
   - `renderTranscriptLine(data map[string]any) string` — the `⏺`/`⎿` line (model_text/thinking/command_*/
     apply_patch), Lip-Gloss-styled.
   - `ageLabel(seconds float64) string` + `ageStyle(seconds float64, state string) lipgloss.Style`.
   These are where Go's `testing` + table-driven tests pin parity with the Python output (TDD lands here).
3. **`model.go` — Bubble Tea model (state + Update + View).** `view ∈ {orchestrators, seats}`; holds
   `orchestrators`, `seats map[taskID]seat` + ordered `seatOrder`, cursors, `selectedTask`, a
   `viewport.Model` for the transcript, the active stream's `cancel context.CancelFunc` + `frameCh` + a
   monotonically-increasing **`streamGen int`**, terminal `width/height`. **Streaming pattern (panel-hardened):**
   a goroutine runs `streamSSE(ctx, …)` writing to `frameCh`; a `listen(ch, gen)` `tea.Cmd` does a comma-ok
   `frame, ok := <-ch` → returns `frameMsg{gen, frame}` (or `streamClosedMsg{gen}` when `ok==false`); `Update`
   **drops any msg whose `gen != m.streamGen`** (kills the stale-channel race) and re-issues `listen` only for the
   current gen. Switching streams: `m.cancel()` (cancels the ctx → unblocks the HTTP body read) + `m.streamGen++`
   + new ctx/ch/cancel. A 2s `tea.Tick` → `tickMsg` re-renders ages + header. On `streamClosedMsg` for the
   current gen: **bounded-backoff reconnect** (see Error handling). `View` = Lip Gloss
   `JoinHorizontal(seat table | JoinVertical(header bar, viewport))`. `Update` handles `tea.WindowSizeMsg` →
   resize the layout + the viewport width/height (transcript lines wrap to the viewport width).
4. **`main.go` — entry.** Flags `--base-url`, `--token` (or `ARB_VISIBILITY_TOKEN`), `--orchestrator` (optional;
   omitted → start at the fleet menu, matching Python). Build the model, `tea.NewProgram(m, tea.WithAltScreen()).Run()`.

### Concurrency contract (panel P1 — the load-bearing part)
- **`streamSSE(ctx context.Context, …)`** uses `req.WithContext(ctx)` so cancelling the ctx unblocks a goroutine
  parked in the HTTP body read (a bare `done` channel does NOT — it can't interrupt `Read`). Every channel send is
  cancellation-aware: `select { case ch <- frame: case <-ctx.Done(): return }`, and it closes the response body on
  return. This is the real equivalent of the Python control's `worker.cancel()`.
- **Stream-generation tag:** `frameMsg`/`streamClosedMsg` carry the `gen` they were issued under; `Update` ignores
  any whose `gen != m.streamGen`. Without this, an already-scheduled `listen(oldCh)` fires after drill-in and
  injects a dead stream's frame into the new view (panel P1, opus+codex).
- Tests must drive seats→seat→menu while injecting frames and assert **only the current stream mutates state** +
  no goroutine leak (cancel + assert the goroutine returns).

## Data flow
gateway SSE (unchanged) → `streamSSE` → `frameCh` → `frameMsg` in `Update` → `reduceSeat`/`renderTranscriptLine`
(pure) → model state → `View` (Lip Gloss). Identical inputs to the Python client; only the render layer differs —
which is exactly the variable under test.

## Error handling + reconnect (panel P1 — reconnect IS a looks-verdict input)
The Python control auto-reconnects with bounded backoff and resumes via `Last-Event-ID` (`sse_client.py:68-97`).
**The spike MUST match this** — otherwise a gateway blip leaves the Go client dead while the Python one silently
survives, a *visible* asymmetry that contaminates the comparison (panel, opus+codex). So:
- On `streamClosedMsg` (current gen) or a connect error: reconnect with bounded backoff (e.g. 0.5s→…→5s cap),
  Bearer on every request.
- **Endpoint-aware resume IDs (codex P1 — a real protocol detail):** `/sse/orchestrator/{id}` emits raw Redis
  stream IDs (`^\d+-\d+$`); `/sse/seat/{task_id}` now emits **composite** resume IDs `e=<events_id>;t=<trace_id>`
  (server merges the bus + trace streams — `visibility.py` `_seat_resume_id`/`SEAT_RESUME_ID_RE`). The Go
  transport tracks the last seen `id:` per stream and resends it as `Last-Event-ID` on reconnect, **without
  hard-coding `^\d+-\d+$`** — it must pass through the composite seat id too. Skip synthetic ids
  (`backfill-N`/`stale-X`). Parser tests for BOTH id shapes. (This is client protocol handling, not gateway logic.)
- A failed `fetchOrchestrators` shows an error line. Malformed frames are skipped. Defensive map reads — never
  panic on a missing/odd field.

## Testing
Go `testing`, table-driven, on the pure `reduce.go` functions — the TDD core: `reduceSeat` state transitions
(parity with the Python reducer's cases), `agentOf`, `formatCommand` (port the Python regex VERBATIM — DOTALL +
inner-quote strip), `renderTranscriptLine` (**a golden test vector per kind** → `⏺`/`⎿` shape, incl. the 2-space
`⎿` indent + the `\n`→`\n    ` continuation-line indent), `ageLabel`/`ageStyle` thresholds, `isLifecycleNoise`.
Where practical, assert the Go render's plain text equals the Python `_render_event(...).plain` for the same
input (golden parity fixtures — panel P2). `sse.go` gets: a frame-parser test against a synthetic SSE byte stream
(httptest server); a **resume-id parser test for BOTH shapes** (Redis `^\d+-\d+$` and composite `e=…;t=…`,
skipping synthetic ids). `model.go`'s `Update` gets message-driven tests: a `frameMsg` populates the seat table;
selecting drills in; `m` returns to menu; **stream-switch correctness** (seats→seat→menu with injected frames →
only the current `streamGen` mutates state; a stale-`gen` `frameMsg` is ignored); reconnect on `streamClosedMsg`.
Bubble Tea models are plain funcs, so `Update` is unit-testable without a terminal. No live-gateway dependency in
the unit tests.

**E2E (the empirical gate, per the usual flow):** a Go test (build-tagged, run against the live demo gateway)
that drives the REAL transport — `fetchOrchestrators` + `streamSSE` against `http://localhost:8810` with a real
token — for one seat with a real transcript, and asserts the decoded frames flow + `renderTranscriptLine`
produces `⏺`/`⎿` output (the Go analogue of the Python `e2e_visibility_roundtrip`). Skips cleanly if the gateway
isn't reachable.

## Success criteria (what closes the spike)
Run `arb-watch-go` and the Python `arb-watch` side-by-side against the same demo gateway, watching the same seats.
Judge: visual quality (Lip Gloss vs Textual), dev-feel (Go build/iterate vs Python), and — the load-bearing
question — whether it makes us want **one Go backend end-to-end** or just **one nicer TUI**. The spike answers the
question; it commits to nothing.

## Out of scope (restate)
Merged all-seats view; seq-scrubbing; copy/timestamp/label toggles; auto-reconnect; any gateway/psql/stream-merge
logic in Go; the model-wiring backlog item (separate). Retrospective psql reads are a *follow-on* if the spike
graduates, not part of it.

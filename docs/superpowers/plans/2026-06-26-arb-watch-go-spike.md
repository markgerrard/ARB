# ARB Watch — Go/Charm Bubble Tea spike — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Go TDD: write a `*_test.go`
> with table cases, `go test ./...` → FAIL, implement, `go test` → PASS, commit. Checkbox steps.

**Goal:** A Go/Charm (Bubble Tea + Lip Gloss + Bubbles) `arb-watch` reimplementation — a PURE CLIENT of the
existing gateway — at parity with the Python Textual version, for a controlled looks/dev-feel comparison.

**Architecture:** `tools/arb-watch-go/` (module `arb-watch-go`, go 1.26; bubbletea v1.3 + lipgloss v1.1 +
bubbles v1.0 + bubbles/viewport already fetched). Pure-transform core (`reduce.go`) is the TDD seam; `sse.go` is
the transport; `model.go` is the Bubble Tea model; `main.go` is the entry. NO gateway logic in Go.
Spec: `docs/superpowers/specs/2026-06-26-arb-watch-go-spike-design.md`.

## Global Constraints
- **Pure client, ZERO gateway/psql/stream-merge logic in Go.** Hits only `/orchestrators`,
  `/sse/orchestrator/{id}`, `/sse/seat/{task_id}` with `Authorization: Bearer`.
- **Parity with current Python `src/arb_memory/watch/` ONLY** — fleet menu, live seat table + age column,
  transcript ⏺/⎿ pane, header bar, transcript-only filtering. OUT: merged view, seq-scrub, copy/timestamp/label/
  full-width toggles (named cuts).
- **Concurrency contract (load-bearing):** `streamSSE(ctx, …)` with `req.WithContext(ctx)`; cancellation-aware
  sends (`select { case ch<-f: case <-ctx.Done(): }`); stream-generation tag on frame msgs (`Update` drops stale
  `gen`); cancel-on-switch unblocks the HTTP read + no goroutine leak.
- **Reconnect parity:** bounded-backoff reconnect on stream close/error, Bearer every request, endpoint-aware
  resume ids (Redis `^\d+-\d+$` for orchestrator; composite `e=…;t=…` for seat; skip synthetic ids).
- Render parity: port `formatCommand` regex + the `⏺`/`⎿` shapes verbatim; golden tests vs the Python `.plain`.

### Test harness
```bash
cd /Users/<user>/<workspace>/tools/arb-watch-go && export PATH="/opt/homebrew/bin:$PATH"
go test ./...        # unit
go build ./...       # compiles
```

---

### Task 1: `reduce.go` — the pure-transform core (TDD seam)

**Files:** Create `tools/arb-watch-go/reduce.go`, `tools/arb-watch-go/reduce_test.go`.

**Interfaces (Produces):**
- `reduceSeat(prev, entry map[string]any) map[string]any` — mirror `watch/reducer.py reduce_seat`: task_started/
  continuing→`state:running`+`last_event_ts`+`last_event`; task_finished→`done`/`failed` by `ok`; vote→`voted`
  (unless already done/failed); stale→`stale`. Carry `seat_id`, `run_id`, `orchestrator`, `task_id`, `model`.
- `agentOf(seatID string) string` — first `-`-segment, mapped (codex/agy/pi/gemini/cursor/grok/kimi/claude), else
  the raw head, else `"?"`.
- `isLifecycleNoise(ev map[string]any) bool` — `true` unless `source == "transcript"` (drops eval/audit).
- `formatCommand(raw string) string` — port `app.py _SHELL_WRAPPER` VERBATIM: `^\S*sh\s+-l?c\s+(.*)$` (DOTALL),
  strip matching outer quotes → `Bash(<inner>)`; non-shell → unchanged.
- `renderTranscriptLine(ev map[string]any) string` — Claude-Code shape (PLAIN text, styling added in View):
  model_text→`⏺ <content>`; model_thinking→`⏺ <content>` (thinking); command_started/tool_call→`⏺ <formatCommand
  (tool_name|content)>`; command_output/finished/tool_output→`  ⎿ <content>` with continuation lines indented
  (`\n`→`\n    `, trailing newline stripped); apply_patch (tool_name=="apply_patch" & meta.file)→`⏺ Update(<file>)
  +<added>/-<removed>` (+ `\n  ⎿ <content>` if content).
- `ageLabel(seconds float64) string` — `<60`→`Ns`, `<3600`→`Nm`, else `Nh`.
- `ageStyle(seconds float64, state string) lipgloss.Style` — running: `<30` dim, `<90` amber(yellow), `>=90` red;
  non-running → dim.

- [ ] **Step 1: Failing tests** — table-driven for each fn. `reduceSeat`: the 5 transition cases (assert state +
  last_event_ts). `agentOf("codex-ff-demo")=="codex"`, `agy-bridge-dev`→`agy`, `pi-glm`→`pi`. `formatCommand`:
  `/bin/zsh -lc 'python3 fibonacci.py'`→`Bash(python3 fibonacci.py)`; `python3 x`→unchanged; `Read`→unchanged.
  `renderTranscriptLine` — **golden vector for EVERY branch (panel P1, opus+agy — don't miss these):**
  - model_text `"hi"`→`⏺ hi`; model_thinking `"pondering"`→`⏺ pondering`; **model_thinking empty→`⏺ thinking`**;
  - command_started tool_name=`/bin/zsh -lc 'ls'`→`⏺ Bash(ls)`; **tool_call with only content (no tool_name)→`⏺ <content>`**;
  - command_output `"a\nb\n"`→`  ⎿ a\n    b` (2-space lead, 4-space continuation, trailing `\n` stripped);
  - apply_patch {file:f.py,added:3,removed:1} (no content)→`⏺ Update(f.py) +3/-1`; **apply_patch WITH content
    `"+x"`→`⏺ Update(f.py) +3/-1\n  ⎿ +x`**;
  - **unknown kind `"weird"` with content `"z"`→`⏺ z` (the else/fallback branch)**.
  - **content is `.strip()`'d** before rendering (mirror Python — `(content or "").strip()`); assert a
    leading/trailing-space content renders trimmed.
  `ageLabel(5)=="5s"`, `ageLabel(90)=="1m"`. `isLifecycleNoise({source:eval})==true`, `{source:transcript}==false`.
- [ ] **Step 2: `go test ./...` → FAIL** (undefined funcs).
- [ ] **Step 3: Implement** `reduce.go` per the interfaces (defensive map reads via small `getString`/`getFloat`
  helpers; never panic on missing keys). **`reduceSeat` must COPY `prev` into a new map** (don't mutate the
  caller's map — panel P2, agy): `out := map[string]any{}; for k,v := range prev { out[k]=v }`.
- [ ] **Step 4: `go test ./...` → PASS.**
- [ ] **Step 5: Commit** `feat(arb-watch-go): pure-transform core (reduceSeat/render/age) + table tests`.

---

### Task 2: `sse.go` — transport (pure client, context-cancellable, resume-aware)

**Files:** Create `tools/arb-watch-go/sse.go`, `tools/arb-watch-go/sse_test.go`.

**Interfaces:**
- `type Frame struct { Event string; ID string; Data map[string]any }`
- `fetchOrchestrators(baseURL, token string) ([]string, error)` — GET `/orchestrators`, decode `{orchestrators}`.
- `streamSSE(ctx context.Context, url, token, lastID string, ch chan<- Frame)` — `req.WithContext(ctx)`, Bearer,
  `Last-Event-ID` header if `lastID` is real (Redis id or composite — NOT synthetic `backfill-`/`stale-`), parse
  `event:`/`id:`/`data:` frames (skip `:` heartbeats; 1 MiB buffer), `select { case ch<-Frame{…}: case
  <-ctx.Done(): return }` per send; close body + `close(ch)` on return.
- `isResumableID(id string) bool` — `^\d+-\d+$` OR composite `^e=[^;]*;t=[^;]*$`; false for `backfill-*`/`stale-*`/"".

- [ ] **Step 1: Failing tests** — `parse`/`streamSSE` against an `httptest` server emitting a multi-frame SSE body
  (incl. a `: ping` + `id:` lines + a partial trailing frame): assert the decoded `Frame`s (event/id/data) + that
  ctx-cancel stops it. `isResumableID`: `"1782-0"`→true, `"e=5-0;t=9-0"`→true, `"e=-;t=9-0"`→true, `"e=5-0;t=-"`→
  true (gateway one-sided `-` sentinel — panel P2 codex), `"e=-;t=-"`→false (both empty = no cursor),
  `"backfill-3"`→false, `"stale-x"`→false, `""`→false.
  `fetchOrchestrators` against an httptest server returning `{"orchestrators":["o1","o2"]}`.
- [ ] **Step 2: `go test ./...` → FAIL.**
- [ ] **Step 3: Implement** `sse.go` (bufio.Scanner with 1 MiB buffer; defensive JSON decode — skip malformed).
- [ ] **Step 4: `go test ./...` → PASS** (+ a goroutine-leak check: cancel ctx mid-stream, assert streamSSE returns).
- [ ] **Step 5: Commit** `feat(arb-watch-go): SSE transport (context-cancellable, Bearer, resume-id aware)`.

---

### Task 3: `model.go` — Bubble Tea model (state + Update + View + viewport + reconnect)

**Files:** Create `tools/arb-watch-go/model.go`, `tools/arb-watch-go/model_test.go`.

**Interfaces (Consumes Task 1 + 2):**
- `type model struct { baseURL, token, orchestrator, view string; orchestrators []string; orchCursor int;
  seats map[string]map[string]any; seatOrder []string; seatCursor int; selectedTask string;
  vp viewport.Model; transcript []string; cancel context.CancelFunc; frameCh chan Frame; streamGen int;
  streamURL, lastID string; backoff time.Duration; width, height int; status string;
  startStream func(m *model, url, lastID string) tea.Cmd }`
  — **`startStream` is a FIELD (injectable seam), not a method (panel P1, codex)**: production sets it to the real
  one (spawns `go streamSSE`); model unit tests inject a fake that records the call + returns a controllable
  `listen` over a test channel, so cancel/reconnect/stale-gen behaviour is testable without real HTTP. `lastID`
  tracks the last resumable `id:` seen on the current stream (for reconnect resume); `backoff` is the current
  reconnect delay (reset to base on a successful frame, grows to a cap on `streamClosedMsg`).
- msgs: `orchestratorsMsg []string`, `frameMsg{gen int; f Frame}`, `streamClosedMsg{gen int}`, `tickMsg`,
  `errMsg error`.
- `Init() tea.Cmd` (fetch orchestrators or, with `--orchestrator`, enter seats). `Update(tea.Msg) (tea.Model,
  tea.Cmd)`. `View() string`.
- `listen(ch chan Frame, gen int) tea.Cmd` — comma-ok read → `frameMsg{gen,f}` or `streamClosedMsg{gen}`.
- `startStream(url, lastID string) tea.Cmd` — `m.streamGen++`, new ctx+cancel+ch, `go streamSSE(ctx,…)`, return
  `listen(ch, m.streamGen)`.

- [ ] **Step 1: Failing tests** — drive `Update` directly (no terminal), with a FAKE `startStream` injected (records
  calls; no real HTTP): (a) `orchestratorsMsg{["o1","o2"]}` in `view=="orchestrators"` populates the list;
  (b) selecting an orchestrator (enter) → `view=="seats"` + the fake `startStream` was called (stream started);
  (c) a `frameMsg` with a seat frame upserts into `seats`/`seatOrder`; (d) selecting a seat → a transcript
  `frameMsg` appends a rendered line (filtered via `isLifecycleNoise`); (e) **stale-gen drop**: a `frameMsg` whose
  `gen != m.streamGen` does NOT mutate state; (f) `m` returns to `orchestrators` and the prior `cancel` was called
  (assert via a flag the fake sets) + view reset; (g) **reconnect+backoff**: `streamClosedMsg` (current gen)
  re-invokes `startStream` with the tracked `lastID` and grows `backoff`; a `frameMsg` resets `backoff` to base.
- [ ] **Step 2: `go test ./...` → FAIL.**
- [ ] **Step 3: Implement** `model.go`: Update routing (keys ↑/↓/enter/m/q, msgs), the stream-gen guard, the
  cancel-on-switch, viewport append + auto-bottom, `tea.WindowSizeMsg` → resize layout + viewport, 2s `tea.Tick`,
  reconnect with bounded backoff. `View` = `lipgloss.JoinHorizontal(lipgloss.Top, seatTable,
  lipgloss.JoinVertical(lipgloss.Left, header, vp.View()))` (**note the leading `lipgloss.Position` arg — panel P1
  codex**), header bar with a distinct background (`lipgloss.NewStyle().Background(lipgloss.Color(...))`) showing
  `seat · agentOf · model? · run · state · ageLabel`, seat rows with `ageStyle` colour, transcript styled
  (`⏺` green/cyan, `⎿` dim).
- [ ] **Step 4: `go test ./...` → PASS.**
- [ ] **Step 5: Commit** `feat(arb-watch-go): Bubble Tea model (drill-down, viewport, stream-gen, reconnect)`.

---

### Task 4: `main.go` — entry + manual build/run check

**Files:** Create `tools/arb-watch-go/main.go`.

**Interfaces:** `main()` — flags `--base-url` (default `http://127.0.0.1:8810`), `--token` (or env
`ARB_VISIBILITY_TOKEN`), `--orchestrator` (optional). Build the model + `tea.NewProgram(m,
tea.WithAltScreen()).Run()`; error if no token.

- [ ] **Step 1:** Implement `main.go`.
- [ ] **Step 2: `go build ./...`** → a binary builds with no error. (TUI visual check is manual — note in report.)
- [ ] **Step 3: `go vet ./...`** clean.
- [ ] **Step 4: Commit** `feat(arb-watch-go): main entry (flags + Bubble Tea program)`.

---

### E2E (the empirical gate)
`tools/arb-watch-go/e2e_test.go` (build tag `//go:build e2e`): against the live demo gateway
(`http://localhost:8810`, token from `ARB_VISIBILITY_TOKEN`/`ff-demo-token`) — `fetchOrchestrators` returns ≥1;
`streamSSE` on `/sse/seat/<a real task>` yields frames; `renderTranscriptLine` over the transcript frames produces
≥1 line containing `⏺`. **Skips cleanly (`t.Skip`)** if the gateway isn't reachable / no token. Run:
`go test -tags e2e ./...`. This is the Go analogue of the Python `e2e_visibility_roundtrip` and the spike's
empirical gate — it proves the Go client really talks to the real gateway end-to-end.

## Self-Review
Spec coverage: reduce core (T1) + transport (T2) + model (T3) + entry (T4) + E2E. Concurrency contract (ctx-cancel,
stream-gen, cancellation-aware sends) in T2+T3 + tested in T3-step1(e/f) & T2-step4. Reconnect + composite resume
ids in T2 (`isResumableID`) + T3 (reconnect cmd). Render parity via golden tests (T1). Named cuts honored.
Type consistency: `Frame`/`map[string]any` shapes flow T2→T3; `reduceSeat`/`renderTranscriptLine` signatures match
across tasks. Placeholder risk: the `View` styling is described, not pixel-specified — that's intentional (it's the
variable under test); the implementer picks lipgloss colours close to the Python ($accent header, green/cyan/dim).

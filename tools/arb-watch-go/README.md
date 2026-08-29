# arb-watch-go

The **maintained** ARB fleet watcher — a Go / [Bubble Tea](https://github.com/charmbracelet/bubbletea)
TUI. Chosen 2026-06-26 as the canonical implementation, superseding the Python/Textual
`arb-watch` (`src/arb_memory/watch/`).

> The Python version is **deprecated but retained** (so the original design/work isn't lost). It
> still runs — both are pure clients of the same visibility gateway — but new work lands here. The
> Python entry point prints a deprecation notice on launch.

## What it is

A pure **client** of the ARB visibility gateway: it talks only to `/orchestrators`,
`/sse/orchestrator/{id}`, and `/sse/seat/{task_id}` (Bearer token) and renders the live fleet —
seat list, per-seat transcript (Claude-Code-style `⏺`/`⎿`), age column, header. **Zero** gateway /
stream-merge / DB logic lives in Go; if you find yourself writing that here, you've drifted into a
backend rewrite.

## Build & run

```sh
cd tools/arb-watch-go
go build -o arb-watch-go .
./arb-watch-go --base-url http://127.0.0.1:8000 --token "$ARB_VISIBILITY_TOKEN"
```

Flags: `--base-url` (default `http://127.0.0.1:8000`), `--token` (or `ARB_VISIBILITY_TOKEN`),
`--orchestrator` (open one directly), `--no-mouse` (disable mouse capture for native
selection / mosh).

## Keys

| Key | Action |
|-----|--------|
| `↑`/`↓` | Seat list focused: move the seat cursor (auto-previews the transcript). Transcript focused: scroll one line. |
| `←` / `→` | Focus the seat list (left) / transcript (right). The active pane's border is highlighted. |
| `⇧↑` / `⇧↓` | Page up / down in the transcript (also `PgUp`/`PgDn`). |
| `Enter` | Open the cursor seat / orchestrator. |
| `c` | Copy the whole transcript (OSC-52). |
| `Ctrl+C` | Copy-range overlay: line-numbers the transcript; type `10-20` / `12` / `10-` / `-5` + Enter to copy those lines. Esc cancels. (`q` quits — `Ctrl+C` no longer quits.) |
| `e` | Expand truncated tool output (default: 6 lines + `… +N`). |
| `t` / `l` | Toggle timestamps / source-kind labels. |
| `s` / `a` | Cycle the seat-pane status / agent filter. |
| `f` | Full-width transcript (hides the seat list). |
| `m` | Back to the fleet menu. |
| `q` | Quit. |

Features beyond Python parity: full-width seat header, `e` expand toggle, `s`/`a` filters,
`Ctrl+C` copy-range, and the `←`/`→` pane-focus model.

## Tests

```sh
go test ./...                      # unit + render golden tests
go vet ./...
ARB_VISIBILITY_TOKEN=… ARB_WATCH_GO_E2E_BASE_URL=http://localhost:8810 \
  go test -tags e2e -run TestLiveGatewayE2E .   # live gateway, read-only (skips if unreachable)
```

## Design

The paneled spike design + plan live in
`docs/superpowers/specs/2026-06-26-arb-watch-go-spike-design.md` and
`docs/superpowers/plans/2026-06-26-arb-watch-go-spike.md`.

# ARB Visibility worktrack — follow-ups (designed backlog)

> Status: **backlog, designed, not started** (logged 2026-06-27). Sits on top of the now-live
> observability + gateway stack (see memory `arb-observability-prod-live`). Pairs with the Go
> client-edge worktrack (`2026-06-27-go-client-edge-and-seat-host-container-design.md`).

Context: the gateway + `arb-watch-go` are live end-to-end. Watching the fleet surfaced two distinct
gaps, both real, both backlogged here.

---

## A. Live transcript tail — DONE (2026-06-27, `1457559`)

**Resolved as a wiring fix, NOT a toggle.** Reading the source: the seat endpoint already implements
both PG backfill (durable history) AND a live tail of the db7 `arbmem:trace` stream (`read_trace_live`)
in one endpoint — the "live/durable toggle" was already built in. It was dormant only because the
visibility container wasn't passed `ARB_TRACE_REDIS_URL`. Wiring that env var activated the live tail;
verified E2E (transcript frames stream command-by-command in realtime, +5→15s as the seat works, not
batch-at-end). The default is now the ideal: complete history (PG) + new entries live (Valkey). An
explicit `?source=live|durable` toggle is redundant; add only if explicit live-only/durable-only
control is later wanted. Original design retained below for reference.

### (original backlog design — superseded by the wiring fix)

**Observed:** the seat *lifecycle* (appear/run/finish roster) is realtime via `events:live` (db12),
but the detailed *transcript* (tool calls + output) is read from PG `transcript_io` as **backfill** —
so it "wasn't there, then all at once was" (it lands in a batch a few seconds behind, after the
transcript consumer writes it). Both planes already exist; the watcher should let the operator choose.

**Design:**
- **Gateway** (`src/arb_memory/visibility.py`): add `?source=live|durable` (default `durable`) to the
  seat endpoint `/sse/seat/{task_id}`.
  - `durable` = current behaviour: PG `transcript_io` backfill + `events:live` lifecycle tail.
  - `live` = `XREAD` the **transcript Valkey stream (db7, `ARB_TRACE_REDIS_URL`)** filtered to
    `task_id`, streamed incrementally as the seat works ("watch it type"). The gateway tails db7 as an
    independent reader (XREAD doesn't consume; or a dedicated consumer group) — the durable transcript
    consumer remains the PG writer. Gateway needs read access to db7 wired (it currently reads
    `ARB_BRIDGE_BUS_URL`/db12 + PG only; add `ARB_TRACE_REDIS_URL` read).
- **Watcher** (`tools/arb-watch-go`, Go): a keybind (e.g. `v`) on the open seat view toggles the
  source; re-subscribe to the SSE with the new `source`, render the live stream incrementally.
- **Trade-off to show in the UI:** `live` = realtime + incremental but **recent-only** (db7 is
  maxlen-bounded/ephemeral — may miss entries aged out past maxlen); `durable` = **complete + full
  history** but batch-lands a few seconds behind. Default `durable`; toggle to `live` to watch active
  work.

**Scope:** read-side only (gateway holds the SELECT/read role); no producer or schema change.

---

## B. Transcript fidelity for non-codex seats (agy / pi)

### pi-sdk — DONE (2026-06-27, `e9952ba`)
pi-sdk seats now capture `command_started` (with tool args), `command_output` (tool result content),
`command_finished` — codex parity. The SDK *does* expose `args` (on `tool_execution_start`) and
`result.content[].text` (on `tool_execution_end`); host.mjs forwards them, pi_sdk.py maps them. 14
Python + 23 Node tests; E2E verified on both pi-glm and pi-m3 (command_started/output/finished + content
in prod transcript_io). Tri-reviewed (agy + cold-Opus approve, P2-only). **P2 follow-ups (deferred, all
bounded downstream by the flusher's 256KB cap + maxlen):** (1) a None/empty tool result coalesces
command_finished into the command_started row; (2) no source-side size cap — a multi-MB tool result
transits Node+Python before the 256KB truncation; (3) non-text content blocks serialize as JSON noise.
**agy-print remains final-text-only** (one-shot CLI, no per-step events) — accepted limitation.

### agy — DONE via agy-tmux (2026-06-27, `309c59f`) — overturns the agy limitation
agy seats CAN be granular after all: the `agy_tmux` engine drives a fresh interactive agy in tmux and tails
the **structured `transcript.jsonl`** agy writes (it was never truly final-text-only — that was a stdout-only
view). Watchable like codex/pi (command_*/model_text), nonce-based brain ownership, fresh-per-turn isolation.
See [[agy-tmux-streaming]] / `docs/superpowers/specs/2026-06-27-agy-tmux-streaming-design.md`. Dispatch
`--engine agy-tmux`. agy-print (the one-shot engine) is unchanged.

### (original analysis)


**Observed (empirical, prod `transcript_io`):**
| Seat | rows | kinds | tool calls |
|---|---|---|---|
| codex-bridge-dev | 102 | `command_started`/`command_output`/`command_finished`/`model_text` | 43 |
| agy-bridge-dev | 2 | `model_text` only | 0 |
| pi-sdk-glm | 1 | `model_text` only | 0 |

codex captures the full command-by-command transcript; **agy and pi capture only their final
`model_text`** — even on a multi-step task that ran `find`/`git` internally, only the final reply was
recorded. So non-codex seats are transcript-thin: the watcher shows full detail for codex, just the
reply for agy/pi, because that's all the bridge captures.

**Design (capture-side, in the bridge — engine-specific, and asymmetric in feasibility):**
- **pi-sdk** — FEASIBLE: the pi SDK's agent loop emits tool-call / tool-result events. Wire those into
  the bridge's transcript capture (map to `command_started`/`command_output`/`command_finished` or a
  `tool_call`/`tool_result` kind, with `tool_name`), the same shape codex produces. This is the higher
  value of the two (pi seats are used heavily).
- **agy-print** — LIMITED: agy-print is a one-shot CLI invocation; the bridge sees its stdout/final
  output, not per-step events. Granular tool-call capture likely isn't possible without agy emitting
  structured events — agy seats may stay final-text-only. Document this as an accepted limitation
  rather than chasing it; revisit only if agy gains a streaming/structured mode.

**Scope:** bridge transcript-capture path + per-engine event mapping. Independent of A.

---

## Sequencing
A (the toggle) is the cleaner, read-only win and the one the operator directly asked for — do it
first. B (fidelity) is capture-side and splits by engine: do **pi-sdk** (feasible, high value), note
**agy-print** as a likely permanent limitation. Both ride alongside the Go/visibility worktrack.

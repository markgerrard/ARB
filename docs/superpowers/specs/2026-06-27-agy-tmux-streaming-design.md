# agy-tmux streaming — design (v2: structured transcript tail)

> Status: **design, spiked-out.** Supersedes the v1 pane-scrape/emulator approach (which the design panel
> REQUEST-CHANGED with 2 P0s). The breakthrough: **agy writes a structured `transcript.jsonl`** per
> conversation, and the *interactive* session **live-appends** it during a turn — so we drive agy in tmux
> but read the *structured file*, not the TUI. Every panel P0/P1 dissolves. Goal: watch an agy seat work
> live in arb-watch-go, structured like codex/pi.

## Spike findings (all validated 2026-06-27 — the design rests on these)
- agy (v1.0.13, authenticated) writes `~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/transcript.jsonl`.
- **Structured events**, each `{step_index, source, type, status, created_at, content}`:
  `USER_INPUT` (the task, verbatim in `<USER_REQUEST>`), `PLANNER_RESPONSE` (model text), tool events
  (`VIEW_FILE`/`RUN_COMMAND`/`EDIT_FILE`/…) whose `content` carries the args **and** the result, `CHECKPOINT`.
- **`--print` writes the file at turn-END** (brain materialises on completion) → no live there.
- **Interactive agy LIVE-APPENDS** the file during the turn (observed 7→10 lines as it worked) → tailable live.
- **Trust-dialog**: auto-confirm on launch with `Enter` (Yes pre-selected).
- **`--dangerously-skip-permissions`** (agy-print already uses it) → no mid-turn per-tool permission prompts.
- **Completion signal**: status line flips `esc to cancel` (busy, + `⣽ Generating…`) → `? for shortcuts` (idle).
- **Task injection**: `tmux send-keys <task>` then `send-keys Enter`.
- Brain id can't be pinned (`--conversation` resumes only; no auth-safe data-root redirect) — but we **own the
  session** (fresh per turn), so its brain is the one our launch created. No content-matching, no race.

## Architecture — a new seat
A new **`agy_tmux` engine** (`src/agent_redis_bridge/engines/agy_tmux.py`) + an **`agy-tmux-dev` seat**.
agy-print stays the fast one-shot dispatch engine; agy-tmux is the watchable, structured-live engine.

## The engine — fresh interactive session per turn (isolation by construction)
`run_turn_with_progress(task, timeout, policy, on_event)` per turn:
1. **Launch** a fresh `agy --dangerously-skip-permissions` in a dedicated tmux session (fixed geometry,
   `--add-dir <cwd>`). Auto-confirm the trust-dialog (`Enter`); wait for the idle prompt (`? for shortcuts`)
   — if it never appears, fail-loud + unhealthy (don't hang).
2. **Inject** the task: `send-keys <task>` then `send-keys Enter` (escape literally — the `\n`/control-char
   care from the dispatch gotchas).
3. **Tail** the session's `transcript.jsonl` (the brain created by *our* launch) as it live-appends; for each
   new event, map → `on_event(...)` (see mapping) so the structured transcript streams live.
4. **Detect completion**: poll `capture-pane` for the busy→idle flip (`esc to cancel`→`? for shortcuts`),
   bounded by `timeout` (on timeout: `send-keys Escape`, mark unhealthy). Drain the final transcript appends.
5. **Extract result**: the last `PLANNER_RESPONSE` text → `TurnResult.result`.
6. **Cleanup**: kill the tmux session + **delete the brain dir** → no lingering transcript for a future agy to
   read (context-bleed P0 gone) + disk stays clean.

Fresh-per-turn = per-task isolation (matches agy-print's stateless contract); the cost is ~5–6s launch +
trust + brain-init per turn, acceptable for a watchable seat.

## Event mapping → the existing transcript schema (kind/tool_name/content/seq/ts)
Per transcript.jsonl event (ordered by `step_index`):
- `PLANNER_RESPONSE` → `model_text` (content = the text).
- tool events (`VIEW_FILE`/`RUN_COMMAND`/`EDIT_FILE`/etc.) → `command_started` (tool_name = the `type`,
  content = the args portion of `content`) + `command_output` (content = the result portion) + `command_finished`
  (status from the event's `status`). Splitting args vs result inside `content` is the parser's job; if not
  cleanly separable, emit the whole `content` as `command_output` (never drop it).
- `step_index` → `seq`; `created_at` → `ts`; `USER_INPUT`/`CHECKPOINT` → not re-emitted (USER_INPUT is the task
  we sent; CHECKPOINT may serve as the turn-end drain marker).
These flow through the **existing** progress→capture→transcript path (`handle_progress`/`_capture`/`_transcript_q`
→ flusher → trace stream → consumer → `transcript_io` + redaction + live tee) — identical to codex/pi. No new
plane, no new consumer, redaction applies (structured content, clean — no ANSI).

## Lifecycle / roster
The engine emits `task_started`/`turn_*`/`task_finished` so an agy-tmux seat appears in the orchestrator roster
and live-watch like codex/pi.

## Risks / open
- **Status-line fragility**: `esc to cancel`/`? for shortcuts` are agy-version-specific — centralise as
  constants + a startup self-check (assert the idle marker appears after trust-confirm); fail-loud, never hang.
- **Tail vs completion race**: the transcript may append a final event just after the pane goes idle — drain the
  tail (read to EOF / await CHECKPOINT) before returning.
- **send-keys escaping**: task text injects literally (one prompt, not many) — existing `\n`-quoting discipline.
- **tmux dependency** on the seat host (present on dev; document for deploy).

## Non-goals
Pane-scraping / terminal emulator (rejected — we read the structured file). agy-print unchanged (one-shot path).
Realtime cursor-accurate terminal fidelity (we stream structured events, not raw bytes).

## Testing
- Engine unit tests with a **fake tmux + fixture transcript.jsonl**: trust-confirm, task inject, live-tail
  event mapping (PLANNER_RESPONSE→model_text; tool event→command_started/output/finished with tool_name+content;
  seq from step_index), busy→idle completion, timeout/cancel, unhealthy-on-no-idle, brain cleanup/delete.
- Mapping unit test against a **real captured transcript.jsonl fixture** (a tool run) → assert the emitted
  events match codex-parity shape.
- Live E2E (authenticated agy present): drive an agy-tmux tool task with a run_id → assert structured events
  stream to the trace stream live AND `transcript_io` has command_started/command_output/command_finished +
  model_text for the run (redacted), and the brain dir is deleted after.

# Claude-layer visibility — warm orchestrator + cold-Opus into ARB Visibility (design)

> Status: design, **panel-reviewed + revised** 2026-06-28. Brainstormed 2026-06-28; empirical gate
> (incremental transcript flush) verified (§ Empirical findings). 4-seat design panel (codex +
> cold-Opus REQUEST CHANGES; pi-GLM + agy APPROVE WITH NOTES) — approach unanimously sound; this
> revision closes the convergent plan-level gaps: synthetic lifecycle events, the `task_id`/
> `orchestrator` identity contract, explicit redact-before-live-tee (events:live is not redacted
> today), the standalone tee-helper extraction, marker-failure → session-run_id fallback, offset
> commit-point + rotation, and an executable format self-check. agy-print's review (the prior-art
> seat) additionally caught a contradiction — **offsets must live in Redis, not a local file, since the
> container mounts are read-only** — plus the live-vs-trace stream-routing rule, a stable
> bind-mountable cold-Opus path, and container-visible fail-loud (`drift_error` event + non-zero exit).

## Problem

ARB Visibility (arb-watch) shows the **bridge seats** (codex / agy / pi): the bridge daemon
tees each seat's turn events to `events:live` (the roster) and `arbmem:trace` (the granular
transcript), gated on `run_id`. The **Claude orchestration layer is invisible**:

- The **warm orchestrator** (the driving Claude Code session) is not a bridge seat — it runs as
  the Claude Code process, with no daemon teeing its activity. It surfaces only as the *grouping
  label* (the `orchestrator` field on dispatched-seat events), never as a watchable seat.
- **cold-Opus reviewers** (spawned via the Agent/Task tool) are in-process subagents — also no tee.

Goal: make the warm orchestrator and each cold-Opus reviewer appear as **first-class watchable
seats** in arb-watch, streaming live, **symmetric with the bridge seats** — for the same
roster + drill-in transcript experience across the whole fleet, including the Claude driver.

## Key insight — Claude is "just another seat whose store is a transcript file"

A bridge seat like **agy-print is itself a tailer**: it polls its conversation store (SQLite) and
tees granular events live. The exact analog for Claude is to **tail the session transcript
`.jsonl`** Claude Code writes, map its events to the same contract, and tee to the same streams.

**Scope of "symmetric" (panel-corrected):** there is **no backend gateway route/schema change** — the
gateway renders any seat whose `events:live` + `arbmem:trace` events carry the right fields. But the
tailer must supply the **full** contract the gateway depends on — not just `model_text`/`command_*`,
but `task_id` (the roster key), `orchestrator` (grouping), and **synthetic `task_started`/`task_finished`
lifecycle events** (state derivation) — see § Identity & lifecycle. And the desired arb-watch *behavior*
(isolated orchestrator section, `agentOf` for opus/claude) **is** a UI change. So: zero *backend*
gateway change; specified contract + small arb-watch UI changes required.

## Empirical findings (the gate)

Verified on the live session `7c89a511-…jsonl`:

- **Incremental per-event flush** — each conversation event is appended as its own timestamped
  JSON line as it happens (`11:02:45 assistant thinking`, `:48 assistant text`, `:52 tool_use`,
  `:53 user tool_result`). Distinct timestamps prove per-event flushing, not turn-batching → tailing
  gives **true-live** granularity, like agy-print's WAL poll.
- **Event shapes map cleanly** to the bridge contract (see § Event mapping).
- **cold-Opus subagents have their own transcript files** — the `tasks/<agentId>.output` JSONL files
  the harness writes per subagent. So each reviewer is independently tailable.
- **Zero token cost** — the tailer only reads files + writes Redis; it never invokes an LLM.

## Architecture — the `claude-tail` service

A small service (in the bridge repo, run as a host service like the other bridge processes) that:

1. **Discovers** the transcripts to tail (see § Correlation): the active warm session `.jsonl` and
   each registered cold-Opus subagent `.output`.
2. **Tails** each file (read new lines as appended; resume from a committed offset across restarts —
   see § Offset & ordering).
3. **Maps** each transcript line → the bridge event contract (§ Event mapping), **including synthetic
   lifecycle events** (§ Identity & lifecycle) so the gateway's roster reducer can derive seat state.
4. **Redacts** each event's `data` (content + tool_name) with the bridge's `redact()` — applied
   **explicitly at the tailer** for BOTH sinks (the live stream is not a redacted sink today; § Redaction).
5. **Tees** to `events:live` (roster) + `arbmem:trace` (transcript) via an **extracted standalone
   live-tee helper** (§ Tee helper) — `_tee_live_event` is currently a `Bridge` instance method, so a
   small refactor lifts the publish logic out for reuse without a running bridge.

It owns no model calls and no engine — it is pure transport, the same shape as the bridge's event
tee, but as a **decoupled long-running daemon** (vs agy-print's in-engine poller — agy's review note).
One process can tail many transcripts (the warm session + N subagents) concurrently.

## Event mapping (Claude transcript line → bridge event)

| Claude `.jsonl` line | Bridge event | Notes |
|---|---|---|
| `type=assistant`, content block `text` | `model_text` (delta) | the model's prose |
| `type=assistant`, content block `thinking` | `model_thinking` | reasoning |
| `type=assistant`, content block `tool_use` | `command_started` | tool name + input as the command |
| `type=user`, content block `tool_result` | `command_output` + `command_finished` | result content + status |
| `type=user` (plain) | user-input marker | turn boundary |
| `type=file-history-snapshot`, `attachment`, `bridge-session`, … | dropped | Claude-internal, not transcript content |
| **(synthetic)** first event seen for a transcript | **`task_started`** | so the reducer marks the seat `running` |
| **(synthetic)** transcript idle past a threshold / `SessionEnd` / subagent `.output` final line | **`task_finished`** | so the seat reaches a terminal state (and stale detection applies) |

**Why the synthetic lifecycle events are mandatory (panel P1, triple-convergent):** the gateway's
roster reducer (`visibility.py:109 _reduce_seat`, `:88 _is_stale`) derives a seat's `state` **only**
from `task_started`/`task_continuing`/`task_finished`/`vote`. `model_text`/`command_*` events update
`last_event_ts` but **never set state** — so without the synthetic `task_started`/`task_finished`, a
tailed seat is a permanent stateless ghost (never running/done/stale). The bridge gets state for free
because its turn loop emits these (`bridge.py:~934`); the tailer must emit them too.

## Identity & lifecycle contract (what the gateway needs)

Each emitted event carries the full `events:live` field set `{run_id, task_id, seat_id, orchestrator,
event_type, sent_at, data}`. The gateway **keys the roster on `task_id`** (`visibility.py:435`) and
**groups/filters orchestrator views on the `orchestrator` field** (`:429-458`) — so all three of
`task_id`, `orchestrator`, `run_id` must be specified, not just `seat_id`:

| field | Warm orchestrator | cold-Opus reviewer |
|---|---|---|
| `seat_id` | `claude-<project>-<workspace>` | the marker `ARB_SEAT` (e.g. `cold-opus-1`) |
| `task_id` (roster key — must be unique+stable) | the **session id** (one stable row) | the **subagent `agentId`** (the `tasks/<agentId>.output` filename is unique per spawn → no collision even if two reviewers share a marker seat name) |
| `run_id` (group label) | the session id (isolated; its own group) | the marker `ARB_RUN` (the panel) → groups with codex/agy/pi; **fallback = session id if marker absent** (so it stays visible, just ungrouped) |
| `orchestrator` (grouping) | itself (`claude-<project>-<workspace>`) | the marker `ARB_ORCH` = the **spawning warm orchestrator's id** (so reviewers pane under it); `""` if the marker omits it (visible, ungrouped). **NOT hardcoded** — carried in the marker so multiple concurrent warm sessions each group their own reviewers correctly (T4 panel correction; resolves the multi-session open decision) |

## Correlation (seat_id + run_id per transcript)

- **Warm orchestrator** — a one-time `SessionStart` **hook** (settings.json, once per host) registers
  the live session into a small registry (a file or Redis key the tailer reads):
  `{session_id, transcript_path, seat_id = claude-<project>-<workspace>, run_id = session_id}`.
  → one stable, **isolated** seat; `run_id = session id` (it is its own group, out of the run
  grouping). The `SessionEnd` hook (or heartbeat TTL) marks it offline.
- **cold-Opus reviewers** — the label rides in the prompt, symmetric with agy's nonce. When the
  orchestrator spawns a reviewer it includes a marker carrying its OWN id, e.g.
  `[ARB_RUN:arb-goclient2-20260628 ARB_SEAT:cold-opus-1 ARB_ORCH:claude-bridge-dev]`. `parse_marker`
  returns `{run_id, seat_id, orchestrator}` (orchestrator `""` if `ARB_ORCH` omitted — 2-field markers
  still parse). `task_id` is the subagent `agentId` (unique per spawn — collision-proof; § Identity).
  Marker robustness (panel P2):
  - Read the marker **ONLY from the subagent's first user message** (the spawn prompt), never from
    later content — so a subagent that *quotes* the marker into its own output can't re-trigger/spoof it.
  - **Forgotten / unparseable marker → fall back to `run_id = session id`** so the reviewer is **still
    visible** (grouped under the session, not the panel). This corrects the original risk note: without
    *any* run_id the event is run_id-gated and **dropped (invisible)**, so the fallback is mandatory.
  - (Future: auto-inject the marker via a `PreToolUse(Task)` hook reading a "current run_id".)

## arb-watch UI changes (Go watcher)

- **Isolated orchestrator section** — the warm orchestrator seat is pinned to the **bottom** of the
  left pane under a `── orchestrator ──` divider, excluded from the activity-sort and the run
  grouping (it is always-present, not part of any one run).
- **`agentOf()` learns the new agents** so the `a` filter works: cold-Opus seat ids → `opus`, the
  orchestrator → `claude`. cold-Opus reviewers otherwise behave as ordinary roster seats
  (activity-sorted, status/agent filterable) — symmetric with bridge seats.

Picture:
```
run go-client review:  codex · agy · pi-glm · cold-opus-1 · cold-opus-2
run agy-print review:  codex · cold-opus-1
…(activity-sorted, filterable)…
── orchestrator ──
claude-bridge-dev   (isolated, always present)
```

## Security / redaction (panel-corrected — this was overclaimed)

The original "reuse the bridge redaction" was imprecise. The actual surfaces:
- `TranscriptFlusher._write` **does** `redact()` content + tool_name before `arbmem:trace`
  (`transcript_flusher.py:167`). So the trace sink is covered.
- `_tee_live_event` (`bridge.py:1897`) **does NOT** call `redact()` — it `json.dumps(data)` verbatim.
  **The live stream is not a redacted sink today.**

So the tailer must apply `redact()` **explicitly, itself, before building BOTH the live `data` dict and
the trace event** — this is a **new** application of the existing `redact()` helper at the tailer, not a
reuse of an existing call site. It matters more here than for bridge seats: a Claude `tool_use` whose
input reads a `.env`, or a `tool_result` output, is far more secret-bearing than the engine status
fields the bridge's live events usually carry. **Decision:** the tailer redacts content + tool_name on
every event before either sink; do NOT put raw tool args/results on `events:live`.

**Stream routing (agy Pitfall B — match the bridge's split):** the bridge routes `model_text` /
`model_thinking` to the **trace stream only** (`arbmem:trace`), and the lifecycle/command boundaries
(`task_started`/`task_finished`, `command_started`/`command_finished`/`command_output`) to **both**.
The tailer must replicate this — high-volume raw prose/thinking belongs only in the trace; pushing it
to `events:live` would bloat Redis and flood the SSE roster. So `events:live` carries roster + boundary
events; `arbmem:trace` carries the full (redacted) transcript.

Transcript capture stays gated on the existing `ARB_TRANSCRIPT_CAPTURE` flag and the run_id gate.

**T1 build correction — redaction is enforced in `trace_tee` structurally, not asked of the caller.**
The first cut extracted `trace_tee` with the flusher's field *shape* but not its *value contract* (no
`redact()`, no 256 KB cap, no drop-on-empty/missing) — a 4-seat panel caught it (cold-Opus, pi-GLM,
codex, agy; agy additionally caught a `turn_index` source-mismatch that collapsed all items into turn 0).
A tee that *can* publish un-redacted content to a human-visible stream is the exact soft spot named in
§ Isolation invariant below. **Decision (adjudicated REQUEST-CHANGES over a 2-1 certifying split — a
security/privacy property outranks a "dormant nit"):** `trace_tee` carries the redact + cap + drop
contract itself (one safety contract for `arbmem:trace`, preferably extracted from
`TranscriptFlusher._write` so both producers share it), and `turn_index` is an explicit parameter, not a
silent default. So redaction is belt-and-braces: the tailer redacts before tee (above) AND `trace_tee`
is structurally unable to emit un-redacted/uncapped content.

## Isolation invariant — observability is for the human, not for other seats

Teeing the warm orchestrator + cold-Opus transcripts into ARB Visibility makes them watchable **by the
operator**. That is the win. It must NOT make a seat's reasoning **readable by another seat** — the cold
reviewer's whole value is that it did not watch the orchestrator think (it gets only the brief).
Human-observability and seat-decorrelation point in slightly different directions, and this is where they
either stay separate or quietly merge.

**Rules:**
- The arb-watch gateway is a least-privilege **read** role and is NOT reachable by any bridge seat (bridge
  cred-wipe is the decorrelation guarantee). The warm/cold trace is operator-observability only.
- Never fold the orchestrator's transcript into a review brief, nor into any store a review seat can query
  (e.g. ARB Memory MCP read paths). The brief stays the cold seat's only input.
- The redaction contract (`trace_tee` above) is the structural backstop: even if the trace plane were
  ever reachable, it carries no raw secrets. But redaction is not isolation — keep both.

## Tee helper (extraction the tailer needs)

`_tee_live_event` is a `Bridge` instance method bound to bridge args, Redis config, the live flusher,
and the turn-heartbeat throttle (`bridge.py:1897-1933`); `_capture`/`TranscriptFlusher` likewise. The
tailer has no `Bridge`. So a small refactor **lifts a standalone live-tee + trace-tee helper** (taking
a redis handle + prefix + the field dict) out of `Bridge`, which both the bridge and the tailer call.
Shared tests assert a tailer-emitted event produces the **same `events:live`/`arbmem:trace` fields the
gateway expects** (a fixture diff against a real bridge-seat event).

## Deployment — containerised (easy new-host rollout)

The tailer is pure I/O, so it containerises cleanly, mirroring the seat-host container:

- **Image** = the tailer baked in (Python + the bridge tee/redaction code). `docker pull` to a new host.
- **Bind mounts (read-only)** — the host's Claude transcript dirs: `~/.claude/projects` (warm sessions)
  + the harness `tasks/` tmp dir (cold-Opus `.output`) + the registry path. The container reads; it
  never writes the transcripts.
- **Bus** — `host.docker.internal` locally or the managed Valkey URL (`ARB_LIVE_REDIS_URL` /
  `ARB_TRACE_REDIS_URL`), same env as the bridge tees.
- **Run as host UID** (`--user $(id -u)`) so it can read the host-owned transcript files.
- **Host/container split** — the `SessionStart` hook runs **host-side** (it's a Claude Code hook) and
  writes the registry into a shared, bind-mounted path the container reads. Per-host config is only
  the mount paths + bus.

## Testing

- **Unit:** transcript-line → event mapping (each `type`/content-block shape; the drop set);
  **synthetic lifecycle emission** (first event → `task_started`; idle/end → `task_finished`);
  **identity** (warm `task_id`=session-id; cold `task_id`=agentId; `orchestrator` field per type);
  marker parsing incl. **failure modes** (forgotten → session run_id fallback; marker only from first
  message; quoted-marker-in-output ignored); **offset commit-point + rotation** (crash-before-publish
  re-reads; shrunk file resets to 0); **format self-check** (a mutated/unknown shape trips fail-loud).
- **Contract:** a tailer-emitted event produces the **same `events:live`/`arbmem:trace` fields a real
  bridge-seat event does (fixture diff), and the gateway's `_reduce_seat` marks it running→done.
- **Redaction:** a secret in a `tool_use` input / `tool_result` output is stripped from BOTH the live
  `data` and the trace event before tee (inject-revert).
- **Redaction:** a secret in a tool arg / output is stripped before tee (inject-revert).
- **Integration (live, like agy-print):** run the tailer against a real Claude transcript that's
  actively growing; assert events land on `events:live` + `arbmem:trace` and arb-watch shows the seat
  with a live transcript; cold-Opus marker correctly groups a reviewer under its panel run_id.

## Offset & ordering (no double-emit / no loss)

The resume guarantee needs an explicit **commit point** (panel P2): advance the persisted offset
**only after** the publish is acknowledged (XADD returned / flusher enqueued), so a crash between read
and publish re-reads (at-least-once) rather than silently skipping. At-least-once + an idempotent
consumer (the gateway is roster-reduce, last-writer-wins) is acceptable; exactly-once is not required.
Also handle **`.jsonl` rotation/truncation** — if a transcript's size shrinks below the stored offset
(new session reusing a path, or a rewrite), reset the offset to 0 rather than skipping.

**Offset store = Redis, NOT a local file (agy Pitfall D — a contradiction in the v1 draft).** The
container's bind mounts are **read-only**, so the tailer cannot write offset files next to the
transcripts, and a stateless container has no durable local FS. Store offsets in Redis/Valkey keyed by
`(transcript_path|inode)` — e.g. `claude:offset:<hash>` — so the container stays stateless and a
restart resumes correctly instead of replaying everything (double-emit).

## Risks / open questions

- **Transcript format drift** — the `.jsonl` schema is reverse-engineered; a Claude Code update could
  change it. Mitigation: an **executable self-check** (panel P2) — validate each line against the
  expected `type`/content-block shapes; an unknown/changed shape → never emit garbage. **Fail loud in a
  way a container surfaces** (agy Pitfall C — a stderr WARN is invisible in a background container):
  emit a synthetic `drift_error` event to the stream so the UI flags the tailer's degradation, AND past
  a threshold exit non-zero so the container restarts + stands out in host telemetry. Same fail-loud
  discipline as agy-print's `_schema_ok`; a sample-fixture test pins the current shapes so drift trips it.
- **Marker discipline (v1)** — see § Correlation: a forgotten marker falls back to the session run_id
  (still visible, ungrouped). The Task-hook auto-inject removes it later.
- **Multiple concurrent Claude sessions on one host** — "which is the orchestrator?" The `SessionStart`
  registry must hold ALL active sessions (each a seat keyed by its session id), not assume one; the
  isolated "orchestrator" UI section lists each warm session. Decide whether non-orchestrator Claude
  sessions are tailed at all (scope).
- **Containerised cold-Opus path (panel P1 feasibility; agy Pitfall F)** — the `.output` files live in a
  per-session, UID-namespaced tmp path (`/private/tmp/claude-<uid>/<session>/tasks/`) that a static
  container can't pre-mount. Resolution, preferred order: **(B, recommended)** have the host-side
  `SessionStart` hook **mirror/symlink the subagent `.output` files into a stable, bind-mounted
  `~/.claude/tasks/`** (already under the `~/.claude` mount); **(A)** the hook writes the dynamic
  `tasks/` path into the shared registry and the container mounts the parent `/private/tmp`;
  **(fallback)** run the cold-Opus tail **host-side** (warm via container, cold via a host process).
  Pick before the container slice.
- **Warm transcript volume** — a long orchestrator session is a large transcript; the flusher's
  256KB cap + maxlen bound the load, but the warm seat's drill-in can be huge. Consider a tail-window.

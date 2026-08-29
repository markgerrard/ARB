# ARB Observability — Slice 4 design: ARB Visibility (per-orchestrator live seat-watch, TUI + web)

**Status:** design (autonomous run, 2026-06-25). Builds on Slice 1 (eval→prod, `9e6d423`) + Slice 3
(audit votes/verdict, `c5050db`). The live read surface over the observability data those slices land.

## Goal
Watch the seats **bound to a warm orchestrator** execute, in real time — a Claude-Code-style agent pane:
a list of that orchestrator's live seats; cursor/click one → its live event timeline. Two front-ends (TUI
+ web) over one SSE gateway. MCP-OAuth-gated. Own service.

## Scope decision (Mark): per-orchestrator, not fleet-wide nor single-run
A warm Claude Code orchestrator (e.g. `claude-bridge-dev`) dispatches seats over many runs/panels. The view
is scoped to **one orchestrator's seats** — the middle granularity between fleet-wide (all seats) and one
`run_id`. Grouping key = the **dispatcher** = the envelope `from` (= `FROM_AGENT_ID`), which the bridge sees
as `request.sender` and already writes as the `to` field on every live task event
(`push_task_event` fields: `{"from": seat_id, "to": request.sender, ...}`). So a seat "belongs to"
orchestrator X iff its task's `to == X`.

## Decompose into sub-slices (each independently shippable)
- **4a — SSE gateway (backend).** The standalone service: own Starlette app, MCP-OAuth-gated, reads the live
  streams → SSE push, Postgres backfill. The foundation; build first. Testable by `curl`-ing the SSE.
- **4b-web — web agent pane.** Static HTML/JS (EventSource) rendering the pane in a browser.
- **4b-tui — terminal agent pane.** A terminal app (textual/rich or curses) rendering the same pane; consumes
  the same SSE endpoints (or a thin local client). Shares the data contract with 4b-web.

## 4a — SSE gateway (the core of this design)

### Service shape
A new `run_visibility()` in `run.py` (+ `visibility` choice) + a `visibility` prod compose service, mirroring
`run_writer` (Starlette + uvicorn). Reads Redis (live streams) + Postgres (backfill). Stateless; horizontally
trivial (each client gets its own XREAD cursor).

### Auth — reuse the MCP OAuth door (precise seam, panel-corrected)
Every endpoint requires a valid bearer token issued by the MCP door. The clean seam is
`oauth_store.get_access_token(conn, token)` (enforces `revoked_at IS NULL AND expires_at > now()`,
`oauth_store.py:88-93`) — a thin Starlette middleware validates the bearer token via that lookup and 401s on
missing/invalid/expired. **Do NOT instantiate the full `ArbMemoryOAuthProvider`** — `load_settings()` requires
the MCP login/TOTP secrets (`config.py`), which the read-only visibility service should not hold. **Token
audience:** door-issued tokens bind `resource == public_base_url` (`oauth.py:270`); the visibility middleware
must check the token's resource against the **shared** door `public_base_url` (config), or door tokens 401.
The web UI obtains a token via the existing OAuth flow; the TUI passes a token (env/flag). No new auth surface —
the door stays the single trust boundary; the gateway only *reads* its token table.

### Data model — "a seat" = an active task
Derived from the live event stream, keyed by `task_id`, grouped by orchestrator (`to`):
`{task_id, seat_id, orchestrator (=to), model, role, run_id, state, started_at, last_event_ts, last_event}`.
State transitions from the event types the bridge already emits: `task_started` → running; `task_finished` →
done/failed; `task_continuing` → running; (panel) `vote` → voted. Presence: a seat is "live" between
`task_started` and `task_finished` (+ a short TTL grace so a crashed seat ages out).

### Live source — a global visibility stream (panel-required; the "no bridge change" claim was wrong)
The design panel (unanimous) found that live events are written to **per-task** keys
`task:{task_id}:events` (`redis_io.py:60`), and Redis `XREAD` cannot wildcard `task:*:events` or filter by the
`to` field — so an orchestrator roster is NOT buildable by "XREAD grouped by `to`" without a discovery layer.
**Decision (panel-converged): add one global visibility stream.** `push_task_event` tees every task event to a
single `{prefix}events:live` stream on the bridge bus (db-12), in addition to the existing per-task key and the
eval tee — a small bridge addition mirroring `_tee_eval_event`. Each entry carries
`{run_id, task_id, seat_id (=from), orchestrator (=to), event_type, sent_at, data-summary}`. The gateway does a
SINGLE `XREAD` on `events:live` and groups/filters by `orchestrator` in-process — no keyspace SCAN, no
pattern-subscription. Bounded `maxlen` + TTL like the eval/task streams. This stream is the live source for
both the roster and the per-seat timeline (filter by task_id).

**Votes are a second tee point (panel-corrected).** `_emit_vote` (bridge.py:1646) emits to the AUDIT stream
directly, NOT via `push_task_event` — so a panel seat's `vote` would never appear on `events:live` and the live
"voted" badge would have no source. Fix: `_emit_vote` ALSO tees a lightweight `{event_type:"vote", run_id,
task_id, seat_id, orchestrator, stance}` entry to `events:live` (one extra xadd, fail-soft, **panel dispatches
only** — not the all-dispatch hot path). So `events:live` is the single unified live feed (lifecycle from
push_task_event + votes from _emit_vote); the gateway derives the `running/done/failed/voted` seat state from it.

### SSE endpoints (async — `redis.asyncio`, panel-required)
Blocking sync `XREAD` inside an async Starlette handler blocks the uvicorn event loop and starves every other
SSE client (panel P1). The gateway MUST use `redis.asyncio` (async `xread`) — or `anyio.to_thread.run_sync` for
the blocking read — so one client's blocked read cannot stall others' auth/heartbeats. A test MUST drive TWO
concurrent SSE clients to prove non-blocking.
- `GET /sse/orchestrator/{orchestrator_id}` → roster: async-XREAD `events:live`, filter `orchestrator==id`,
  reduce to seat presence, emit `seat_appear` / `seat_update` / `seat_finish`. Drives the left pane.
- `GET /sse/seat/{task_id}` → one seat's timeline: backfill on connect, then async-XREAD `events:live` filtered
  to `task_id`. **Backfill join (panel-corrected):** `eval_event_raw` keys by `task_id` directly; `audit_events`
  votes key by `payload.actor` (= `"seat:"+seat_id`), NOT task_id. So: read the seat's eval rows by `task_id`
  (gives `run_id` + `seat_id`), then read its audit votes by `(run_id, payload->>'actor' = 'seat:'+seat_id)`;
  merge ordered by ts.
- `GET /orchestrators` (JSON) → orchestrators with ≥1 live seat (for a picker).
Each SSE response: `text/event-stream`, periodic heartbeat comment, `id:` per event (the stream entry id) so a
reconnect sends `Last-Event-ID` to resume the XREAD from that id. Fail-soft: a dropped read closes the stream;
the client reconnects.

### Backfill orchestrator column (panel-corrected)
`eval_event_raw` has `run_id/task_id/seat_id` but NOT the orchestrator, and the panel confirmed the orchestrator
is NOT recoverable from audit `source` (it is the literal string `"orchestrator"`, not the dispatcher id). So
Slice 4a adds the dispatcher to the eval record — a **5-touch** change (panel-corrected; the producer alone
doesn't persist it): (1) `build_eval_record(..., orchestrator=...)`; (2) the call site `_tee_eval_event` passes
`request.sender`; (3) the consumer `PostgresEvalSink.write` INSERT adds the `orchestrator` column +
value; (4) `EvalConsumer._parse_event` reads `fields.get("orchestrator")`; (5) a nullable
`eval_event_raw.orchestrator` column + idempotent `ALTER` (Slice-1/3 pattern). Old rows have `orchestrator=NULL`
(live view unaffected; only historical scoping partial until backfill). The same `orchestrator` field rides the
`events:live` entry.

## 4b — front-ends (shared contract)
Both render the same two-pane model and consume 4a's SSE. The **data contract** (the SSE event shapes:
`seat_appear/update/finish` + timeline `event`) is frozen by 4a so TUI and web are interchangeable clients.
- **web:** a single static page; `EventSource('/sse/orchestrator/<id>')` for the list, `EventSource(
  '/sse/seat/<task_id>')` on selection; OAuth for the token. Minimal JS, no framework required.
- **tui:** a terminal pane (Python `textual` recommended — async, SSE-friendly, list+detail widgets) launched
  with an orchestrator id + token; same endpoints. Ships as a small CLI (e.g. `arb-watch`).

## Layout (both front-ends)
```
┌─ ARB Visibility · orchestrator: claude-bridge-dev ─────────────────────────┐
│ SEATS (live)                    │ codex-bridge-dev · run a1b2 · running      │
│ ▶ codex-bridge-dev   running    │ 0:00 task_started                          │
│   agy-bridge-dev     done       │ 0:05 tool_call  bash                       │
│   pi-glm (panel)     voted ✓    │ 0:40 vote  approve  (panel a1b2)           │
│ 3 seats · 1 run                 │ 0:42 …                                     │
└────────────────────────────────┴────────────────────────────────────────────┘
```

## Error handling
- Auth: 401 on invalid token (both REST + SSE handshake).
- Redis/Postgres read failure: close the SSE with a retry hint; client reconnects with Last-Event-ID.
- Best-effort/read-only: Visibility never writes to the observability stores; a gateway outage loses no data
  (the streams + Postgres are the source of truth).

## Testing
- 4a: unit — seat-state reducer (event types → presence/state); orchestrator grouping by `to`; auth
  middleware (401 without token). Integration/E2E — start the gateway against the local substrate, push live
  events for two seats under one orchestrator, assert the `/sse/orchestrator/<id>` stream emits
  seat_appear/finish and `/sse/seat/<task_id>` emits the timeline (curl/httpx with a real token). Backfill: a
  persisted run is replayed from Postgres on connect.
- 4b: contract tests against recorded SSE fixtures; manual visual check of the pane.

### Config surface (panel-flagged — the gateway spans two Redis planes)
The visibility service is configured with: **bridge-bus** access (`events:live` on db-12 — the live source;
needs the bridge-bus Redis URL/creds, a different plane from arb-memory), **arb-memory Postgres** (`ARB_MEMORY_DSN`
for backfill of `eval_event_raw`/`audit_events`), and the **OAuth** `public_base_url` (token-audience check) +
the DSN for the token table. Resolve from `.env` (exported wins, Slice-1 pattern). Read-only everywhere.

## Resolved decisions
1. Scope = **per-orchestrator** (group by envelope `to`/sender).
2. Live source = **a global `events:live` stream** the bridge tees to (panel-required; per-task keys can't be
   wildcard-XREAD). Small `push_task_event` addition mirroring `_tee_eval_event`. Gateway XREADs the one stream,
   filters by `orchestrator`/`task_id`.
3. SSE uses **`redis.asyncio`** (no event-loop blocking); a 2-concurrent-client test is mandatory.
4. Backfill scoping adds **`eval_event_raw.orchestrator`** (audit `source` does NOT carry the dispatcher id).
5. Auth = thin middleware over **`oauth_store.get_access_token`** + shared `public_base_url` audience check;
   NOT full-provider instantiation.
6. **Own service** (`visibility`), SSE push. **Two front-ends** (web + TUI) over one frozen SSE contract;
   build 4a first, then 4b-web, then 4b-tui.

## Out of scope
- Writes/control (this is read-only watch; steering stays on the bridge control plane).
- Span tables / retention (Slice 5).
- Fleet-wide cross-orchestrator board (could be a later superset; per-orchestrator first).

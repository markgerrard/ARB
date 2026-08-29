# codex-redis-bridge — Spec Brief

> **STATUS (historical MVP brief, 2026-04-26):** this document captures the original
> Codex-only MVP. The current envelope has since gained mandatory `run_id`,
> worktree / `expected_artifacts` / `allowed_paths` / `commit_message` payload
> fields, and the `audit_vote_expected` stamp; message kinds now include `steer`
> and `cancel`. For current behavior, start with `docs/architecture-overview.md`
> and the code.

> **Audience:** the Codex agent (GPT 5.5) implementing this MVP. Paste this whole document into the task prompt or have Codex read it directly. Don't paraphrase the hard constraints — they were learned the hard way.

## Goal

A long-running Python process that bridges OpenAI **Codex App Server** to the existing `agent_scratch:` cross-agent Redis bus on this host. The process is itself a fully-participating agent named `codex-project-c-<workspace>` — it receives task requests via its inbox, executes them via App Server, replies via the requester's inbox, and emits semantically-meaningful events to stdout for Claude's Monitor consumer to surface.

## Architecture

```
agent_scratch:agent:codex-project-c-<workspace>:inbox  (Redis LIST, BLPOP)
          ↓
     bridge.py main loop
          ↓
   ws://127.0.0.1:8765  (WebSocket JSON-RPC)
          ↓
     codex app-server  (subprocess, supervised by bridge)
          ↓
   streaming events: turn.start, turn.partial, turn.complete, tool calls
          ↓
agent_scratch:agent:<requester>:inbox  (LPUSH reply with in_reply_to)
```

## Hard constraints — DO NOT violate

### 1. Namespace prefix has a trailing colon

`AGENT_REDIS_PREFIX=agent_scratch:` — note the trailing `:`. Concatenate suffixes WITHOUT a leading colon:

```python
INBOX    = f"{prefix}agent:{agent_id}:inbox"      # ✓ correct
STATUS   = f"{prefix}agent:{agent_id}:status"     # ✓ correct
REGISTRY = f"{prefix}registry:{agent_id}"         # ✓ correct

# ✗ WRONG — produces "agent_scratch::agent:..." (doubled colon)
INBOX = f"{prefix}:agent:{agent_id}:inbox"
```

This was the bug in Codex's first one-shot bootstrap on this bus. **Don't repeat it.** Adding tests for the key-builder should catch it.

### 2. Connection params (read from .env)

```
AGENT_REDIS_HOST=127.0.0.1
AGENT_REDIS_PORT=6390     # NOT 6379 — that's the docker compose Redis
AGENT_REDIS_DB=12
AGENT_REDIS_PREFIX=agent_scratch:
AGENT_WORKSPACE=dev|staging
AGENT_PROJECT=project-c
```

For local-only buses, no TLS or auth is required (localhost is the gate). For a shared/managed bus (e.g. DigitalOcean Valkey, AWS ElastiCache) the bridge accepts three optional env vars:

```bash
AGENT_REDIS_TLS=1                       # enables --tls on every redis-cli call
AGENT_REDIS_USER=default                # ACL user (managed clusters ship one)
AGENT_REDIS_PASSWORD=AVNS_***           # exported as REDISCLI_AUTH so the
                                        # secret never appears in `ps`
```

When unset, behaviour is identical to a local unauthenticated Redis. DB 12 is **reserved** for agent comms — don't write cache/session/queue keys there.

The bridge MAY also accept these via CLI flags (`--workspace dev`, etc.) which override `.env` for testing.

### 3. Agent ID format

`<tool>-<project>-<workspace>` — 3 segments, no branch.

Examples: `codex-project-c-dev`, `codex-project-c-staging`, `claude-project-c-dev`, `claude-project-c-staging`.

Branch is **NOT** in the ID. It's transmitted in each message envelope as `branch:` (sender's current branch at send time).

### 4. Currently alive agents on the bus (as of 2026-04-26)

- `claude-project-c-dev` — Claude Code in `/srv/projects/example-generator-dev`, branch `feat/fire-and-select-hero`
- `claude-project-c-staging` — Claude Code in `/srv/projects/example-generator`, branch `dev`
- `codex-project-c-dev` — was a one-shot ping earlier today; this bridge **replaces** that with a long-running registration at the same ID. On boot, the bridge HSETs the registry HASH (overwriting any stale one-shot data).

## Schema

### Registry (HASH)

```
agent_scratch:registry:<agent-id>
  tool            codex
  project         project-c
  workspace       dev | staging | ...
  current_branch  <git branch at boot>
  path            <abs path to working dir>
  registered_at   <ISO 8601>
  pid             <process pid>
  owner_token     <random token unique to this daemon boot>
```

Created on bridge boot (HSET, no TTL — explicit DEL on clean shutdown).

### Heartbeat (STRING with TTL)

```
agent_scratch:agent:<agent-id>:status   value: "alive:<owner-token>"   TTL: 60s
```

Bridge MUST refresh this every ≤30s while alive. A new boot waits in-process for a stale
foreign lease to expire before claiming the ID. When TTL expires, peers infer the owner is offline.

### Inbox (LIST)

```
agent_scratch:agent:<agent-id>:inbox    each element = JSON envelope (string)
agent_scratch:agent:<agent-id>:processing    envelopes atomically parked while being handled
```

Producers `LPUSH` envelopes addressed to recipient. The bridge consumes from the
LEFT side of its own inbox, preserving the historical newest-first/LIFO behavior.
When supported by Redis/Valkey, the bridge uses `BLMOVE inbox processing LEFT RIGHT`
so a delivered envelope is atomically parked in `:processing` before the client sees
it. After synchronous handling returns for validation rejects, sender rejects, drops,
and handler errors, or after the accepted request worker finishes and replies, the
bridge removes that parked value with `LREM processing 1 <raw-envelope>`.

On startup, before blocking on the inbox, the bridge drains `:processing` back to
`:inbox` with `LMOVE processing inbox RIGHT LEFT` until empty. This restores parked
envelopes in their original parked order and places them on the left side so they are
eligible before traffic that was already waiting in the inbox. If new producers race
the startup drain, normal `LPUSH` newest-first ordering can still put newer traffic in
front. Each recovered value logs `[bridge] recovered in-flight envelope id=<id>`, using
`id=unknown` for payloads whose id cannot be parsed.

This gives request delivery **at-least-once** rather than at-most-once. If the daemon
dies after parking an envelope but before completing the turn, the next startup re-runs
that request. Final task results are keyed by request id, so a re-run overwrites the
same result key rather than creating a second result key. Recovery re-runs are not
idempotent for side effects such as worktree creation, orchestrator commits, or repo
mutations; a daemon killed mid-turn may partially apply those side effects twice.
Callers that care should design briefs to be safe to re-run. If shutdown is noticed
after `BLMOVE` has parked an envelope, the bridge leaves it in `:processing`, logs
`[bridge] shutdown with parked envelope id=<id> (will recover on restart)`, and exits
without handling it.

Redis servers older than 6.2 do not support `BLMOVE`. If the server rejects the command
as unknown, the bridge logs one warning, `[bridge-warning] blmove-unsupported falling
back to blpop (at-most-once delivery)`, and uses the old `BLPOP` behavior for the rest
of that process lifetime.

### Message envelope (JSON)

```json
{
  "id": "<uuid>",                 // required, unique per message
  "from": "<agent-id>",           // required, sender's agent ID
  "branch": "<git-branch>",       // required, sender's current branch
  "to": "<agent-id>",             // required, recipient
  "kind": "hello|request|reply|notify",  // required
  "in_reply_to": "<uuid>",        // present iff kind=reply (refers to the request id)
  "sent_at": "<ISO 8601>",        // required
  "payload": { ... }              // required, kind-specific shape (see below)
}
```

Reject envelopes that don't validate. Log `[bridge-error] envelope-invalid <reason>` and skip. Don't crash.

### Payload shapes

`hello` — connection probe (informational, no expected reply):
```json
{"msg": "<short text>", "my_path": "<path>"}
```

`request` — task to execute (one reply expected):
```json
{
  "task": "<natural language instruction for Codex>",
  "context_files": ["<optional list of paths to surface to Codex>"],
  "thread_id": "<optional App Server thread to continue on engines that support resume>",
  "fork_from_thread_id": "<optional App Server thread to fork into a new codex thread>",
  "expect_structured": true,
  "fresh_context": true,
  "turn_timeout": 7200
}
```

`turn_timeout` is an optional positive JSON integer in seconds (JSON booleans do not
count as integers). It bounds one task engine turn, not total dispatch duration: the
initial task turn and every drive-to-completion continuation use the requested ceiling,
while bridge-generated helper turns use the seat's default. A dispatch can therefore
span multiple turn ceilings. A trusted sender may request a ceiling below the seat
default as a deliberate self-cap. If absent, the seat's `--turn-timeout` applies.

Only a sender whose resolved policy is `trusted` may supply `turn_timeout`. The bridge
refuses invalid values, non-trusted requests, and values above
`--turn-timeout-max` before engine work begins. Every refusal is an ordinary correlated
reply (`ok=false`) naming the offending value or policy; over-max errors name both the
requested value and the seat maximum. Requests are never clamped.

`expect_structured` is optional and defaults to false. When true, the bridge appends
structured-reply instructions to the task prompt. The engine should end its normal reply
with a fenced JSON object whose `status` is one of `DONE`, `DONE_WITH_CONCERNS`,
`BLOCKED`, or `NEEDS_CONTEXT`; optional fields are `summary`, `concerns`, `next_steps`,
`questions`, and `artifacts`.

`fresh_context` is optional. When it is exactly boolean `true`, the bridge asks the
acquired engine to reset its conversation context after pool acquire and before the first
turn. When it is exactly boolean `false`, the request explicitly uses the warm context.
When the field is absent, the daemon flag `--fresh-context-default` decides whether the
request is fresh; the global default remains warm. If the selected engine does not support
context reset, the bridge logs `[bridge-warning] fresh-context-unsupported
engine=<name> task_id=<id>` and runs the request warm. If reset fails, the bridge logs
`[bridge-warning] fresh-context-reset-failed engine=<name> task_id=<id> error=<exc>` and
runs the request warm.

Warm context under the engine pool without an explicit `thread_id` is **best-effort**, in
both directions: a warm request may inherit context from whatever task its pooled engine
last served, and a follow-on request that omits `thread_id` is not guaranteed to acquire
the same pooled engine that served the earlier task. Do not design callers that depend on
implicit warm continuation.

`thread_id` is optional explicit continuation. When present, it must be a non-empty
string. Codex engines honor it by calling App Server `thread/resume` with that
`threadId` after pool acquire and before the first turn; App Server loads the thread
from disk, so no bridge-side engine affinity is required.

Session-in-process engines that do not support `thread/resume` honor `thread_id` by
engine affinity routing: the pool routes the request to the live engine instance whose
current `thread_id` or `session_id` matches the requested value. This guarantee only
holds while the owning engine instance remains present and healthy in this daemon's
pool. If the owner is gone, the task fails without running a turn:
`ok=false, error="thread-affinity-miss thread=<id>"`. If the owner is busy on another
turn, the task fails fast: `ok=false, error="thread-affinity-busy task=<task-id>"`.
If multiple live engines claim the same id, the task fails closed:
`ok=false, error="thread-affinity-ambiguous thread=<id>"`. Session-engine requests that
combine `thread_id` with `payload.worktree` are rejected before running because worktree
turns use a fresh single-use engine: `ok=false,
error="thread-affinity-worktree-incompatible"`. If `thread/resume` fails on a
resume-capable engine, the task fails without running a turn:
`ok=false, error="thread-resume-failed: <reason>"`.

Session affinity is in-process only. It cannot cross daemon boundaries; with multiple
daemons consuming one shared agent_id inbox, a continuation may be popped by a daemon
that does not host the session and will fail `thread-affinity-miss`. The operational
rule remains one live daemon per agent_id.

`thread_id` and explicit `fresh_context: true` are contradictory and are rejected at
envelope validation as `[bridge-error] envelope-invalid contradictory-context`.
`thread_id` with explicit `fresh_context: false` is valid. If `thread_id` is present
and `fresh_context` is absent, it suppresses daemon `--fresh-context-default`; the
request resumes the named thread and no reset is attempted.

`fork_from_thread_id` is optional explicit codex thread forking. When present, it
must be a non-empty string. Codex engines honor it by calling App Server
`thread/fork` with that `threadId` after pool acquire and before the first turn;
App Server loads the base thread from disk and creates a NEW child thread. The
task's turn runs on that new child thread, and the reply payload's `thread_id` is
the new child thread id, not the base id. App Server persists `forkedFromId` on
the Thread object for later audits; the bridge does not duplicate that lineage in
reply payloads.

Forking is codex-only in this bridge round. If the selected engine does not expose
a fork hook, the task fails without running a turn: `ok=false,
error="thread-fork-unsupported engine=<name>"`. If App Server or the engine hook
fails, the task fails without running a turn: `ok=false,
error="thread-fork-failed: <reason>"`.

`fork_from_thread_id` and `thread_id` are contradictory and are rejected at envelope
validation as `[bridge-error] envelope-invalid contradictory-context`.
`fork_from_thread_id` and explicit `fresh_context: true` are also contradictory
and rejected the same way. `fork_from_thread_id` with explicit `fresh_context:
false` is valid. If `fork_from_thread_id` is present and `fresh_context` is absent,
it suppresses daemon `--fresh-context-default`; the request forks the named thread
and no reset is attempted. `fork_from_thread_id` with `payload.worktree` is valid
on codex; worktree requests create the fresh worktree engine first, then fork on
that worktree engine before the turn. Fork requests do not use session affinity
routing; session-in-process engines fail unsupported instead of affinity-miss.

Tree-of-thought pattern: run a base codex analysis once and capture the reply
`thread_id`, then dispatch N parallel requests with
`fork_from_thread_id=<base-thread-id>`. Each reply carries its own new child
`thread_id`, which callers can pass later as `thread_id` for continuation or as
`fork_from_thread_id` for deeper branching. Parallel divergent turns without
`payload.worktree` share the engine workdir, so combine forking with worktrees
when filesystem isolation matters. Because inbox recovery is at-least-once, a
daemon crash after `thread/fork` but before result write can re-run the fork
request and create an extra child thread; this is the same class of side effect as
duplicate worktree creation under recovery.

`reply` — terminal response to a request (sent when Codex's turn completes):
```json
{
  "result": "<summary of what Codex did or produced>",
  "ok": true,
  "error": "<set when ok=false>",
  "thread_id": "<engine thread/session id that served the request, or null>",
  "turn_timeout_requested": 7200,
  "turn_timeout_served": 7200,
  "artifact_paths": ["<optional outputs Codex wrote>"],
  "structured": {
    "status": "DONE",
    "summary": "<optional one-sentence summary>",
    "concerns": [],
    "next_steps": [],
    "questions": [],
    "artifacts": []
  }
}
```

For requests with `expect_structured: true`, the bridge attaches `structured` to both
the reply payload and the persisted `task:<id>:result` JSON. If the block is missing or
invalid, the bridge logs `[bridge-warning] structured-reply-parse-failed task_id=<id>
error=<reason>` and stores `structured: null`; parsing must not fail the task. For
requests without `expect_structured`, the bridge omits `structured` to preserve the
existing payload shape. Parse-refusal replies (envelope-invalid on a validated-header
request) follow the same convention: `structured: null` when the refused payload set
`expect_structured`, otherwise the key is omitted.

Replies and persisted `task:<id>:result` JSON always include `thread_id`. It is the
engine's thread/session id when known (for example codex `thread_id` after the turn, or
an ACP engine session id), otherwise `null`.

When a request supplies `turn_timeout`, a refusal reply echoes `turn_timeout_requested`
only; it never claims that a timeout was served when no turn ran. Accepted requests echo
`turn_timeout_requested` and `turn_timeout_served` in the reply payload, the durable
`task:<id>:result` JSON, and the task status hash from turn start. Both keys are omitted
when the request field is absent. On a mixed-version fleet, absent echo fields mean the
request reached an older bridge and no per-dispatch timeout headroom was granted.

The override applies only to task dispatches. The scored-eval control plane
(`ScoredBridgeControl`) runs at its own configured ceiling and does not receive the
override; requests carrying `turn_timeout` are still validated and refused when invalid
or unauthorized before scored-eval handling begins.

`notify` — non-terminal one-way notification:
```json
{"event": "<name>", "data": { ... }}
```

## Monitor stdout format

Claude consumes the bridge's stdout via the Monitor tool. **Each line of stdout becomes a notification** to Claude. Emit ONLY semantically-meaningful events — don't pipe raw logs.

Required line shapes:

```
[bridge] codex-project-c-<workspace> online at <ISO 8601> (pid=<pid>)
[inbox] <full JSON envelope>            # one line per arriving message
[turn-start] <request-id>
[turn-tool] <request-id> <tool-name>    # optional, one per Codex tool invocation
[turn-end] <request-id> ok|error <short summary>
[reply-sent] <reply-id> in_reply_to=<request-id>
[bridge-error] <description>
```

**Don't:**
- Dump raw App Server WS frames
- Echo Codex's full streaming token output (verbose; clogs Monitor)
- Emit a heartbeat line on every routine refresh — silent on success, only emit `[bridge-error] heartbeat-fail` on failure

If you want richer logs, write them to a file (e.g. `~/.local/state/codex-redis-bridge.log`) and put just the summary on stdout.

## Dedup / rate-limit conventions

- **Idempotency on requests**: bridge SHOULD dedupe incoming requests within a 60s window by `request.id`. If duplicate arrives, log `[bridge] duplicate-request <id>` and drop without spawning another turn.
- **Don't reply to your own messages**: when `envelope.from == self_agent_id`, log + drop.
- **Heartbeat refresh** every 30s ≤ TTL/2; re-assert the registry hash with the
  daemon's original `registered_at`, refresh the status TTL, and if 3 consecutive
  refresh attempts fail, exit cleanly with `[bridge-error] heartbeat-fail` line.
- **Cooperative back-pressure**: if the bridge has a turn in flight, queue subsequent requests rather than parallelising. Codex App Server may not be safe for concurrent turns from one bridge instance — assume serial unless you've verified otherwise.
- **Fresh-context default**: `--fresh-context-default` treats requests that omit
  `payload.fresh_context` as fresh-context requests. A request with
  `payload.fresh_context: false` still uses the warm context.

## Self-test / dry-run modes

These exist to test routing/key/envelope bugs without burning Codex tokens.

### `--self-test`
1. Loads `.env`, connects to Redis, registers, heartbeats once
2. LPOPs any pending inbox messages
3. Echoes them back as `[would-handle] <id>` lines
4. Does NOT spawn `codex app-server`
5. Does NOT submit Codex turns
6. Exits cleanly within 5s

### `--once`
Handle exactly one inbox message then exit. Useful for tests.

### `--dry-run`
Same as normal flow BUT Codex turns are stubbed: 1s sleep then return `{"result": "DRY RUN — no Codex call made", "ok": true, "dry_run": true}` as the reply payload. Real WS connection to App Server still happens (so connection bugs are caught) — only the turn submission is faked.

## Smoke test plan (Claude as peer)

1. Start bridge with `--self-test` — verify registration in Redis (`HGETALL agent_scratch:registry:codex-project-c-dev`), no codex subprocess spawned
2. Start bridge normally — verify Codex App Server spawns, WS connects, heartbeat refreshes (`TTL` on status key stays ≤60s but >0 and the registry hash is re-created if deleted)
3. From `claude-project-c-dev`, LPUSH a `kind=request` to codex inbox, payload.task = `"echo hello"`
4. Watch bridge stdout via Monitor — see `[inbox]`, `[turn-start]`, `[turn-end]`, `[reply-sent]`
5. Watch Claude's inbox for the reply — verify `in_reply_to` matches request id
6. Re-run with `--dry-run` — confirm dry-run path completes without Codex tokens

## Implementation hints

- **Python 3.11+** (asyncio, structural pattern matching)
- **Concurrency**: asyncio. Single event loop, three coroutines:
  1. `heartbeat_loop()` — re-assert registry hash and refresh status key every 30s
  2. `inbox_loop()` — `BLPOP` with 30s timeout, dispatch validated requests
  3. `app_server_reader()` — read WS events, route to active request handlers
- **Subprocess supervision**: spawn `codex app-server --listen ws://127.0.0.1:<port>` with stdio piping. Restart with exponential backoff (1s → 2s → 4s → 8s, cap 30s) on crash. On bridge shutdown, terminate gracefully (SIGTERM, then SIGKILL after 5s).
- **WebSocket JSON-RPC**: try `codex-app-server-client` from PyPI first (saves work if SDK is healthy); fall back to `websockets` + manual JSON-RPC framing if SDK has issues.
- **Redis**: `redis-py` (async via `redis.asyncio`).
- **Graceful shutdown**: SIGTERM/SIGINT → cancel inbox/heartbeat tasks → DEL status key and registry key only when their pid still matches this process → close WS → terminate codex subprocess → exit 0.
- **Validation**: keep envelope schema in one place (`envelope.py`), validate on every read AND every write. Easier to catch bugs early than chase mysterious malformed messages later.
- **Tests**: `pytest` + `pytest-asyncio`. The self-test mode is itself a smoke test you can run in CI.

## Auth

OpenAI Codex App Server owns ChatGPT auth. Bridge doesn't touch it. User runs `codex login` once on the host before starting the bridge. The bridge just connects to App Server's local socket — auth is App Server's problem.

## Out of scope (Phase 2+)

- Slack mirror
- Web/terminal UI for human chat to Codex (the Codex TUI handles that)
- Thread listing/forking/session management beyond explicit `thread_id` continuation. Without `fresh_context: true`, `--fresh-context-default`, or explicit `thread_id`, pooled engines may retain warm conversation context from prior requests.
- Multi-turn dialogues from a single request (one request → one reply for v1)
- Cancel/timeout of in-flight turns (v1 lets them finish)
- Multi-Codex per host (one bridge per workspace; if user wants two, run two with distinct `--workspace` flags)

## Suggested file layout

```
/srv/projects/example-bridge/
├── README.md
├── SPEC.md          (this document)
├── pyproject.toml
├── src/
│   └── codex_redis_bridge/
│       ├── __init__.py
│       ├── __main__.py
│       ├── bridge.py        # main loop / orchestration
│       ├── redis_io.py      # inbox/heartbeat/registry helpers
│       ├── codex_io.py      # App Server WS client + subprocess supervisor
│       └── envelope.py      # message validation/serialization
├── tests/
│   ├── conftest.py
│   ├── test_envelope.py
│   ├── test_redis_io.py     # uses fakeredis or a sidecar 6391 instance
│   └── test_bridge_self_test.py
├── .gitignore
└── .env.example             # documented but no real values
```

## Acceptance criteria for v1

1. `python -m codex_redis_bridge --self-test` registers + reads inbox + exits clean within 5s.
2. `python -m codex_redis_bridge --workspace dev` spawns Codex App Server, connects, heartbeats, idles waiting for inbox messages.
3. LPUSH a `kind=request` envelope from `claude-project-c-dev` → bridge processes → reply lands in claude's inbox with matching `in_reply_to` and `ok=true`.
4. `--dry-run` variant of (3) completes without burning Codex tokens.
5. SIGTERM cleans up all keys (status + registry) and terminates Codex subprocess.
6. Tests for envelope validation + redis key-builder + self-test mode all green.

When all 6 pass, the bridge is shippable.

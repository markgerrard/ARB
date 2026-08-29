# AgentRedisBridge — Architecture Overview

> Synthesized 2026-06-10 from a four-agent parallel analysis of the codebase
> (comms layer, engine layer, workflow docs, ops/ecosystem); **refreshed
> 2026-07-31 (ARB-B12)** to cover the memory/evidence plane added since. This is
> the orientation doc: read it first, then jump to the focused docs it links.
> `scripts/check-doc-drift` now asserts that every top-level package under
> `src/` is named here, so the 13-month silence this refresh ended cannot
> silently recur.

The repo has **three parts**:

1. **Peer-agent communications** — a lead agent (usually a Claude Code session)
   dispatches tasks to worker agents (codex, grok, pi, agy, cursor, devin,
   other Claude sessions) over a Redis bus, using a small JSON envelope
   protocol.
2. **Agentic engineering orchestration** — the machinery and documented
   workflows that turn that bus into a parallel dev-work system: engine pools,
   worktree isolation, completion gating, role routing, review panels, and the
   A/B pipeline workflows.
3. **ARB Memory and the evidence/audit plane** — a Postgres-backed store for
   artefacts, memories, panel audit rows and transcripts, exposed over MCP and
   deployed as its own service stack (`deploy/`), plus the sibling `arb_*`
   packages: privileged-action messaging, S3 file transfer, sealed secret
   transfer, and email.

```
            PART 2: orchestration (process + machinery)
  ┌──────────────────────────────────────────────────────────────┐
  │  Warm Opus (orchestrator)  ── workflows A/B, briefs, triage  │
  │       │ dispatch                       ▲ cold-Opus review    │
  │       ▼                                │ (Agent tool, not bus)│
  ├──────────────────────────────────────────────────────────────┤
  │            PART 1: comms plane (Redis bus)                   │
  │  envelopes ──► agent inbox ──► bridge daemon ──► reply       │
  ├──────────────────────────────────────────────────────────────┤
  │  bridge internals: EnginePool ─► engines (codex / pi-sdk /   │
  │  pi-rpc / grok-acp / kimi / minimax / agy / agent-sdk /      │
  │  agy-tmux / cursor; gemini-acp deprecated) + completion gate │
  │  + orchestrator-commit                                       │
  ├──────────────────────────────────────────────────────────────┤
  │  PART 3: evidence plane — ARB Memory store (Postgres) +      │
  │  audit streams ─► close consumer (reconcile-gated verdicts); │
  │  MCP doors (read via tunnel, writes via writer service);     │
  │  arb_messages / arb_files / arb_secrets / arb_email /        │
  │  arb_registration                                             │
  └──────────────────────────────────────────────────────────────┘
```

---

## Part 1 — Peer-agent communications (lead → worker)

### The envelope protocol

Every message on the bus is a compact JSON envelope
(`src/agent_redis_bridge/envelope.py`, spec in `SPEC.md`):

| Field | Notes |
|---|---|
| `id` | UUID; replies reference it via `in_reply_to` |
| `from` / `to` | agent IDs (sender validated against trust policy) |
| `branch` | required non-empty (detached HEAD → `invalid-branch` error) |
| `kind` | `request` \| `reply` \| `notify` \| `steer` \| `cancel` \| `hello` |
| `sent_at` | ISO 8601 |
| `payload` | kind-specific; `request` needs `payload.task`, `steer` needs `payload.message` |

`request` payloads may also carry `worktree` (name/base_ref/cleanup),
`expected_artifacts`, `allowed_paths`, and `commit_message` — these drive the
orchestration machinery in Part 2. Replies carry `ok`, `result`, `error`,
`completion`, `artifact_paths`.

### Redis key namespace

All keys live under `AGENT_REDIS_PREFIX` (default `agent_scratch:` — note the
prefix already ends in `:`; suffixes are concatenated without another colon,
the original bootstrap bug). Key builders: `redis_io.py` (`RedisConfig`).

| Key | Type | Purpose |
|---|---|---|
| `agent:<id>:inbox` | LIST | per-agent inbox; LPUSH to send, bridge BLPOPs |
| `agent:<id>:notify_inbox` | LIST | notify split when `BRIDGE_NOTIFY_INBOX=0` |
| `agent:<id>:status` | STRING+TTL | heartbeat `alive:<boot-token>`, TTL 60s, refreshed 30s |
| `registry:<id>` | HASH | tool/project/workspace/branch/path/pid/owner_token |
| `task:<id>:events` | STREAM | per-task progress events (maxlen ~500, 7d TTL) |
| `task:<id>:status` | HASH | compact state/phase/last_summary (7d TTL) |
| `task:<id>:result` | STRING | final structured result (30d TTL) |
| `usage:<scope>:<day>:*` | STRING | daily request / turn-seconds budgets |
| `tap` | STREAM | optional passive envelope archive (bus-tap logging) |

### Agent identity and trust

Agent IDs are `{tool}-{project}-{workspace}[-{role}]` (e.g. `codex-project-c-dev`,
`pi-sdk-project-b-dev-qcn-w`). The tool prefix comes from the engine
(`ENGINE_TO_TOOL`, `bridge.py`): `codex`, `gemini` (deprecated `gemini-acp`),
`grok`, `kimi`, `minimax`, `pi` (pi-rpc), `pi-sdk`, `asdk` (agent-sdk), `agy`,
and `cursor` (experimental / non-certifying).

Senders are validated per-request against `AGENT_TRUSTED_SENDERS`
(`agent_id=trusted|human|reject` pairs); unknown senders default to **reject**
→ `[bridge-error] sender-rejected` plus an error reply to the sender. Policy
also gates engine permissions: `trusted` → approvals off / workspace-write
sandbox; `human` → on-request approvals. Dispatchers set their identity via
`FROM_AGENT_ID` — getting this wrong is the #1 cause of `sender-rejected`.

### Worker (bridge) lifecycle

`python -m agent_redis_bridge` (`bridge.py`) runs one daemon per seat:

1. **Register**: HSET registry, SET heartbeat, print `[bridge] <id> online`.
2. **Warm up**: pool acquires + releases one engine so spawn failures surface
   at startup, not on first dispatch.
3. **Inbox loop**: BLPOP with 30s timeout (never `BLPOP 0` — managed buses
   drop idle sockets). Each valid `request` is validated (recipient, sender
   policy, 60s dedup window, usage budget), a pool slot is acquired (`busy`
   error reply if full), and the turn runs on a daemon thread.
4. **Per turn**: status/events written to Redis, `task_started`/milestone/
   `task_finished` notifies sent, then a `reply` envelope is LPUSHed to the
   requester's inbox.
5. **Shutdown**: SIGTERM/SIGINT → drain, clean registry/status keys, stop all
   engines. Heartbeat thread self-terminates the bridge after 3 consecutive
   Redis failures.

Per-turn wall clock is capped by `--turn-timeout` / `AGENT_TURN_TIMEOUT`
(default 3600s; set lower, e.g. 600s, for `agy-print` seats which can hang
after producing output). All engines normalise the message to
`"turn timed out after <N>s"`, which downstream salvage logic keys on.

### One dispatch round-trip

```
Orchestrator                Redis                    Worker bridge
    │  LPUSH request ───────► agent:<worker>:inbox      │
    │  (task-id on stderr)        │  BLPOP ────────────► │ validate, acquire slot
    │                             │ ◄── HSET task status │ [turn-start]
    │                             │ ◄── XADD task events │ engine runs turn
    │                             │ ◄── LPUSH notifies (to :notify_inbox)
    │                             │ ◄── SET task result  │ [turn-end]
    │  BLPOP own inbox ◄───────── │ ◄── LPUSH reply      │ [reply-sent], release slot
    │  match in_reply_to, print payload, exit 0/1
```

`scripts/agent-dispatch` is both the sender and the wait: it BLPOPs the
caller's inbox until a `reply` with matching `in_reply_to` arrives (exit 0 =
ok, 1 = worker reported failure, 124 = timeout). Non-matching replies are
pushed back for sibling dispatchers; notifies are dropped.

### CLI tooling

| Script | Role |
|---|---|
| `scripts/agent-dispatch` | canonical dispatcher: envelope build, `--check` preflight, `--dry-run-envelope`, worktree/artifact flags, reply wait |
| `scripts/agent-bridge-ping` | heartbeat check before dispatch (`alive`/`dead`/`registry=missing`) |
| `scripts/agent-inbox-watcher`(`-split`) | long-running inbox monitor for Claude sessions (Monitor-tool friendly; `-split` variant writes full envelopes to files and emits truncation-proof `[inbox-meta]` lines) |
| `scripts/agent-inbox-watcher-reliable` | hardened watcher: atomic `BLMOVE :inbox → :processing`, LREM-ack only after the envelope is durably on disk, startup re-drain — closes the BLPOP in-flight-loss mode that dropped a message on 2026-06-08 (`docs/claude-peer-coordination.md § "Reliable inbox daemon"`) |
| `.pi/extensions/agent-bus-seat/` | pi extension giving pi-based seats native bus tools (send/check envelopes) instead of shelling out to redis-cli |
| `src/agent_redis_bridge/ctl.py` (`codexctl`) | send / steer / cancel / watch / status / result / chat |
| `scripts/codex-dispatch`, `codex-bridge-ping` | deprecated aliases |

### Comms gotchas (hard-won)

- **`\n` shell-quoting**: bash double quotes don't interpret `\n`; use
  `$'...'`, heredocs, or the brief-to-file pattern. Verify with
  `--dry-run-envelope`.
- **Notify flood**: with `max_parallel > 1` keep `BRIDGE_NOTIFY_INBOX=0` so
  notifies go to `:notify_inbox`, not the reply inbox (a 2026-06-04 livelock
  stranded 3,437 envelopes before this split).
- **No Redis I/O in token hot paths**: per-token XADD on a TLS managed bus
  stalled generation by minutes; streaming now only heartbeats an 8s-throttled
  HSET.
- **One active session per agent_id**: two processes BLPOPping the same inbox
  race atomically; messages go to exactly one.
- **`FROM_AGENT_ID` and `BRANCH` must be set explicitly** per dispatch; stale
  env defaults cause `sender-rejected` / `invalid-branch`.

Full operational detail: `skills/using-agent-bridge/SKILL.md`,
`docs/claude-peer-coordination.md`.

---

## Part 2 — Agentic engineering orchestration

### Engines: one abstraction, many CLIs

An **engine** (`engines/base.py`, `AgentEngine` protocol) wraps one coding
agent process: `start()`, `run_turn_with_progress(task, timeout, policy,
on_event) -> TurnResult`, `steer()`, `interrupt()`, `stop()`. Normalised
events (`turn_started`, `model_text`, `command_started`, `turn_timeout`, …)
flow back to the bridge regardless of the underlying wire protocol.

| Engine | Wraps | Protocol | Process model |
|---|---|---|---|
| `codex` | `codex app-server` | JSON-RPC/stdio | persistent per slot |
| `pi-sdk` | `node tools/pi-sdk-host/host.mjs` (pi TypeScript SDK) | codex-mirrored JSON-RPC | persistent — **default worker seat** |
| `pi-rpc` | `pi --mode rpc` | NDJSON/stdio | persistent — fallback seat |
| `grok-acp` / `kimi-code-acp` / `mini-agent-acp` | respective CLIs | ACP JSON-RPC 2.0 | persistent (shared ACP base class) |
| `omp-acp` | `omp acp` (oh-my-pi, a pi fork) | ACP JSON-RPC 2.0 | persistent; **only ACP engine with a `--pi-tools` allowlist**, so readonly-gate certifiable. No `session/set_model` — model pinned at spawn |
| `opencode-acp` | `opencode acp` | ACP JSON-RPC 2.0 | persistent; modes `build`/`plan`. No allowlist surface ⇒ **not** readonly-gate certifiable |
| `cursor-acp` | Cursor Composer CLI | ACP JSON-RPC 2.0 | persistent; experimental, non-certifying |
| `gemini-acp` | deprecated Gemini CLI | ACP JSON-RPC 2.0 | **deprecated 2026-07-03; rejected by agent-dispatch** |
| `agent-sdk` | Claude Code Agent SDK | SDK | persistent Claude-over-bridge seats |
| `agy-print` | `agy --print <task>` | subprocess | **one-shot per turn** — stateless, good for reviewer seats |
| `agy-tmux` | `agy` in tmux | terminal session | persistent tmux-backed agy seat |

The pi-sdk host (`tools/pi-sdk-host/host.mjs`) exists because the typed SDK
eliminated ~280 LOC of pi-rpc quirk handling; `BRIDGE_PI_SDK_EVENT_LOG`
captures a replayable NDJSON trace of every SDK event for debugging.

### The engine pool

`engine_pool.py` is a thread-safe bounded pool sized by
`BRIDGE_MAX_PARALLEL` / `--max-parallel` (default 1). Acquire prefers idle
engines, lazily spawns up to the cap, returns `None` (→ `busy` reply) at
capacity. On release, engines reporting `is_healthy() == False` are discarded
and respawned for the next task. Steer/cancel envelopes route through
`task_engines` so they reach the engine actually running the turn.

### Worktree-isolated dispatch

`agent-dispatch --worktree <name> [--worktree-base <ref>] [--worktree-cleanup
keep|auto]` makes the bridge create a git worktree and a **fresh single-use
engine whose cwd is that worktree** — the worker physically cannot touch the
base checkout (isolation by construction, not discipline). The pool slot still
gates concurrency. This is the default posture for write-dispatches and the
enabler for parallel implementation on one agent_id.
See `docs/worktree-isolated-dispatch.md`, `docs/bridge-parallelism.md`.

### The completion machinery (bridge.py)

Runs after every turn, in order:

1. **Drive-to-completion loop** — for continuation-capable engines (pi-sdk),
   up to 3 nudge turns when expected artifacts are missing or the tree is
   dirty, with a progress-stall detector (same HEAD + same dirty set → stop).
2. **Orchestrator-commit** — when `--expected-artifact` flags are present and
   satisfied: verifies nothing landed outside `--allowed-path`, then commits
   on the worker's behalf (`Committed-by: agent-redis-bridge` trailer) or
   adopts the worker's own commit.
3. **Post-timeout adoption** — salvage path: a timed-out turn that nevertheless
   produced all expected artifacts gets committed and flipped to `ok=true`
   (timeout preserved in the completion record).
4. **Completion gate** (`completion_gate.py`) — classifies the end state
   (`committed_clean`, `dirty_uncommitted`, …) using only dirt *introduced
   during the turn*; bounces dirty turns to `ok=false` and preserves the
   worktree for inspection. Kills the "worker says done but left uncommitted
   edits" failure class.

### Role routing and seats

Seats are bridge instances named for their job:
`pi-sdk-<project>-dev-qcn-w` (worker), `...-qcn` (reviewer), etc. Seats get a
system-prompt addendum via `BRIDGE_ROLE_PROFILE_FILE` pointing at a profile in
`roles/`:

- `roles/reviewer.md` — adversarial reviewer; verdict vocabulary `SHIP` /
  `SHIP_WITH_NITS` / `FIX_BEFORE_MERGE` / `BLOCK_MERGE`; paired with a
  restricted toolset (`BRIDGE_PI_TOOLS=read,grep,find,ls`).
- `roles/lead.md` — coordination lead for a persistent multi-host bus seat:
  decide-and-own posture, evidence before assertion, terse over the bus, plus
  an irreversible-ops guard.
- `roles/team-seat.md` — team-member peer seat: executes a slice under a lead,
  reports done/blocked with checkable evidence (SHA, test counts, file:line).

Current routing policy
(`docs/agent-role-routing.md`, `docs/implementor-routing.md`):

- **Implementor ladder**: qwen3-coder-next via pi-sdk → Cursor Composer →
  Codex for the conceptual end.
- **Reviewer quorum**: Codex + cold Opus + agy as primary verdict seats;
  kimi / minimax as adjunct reviewers (findings count, verdicts advisory).

### Quality loops

- **gotcha-lint** (`gotcha_lint.py`, `.gotchas.json`): recurring bug classes
  start as briefing warnings injected into review briefs; after 3 recurrences
  they *graduate* to enforced CI failures (`--check-graduation` closes the
  "someone must remember" gap). Caught by construction, not reviewer memory.
- **calibration loop** (`calibration.py`, `.calibration/log.jsonl`): records
  review decisions and later confirmed/false-positive outcomes per reviewer
  seat; **report-only by design** — it raises questions about low-confirmation
  seats but never auto-tunes weights or trust.

---

## Part 3 — ARB Memory and the evidence/audit plane

Everything in Parts 1–2 produces claims; Part 3 is where the *evidence* lives.
Added incrementally after the 2026-06-10 synthesis, it is now roughly half of
`src/` and runs as its own deployed stack on arb-prod.

### The packages

| Package | What it is |
|---|---|
| `src/arb_memory` | The store and its whole service surface: versioned artefacts + semantic memory/hints (`store.py`, pgvector embeddings via `embed.py`), the audit plane (`audit.py`, `close.py`, `consumer_loop.py`), panel tooling (`panel_run.py`, `panel_audit.py`, `stance.py`), MCP servers (`mcp/`), eval/transcript flushers, brief hydration (`brief_hydrate.py`), served-hint records (`hint_reads.py`), the `arb-watch` TUI/web visibility seat (`watch/`, `visibility.py`), and journey export. Schema in `schema.sql` (owner-role step; `setup-schema` does NOT apply it). |
| `src/arb_messages` | Privileged-action broker (its own docstring): agents request actions they lack credentials for (`messages_request`), an operator door claims/fulfils/denies them, with per-agent registered keys (`keys.py`) and an audit trail. |
| `src/arb_files` | S3-backed file transfer MCP: put/get by inline bytes or presigned URL, sha256-tracked, audited. |
| `src/arb_secrets` | Sealed peer↔peer secret/env transfer over the bus: NaCl `Box` to the recipient's key, TOFU fingerprint pinning, TTL'd blobs, consume-once `GETDEL` delivery (`peer.py`, `protocol.py`). The bus sees routing metadata, never values. |
| `src/arb_crypto` | The shared NaCl primitive layer under `arb_secrets`: keypairs, seal/unseal, `Box` seal/open, fingerprints. |
| `src/arb_email` | Email-send MCP support (`email_send` tool surface), audited like the other doors. |
| `src/arb_registration` | One-time, operator-approved Buzz seat admission: hashed-at-rest token state, secp256k1 identity-bound registration signatures, exact Mark-pubkey thread approval, owner/membership provisioning, and profile read-back before final grant. Runbook: `docs/seat-self-registration.md`. |
| `src/agent_redis_bridge` | Parts 1–2 (the bridge itself), including the store-backed `claim_gate.py` (no-I/O, injected resolvers) and the older `readonly_gate.py` it supersedes in design. |
| `src/arb_warm_orch` | Agent SDK warm orchestrator (buzz control-plane pilot): a long-lived, channel-keyed, auto-resuming SDK session with the `dispatch_seat` typed tool (`dispatch.py`, lens schema-required) and merge/close PreToolUse gate (`gates.py`). Second consumer of the bridge's engine parts with inverted polarity — workers retire after each turn, the orch keeps context. |
| `src/buzz_ops` | Operational checks against the Buzz relay this fleet runs, distinct from the bridge's own bus. `seat_auth_tag_check.py` answers "would the observer proxy admit this seat's `kind:0`?" for every pubkey the relay treats as an agent, and `nip_oa.py` is the NIP-OA verifier under it (BIP-340, checked against the specification's vectors) — a missing or invalid auth tag blanks the Agent activity panel with nothing logged at any level. CLI: `scripts/buzz-seat-auth-tag-check`. |
| `src/codex_redis_bridge` | Pure re-export shim for the legacy name. |

### Store facts worth knowing before you touch it

- **Artefacts are versioned, never edited in place**: same `artefact_id` →
  `memory_store` bumps the version. `content_hash` is a domain-separated digest
  (`sha256("arbmem:artefact:v1\0" + mime + "\0" + kind + "\0" + payload)`,
  `hash.py`) whose first 16 hex chars mint content-derived ids — it is NOT a
  bare sha256 of `content` (a documented misread that cost a false defect
  filing, ARB-B15/B19).
- **Optimistic concurrency is live**: `memory_store` accepts
  `expected_version` and refuses a moved head with `refused_version_mismatch`
  (three fail-closed doors; design
  `docs/superpowers/specs/2026-07-30-arb-b19c-expected-version-design.md`).
  Guard it on any multi-session artefact — the backlog artefact collided three
  times in one day before this existed.
- **The audit plane closes runs, not prose**: panel votes ride audit streams
  into Postgres; a verdict only exists after the reconcile-gated close
  (`arb-audit-close-request` over the bus → `audit-close-consumer`), which
  refuses missing/duplicated/laundered stances. An announced verdict without
  `outcome=emitted` is un-audited.

### Deployment shape

`deploy/docker-compose.yml` (run on arb-prod, deploys owner-gated) ships the
service set the compose-pin tests assert: `memory`, `writer` (the only write
path), `mcp` (read-only OAuth door; no Redis env, no published ports), `audit`,
`audit-close-consumer`, `eval`, `transcript`, `visibility`, `cloudflared` (the
sole ingress — every public hostname is a tunnel route), and `arb-tools-static`
(static installer host for ARB codex builds). Only `memory` carries a `build:`
block — `docker compose build mcp` is a silent no-op, a documented trap.
Operational steps: `deploy/README.md`.

---

## The workflow layer (how the machinery is used)

### Roles: warm Opus, cold Opus, workers

- **Warm Opus** (the driving session) owns the user relationship, workflow
  choice, dispatch, and *integration* — only the orchestrator merges, because
  cross-work conflicts aren't visible from inside a single dispatch. It
  verifies worker output **from git (SHA, diff, test run), never from reply
  prose**.
- **Cold Opus** is a fresh subagent (native `Agent` tool, *not* the bridge)
  given only the brief — spec path + diff + output format + report path. It is
  never the author of what it reviews; brief contamination is its only failure
  mode.
- **Workers** stay inside their dispatched worktree, report evidence not
  assertions, escalate on ambiguity, and never merge.

The **visibility boundary** is the file split: workers read `AGENTS.md`,
orchestrators read `CLAUDE.md` (plus `AGENTS.md`). Each layer's rules are
noise to the other — that's deliberate.

### Workflows A and B (`docs/pipeline-operating-manual.md`)

**At project kickoff, the orchestrator must confirm with the user which
workflow to run before the first dispatch.** Default A unless the risk profile
demands B; mixed projects record per-phase choices in
`docs/phase-workflow.template.md`.

| | A — lightweight | B — rigorous |
|---|---|---|
| Per task | codex implements → one independent review → triage | implement → **parallel** self-review + independent review, ≤3 re-review rounds |
| Pre-merge | none | triple review: codex + gemini/agy + **cold-Opus** (fresh context, spec + diff only) |
| Use for | reversible features, CRUD, UI, plumbing | migrations, irreversible ops, regulated paths, behaviour-preservation rewrites |
| Cost | baseline | ~1.3× wall-clock |

Both shapes: spec + plan + briefs committed to main *before* the worktree is
created; workers commit on `feat/<topic>`; merge with `--no-ff` to preserve
the review trail.

### Pattern catalogue (`docs/orchestrator-patterns.md`)

- **Parallel dispatch via worktrees** — N tasks on one agent_id, one worktree
  each, `BRIDGE_MAX_PARALLEL=N` (≈2h sequential → 30–40min observed).
- **Zero-poll monitoring** — `agent-dispatch` *is* the wait; run it with
  `Bash(run_in_background=true)` (never wrapped in `&`), shell exit = task
  completion. No status polling, no token burn.
- **Dual review with cold reviewers** — implementer self-review (catches
  data-flow bugs) + cold reviewer (catches scope creep and architecture
  smells); the two find disjoint bug classes.
- **Gotcha briefing → graduation** — recurring review catches go into the next
  brief, then into `gotcha-lint` enforcement.
- **Brief-to-file** — any non-trivial dispatch is "Read the brief at <path>
  and execute it": no quoting hazards, durable artifact, reviewable in git.
- **Cross-host orchestration** — all bridges on one managed TLS bus; target
  IDs resolve across hosts; registry heartbeats give pre-dispatch liveness.

### Review hygiene and decisions

- **Independence**: during an independent review phase, reviewers must not see
  each other's reports — write outside the repo (or per-reviewer worktrees)
  until all finish. Motivated by a real near-miss (project-g, 2026-05-31:
  codex's report leaked into a concurrent cold-Opus pass).
- **Consensus is input, not authority** (`docs/quorum-decision-taxonomy.md`):
  every panel outcome is logged in a closed taxonomy; overriding a consensus
  requires five named fields (voter results, auditor result, falsifiable
  doubts, chosen safer action, deferred follow-up).
- **Evidence-first remediation** (`docs/evidence-first-remediation.md`): no fix
  is scoped from observation alone — mechanism must be verified, and a single
  reviewer with contradictory *evidence* outweighs the other seats' opinions.
- The flagship panel win (`docs/multi-model-consensus.md`): three seats
  unanimously rejected all three framed options and converged on an unframed
  fourth (citation-marker namespace collision) — the panel's value was the
  option the orchestrator hadn't written down.

### Field lessons (case studies, `docs/orchestrating-claude-peers.md`)

- Designate a coordination lead in writing; decisions go in git (ADRs), not
  the ephemeral bus — successor sessions inherit a seat cold from the decision
  log (proven by a 5-week peer absence and clean reconnect, 2026-06-07).
- Phase gates must be observable checks (queue depths, rows, logs), verified
  independently by the lead before closing.
- Symmetric vulnerabilities across hosts need coordinated fixes on every
  sharing node, propagated with evidence, not prose.
- Heartbeat TTL is the only truthful liveness signal; watcher absence ≠ app
  outage.

---

## Operations

### Configuration

One `.env` per seat/worktree configures daemon and dispatcher alike
(`.env.example` is the reference): bus location (`AGENT_REDIS_HOST/PORT/DB/
PREFIX`), TLS+auth for managed buses (`AGENT_REDIS_TLS/USER/PASSWORD` — the
password rides `REDISCLI_AUTH`, never `-a`), identity (`AGENT_PROJECT/
WORKSPACE/WORKDIR`), trust (`AGENT_TRUSTED_SENDERS`), dispatch defaults
(`AGENT_DISPATCH_FROM/BRANCH`), budgets (`AGENT_DAILY_REQUEST_LIMIT`,
`AGENT_DAILY_TURN_SECONDS_LIMIT`, `AGENT_USAGE_SCOPE`), and per-turn cap
(`AGENT_TURN_TIMEOUT`). This repo's own dev bus is local
`127.0.0.1:6390/db12` (`.env.pi-dev`); production setups point every host at
one managed Valkey.

### Deployment

`systemd/agent-bridge@.service` (recommended) runs one bridge per instance env
file with **`Restart=always`** — `on-failure` would not respawn after SIGTERM
or clean exits, exactly the deaths supervision must catch; crash-loops are
bounded by `StartLimitBurst=5/60s`. `scripts/verify-bridge-supervision` proves
the unit actually respawns (SIGKILL *and* SIGTERM → new MainPID + fresh
boot-token heartbeat). A successor encountering a stale hard-crash lease waits
in-process through its TTL, so systemd does not exhaust the start limit before
the identity becomes claimable. Replies are durable in Redis, so a bridge restart never loses a
finished task.

Beyond bridge daemons, **persistent interactive seats** (a Claude Code or pi
session holding an agent_id long-term) have their own supervision stack:
console + supervisor scripts per seat (e.g. `scripts/pi-project-b-a-console` /
`-supervisor`, systemd units in `systemd/pi-seat-*.service`) that boot the
session in tmux, poll readiness, and self-heal off the heartbeat key — design
rationale in `docs/always-up-seats.md`. To stand up a brand-new node end to
end (Claude team seat + pi seat joined to the bus), follow
`docs/runbooks/agentbridge-seat-setup.md`.

### Observability and debugging

- **Bus-tap logging** (`docs/bus-tap-logging.md`): best-effort XADD of every
  envelope to a capped `tap` stream, drained by a daemon into dated JSONL
  files (14-day retention) — token-free forensics (`grep <id> agent-bus-*.log`).
  Archives are written 0750/0640 and persist envelope contents at rest, so a
  secret that crossed the bus is **rotated**, not scrubbed (and never grepped
  for by value).
- **Probes**: `agent-bridge-ping` (liveness), `codex_cwd_probe.py`,
  `codex_thread_soak.py`, `scripts/cc-channel-probe` plus
  `docs/runbooks/cc-channel-corruption.md` for the Claude Code tool-result
  channel regression (pin 2.1.153; trust independent artifacts, not channel
  echo, while suspect).
- **Task-level**: `task:<id>:status` for cheap checks, `:events` stream only
  when needed, `:result` for the final record.

### Sister tool: candid

`tools/candid` is a standalone adversarial-critique CLI wrapping `codex exec`
directly (no bridge required): `git diff | candid --mode code`. Modes:
default adversarial, `code`, `product`, `plan`. Sub-30s prose-only second
opinions, complementary to the bridge's multi-minute tool-using dispatches.

### Tests and packaging

The runner is **pytest** (config in `pyproject.toml`; ~3800 tests as of
2026-07-31), run from a venv created **at `<checkout>/.venv`** — any other
interpreter is exec'd out from under pytest at collection
(`src/agent_redis_bridge/README.md` § Tests,
env-trap B4(g)). Full recipe and the non-skippable gates
(`scripts/suite-gate` for the whole suite with its skip-set allowlist,
`scripts/graph-sql-gate` / `scripts/arb-messages-gate` for the DSN-backed
subsets): `src/agent_redis_bridge/README.md` § Tests. Packaging (`pyproject.toml`): Python ≥3.11, four
hard deps (`redis`, `jsonschema`, `pynacl`, `psycopg[binary]`), per-subsystem
extras (`agent-sdk`, `arb-memory`, `arb-files`, `arb-email`, `visibility`,
`bench`), and console scripts for the bridge, `codexctl`, and the `arb_*`
service entrypoints. `src/codex_redis_bridge` is a pure re-export shim.

---

## Where to go next

| Question | Doc |
|---|---|
| Wire protocol, envelope/key rules | `SPEC.md` (historical; see banner) |
| Day-to-day dispatch recipes, failure shapes | `skills/using-agent-bridge/SKILL.md`, `src/agent_redis_bridge/README.md` |
| Dispatch/monitoring patterns | `docs/orchestrator-patterns.md` |
| Full A/B workflow manual | `docs/pipeline-operating-manual.md` |
| Pi warm-orchestrator operations | `docs/pi-orchestrator-operating-guide.md` |
| Pi extension mechanics (`arb_dispatch`, `/arb-watch`) | `pi-extensions/README.md` |
| Engine pool / parallelism design | `docs/bridge-parallelism.md` |
| Which engine for which job | `docs/agent-role-routing.md`, `docs/implementor-routing.md` |
| Review panels and decision rules | `docs/multi-model-consensus.md`, `docs/quorum-decision-taxonomy.md`, `docs/evidence-first-remediation.md` |
| Claude↔Claude peering | `docs/claude-peer-coordination.md`, `docs/orchestrating-claude-peers.md` |
| Worker brief discipline | `docs/dispatching-implementation-briefs.md`, `docs/worker-brief-preamble.txt` |
| Deployment | `systemd/README.md`, `docs/always-up-seats.md` |
| Provisioning a new seat node | `docs/runbooks/agentbridge-seat-setup.md` |
| Seat role profiles | `roles/reviewer.md`, `roles/lead.md`, `roles/team-seat.md` |
| Incident runbooks | `docs/runbooks/cc-channel-corruption.md`, `docs/runbooks/fleet-restart.md`, `docs/bus-tap-logging.md` |
| ARB Memory store design (hash preimage, phase 0) | `docs/superpowers/specs/2026-06-20-arb-memory-phase0-store-design.md` |
| Optimistic-concurrency store guard | `docs/superpowers/specs/2026-07-30-arb-b19c-expected-version-design.md` |
| Deploying / operating the memory stack | `deploy/README.md` |
| Defect-class corpus (review detection moves) | `docs/defect-classes/README.md` |
| Doc routing layer (index of everything) | `docs/INDEX.md` |

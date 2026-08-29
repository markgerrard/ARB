---
name: using-agent-bridge
description: Operate AgentRedisBridge for peer-agent dispatch, isolated implementation or review work, audited panels, completion monitoring, and bridge diagnosis. Use when requests mention `agent-dispatch`, `agent-redis-bridge`, `codex-redis-bridge`, `agent-bridge-ping`, codex/grok/agy/pi/Claude seat IDs, `agent_scratch:` Redis keys, shared-bus or cross-host orchestration, Claude peer coordination, `sender-rejected`, `envelope-invalid`, `bridge busy`, task monitoring, panel votes, or audit closure. Also use whenever another model or bridge seat should review, implement, investigate, or coordinate work.
---

# Using the Agent Redis Bridge

This skill captures the operational knowledge for driving the `agent_redis_bridge` from inside Claude Code. The bridge turns a local codex/gemini CLI into a Redis-addressable peer — you LPUSH a request envelope to an agent's inbox, the bridge runs the engine, the reply lands back on your inbox. Sounds simple; has a handful of sharp edges.

The protocol guarantees and helper-script shapes documented here apply on any host running the bridge. Host-specific state — which Redis bus the bridge currently points at, which agent IDs are registered, where the env file lives — belongs in personal memory or operator notes, not here.

For the whole-system picture — how the comms plane, engine orchestration, workflow layer (A/B, warm/cold Opus), and ops layer fit together, plus a question→doc map — read [`docs/architecture-overview.md`](../../docs/architecture-overview.md). This skill is the operational subset: dispatch recipes, monitoring, and failure shapes.

## When to reach for this skill

- You want a peer agent (codex, gemini, or grok via `--engine grok-acp`) to do a substantial piece of work in parallel — code review, implementation, investigation, audit
- You're already mid-dispatch and the bridge returned `[bridge-error] sender-rejected ...`, `envelope-invalid invalid-branch`, or `bridge busy with task <uuid>`
- You're trying to figure out whether a task is still running, and considering polling Redis
- You see notify envelopes piling up on an inbox and aren't sure if it's normal
- The user mentions cross-host orchestration ("dispatch from dev to staging codex", "kick off a build on the prod-host gemini")

### Different shape: Claude ↔ Claude coordination only (no engine)

If you want **Claude Code sessions to talk to each other** over the bus — no engine on either side — that is a distinct workflow from everything below. See [§ The two shapes of the bus](#the-two-shapes-of-the-bus-engine-dispatch-vs-claude-peers) before going further.

## The two shapes of the bus: engine dispatch vs Claude peers

The bus supports two workflows that share keys and envelope format but are **operationally distinct**. The bridge *evolved into* the first; it *grew out of* the second. Recognise which one you're in before reaching for any recipe in this skill — most of this skill is shape 1 only.

| | Shape 1 — engine dispatch (rest of this skill) | Shape 2 — Claude ↔ Claude peers |
|---|---|---|
| The other side is | A headless engine worker (`codex-…`, `gemini-…`, `agy-…`) wrapped by the bridge daemon | Another interactive Claude Code session (`claude-…`) |
| Relationship | You **dispatch work**; the worker executes a self-contained brief and exits the turn | You **negotiate**; the peer is an autonomous session that pushes back, asks, and disagrees |
| Daemon required | Yes (systemd `agent-redis-bridge@`) | **No** — just the inbox watcher under a persistent Monitor + `LPUSH` to send |
| Completion signal | `agent-dispatch` process exit (dispatch-is-the-wait) | There isn't one — peer replies arrive whenever the peer's session gets to them |
| Progress | `task:<id>:status` / `:events` / `:result` keys | Inbox envelopes only; evidence lives in git (plan docs, ADRs, commits) |
| Sender policy | Enforced by the daemon (`AGENT_TRUSTED_SENDERS`) | Convention only — peers SHOULD assert `to == my_id` per envelope |
| Brief quality | Must be fully self-contained (worker can't ask follow-ups cheaply) | Can be conversational; under-specification gets negotiated, not mis-executed |
| Liveness | Health-check script / heartbeat | `TTL :status` (>0 alive) — check **before** diagnosing silence; peers go quiet for long stretches mid-task legitimately |

**Shape-2 plumbing** (env, DB probing, watcher invocation, wire-level gotchas — idle-timeout BLPOP drops, chunk-shift truncation, cutover message loss): [`docs/claude-peer-coordination.md`](../../docs/claude-peer-coordination.md).

**Shape-2 workflow at N ≥ 3 peers** (one session orchestrating the others — field-proven on the Project A ↔ Project B run): [`docs/orchestrating-claude-peers.md`](../../docs/orchestrating-claude-peers.md). The load-bearing rules, compressed:

1. **Designate a coordination lead in writing** (ADR) — lead sequences and assigns; peers handle tactics among themselves; human keeps product/scope only. Explicit escalation: peer disagreement → lead → human.
2. **A phase plan with observable gates is the shared ground truth** — peers can't see each other's terminals; evidence (IDs, SHAs, log lines) closes gates, prose doesn't.
3. **The lead verifies independently before closing a phase** — a peer's "done" is a claim filtered through what's visible from *their* host. Same principle as "the reply is a claim; the commit is the evidence", applied cross-host.
4. **Decisions go in git, not the channel** — the bus has no retention; a successor session claiming a vacated `agent_id` inherits queued inbox messages *and* reads the decision log to inherit context. (Corollary: one active session per agent_id — concurrent BLPOPs race.)
5. **Files travel as `kind=notify`, `event=file_drop`** payloads (`{name, sha256, bytes, lines, content, note}`) — verify the sha on receipt. For docs/diffs, not binaries.
6. **Arm the inbox watcher at session start** (it doubles as your heartbeat) **plus a re-armed ScheduleWakeup safety net** (~1800s) as the dead-man's switch for a silently dead Monitor or Redis connection.

Mixed runs are normal and good: a Claude peer on another host (shape 2) can itself dispatch to engine seats on *its* host (shape 1). The shapes compose; the recipes don't — don't point `agent-dispatch` at a `claude-…` peer expecting task keys, and don't treat an engine worker as something you can negotiate with mid-task (that's what `steer` is for).

## Hard rule: bridge seats are the default, raw API calls are not

**Whenever you need a model that has a bridge seat on this host, dispatch through the seat. Do not reach for `curl` against OpenRouter / OpenAI / Gemini / Anthropic directly as a shortcut.** Raw API calls bypass the bridge's harness, envelope, trusted-sender policy, orchestrator-commit, audit trail in Redis, and any role-profile / completion-gate behaviour we've configured per engine. They also bill against the wrong account (e.g. OpenRouter spend for an OpenAI model that the codex bridge would handle for free on your direct OpenAI key) and give you the *raw* model instead of *the model inside its agentic harness*, which is materially different — "Codex GPT-5.5" in the routing doc means "GPT-5.5 inside codex's CLI", not "raw OpenAI GPT-5.5".

The map between model and seat (this host, as of 2026-06-06):

| Model family / "role"          | Seat (`--engine` / `--target-id`)             | Notes |
|---|---|---|
| Codex GPT-5.5 (OpenAI frontier inside the codex harness) | `codex` / `codex-<project>-<workspace>`      | This is what the routing doc means by "Codex GPT-5.5" |
| agy-print (Gemini-family inside agy)                    | `agy-print` / `agy-<project>-<workspace>`    | Gemini-equivalent on this stack |
| Cursor Composer 2.5                                     | `cursor-acp` / `cursor-<project>-<workspace>`| Implementor, not reviewer (per routing-doc ladder) |
| qwen3-coder-next                                        | `pi-rpc` / `pi-<project>-dev-qcn-w`          | Default implementor for bounded work |
| qwen3.7-max                                             | `pi-rpc` / `pi-<project>-dev-qwen37max`      | Off-ladder Codex-unavailable fallback |
| kimi-k2.6 / minimax-M3                                  | `pi-rpc` / `pi-<project>-dev-<role>`         | Adjunct reviewers (see panel section) |
| Devin SWE-1.7                                           | `devin-acp` / `devin-<project>-<workspace>`  | Adjunct reviewer, ACP/protocol-native lens (see panel section); also a proven implementor |
| Cold-Opus (Anthropic)                                   | NOT a bridge seat — spawn via Claude's Agent subagent tool with `subagent_type: code-reviewer-report-writer` (or similar) and `model: opus` | The only "non-bridge" reviewer; lives in-process |

The **only** times a raw API call is appropriate:

1. **Capability probe before standing up a seat** — e.g. you need to verify a new OpenRouter model slug exists and produces sane output before wiring an env file + drop-in for it. One-off, ~2 messages, billed under a few cents.
2. **Health / billing introspection** — e.g. `GET /api/v1/credits` to measure usage delta as proof of routing (used to confirm a pi-rpc dispatch actually hit OpenRouter, not a local fallback).
3. **No bridge seat exists for the model on this host AND standing one up would be disproportionate for a one-shot need** — narrow case; if you're using a model more than once, stand up the seat.

If you find yourself writing `curl ... openrouter.ai/api/v1/chat/completions` (or similar) for *real* work — a design panel, a review, an implementation, an analysis — **stop and stand up the seat instead** (env file + systemd drop-in + reload + enable; ~3 min total per the brain-seat pattern in `docs/qwen-worker-seats.md`). The "I'll just curl it this once" shortcut is the same class of laziness as borrowing a project-b seat for brain work — both bypass the per-project / per-harness pattern we've shipped.

### Per-project seat hygiene

Even when the seat exists, **dispatch to a seat pointed at the project you're working in**, not a sibling project's seat. Borrowing `codex-project-b-dev` to think about a brain change works (the model doesn't read files for a pure design task) but couples your dispatch to project-b's bridge load, gives the model a workdir that doesn't match your brief's relative paths, and clutters the audit trail on the wrong project. Stand up `codex-<your-project>-dev` and `agy-<your-project>-dev` first; cost is the same env-file + drop-in pattern.

### Seat trust is set at spawn by the spawning orchestrator (co-signed Mark, 2026-07-18)

A seat's `--sender-policy` set is a **spawn-time parameter owned by the orchestrator that stands
the seat up** — in the plist's `ProgramArguments` for launchd seats, on the CLI for ad-hoc
daemons. Widening or narrowing a seat's trusted senders is execution-layer seat administration:
the owning orchestrator edits its seat's flags and restarts (`launchctl kickstart -k`); it does
not route through a separate human gate. Two rails that keep this safe:

- **Trust lives in per-seat flags, never the shared env file.** `AGENT_TRUSTED_SENDERS` in a
  shared env file widens every seat that sources it in one stroke; per-plist `--sender-policy`
  pairs keep each seat's trust surface independently visible and independently changeable.
- **Widening is not a substitute for per-project seats.** Granting another *orchestrator
  identity on the same project* is what widening is for (e.g. a second warm session or a
  consult identity of the same project). Another *project* wanting the engine gets
  its own `<engine>-<project>-<workspace>` seat per the hygiene rule above — trust follows the
  seat, not the other way round. Remember `trusted` maps to the engine's auto-approve mode
  (e.g. Devin `bypass`); `human` is the read-only-ish middle ground for consult-style senders.

## Worktree-by-default for any dispatch that writes to the repo

**Before dispatching any task that will write to the repo, pre-create a git worktree and pass its path into the task body.** This is Pattern A in `docs/orchestrator-patterns.md`. Skipping it is a real, recurring footgun.

The bridge daemon's `AGENT_WORKDIR` typically points at the same git checkout the parent Claude session is operating in. So when the dispatched codex/gemini creates a feature branch and commits, *the parent session inherits the branch checkout* — every subsequent `git commit` from the parent session interleaves into the feature branch's history. `.git/index.lock` is shared between the two writers, so concurrent commits race. The bug only surfaces after the impl returns and you're trying to reason about a feature branch whose log has unexpected docs commits mixed in.

### The right recipe

```bash
cd <repo-root>
git worktree add -b feat/<branch-name> .claude/worktrees/<task-name> <base-branch>

# Publish (FABA driver; holds ARB_MEMORY_REDIS_URL), then enqueue via the quartet:
/<bridge-clone>/scripts/arb-memory-harness-publish \
  --target-agent-id codex-<project>-<workspace> \
  --brief <brief-path> \
  > /tmp/<task>.receipt.json
FROM_AGENT_ID=claude-<project>-<workspace> \
BRANCH=feat/<branch-name> \
AGENT_ENV_FILE=<path-to-app-worktree>/.env \
env -u ARB_MEMORY_REDIS_URL \
/<bridge-clone>/scripts/agent-dispatch \
  --engine codex \
  --target-id codex-<project>-<workspace> \
  --timeout 5400 \
  --run-id "$RID" \
  --artefact-id "$(jq -r .artefact_id /tmp/<task>.receipt.json)" \
  --version "$(jq -r .version /tmp/<task>.receipt.json)" \
  --receipt /tmp/<task>.receipt.json \
  --brief <brief-path> \
  > /tmp/<task>.out 2> /tmp/<task>.err
```

Put the worktree path in the **brief** (not a free-form positional task string — removed in Slice 1d-iv). The `BRANCH=feat/<name>` env var only feeds the envelope's invalid-branch check; it doesn't make codex `cd` anywhere.

After the dispatch returns, the worktree is independent — review at leisure, `git push` from inside it, merge to base, then `git worktree remove .claude/worktrees/<task-name>` cleans up.

#### Hard isolation: `--worktree` (collision-free by construction)

The prose pattern above is *soft* — the agent is asked to `cd` into the worktree but its cwd is still the repo root, so a misbehaving (or parallel) agent can still touch the base checkout. For **file-mutating parallel dispatch**, prefer the `--worktree` flag: the bridge creates the worktree and runs the task on a fresh engine whose **cwd IS the worktree**, so the base checkout cannot be modified — by construction, not by instruction.

```sh
# After harness-publish → receipt; ordinary path requires the pre-minted quartet:
agent-dispatch --engine codex --target-id codex-<proj>-<ws> \
  --worktree task-a [--worktree-base <ref|HEAD>] [--worktree-cleanup keep|auto] \
  --run-id "$RID" \
  --artefact-id "$(jq -r .artefact_id /tmp/<task>.receipt.json)" \
  --version "$(jq -r .version /tmp/<task>.receipt.json)" \
  --receipt /tmp/<task>.receipt.json \
  --brief <brief-path>
```

- The worktree is `<workdir>/.claude/worktrees/<name>`, created with `git worktree add --detach <base>` (default base `HEAD`); `<name>` is charset-restricted (no path traversal).
- `--worktree-cleanup keep` (default) leaves it for you to inspect / push / `git worktree remove`; `auto` removes it after the reply.
- Counts against `max_parallel` like any task. Omit the flag → unchanged behaviour (pooled engine, repo-root cwd). See `docs/worktree-isolated-dispatch.md`.
- A **gitignored** base `.venv` is mirrored into every fresh worktree (real `bin/` + `lib/…/site-packages` dirs, symlinked entries, **copy-on-write editable hooks** rewritten to the worktree), so seats can run `.venv/bin/python -m pytest` and it exercises the WORKTREE's checkout of the repo's own package; new pip installs land worktree-side. Briefs should still name that invocation (the venv is discoverable, not on `PATH`; plain `pytest` still misses), and use `python -m …`, never the bin entry-point scripts (base-interpreter shebangs). Closes the silent test-execution loss from memory `seat-worktree-python-env-gap` + panel finding F2 (`docs/worktree-isolated-dispatch.md` § venv mirror).
- **Independent-review panels:** give each bridge reviewer its own `--worktree review-<engine>` off the same base ref (default `keep` cleanup so the reports survive to collect). Besides the write-collision guard, each reviewer's separate cwd means ordinary workspace-scoped `ls`/glob won't surface a peer's in-repo report (the concurrent-review *read-leak*) — accident-prevention, not a sandbox (a reviewer that walks `../` or an absolute path can still reach a sibling). So the out-of-repo `/tmp` discipline is redundant for bridge reviewers writing in their own worktrees. A native subagent reviewer (cold-Opus) isn't a bridge dispatch and still needs the out-of-repo rule. See `docs/multi-model-consensus.md`.

### When you can skip the worktree

- **Read-only dispatches** (reviews, audits, investigations) shouldn't write to `.git` — but if a parallel impl dispatch is running on the same host, even read-only dispatches still see whatever HEAD that impl has checked out. Worktree-everything is the safer default; the cost is one `git worktree add`.
- **Trivial one-sentence dispatches** that finish in seconds and don't race with anything. Still: when in doubt, worktree.

### Recovery if you've already dispatched without one

Options ranked least- to most-disruptive:

1. **Don't commit anything in the parent session until the dispatch returns.** `Write`/`Edit` stage files in working tree without `git add` — they sit untracked, codex's commits proceed unimpeded, you clean up after.
2. **Cherry-pick the interleaved commit to the right branch, drop from the feature branch.** Only after the dispatch finishes — never rebase a branch the dispatched agent is actively writing to.
3. **Migrate mid-flight.** Risky. Aborts the dispatch, requires re-dispatch, loses work-in-progress. Only worth it if the interleave is structural (multiple commits, conflicting changes), not cosmetic.

## The canonical dispatch recipe

Use `agent-dispatch`, not raw `LPUSH`. The shell helper handles envelope construction, BLPOPs for the reply with strict ID matching, and exits 0/1/124 based on the reply payload. It is the same protocol surface a fresh implementation would re-derive — there's no reason to bypass it.

<!-- fragment:dispatch-recipe begin -->
```bash
# Slice 1d-iv: ordinary dispatch is store-before-send via dispatch_authority.
# 1) Short-lived FABA driver publishes the brief (holds ARB_MEMORY_REDIS_URL):
/<bridge-clone>/scripts/arb-memory-harness-publish \
  --target-agent-id codex-<project>-<workspace> \
  --brief <brief-path> \
  > /tmp/<task>.receipt.json
# 2) Non-FABA enqueue (no publish credential) through the single authority:
FROM_AGENT_ID=claude-<project>-<workspace> \
BRANCH=<your-current-branch> \
AGENT_ENV_FILE=<path-to-the-app-worktree>/.env \
env -u ARB_MEMORY_REDIS_URL \
/<bridge-clone>/scripts/dispatch-dev \
  --engine codex \
  --target-id codex-<project>-<workspace> \
  --timeout 5400 \
  --run-id "$RID" \
  --artefact-id "$(jq -r .artefact_id /tmp/<task>.receipt.json)" \
  --version "$(jq -r .version /tmp/<task>.receipt.json)" \
  --receipt /tmp/<task>.receipt.json \
  --brief <brief-path> \
  > /tmp/<task>.out 2> /tmp/<task>.err
```

`dispatch-dev` wraps the Go client edge (`tools/go-client`, auto-built on first use;
`USE_BASH_DISPATCH=1` falls back to the raw Python `scripts/agent-dispatch`) and
AUTO-DEFAULTS a meaningful `--run-id` (from the `--brief` path slug, or
`<target>-<branch>-<HHMMSS>`) when one isn't given — so it never hits the
`--run-id`/`--adhoc` hard-refuse the raw `agent-dispatch`/`go-client` binaries enforce as of
2026-07-01. Ordinary request/worktree_run **must** pass the pre-minted
`--artefact-id`/`--version`/`--receipt`/`--brief` quartet; free-form positional task
strings were removed in Slice 1d-iv (enqueue only via `dispatch_authority.publish_and_enqueue`).
Still mint one yourself for a panel/multi-round workflow —
`RID=panel-<slug>-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 3)`, reused verbatim on
every seat in that round — because the auto-default is per-call (different target/timestamp
per seat unless they share one brief path), so it won't group a multi-seat
panel under one label on its own. See "Auditing a review/design panel" below.
<!-- fragment:dispatch-recipe end -->

**`gemini-acp` is DEPRECATED (2026-07-03) and non-functional** — Google deprecated the `gemini` CLI,
so the engine no longer runs; `agent-dispatch --engine gemini-acp` now exits with a deprecation error.
Don't stand up gemini seats (memory `gemini-cli-deprecated`).
For Grok Build (the TUI driving this session) use `--engine grok-acp --target-id grok-<project>-<workspace>`. The local `grok` binary must be logged in (same credentials the TUI uses).

### Why each override matters

<!-- fragment:env-overrides begin -->
| Override | Why it's needed |
|---|---|
| `FROM_AGENT_ID` | The bridge's `--sender-policy` only trusts specific agent IDs. The shell helper's default may be a legacy value, so supply the real ID. |
| `BRANCH` | The bridge rejects empty branches as `envelope-invalid invalid-branch`. In detached HEAD, `git branch --show-current` returns `""`. |
| `AGENT_ENV_FILE` | Points helper scripts at the correct Redis and project settings for this worktree. |
| `--target-id` | Overrides legacy or inferred target names; use the actual registered agent ID. |
| `--timeout` | Default is 1800 seconds. Use 5400+ for substantial review or implementation tasks. |
| `--turn-timeout` | Optional ceiling for one task engine turn, not total multi-turn dispatch duration. Trusted senders may request above or below the seat default, up to its `--turn-timeout-max`; keep client `--timeout` above it. |
<!-- fragment:env-overrides end -->

## Background it, do not wrap it

Fire `agent-dispatch` as a backgrounded shell command via `Bash(run_in_background=true)`. **Do not** wrap it in `(... &) sleep N && cat` or any other pseudo-detach. The dispatcher's own BLPOP loop is the wait mechanism — wrapping it detaches the real waiter and forfeits the harness's natural task-completion notification, forcing wasteful `ScheduleWakeup` polling instead.

```
✗ Anti-pattern:  Bash("(agent-dispatch ... &) ; sleep 60 && cat /tmp/out")
✓ Canonical:     Bash(command="agent-dispatch ...", run_in_background=true)
```

When the harness fires the task-completion notification, read the captured `/tmp/<task>.out` — the dispatcher prints the JSON `payload` (which contains the agent's reply text) before exiting.

## Monitoring without burning tokens

### Which bus? (managed vs self-hosted — read before trusting any inbox idiom below)

The ARB bus is migrating from the shared managed DO Valkey (single `default` user, no ACLs) to a
**self-hosted Valkey with per-identity scoped ACLs** (`arb-bus.example.com:6379`, TLS-only). Both are
live during the migration; each host flips independently. **On the self-hosted bus, `NOPERM` exists**,
and several idioms in this skill and in `docs/claude-peer-coordination.md` break:

- **Foreign-inbox `LLEN`/`LRANGE` is NOPERM.** `LPUSH` to a peer still succeeds, but you cannot read
  back their queue depth — the recipient's **reply** is the only consumption signal. (Your OWN inbox
  `LLEN`/`LRANGE` still works, so the pollution-drain recipe below is unaffected.)
- **`KEYS`/`SCAN`/`CONFIG` are NOPERM** to non-admin creds — no "who's online" browse, no DB probe.
  Use per-id `GET`/`TTL` on `:status`, or `agent-bridge-ping`.
- **Audit-emit denial fails soft** and surfaces at verdict close as `refused_reconcile`, not at emit.
- DB is NOT an ACL boundary on Valkey 8.1.9 — isolation rides key prefixes; a wrong-DB write succeeds
  silently.

Full model, credential pattern (one cred per orch type per host, suffix-wildcard session ids,
`arb-worker-<host>` for seats), onboarding, and the Workflow-1 flow-by-flow impact are in
**`docs/self-hosted-bus.md`**. The rest of this section is correct on BOTH buses unless noted.

### The BLPOP / LLEN trap

An idle bridge's inbox **always** reads `LLEN = 0` (this is your OWN inbox, so it works on both buses). The bridge is blocked-waiting on `BLPOP inbox 30s` and Redis pops messages back atomically the instant an `LPUSH` lands. You'd only see non-zero depth if (a) every engine slot is full **and** another dispatch is queued, or (b) the bridge has crashed mid-BLPOP. So `LLEN` is not a useful signal for "is parallelism working" or "is my task being processed".

The load-bearing observability surface is the **bridge's own log**:

```
[turn-start] <task-a-uuid>
[turn-start] <task-b-uuid>      ← before any [turn-end] = parallel execution
[turn-end]   <task-a-uuid> ok ...
[reply-sent] in_reply_to=<task-a-uuid>
[turn-end]   <task-b-uuid> ok ...
[reply-sent] in_reply_to=<task-b-uuid>
```

Two `[turn-start]` lines back-to-back before either `[turn-end]` is what `--max-parallel ≥ 2` actually looks like. With `--max-parallel 1` the second `[turn-start]` only appears after the first `[reply-sent]`.

### Single-shot status queries (when the user asks "give me an update")

`agent-dispatch` emits `task-id: <uuid>` on stderr just before its `LPUSH`, so each dispatch's request ID is recoverable without parsing the inbox:

```bash
TASK_ID=$(grep -oE 'task-id: [0-9a-f-]+' /tmp/<task>.err | tail -1 | awk '{print $2}')

# Snapshot status — single HGETALL, ~5 lines back
PYTHONPATH=/<bridge-clone>/src python3 -m agent_redis_bridge.ctl \
  --env-file <path-to-app-env> status --task-id "$TASK_ID"

# Read final result (empty until task completes)
... ctl result --task-id "$TASK_ID"
```

`status` and `result` are single-shot. **Never use `watch`** — it streams the entire event log into context and burns tokens proportional to the activity stream's verbosity. If you want continuous progress, the right answer is to wait for the harness's completion notification on the backgrounded `agent-dispatch`, not to poll.

### Third-party observers must not consume the sender inbox

A session that did not start the dispatch may inspect its durable
`task:<id>:status` and `task:<id>:result` keys, but it must never `BLPOP`, `LPOP`,
or otherwise read destructively from the dispatcher's `FROM_AGENT_ID` inbox. The
reply is consume-once; an observer can steal it and strand the real dispatcher
until timeout. Delivery belongs to the dispatching process, while status/result
are the multi-reader record. The harness-neutral contract and Codex process-wait
example live in
[`docs/runbooks/bridge-dispatch-completion-generic.md`](../../docs/runbooks/bridge-dispatch-completion-generic.md).

For a Codex/generic harness that can yield an active tool call, arm one
persistent watcher that loops internally over bounded `write_stdin` waits,
carrying forward the returned live `session_id`, and immediately yield control.
This is the same completion-notification shape as Claude Code's background Bash:
the chat stays responsive and process exit resumes the orchestrator. Its terminal
branch must call `notify()`; `text()` merely stores background-cell output and
does not wake the model. Short-delay automatic wake is proven, but long-idle
`notify()` delivery has remained queued without starting a turn on this runtime.
Pair the persistent watcher with one coarse watchdog just beyond each bounded
wait window (about 310s for 300s), re-arming only if still running. This guarantees
progression with at most one fallback check per five minutes. A single
five-minute wait is not persistent; repeated model-side waits or Redis polling
are also wrong. Keep the renewal loop inside the yielded watcher. This pattern
and its evidence limits are recorded in the linked runbook.

### Strict reply filtering (if you build a custom monitor)

If for some reason you must inspect the inbox directly (don't, but if you must): filter on `kind == "reply" AND in_reply_to == <YOUR_ID>`. Both codex and gemini bridges emit `kind=notify` envelopes carrying `payload.data.task_id == <YOUR_ID>` for every tool call the remote agent makes — a single task can produce hundreds. A naive `grep "$YOUR_ID"` false-trips on every one. Strict structured filter only.

## The brief-to-file pattern

For a **review** brief, generate it with `scripts/review-brief` rather than hand-writing it — the generator always emits the evidence-first findings contract (verdict + per-finding observed-behaviour / evidence-artifact / mechanism / confidence / alternatives, per `docs/evidence-first-remediation.md`) and refuses to emit a brief with no evidence basis (no `--diff` and no `--spec`). This makes the evidence ask structural instead of something you have to remember:

```sh
scripts/review-brief --title "Phase 3 i5a (quota calc)" \
  --repo /path/to/repo --spec docs/.../i5a-design.md --diff 1c043b3..60e9f1b \
  --oracle "legacy quota_data (publication.php:2248)" \
  --question "Faithful port?" --verify "docker exec … pest …" \
  > docs/superpowers/reviews/2026-05-31-phase3-i5a-review-brief.md
```

For any non-trivial dispatch (anything longer than one sentence), write the task body to a markdown file first, **harness-publish it**, then enqueue with the pre-minted quartet (Slice 1d-iv — free-form positional task strings are gone):

1. Write `docs/superpowers/reviews/YYYY-MM-DD-<engine>-<purpose>-brief.md` with the full instructions, including where the agent should write its report (`docs/superpowers/reviews/YYYY-MM-DD-<engine>-<purpose>-report.md`)
2. `git add` and commit the brief if you want a durable artifact (recommended for reviews — the brief becomes the audit trail of what you asked for)
3. `arb-memory-harness-publish --target-agent-id … --brief <path> > /tmp/<task>.receipt.json`, then `agent-dispatch` / `dispatch-dev` with `--artefact-id` / `--version` / `--receipt` / `--brief` (see canonical recipe above)
4. When the reply lands, read the report file the agent wrote — the dispatch reply payload usually summarizes, but the full output is on disk

Why brief-to-file (then publish+quartet) beats an inline string:

- Avoids shell-quoting pitfalls (see next section)
- Gives the human a durable artifact to stage afterwards
- Lets the brief evolve in git if you need to re-dispatch with corrections
- The agent's reply summary is shorter, so the harness notification doesn't bloat context
- Satisfies the authority seam: only receipt-hashed brief bytes reach the wire

## The `\n` shell-quoting gotcha

Bash double-quotes do **not** interpret `\n` as a newline. They preserve the literal backslash-n characters. Free-form positional task strings are gone (Slice 1d-iv), but the same trap still applies when composing **brief file contents** or commit-message fields with shell escapes:

```bash
# BAD — literal \n characters land in the brief/commit body
printf 'fix: title\n\nbody' > /tmp/brief.md   # if the shell didn't expand \n

# GOOD — $'...' ANSI-C quoting interprets escapes into the brief file
printf $'fix: title\n\nbody line one\nbody line two\n' > /tmp/brief.md

# GOOD — heredoc preserves real newlines in the brief file
cat > /tmp/brief.md <<'EOF'
# Dispatch brief

## Assumptions
(json fence with {"items":[]} — see faba_schema / canonical recipe)

## Instructions
body line one
body line two
EOF

# Then publish + quartet enqueue (not a free-form positional task string):
arb-memory-harness-publish --target-agent-id codex-<proj>-<ws> --brief /tmp/brief.md \
  > /tmp/task.receipt.json
env -u ARB_MEMORY_REDIS_URL agent-dispatch --engine codex --target-id codex-<proj>-<ws> \
  --run-id "$RID" \
  --artefact-id "$(jq -r .artefact_id /tmp/task.receipt.json)" \
  --version "$(jq -r .version /tmp/task.receipt.json)" \
  --receipt /tmp/task.receipt.json \
  --brief /tmp/brief.md
```

Diagnostic: if you suspect a body is full of literal `\n` chars rather than real newlines, run `printf '%s' "$BODY" | od -c | head -3` and look for `\ n` (two separate chars) vs `\n` (the byte `0a`). The first means a caller composed the body with escapes that weren't interpreted; the second means real newlines that something downstream is double-escaping.

## Health check before dispatch

If you haven't dispatched to a particular target in a while, ping it first:

```bash
AGENT_PROJECT=<project> \
/<bridge-clone>/scripts/agent-bridge-ping --engine codex <workspace>
# or
/<bridge-clone>/scripts/agent-bridge-ping --engine grok-acp <workspace>
```

Output shapes:

| Output | Meaning |
|---|---|
| `agent_id=... registry=<iso-ts> heartbeat=alive ttl=<seconds> consumer=alive consumer_ttl=<seconds>` | The registered boot instance owns the identity and its inbox loop is progressing |
| `agent_id=... registry=<iso-ts> heartbeat=dead ttl=<negative>` | Bridge registered itself but the heartbeat stopped — daemon crashed or is paused |
| `agent_id=... heartbeat=alive ... consumer=dead ...` | Process lease exists but the inbox loop is not proving consumption; do not dispatch |
| `agent_id=... heartbeat=alive ttl=<seconds> consumer=legacy` | Healthy pre-token daemon (that build has no consumer key) — dispatchable; restart it onto current code when convenient |
| `agent_id=... heartbeat=owner-mismatch ...` | Registry and bus-global boot lease disagree; do not dispatch |
| `agent_id=... registry=missing` | No registration at all — either the bridge has never run, or you're looking at the wrong bus / wrong agent_id (check `AGENT_PROJECT` and `--workspace`) |

The `AGENT_PROJECT` override is often needed because the helper falls back to whatever the env file says, which may be a stale value from before a project rename while the actual systemd unit registered with `project-c` or your real project name.

## Notify-inbox split (recent feature)

Bridges run with `BRIDGE_NOTIFY_INBOX=0` route every `kind=notify` envelope onto a separate `agent_scratch:agent:<sender>:notify_inbox` list (LTRIM-capped, default 5000). The main `:inbox` becomes reply-only, so dispatcher BLPOP is O(1) regardless of activity-stream volume — no inbox-drain dance needed when running N≥4 parallel dispatches.

Two implications worth knowing:

1. **The decision is made on the bridge that PROCESSES the request**, not the caller. If you dispatch cross-host to a remote bridge that does NOT have `BRIDGE_NOTIFY_INBOX=0`, that remote bridge will still flood your `:inbox`. For shared-bus topologies, every host's bridges need the flag or the protection has holes.
2. **`agent-dispatch` post-`a51c65e` filters by dropping notifies** — it no longer RPUSH-backs them. Before that commit, the dispatcher RPUSH-back'd any `kind != reply` envelope and **livelocked** under modest notify pollution (caught 2026-06-04 with 3,437 stranded envelopes on a managed bus: 3,421 stale notifies, 16 orphan replies, dispatcher hit timeout while shuffling cruft at ~100ms per BLPOP+RPUSH TLS round-trip). The flag is now both a performance mitigation AND a correctness defense; the dispatcher fix is the safety net.

**Diagnosis when the wrapper appears stuck despite `[reply-sent]` in the bridge log:**

```python
# In a Python repl from the bridge clone — single-shot, no churn
from pathlib import Path
from agent_redis_bridge.redis_io import RedisCli, RedisConfig
cfg = RedisConfig.from_env_file(Path('envs/<env-file>'), {})
r = RedisCli(cfg)
inbox = cfg.inbox_key('<FROM-agent-id>')
import json; from collections import Counter
items = r.client.lrange(inbox, 0, -1)
print(f'LLEN={len(items)}  by kind={dict(Counter(json.loads(b).get("kind") for b in items))}')
```

`LLEN > 50` with most being `kind=notify` ⇒ pollution. `DEL` the key (replies persist at `task:<id>:result` so no data lost — recoverable via `ctl result <task-id>`).

If you find yourself manually draining a flooded inbox, source the env file first so the connection params match the active bus (don't hardcode `127.0.0.1:6390`):

```bash
set -a; source <path-to-app-env>; set +a
redis-cli -h "$AGENT_REDIS_HOST" -p "$AGENT_REDIS_PORT" -n "$AGENT_REDIS_DB" \
  ${AGENT_REDIS_TLS:+--tls} ${AGENT_REDIS_USER:+--user "$AGENT_REDIS_USER"} \
  --no-auth-warning \
  DEL agent_scratch:agent:<your-sender-id>:inbox
```

## Managed-bus topology (TLS + auth)

The bridge supports plain Redis on localhost and managed buses (DigitalOcean Valkey, AWS ElastiCache, etc.) with TLS + ACL auth via three optional env vars:

```bash
AGENT_REDIS_TLS=1
AGENT_REDIS_USER=default
AGENT_REDIS_PASSWORD=...
```

The password is exported as `REDISCLI_AUTH` so it doesn't appear in `ps`. When unset, the bridge behaves exactly like a localhost-only setup. The same env-file is read by the Python bridge daemon, by `agent-dispatch`, `agent-bridge-ping`, and `agent-inbox-watcher` — so a single `.env` per worktree is sufficient.

Cross-host orchestration works once every host's bridges point at the same managed bus: a Claude on host A can `agent-dispatch --target-id codex-<project>-<workspace>` and the host-B bridge processes it via the shared registry. This is documented in `src/agent_redis_bridge/README.md` § "Managed Redis/Valkey buses" and `docs/orchestrator-patterns.md` § "Pattern E".

## Watch for persistence-layer backpressure on managed buses

A managed bus multiplies per-Redis-op latency by ~50-100× (TLS handshake + cross-VPC RTT). Any code path that does Redis I/O per token or per high-frequency event will throttle the bridge's stdout-read loop, and that in turn back-pressures codex App Server and collapses the model generation rate. Symptom: bridge prose dispatches take minutes while bridge tool-call dispatches stay fast — same model, same prompt would run in seconds via direct `codex exec`.

This bit hard on 2026-05-13: per-token `model_text` deltas were XADD'ing + HSET'ing the events stream and status key, each via a fresh `redis-cli` subprocess. On local Redis (~1ms RTT) it was 500ms of hidden cost per 500-token response — invisible. After the DO Valkey migration the same code became 50-100 seconds of self-inflicted stall per response. Fixed in a later revision of this bridge: per-token deltas are no longer persisted; an 8s liveness heartbeat preserves "still streaming" diagnostic without re-introducing the cost.

**The architectural rule that survives:** never put network I/O, subprocess spawning, TLS handshakes, or persistence writes inside a token-stream hot path. Especially not synchronously.

**Diagnostic shape if you see this symptom on a different bridge build:**

- Stream rate suspiciously close to "1 event per Redis RTT" (e.g. 1.5 events/sec on a TLS bus = ~700ms per redis-cli call) → almost certainly per-event persistence in the hot path.
- Bridge log `[turn-tool]` events still fast (one xadd burst per command) but `[turn-end]` arrives minutes later → confirms the persistence-layer-only theory.
- Same prompt via `codex exec` is fast → the model isn't the variable; the bridge's wrapping is.

The longer-term architectural fix is to replace the `redis-cli` subprocess-per-call pattern entirely with a persistent `redis-py` connection so every Redis op pays one amortized TLS handshake at startup instead of one per call. Tracked as a follow-up; not in the 9da7761 fix.

## Diagnosing pi-rpc bridges — `process.title` clobbers argv

The pi CLI (`@earendil-works/pi-coding-agent`) runs `process.title = APP_NAME` at startup, which rewrites the OS process table entry. After that runs, **`pgrep -fl "pi --mode rpc"` returns nothing even when pi is actively running.** Two consequences worth knowing:

1. **"pi died silently" is usually wrong.** When a pi-rpc bridge logs `[turn-start]` and then no further events for minutes, the natural check is `pgrep -fl "pi --mode rpc"` to confirm pi is still alive. It returns nothing. You conclude pi crashed and hunt for crash causes. Pi is fine; pgrep is blind.

2. **`pkill -f "pi --mode rpc"` no-ops silently.** Attempts to clean up orphan pi processes between bridge restarts leave them alive. Each "cleanup" between attempts leaves the orphan holding a stale stdin pipe to a defunct bridge daemon. By the third "fresh" bridge launch the process tree is genuinely wedged, with state mismatched between bridge daemon and pi child.

**Correct diagnosis recipe** (find pi by PPID, not argv):

```bash
BRIDGE_PID=$(pgrep -f 'agent_redis_bridge.*pi-rpc' | head -1)
pgrep -P "$BRIDGE_PID"                          # → pi's pid, visible regardless of title
ps -p <pi-pid> -o pid,state,%cpu,rss,etime      # → confirm alive + check thinking-state CPU
lsof -p <pi-pid> | grep PIPE                    # → stdin/stdout/stderr pipes connected
```

**Correct cleanup** (kill by parent or by pid, never by argv pattern):

```bash
pkill -P <bridge-pid>      # kill all children of bridge daemon
# or:
kill <pi-pid>              # by pid once located via PPID
```

The bridge code restart-cycle is also affected: if you `kill <bridge-pid>; pkill -f "pi --mode rpc"; relaunch`, the pkill silently fails and the orphan pi holds onto its inherited file descriptors. On the relaunch the new bridge spawns a new pi child, but the OS still has the orphan + its stale Redis connections + its half-open stdio pipes. Resulting state at the *next* dispatch looks like "bridge hangs forever after `[turn-start]`" which is what triggered this lesson 2026-06-04.

This affects any tool that calls `process.title = ...` at startup. Codex and Gemini's CLIs may exhibit the same pattern in future releases — when in doubt, use PPID-based discovery regardless of engine.

## Common failure shapes and diagnoses

<!-- fragment:failure-shapes begin -->
| Error / Symptom | Likely cause | Fix |
|---|---|---|
| `[bridge-error] sender-rejected ...` | `FROM_AGENT_ID` is not in the target bridge's trusted-sender list | Set `FROM_AGENT_ID` to a value the bridge trusts, or have the operator add your ID |
| `envelope-invalid invalid-branch` | `BRANCH` is empty or `git branch --show-current` returned `""` | Hardcode `BRANCH=dev` or the intended branch in the dispatch invocation |
| `bridge busy with task <uuid>` | All engine-pool slots on the target are occupied | Wait, cancel, or check whether `BRIDGE_MAX_PARALLEL` is set lower than needed |
| Bridge starts but rejects every dispatch | The target bridge may have no sender policies configured | Set `AGENT_TRUSTED_SENDERS` in the env file or pass `--sender-policy` on the CLI |
| Dispatch exits immediately: "pass --run-id ID ... or --adhoc" | agent-dispatch hard-refuses un-labelled dispatches (since 2026-07-01) | Mint a run-id (dispatch-dev auto-defaults one) or pass --adhoc for a throwaway |
| `agent-dispatch` exits 124 | Timeout reached before a matching reply landed | Increase `--timeout` if the task is still running, or inspect bridge logs for a crash |
| Commit body shows literal `\n` characters | Caller composed the body with Bash double quotes | Use `$'...'`, a heredoc, or the brief-to-file pattern |
| `LLEN inbox` reads 0 while a task is running | Normal BLPOP behavior; the bridge consumes atomically | Use task status/result keys or bridge logs, not inbox length |
| `NOPERM No permissions to access a key` on a foreign inbox `LLEN`/`KEYS`/`SCAN` | You are on the **self-hosted bus** (per-identity ACLs); browse + foreign read-back are denied by design | Not a bug. Use the recipient's reply as the consumption signal, `GET`/`TTL` on a known `:status` for presence. See `docs/self-hosted-bus.md` |
| Panel `refused_reconcile` naming a seat whose vote you never saw fail | On the self-hosted bus, a missing **audit-emitter** grant NOPERMs the emit in the seat daemon log, not your cockpit | Grep that seat's daemon log for NOPERM at vote time; check its `ARB_MEMORY_REDIS_URL` user; recover via new run-id + `supersedes:` (`docs/self-hosted-bus.md`) |
| Bridge log shows `[reply-sent]` but dispatcher does not exit | Caller inbox may be polluted with stale `kind=notify` envelopes | Pull bridge code to a dispatcher that drops notifies and set `BRIDGE_NOTIFY_INBOX=0` |
| Dispatch to a Claude seat fails as unknown engine | A raw model id (e.g. `claude-opus-4-...`) was passed as `--engine` — engines are harness names, not model ids | Use `--engine agent-sdk` with `--target-id asdk-<project>-<workspace>-<model>` |
| `Could not connect to Redis ...: Can't assign requested address` mid-run, after the task-id printed | Ephemeral-port exhaustion. Each `agent-dispatch` spawns a fresh `redis-cli` per BLPOP poll; a wide fan-out held open for tens of minutes exhausts local ports | Stagger to 2–3 concurrent dispatchers. **The reply is lost irrecoverably** — the task ran, but its result key is gone before you can read it, so never fan out a benchmark un-staggered |
| Reply gate returns `dirty_uncommitted` listing files the task never touched | The orchestrator edited the seat's workdir while the dispatch was in flight; the gate diffs against the state at task START | Never edit a seat's workdir mid-dispatch. Note this is **silent for tasks that start after the edit** — they baseline the dirt and report `no_changes_clean`, so one contaminating edit fails only the runs already in flight |
| Later dispatches in a queued fan-out exit 124 while earlier ones succeed | Seats are `--max-parallel 1`; queued dispatchers spend their client `--timeout` waiting their turn, not working | Set `--timeout` to at least `queue_depth × turn_timeout`; keep `--turn-timeout` at the review ceiling |
| Seat dies at startup with `ValueError: invalid sender policy: <id>:trusted` | `--sender-policy` pairs are separated by `=`, not `:` | Pass `<id>=trusted`; valid values are `trusted\|human\|reject` (`Bridge.parse_sender_policies`) |
| `ModuleNotFoundError: No module named 'redis'` from `dispatch_authority`, and the seat log shows NOTHING | `agent-dispatch` resolved a system python without the venv, so it died before enqueueing — the seat looks deaf but never received anything | Put `$PWD/.venv/bin` on `PATH` for the dispatch. Note the asymmetry: `arb-memory-harness-publish` needs `ARB_MEMORY_REDIS_URL` **sourced**, the dispatch step needs it **unset** (`env -u`) |
| `arb-memory-harness-publish` → `invalid brief: missing ## Assumptions section`, or `items[N] must be an object` | The brief has no assumptions block, or its items are strings. `scripts/review-brief` does not emit the section at all | Add `## Assumptions` with a JSON fence whose `items` are objects: `{"statement","status":"assumed"\|"demonstrated","vantage"}`; `demonstrated` also needs `artefact_id` + positive int `version` matching the target's vantage (`tools/faba/faba_schema.py::validate_dispatch_brief`) |
| Verdict close returns `refused_reconcile` with `expected exactly 1 dispatch manifest, found 2; run un-auditable` | The roster manifest was emitted twice under one run-id (e.g. re-emitted after seats were replaced). Two rosters means no single answer to "who was on this panel", so no verdict can be proven complete | Emit the manifest **last**, once seat ids are final. To recover: mint a NEW run-id, emit exactly one manifest, re-emit every seat's vote from its **verbatim** fence, close with `supersedes: <refused-run-id>`. The refused run stays in Postgres as the scar — intended |
<!-- fragment:failure-shapes end -->

Additional engine-specific diagnostics (pi-rpc, agy-print):

| Error / Symptom | Likely cause | Fix |
|---|---|---|
| pi-rpc bridge logs `[turn-start]` then no further events for minutes; `pgrep -fl "pi --mode rpc"` shows nothing | Pi sets `process.title = APP_NAME` clobbering argv; pi IS alive, pgrep is just blind to it. Genuine cause is usually pi orphans from earlier `pkill -f "pi --mode rpc"` no-ops leaving stale stdin pipes after bridge restart | Find pi via `pgrep -P <bridge-pid>`; clean up via `pkill -P <bridge-pid>` not by argv pattern. See § "Diagnosing pi-rpc bridges — `process.title` clobbers argv". |
| pi-rpc bridge logs `[turn-start]` then nothing; pi child alive, 0% CPU, no network connections, blocked in `kevent` | **Pi provider not authenticated.** Pi's `~/.pi/agent/auth.json` is a separate auth store from kimi-code's `~/.kimi-code/credentials/` and from the bridge's env vars. When pi is told `--model <provider>/<id>` and `<provider>` is not in `auth.json`, pi falls back on `--api-key (defaults to env vars)`. That env-var fallback resolution sometimes silently hangs on cold-start. Verified 2026-06-04: pi binary had empty auth.json for hours while bridges spawned `--model minimax/...` etc. Symptom is partial — first turn often wedges, restart works. | Run `pi login` interactively to add every provider the bridge uses (`minimax`, `kimi-coding`, etc.) to `~/.pi/agent/auth.json`. Workaround until then: restart the bridge — the second cold-start usually resolves the provider config and round-trips cleanly. If you need protocol-level visibility into what pi sees, switch a *direct-CLI* pi to `--mode json` (pi's structured event log; a debugging surface, NOT the bridge's `--mode rpc` JSON-RPC channel). See § "Pi-rpc event vocabulary" below. |
| grok-acp dispatch dies mid-turn with `Grok ACP stopReason=cancelled` a few sentences in | TWO known causes, same symptom. (a) **Auth decay** (2026-07-09): interactive `grok` re-auth fixes it; kickstart does not. (b) **Invalid permission reply (GROK-1, root-caused 2026-07-10):** pre-fix bridges answered permission asks with a non-ACP `"approved"` outcome, so ANY permission-requiring op (out-of-cwd writes; NOT reads — reads never ask) was treated as rejected and the turn died. FIXED in `engines/grok_acp.py` (spec-correct `selected`+optionId; deny budget; sessionId gate); **V5 live gate PASSED 2026-07-10** on grok-bridge-dev at dev `635c398` (edit-tool out-of-cwd write executed + `end_turn`; control unchanged) — on seats running ≥ `635c398`, out-of-cwd writes WORK on trusted dispatches, so worktree briefs and file deliverables are viable again. | Discriminate first: seat log shows `-32603 failed to request permission` → cause (b); a small cwd-only READ probe passes → (b); the probe also dies → (a), re-auth `grok` interactively. For (b) on PRE-FIX seats (code < `635c398`): keep briefs entirely inside the seat's cwd tracked tree and return deliverables INLINE. On fixed seats (b) should not recur — a cancelled turn there means cause (a) or something new; check the seat's registered_at + fleet-clone SHA before reaching for either story. Pings pass under BOTH causes — never trust a ping as a grok live gate. (ARB Memory `art-d893502c280b1740`.) |
| agy-print dispatch returns `ok=false` with error "exited 0 with empty stdout — likely unauthenticated"; on pre-hardening builds the reply instead claims `ok` with result "agy --print pid N produced no output." | **Agy not authenticated.** In non-TTY `--print` mode an unauthed agy exits 0 with empty stdout instead of erroring — its auth lives in agy's own dotfiles, separate from the bridge env file. Verified 2026-06-11: the seat's first-ever turn ran 10s after standup, before `agy` had been logged in; same invocation succeeded once authed. | Run `agy` interactively once to log in, then re-dispatch — the engine spawns a fresh `agy` per turn, so no bridge restart is needed. Adapter hardening (exit 0 + empty stdout ⇒ `ok=false` with auth-hinting error) ships in `engines/agy_print.py`; if you see the silent-`ok` shape, your bridge build predates it. |

## Pi-rpc event vocabulary (for engine work)

When extending or debugging the pi-rpc engine, this is the event-type catalogue pi emits over stdio. Compiled from a parallel agent's `--mode json` capture 2026-06-04 — current `normalize_pi_event` only handles a subset. The bridge surfaces these as normalized event names; the wire-protocol names below are what the engine sees.

**Wire-protocol vocabulary observed (snake_case + camelCase intermingled — likely version drift):**

- Turn lifecycle: `turn_start`, `turn_end`, `agent_start`, `agent_end`
- Message lifecycle: `message_start`, `message_end`, `message_update`
- Text deltas: `text`, `text_delta` (typically wrapped in `message_update.assistantMessageEvent`)
- Thinking deltas: `thinking`, `thinking_delta` (likewise wrapped)
- Tool calls (snake_case form): `tool_execution_start`, `tool_execution_end`, `tool_execution_update`
- Tool calls (camelCase form): `toolCall`, `toolcall_start`, `toolcall_delta`, `toolcall_end`

The two tool-call shapes likely represent **version drift between pi releases** (the parallel agent saw the camelCase shapes on 0.78; older code paths may emit snake_case). When adding to `normalize_pi_event`, handle both spellings.

**`normalize_pi_event` coverage as of `0eac4b0`:**

| Wire event | Engine emits | Bridge logs as |
|---|---|---|
| `message_update.text_delta` | `model_text` | (heartbeat throttled, no per-event log) |
| `message_update.thinking_delta` | `model_thinking` | (heartbeat throttled — added 2026-06-04 for visibility into reasoning models) |
| `tool_execution_start` | `command_started` | `[turn-tool] <id> command` |
| `tool_execution_end` | `command_finished` | `[turn-tool] <id> command` |

**Not yet handled** (silent-drop, would surface as no observability for these phases): `turn_start`/`turn_end`/`agent_start`/`agent_end`, `message_start`/`message_end`, camelCase `toolCall`/`toolcall_*`, `text` (non-delta), `thinking` (non-delta). For engine work, the camelCase tool-call shapes are the highest-value to add — they're what newer pi versions actually emit.

**Diagnostic recipe** when a pi-rpc bridge is silent: run pi directly with `--mode json` outside the bridge to see what events it's emitting; then check those against `normalize_pi_event`'s recognized list. Anything new = parser gap.

```bash
# Replace <prompt> with a representative task; capture pi's wire events
pi --mode json --no-session --model <provider/model> --prompt "<prompt>" > /tmp/pi-events.json
# Histogram the event types
jq -r '.type // .sessionUpdate // ""' < /tmp/pi-events.json | sort | uniq -c | sort -rn
```

## Multi-model review panel composition (operational rule)

When dispatching a multi-reviewer panel against a branch diff, treat the engines as having distinct roles — not as equal judges. Established 2026-06-04 after a 5-model panel run demonstrated a systematic calibration gap on reasoning models; recalibrated 2026-07-10 after a 6-seat bake-off (3× gpt-5.6 pins + grok-4.5 + GLM-5.2 + bridge-seated Opus 4.8 on one real diff with two latent defects — ARB Memory `art-d893502c280b1740` has the companion grok findings; scorecard in `docs/superpowers/reviews/`).

**Primary verdict quorum** (3 reviewers — vote determines the merge label):

- **codex** — mechanical / code-path reviewer; best at same-file integration regressions (especially when used as a self-reviewer of its own impl). **2026-07-10: within-model variance is LARGE** — two gpt-5.6 pins on the same brief returned a reproduced P1 (5.5KB report) vs zero findings (1.9KB). For high-stakes / concurrent-mechanism reviews, dispatch **two codex seats** (different pins, e.g. `-sol` + `-terra`) and treat them as independent samples, not redundant copies.
- **cold-Opus** (Claude subagent, not bridge) — architectural adversarial auditor; crispest write-ups; lowest tool-call count. A *bridge-seated* Opus (asdk seat) is fine for fairness tests but its fail-closed tool ceiling (no exec, no out-of-repo writes) caps verification depth below the native subagent — prefer native for production panels.
- **agy-print** — alternative reasoning, runs tests, catches execution-level regressions; tolerate the narration overhead

**Adjunct specialist reviewers** (findings count; label trust varies per seat — see calibration below):

- **pi-GLM-5.2** (pi-sdk) — **promoted 2026-07-10**: best epistemic hygiene observed on the fleet (only seat to detect embedded prior-review reports in a brief and refuse to launder their test claims; correctly calibrated an intended-limitation as informational). Findings AND labels both count. (Stale note removed 2026-07-23: "read-only seat" described an earlier deployment. Current launchd pi-sdk seats run pi's default toolset, shell included — GLM and its pi-sdk siblings CAN execute tests and reproduce findings; verified via the qwen38max seat's calibration review running 30 shell commands. Interpret their severity labels as executing-seat labels.)
- **grok-4.5** (grok-acp) — structured static analysis, honest method disclosure; systematically soft labels ("approve with notes" over what executing seats rate P1). Findings count, labels advisory. **Two hard quirks:** dispatches must NEVER write outside their cwd (ACP permission RPC fails with -32603 → turn dies `stopReason=cancelled`, indistinguishable from auth-decay without the seat log) — reviews return INLINE in the reply; and check the seat is on retire-capable code before trusting its independence.
- **kimi-k2.6** (pi-rpc) — accessibility, async UI behaviour, progressive enhancement, screen-reader / live-region semantics, browser interaction details
- **minimax-M3** (pi-rpc) — completeness sweeps, sibling-function parity, test fragility / coupling, meta-review of the review framing itself
- **cline / DeepSeek V4 Flash** (cline-acp, pinned `deepseek/deepseek-v4-flash`) — **calibrated 2026-08-13** across trust-root rotation rounds 4 and 5 (`panel-gaterotate-r4-20260813T134855Z-7b0580`, `panel-gaterotate-r5-20260813T203328Z-938f9d`). **Adversarial-probe specialist: trust it hardest on "what input defeats this validator".** In two consecutive rounds it found the under-block the round's fix had just missed — a ZERO WIDTH SPACE surviving `str.strip()` (r4), then standalone Mn/Me combining marks surviving a Cf/Cc category filter (r5). Both reproduced independently by the orchestrator. It got there by probing OUTSIDE the space the fix had just enumerated, which is the one direction an author's own probe systematically cannot reach. Fastest seat in both rounds (10m, ~15m) while still running the both-directions check and the scoped suite, and it follows brief ordering precisely. **Split its credibility by domain — this is the calibration.** On CODE its findings have held up under independent check every time. On its OWN ENVIRONMENT it has twice given a plausible reason to skip the expensive step that did not survive checking: r4 "full suite unrunnable (dead venv mount)" (the venv was healthy — pytest 9.1.1, redis 8.0.1, 144 packages), and r5 "one-writer-per-worktree, a full-suite run was in flight" (each seat has its own bridge-created worktree, so nothing was contended). Weight its code findings heavily; re-check its environment disclosures before accepting a skipped step. **Reports assert more than they show** — 51KB with two fenced blocks — so its claims need re-running rather than reading, even though they have so far been true. Executing seat; labels have run needs-changes/P2 on findings the orchestrator agreed were real, i.e. neither systematically soft nor strict so far — too few rounds to call. Self-ID is unreliable: the engine hallucinates "sonnet"; read the model from the plist, which the cline engine enforces with a set-then-read-back that refuses to run on a mismatch.
- **devin SWE-1.7** (devin-acp) — **rostered 2026-07-18** after its first live cycle (calibration record: ARB Memory `art-81ef40a78683d2e9`): ACP/protocol-native specialist — only reviewer of seven to catch a test fixture silently starving an ACP `authenticate` handshake step, found COLD via per-test timing measurement. Trust it hardest on ACP/protocol-level findings. Executing seat that escalates labels in the STRICT direction (voted `block` on a test-only P2 nit): findings count, labels advisory — same hard rule as grok/kimi, opposite direction. **Non-certifying on Devin-authored subjects by LINEAGE, not bias** — a fresh Devin session has no authoring context to defend but shares SWE-1.7's model blind spots (same mechanism as author-non-quorum); on non-Devin-authored subjects it needs no discount at all.

**Severity-calibration pattern (2026-07-10, held across the whole panel):** seats that can EXECUTE escalate (block/P1 with repro); static-only seats moderate (approve/P2/none) — *on the same underlying finding*. Findings converge; labels split along the execution line. Trust the convergence of findings; the orchestrator owns severity.

**Brief hygiene:** exclude `docs/superpowers/reviews/` from a review brief's diff range — embedded prior reports anchor every reviewer that doesn't explicitly de-weight them (only GLM did).

### The hard calibration rule

```
Adjunct findings COUNT.
Adjunct verdict labels are ADVISORY ONLY.
```

Pi reasoning models (kimi, minimax) are systematically softer on severity labels — they will return `SHIP_WITH_NITS` while describing a finding the quorum calls `FIX_BEFORE_MERGE`. Trust the finding, not the label. Repeated soft labels do not downgrade a finding. The orchestrator decides severity. These are prose verdict labels; when they feed a vote fence, map them to the canonical stance vocabulary in the vote-fence fragment below.

**Caveat verified 2026-06-04 afternoon (4-way A/B on PA #22):** the soft-label tendency is *harness-driven*, not *model-driven*. Same MiniMax-M3 returns `SHIP_WITH_NITS` via pi-rpc (lean default system prompt) but `FIX_BEFORE_MERGE` via `mini-agent-acp` (75-line opinionated system prompt at `~/.mini-agent/config/system_prompt.md`). Pi's headless default is sparser than mini-agent's, kimi-code's, or gemini's — so when the bridge dispatches a review via pi-rpc, the model gets less ambient "be thorough" pressure. To level the harness, append a reviewer-mode role profile to pi-rpc dispatches via `--append-system-prompt` (pi's native flag); see the *role-profile passthrough* mechanism below. ACP engines (gemini-acp, mini-agent-acp, kimi-code-acp) carry their own opinionated prompts and are NOT affected by this gap.

Concretely:
- Adjunct findings enter the same P0/P1/P2 triage as quorum findings.
- Adjunct verdict labels do NOT participate in the primary merge vote.
- Any unique P0/P1 finding from an adjunct must be reconciled before merge.

### When to run adjuncts

**Run both adjuncts** for high-stakes customer-visible work — chat UI, accessibility, browser rendering, progressive enhancement, form interactions, Playwright / browser test harness changes, customer-visible Blade/JS, live-region / aria patterns.

**Skip or optional** for pure PHP service refactors, migration-only changes, seeders, corpus ingestion commands, CLI-only tools, internal pipeline / queue / job work.

For ambiguous middles (backend touching customer-visible response shape): run minimax for completeness; skip kimi.

**Run devin** when the diff touches ACP/protocol surfaces, engine adapters, or handshake/timing-sensitive test fixtures — its proven lens. Optional elsewhere; on Devin-authored subjects it reviews and contributes findings but cannot certify (lineage rule above).

### Operational shape

1. Fast quorum (codex + opus + agy) starts immediately.
2. Adjuncts run in parallel only when surface warrants.
3. If quorum has blockers, fix blockers first — don't wait on adjuncts to find more.
4. If adjuncts return unique P0/P1 before merge, reconcile them.
5. If adjuncts are late and the change is low-risk, **don't wait** — merge on quorum.
6. For high-stakes customer-visible work, **do wait** for adjuncts before merging.

### Cost baseline (2026-06-04)

Full 5-model panel: under $1 + ~10 min wall-clock. Cost is not the gating factor — *surface area* is. Use the surface-area rule to decide whether to run adjuncts.

### Acting on panel findings — verify a P0 before you remediate it (2026-07-03)

A panel P0 is a **candidate, not a verdict.** The panel is a strong candidate-*surfacer*, not a
verdict-*issuer* — it surfaces real findings and confident-but-*wrong* ones with equal polish. So before
remediating any P0/P1: **identify the single empirical claim the finding rests on, and reality-check *that*
claim against the actual code/behaviour** — by tracing the real thing (emission order, which events reach a
store, what a query actually returns), NOT by re-reasoning about plausibility (which may find a false finding
plausible too). True findings survive the check; false ones dissolve on contact with the real sequence.

Why it matters: a false P0 remediated on faith doesn't just waste a fix — it becomes a **permanent fixture
that manufactures the appearance of a handled edge case that was never real**, which the next reader trusts.
Line-citations and mechanism-tracing are what make a finding *feel* true; they are not what make it true. Do
NOT let this over-swing to "panel P0s are probably wrong" — they are often real (a decorrelated seat catches
what all the authors, including frontier ones, missed). The rule is: **every** P0 gets its hinge-claim
reality-checked before remediation — true ones pass, false ones fail, and you can't tell which without the
check. (Two-sided calibration: ARB Memory `art-613270f63c6d4850` (panel caught a real author-missed P0) +
`art-ea6208432de0e49d` (panel produced a confident false P0 that verification rejected).)

## Per-stage authoring rotation for design / spec / plan (author-non-quorum)

Distinct from the implementation-review panel above: this governs who writes the **first draft** of each
authored artifact (design, spec, plan) and how that changes the panel that reviews it. Full derivation +
the quorum math live in ARB Memory artefact `art-49c566cc076f374a` (*author-of-a-stage becomes that
stage's non-certifying reviewer*); this is the operational subset.

**Why rotate authoring.** Authoring a design/spec is the blank-page, deep **no-tools** reasoning act — the
one regime where a Sonnet warm orchestrator has a real gap (HLE no-tools 43.2 vs Opus 49.8). Offload the
FIRST DRAFT to a stronger reasoner (Opus or Fable) and let the warm orchestrator do the tool-bound
convergent work it's frontier-class at: run the panel, synthesise findings, drive revisions. Also a
credit/cost lever when the orchestrator is Sonnet.

**Ask ONCE, up front — one AskUserQuestion batch, three questions (design / spec / plan).** Before any
authoring begins, ask the human who authors each stage, each with the same three options:

- **1 — Inline (RECOMMENDED DEFAULT)** — the warm orchestrator authors the draft itself. When the
  orchestrator is Sonnet this is the **cost/plan-credit-optimized** choice, and for **tool-bound /
  code-grounded design** (design a feature against an existing codebase — read the code, verify the
  schema) Sonnet is at Opus-parity, so the capability case for a pricier author does not apply.
- **2 — Opus subagent** — spawn a cold Opus `Agent` (native subagent, `model: opus`) to author the draft.
- **3 — Fable subagent** — spawn a Fable `Agent` to author the draft.

**Author-mode nuances (`inline` is a different *kind* of option from the three `-sa` subagent modes).** The
three subagent modes pick *which model* authors, cold, in an isolated worktree, independent of the
orchestrator. `inline` doesn't name a model — it means "author = whoever is orchestrating," so its meaning
and safety depend on who orchestrates:
- **Single-inline-seat constraint:** there is only ONE inline seat (the warm orchestrator). You cannot run
  Sonnet inline while Fable orchestrates — so whenever a *specific* reasoning-orchestrator (Fable/Opus) is
  set, the author is a **subagent by construction** (the inline seat is taken). "Cheap-author default" thus
  means "cheap *subagent* author" in most real runs — which is also the architecturally-correct state
  (author ≠ orchestrator → independence preserved).
- **`inline-cold` (the middle rung):** plain-inline authors *with* the orchestrator's accumulated session
  context — cheapest, but context-*contaminated* (anchored to framing the orchestrator already committed to
  → weakest independence). `inline-cold` = the warm orchestrator authors the stage in a context-**isolated**
  pass (given only the brief + the code, NOT the prior conversation), buying back framing-independence
  without spinning a subagent. Use it for the all-one-model case. Limit: `inline-cold` under a *same-model*
  later verifier still shares the model blind spot — a different-model `-sa` remains the cleanest
  independence.
- **Guard:** plain-inline where the author model == the first-pass verifier is an **author-non-quorum
  violation** — require `inline-cold` (or a decorrelated first-pass reviewer) instead.

**Which author to pick (calibrated 2026-07-03, arb-watch authoring bake-off — Opus vs Sonnet vs Fable on
one brief).** Default to **Inline (1)**. Reach for **Opus/Fable (2/3) only when the initial authoring is
genuinely hard in the way that model tier addresses** — a *no-tools, wide-open, blank-page, ambiguous*
design where the deep-reasoning gap actually bites (few code facts for the panel to check the draft
against). **Do NOT default to the strongest model to author.** The draft is reviewed by the decorrelated
panel (codex/GPT-5.5 traces the code paths; agy/Gemini; pi-GLM; sometimes cold-Opus) **and** the warm
orchestrator's own verification — all of which run regardless of author — so a frontier author's
front-loaded verification is **largely redundant with the panel**. The bake-off showed all three authors
converged on the same architecture; Fable's unique catches (a dead code-path branch, a data-source gating
fact) were exactly what the code-reading panel surfaces anyway, and Fable is frontier-priced — the worst
cost/value point for a code-grounded design. The panel is the verification backstop; **don't pay for
author-perfection.** (Full record: `docs/superpowers/reviews/2026-07-03-arb-watch-history-authoring-bakeoff.md`.)

Do **not** re-ask per stage. On an autonomous run the human sets all three answers once and is then out of
the loop for the whole design→panel→spec→panel→plan→panel sequence — that longer hands-off window is the
point of asking up front.

**The author subagent writes the INITIAL DRAFT ONLY.** It does not synthesise the panel review and it does
not remediate the artifact afterward. Panel dispatch, findings synthesis, and every revision/remediation are
done **inline by the warm orchestrator**. The author subagent is a one-shot "write the first draft of
<stage>" dispatch — nothing after that.

**Dispatching a Fable author subagent — use Anthropic's Fable prompting guide.** Apply the guide at
**`docs/prompting-claude-fable-5.md`** (an in-repo mirror of Anthropic's official *Prompting Claude Fable
5* — kept in the repo deliberately so this does NOT depend on ARB Memory being reachable; upstream source
of truth is ARB Memory artefact `prompting-claude-fable-5`) to the subagent's brief/system prompt: effort
levels (high default, xhigh for hard authoring), brief-steer over enumeration, ground progress claims
against tool results, and the **reasoning-extraction caveat** — do NOT instruct Fable to echo its
reasoning as response text; it triggers a fallback to Opus 4.8. (Skipping this guide is a known
quality/cost regression for Fable seats.)

**Quorum-swap (load-bearing): the author's lineage reviewer goes NON-CERTIFYING for the stage it authored.**
Identical mechanism to codex reviewing its own implementation — the author stays ON the panel as a
non-certifying reviewer contributing findings, but its vote is OUT of the certify quorum for the stage it
wrote. Because **every authoring option is Anthropic lineage** (Inline warm = Claude, Opus-sub, Fable-sub),
**cold-Opus is non-certifying on every design/spec/plan panel** — it still reviews and surfaces findings,
but the certify quorum is the decorrelated **non-Anthropic** seats:

> **Certify quorum for an authored stage = codex (GPT — the anchor certifier, a different weight family from
> Opus/Fable/Sonnet) + pi-GLM + agy-print (Gemini-family).** cold-Opus = admissible, non-certifying.

(Use `agy-print`, not the deprecated `gemini-acp`.) codex is the anchor because it's decorrelated from
whichever Anthropic seat authored — it guarantees ≥1 clean certifier even if Fable turns out Opus-derived.
codex still does NOT certify its own *implementation* (that's the original author-non-quorum case).

**Input hygiene for the non-certifying author-reviewer.** Extract the author's knowledge as
**intent-disclosure** ("here's the framing, here's what I discarded, here's where the bodies are buried"),
NOT a quality verdict — an author defends its own work. Keep it **blind-to-identity** so a confident "this
is sound" from the author doesn't anchor the panel toward approval. Frontier-authored drafts (Opus/Fable)
are persuasive and feel done — apply a **harder** adversarial pass on the converged parts.

The panel wiring itself (independent phase → verify, audit-emit, run-id discipline) is unchanged — see
"Multi-model review panel composition" and "Auditing a review/design panel" above.

**Workflow C (bounded-context rounds) extends this rotation** — `docs/pipeline-operating-manual.md`
§ "Workflow C", adopted 2026-07-19. Under C both the author AND the synthesiser run as native
FABA-style subagents (bridge seats stay for the review panel), and the load-bearing addition is the
**return-channel rule**: a subagent returns ONLY `{artefact_id, version, change summary}` — the
body goes to ARB Memory via the harness-publish gate, never through the Task result, so the warm
cockpit stays at pointer scale. If a subagent's reply contains the document, the workflow has
failed regardless of how good the document is. Reference: FABA ADR `art-81438f2f5a5c4955` (v13+),
explainer `art-96faf77da9149e80`.

## Role-profile passthrough

Pi defaults to a lean system prompt — fine for neutral RPC use, soft for review work. A bridge seat can carry a role profile for any engine:

```bash
export BRIDGE_ROLE_PROFILE_FILE=/path/to/AgentRedisBridge/roles/reviewer.md
# launch the bridge as normal
```

Pi engines read the file through their native role-profile path (`--append-system-prompt` / `appendSystemPrompt`). Codex, gemini-acp, mini-agent-acp, kimi-code-acp, grok-acp, cursor-acp, and agy-print receive the same content as a first-turn `<system_guidance>` prompt wrapper. The bridge skips wrapping for pi engines, so pi seats are not double-injected. The role profile augments each engine's default — it doesn't replace it.

Available role files in the bridge repo:

| File | Use |
|---|---|
| `roles/reviewer.md` | Adversarial code reviewer with verdict-calibration framing; appropriate for any review-panel dispatch |

To add a new role profile: drop a markdown file in `roles/`, point `BRIDGE_ROLE_PROFILE_FILE` at it, restart the bridge. The file content is appended verbatim — keep it under ~2000 words to avoid bloating every first-turn prompt.

## What goes in this skill vs in personal memory

- **In this skill** (ships with the bridge code, applies on any host): protocol shapes, helper-script invocation patterns, gotchas the bridge's own design implies (BLPOP/LLEN, notify routing, shell-quoting), env-var names and their meanings.
- **In personal memory** (specific to each operator's environment): which Redis/Valkey bus this host's bridges point at, what agent IDs are currently registered, where the env file lives, which app worktrees this Claude has permission to touch, rollback file paths.

If you find yourself wanting to mention `db-valkey-...digitalocean.com` or a specific path like `/srv/projects/project-c-dev/.env`, that belongs in memory, not in this skill.

## Auditing a review/design panel (bus audit wiring)

When running a panel you want on the audit trail, `ARB_MEMORY_REDIS_URL` must point at
the configured audit bus. Do not hardcode a logical database number in portable guidance.

`agent-dispatch` hard-refuses any dispatch (except `--check`) that has neither `--run-id` nor
`--adhoc` — a panel dispatched without a minted `run_id` used to silently show a raw task-id
GUID in arb-watch's Run column instead of a label, and drop all audit/vote evidence for the
round (memory `dispatch-run-id-discipline`, 2026-07-01). `--audit-panel` and the steps below all
assume you've already minted `$RID`.

Two prerequisites are load-bearing:

- `ARB_MEMORY_REDIS_URL` must be set in the **bridge daemon's env file** (`bridge.py` `resolve_audit_redis`), not merely in the dispatcher's shell. If it is absent, the daemon fail-softs to a silent no-vote; the gap appears only when the verdict reconciler refuses.
- The verdict close is **reconcile-gated against Postgres** (roster + one-verdict-per-actor claim). That check — and the Postgres DSN it needs — live inside the arb-prod `audit-close-consumer`; the orchestrator reaches them **over the bus** (step 5) with only `ARB_MEMORY_REDIS_URL` and never holds the DSN or needs SSH. A `refused_reconcile` (exit 4) whose `gaps` contains `audit-consumer-incomplete` means the audit stream hadn't drained into Postgres yet (consumer lag) — not necessarily that a stance was laundered. The bus close blocks for the consumer's own reconcile, so this is usually self-resolving on a retry; only the break-glass CLI path needs the stream drained before you run it.

1. **Mint one `run_id` per panel, before any dispatch:** `RID=panel-<slug>-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 3)`. Reuse it verbatim everywhere; a re-panel mints a NEW id (reference the prior via `supersedes:` in the verdict payload). If this round spawns any cold-Opus reviewer (native `Agent`/Task tool), embed `[ARB_RUN:"$RID" ARB_SEAT:cold-opus-<slug> ARB_ORCH:<your-seat-id>]` as the literal first line of its prompt at the same time — no CLI gate enforces this for native spawns, so a forgotten marker silently degrades to a raw GUID in arb-watch (memory `cold-opus-marker-discipline`).
2. **Emit the roster manifest FIRST (it must be seq 1):**
   `arb-audit-emit --run-id "$RID" --kind dispatch --payload "{\"kind\":\"dispatch\",\"roster\":[\"seat:codex-bridge-dev-example\",\"seat:cold-opus-a\",\"seat:cold-opus-b\"],\"task\":\"...\",\"branch\":\"dev\"}"`
   Manifest roster entries MUST equal the `seat:<target-id>` string `agent-dispatch` emits (`seat:$TO`); short names like `seat:codex` will refuse.
3. **Dispatch bridge seats with `--audit-panel --run-id "$RID"`** — the vote is auto-emitted from the seat's stance fence (fail-soft). Instruct every seat (bridge and in-session) to END its reply with the ```` ```vote ```` block.
<!-- fragment:vote-fence begin -->
Canonical audit stance vocabulary:

```text
abstain | approve | block | needs-changes | timed-out
```

`parse_stance` rejects any other stance. Incident note: on 2026-07-06, a reviewer emitted
`approve-with-nits`; the non-canonical stance cost a vote re-fire.

The fence carries an optional `severity` (`none | P2 | P1 | P0`, the seat's highest surviving
finding). Include it — it feeds triage. Since 2026-07-08 an **omitted** `severity` defaults to
`none` instead of dropping the whole vote (the pre-fix behaviour cost re-fires when a seat ended
with just `{"stance":"approve"}`); a **present-but-invalid** severity (e.g. `P3`) is still
rejected. So: always emit `severity`, but a forgetful seat no longer loses its vote.

Verdict-to-stance mapping from `scripts/review-brief`:

```text
APPROVE→`approve`, APPROVE WITH NOTES→`needs-changes`, REQUEST CHANGES→`block`,
cannot-assess→`abstain`.
```
<!-- fragment:vote-fence end -->
4. **For in-session (cold-Opus) seats, emit each vote yourself** from the subagent's final message:
   `printf '%s' "<reply text>" | scripts/arb-panel-vote --run-id "$RID" --actor seat:cold-opus-a`
5. **Close with the verdict over the bus (default path) — needs only `ARB_MEMORY_REDIS_URL`, NOT a DSN; use the honest orchestrator identity:**
   ```
   printf '%s' '{"kind":"verdict","roster":["seat:codex-bridge-dev-example",...],"stances":{"seat:codex-bridge-dev-example":"approve",...},"decision":"approve","rationale":"..."}' \
     | scripts/arb-audit-close-request --run-id "$RID" --payload-file - --requested-by "$ORCHESTRATOR_ID"
   ```
   The privileged, reconcile-gated emit — the part that owns the Postgres DSN — runs inside the
   arb-prod `audit-close-consumer`; the orchestrator publishes a close-*request* on the bus and
   blocks for the result, so it **never needs `ARB_MEMORY_DSN`** (a missing local DSN is intended
   containment, not a gap — do not go hunting for one, and do not bypass the consumer with a local
   DSN or SSH). Pass `--requested-by "$ORCHESTRATOR_ID"` so the close carries honest requester
   attribution. The script prints `{"outcome": ..., "gaps": [...]}` and exits with the consumer's code:
   **0** `emitted` (done) · **4** `refused_reconcile` (a seat is missing/unrostered/duplicated or a
   stance is laundered — fix the gap named in `gaps`) · **5** `different_verdict` (a different verdict
   already closed this run) · **6** `orphaned` (a prior-crash close-claim exists with no verdict — DEL
   the claim key and re-run) · **7** no close-consumer response (consumer down / not deployed — fall
   back to break-glass). **Any nonzero exit means NO verdict was emitted; do NOT announce it.**
   **Break-glass ONLY (bus or close-consumer down):** ssh to the droplet and run the DSN-coupled
   close *inside* the audit container, which holds the DSN by construction:
   `docker compose exec -T audit-close-consumer python -m arb_memory audit-close --run-id "$RID" --payload-file <path>`.
   Same reconcile-gate + exit-code contract (0/1/2/4/5/6); use it only when the bus path can't reach.
6. **Residual (named):** the reconcile-gate only guards verdicts that actually go through a close path (bus `arb-audit-close-request` or the break-glass CLI). Before announcing any verdict in prose, run the done-criterion query and cite the `run_id` + the successful close `outcome=emitted` — a verdict announced without a passing audit-close is un-audited.
7. **Votes are append-only, one per actor, NO supersede (incident 2026-07-11):** the
   reconciler hard-refuses a round with duplicate votes for any actor — a wrong vote
   cannot be corrected in place. Prevention: the seat's OWN fence is authoritative over
   any orchestrator prose-mapping, and bridge seats sometimes put the fence in their
   report FILE rather than the inline reply — search both before constructing anything.
   Recovery when a correction is unavoidable: mint a NEW run-id (`...-r1b-...`), re-emit
   the manifest + EVERY seat's vote from its verbatim fence, and close with a verdict
   whose payload carries `supersedes: <old-run-id>`. The refused run stays in PG as the
   visible scar — that refusal is the anti-stance-laundering guard working.
8. **Absent seats: record `timed-out` with evidence, then RE-FIRE against the artifact
   AS IT NOW STANDS (incident 2026-07-11):** a re-fired reviewer reads the FOLDED
   artifact — surface its peers never saw, authored by the folder — and is structurally
   the only reviewer of that fold (GLM's re-fire caught the fold's own P1). Point the
   re-fire at the fold changelog ("verify the folds; hunt anything NEW"), and preempt
   the original failure cause in the prompt (e.g. the exact path a runaway search was
   hunting). Skip the re-fire only when the artifact is unchanged since the absence AND
   the seat's lens is redundant; own-harness seats re-fire by default.

## ARB Secrets: sealed peer↔peer secret / env transfer

When one Claude peer needs to hand a **credential or a whole `.env`** to another peer over the bus,
use **ARB Secrets** (`src/arb_secrets/`, merged dev `c2a95ea`) — do NOT paste the secret into a
coordination message (it lands in the recipient's inbox, the split-watcher files, and the
human-eyes tee in cleartext). ARB Secrets is the peer↔peer, *no-human-fulfiller* shape (the opposite
of ARB Messages' operator door): every body is NaCl `Box`-sealed to the recipient's key and travels
as a TTL'd blob + a pointer, so the bus and every retained store see only routing metadata, never the
value. It moves creds a peer **already holds** — it does not mint (new privilege still comes from you
or ARB Messages).

**Setup (once per peer):** each peer holds an X25519 keypair (`~/.arb-secrets/privkey.b64`, mode-600,
auto-minted) and publishes its pubkey to `agent_scratch:secrets:pubkey:<agent_id>`. Trust is TOFU:
the first time you resolve a peer you pin its fingerprint (`~/.arb-secrets/known_peers.b64`); a later
mismatch surfaces, never auto-updates. **The pin is auto-set on first sight with no human gate** — so
the operator must vouch a new peer's fingerprint out-of-band when standing it up (the one trust
assumption).

**Send (either direction):**
```python
from arb_secrets.peer import Peer
me = Peer(redis, my_agent_id, "~/.arb-secrets/privkey.b64", "~/.arb-secrets/known_peers.b64",
          allowed_requesters={"claude-project-a-lead"})   # who may REQUEST from me (direction D)
me.push_secret(peer_id, open(".env","rb").read(), ttl=120)   # A: hand it over
req_id = me.request_secret(holder_id, "prod .env", ttl=120)  # D: ask (the 'what' is sealed too)
```

**Recipient pattern — the load-bearing discipline (spec §9, NOT enforced by code):** ARB Secrets
protects the value on the wire and at rest in the bus. Once you decrypt it, protection is *yours* to
keep — and for an env file that matters more than a token because its natural home is a file on disk.
So on claim: **write the decrypted bytes to a mode-600 file via an atomic exclusive create, and echo
only a fingerprint/label to the transcript — never `cat` the contents** (the transcript is teed to
the human-eyes plane; a `print(secret)` there defeats the whole seal).

```python
import os, hashlib
for inc in me.claim_incoming():           # drains inbox, GETDEL-claims blobs, Box-verifies + decrypts
    if inc.event == "secret_drop" and inc.secret:
        dest = os.path.expanduser("~/.config/myapp/.env")
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # atomic — no perms window
        with os.fdopen(fd, "wb") as f:
            f.write(inc.secret)
        print(f"received env from {inc.sender}: {len(inc.secret)} bytes, "
              f"sha256={hashlib.sha256(inc.secret).hexdigest()[:12]} -> {dest} (0600)")  # label only
```

`claim_incoming()` returns typed rejections for anything that fails (`WrongHolder`, `WrongAnswer`,
`Unauthorized`, `Expired`, `Replay`, `AuthFail`) — a bad envelope from any bus participant is rejected
per-item, never crashes the batch or loses co-batched secrets. Delivery is consume-once (`GETDEL`): a
recipient crash after claim means re-request, not silent loss. Proven end-to-end (`tools/arb-secrets-e2e.py`):
a full multi-line `.env` round-trips byte-identical with zero plaintext in the inbox.

## See also

- `src/agent_redis_bridge/README.md` in the bridge repo — installation, env-file shape, systemd units
- `skills/README.md` — the skill roster, install symlink, role profiles, and which layer reads which instruction file
- `docs/claude-peer-coordination.md` — lightweight Claude↔Claude over the bus, **no engine** (shape 2 plumbing)
- `docs/orchestrating-claude-peers.md` — shape-2 workflow for N ≥ 3 Claude peers with a coordination lead
- `docs/orchestrator-patterns.md` — full patterns for parallel dispatch, zero-poll monitoring, dual-review, gotcha briefing
- `docs/bridge-parallelism.md` — engine-pool design and `--max-parallel` flag
- `SPEC.md` — protocol envelope format and Redis key naming

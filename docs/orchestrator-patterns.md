# Orchestrator patterns: parallel dispatch + zero-poll monitoring

Field-tested patterns for driving the bridge from an orchestrator (Claude Code, other CLI tools). Both patterns came out of a single project-i orchestration sprint on 2026-05-12/13 that dispatched ~20 Codex tasks across 6 subprojects in one evening.

Two things, neither obvious on first read of the bridge:

1. **You can run several Codex tasks at once on the same agent_id** — set `BRIDGE_MAX_PARALLEL=N` and give each task its own git worktree. The bridge spawns N engine processes; workspace isolation is on you. (Applies to any live bridge engine — the `gemini-acp` engine is deprecated as of 2026-07-03 and `agent-dispatch` now rejects `--engine gemini-acp`; use `agy-print` or another canonical-quorum seat instead.)
2. **You don't need to poll for task completion** — `agent-dispatch` is itself the wait. Run it bare in a backgrounded shell and the natural process exit IS the completion signal.

Each is independently useful. Combined, you can fire N tasks in parallel and get N exit notifications, no wakeup loops, no token burn.

## Bridge operations quick reference

For day-to-day dispatch, prefer the canonical recipe in
`skills/using-agent-bridge/SKILL.md`; this section keeps only the compact protocol
and recovery reference an orchestrator needs while operating the bridge.

### Request envelope

Ordinary `request` / `worktree_run` envelopes are built only by
`dispatch_authority.publish_and_enqueue` (Slice 1d-iv). Callers supply a
pre-minted `{artefact_id, version}` plus target-bound receipt and original brief
bytes; the authority selects the wire (Stage 1d-iv: **legacy** for every target,
because no seat advertises `brief_hydrate=v1` yet). Dual-accept on the receive
side also parses the exact ref object `{"artefact_id","version"}`; a ref on a
parse-only seat fails `brief_hydration_unavailable` before prompt construction.

```json
{
  "id": "uuid",
  "from": "claude-project-c-dev",
  "branch": "feat/fire-and-select-hero",
  "to": "codex-project-c-dev",
  "kind": "request",
  "sent_at": "2026-04-26T19:00:00+01:00",
  "payload": {
    "task": "<authority-selected: legacy brief text, or future ref object>"
  }
}
```

Canonical caller recipe: `docs/fragments/dispatch-recipe.md` (publish via
`arb-memory-harness-publish`, then `dispatch-dev --artefact-id --version --receipt --brief`).

### Progress consumption

Read compact status first:

```bash
redis-cli -p 6390 -n 12 HGETALL agent_scratch:task:<task-id>:status
```

Tail verbose events only when needed:

```bash
redis-cli -p 6390 -n 12 XREAD BLOCK 0 STREAMS agent_scratch:task:<task-id>:events 0
```

Read the final structured result:

```bash
redis-cli -p 6390 -n 12 GET agent_scratch:task:<task-id>:result | jq
```

Claude inbox messages are decision points only. Do not push raw token streams into
agent inboxes.

### Control messages

```json
{"kind":"steer","payload":{"task_id":"uuid","message":"Focus only on failing tests."}}
{"kind":"cancel","payload":{"task_id":"uuid"}}
```

### Failure matrix

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

### Helpers

```bash
scripts/agent-bridge-ping dev
# Ordinary dispatch is pre-minted (Slice 1d-iv). See docs/fragments/dispatch-recipe.md.
# scripts/agent-dispatch ... --artefact-id ... --version ... --receipt ... --brief ...
```

`scripts/codex-dispatch` is a deprecation wrapper around `agent-dispatch`. New
scripts and docs should call `agent-dispatch` directly.

## Pattern A — Parallel dispatch on one agent_id via git worktrees

### Why you'd want this

A typical orchestration produces ~4 implementation tasks that are read-mostly independent (different subdirectories, different concerns). Running them sequentially is dead time when one of them is doing the slow part (LLM reasoning, file I/O, test runs). Parallel reduces wall-clock from ~2h sequential to ~30–40min concurrent.

### Why worktrees, not branches

Each Codex App Server process operates with **one `cwd` and one git index**. Two tasks dispatched against the same `cwd` would step on each other's `git checkout`, staging area, test runs, etc. — even if they're working on different branches.

Git worktrees are the cleanest answer: each task gets its own physical checkout, its own branch, its own working tree. The same agent_id can serve all of them because the bridge passes the worktree path into each dispatch's task text and Codex `cd`s into it.

### Setup

```bash
# Enable parallelism on the bridge (default is 1 — preserves prior behaviour)
# In your systemd unit's override.conf:
[Service]
Environment=BRIDGE_MAX_PARALLEL=4

# Then daemon-reload + restart:
systemctl --user daemon-reload
systemctl --user restart codex-redis-bridge@dev
```

Each parallel slot spawns a fresh `codex app-server` child process (previously also `gemini --acp`, since deprecated) — ~400MB each. Sensible cap is 2–4; the engine pool lazily creates engines on demand and recycles them after release.

### Worktree-per-subproject pattern

Pre-create one worktree per task before dispatching, all branched from the same base:

```bash
cd /path/to/your/project
RID=worktree-batch-$(date -u +%Y%m%dT%H%M%SZ)
git worktree add -b feat/work-A   .claude/worktrees/work-A   feat/base-branch
git worktree add -b feat/work-B   .claude/worktrees/work-B   feat/base-branch
git worktree add -b feat/work-C   .claude/worktrees/work-C   feat/base-branch
git worktree add -b feat/work-D   .claude/worktrees/work-D   feat/base-branch
```

Each worktree is a full checkout. They share the same `.git/objects` (no disk waste) but have independent indices and `HEAD`s.

### Dispatch

Fire all four backgrounded — each one is its own `agent-dispatch` foreground process inside a `Bash(run_in_background=true)` (per Pattern B below):

```bash
# Worker A — publish then quartet enqueue (Slice 1d-iv; free-form task strings removed)
scripts/arb-memory-harness-publish \
  --target-agent-id codex-myproject-dev \
  --brief docs/briefs/A.md \
  > /tmp/work-A.receipt.json
FROM_AGENT_ID=claude-myproject-dev \
BRANCH=feat/work-A \
AGENT_ENV_FILE=/path/to/.env \
env -u ARB_MEMORY_REDIS_URL \
agent-dispatch --target-id codex-myproject-dev --timeout 5400 \
  --run-id "$RID" \
  --artefact-id "$(jq -r .artefact_id /tmp/work-A.receipt.json)" \
  --version "$(jq -r .version /tmp/work-A.receipt.json)" \
  --receipt /tmp/work-A.receipt.json \
  --brief docs/briefs/A.md \
  > /tmp/work-A.out 2> /tmp/work-A.err

# Repeat for B, C, D — each in its own backgrounded Bash tool call so they run
# concurrently from the orchestrator's perspective. Put worktree paths in the brief.
```

The bridge accepts all four (pool capacity = 4), spawns four engine processes, each operates in its own worktree. No file conflicts because the worktrees are physically separate.

### Verifying parallelism is working

```bash
# Watch the bridge log:
journalctl --user -u codex-redis-bridge@dev --since "5 min ago" -f | \
  grep -E "turn-start|turn-end|busy"
```

You should see N `[turn-start]` lines before any `[turn-end]` — that's the proof of concurrent execution. A `bridge busy with task <uuid>` reply means you exceeded the pool cap.

### When to NOT use this

- **Single-file changes across one branch.** No reason to bother.
- **Tightly coupled subprojects** where one depends on another's commits. Run sequentially so the later one starts from a stable state.
- **Heavy memory pressure.** Each engine slot is real RAM. Don't set `BRIDGE_MAX_PARALLEL=8` on a 4GB box.

## Pattern B — Zero-poll task-completion monitoring

### The problem

`agent-dispatch` writes the eventual reply to stdout and exits 0 (or 1/124 on failure). When orchestrating from a CLI agent, you want to be **notified the instant the reply lands** without polling. The naïve approach is to poll Redis status keys or schedule wake-ups, which burns tokens reading the same in-progress state over and over.

### The right pattern

`agent-dispatch` is a synchronous BLPOP loop on the caller's inbox. **It is its own wait.** Run it in foreground inside a backgrounded shell, and the orchestrator's harness will surface a "task completed" notification the moment the dispatcher exits.

**Claude Code example (using `Bash(run_in_background=true)`):**

```bash
# After harness-publish of docs/briefs/X.md → /tmp/dispatch-X.receipt.json:
env -u ARB_MEMORY_REDIS_URL \
agent-dispatch --target-id codex-myproject-dev --timeout 5400 \
  --run-id "dispatch-X-$(date -u +%Y%m%dT%H%M%SZ)" \
  --artefact-id "$(jq -r .artefact_id /tmp/dispatch-X.receipt.json)" \
  --version "$(jq -r .version /tmp/dispatch-X.receipt.json)" \
  --receipt /tmp/dispatch-X.receipt.json \
  --brief docs/briefs/X.md \
  > /tmp/dispatch-X.reply.json 2> /tmp/dispatch-X.err
```

Run with `run_in_background=true`. **No `&`. No wrapper sleep. No `tail -f`.** The shell process IS the dispatcher; its exit IS the completion event.

When the reply lands:
- `agent-dispatch` writes the JSON reply payload to stdout (`/tmp/dispatch-X.reply.json`)
- The shell exits 0/1 depending on `ok`
- The orchestrator receives a task-completion notification
- The orchestrator reads `/tmp/dispatch-X.reply.json` for the result

### The anti-pattern (and why)

<!-- doc-recipes: allow-bare -->
```bash
# DON'T DO THIS
agent-dispatch ... > /tmp/reply.json 2> /tmp/err &       # <-- the & detaches the dispatcher
DISPATCH_PID=$!
sleep 3 && cat /tmp/err                                  # <-- this is the only foreground work
```

Run with `run_in_background=true`. The bash task exits in ~3s (sleep + cat finishes). The actual `agent-dispatch` continues in the background, **detached from the harness**. The orchestrator gets a completion notification for the wrapper, NOT for the dispatcher. Hours later, the reply lands in `/tmp/reply.json` with no signal at all — the orchestrator either has to poll (expensive) or schedule periodic wake-ups (also expensive, and your context cache misses on each wake-up).

This bit a real orchestrator and forced reliance on `ScheduleWakeup`-style polling for hours before being caught. Don't repeat it.

### How to confirm dispatch was accepted

If you need an early sanity check that the dispatch was actually queued (vs rejected by sender policy or busy bridge), `agent-dispatch` writes a stable `task-id: <uuid>` line to stderr **immediately** after the LPUSH:

```bash
# Spin a separate foreground Bash AFTER the dispatcher started:
cat /tmp/dispatch-X.err
# Output:
# task-id: 7b49297c-b59d-4b98-8b7f-3194351c4ce2
```

Capture the UUID once; you can correlate it later if you need to inspect bridge logs or per-task status keys. **Don't watch the inbox itself** — the bridge floods `kind=notify` events for every tool call (`command_started`, `command_finished`, etc.), and a naive grep will false-trigger constantly.

### When you DO need polling

Two legitimate cases:

1. **The orchestrator's own process can't sleep through a long task** (token budget, conversation context window). For genuine multi-hour waits, scheduling a wake-up at ~80% of the expected duration is fine. Don't burn cache on short waits; pre-2025 wake-ups within the 5-minute prompt-cache TTL beat re-firing.
2. **You explicitly want to interleave checks with other work.** Then a single-shot `redis-cli HGETALL` on the per-task status key is cheap and unobtrusive — never `watch`, never `tail -f` from the orchestrator.

For everything else, the natural completion notification is the right tool.

## Pattern C — Dual-review with cold reviewers

This isn't bridge-specific but it's the pattern the recurring orchestration sprint converged on, and it relies heavily on Pattern A (parallel) + Pattern B (zero-poll).

After an implementation lands:

1. **Codex self-review** — dispatch via the bridge with the same agent_id. Codex re-reads the brief, its own diff, and writes a structured review (BLOCKERS / MAJOR / MINOR / NITS / VERDICT) to a file.
2. **Cold reviewer subagent (e.g. Opus)** — spawn outside the bridge with NO prior conversation context. Brief it with the same brief + branch + worktree path. It reads the diff fresh.

The two reviewers catch different things. Codex catches issues that emerge from re-reading its own work with a critical eye (data-flow bugs, test fixtures that don't exercise the production path). The cold reviewer catches issues from never having seen the implementer's reasoning (architecture odors, scope creep, security gaps).

Where they converge → real bug. Where they diverge → either a missed insight from one or context the other shouldn't have had. Either way you learn something.

**Consult the defect-class vocabulary (retrieval — the loop's second half).** Every review MUST check its findings against the named defect-classes in Pattern F and the evidence-first findings contract that `scripts/review-brief` emits (see `docs/evidence-first-remediation.md`). A finding that matches a known class is TAGGED as a recurrence — which is how a class earns promotion and how a recurrence of `cheap-fake-hidden-by-wrong-axis` (etc.) gets *recognized* by its canonical name rather than re-discovered as new. For any change touching a gate, a recompute, an authenticity check, or a contamination boundary, apply the evidence-first bars explicitly: representative production input on the load-bearing axis with deny-proven fixes; immutable or independent ground truth, not caller-supplied mutable state; and adversarial controls that traverse the vulnerable channel and move the verdict. A corpus written but never consulted is a diary; the review step is where it becomes a loop. This is a STICKY field, not a remember-to: for gate/security changes the cold-reviewer brief carries it as a required line (below) and the verdict can't be returned with it blank.

Useful brief shape for cold reviewers:

```
You have NO prior context. Read the brief, then the actual diff. Check specifically for:

- [domain gotchas the team has been bitten by before — list each]
- [recurring testing patterns to verify — e.g. "concrete exception classes in toThrow"]
- [security gates that must hold]

REQUIRED (gate / recompute / authenticity / contamination changes) — §6a bars, state which apply + the evidence:
  - cheap-fake-hidden-by-wrong-axis: does each gate/security test hit the load-bearing axis on real production
    output (not an unfaithful fixture)? is the fix deny-proven (fails on the old impl)?
  - orchestrator-supplied-state-forgeable: does verification anchor to immutable/independent ground truth?
  - control-proves-only-its-path: does each adversarial control MOVE the verdict on the vulnerable channel
    (clean twin passes, mutated blocks)?
  (Do not return a verdict with this section blank for such changes.)

Output format: BLOCKERS / MAJOR / MINOR / NITS / OVERALL VERDICT (APPROVE / NEEDS FIXES / BLOCK).
Audit stances are the canonical `abstain|approve|block|needs-changes|timed-out`; map prose verdicts per `docs/fragments/vote-fence.md` before emitting.
End with one of those three verdicts. Under 800 words.
```

If the reviewer mostly agrees with the implementer it's cheap. If they catch a real bug you've avoided a regression. Either way the verdict is decision-grade.

## Pattern D — Recurring-gotcha briefing

After ~3 subprojects you start to see classes of bug recur. Capture them as memory notes and explicitly brief reviewers on them. The orchestration sprint that produced this doc bit the same `access_token` URL-leak gotcha **three times** before the third reviewer was briefed to grep for it specifically.

Examples worth briefing reviewers on (in this codebase):

- Token leak in error messages (`ConnectionException::getMessage()` includes the request URL; scrub both URL form and JSON form, both response body AND exception message)
- `ShouldBeUnique` + `release($delay)` deadlock (the unique lock blocks the re-dispatch for `$uniqueFor` seconds — pick a different retry mechanism)
- Pest 4 `toThrow(\Throwable)` falls back to message-substring matching (use concrete exception classes)
- Migration data dependencies (don't create a column AND read from it in the same migration; existing rows can't have data in the just-created column)
- `cascadeOnDelete` on history-bearing FKs (a single hard delete vaporizes the entire audit trail; prefer `nullOnDelete` + softDeletes)

The point is not the list itself but the habit: every time a reviewer catches a class of bug, capture it for the next reviewer brief.

**Graduation (the structural half).** Briefing relies on memory. Once a gotcha has
recurred ~3× AND is expressible as a pattern, *graduate* it from "briefed" to
"enforced": add it to a `.gotchas.json` registry and let **`gotcha-lint`**
(`scripts/gotcha-lint`, see `docs/gotcha-lint.md`) fail CI when the pattern
reappears — caught by construction, not by a reviewer remembering. Briefing-stage
(not-yet-graduated, or un-greppable) gotchas are auto-injected into review briefs
via `review-brief --gotchas .gotchas.json`. `gotcha-lint --check-graduation` nags
when a briefing gotcha has hit the threshold but hasn't been promoted, so even the
promotion decision isn't left to memory.

## Pattern E — Cross-host orchestration via a managed bus

By default each host runs its own local Redis on 127.0.0.1:6390 — fine for solo work on one host, but the bus is invisible to peers. To run a dev/staging/prod fleet where an orchestrator on **any** host can dispatch to **any** other host's engines, point every bridge at one managed bus (DigitalOcean Valkey, AWS ElastiCache, Upstash, etc.) by setting `AGENT_REDIS_HOST/PORT/TLS/USER/PASSWORD` in each host's `.env`.

Once that's done, the existing patterns work unchanged — but `--target-id` now resolves across hosts:

```bash
# Run from your dev workstation
FROM_AGENT_ID=claude-myapp-dev /srv/projects/example-bridge/scripts/agent-dispatch \
  --target-id codex-myapp-staging --timeout 1800 \
  --run-id "staging-audit-$(date -u +%Y%m%dT%H%M%SZ)" \
  "Run the migration audit on the staging worktree."
```

The staging-host bridge BLPOPs the request from the shared registry, runs codex against the staging worktree, and the reply lands back on `claude-myapp-dev:inbox` on the same shared bus.

**Why this matters:** worktree-per-subproject (Pattern A) gives parallelism on one host; managed-bus topology gives parallelism across hosts. The dispatcher protocol is identical — no orchestrator change required.

**Operational notes:**
- Keep `BRIDGE_NOTIFY_INBOX=0` on every bridge that participates. The notify-flood caveat (see Caveats below) compounds across hosts.
- Each host is still its own engine pool — `BRIDGE_MAX_PARALLEL=4` on the dev host's `codex-myapp-dev` is independent of the staging host's `codex-myapp-staging` cap.
- `agent_scratch:registry:<agent-id>` heartbeats let `agent-bridge-ping` from any host confirm a peer is alive before dispatching.
- Cost: every BLPOP cycle is one round-trip to the managed bus. For idle bridges this is one BLPOP-per-30s of egress — negligible. For an active fleet running parallel dispatches at the engine-pool cap, egress is bounded by the message rate, not by the BLPOP cadence.

## Pattern F — Verification discipline for gate-touching / security-property changes

When the change *is* a verification mechanism (a gate, a recompute, an authenticity check, a contamination
boundary), "build passes + tests green" is NOT the proof. These disciplines emerged from the
diagnose-live-panel build (2026-06-19); each ends in a **candidate-rule** (the actionable output) and a
**recurrence-status** (one instance = observation; recurred-under-varied-conditions = promotion candidate).
The **named classes are the index** — a review consults them by name so a recurrence is *recognized* as a
recurrence, not re-discovered as new.

**Named defect-classes (the review vocabulary):**

- **`control-proves-only-its-path`** — a passing adversarial control proves the logic moves on the channel
  it *targets*, not that *every* input channel is anchored. *Observable:* the swap-traceback control moved
  the verdict via the `window` channel and passed; the *content* channel (read from `repo_root`) was never
  swapped, so its un-anchoring survived a green control. *Recurrence:* 2× this build (swap→content;
  forge→bus-provenance). *Candidate-rule:* an adversarial control must be shown to traverse the **vulnerable
  path** (enumerate every input channel; one control per channel that MOVES the verdict), not merely a
  tested path.
- **`orchestrator-supplied-state-forgeable`** — verification anchored to caller-supplied **mutable** state is
  forgeable by construction; a self-consistent caller passes. *Observable:* the gate recompute read content
  from `repo_root` (caller path) and `bus_records` from `phase_input` (caller list) — both full-review P0s.
  *Recurrence:* root of BOTH P0s. *Candidate-rule:* gate-touching verification must trace its data source to
  **immutable / independent ground truth** (git blob at the committed SHA; an independent ledger) — never
  caller-supplied mutable state. Where no independent source exists yet, **name the limit, do not fake the
  guarantee** (`honest-limit-named > fake-guarantee-shipped`).
- **`cheap-fake-hidden-by-wrong-axis`** — a test certifies the easy/negative axis while silent on the
  load-bearing/positive axis. *Observable:* the node-id test asserted reject-garbage (passed) but never
  accept-the-real-distribution; the confidence dogfood tested strong-vs-weak, not weak-*additions*.
  *Recurrence:* 3× (node-id regex; count-normalized confidence; plain noisy-OR within-category).
  *Candidate-rule:* every validator test must include the held-out **positive/load-bearing case**, and the
  fix must be **deny-proven** — shown to FAIL against the old/wrong implementation (else it was broadened on
  faith).

**Supporting disciplines (warm-seat, every build):**
- **Verify by execution, not reading** — inject-revert, run the stub, run the gate, run the escape: the
  read-based "looks real" verdict missed real holes twice; the execution-based one caught them.
- **Base-SHA attribution** — "this failure is pre-existing / not mine" is a *claim*; check it against the
  base SHA. (It converted a "4 pre-existing" into "3 pre-existing + 1 flaky" — a flaky test poisons
  attribution and is its own debt.)
- **Batched build with a verify-gate between** — bank each layer (verified from git) before the next stacks
  on it, so a failure is the new layer's, not an unverified foundation's leaking upward.

**Retrieval + promotion (the half that closes the loop):**
- **Consult these named classes at the review step.** A review that doesn't read the vocabulary
  re-discovers the same class as new every time; reading it lets a `cheap-fake-hidden-by-wrong-axis` recurrence get *tagged* as a
  recurrence (which strengthens the promotion case).
- **Promotion is deliberate + panel-verified, earned by recurrence.** Capture here is cheap and continuous;
  a class is promoted to a **standing check in bridge-protocol** (so the gate-change workflow *runs* it) only
  on a deliberate consolidation pass, when it has recurred under varied conditions — branch-only, panel-
  verified, like any ARB change. Do NOT promote a one-off into a standing rule (over-fit). Current promotion
  candidates (recurred this build): `orchestrator-supplied-state-forgeable` and `control-proves-only-its-path`
  → a bridge-protocol gate-change check: *"a gate-touching change must trace its verification's data source
  to immutable ground truth, and prove each adversarial control traverses the vulnerable channel."*

## Pattern G — Gate-first auto-validation (opt-in)

Use this pattern for non-trivial or high-blast-radius builds where an executable acceptance contract pays
for its authoring and review cost. Skip it for trivial edits: verification effort must be calibrated to
blast radius. The green gate is executing-seat evidence, never a substitute for the review panel.

1. **Pick an independent validator.** The orchestrator selects a VALIDATOR seat whose model lineage differs
   from the builder's. Apply author-non-quorum logic to the executable gate: its author cannot certify it.
   The validator works read-only in a worktree at the task base and writes only the dictated `gate.py`.
2. **Pin and prove the baseline before spending build tokens.** The orchestrator runs
   `scripts/run-gate --gate <gate.py> --project <worktree> --repin`, records the appended sha256, then runs
   `scripts/run-gate --gate <gate.py> --project <worktree> --baseline` without re-pinning. Each
   `CHECK[id]: class=delta|invariant` is judged independently:
   delta must FAIL, invariant must PASS, and any `baseline-exempt=<reason>` is loud. A `RED-INVALID` or a
   green/vacuous baseline stops the run until the validator fixes the gate; a declared exemption is evidence
   to scrutinise, not silent permission to accept a weak check.
3. **Bind the builder to the pinned artifact.** The implementation brief includes the gate path, pinned
   digest, and loop contract: *the gate is immutable to you; done means `run-gate` exits 0*. The builder does
   not invoke the gate as an unrestricted script and cannot re-pin it.
4. **Keep correction transport verbatim and bounded.** Between rounds the ORCHESTRATOR runs `run-gate` and
   sends its `FAIL[...]` lines back unchanged as the correction dispatch. Default to three rounds, then
   validator triage; hard-cap at five rounds and halt loudly. These counts are parameters of the run, not
   runner policy.
5. **Permit one attributed repair.** If triage proves the gate defective, the validator may repair it once
   per run. The orchestrator uses `--repin`, whose sidecar appends rather than overwrites, and puts both old
   and new digests in the review evidence. A repair must not weaken a legitimate requirement.
6. **Attach execution evidence to review.** Add the final gate stdout and `--json` summary to the panel brief
   as executing-seat evidence. The panel still runs. Green anchors severity and reproduction; it does not
   issue the verdict.

`run-gate` executes `uv run <gate>` from a temporary directory with a four-variable environment allowlist
(`PATH`, `HOME`, `TMPDIR`, `LANG`), optional explicit `--pass-env NAME`, resource and process-group timeout
limits, best-effort Linux `unshare -n`, and before/after git dirt fingerprints. macOS cannot provide an
`unshare` network namespace, so the runner emits that containment gap loudly instead of claiming denial.
Digest history lives at `<gate>.sha256`; ordinary runs refuse a missing or changed pin. Exit codes are
0 green/valid baseline, 1 normal red, 3 digest refusal, 4 invalid baseline, 5 gate/runner error, and 6 timeout
(argparse reserves 2 for usage errors).

This is itself a gate-touching mechanism, so its changes inherit the stricter review bars in **Pattern F**
and [`evidence-first-remediation.md`](evidence-first-remediation.md): immutable ground truth, adversarial
controls on the vulnerable channel, representative positive cases, and deny-proven fixes. The external
technique and decision provenance are recorded in [`learn-candidates.md`](learn-candidates.md), Category 6.

## Putting it together

A typical orchestration loop that uses all three patterns:

```
1. Set up N worktrees, one per subproject               (Pattern A)
2. Dispatch N implementation tasks in parallel          (Pattern A + B)
3. Wait for the N completion notifications              (Pattern B)
4. After each implementation, dispatch dual review:
   - Codex self-review through the bridge               (Pattern A + B + C)
   - Cold reviewer subagent outside the bridge          (Pattern C)
5. Brief each reviewer on accumulated gotchas           (Pattern D)
6. Aggregate findings, dispatch fix briefs              (Pattern A + B again)
7. Merge approved branches sequentially                 (NOT parallel — merges aren't worktree-isolated)
```

At each "wait" step the orchestrator is not actually waiting on its own clock — it's standing by for the harness's natural completion events. Token burn is roughly linear in the number of dispatches, not in their duration.

## Caveats

- **Pool size is per agent_id.** `BRIDGE_MAX_PARALLEL=4` means 4 concurrent turns on `codex-myproject-dev`, not 4 across all agents. If you also run a `codex-myproject-staging` bridge, it has its own cap.
- **Restarting the bridge kills in-flight engines.** Don't restart while a task is in flight unless you're prepared to lose its work.
- **Workspace isolation isn't enforced by the bridge.** Two tasks dispatched against the same worktree path will fight. The bridge has no way to know — it's up to the orchestrator to give each task its own checkout.
- **Replies are not guaranteed in dispatch order.** If you fire A, B, C in that order and they take different durations, you might get C's reply before A's. Match by `task-id` (the UUID written to stderr at LPUSH time), not by order.
- **The dispatcher's `--timeout` is wall-clock from LPUSH to reply.** Long-running tasks need long timeouts. Default 1800s is fine for review tasks; implementation tasks often want 5400–7200s.
- **Shared-test-database contention across worktrees.** Each parallel worktree shares one git checkout of dependencies but you almost certainly have **one test database** behind your container/runtime. When worktree A runs `migrate:fresh` (or any test framework's database-rebuild step — Laravel's `RefreshDatabase`, Django's `--keepdb=False`, Rails' `db:test:prepare`) it nukes the schema for whatever worktrees B/C/D are mid-test. Symptom: focused per-file test runs pass cleanly in each worktree, but combined multi-file reruns fail with schema-missing errors that look like implementation bugs but aren't. This bit a 4-task parallel orchestration: each subproject's focused Pest verification passed; each subproject's broader combined verification got stomped on by sibling worktrees mid-run. Three mitigations:
  - **Accept focused-only verification.** Have each implementer run only the directly-touched test files, not the broader slice. Cheaper but lower confidence.
  - **Serialize the broader verification step.** Implementations run in parallel; a final "full slice" verification runs sequentially across worktrees afterward, after one consolidated branch is built.
  - **Per-worktree test database.** Override `DB_DATABASE` (or equivalent) per `docker exec` so each worktree has its own DB. Adds setup overhead and per-worktree migration time, but gives each parallel run full isolation. Worth it for sustained parallel orchestration.

  Where this last mitigation lives is co-location-sensitive. **If the dev DB is local docker, spinning per-worktree test DBs on a managed cluster (DO, ElastiCache, RDS) is ≈10× slower than local — RefreshDatabase pays one network RTT per migration statement, hundreds per class.** The cheap path is per-worktree DBs on the same local docker cluster the dev DB uses. **If the dev DB itself moves onto managed infra**, that economics flips: the test cluster sits one VPC hop from the dev cluster, the orchestrator runs locally but only ships small request envelopes through the bridge (cheap), and per-worktree test DBs on managed infra become the natural shape — full isolation, no extra latency vs the dev DB it's modelling. So the rollout sequence is: keep test DBs local until the dev DB cutover; flip to managed test DBs at the same time as (or after) the dev DB cutover. The `bin/test` profile-switch pattern (`TEST_DB=do|local`) lets both shapes coexist during the transition.
- **Notify-flood caveat for N≥4 concurrent dispatches** — *mitigated, opt-in.* By default, the bridge mirrors every tool call from Codex (or any other live engine) as a `kind=notify` envelope on the caller's inbox. With 4 concurrent dispatches each producing tool calls, the inbox can accumulate 3000–4000 notifies before the implementations finish. Each dispatcher BLPOPs the inbox and re-RPUSHes non-matching envelopes, so finding its own reply is O(inbox-depth) per cycle and reply-landing latency grows with N. **Set `BRIDGE_NOTIFY_INBOX=0` (or `--notify-inbox 0`) on the bridge to route notifies to a separate `:notify_inbox` list (LTRIM-capped, default 5000 entries).** The main `:inbox` stays reply-only and dispatcher BLPOP becomes O(1) regardless of activity-stream volume. Notifies remain available via the new list AND the existing per-task events stream (`task:{id}:events`). Strongly recommended for any orchestrator that runs parallel dispatches.

## See also

- `docs/bridge-parallelism.md` — the engine pool design, `--max-parallel` flag, control envelope semantics.
- `README.md` § "Run Modes" — how `--target-id` + `FROM_AGENT_ID` overrides interact.
- `README.md` § "Sender Policy" — the trust mechanism that gates dispatch acceptance.

## Panel audit runbook (ARB Observability Slice 3)

A panel run becomes an auditable decision record via three emitters joined on one `run_id`:

1. **Preflight manifest (orchestrator, seq=1).** BEFORE dispatching any seat, declare the roster:
   `arb-audit-emit --kind dispatch --payload '{"roster":["seat:<id1>","seat:<id2>",...]}'`.
   Roster entries MUST be the bridges' **registered agent-ids** (what each bridge self-derives — usually the
   `--target-id` you dispatch to, e.g. `seat:codex-bridge-dev-example`). For `--role` panels, use the role-suffixed
   ids. The manifest takes `seq=1` because it runs before any vote and `audit_events` carries only
   manifest+votes (bridge lifecycle lives in eval, not here).
2. **Dispatch with `--audit-panel`.** Each per-seat `agent-dispatch --audit-panel --run-id <run>` stamps
   `payload.audit_vote_expected=true`. On task-finish the **bridge** (not the wrapper) transcribes the seat's
   explicit fenced ```vote block into a `vote` audit row (`actor="seat:<agent_id>"`); a turn-timeout emits a
   synthesized `timed-out` vote; an unparseable/missing stance emits NO vote (intentional — see below).
3. **Verdict (orchestrator request, privileged reconcile gate).** After replies land,
   pipe the verdict payload to
   `scripts/arb-audit-close-request --run-id <run> --payload-file - --requested-by <honest-orchestrator-id>`.
   The bus consumer calls `reconcile()` and **REFUSES** unless every rostered seat has
   exactly one vote, no unrostered votes exist, and the proposed stances match the
   committed vote rows. Only `{"outcome":"emitted"}` is a closed panel; the
   orchestrator needs bus access, not a Postgres DSN or SSH.

**A missing vote fails loud, by design.** A seat that never ran (never popped, bridge down) or replied
without a parseable fenced stance leaves a `reconcile` gap ("seat … never voted") and blocks the verdict.
Resolve it explicitly — re-dispatch, or record a stance with `arb-panel-vote --run-id <run> --actor seat:<id>`
(the manual recovery CLI). Never paper over a never-ran seat.

The bridge needs `ARB_MEMORY_REDIS_URL` for the configured audit bus in its env to emit
votes; it resolves this from the bridge `.env` as a fallback. Vote emission is fail-soft
(a down audit bus never breaks the turn); a dropped vote surfaces as a fail-loud
`reconcile` gap, never a silent accept.

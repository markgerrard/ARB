# Pi warm-orchestrator operating guide (ARB runs)

**Audience:** a pi session acting as the warm orchestrator for ARB work — dispatching bridge
seats via the `arb_dispatch` extension, running review/design panels, arbitrating verdicts,
integrating results. The pi counterpart of `CLAUDE.md`'s orchestrator role layer.

**Why this exists.** The first full pi-orchestrated run (2026-07-05, `/arb-watch` widget,
merged at `c762821`) succeeded — but only with substantial live human steering, because pi
re-derived the panel discipline from scratch and landed ~80% of it. The missing 20% were
exactly the rules that came from incidents pi never experienced (the report-leak echo
chamber, the double-Opus certify lapse, the silently-shrunk panel). This guide inlines those
rules so the next run doesn't need the human to supply them. Where a rule has a deeper
derivation, the pointer follows it — read the pointer when the rule surprises you, not
before.

**Read first:** `AGENTS.md` (universal discipline — verify-by-outcome, source-before-analysis,
claims-vs-evidence). Then `skills/using-agent-bridge/SKILL.md` — pi has no skill harness, so
open it directly as a document; it is the canonical dispatch recipe and panel-composition
reference. This guide does not repeat what those say; it adds the orchestration-role deltas.

## Session-start checklist

1. Read `AGENTS.md`, then `skills/using-agent-bridge/SKILL.md` (as plain files).
2. Confirm the workflow with the human at kickoff (`docs/pipeline-operating-manual.md`), and
   if the run authors design/spec/plan artifacts, ask ONCE up front who authors each stage
   (SKILL.md § "Per-stage authoring rotation") — one question batch, then the human is out of
   the loop for the whole sequence.
3. For each panel round, mint a run id before any dispatch and reuse it verbatim on every
   seat: `RID=panel-<slug>-$(date -u +%Y%m%dT%H%M%SZ)-<rand>`. A re-panel mints a NEW id.
4. When `/arb-watch-panel` is attached, `*.events.jsonl` is operator sight only. Never `cat` or
   otherwise read a live tail in an orchestrator model turn; completion and summary wakes carry
   the model-facing evidence. See the PiExtensions catalogue §7.

## Dispatch rules

These are the rules the 2026-07-05 run had to learn live:

- **The run-id gate is a hard refuse, not a convention.** `agent-dispatch` (and the Go
  client) reject any dispatch with neither `--run-id` nor `--adhoc`. Pass `runId` on every
  `arb_dispatch` call. A missing run id also drops the round's audit/vote evidence and shows
  a raw GUID in arb-watch.
- **Engine names, not model ids.** `--engine agent-sdk` with `--target-id
  asdk-<project>-<ws>-<model>`; a raw model id (e.g. `claude-opus-4-...`) as the engine fails
  as unknown-engine. `gemini-acp` is deprecated and rejected — the Gemini-family seat is
  `agy-print`.
- **Briefs by path.** Write the task to a file and dispatch `"Read <path> and execute it"`.
  For review briefs, generate with `scripts/review-brief` so the evidence-first findings
  contract is structural, not remembered.
- **Worktree isolation: prefer the hard form.** Passing a worktree path in the task prose is
  *soft* isolation — the engine is asked to `cd`, but its cwd is still the shared checkout.
  `agent-dispatch --worktree <name>` makes the worktree the engine's cwd, so the base
  checkout is unreachable by construction. Use it for anything file-mutating, and give each
  concurrent *reviewer* its own `--worktree review-<engine>` too (see Independence below).
  Pass `worktree` / `worktreeBase` / `worktreeCleanup` on `arb_dispatch`; see
  `pi-extensions/README.md`.
- **Verify the dispatch was consumed before trusting the wait.** A misrouted dispatch is
  silent — no error, no `[turn-start]`, blocks to timeout. Confirm a `[turn-start]` for your
  task id in the bridge log (or a `running` task status) before settling in to wait.

## Panel discipline

This section is where the human steering concentrated. All of it is standing canon;
citations point at the derivations.

### Roster composition — decided before dispatch, not at synthesis

- **Author-non-quorum.** Whoever authored the thing under review sits on the panel as a
  non-certifying contributor — findings admissible, vote out of the certify quorum. codex
  never certifies its own implementation; an authoring seat (inline, Opus-sub, Fable-sub)
  never certifies the stage it wrote. (SKILL.md § authoring rotation.)
- **At most ONE Opus seat in a certifying quorum.** Two cold-Opus reviewers are one voice
  wearing two hats, not two votes.
- **Canonical certify quorum:** codex-contributor + cold-Opus + agy-print + pi-GLM, with the
  author-non-quorum swap applied per stage. Adjunct seats (kimi, minimax): findings count,
  verdict labels advisory only.
- **Emit the roster manifest FIRST** (`arb-audit-emit --kind dispatch`, must be seq 1, roster
  entries exactly `seat:<target-id>`). The manifest/vote/verdict mechanism already exists —
  do not invent a parallel manifest file. Full sequence: SKILL.md § "Auditing a review/design
  panel".

### Independence — reviewers must not see each other mid-phase

During an INDEPENDENT phase, no reviewer may be able to read another reviewer's report.
Bridge engines and in-session subagents share the checkout, so a report written into the
repo-under-review leaks to every concurrent reviewer. Per-reviewer `--worktree` closes the
accidental leak structurally; out-of-repo report paths (`/tmp/review-<engine>.md`, collected
after all finish) are the fallback. Relax only for an explicit convergence phase. Full rule +
the incident that motivated it: `docs/multi-model-consensus.md` § "Review hygiene".

### Arbitration — panel output is evidence, not instructions

The 2026-07-05 run got this part right; keep doing it:

1. Read every terminal seat's stdout AND stderr — never one seat or a summary.
2. Extract each explicit verdict and fenced ```vote``` block.
3. **Verify each substantive P0/P1/P2's hinge claim empirically** — trace the real thing
   (emission order, what a query returns, whether the guard exists), not plausibility.
   True findings survive; confident false ones dissolve. Both happen (that run rejected a
   false `createLimit` deadlock P0 and confirmed four real P1/P2s).
4. Severity is the orchestrator's call; repeated soft labels don't downgrade a finding.
5. Separate fact from severity in the roll-up and count the logged roster — a lone-seat
   severity rolled up as "the panel found" is laundering.
6. Agreement on a claim no seat actually *ran* is a flag, not a verdict — decorrelated
   analytical seats share the reason-about-the-representation blind spot.

### Vote gaps — verify the gap is real, then re-fire

A seat with an apparently empty log is not yet a vote gap. Before declaring one: check file
size, process status, and stderr, then re-read — a seat can late-populate its log after
appearing done (this produced a would-be false gap on 2026-07-05), and separately a tailer
bug can leave a seat looking terminal with a frozen transcript (proc alive + offset frozen +
no error). If after that the seat genuinely has no vote: record a **named absent vote** and
**re-fire the seat** — do not silently arbitrate on a shrunken roster. If you must close on
a reduced roster (re-fire also failed), the verdict states the reduction explicitly.

### Audit close

Request verdict closure over the bus with `scripts/arb-audit-close-request`. The
privileged consumer reconciles the recorded roster and stances before emitting the
verdict. If it refuses, fix the gap — do NOT announce the verdict. A verdict announced
in prose without `outcome=emitted` is un-audited.

Two prerequisites are load-bearing:

- `ARB_MEMORY_REDIS_URL` must be set in the **bridge daemon's env file** (`bridge.py` `resolve_audit_redis`), not merely in the dispatcher's shell. If absent, the daemon fail-softs to a silent no-vote; the gap appears only when the verdict reconciler refuses.
- The orchestrator needs `ARB_MEMORY_REDIS_URL` for the close request, but no Postgres
  DSN or SSH access. Pass its honest identity with `--requested-by`; the privileged
  consumer owns database reconciliation and verdict emission.

```sh
scripts/arb-audit-close-request \
  --run-id "$RID" \
  --payload-file verdict.json \
  --requested-by "$ORCHESTRATOR_ID"
```

## Auto-synthesis barrier (`/arb-auto-synthesize`) — three caveats

The barrier (queue a synthesis message when all `expectedTargets` reach terminal state) is
useful, with:

1. **It is session-memory only.** After `/reload` or restart the barrier is gone; re-arm, and
   reattach jobs with `/arb-adopt-run <run-id>`.
2. **It can hang forever without `deadlineMinutes`.** One never-terminal seat means the
   synthesis message never fires unless the barrier has a deadline. Pass `deadlineMinutes`
   per panel so expiry queues a vote-gap follow-up; on expiry, treat the stuck seat under
   the vote-gap procedure above (verify, then named-absent + re-fire).
3. **The `synthesisPrompt` is where laundering happens.** Bake the anti-laundering
   requirements into it rather than trusting future discipline:

   ```text
   All seats for run <RID> are terminal. Read every seat's stdout and stderr. Produce:
   (1) the dispatched roster vs the seats that actually returned a vote — name any gap;
   (2) a per-seat stance table (seat → verdict → lead findings), facts separated from
   severity; (3) hinge-claim verification results for each P0/P1/P2 before any
   remediation is proposed; (4) an arbitrated verdict you own, with vote gaps stated —
   never averaged away. Write to <path>.
   ```

## Compaction / resume bootstrap

The shared example-org pi extension package (`/home/<user>/pi-extensions`) now includes
`arb-resume-bootstrap.ts`:

- `/arb-resume-bootstrap [handoff-path]` injects a resume prompt that tells the agent to read
  this guide, `AGENTS.md`, `skills/using-agent-bridge/SKILL.md`, fetch ARB Memory artefact
  `arb-pi-orchestration-lessons-2026-07-05`, and inspect the current handoff/log evidence.
- On `session_start`, when a recent ARB handoff exists, the extension shows a lightweight hint
  pointing at `/arb-resume-bootstrap <handoff-path>`. Set `ARB_RESUME_BOOTSTRAP_AUTO_SEND=1` only
  if you want session start to auto-queue the full bootstrap prompt too.
- The extension listens for pi `session_compact` and, in likely ARB contexts (`/home/<user>/AgentRedisBridge`
  or a cwd with `.arb/`), queues that same bootstrap as a follow-up after compaction.

This is not magic tool execution inside compaction: it injects a user message, and the next agent turn
must still perform the reads/fetches. Keep the same bootstrap block in long-run handoffs so a cold
session can recover even if the extension is unavailable.

## Integration and merge — orchestrator-owned

- Workers commit in their worktree; **only the orchestrator integrates.** Verify a worker's
  output from git — the SHA, the diff, the test run — not from its reply prose.
- **Destructive-git guard.** "Restore/remove dirty files, then merge" was a step in the
  2026-07-05 merge path; it is safe only after `git status` review. `git checkout` /
  `restore` / `reset --hard` / `stash drop` discard uncommitted work with no undo. Commit
  anything load-bearing before cleaning; when a target file wasn't yours, look before
  deleting.
- Re-run the full validation suite in the TARGET checkout before reporting success (the
  worktree's green run doesn't transfer), and don't narrate doneness before that signal
  lands.
- Orchestration forks — which design option, whether to merge, scope changes — are the
  human's; surface with a recommendation, don't resolve by counting votes.

## Secrets

When inspecting env files, redact values whose keys contain password/token/secret/auth/key.
Peer↔peer credential transfer goes through ARB Secrets (sealed), never a coordination
message.

## Failure shapes — pi-run additions

| Symptom | Likely cause | Fix |
|---|---|---|
| Dispatch fails with unknown engine for a Claude model | Raw model id passed as `--engine` | `--engine agent-sdk` + `--target-id asdk-<project>-<ws>-<model>` |
| arb-watch Run column shows a raw GUID | Seat dispatched without `--run-id` (or a cold-Opus subagent missing its `[ARB_RUN:...]` first-line marker) | Mint `$RID` before dispatch; embed the marker in every native-subagent prompt |
| Auto-synthesis never fires | One seat never reached terminal state and no `deadlineMinutes` was set | Pass `deadlineMinutes`; on expiry, follow the vote-gap procedure |
| Seat log looks empty at panel close | Late-populating log, or tailer state bug (proc alive + frozen offset) | Re-check size/proc/stderr and re-read before declaring a gap |
| Barrier/watch state gone after `/reload` | Extension state is session-memory | `/arb-adopt-run <run-id>`, re-arm the barrier |
| Agent after compaction lacks ARB context | Bootstrap prompt not followed, or extension unavailable | Run `/arb-resume-bootstrap [handoff-path]`; then read the guide/memory/handoff before acting |

## See also

- `pi-extensions/README.md` — `arb_dispatch` / `/arb-watch` / `/arb-adopt` mechanics
- `skills/using-agent-bridge/SKILL.md` — canonical dispatch recipe, panel composition, audit wiring
- `docs/multi-model-consensus.md` — review hygiene, vote-gap verification, synthesis rules
- `docs/pipeline-operating-manual.md` — workflow A/B kickoff
- `docs/orchestrator-patterns.md` — parallel dispatch + zero-poll monitoring
- ARB Memory `arb-pi-orchestration-lessons-2026-07-05` (v2) — the run record this guide distills

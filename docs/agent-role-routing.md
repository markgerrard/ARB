# Agent role routing

Which engine to dispatch for which job, and why. This is the default routing the
orchestrator follows; it is evidence-based (see [Evidence basis](#evidence-basis)),
not preference. Deviate when a specific task's risk profile argues for it, but start here.

Related: [orchestrator-patterns.md](orchestrator-patterns.md) (how to dispatch in
parallel) · [worktree-isolated-dispatch.md](worktree-isolated-dispatch.md) (isolation +
the completion gate) · [multi-model-consensus.md](multi-model-consensus.md) and
[quorum-decision-taxonomy.md](quorum-decision-taxonomy.md) (review panels).

## The table

| Job | Default | Engine id |
|-----|---------|-----------|
| Reviewed-plan execution (steps spelled out) | **Composer** | `cursor-acp` / composer-2.5 |
| Verbatim code/plan application | **Composer** | `cursor-acp` / composer-2.5 |
| Scaffolding — migrations, models, tests | **Composer** | `cursor-acp` / composer-2.5 |
| Mechanical fix-loop (apply review findings) | **Composer** | `cursor-acp` / composer-2.5 |
| Off-spec design implementation | **Codex or Composer** (by risk) | `codex` / gpt-5.5 |
| Conceptual fix-loop (findings need judgement) | **Codex** | `codex` / gpt-5.5 |
| Defensive-engineering review | **Codex** | `codex` / gpt-5.5 |
| Architecture / business-rule review | **Cold Opus** | `opus` (fresh context) |
| Independent dissent | **Agy** | `agy` (Antigravity) |

**Quorum vs adjunct (for review jobs):** the canonical certify quorum is
codex-contributor + cold-Opus + agy-print + pi-GLM. The author of the
stage/implementation contributes findings but does not certify it; at most one Opus seat
certifies in a quorum; and when the author is Anthropic-lineage, cold-Opus becomes
non-certifying and the quorum is the non-Anthropic seats. kimi-k2.6 / minimax-M3 are
**adjunct specialist reviewers** — their findings count, but their verdict labels are
advisory only (they run systematically softer on severity). Run adjuncts for chat-UI /
accessibility / JS / Playwright work; skip them for pure PHP refactors, migrations, and
CLI tools.

### Read-only judge seats (M3, GLM-5.2) run on the **pi-sdk** engine — never agent-sdk

The persistent read-only judge seats — minimax-M3 and GLM-5.2 — run under the **`pi-sdk`** engine,
driving their vendor through pi's own model registry (`--model minimax/MiniMax-M3`, `--model
zai/glm-5.2`) with a read-only tool surface (`read,grep,find,ls`) and the
`roles/judgment-oracle.md` profile. **Do not route GLM (or these seats) through the `agent-sdk`
engine.** agent-sdk sends the full Claude Code system prompt + every tool schema to the model's
Anthropic-compatible endpoint; against z.ai's `/api/anthropic` that large request stalls past the
dispatch timeout (steep time-to-first-token vs input size). pi-sdk uses z.ai's *Coding-Plan*
endpoint (`/api/coding/paas/v4`) with pi's lean prompt, which answers in seconds. The rule is
engine-level and platform-independent (same on macOS/launchd and Linux/systemd). Full root-cause +
recipe: [decisions/m3-judgment-seat.md](decisions/m3-judgment-seat.md) §D4.

GLM's "read-only judge" framing describes its tool surface, not its voting status. The
pi-GLM seat is certifying when it is part of the canonical non-author quorum; it is still
read-only in how it inspects the artifact.

## The two axes

Routing separates two things that look similar but are not:

1. **Implementation quality** — does the code work, is it complete, is it fast to produce.
2. **Instruction fidelity** — does the agent do *exactly* what the plan says, or improve on it.

Composer wins on speed and fidelity. Codex wins on defensive instinct. The job determines
which axis matters: a reviewed plan wants fidelity (Composer); an open-ended "make this
robust" wants defensive instinct (Codex).

## Default operating loop

```
Composer first pass  →  reviewers (codex / Cold Opus / agy) catch gaps
                     →  Composer patches the mechanical findings
                     →  escalate to Codex ONLY if a reviewer flags a
                        CONCEPTUAL/domain issue, not a mechanical one
```

Commit is guaranteed for either engine by the completion stack (see below), so "which
engine commits more reliably" is no longer a routing input.

## Evidence basis

Established 2026-06-05 by two controlled bake-offs implementing the same
`AuthorityResolver` task (project-g consult ingest plan), reviewed by the usual panel.

- **Bake-off 1 (off-spec, implement from a design spec):** Composer reached
  Codex-equivalent functional level **~5.7× faster** and leaner, but Codex was more
  defensive on first pass (fail-loud loading, malformed-input handling). Given the review
  findings, Composer produced the hybrid-best version in **one** pass. Composer's only
  first-pass deficit was mechanical error-handling discipline — teachable via review, not
  a reason to change model.
- **Bake-off 2 (verbatim, implement a reviewed plan exactly):** Composer **4.2× faster**
  (55s vs 3m49s); both wrote all files and ended clean+committed. Two of three files
  byte-identical. The decider was fidelity on the third: **Composer reproduced the plan
  exactly; Codex editorialised** — it reordered a resolver's lookup precedence (a semantic
  change) and injected an unrequested regex normalisation. Conclusion: for executing a
  reviewed plan, Composer is the better implementor; Codex's defensive instinct becomes
  unrequested drift.

Net: Composer is the default implementor for explicit/reviewed work; Codex is reserved for
conceptual/off-spec work and defensive review.

## Completion stack (why commits are guaranteed)

A mutating dispatch must run in a bridge-created worktree so the completion machinery can
attribute and enforce the result:

```
ctl --target <engine>-<project>-<workspace> \
    send "<task>" \
    --worktree <name> --worktree-base <branch> \
    --expected-artifact <each file the task must produce> \
    --allowed-path <dir/>            # for files you can't name exactly, e.g. timestamped migrations
    --commit-message "<exact message>"
```

Three layers run after the engine's turn (all in
[worktree-isolated-dispatch.md](worktree-isolated-dispatch.md)):

1. **Completion gate** — bounces a turn that left the tree dirty with no commit.
2. **Drive-to-completion loop** — re-prompts a continuation-capable engine until every
   `--expected-artifact` exists.
3. **Orchestrator-commit** — idempotent state machine: adopts the agent's commit if it
   made one, commits present-but-uncommitted artifacts itself otherwise, fails on partial
   or stray-file commits. This closes Composer's known multi-file commit gap (it reliably
   *writes* files but often ends its ACP turn before committing).

Because orchestrator-commit can commit on the agent's behalf, the commit is guaranteed
regardless of the engine's commit habits — Codex self-commits (orchestrator adopts);
Composer typically doesn't (orchestrator commits). The bridge creates the worktree
**detached**, so the orchestrator advances the real feature branch afterward
(`git branch -f <feature> <worktree-HEAD>`) to keep lineage clean.

> **Never adopt a benchmark/bench worktree as production feature work.** Bench dispatches
> are controlled experiments; production work starts fresh on a real `feat/` branch and
> takes the *findings* forward, not the experimental commits.

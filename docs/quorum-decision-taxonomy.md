# Quorum decision taxonomy + override discipline

A peer-quorum mechanism for autonomous planning queues that dispatches voter agents (codex / agy-print / etc.; `gemini-acp` is deprecated) over the bridge, then synthesises a decision. **The product of this mechanism is safe executable decision-making, not consensus.** A unanimous voter result that the orchestrator overrides is not a mechanism failure — it is the intended behaviour when the orchestrator has information the voters didn't.

This doc defines:

1. A **closed taxonomy** of quorum outcomes — every decision is logged with one of these states, no ad-hoc "Claude disagreed" notes.
2. An **override discipline** — five mandatory fields a `consensus-overridden-by-orchestrator-doubt` decision must carry, or it doesn't ship.

Both ride on top of `docs/pipeline-operating-manual.md` Workflow B (parallel review + cold-opus final). Use this when the *decision-shape* itself needs to be safely arrived at before any code is written — not after.

## Outcome taxonomy

Every quorum decision doc declares a `**Consensus state:** <state>` field using one of:

| State | When |
|---|---|
| `consensus-accepted` | Voters converged, auditor clean, orchestrator agrees. Ship the voter pick. |
| `consensus-invalidated-by-audit` | Voters converged; cold-auditor surfaced a premise defect (corrupt data, stale assumption, unsound option set). Re-prompt with corrected options OR stand down. |
| `consensus-overridden-by-orchestrator-doubt` | Voters converged; orchestrator names specific doubts the voters didn't address. Pick a safer scoped action; file the deferred option as a backlog follow-up. |
| `split-resolved-by-verification` | Voters disagreed; a verification step (DB sample, code read, doc check) made one option's premise verifiably correct. |
| `stand-down-stale-premise` | Verification showed the backlog item's premise is no longer true (already done, blocked by another item, scope-rotated upstream). |
| `stand-down-unresolved-risk` | No safe option found within the time/cost budget. File the analysis, hand back to user. |

The taxonomy is closed. If a decision doesn't fit one of these, the round wasn't run cleanly — re-run, escalate, or stand down. Don't invent a new state; the closure is the point.

## Override discipline

For `consensus-overridden-by-orchestrator-doubt` (and `consensus-invalidated-by-audit` when the audit came from the orchestrator), the decision doc **must** include all five of:

1. **Voter result** — each voter's pick + one-line reasoning, verbatim where possible. No paraphrase substitution.
2. **Auditor result** — cold-auditor finding + verification step that was run + the verified-fact statement that resulted.
3. **Orchestrator doubts** — numbered, *named*, each a falsifiable claim. Not "this feels risky"; rather "Named doubt #1: exact-quote matching against retrieved chunk text has unquantified false-negative risk because chunk text contains markdown formatting (`**bold**`, paragraph breaks) that the synth's prose elides."
4. **Chosen safer action** — what actually ships and why it is lower-risk than the voter pick. The "safer" needs a one-sentence falsifiable justification, not an aesthetic claim.
5. **Deferred follow-up item** — an issue / backlog number filed for the option the override declined. The analysis isn't lost; it inherits the voter reasoning + audit + named doubts as decision provenance.

If any of these five fields is missing, the override is *arbitrary hidden authority* and must not proceed. Override power is only useful if it is auditable after the fact — the discipline is the load-bearing reason the mechanism doesn't degrade into "the orchestrator vetoes whatever it wants and writes it up later."

## Why the cold auditor is non-voting

Voters reliably **anchor on the option set** they're handed. Round 1 of one decision (#40) → C; round 1 of another (#26) → B; round 2 of #26 (re-prompted with corrected options) → D2. Each time, voters optimised *within* the choices rather than questioning whether the underlying question was well-posed.

The cold-Opus (or equivalent) non-voting auditor exists to break that anchoring. If the auditor were a voter, it would inherit the same option-set anchor. Keep the role separate.

## Field evidence

The mechanism + override discipline was articulated through two consecutive overrides on the same backlog item (admin per-citation drill-down, 2026-05-26):

- **Round 1:** voters unanimous on option B (paragraph-ref drift with expanded range). Audit found `paragraph_refs` was known-corrupt on the live corpus (verified via a separate backlog item documenting the corruption). State → `consensus-invalidated-by-audit`. Re-prompt with corrected option set.
- **Round 2:** voters unanimous on option D2 (exact-quote drift). Orchestrator named three doubts: formatting false-negatives, paraphrase false-positives, unspecified similarity threshold. State → `consensus-overridden-by-orchestrator-doubt`. Chosen safer action: ship the panel without drift detection (A2). Deferred follow-up: filed as a separate backlog issue for drift-algorithm design + impl.

The shipped scope (A2) was picked by *no voter in either round*. The mechanism worked — it just didn't ratify a winning option. Two consecutive scope reductions kept an unproven algorithm out of an autonomous-queue ship and preserved the analysis for a real design conversation later.

## Decision doc template

Put quorum decision docs under your project at `docs/peer-quorum/YYYY-MM-DD-<topic>-NN.md`. Minimal frontmatter:

```markdown
# Peer quorum decision: <topic>

**Date:** YYYY-MM-DD
**Topic:** <one-line question>
**Context:** <backlog item, queue position>
**Final decision:** <chosen option> — <one-line summary>
**Confidence:** low|medium|high (<one-line reason>)
**Consensus state:** <one of the six taxonomy states>

---

## Round 1
### Voter prompt
> <prompt>

### <Voter A> (voter)
> <verbatim or close-paraphrase pick + one-line reason>

### <Voter B> (voter)
> <same>

### Cold <auditor> (premise auditor)
- **SUSPECT:** <premise the auditor doubts>
- **VERIFICATION:** <step that would falsify or confirm>
- **SEVERITY:** advisory | blocking
- **CONFIDENCE:** low | medium | high

### Verification (orchestrator ran)
- <evidence gathered>
- **RESULT:** confirmed | refuted | inconclusive
- **VERIFIED FACT:** <one-line load-bearing statement>

### Orchestrator assessment after Round 1
<one paragraph: ship, re-prompt, override, stand down>

## Round 2 (only if re-prompted)
<same shape>

## Final synthesis
<if override:>
**Decision: NOT <voter pick>. <chosen option>. File [#NN] for deferred option.**

### Named doubts
**Named doubt #1:** <falsifiable claim>
**Named doubt #2:** <falsifiable claim>
...

### Chosen safer action
<what ships + one-sentence why-safer>

### Deferred follow-up
<#NN with one-line description>

## Disposition
- [x] Decision logged
- [x] Follow-up filed
- [ ] Implementation dispatched
```

## Relation to other docs

- **`docs/pipeline-operating-manual.md`** — defines the multi-agent workflow shapes (A/B). This taxonomy sits *inside* Workflow B's planning phase, before any implementation dispatch.
- **`docs/orchestrator-patterns.md`** — defines parallel dispatch + zero-poll monitoring. The voter dispatches in a quorum round use Pattern B (zero-poll) since voters reply asynchronously.
- **`docs/claude-peer-coordination.md`** — for lighter-weight Claude↔Claude coord without an engine; not the right home for a multi-engine quorum.

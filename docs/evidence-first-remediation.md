# Evidence-First Remediation

> Cross-references: `quorum-decision-taxonomy.md` (the disagreement/override rule this enforces), `orchestrator-patterns.md` (Pattern E dual-review), `pipeline-operating-manual.md` (where a review gates a phase).

> The rule in one line: **no remediation is scoped from an observation alone — evidence of the mechanism must exist first.** Fix observed mechanisms, not inferred causes.

## Principle

No remediation task, code change, design change, operational action, review brief, or implementation step may be created from an observation alone. Every finding must carry evidence that demonstrates the observed behaviour *before* a fix is proposed.

This is the discipline that makes multi-agent review trustworthy. A panel of reviewers that reasons from titles, summaries, or plausible stories will converge on confident, wrong conclusions. A panel that must attach evidence converges on what is actually true.

## Required workflow

```
Observation → Evidence → Mechanism hypothesis → Verification → Confirmed mechanism → Remediation → Implementation
```

## Prohibited workflow

```
Observation → Summary → Assumed mechanism → Remediation
```

The jump from *summary* to *assumed mechanism* is where false diagnoses and remediation drift come from. Evidence must exist before remediation is scoped.

## Acceptable evidence

Anything that shows the behaviour directly, e.g.: logs, stack traces, error messages, test failures/output, diffs, source excerpts (the actual bytes, not a memory of them), query/SQL results, API responses, network/runtime traces, metrics, screenshots, retrieved documents, retrieval rankings/chunks, evaluation results, reproduction transcripts, framework-inspection output, tool output, direct observation of a running system.

The test: could someone else reach the same conclusion from the artifact you attached, without trusting your narration?

## Disagreement rule

A **single** reviewer presenting contradictory evidence is enough to pause remediation and trigger investigation. Consensus without evidence is not sufficient to proceed. **Evidence outweighs reviewer count.**

(This is the same principle `quorum-decision-taxonomy.md` encodes for decisions: the quorum is a safe-decision mechanism, not a vote-counter.)

## Reclassification rule

If new evidence contradicts the current explanation:

1. Stop remediation.
2. Reclassify the finding.
3. Update the mechanism hypothesis.
4. Re-verify.
5. Scope a new remediation only after the mechanism is re-confirmed.

## What every finding should carry

- The observed behaviour.
- The evidence artifact(s).
- The proposed mechanism.
- Confidence level.
- Any known alternative explanations.

## Why this is in the bridge repo

Dispatched peer agents (codex / agy / cold-Opus subagents) return *prose conclusions*. Prose is cheap to generate and easy to trust. The failure modes this rule prevents are exactly the ones that recur in bridge-orchestrated review:

- **False "all clear."** A reviewer reports a set of fixes as correctly applied — while describing the *commit message* or the *intent*, not the file. Caught only when a second reviewer (or a `grep`/`git show` probe) reads the actual bytes and finds a stale, silently-failed edit still in place. → Verify "all clear" against the artifact, especially when an edit's success is uncertain.
- **Confabulated state.** A garbled large tool output (a "dropped commit", a "malformed 2000-line file") read as fact nearly triggers a destructive `git` operation — until a minimal authoritative probe (`git merge-base --is-ancestor`, `git cat-file -s`, `wc -l` on the committed blob) shows the state was intact all along. → Never act on a large inline diff/log/cat; verify with single-value probes.
- **Prod-only bugs a summary would miss.** A reviewer doing *real framework verification* (e.g. checking a Postgres `uuid` column vs the sqlite test DB) finds a defect that "looks fine" and that the test suite is structurally blind to. A reviewer reasoning from the summary alone passes it. → The evidence is the run, the schema, the framework behaviour — not the description of them.

In each case the corrective was the same: **go to the bytes.** This document is that corrective, written down.

## Goal

Reduce false diagnoses, prevent remediation drift, and direct engineering effort at verified problems rather than plausible stories.

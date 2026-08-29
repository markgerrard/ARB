# Phase workflow declaration

Copy this file into your project as `docs/phase-workflow.md` and fill in the
choices. A fresh Claude session reading the project should be able to load
this file and know what cadence each phase runs at — Workflow A (lightweight
single-reviewer) or Workflow B (rigorous parallel-review + cold-opus final).

The two workflows are defined in
[`AgentRedisBridge/docs/pipeline-operating-manual.md`](https://github.com/markgerrard/ARB/blob/main/docs/pipeline-operating-manual.md).

Read `<<replace-with-canonical-manual-path>>` for the full shape of each
workflow; this file only records the per-phase *choice*.

---

## Project workflow defaults

- **Default workflow for this project:** <<A | B>>
- **Reason:** <<one-sentence justification — e.g. "greenfield product, reversible changes, prefers wall-clock speed" or "legacy migration with real customer data, prefers regression-risk floor">>

A fresh Claude session reading this file should adopt the default for any
work that doesn't have a phase entry below.

---

## Per-phase choices

If the project is phased and different phases warrant different cadence,
record the choice per phase below. Add rows as new phases get scoped.

| Phase | Scope (1-line) | Workflow | Reason |
|---|---|:--:|---|
| <<0: Foundation>> | <<set up skeleton + auth + regression-test harness>> | <<A>> | <<low-risk plumbing>> |
| <<1: Reports>> | <<port read-only report endpoints>> | <<A>> | <<reads-only, reversible>> |
| <<2: CRUD UI>> | <<port edit forms and list views>> | <<A>> | <<reversible writes, no irreversible side effects>> |
| <<3: Worker cutover>> | <<port the OpenAI scoring + curation worker>> | <<B>> | <<writes real customer data; 165 ops/sec sustained rate; irreversible decisions land in include_in_circulation>> |
| <<4: Correspondence>> | <<port outbound email + scheduling>> | <<B>> | <<a missed guard sends real customer mail; outbound-mail kill switch is the only safety net>> |
| <<5: Cutover + decom>> | <<flip DNS to new app, decommission legacy>> | <<B>> | <<single-shot, irreversible, prod traffic>> |

Replace the placeholder rows with your project's actual phases. Keep the
columns. Delete this paragraph and the next one once filled in.

The B-tagged rows should align with **at least one** of these triggers:
- Touches real customer data with no easy rollback
- Single-shot / irreversible operations (re-encryption, schema migrations,
  data backfills, cutovers)
- Regulated paths (privacy, financial, safety-of-life)
- Behaviour preservation under rewrite (parity validation needed against
  a legacy system)

If a B-tagged row doesn't match any of those triggers, ask whether the rigor
is actually paying for itself — the wall-clock cost is real (~1.3× per task
plus the full-feature triple review) and shouldn't be paid for habit.

---

## Cold-opus dispatch mechanism

If any phase uses Workflow B, confirm the cold-opus dispatch mechanism is
operational on this project:

- [ ] **Orchestrator is Claude Code with the `Agent` tool available.** Cold-opus dispatches as a native sub-agent via `Agent(subagent_type="general-purpose", prompt=…)` — no bridge engine needed. Sub-agent context isolation is harness-enforced, so the sub-agent only sees what the orchestrator passes in the prompt.
- [ ] **Brief-cleanliness discipline understood.** The cold-opus brief must contain literally only: (1) path to the spec, (2) the diff (or instruction to compute it), (3) output format requirements, (4) where to commit the report. Nothing about the plan, per-task reviews, deviations, or the implementation conversation. **Brief contamination is the only way cold-opus can fail to be cold** — protect it accordingly.

Higher-rigor escape hatch (rarely needed): open a fresh Claude Code session
in a separate worktree with truly empty conversation history
(`ls ~/.claude/conversation/` shows only the current session's file). Use
only when there's specific reason to distrust the sub-agent's harness-level
isolation; otherwise the brief-cleanliness discipline above is sufficient.

---

## Revisions

| Date | Change | Why |
|---|---|---|
| <<YYYY-MM-DD>> | <<initial>> | <<initial scoping>> |

Add a row whenever a phase changes workflow (e.g. a Phase originally tagged
A reveals data-parity risk during scoping and gets re-tagged B). The audit
trail matters — workflow choices that change mid-project usually signal a
scope-or-risk discovery worth recording for the post-mortem.

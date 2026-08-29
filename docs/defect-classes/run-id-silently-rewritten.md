# The identifier was silently rewritten — records that are individually perfect and collectively absent

**The class that makes an audit trail look complete while the thing it was supposed to record
never existed.** A caller hands a tool a correlation identifier — a run id, a trace id, a batch
key — believing everything the tool does will be filed under it. The tool **rewrites the
identifier per work-item** and files each item separately. Every individual record is then
*internally perfect*: well-formed, gapless within itself, no errors. The **aggregate** — the thing
the identifier existed to create — is simply missing, and nothing anywhere reports a failure.

**The individually-valid record is not the assertion. The aggregate under the identifier you
supplied is.**

## Why it survives every local check

The defect is invisible from inside the tool by construction:

- **The tool is self-consistent with its own rewrite.** `arb-orch-panel:395` filters live events with
  `event_run == run_id or event_run.startswith(run_id + "-")` — written to accept exactly the
  suffixed form it produces. Nothing in the tool's own view looks wrong.
- **Every worker reports success.** Each seat returns exit 0, `ok=True`, a real reply and a parseable
  vote. They did their jobs.
- **The gate designed to catch it cannot fire.** `panel_audit._assert` refuses a close unless every
  rostered seat voted exactly once — but with per-item runs **there is no roster and no close is
  ever requested**, so there is nothing to refuse. A guard that must be *invoked* to fail is no
  guard against a path that never invokes it.
- **Each orphan is clean on inspection.** One vote, `seq 1`, no gaps, zero deadletters. Sampling one
  and finding it healthy is positive evidence for the wrong proposition.

**A run that does not exist and a run that is clean are indistinguishable from every vantage except
the store.**

## The detection move

**Query the aggregate by the identifier you supplied — not the items, and not the tool's own
report.** `SELECT ... WHERE run_id = <the id you passed>` returning **zero rows** while every worker
reported success is the signature. If you can only enumerate records the tool tells you about, you
cannot detect this class at all.

Corollary for tool authors: **a tool that accepts a correlation identifier owes the caller either
(a) records under that exact identifier, or (b) a refusal.** Silently accepting and rewriting is the
defect. Emitting a *derived* identifier is fine only when the derivation is visible in the record
and the aggregate still exists.

## Proving instance — `arb-orch-panel`, 2026-07-18 → 2026-08-10

`scripts/arb-orch-panel:952` dispatches every seat as `run_id=f"{run_id}-{slug}"`, unconditionally,
with no flag to disable it. The tool has **zero** call sites for `arb-audit-emit` or
`arb-audit-close-request`, so it emits **no dispatch manifest and no verdict**; `--audit-panel` only
makes the *bridge* transcribe each seat's vote. Votes are therefore the only rows it can produce.

Measured in `audit_events`:

- **92 orphan seat runs, 92 vote rows, 28 inferred panel bases.** **Zero** of those bases carry a
  base row, a dispatch, or a verdict.
- Families: `panel-piext-*` (17 bases / 63 seat runs), `panel-project-g-agent-apple-review-*` (7 / 21),
  `panel-codex-router-*` (2 / 3), `phase2-plane-acceptance-*` (2 / 5, migration diagnostics).
- Excluding migration diagnostics: **26 real panels over three weeks produced votes and no
  auditable decision**, ending 2026-08-08.

A separate, broader vote-only population — 149 run_ids / 246 vote rows, of which 57 run_ids / 154
votes also lack dispatch and verdict — is **not** attributable to this tool and remains open.

**No purge, backfill or reconstruction has been performed, and none should be without a ruling.**
The rows are accurate about what happened; it is the *absence* of aggregates that must not be read
as data loss. This section is the explanation that gap points to.

## The fix, and what it deliberately does not do

`refuse_run_id_this_tool_will_rewrite()` (`scripts/arb-orch-panel`) refuses an explicit `--run-id`
when vote emission is enabled, naming the rewrite, what is missing from the record, and the
three-emitter procedure that works. It fires as the **first** statement in `run_panel`, before
artifact validation and before the `--detach` re-invocation, so no detached child is ever spawned
under an identifier that will not survive.

It does **not** fire when `--run-id` is absent (the auto-generated label promises nothing) or under
`--no-audit-panel` (no votes are written, so no orphan can be created). Unaudited parallel dispatch
is untouched.

Making the tool emit the manifest and verdict itself — so the identifier becomes true rather than
refused — is a larger change and is deliberately out of scope here.

The working procedure is three emitters joined on **one** identifier
(`docs/orchestrator-patterns.md` § "Auditing a review/design panel"), proven end to end on
`phase2-acceptance-20260810T111117Z-r039634`: verdict row 4403, seq gapless 1..5, zero deadletters.

## Relatives

- `prediction-written-as-result.md` — a claim that outran its evidence.
- `claim-scope-exceeds-evidence-scope.md` — the same shape at the level of a sentence rather than a
  tool: `outcome=emitted` measures publication, not persistence.
- `refusal-is-ambient-assert-the-code.md` — the mirror image: there, refusal carries no information;
  here, success carries none.

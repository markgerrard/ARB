# Served-hint record — partition plan (round 7's precondition)

**Status:** drafted 2026-07-28 under operator delegation, after round 5 and the two harness-killed
folds. **Not yet executed.** This is round 7's precondition — it replaces "round 7 = another content
fold" and must land before any further fold of the monolith.

## Why now, and why this is not just tidying

Two things are growing, and they are the same problem: **the document, and the time each fold
takes.** The fold reads the whole document, reasons about all of it, and rewrites all of it, so every
byte added makes every future round slower.

Measured, on this artefact:

| | v4 | v5 | v6 draft (killed) |
|---|---|---|---|
| Lines | 900 | 1256 | 1391 |
| Bytes | 64KB | 97KB | 111KB |
| Fold wall-clock | — | 23.5 min | 26+ min (killed) |

The line budget slows the growth *rate*. It cannot remove the ceiling: round 6's author invoked the
escape hatch correctly with "the findings added irreducible new content", which is the budget
telling us that a monolith absorbing every round's output grows monotonically whatever number we
set. And the harness kills background tasks around ~26 minutes — so the v5 fold succeeding at 23.5
minutes was not the pipeline working, it was the pipeline getting lucky. We only know that because
two later runs died.

**Do not fight the harness timeout.** Even if the knob were found, v8 would be back at the same wall:
the job grows, the ceiling does not.

## The change

Split the spec into per-section files with a thin index. A fold then rewrites **only the files that
round's findings touch.**

```
docs/superpowers/specs/served-hint-record/
  INDEX.md          <- thin: purpose, section list, per-file one-line summary, invariant pointer
  01-problem.md     ~33 lines
  02-scope.md       ~22
  03-non-goals.md   ~25
  04-architecture.md  ~595   <- 43% of the bytes; split FIRST, alone, if a staged approach is wanted
  05-alternatives.md  ~26
  06-schema.md      ~241
  07-failure-posture.md ~38
  08-retention.md   ~23
  09-testing.md     ~68
  10-residuals.md   ~47
  11-questions.md   ~32
```

### What this buys, in order of value

1. **Folds become partial.** The writer reads less context and emits far fewer bytes. A typical fold
   touching §4, §6 and §9 rewrites ~900 of 1400 lines instead of all of them; a fold touching only
   §9 rewrites 68. That takes a normal round well under the harness kill without fighting it.
2. **Untouched sections carry forward byte-identical — and their hashes prove it.** This is
   something the current pipeline cannot do at all: today, "the fold did not disturb settled
   material" is an assertion nobody checks. After the split it is free and mechanical — unchanged
   file, unchanged hash. Given that round 5 found six change-table rows that did not hold, a
   structural proof of *what did not change* is worth as much as the change table itself.
3. **Review cost stops scaling with total document size.** The panel reviews the index plus the
   changed sections. Today four seats each read 1256 lines to review a fold that touched a third of
   them.
4. **It fixes J-01's mechanism.** §4's duplication is not merely bulk — round 5 established it is
   the delivery mechanism of J-01 (the two copies of the parent+hits write diverged). Splitting §4
   into its own file is the precondition for stating that contract once.

### Sequencing inside the split

**§4 first, alone**, if a staged approach is wanted — it is 43% of the bytes and the highest-value
single move. But note the round-5 caveat still binds: **do not collapse §4's per-tier restatement
until J-01's wire contract is settled and has survived a panel.** Splitting §4 into its own file is
safe now; *compressing* it is not. Those are different operations and the plan must not conflate
them.

## Migration rules — the split must be provably content-preserving

The split is a **pure move**. It changes addresses, not content.

1. **Byte-preserving.** Every line of v6 lands in exactly one section file. Concatenating the section
   files in index order, with the preamble, must reproduce the source document byte-for-byte apart
   from the added per-file headers. **Verify this mechanically before committing** — a diff, not an
   assertion.
2. **No editorial changes in the same commit.** No rewording, no compression, no fixing findings.
   A split commit that also edits content is unreviewable, because the diff cannot distinguish a
   move from a change. Content fixes come in the *next* fold, against the split files.
3. **Cross-references become file references.** Anything citing `§N` gains the file name. Broken
   references after the split are the main mechanical risk — check them.
4. **The index carries the RECEIPT INVARIANT pointer**, so a reader of any one section can find the
   invariant that constrains it without loading §4.

## What the panel reviews after the split

The index plus the changed section files, plus the unchanged-hash manifest. A reviewer should be able
to confirm in seconds that settled sections were untouched, and spend the round on what moved.

## Checkpointing — fix 3, and explicitly AFTER this

Once sections are separate, the natural driver becomes: fold one section, gate it, publish it, move
to the next — each a short task. A harness kill then costs one section, not a complete 111KB draft.

**This must not be built before the split.** Checkpointing a *monolithic* fold would mean publishing
partial documents in intermediate states, which the gate rightly forbids; checkpointing *per-section*
folds is just publishing finished units. Same reasoning that deferred the early abort: it restructures
the spawn path from one long blocking run into a sequence, so it earns its own verification pass
rather than being bolted on mid-incident.

## Honest limits of this plan

- **It does not make the document smaller.** It makes each *fold* smaller. Total bytes still grow
  with findings; the convergence criteria
  (`2026-07-27-served-hint-record-CONVERGENCE.md`) are what bound that, not this.
- **It adds a failure mode**: sections drifting out of agreement with each other, which a monolith
  makes impossible by construction. The index and the invariant pointer are the mitigation; a
  cross-section consistency check belongs in the panel brief afterwards.
- **The split itself costs a round** and produces no design improvement. That is the price of
  removing the ceiling rather than raising it.

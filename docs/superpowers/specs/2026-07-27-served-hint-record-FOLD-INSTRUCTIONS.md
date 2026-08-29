# Served-hint record — standing fold instructions

**For the FABA author of any future fold of `2026-07-27-served-hint-record-design.md`.**
Supersedes an earlier round-5-specific fold-instructions note. These are standing: they apply
to v6 and every fold after it.

Pass this file to the author as a **pointer**, never inline. The `--task` string is interpolated
into the brief the child *orchestrator* reads, and a task carrying instructions rather than a
pointer makes the orchestrator follow them itself instead of dispatching the author — four rounds
failed exactly that way on 2026-07-28. Keep `--task` to one line naming this file.

## Your inputs

1. **The document you are revising:** `docs/superpowers/specs/2026-07-27-served-hint-record-design.md`.
2. **The findings to fold:** the current panel decision record, staged in your workspace as
   `prior-record.md`.
3. **Any §9 EXECUTION report for the version you are revising** (a separate execution-pass
   report against the prior version's §9 rows). **Executed evidence outranks
   read-only assessment.** Where a §9 row was run and FAILED, fix the row — do not re-argue it from
   reading. Where a row is reported NOT RUNNABLE, that is a fact about the spec's own testability,
   not a gap in the report.
4. **The panel record:** `docs/superpowers/specs/2026-07-27-served-hint-record-PANEL-RECORD.md` —
   what is already settled and closed. Read it so you do not re-open settled items, and append to
   it (see below).

Write `artefact.md` early and revise in place. Do not exhaust your turn budget reading first.

---

## SIZE BUDGET — binding, not advisory

**v6's ceiling is 1400 lines** — raised from v5's 1256 to 1300, then to 1400 after the round-6
author invoked the escape hatch correctly (below). Deliberate, with the reasoning on record.
From v7 the rule reverts to "must not exceed the predecessor," and each later fold inherits its
predecessor's line count as its ceiling.

> **Why v6 gets a raise rather than an overrun.** The original 1256 ceiling assumed v6 would compress
> §4 (47% of the document) in the same round. That compression is now deferred to v7 (below), so the
> single largest source of savings is unavailable, while twelve round-5 findings and six
> falsely-claimed change rows all need fixing. Relocating the process history returns only ~6%.
> Holding v6 to 1256 would therefore force the saving to come out of §9 or the contested-findings
> records — exactly the "paid from the wrong account" failure the check below exists to catch.
> Setting the number honestly up front is better than watching an author overrun it and explain.
> **This is the escape hatch working as designed**, one round earlier than expected: the constraint
> met reality, reality won, and the number moved with its reasoning recorded rather than the rule
> being quietly ignored.

**Every addition must be paid for with a removal or a tightening.** If folding a finding needs 20
new lines, find 20 lines of superseded or redundant prose and cut them. Prose that describes a
mechanism the fold has just replaced is the first place to look.

**Why this is binding.** Each round is instructed to *add* — findings in, nothing out — so the
document grew +40% in lines and +52% in bytes between v4 and v5 alone. At that rate a sixth round
lands near 1,700 lines, and the panel must read all of it to review any of it. Round 4's single most
important finding (H-03: tests that cannot fail on the defects they label) is exactly the kind that
gets harder to spot as the document grows. Size is a review-quality problem, not a tidiness one.

**If you cannot meet the budget**, say so explicitly in your change summary with the line count and
the reason, and name what you would cut given permission. Do **not** silently overrun it, and do not
meet it by dropping content the panel is relying on.

**Be specific about WHICH kind of overrun it is** — the reason is the instrument, not an apology,
and the two cases lead to opposite responses:

- *"The findings added irreducible new content"* (e.g. round N's fold introduced two new invariant
  rules that must be stated) → the budget **number** is wrong and gets adjusted.
- *"§4's per-tier deltas still need the invariant restated to be readable"* → the **§4 rule itself**
  does not work as written, and splitting §4 into its own document moves up the queue.

A vague "couldn't fit it" tells the operator nothing and wastes the signal.

**COUNT YOUR OUTPUT, DO NOT ESTIMATE IT.** State the line count of the file you actually wrote, and
be able to cite it. The round-6 draft disclosed "1349 lines against a 1300 ceiling" while the file on
disk was **1391** — a 42-line error inside an explicit numeric claim. A budget disclosure that
mis-states the number defeats its own purpose: it converts an honest escape hatch back into an
unverified assertion, which is the failure class this whole document exists to remove. If you cannot
count it, do not claim it.

> **Escape-hatch precedent, round 6 (2026-07-28).** The author overran, disclosed it rather than
> hiding it, and named the kind as "the findings added irreducible new content" — which is exactly
> what this instrument is for. Per the rule above, that answer adjusts the NUMBER rather than
> forcing a cut, so the ceiling moved 1300 → 1400. The instrument is honoured when used correctly;
> that is what makes it worth using instead of quietly overrunning.

### How this budget will be checked — so you optimise the right thing

The check is **not** the total alone. It is the per-section byte table.

**For v6 specifically** — §4 compression is deferred, so §4 is *expected* to hold its ~43% share
(41,356 of 96,967 bytes in v5; 47% by lines). **§4 holding steady is a PASS for v6, not a failure.**
What is checked instead:

- **§9 must not shrink.** Round 4's headline finding was §9 tests that could not fail on the defects
  they labelled, and round 5 found more of the same by executing them. §9 getting smaller is a
  regression signal, never an efficiency — it is the worst possible place to find savings.
- **The contested-findings records and the residuals must not be thinned** to buy room.
- Savings should come from the relocation, from prose superseded by this fold, and from the six
  falsely-claimed change rows being replaced with accurate ones.

**From v7**, once compression is live, the check reverts to its original form: §4's share must fall
toward **~30%** via de-duplication, and meeting the line budget while §4 holds its share means the
bill was paid from the wrong account.

## RELOCATION — process history leaves the spec

The spec describes the **system**. The panel record describes the **argument about** the system.

**Do NOT include in the spec:**

- a "What changed, vN → vN+1" table
- a "CLOSED — verified fixed in …" section
- a "Settled scope — do NOT re-litigate" section

**Instead:**

- **You cannot write to the panel record — you produce exactly one file (`artefact.md`).** Put the
  changelog table and any newly-closed findings in a clearly-marked trailing section of
  `artefact.md`:

  ```
  # APPENDIX — panel-record append (NOT part of the design doc)
  ```

  The **orchestrator** splits at that heading and moves the appendix into
  `2026-07-27-served-hint-record-PANEL-RECORD.md` at integration. Exclude the appendix from your
  line budget and say so.

  > Corrected 2026-07-28 after round 6. The earlier instruction told the author to "append to the
  > panel record", which the one-artefact FABA contract makes impossible. The round-6 author did the
  > only available thing — segregated the content under exactly this heading, labelled it for its
  > destination, and disclosed the budget exclusion. That was good behaviour against a bad
  > instruction, and the instruction is now what the author actually has to do.
- Keep exactly this pointer near the top of the spec, and nothing more:

  > **Settled findings and round history:** see
  > `2026-07-27-served-hint-record-PANEL-RECORD.md` — do not re-open items marked settled there.

The panel keeps its guard rails; the spec stops paying for them on every future read.

## EVERY CHANGE-TABLE ROW MUST CITE THE BYTES IT CHANGED

**Binding, and the single most important rule in this file.**

Each row of your changelog must carry a citation to the change it claims: a line reference into the
draft you are producing, or the diff hunk. A row that cannot cite the bytes it changed **must not be
written as "Folded"** — mark it `NOT DONE` or `PARTIAL` and say what remains.

**Why.** In round 5, **six of fifteen** change-table rows did not hold against source. H-05 was
marked "Folded" while the timestamp still advanced on success rather than on attempt as specified;
H-02's row claimed a fix that had reintroduced the same defect on the other tier. The next round
reads the change table **before** it reads the spec, so a false row does not merely fail to help —
it actively misdirects, sending an author to fix what is already done or to skip what is not. An
unfixed finding costs a round; a falsely-claimed fix costs trust in the whole table.

The citation is what makes the claim checkable in seconds instead of requiring a reviewer to
re-derive it. If you cannot cite it, you do not know that you did it.

## §4 — DEFERRED for v6. Do NOT compress §4 in this round.

> **Sequencing, set 2026-07-28 after round 5 (operator-approved).** The compression rule below is
> **suspended for v6** and applies from v7 onward. Round 6 leaves §4's per-tier restatement in
> place, deliberately.
>
> **Why deferring is not the same as not doing it.** Round 5 found that §4's duplication is the
> *mechanism* of J-01, not merely bulk: §4 states the parent+hits write twice, the two copies
> diverged, and the author transplanted the local accessor onto the bus tier — its own inline comment
> says so — without noticing the input shape differs. That strengthens the case for compressing.
>
> **But collapsing two divergent descriptions is destructive while they still disagree.** The edit
> would silently pick whichever shape survives, and the survivor would then read as authoritative
> with no record that a contradiction was resolved by accident. The order has to be: **settle the
> wire contract (J-01) and let it survive a panel → then state it once.** At that point there is one
> contract to state, and compression is safe.
>
> **For v6:** fix J-01's wire contract and specify the parser. State the contract correctly in BOTH
> places rather than collapsing them. v7 collapses.

### The compression rule (applies from v7, once J-01 is settled)

§4 is 43% of the document (41KB of 97KB). That is a consequence of the round-3 directive being
implemented as *restatement*: the invariant is stated, then effectively restated per tier.

Keep the directive — one named RECEIPT INVARIANT (COMMIT + ISOLATION), per-tier mechanics as
consequences, rules as testable properties rather than line references. **Change only the
redundancy:**

- State the invariant **once**, in full.
- For each tier, give **only the delta** — the mechanism by which that tier satisfies it. Do not
  restate the invariant, its clauses, or its rationale inside a tier's subsection.
- **At most 6 lines per rule per tier.** If a tier needs more, the invariant is under-specified —
  fix it once at the top rather than twice below.
- Cross-reference by property name, not by repeating the property.

This must not weaken the invariant. It is still the load-bearing structure, and all three reporting
seats in round 4 credited it as genuine rather than decorative. Compress the duplication, keep the
content.

---

## Standing rules, unchanged from round 5

**Contested findings.** Where the record marks a finding CONTESTED (one seat calls it defective,
others verified it holds), **do not pick a side and do not resolve by counting votes.** Make the
spec's claim and its mechanism agree, and record both positions with their evidence so the next
panel can adjudicate. Mark any substantive choice you make as an author choice requiring operator
adjudication.

**Preserve the invariant.** v4→v5's new defects sat on data-shape and guard-boundary axes the
RECEIPT INVARIANT does not cover. Extend its test coverage to those axes; do not restructure or
weaken it.

**Do not re-litigate** anything marked settled or closed in the panel record. The §4 single-writer
question stays closed.

**§9 must be able to fail.** Every test named as a tripwire for a finding must actually fail if that
finding's defect is present. This was round 4's headline finding; a §9 that reads well but cannot
fail is the defect, not the fix.

## Required in the spec

- A **"Questions for the next panel"** section.
- The panel-record pointer line above.
- Nothing else from the relocation list.

# ARB eval-suite design v2 — panel review synthesis

> Status: **review record** (not committed). Panel review of `docs/eval-suite-design.md` (v2).
> Reviewers: codex, agy-print, cold-opus — independent, unique paths. Date: 2026-06-16.
> Reports: `/tmp/eval-v2-codex.md`, `/tmp/eval-v2-agy.md`, `/tmp/eval-v2-coldopus.md`. Prior: `eval-suite-design-review.md`.

## Verdicts

| Reviewer | Verdict |
|---|---|
| codex | REJECT |
| agy-print | BLOCK_MERGE (REJECT) |
| cold-opus | FIX_BEFORE_BUILD (2 P0, 3 P1, 3 P2) |

## What v2 closed (unanimous): Instrument 1's confound list

CLOSED cleanly: matcher format-bias (P1-2), scenario error bars (P1-3), model×harness factorial (P1-4), cross-role
scoping (P1-5), **OTel→NDJSON-authoritative (P1-6)**, cost/calibration caveats (P2-1/2), N≥10 (P2-3), and gameable
recall (P0-2, with a minor residual: the unverified-finding classifier is itself an unscored judge).

## What v2 did NOT close — it *relocated* the two deepest v1 findings into Instrument 2

### P0-1 — `lone-correct rate` is the v1 confirmer-confound, reincarnated (3/3)
A confirmer/adjudicator seat — one that *independently re-catches* a P0 but is rarely the *sole* catcher — scores ~0 on
lone-correct-rate by construction. That is verbatim v1-P1-1 ("a confirmer seat has high decision value yet ~0 unique
catch"). **Aggravator:** v1's review prescribed the fix — *add a confirmation/agreement metric* — and v2 did not carry
it into Instrument 2. ARB's own CLAUDE.md frames cold-Opus as the corroborator/contrarian whose value is confirmation
under adjudication — so v2 would once again emit "make cold-Opus non-voting," for a metric artifact, on the exact seat
the eval exists to judge.

### P0-2 — the disagreement corpus excludes whole-panel misses → a third, quieter copy of the v1 disease (3/3)
A split exists **only if at least one seat flagged the defect.** A truly illegible defect the *whole* panel missed never
becomes a split, so it never enters the corpus. The corpus is therefore *"defects legible to at least one seat"*:
- v1 seeded = legible-to-the-seeder
- harvested = legible-enough-to-be-eventually-fixed
- **v2 disagreement = legible-to-at-least-one-seat**

**The internal contradiction cold-opus caught:** v2's §4 argues *against* the harvested corpus *precisely because* it
"still excludes the never-caught set" — but the disagreement corpus **also** excludes the never-caught set (a whole-panel
miss is not a disagreement). The design's own discriminating argument applies verbatim against its own instrument. And
v1 was never rejected for *being seeded* — it was rejected for *excluding the never-caught set*. v2's "immune to seeding
circularity" defeats only the seeding half and leaves the never-caught exclusion fully intact. **v2 fixed the wrong half.**

## Other v2 findings (cold-opus)

- **P1-1 — the small-N caveat is *voiding*, not bounding.** lone-correct sliced by-class × per-seat fans out to ~12
  classes × N seats; real history yields a handful of adjudicated splits per cycle → most cells stay N=0–2 indefinitely
  → the seat-keep/drop verdict can never reach significance on any practical timescale. The instrument's central purpose
  is unmet by the data it can collect.
- **P1-2 — the §1 wall is semantic, not structural.** Instrument 1 still ships a *ranked reviewer-role floor leaderboard*;
  a ranking is the input to a drop decision. cold-Opus ranks low on *legible* floor recall → read as "weak → drop." The
  forbidden verdict re-enters through a labelled-but-comparative artifact. v1-P0-1 → PARTIAL.
- **P1-3 — bootstrap contamination.** Seeding Instrument 2 from *this session's* splits = adjudication quorum-correlated
  (judged in-quorum at the time) + self-referential curation (the splits judging cold-Opus were hand-picked from the
  session arguing for cold-Opus). §11.1 flags this unresolved, yet §9 bootstraps anyway.
- **P2s** — lone-correct denominator/participation unspecified; "missed" vs "absent/out-of-scope" conflated; the wall is
  prose-only, not mechanically enforced (separate output namespaces; reporter must refuse seat-drop fields from
  Instrument 1).

## The structural conclusion (the finding bigger than v2)

Three corpus reframes — seeded → harvested → disagreement — **all hit the same wall.** cold-opus names the invariant:

> **Every catch-record is definitionally blind to what the whole panel missed.** A record of catches can only contain
> things that were caught; the never-caught set — exactly what justifies a deep seat — is structurally unobservable from
> any internal panel output.

So the decorrelation question (does cold-Opus catch a class no other seat catches?) **cannot be answered from the
panel's own outputs**, by construction. Any internal corpus is a "legible-to-X" set for some X, and the thing being
asked about lives outside every such set.

Two consequences:
1. **The only instrument that can see the never-caught set is an *external* oracle** — defects observed *independently*
   of whether the panel caught them: escaped-defect post-mortems, production incidents, later-discovered bugs. The bug
   that shipped and bit prod is a whole-panel miss, observed from outside. This is slow, sparse, and external — and
   that's honest: measuring whether your panel has blind spots requires evidence from *outside the panel*, accruing at
   the rate reality produces escapes. There may be no fast internal proxy.
2. **A seat's value ≠ its unique-catch rate.** The decision-relevant metric is **leave-one-seat-out impact on panel
   decision quality** (does removing the seat cause escapes / verdict flips), which credits confirmation, calibration,
   and tie-break — and which *also* requires the external oracle to know what an "escape" was.

## The decision this forces (user's call)

- **(A) Pursue the external-oracle instrument.** Redefine Instrument 2 around escaped-defect post-mortems / prod
  incidents + a leave-one-seat-out decision-impact metric. Honest about being slow/sparse/external. The only thing that
  can actually answer the question.
- **(B) Accept the question is not cheaply measurable, decide cold-Opus's status on principle.** This is what the
  `autonomous-mode` skill already did — kept cold-Opus voting on defect-class reasoning, logged the model-correlation as
  an open limitation. The asymmetric cost (dropping a seat that catches illegible defects you can't measure >> keeping a
  maybe-redundant one) favors keeping it; measurement can't override that asymmetry because measurement is blind to the
  upside.
- **(C) Ship Instrument 1 only** (floor-capability + pipeline validation), with the wall made structural (unordered
  pass/fail table, not a ranked leaderboard; mechanical output separation), and explicitly **abandon the internal
  decorrelation instrument** as ill-posed. Revisit via (A) if/when escaped-defect data accrues.

## Meta

This is the third time in the conversation the dogfooding caught a confidently-wrong measurement of the panel's own
value — and this time it caught that the *measurement approach itself* is ill-posed for the question, across three
reframes, for a structural reason (catch-records can't see misses). The benchmark would have told you to drop your best
seat, three different ways. The reason you won't is that each time, you ran the design through the seat first.

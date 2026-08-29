# Measurement principles (ARB eval + review system)

> Cross-cutting principles the eval/review work kept re-discovering per artifact. Written down once so
> they're applied by design, not rediscovered by panel. Each is a *law of this system*, earned from a
> specific incident.

## P1. A validator is blind to error structured along a dimension it doesn't measure

**General invariant.** A validator can only catch error along the dimensions it actually measures. Error
structured along an *unmeasured* dimension is invisible — and worse, the validator's existence manufactures
confidence that such error *would* have been caught. **Aggregate-validators-hide-correlated-error is the
leading instance, not the whole law.** The next blind spot may be a *different* shape — error structured
along time, a rare class, an adversarial input, a seat not yet on the panel — that the "averaging" framing
wouldn't flag. State the general form, or a differently-shaped blind spot slips past *precisely because
this file made everyone confident it wouldn't* — the principles file becoming the aggregate validator it
warns against.

**Leading instance (correlated error under aggregation).** A validator that reports a *global aggregate* is
blind to error *correlated with a subgroup*: a bias high for one stratum and low for others shows as a
moderate global number that flags nothing, while the stratum it corrupts is scored as clean. **Measure at
the granularity where the error lives, or you cannot see the error you exist to catch.**

**The general fix.** Identify the dimension the error is *structured along* (subgroup, time, class,
adversary, absent-member); measure and report along it, not just in aggregate; treat any aggregate as
descriptive, never as the gate. Before trusting a validator, ask: *along what dimension could error be
structured such that this validator's output wouldn't move — and is that dimension measured?* Slice to it.

**Where this recurred (five instances of the general invariant, escalating):**

1. **Posture oracle** (`autonomous-mode/SKILL.md`) — a single cold-Opus conformance pass is brief-
   independent but not *model*-independent; its blind spot correlates with the voting cold-Opus (same
   family). Fix: route the judgment tier off-quorum; log the residual correlation.
2. **Disagreement corpus** (`disagreement-corpus.md`, DC-002) — a catch-record is blind to whole-panel
   misses; the never-caught set correlates with "what no seat could see." Fix: external oracle; relabel
   the corpus to what it *can* see (calibration on legible defects).
3. **Escaped-defect journal** (`escaped-defect-journal.md`) — even the external oracle sees only the
   *surfaced* never-caught set; the unobserved escape correlates with "never visibly bit." Fix: state the
   bound (reality's legibility, not unbounded); don't read "external" as "complete."
4. **Instrument 1 gold set** (`eval-instrument1-v0-schema.md`, P0-C) — a *global* matcher-validation set
   averages away *seat-correlated* normalizer mis-classification; the format-bias is high for prose seats
   and invisible in the aggregate. Fix: stratify the gold set **per seat**, report per-seat normalizer
   fidelity.
5. **Instrument 1 control-count CI** (`eval-p3-fixture-corpus-design.md`; P-3 panel) — the viability
   oracle computes ν_s's Wilson CI over the *nominal* control count, blind to **correlation among
   controls** along the "why-clean" dimension it doesn't measure: 19 controls that are clean for ~12
   distinct reasons are ~12 independent samples, not 19, so the CI is falsely narrow → false PASS. The
   blind spot is general: a *real* codebase can have correlated controls too, so more/realer fixtures
   reduce *how often* it bites but don't close it. Fix: tag each control with its why-clean **cluster**;
   compute ν_s's CI on the **cluster (effective) count**, the cluster being "flagged" if any member is.
   Closes the over-claim structurally, for synthetic and real fixtures alike — the suite measures the
   controls' effective independence rather than trusting their nominal count.
   - **Applied symmetrically (both axes).** The same disease lives on the *caught* side: correlated
     *seeds* (same mechanism relabelled) inflate `caught_n` → caught-CI falsely narrow → PASS too easy.
     Fix mirrored via a shared `cluster_key` helper: `Seed.cluster` (= mechanism); `caught` CI on the
     seed-cluster count; a caught cluster counts as detected only if **all** its member seeds are
     detected (anti-over-claim — the mirror of "any flagged" for noise). (Moving the class-level gate
     `I_min` from distinct *locations* to distinct *mechanisms* is the separable next step, human-owned.)
   - **Repeats are pooled, not multiplied (the resolved design).** Aggregating to clusters is necessary
     but not sufficient: *repeats* of a cluster are correlated (a deterministic seat repeats its
     outcome), so counting clusters × repeats re-inflates the Wilson n and over-claims — on BOTH the
     noise side (control clusters) and the caught side (seed mechanisms). The fix is to **pool repeats**:
     the trial unit is the cluster, n = #clusters; a cluster's outcome is pooled conservatively across
     repeats (noise flagged if flagged in any; caught detected only if detected in all). Small n → wide
     Wilson CI → it self-limits, so **no hard gate is needed** — a clean seat with genuine separation
     PASSes at small n (valid inference), a one-mechanism scenario stays UNKNOWN via the wide CI.
   - **Class-level eligibility keys on mechanisms, not locations.** Independent of the CI: a class-level
     claim requires ≥ I_min distinct *mechanisms* (seed clusters where untagged = location, so duplicate
     locations and one-mechanism-many-locations both collapse). This is the eligibility gate; the pooled
     CI is the verdict. (History: a first cut shipped a hard under-T *guard* — force UNKNOWN if clusters
     < T — but a unanimous decision panel found it over-conservative to the point of *broken*: T≈19
     independent seed mechanisms don't exist for posture classes (~5–12), so the guard made those
     classes structurally un-PASSable on any fixture forever. Replaced by pooling + the I_min-on-
     mechanisms eligibility gate, which counts real independence rather than gating on an infeasible
     threshold. Rejected an ICC/design-effect estimator for now: it reintroduces an estimable-and-
     mis-estimable parameter — the very failure mode this principle is about.)
   - **Repeat-correlation — addressed by POOLING (decision panel B), not a rail.** Repeats of a cluster
     are pooled to a single trial (noise: any-flagged; caught: all-detected), so n = #clusters, NOT
     #clusters × repeats — repeats cannot re-inflate the Wilson n at all. (An earlier cut shipped a hard
     under-T *rail* and was replaced: it made posture classes structurally un-PASSable, since ≥T
     independent seed *mechanisms* don't exist in reality.) A finer ICC/design-effect estimate (to let
     genuinely-independent repeats add power) is deferred — it needs empirical repeat data and
     reintroduces an estimable parameter. So the honest claim: "incapable of over-claiming via
     correlated controls, seeds, OR repeats (pooled)," with the ICC refinement an optional later gain.

**The tell.** Each time, the validator's number looked fine and the error lived in a subgroup the number
couldn't resolve. The fix is always the same shape — *name the blind spot, measure at the granularity
where the bias lives, never let a global aggregate gate a per-subgroup decision.*

## P2. The wall stops the writer, not the reader

**Statement.** A mechanical guard (denylist, field separation) prevents a *producer* from *emitting* a
forbidden conclusion. It does **not** prevent a *reader* from *deriving* that conclusion from a faithful
artifact. Any per-subject comparative artifact (a per-seat-per-class grid, a leaderboard) is
*reader-convertible* into the ranking the guard never wrote.

**The honest fix.** Do not claim a wall you don't have. Minimize the convertible surface (emit
PASS/FAIL/UNKNOWN, not raw sortable rates, in the headline; relegate continuous numbers to detail), and
then **state the residual convertibility as inherent-and-accepted, not walled** — in the artifact's own
construct-validity disclaimer, so a future reader is warned rather than misled. "Accepted residual" decays
into "ignored residual" the moment the caveat is dropped; keep it explicit.

**Where this recurred:** the posture-oracle "mechanical not prose" gate; the eval suite's §1 wall (the
field denylist holds for emitted fields, the grid stays sortable — Instrument 1 P1-G).

---

## P3. The benchmark harness is part of the measurement — and can destroy its own data

**Statement.** When you benchmark seats by dispatching them, the dispatch harness is not a neutral
observer. It shares a working tree, a bus, and a local port range with the thing being measured. Three
distinct failure modes follow, all observed in one 8-dispatch run on 2026-07-24:

1. **The harness perturbs the subject.** Editing a seat's workdir while a dispatch is in flight fails
   that dispatch's completion gate as `dirty_uncommitted` — the gate diffs against the state at task
   *start*, so the orchestrator's edits are attributed to the seat. Worse, it is **silent for tasks that
   start after the edit**: they baseline the dirt and report `no_changes_clean`. One contaminating edit
   therefore produces a *mixed* result set where only the in-flight runs are invalid, which reads as
   per-run flakiness rather than as contamination.
2. **The harness destroys results it fails to collect.** A dispatcher's stdout is the only durable copy
   of a reply. When the dispatcher dies (see 3), the task still *ran* — but its result key is reaped
   before it can be recovered. Half a benchmark can evaporate while every seat behaved correctly.
3. **The harness exhausts shared local resources.** Each `agent-dispatch` spawns a fresh `redis-cli` per
   BLPOP poll. Eight concurrent dispatchers held open for ~40 minutes exhausted ephemeral ports
   (`Can't assign requested address`), killing 4 of 8 runs — the four that died were not the slow ones or
   the hard ones, just the unlucky ones.

**Why it matters for measurement specifically.** Every one of these produces *non-random* missing data.
Contamination hits in-flight runs; port exhaustion hits long-running ones; both correlate with the
conditions a benchmark deliberately creates (wide fan-out, long turns). Missing-not-at-random is exactly
the pattern that biases a small-N comparison, and it is invisible in the surviving rows.

**The honest fix.**
- **Stagger** to 2–3 concurrent dispatchers, never a wide fan-out.
- **Freeze the workdir** for the whole run: no edits, no commits, no test runs in the seat's checkout
  until every dispatch has landed. Verify clean before firing.
- **Do not rely on the dispatcher as the system of record.** Capture per-run evidence independently
  (`--run-id` with audit rows, or have the seat write to a path outside the repo) so a dead dispatcher
  costs a reply, not a data point.
- **Size `--timeout` for the queue**, not the task: serial seats mean position N waits `N × turn_timeout`.
- **Report the denominator.** State how many runs were dispatched, how many survived, and why the rest
  did not. A table of surviving rows with the losses unmentioned is the same failure as a leaderboard
  with the caveat dropped (P2).

**Where this recurred:** the 2026-07-24 Opus 4.8 vs Opus 5 smoke test — one arm gate-failed by a
mid-flight edit (`disagreement-corpus.md` DC-007, row flagged provisional), then 4 of 8 follow-up runs
lost to port exhaustion, leaving the arm that most needed depth with no control run at all.

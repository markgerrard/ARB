# Defect classes — a version-controlled corpus of reusable detection moves

This directory is the **durable, version-controlled home** for generalizable defect-detection doctrine —
the *rules* (how to hunt the next instance), distinct from the per-incident *recollections* that live in a
session's machine-local memory. A rule that only exists on the one host that learned it can't prevent the
next instance on a different machine; codifying it here gives it the reach the lesson is *for*.

**These are review MOVES, not facts to know.** Each entry's value is its detection question — the thing a
reviewer (or a panel) actively asks. Read them that way, and apply them.

## The corpus

- [`fake-cheaper-than-real.md`](fake-cheaper-than-real.md) — **the umbrella class**: a test double cheaper or
  simpler than the real component *along the dimension that matters* certifies a behaviour the real thing
  doesn't have. Two specific faces, with *different* detection moves:
  - [`fixture-supplies-what-code-lacks.md`](fixture-supplies-what-code-lacks.md) — the **fixture face**: the
    test scaffold *provides* a property the production code fails to (autocommit hiding a store that never
    commits). Hunt: *"is any test scaffold supplying a property the production code is supposed to supply?"*
  - [`test-behind-framework-drive-directly.md`](test-behind-framework-drive-directly.md) — the **framework
    face**: the framework/SDK rejects the violation *before* your test reaches your code, so the test can't be
    adversarial and goes green against a no-op impl. Hunt: *drive your component's methods directly + seed
    adversarial rows; test only what YOU enforce; verify the split against framework source.*
- [`deny-proofs-need-adversarial-verification.md`](deny-proofs-need-adversarial-verification.md) — **the
  corollary that closes the loop on the gate itself**: a green deny-proof is just another green test and can
  be fixture-masked too. Inject-revert every load-bearing deny-proof — remove the mechanism, the test MUST go
  red.
- [`residual-remedy-is-also-a-claim.md`](residual-remedy-is-also-a-claim.md) — **the prose face of
  the same loop**: naming a gap honestly does not make the fix you prescribe for it true, and a wrong
  remedy is worse than an unnamed gap because the reader stops looking. Hunt: *for every residual you
  name, has the remedy been executed or only reasoned about?* Then deny-proof the REJECTED remedy —
  a test that guards a sentence, and fails loudly if the rejected remedy ever becomes sufficient.
- [`primary-path-was-the-unreviewed-path.md`](primary-path-was-the-unreviewed-path.md) — **a process/coverage
  class**: the slices test the *easy* door thoroughly and defer the *hard* one — which is usually the primary,
  high-frequency path. Hunt: *name the highest-frequency path and ask "has it run end-to-end as the real
  actors in the real env?"* (ARB Memory's seat-over-bus path was never executable while the external door was
  reviewed to exhaustion).
- [`bug-lives-on-the-held-axis.md`](bug-lives-on-the-held-axis.md) — **the meta-pattern under most of the
  others**: the unit suite holds env/config axes *constant*, so a value wrong-off-default but identical-on-
  default hides until something varies the axis. Two hunts: *run-the-path* (violate each environmental
  assumption) and *vary-the-config / grep-the-constant* (when X becomes configurable, did every reader move
  together?). Four convergent instances in one session — the validation set for the diagnosis-skill promotion.
- [`verification-is-context-triggered-not-risk-triggered.md`](verification-is-context-triggered-not-risk-triggered.md)
  — **the class about the reviewer, not the code**: a seat allocates verification by *pattern familiarity*
  (is checking part of the recognised shape of this operation?), never by *consequence*. Two documented
  triggers — **surprise** (so polish is anti-correlated with scrutiny) and **script** — and risk is neither.
  Hunt: *did this check fire because the operation's shape includes it, or because the stakes warranted it?*
  and *name the highest-consequence claim accepted this session, and the check that fired on it.* Instance:
  the same seat, on one day, accepted unverified findings into remediation dispatch **and** hash-verified a
  25KB document transfer unbidden. Why it matters here: instructions and raised effort were both tried and
  both failed, so the fix is tool contracts — put the check in the operation's interface and the seat's
  scripted diligence runs it for free.

- [`claim-scope-exceeds-evidence-scope.md`](claim-scope-exceeds-evidence-scope.md) — **the
  population-scope face of evidence generalization**: proof from one member of a population
  recorded as a property of the population (one seat's hydration proof → "the fleet hydrates").
  Hunt: *over what population is the claim quantified, and over what population was the evidence
  collected?* n-of-n claims need n-of-n evidence. First logged occurrence: E26 fleet-hydration
  readiness (Slice 1d, 2026-07-29) — one-seat proof survived 4 panels as a fleet claim until a
  20-minute 13-seat sweep falsified it. Not yet promoted (accretion rule: second occurrence
  promotes a scope bullet).
- [`refusal-is-ambient-assert-the-code.md`](refusal-is-ambient-assert-the-code.md) — **the class
  that makes defence-in-depth individually untestable**: in a default-deny system refusal is the
  ambient outcome, so deleting one mechanism usually just lets the next layer refuse instead —
  "the gate said no" is nearly information-free. Hunt: *if I delete the mechanism this test names,
  does it go red, or does another layer keep it green?* Assert the refusal CODE, never a bare
  refusal. Demonstrated on this repo's own gate deny-proof, which certified a lane check that had
  been deleted. Enforced by `tests/defect_hunts/test_gate_assertions.py`. Also the proving instance
  for the bus-side gate spec's §9.4 residual.
- [`mocked-subprocess-shape-never-matched-live.md`](mocked-subprocess-shape-never-matched-live.md) —
  **the held-axis sub-shape for external commands**: the suite mocks the subprocess layer, so the
  real binary's output shape (plain text vs JSON, list vs object, schema types, row-creation
  timing) is never exercised — green tests over code that cannot work. Four instances in one live
  acceptance (seat registration, 2026-08-08). Hunt: *for every parser of external output, does a
  live-captured fixture exist?*
- [`verification-inspected-the-wrong-object.md`](verification-inspected-the-wrong-object.md) —
  **checks that ran correctly against the wrong subject**: a silent failed swap, a count over an
  OR-alternation, a truncated extraction — the binding between inspected object and claimed
  artifact broke, so "verified" confidence attached to a different thing. Three instances in one
  session (2026-08-07). Hunt: *prove the binding (swap happened, which alternative matched, full
  object read) before trusting the check's verdict.*
- [`run-id-silently-rewritten.md`](run-id-silently-rewritten.md) — **records individually perfect and
  collectively absent**: a tool accepts a correlation identifier and rewrites it per work-item, so
  every record is well-formed and gapless while the aggregate the identifier existed to create never
  exists — and every worker reports success. The gate that would catch it cannot fire: with per-item
  ids there is no roster and no close is ever requested, so there is nothing to refuse. Hunt: *query
  the store for the identifier you SUPPLIED; zero rows beside N happy workers is the signature.*
  `arb-orch-panel` produced 92 orphan seat runs across 28 panel bases (2026-07-18 → 2026-08-10), none
  carrying a dispatch or verdict — 26 of them real panels. Enforced by
  `tests/test_orch_panel_refuses_rewritten_run_id.py`.
- [`prediction-written-as-result.md`](prediction-written-as-result.md) — **how fabricated evidence
  enters a repo without anyone lying**: an expected outcome written in the grammar of an
  observation, before the run. Predicting is good (pre-registration is what makes disagreement
  visible); writing the prediction *as a result* is the defect. Hunt: *does an execution artefact
  for this sentence exist, and did I read it?* Caught when half a two-line "INJECT-REVERT RESULT"
  docstring turned out to be false — written while building a proof about verification.
- [`workdir-mutated-while-run-in-flight.md`](workdir-mutated-while-run-in-flight.md) — **the
  voided-result class**: a run executes in a tree that something (usually the operator's own
  concurrent work) mutates before it finishes; the result describes a tree that never coherently
  existed and is void whatever its color. Hunt: *what proves the tree at finish was the tree at
  start?* Nothing → [U] regardless of outcome. Mechanism: `scripts/tree-provenance-run` (start/
  finish HEAD + tree digest, VOID exit 97 on change); policy: background runs get a disposable
  worktree pinned to the commit under test — one worktree, one writer. Two logged occurrences
  (2026-07-24 benchmark contamination; 2026-07-29 voided shr-s2 run) → bullet promoted (ARB-B1,
  owner co-signed 2026-08-01).
- [`readiness-flag-consumed-without-its-predicate.md`](readiness-flag-consumed-without-its-predicate.md)
  — **the class where every party is honest and the gate still certifies something nobody proved**:
  a producer runs a genuine executed check and records a flag; a consumer reads the flag as proof of
  *its* property. A boolean carries no predicate — only the name travels, and names are aspirational
  where contracts are not. Hunt: *for every flag your gate consumes, open the code that SETS it and
  read the predicate — same actor? same time? same conditions?* The **actor** axis hides best.
  Instance: `brief_hydrate=v1` proves the HOST's console script exits 0
  (`brief_hydrate_ready.py:156-166`), while hydration is performed by the ENGINE on its turn
  (`bridge.py:3804-3808`); `seat-preflight`'s gate reads only the flag, so the six shell-less seats
  already recorded as unable to hydrate would pass it. Note the overclaim is *positional*, not
  textual — the check's own prose says "advertise", honestly, while sitting where a PASS reads as
  "prerequisite satisfied". Partly remediated 2026-08-11: the check was renamed `target-hydration` →
  `target-advertises-hydrate` so a PASS says what it means. **Enforcement is unchanged** — those six
  seats still pass; only the misreading is closed.

## The meta-discipline (the habit that catches the next one automatically)

At the close of **any run that generates reusable doctrine, ask: "did a rule just land in the cache?"** —
the same check as "did a rule get filed in the brain instead of CLAUDE.md" (architecture §1, rule-vs-
recollection). Convenience pulls doctrine into the machine-local cache; resist it. Rules → here (durable,
inherited by every clone/teammate). Recollections (what happened on *this* run) → memory.

## Provenance

Promoted out of session-local memory on 2026-06-21 (the ARB Memory Phase-3 build, where all four were first
caught: the autocommit store-that-didn't-store, the OAuth-tests-behind-the-SDK, and a `/token` deny-proof
that passed when its check was deleted). Related doctrine still in memory and not yet promoted:
`vacuously-green-guard-fail-loud`, `evidence-store-no-silent-drop`, `doneness-ahead-of-signal`.

## Not yet done — the skill promotion (separately scoped, panel-reviewed)

This doc is the **reference** tier (CLAUDE.md-tier durable record). The higher-value move is folding these
detection strategies into the **diagnosis meta-skill / `bridge-protocol`**, so a panel *automatically* hunts
these faces rather than relying on a reviewer having read a doc — a skill is *operative* doctrine, not just
reference. That is its own scoped piece of work and **must itself go through the gate** (a defect-detection
skill that wasn't adversarially reviewed would be its own irony). Tracked, not done here.

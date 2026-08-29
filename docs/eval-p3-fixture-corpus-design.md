# P-3 fixture corpus — design + distinctness assessment (3-class scope)

Scope (human, kickoff): build **three** classes at full power first, prove the class-level verdict
path produces meaningful PASS/FAIL, then expand the remaining nine incrementally. Per class, full
power = **≥5 distinct seeds** (I_min) and **≥19 distinct control loci** (≥T), in a genuine repo the
seats investigate with tools (real checkout, per the seat-access fix — never diff text).

## The load-bearing rule (why this doc exists)
Distinctness must **span the class**, not merely clear the mechanical F6 check (distinct
file/symbol/line tuples). Five seeds must be five different *mechanisms* by which the class
manifests — not one shape copied to five locations. Nineteen controls must be plausible-but-clean
for *different reasons* — not 19 copies of one clean pattern. The mechanical check catches duplicate
*locations*; it cannot catch trivially-similar-but-differently-located instances. Correlated
instances make the Wilson CI a lie (ν_s and caught_rate estimated from non-independent samples → a
corpus that passes F6 mechanically but is internally correlated reports false confidence). The
span-judgment is the entire value of this work; author for genuine independence.

---

## Class 1 — secrets-in-logs (proven-shape anchor; pattern-spotting mode)

Five genuinely distinct *channels* by which a secret reaches a log:

| # | Mechanism | Why distinct |
|---|---|---|
| S1 | Explicit secret interpolated into an info log (`logger.info("... token=%s", api_token)`) | Developer names the secret in the log call (the proven live-run shape) |
| S2 | Secret interpolated into an **error/exception message** that is then logged | The leak rides an *error* path, not a normal log; detection must follow the raise→log flow |
| S3 | **Whole-object debug dump** leaking a nested credential field (`logger.debug("cfg=%r", config)` where `config` holds a password) | No secret is *named*; it leaks because a container is dumped wholesale |
| S4 | **Transitive leak via exception repr / stack trace** (logging `format_exc()` where an exception arg carries a DSN/password) | Developer never wrote the secret into a log string at all; it leaks through traceback machinery |
| S5 | Secret embedded in a **logged URL** (basic-auth userinfo / credential query param: `logger.info("GET %s", url)`) | The secret is structurally inside an otherwise-loggable value |

**Controls (19, span strategy):** clean-but-adjacent log calls safe for different reasons — logs an
opaque request ID; logs a *redacted* value (`token[:4]+"…"`); logs a boolean "has_token"; dumps an
object whose secret field was already popped/masked; logs a URL with credentials stripped; logs a
hashed (not reversible) fingerprint; error message that names the *field* but not its value; etc.
These are genuinely different "why it's safe" reasons. **Assessment: authors cleanly** — secrets-in-
logs has wide natural variety in both leak channels and safe-logging idioms. ✓

---

## Class 2 — correctness (general non-posture; confirms the suite isn't security-tuned)

Five genuinely different *kinds* of logic error:

| # | Mechanism | Why distinct |
|---|---|---|
| C1 | Off-by-one / wrong boundary (`range(n-1)` dropping the last; `<=` vs `<`) | Arithmetic boundary |
| C2 | Inverted / wrong boolean condition (`or` where `and`; negation dropped) | Boolean logic |
| C3 | Wrong null/empty handling (`if not x` treating `0`/`""` as absent) | Falsy-vs-absent confusion |
| C4 | State error (mutable default arg; stale accumulator not reset; shared cache) | State/aliasing, not arithmetic |
| C5 | Numeric/type error (int division truncation in money math; str/int compare) | Type/representation |

**Controls (19, span strategy):** code that *looks* buggy but is correct for different reasons — an
intentionally-exclusive range; a `not x` that's correct because `x` is genuinely a presence flag; a
mutable default that's never mutated; an int division that's intentional floor; a comparison that
looks off but operates on normalized inputs; etc. **Assessment: authors cleanly** — correctness has
effectively unlimited natural variety; the only risk is lazy near-duplicates, avoidable by spanning
the five kinds above across the controls. ✓

---

## Class 3 — authorization-scoping (THE IMPORTANT ONE; investigation mode) — ⚠ FLAG

This class validates that the floor suite measures **investigation-capability**, not pattern-
matching — the property the seat-access correctness fix exists to protect. A seat must trace how an
endpoint is reached and verify scope against route/model/role, not flag a line.

Five distinct seed mechanisms (genuinely different reasoning targets):

| # | Mechanism | Why distinct |
|---|---|---|
| A1 | **Missing** authorization check on a route that reads/writes an owned resource | No check at all |
| A2 | **Wrong-scope comparison** (checks `user.id == resource.id` not `== resource.owner_id`; or role but not tenant) | Check present, wrong field |
| A3 | **Check-after-use** (sensitive fetch/mutation happens before the scope check) | Ordering |
| A4 | **Inherited-but-overridden** scope (a subclass/override drops a base check; one route in a decorated group omits the decorator) | Structural inheritance |
| A5 | **IDOR via unscoped query** (fetch by user-supplied ID with no `WHERE owner = current_user` at the data layer) | Scope missing at data layer, not route |

**Seeds: achievable distinctly — BUT require a structured mini-app, not a single file.** To be
*plausible and investigable* (not a toy a seat pattern-matches), each needs realistic structure: an
auth dependency yielding `current_user(id, role, tenant)`, 2-3 ownership models, a router with role
decorators. That is ~150-300 lines across several files — "minimal" for a web app, materially
heavier than the secrets/correctness fixtures, but doable. The 5 seeds are genuinely distinct. ✓

**Controls: this is where the minimal-fixture approach STRAINS.** I attempted to enumerate 19
*genuinely distinct reasons* an authz-adjacent endpoint/query is correctly safe:

1. intentionally public (health/listing) · 2. correct ownership check · 3. correct role check ·
4. correct tenant scope · 5. resource derived from `current_user` (no input ID → no IDOR) ·
6. list scoped-by-construction to caller · 7. correctly-ordered check-then-use · 8. self-resource
compare that looks wrong but is right · 9. override that correctly re-applies scope · 10. decorator
correctly present on a grouped route · 11. auth correctly delegated to a service layer ·
12. public-by-design write (signup/contact) · 13. permission-flag check · 14. ownership enforced
transitively via join · 15. per-user-keyed cache read · …

**By ~10 the reasons are genuinely distinct; from ~11-19 they become cosmetic variants of "a
correct ownership/role/tenant check"** — i.e. correlated. In a *minimal* fixture, manufacturing 19
genuinely-independent authz controls forces contrivance, and contrived/correlated controls violate
the load-bearing rule (the ν_s Wilson CI would be a lie). A real app has 19+ naturally-distinct
authz-adjacent endpoints; a minimal fixture does not.

### The flag (per the kickoff hook — surfaced BEFORE the run, not after)
- **secrets-in-logs, correctness:** author cleanly at 5 distinct seeds + 19 distinct controls in
  minimal fixtures. Building now.
- **authorization-scoping:** the **5 seeds** are distinctly achievable (in a structured mini-app);
  the **≥19 genuinely-distinct controls** strain past ~10 in a minimal fixture — beyond that they
  correlate. This is the predicted signal: authz wants a **larger / real-world fixture base** to
  sustain 19 independent controls. **Decision for the human** (recorded, not resolved by me):
  (a) source a small real-world app as the authz fixture base; (b) build a purpose-built but larger
  multi-file authz app (heavier authoring, still synthetic); (c) run the first class-level milestone
  on secrets+correctness now and bring authz in once its base is decided.

### Recommendation (warm Opus; human decides)
**(c) + (a):** run the first class-level milestone on **secrets-in-logs + correctness now** — that
proves the class-level verdict path produces a real PASS/FAIL (the milestone), and both author
cleanly. Do **authz on a real-world app base** as a focused follow-up, not a contrived minimal one:
the whole point of authz is to measure *investigation*, and a synthetic minimal app risks being
exactly the toy a seat can pattern-match — defeating the validation. A real app also supplies the
19+ naturally-distinct authz-adjacent-but-clean endpoints the minimal approach can't. Option (b)
(larger synthetic app) is faster to control but inherits the contrivance risk on the class where it
matters most. So: milestone now on the two clean classes; authz done right on a real base next.

## Build status
- **secrets-in-logs: BUILT at full power** (`fixtures/src/secrets-full/`, builder + generator,
  `scenarios/floor-secrets-full.json`). Plan confirms CLASS-LEVEL (5 seeds = I_min) + adequately
  powered (19 controls = T). Demonstrated, not asserted.
- **correctness:** assessed clean; build pending the go-ahead.
- **authorization-scoping:** flagged above; base decision pending.

## Authoring constraint discovered while building (record for the corpus)
A **seed and a control must not share an enclosing function.** The matcher attributes a finding to a
locus by symbol → enclosing-function → line-window; two loci in one function are indistinguishable,
and seed-precedence would then mislabel a control false-positive as a seed detection. Author each
seed and each control in its own function (well-separated). The secrets-full fixture follows this
(24 loci, 24 functions).

## Panel outcome (execution-primary, codex + agy + cold-opus) + resolution
The corpus panel found the corpus structurally sound but surfaced the load-bearing finding: the
controls **correlate by why-clean idiom**, so the effective-distinct count is **~12 (secrets) / ~14
(correctness)**, not the nominal 19 (cold-opus's adjudicated count; agy 6/8 too strict, codex 18/19
too lenient). Plus a P0 matcher bug, non-deterministic SHAs, an unproven seed, and a dirty control.

**Resolution (human): direction A, effective-N FIRST, then real bases. Reject C.**

The over-claim risk is a **measurement-validity problem for any fixture, real or synthetic** —
correlated controls inflate confidence regardless of source; a real codebase reduces *how often* it
bites but a suite that still computes on nominal count over-claims on real bases too. So the order is
load-bearing:

1. **Effective-N honesty (done, this branch).** Each control carries a `cluster` (why-clean idiom);
   ν_s's Wilson CI is computed on the **cluster (effective) count**, not nominal — the suite is now
   *structurally* incapable of over-claiming via correlated controls, real or synthetic. This is
   measurement-principles **P1 instance 5**. `plan` now honestly reports secrets = 11 effective
   clusters (19 nominal), correctness = 13 (19 nominal), both `< T=19 → expect UNKNOWN`.
2. **Real-codebase bases (next).** Move the corpus to real apps for genuine control variety, so the
   effective count rises toward nominal and the budget clears *honestly*. Generalizes the authz
   real-base recommendation to all three classes. Bring sourced candidates for human OK before
   building.

**Reject C** (lower the power target so ~12–14 controls suffice): that is weakening the claim to fit
the substrate — the exact failure the eval arc is about. Fix the data (real bases) and the accounting
(effective-N), never the bar.

**Keep the synthetic fixtures, relabeled** as **pipeline-validation / unit scenarios** (they prove
the real-seats→M3→match→honest-verdict chain and exercise the pipeline mechanically), **not** the
substrate for a class-level statistical claim — same move as relabeling the disagreement corpus to
what it can honestly answer. Resolved blockers (this branch): matcher window-conflation P0,
deterministic SHAs, S2 (genuine error-path), S4 (concrete conn-repr leak), c02 (opaque id).

# Design — diagnose/diagnose-steer LIVE PANEL (real 3-model dispatch, contamination-proof by construction)

**Status:** DESIGN (pre-panel). Implements task #7 (BLOCKING real use). Wires the real decorrelated
panel + isolated scribe into `diagnose` / `diagnose-steer`, replacing the stubbed `SEATS` /
`generate_blind_candidates` / attested `clean_scribe_context`. Builds on the merged contamination
boundary, dogfood, fail-loud, run_record schema, confidence formula, and temporal barrier (on `dev`).
Refines specs `2026-06-19-diagnose-skill-SPEC.md` (§1.1/§1.3/§3) + `-diagnose-steer-skill-SPEC.md`.

## 0. Goal & shape
Make `diagnose` actually run a 3-seat decorrelated root-cause panel + an isolated scribe, while keeping
its defining property — **the orchestrator is STRUCTURALLY unable to contaminate panel input** — true *by
construction*, not by trust. The architecture is **skill-authors-and-seals**: the skill's deterministic
extraction authors and seals every seat/scribe brief before the orchestrator sees it; the orchestrator
(per CLAUDE.md dispatch-ownership) forwards opaque sealed envelopes verbatim to the bridge; the gate
**recomputes** each brief from the structured trigger and rejects anything that doesn't match. Read-only:
**no code is applied.** ONE new capability vs the prior read-only skill — it RUNS the failing test (§7).

## 1. The trigger is text-free; the skill derives its own traceback (the regress terminator)
The design-panel finding (GLM): even with the source list rule-derived (§1.1), the orchestrator-supplied
`error_log` makes **the window itself an orchestrator-authored selection prior** — the §1.1 bug recurring
one input down. Closing authorship without closing trigger-selection just relocates the prior.
- **`trigger = {failing_test}`** — a STRUCTURED test node-id (e.g. `pkg/test_x.py::test_y`), validated as
  a node-id (not merely `isinstance str`): matches `^[\w./-]+::[\w\[\]-]+$`, the path resolves in-repo, no
  free-text remainder. **`error_log` is DROPPED from the trigger entirely.**
- The skill **runs the failing test ONCE** (read-only; §7 bounds it) and derives the traceback →
  the error window + the AST import-closure scope (§1.1) FROM THE OBSERVED RUN, not from orchestrator text.
  The observed traceback is recorded as `recorded_traceback` — a **skill-sourced, hash-anchored** input
  pinned in the run_record. The gate **consumes the recorded_traceback; it does NOT re-run the test**
  (re-running is non-deterministic — the root cause of the recompute hole the spec-panel found). The
  recompute verifies the brief against the recorded observation, not a fresh run.
- A node-id cannot carry framing; the window now comes from the skill's own observation. This is the fixed
  point where the contamination regress stops — there is no orchestrator-supplied surface left to smuggle a
  prior through.

## 2. Brief authorship (pure function; sealed; opaque) — TWO determinism bases
The recompute guarantee (§3) is only as strong as the brief being a deterministic function of pinned
inputs. The spec-panel found this holds at FIRST dispatch but not downstream — so the spec names **two
distinct recompute bases** (it does NOT pretend one rule covers both):
- **Pre-response briefs (scribe + the 3 seats):** a pure function of `(failing_test, repo @ the run's
  committed SHA, recorded_traceback §1, committed gate-constants §4)`. Carries ONLY the
  deterministically-extracted observables (repo-RELATIVE file:line + content), the structured role task,
  and the committed role/system-prompt — **never raw trigger prose, never an orchestrator-authored string.**
- **Post-response briefs (certifier + collation):** depend on prior seats' LLM output, which is NOT a
  function of the trigger — so they are a pure function of `(committed gate-constants §4, the SEALED
  upstream submissions)`. Still orchestrator-free (the live output is the SEATS', sealed + clock-logged),
  but the recompute basis is the sealed artifacts, not `(trigger, repo)`. The gate recomputes the
  certifier/collation brief from the sealed submissions it already holds.
- **Canonical env-free byte-form (REQUIRED for both):** every brief is normalised to an environment-free
  canonical form before sealing — repo-RELATIVE paths only, NO timestamps, NO absolute paths, NO clock/
  randomness in the brief body. Else the gate's recompute (different host/checkout path, different time)
  can't byte-match and HONEST runs BLOCK. The `clock` envelope (timestamps, abs `dispatch_log_ref`) is
  OUTSIDE the canonical brief body the hash/recompute covers.
The skill computes each brief, normalises, then SEALS it (content hash in the `clock` dispatch_log) BEFORE
returning control. The orchestrator receives only opaque sealed envelopes + dispatch targets.
**The run_record MUST carry every field the gate recomputes against (else recompute is vacuous —
plan-panel P1):** `repo_sha`, `recorded_traceback` (hash-anchored), the sealed pre-response briefs + seals,
the sealed submissions + their bridge/bus reply refs (§7b consistency), and the post-response briefs +
seals. The run_record schema (`_diagnose_common`) is extended to hold these; the gate reads them as the
committed basis for §3.

## 3. The gate verifies by RECOMPUTATION, not attestation (checks)
A content hash proves bytes→digest, never *who authored* the bytes (the attestation anti-pattern). So the
gate regenerates the clean brief and compares — per the correct basis (§2):
- **(a) `brief-tampered`** — each dispatched brief's hash == the skill-sealed hash (transport integrity).
- **(b) `brief-not-skill-authored`** — the gate **recomputes** each brief on its basis and byte-compares the
  canonical form; mismatch → BLOCK. CRITICAL (full-review P0-1): the observable CONTENT is materialized from
  the **`repo_sha`-pinned blob via `git show {repo_sha}:{path}`** (the immutable git object), NOT read from a
  caller-supplied `repo_root` path — else an orchestrator pointing `repo_root` at a doctored tree makes the
  recompute self-agree on the content channel. Pre-response basis = `(failing_test, repo_sha-pinned content,
  recorded_traceback, constants)`; post-response = `(constants, sealed submissions)`. Negative controls,
  EACH must MOVE the verdict: contaminated-trigger; **doctored-tree (repo_root content ≠ repo_sha blob) →
  BLOCK** (the content-channel control the window-swap control missed); swapped-traceback.
- **(c) `submission-inconsistent` (NOT authenticity — full-review P0-2, narrowed per §7b):** a CONSISTENCY
  check that each sealed submission matches the `bus_records` the caller supplied — catches accidental
  mismatch, NOT adversarial forgery (because `bus_records` is caller-supplied, the orchestrator supplies both
  sides). It is documented as NOT a forgery guarantee. A forged submission in an otherwise-real run is a
  NAMED LIMIT deferred to #6 (§7b) — `submission-inconsistent` must not be presented as closing it.
- **(d) `unverified-without-panel`** — the producer→validator fail-loud migration (#7 / cold-Opus P1):
  the VALIDATOR (not only the producer) rejects a `verified: true` record lacking real-panel evidence
  (all roster seats' sealed submissions present, certifier independent, barrier ordering satisfied).

## 4. Committed gate-constants close the meta-channels
The seal covers brief *content*; these are the channels it would otherwise leave open (design-panel P1).
All committed as gate-constants (a `panel_constants.json`, like `confidence_constants.json`) and folded
INTO the brief recomputation (§3b), so the orchestrator cannot shape them per-run:
- **Model roster** — the decorrelated seat models (distinct models; bridge engines + native), committed.
- **Role-assignment rule** — which candidate→blind/alternative/open, deterministic by fixed rule + recorded
  seed (§diagnose 1.2), not orchestrator-chosen.
- **Certifier-selection rule** — the decorrelated certifier (model ≠ predicate author; no reciprocal cert),
  derived by committed rule.
- **Scribe system-prompt template** — the committed clean, strictly-descriptive prompt (§5).
- **Collation order** — the post-submission collation is order-deterministic by committed rule (closes the
  ordering channel the seal doesn't cover).

## 5. Scribe isolation is VERIFIED, not attested
`clean_scribe_context()` returning a hardcoded `{orchestrator_context_visible: False}` is a self-attested
boolean (design-panel P1) — the vacuously-green pattern. Instead: the scribe runs as a fresh bridge seat
whose brief == the committed clean-scribe template (§4), gate-verified by recomputation (§3b); the scribe's
system prompt is strictly descriptive (extract observables, NO synthesis/evaluation) and contains no
orchestrator context by construction (it is the committed template, nothing else). The gate checks the
dispatched scribe brief equals the recomputed template; divergence → BLOCK.

## 6. Independent phase + partial-panel fail-loud
- **Independent phase:** seats write their submissions OUTSIDE the repo-under-diagnosis (bridge engines
  share the checkout — review-hygiene rule) until all have submitted; only then are they collated. A
  submission readable by another seat pre-collation → `cross-channel-routing` BLOCK (existing neutral
  check).
- **Partial-panel fail-loud (design-panel P1):** if any seat fails to submit (down, timeout, error) the
  run is `verified: false / harness_only: true / blocking_real_use: "incomplete-panel"` — NEVER a partial
  green. A converged result requires all committed-roster seats' sealed submissions present.

## 7. Execution envelope of the self-run test (the NEW risk class — REQUIREMENTS fixed, mechanism deferred)
Running the failing test trades a contamination risk for an EXECUTION (blast-radius) risk: a failing test
can touch the network, write files, hit a database, consume resources, or hang. `diagnose` was read-only;
it is now read-only-**plus-runs-one-test**. "Isolated to the extent the platform allows + attested
residual" is **NOT acceptable for arbitrary code execution** (spec-panel): a bare `subprocess` in a tmp
checkout sandboxes neither network nor `$HOME` on the macOS fleet. So this spec fixes the GUARANTEE and the
FAILURE MODE now; it defers only the MECHANISM (sandbox-exec vs container) to the plan, after a spike.
- **Five containment invariants (HARD must-haves, all required):** (1) no network egress; (2) filesystem
  writes confined to the throwaway checkout/tmp, read-only elsewhere; (3) wall-clock timeout → fail-loud;
  (4) recursive process reaping (the test AND its child processes/daemons are killed — no orphans); (5)
  env-dependent NON-REPRODUCTION → fail-loud (if the test does not reproduce the failure in the isolated
  env, the run is `harness_only` — never diagnose a failure it didn't observe).
- **FAIL-CLOSED default (the §7 analogue of producer→validator fail-loud):** if NO available mechanism on
  the target host satisfies ALL FIVE invariants, the skill **does NOT execute the test** and returns
  `verified: false / harness_only: true / blocking_real_use: "test-containment-unavailable"`. "Can't
  contain" is a fail-loud refusal, never a silent run-under-partial-containment.
- **Mechanism deferred to the plan, decided by a SPIKE with named acceptance criteria:** the plan picks the
  mechanism (e.g. macOS `sandbox-exec` profile, or a container) that DEMONSTRABLY delivers all five, proven
  by ESCAPE-TESTS — a test that attempts network egress is BLOCKED; one that writes outside the confine
  FAILS; a hanging test (and its children) is REAPED; a non-reproducing test → fail-loud. The mechanism is
  not chosen by preference; it is chosen because an attempted escape fails closed. (Naming the timeouts
  `test-execution-timeout` per (3).)
  **SPIKE OUTCOME (2026-06-19, run on the fleet):** RESOLVED to `sandbox-exec` with an `(allow default)`
  profile minus `(deny network*)` + write-confinement to the realpath-resolved work dir — all five invariants
  demonstrated against real escapes; Docker not needed. (Profile gotchas + the validated `run_contained` in
  the plan's Task 0.)
- **Containment posture (attested residual):** under `(allow file-read*)` the contained test CAN read host
  files (including secrets) but CANNOT exfiltrate them — network is denied and writes are confined, so there
  is no exfil path. Read-but-can't-exfil is the accepted posture for a single failing-test run.

## 7a. Confidence formula correction — noisy-OR (corrects MERGED diagnose-steer)
The merged steer confidence used `Q = Σ(weight·exclusivity·strength)/Σweight` — a weighted AVERAGE, so a
valid-but-weaker disconfirmation DILUTES Q (e.g. 1.0 → 0.775), **penalizing thoroughness** — the exact
opposite of steer-spec §4 ("confidence rises with thoroughness"). The anti-strawman fold (norm=Σweight)
traded inflation-resistance for a thoroughness penalty; the dogfood missed it by testing strong-vs-weak,
not weak-ADDITIONS. Correct it as part of #7 (the live panel feeds this formula):
Plain noisy-OR over PREDICATES fixes the across-alternative thoroughness penalty but REOPENS within-alternative
count-inflation (plan-panel/GLM: `[file_line]×12` of the SAME alternative accumulates toward Q≈1). So the
correct form is **noisy-OR over ALTERNATIVES, each alternative's contribution CAPPED**:
- **`Q = 1 − Π_{alt} (1 − capped_disconf(alt))`**, where `capped_disconf(alt)` is the BEST (max, capped)
  `exclusivity·strength` among the §D3-certified predicates disconfirming that alternative — NOT a sum/
  noisy-OR over predicates within an alternative. Properties, BOTH required: (i) MONOTONE in *alternatives
  eliminated* — ruling out an additional distinct alternative RAISES Q (the §4 thoroughness property);
  (ii) BOUNDED *within* an alternative — padding many weak disconfirmations of ONE alternative cannot raise
  its term past the cap (the anti-strawman property). `confidence = P_max · Q`; `strength` from committed
  `strength_by_category[evidence_category]`; uncertified predicates contribute 0.
- **Required dogfood — BOTH properties, each against the case that fails today (plan-panel rider):**
  (a) within-alternative cap: `[file_line]×12` against ONE alternative does NOT outscore `[file_line]×2`
  against it (the term is capped) — the within-category strawman the old anti-strawman test missed;
  (b) across-alternative monotonicity: eliminating an ADDITIONAL distinct alternative raises Q. Tool-exists
  isn't verified; the formula is correct only when a test proves both fire on the inputs that expose their
  absence.

## 7b. Threat model — what diagnose defends, and the TWO honest limits (NARROWED per the full review)
The gate recompute proves INTERNAL CONSISTENCY (`brief == f(repo_sha-pinned content, recorded_traceback,
submissions, constants)`), NOT authorship AUTHENTICITY of the submissions. The full review (cold-Opus + GLM)
found the original "authenticate submissions" claim could NOT be delivered in #7: a forgery-resistant reply
ledger is required, and the bridge today exposes only a **best-effort/optional `bus-tap`** observability
archive — not a hardened, ACL-protected record (seat replies transit the orchestrator's inbox before
reaching the gate as `phase_input`). A `submission-inconsistent` check against caller-supplied `bus_records`
is CONSISTENCY, not authenticity — claiming it as forgery-protection would be a fake guarantee, worse than a
named limit. So the claim is narrowed HONESTLY:
- **diagnose DEFENDS against: framing-contamination of a real panel** — the orchestrator cannot shape panel
  input, proven by gate-RECOMPUTE anchored to `repo_sha`-pinned content (§3b; the swapped-traceback AND
  doctored-tree controls move the verdict).
- **LIMIT 1 (deferred to #6): a FORGED SUBMISSION in an otherwise-real run is NOT yet blocked.** Closing it
  needs a forgery-resistant independent reply ledger the orchestrator can't supply — that is task #6
  (hardened bus authentication), not built. The gate's `submission-inconsistent` check is retained only as
  a non-adversarial CONSISTENCY check (it catches accidental mismatch against the supplied `bus_records`); it
  is documented as NOT a forgery guarantee. Full submission authenticity lands with #6.
- **LIMIT 2 (out of scope by construction): a WHOLESALE-fabricated run** — undefendable from inside diagnose
  (defeatable by tool-bypass: an orchestrator that fabricates could skip diagnose entirely). `recorded_traceback`
  is skill-attested.
**Honest-limit-named > fake-guarantee-shipped (§4b discipline applied to the threat model).** diagnose
defends framing-contamination now; forged-submission and wholesale-fabrication are named limits, the former
tied to #6.

## 8. Dogfood gates (via bridge-protocol; attacks BOTH risk classes)
Each a matched block/clean-twin from a REAL integrated run (END-TO-END mandate; clock-produced
`dispatch_log_ref`):
- **Transit attack** — a dispatched brief altered after sealing → `brief-tampered` BLOCK.
- **Authorship attack** — a contaminated trigger (free-text/`error_log` field, overridden window, injected
  prose) → `brief-not-skill-authored` BLOCK by recomputation. Authorship contamination demonstrably
  impossible (the gate regenerates the clean brief).
- **Meta-channel attack** — an orchestrator-shaped roster/role/certifier/scribe-prompt/collation-order →
  BLOCK (diverges from committed constants under recomputation).
- **Fail-loud** — a forged `verified: true` record without full panel evidence → `unverified-without-panel`
  BLOCK at the validator.
- **Confidence monotonicity (§7a):** adding a valid weak disconfirmation to a strong one raises Q (UP), not
  down — the previously-untested axis; plus the existing strawman (many-weak < few-strong) still holds.
- **Execution-envelope ESCAPE-TESTS (the new risk class, §7):** a test attempting network egress is
  BLOCKED; a test writing outside the confine FAILS; a hanging test (and its children) is REAPED →
  `test-execution-timeout` fail-loud; a non-reproducing test → `harness_only` fail-loud; and the
  fail-closed default — a host where the five invariants can't all be met →
  `test-containment-unavailable`, test NOT run. A partial panel (seat down) → `incomplete-panel` fail-loud.
  Containment is verified because an attempted escape fails closed, NOT because the mechanism is named.

## 9. diagnose-steer delta
diagnose-steer reuses §1–§8 and adds: the labelled steer channel + staged double-blind barrier
(`max_seq`, already built) feed the LIVE panel; the steer brief is ALSO skill-authored+sealed+recomputed
(the steer reason is a committed, attributed input — the gate recomputes the steer brief from the declared
steer + trigger, so the orchestrator can't reshape it post-seal); confidence `P_max·Q` over the live
panel's certified predicates, with Q corrected to the noisy-OR form (§7a — this CORRECTS the merged steer
formula). Steered convergence still requires the discriminating experiment (§4 hard block).

## 10. Resolved positions (NO load-bearing opens — per the bridge-protocol spec-panel rule)
A spec with a load-bearing axis left "open" does not pass spec-panel (the spec-level analogue of
verified-vs-judged: an open question on a load-bearing axis is an unverified premise). So the axes the
panels surfaced are RESOLVED here, not deferred:
- **§7 containment** — resolved as *requirements + fail-closed default* (the five invariants are
  must-haves; can't-meet → refuse-to-run); only the MECHANISM is deferred to a plan-phase spike with named
  escape-test acceptance criteria. Mechanism-pending-spike is a legitimate plan decision; the guarantee is
  not deferred.
- **Recompute determinism** — resolved by the canonical env-free byte-form + the two named bases (§2).
- **`{failing_test}` sufficiency** — resolved as a design position: the skill derives signal from the
  OBSERVED run (recorded_traceback + AST import-closure), which is richer + cleaner than orchestrator
  `error_log` text; any signal a real triage needs is re-derivable from the observed run, never from
  orchestrator prose. (If a future trigger type needs more, it is a NEW structured trigger field with its
  own rule, not free text.)
- **Bridge dependency** — resolved: diagnose is a bridge skill; the bridge (bus + seats) is its runtime.
  If the bus or a roster seat is unavailable, the run fails loud (`harness_only`,
  `blocking_real_use: "bridge-unavailable"`), never a degraded/partial diagnosis.
The only genuinely-deferred item is the §7 mechanism, and it is deferred to a spike with a *pass bar*, not
left open.

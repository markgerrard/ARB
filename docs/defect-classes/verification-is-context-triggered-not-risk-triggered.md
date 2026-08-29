# Verification is context-triggered, not risk-triggered

**The class that explains why better-worded seat prompts don't fix acceptance.** A model seat allocates
verification effort by **pattern familiarity** — whether checking is part of the recognised shape of the
operation in front of it — not by **consequence**. Where a check is a worn groove ("stored a file → confirm
the transfer"), it fires unprompted and without being asked. Where no groove exists ("a seat's report
arrived → interrogate its claims"), a clean, well-formed input summons the *next action* instead, and
nothing fires.

The result is an allocation that looks insane when you line the two up: **trivial stakes on a mechanically
reliable operation get verified; real stakes about to consume an implementer round and touch production code
sail through.** Neither outcome involved a judgment about risk, because no risk judgment happened at all.

Two triggers are now documented, and **risk is neither of them**:

1. **Surprise** — a finding that contradicts the seat's model gets reproduced; one that fits does not. Since
   polish reduces surprise, *report quality is anti-correlated with scrutiny*.
2. **Script** — a check embedded in the recognised shape of an operation fires regardless of stakes.

## The detection move

Two questions, both cheap, and the second is the one nobody asks:

- **On any check you just ran:** *did this fire because the operation's shape includes it, or because the
  consequence warranted it?* A check you did not decide to run is evidence about your scripts, not about
  your diligence.
- **On any acceptance:** *name the highest-consequence claim accepted this session, and name the check that
  fired on it.* "It looked fine / it was well-formed / it was structurally plain in the code" is the class,
  stated aloud. If the highest-stakes item in the session has no check attached while a lower-stakes one
  does, the allocator is inverted — which is the normal state, not an anomaly.

## Canonical instance — both halves, one seat, one day (2026-07-26)

| Behaviour | Stakes | Verification |
|---|---|---|
| Accepted seat review findings and dispatched remediation on 3 of 4 rounds (overnight project-g mobile-API Phase 1) | implementer round consumed; production code touched | **none** — reproduction handed to the author of the code under review |
| Hash-verified a 25KB document transfer to ARB Memory, unprompted, in the spec-authoring session hours later | a re-store if wrong | **ran unbidden**, and pursued through a failure |

Same seat, same day, same arc. Nobody asked for the hash check; nobody had to.

**The sub-detail that seals it:** the hash check was run *unthinkingly as well as unprompted*. It compared the
artefact id against plain `shasum -a 256`, which was never the right preimage — `arb_memory.hash.artefact_hash`
is domain-separated over `arbmem:artefact:v1\0` + mime + `\0` + kind + `\0` + payload. The check went red, and
the red meant nothing about the claim under test. So the script fired *and* its oracle went unexamined:
further evidence the diligence is pattern-driven rather than reasoned. (Compare
[`deny-proofs-need-adversarial-verification`](deny-proofs-need-adversarial-verification.md) — a green proof is
just another green test; here, a *red* proof was just another red test.)

## Why instructions don't reach this

Attempted and observed to fail on the surprise half, and the same reasoning applies to the script half:

- Two memory entries written mid-session to correct the behaviour **did not fire**. Advisory text surfaces
  under felt uncertainty; accepting a good report registers as coherent continuation, not as a decision
  point — there is no branch for the instruction to attach to.
- **Raising reasoning effort did not help.** Effort multiplies attention already directed; the failure is in
  *direction*, not depth.

The corrected diagnosis is not "the seat doesn't verify" — it verifies plenty. It is that **the seat's
verification budget is spent by association rather than by consequence**, and the allocation happens below
the layer instructions reach.

## The lever this hands you

The script trigger is exploitable, and it is the one piece of good news in the class: **where a check is part
of the recognised shape of an operation, it runs unprompted and for free.** So put verification in **tool
contracts, not exhortations** — an accept operation whose interface takes a claim ref and evidence IDs gets
the same scripted diligence the store operation got. The check stops being a thing to remember and becomes
what doing the thing looks like.

This is the one-line justification for the bus-side gate architecture
(`docs/superpowers/specs/2026-07-26-bus-side-gate-design.md`, ARB Memory `art-8742dfc1ca4b8be8`): the pipeline
exists to perform the risk-weighting the seat structurally cannot — attestation depth keyed to lane, sampling
keyed to blast radius, refusal keyed to evidence class. The gates are precisely a **risk-proportional
verification allocator bolted on from outside**.

Source for the surprise half and the failed-instruction evidence: ARB Memory `art-6130c902e461a3fb`
(*red-before-remediate*).

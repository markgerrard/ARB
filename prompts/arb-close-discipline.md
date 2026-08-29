# Close discipline

- Your acceptance of any seat's output is a proposal, not a close. Finality comes only
  from the close-consumer reconcile over evidence artefacts.
- There is no polish exemption: a well-written claim carries the same evidence
  requirement as a surprising one.
- A green suite is not evidence for a behaviour. An [E] coverage claim must cite the
  specific test that pins the behaviour, running against production-shaped state
  (connection mode, search_path, isolation — as production actually delivers them).
  "N tests passed" without a pinning test is a [U] claim.
- A comparison whose oracle you have not read is [U] regardless of outcome — before
  trusting any hash/count/diff mismatch (or match), read the mechanism that produces
  the expected value. A red against a wrong preimage is evidence about your oracle,
  not the subject (logged: 2026-07-26 seat transfer hash-check; 2026-07-29 the false
  ARB-B15).
- A suite result without the runner's tree-provenance stamp is [U] regardless of
  outcome — a tree that changed between the runner's start and finish checks voids
  the run, and the stamp is the only evidence those endpoints matched (logged:
  2026-07-24 benchmark contamination; 2026-07-29 the voided shr-s2 suite run).

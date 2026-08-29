---
name: "arb-panel-protocol"
description: "ARB panel/chain closure v4: subject-first lenses, append-only votes, hard chain termination, dual pins, staged evidence, and substrate switching."
metadata:
  type: feedback
  origin_session_id: "seeded-by-claude-orchestrator-20260721"
  last_write_session_id: "019f8591-dad5-71a0-849e-5b60a1b2a4cc"
  source_project_key: "mark-be695e9f393d"
---

# ARB panel and chain protocol

Why: a round can be mechanically clean yet a revision chain can churn forever. This entry is the global bridge; the canonical source is ARB Memory `orch-round-closure-discipline` v4, content hash `a3bdaa1bbc9dc40c716a1b1c2f6c281279bcc8a400e9c040dcdae0a1305d0253`. Read it in full before every panel.

How to apply:

- A panel ends only as verdict `outcome=emitted` with the exact roster reconciled, or an explicit non-closable record that folds nothing. No third state.
- The orchestrator never authors adjudication. FABA is dispatched, family-disjoint, non-voting, and receipt-confirmed. Discounts are fixed in seq 1.
- Derive lenses from subject risk domains before choosing seats; every panel has negative-space coverage. One lens per seat, pairing rationale recorded. Generalists are only additive to full coverage.
- Put both labeled digest domains in every brief. STOP only if both fail. Isolated/worktree seats use staged evidence only.
- Votes are append-only and come from literal captured fences. Use the mechanical `timed-out` path for absent seats. Never infer or rewrite a stance. A lost adversarial lens can be compensated by FABA only for conservative block/needs-changes outcomes, never approval.
- Fix `max_revision_rounds` (default 3) and non-convergence in the first manifest. Before each author dispatch, compute the trajectory. Two consecutive non-decreases by count, or identical consecutive open sets, force a non-converged terminal with the full trajectory. New chains need operator authorization and a materially different approach.
- When stable findings are protocol/crypto/concurrency implementation issues, stop polishing prose. Retain a concise interface obligation and propose a separate implementation-first artefact reviewed as code and tests.

Calibration: the Polisher chain terminated at v13. Final in-flight run `panel-arb-role-polisher-v13-remediation-r1b-20260722T070211Z-9248d5` closed `outcome=emitted`, `gaps=[]`; terminal record `arb-role-polisher-nonconverged-terminal` v1.

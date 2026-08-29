---
name: "arb-round-convergence"
description: "ARB chain termination and substrate doctrine: budget in first manifest; mandatory non-converged terminal; switch implementation-grade findings from prose to code/tests."
metadata:
  type: feedback
  origin_session_id: "seeded-by-claude-orchestrator-20260721"
  last_write_session_id: "019f89fa-a0e1-7753-81a1-f46e189b69e5"
  source_project_key: "workspace-dev-bcd89c27363b"
---

# ARB round and chain convergence

Why this matters: cleanly closed rounds can still churn forever. The polisher v5→v13 chain complied with round-closure discipline yet produced an open P0/P1 trajectory of 8→17→17→13→14→13→12→12; r12/r13 were byte-identical. Future orchestrators must terminate chains by rule and hand the operator a decision instead of spending more rounds.

Canonical doctrine: ARB Memory `orch-round-closure-discipline` v4, content hash `a3bdaa1bbc9dc40c716a1b1c2f6c281279bcc8a400e9c040dcdae0a1305d0253`. Read it before any ARB panel. This memory is a bridge, not a replacement.

How to apply:

1. In the first round's seq-1 manifest, declare `max_revision_rounds` (default 3 unless the operator sets another value) and the non-convergence predicate.
2. Before every fresh author dispatch, compute the full per-round P0/P1 trajectory.
3. Terminate as `non-converged` when the surviving P0/P1 set fails to decrease across two consecutive revisions, by count, or when any two consecutive rounds have identical open-set identities. Write the terminal record with the full trajectory. Fold nothing further and dispatch no author round.
4. A new chain requires fresh operator authorization and a materially different approach. The non-converged terminal is the success path: it supplies the decision point.
5. Match finding class to substrate. If the stable findings are implementation-grade protocol, crypto, or concurrency issues, stop polishing prose. Keep only the interface obligation in the workflow/design (for example, “MUST be authenticated; mechanism owned by the implementing system”) and propose a separate implementation-first artefact whose panels review code and tests.
6. Round-level closure still binds: exactly `outcome=emitted` with reconciled roster, or an explicit non-closable record that folds nothing. The orchestrator never authors the decision record; FABA is dispatched, family-disjoint, and non-voting; seat discounts are in seq 1; one subject-derived lens per seat, including negative space.
7. Every brief carries both labeled digest domains. STOP only when both fail. Isolated/worktree seats use staged evidence only. A lost adversarial lens may be compensated by FABA only in the conservative block/needs-changes direction, never for approval. Re-fire when truncation loses candidate yield.
8. Derived child rounds carry the parent run-id only as metadata, never embedded in their own run-id.

Operational calibration: the polisher terminal is completed history. The in-flight v13 panel closed `outcome=emitted`; terminal `arb-role-polisher-nonconverged-terminal` v1 was written; no v14 was dispatched. The surviving twelve findings were handed to the operator as an interface-contract + implementation-first authority-authentication split.

Related: `orch-doctrine-imperatives`, `arb-panel-protocol`, `faba-bounded-rounds`.

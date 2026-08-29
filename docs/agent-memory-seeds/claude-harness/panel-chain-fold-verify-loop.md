---
name: panel-chain-fold-verify-loop
description: "Design/plan chains: every fold is new authored surface — delta-verify it; findings shrink ~10x/round; verify your own synthesis claims like panel P0s"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 94eac80c-47e5-4588-98bf-d0c37be20a1c
  modified: 2026-07-23T20:18:28.096Z
---

Proven over a 6-round chain (automem hybrid, 2026-07-23: r1 consult → r2 doc review →
r3/r4 spec verifies → r5 plan review → r6 plan verify): every time the orchestrator
folded panel findings, the fold itself authored NEW P1 defects that only the next
delta-verify caught (r3: 5 fold-authored P1s incl. an unimplementable persistence seam;
r6: the r5 "fix" for an e2e assertion was itself wrong — index injects only at
thread-start, needs a verifier thread).

**Why:** a fold is fresh, unreviewed authored surface written under synthesis pressure;
the author can't see their own invented mechanisms failing. Convergence is visible:
findings shrank roughly an order of magnitude per round (12 → 5 → 1 → prescribed pins).

**How to apply:**
- After folding any panel round, run a DELTA-scoped verify round (enumerate the folds
  as claims; instruct seats to hunt what the fold introduced; forbid re-litigating
  settled material). Stop when a round returns only P2s.
- Orchestrator synthesis claims get the same hinge-claim verification as panel P0s —
  my own "recency inversion" premise and "assert last request" fix were both wrong and
  both caught only by code-grounded review ([[consult-before-retry]] generalizes).
- Trust finding-convergence across seats, not any one seat's stance
  ([[seat-calibration-agy-soft-approve]]).

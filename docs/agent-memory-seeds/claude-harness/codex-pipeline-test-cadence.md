---
name: codex-pipeline-test-cadence
description: "Mark's ruling — no full-workspace cargo test between panel rounds; targeted suites only, ONE full gate after panel passes"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: f70f91e2-f19d-4cc5-b55c-b479406bdb01
  modified: 2026-07-21T04:37:34.235Z
---

During codex-fork pipeline remediation↔review loops, do NOT run the full-workspace
compile-and-test gate between rounds — it takes too long (Mark, 2026-07-21, auto-memory
pipeline). Verify remediations with cheap targeted suites only (the feature crate + touched-
module test filters — seconds once the worktree build cache is warm) and lean on the panel +
spot code-traces for correctness. The expensive full `cargo test` workspace gate runs ONCE, at
the very end, after the panel reaches zero P0/P1 — classified name-level vs
`baseline-test-report.md` + known clusters (with ABAB attribution before blaming the host, see
[[corrupt-target-dir-mimics-host-degradation]]).

**Why:** full workspace compile+test in a cold/warm worktree costs tens of minutes per round;
across a 5-round pipeline that dominates wall-time while adding little — bg-wake evidence:
panels found the defects, full gates mostly confirmed.

**How to apply:** in any codex ARB pipeline (v2 global-tier next, and future fork features),
schedule exactly one full-suite gate on the final tip; all interim verification is targeted.

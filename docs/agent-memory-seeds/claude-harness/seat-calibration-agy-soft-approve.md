---
name: seat-calibration-agy-soft-approve
description: agy-print approves softly on design/plan reviews and can mis-verify; its approve is weak signal — weigh codex/GLM/grok convergence instead
metadata: 
  node_type: memory
  type: project
  originSessionId: 94eac80c-47e5-4588-98bf-d0c37be20a1c
  modified: 2026-07-23T20:18:34.641Z
---

Observed across the automem chain (2026-07-23, 6 audited rounds on arb-codex seats):
`agy-arb-codex-dev` voted approve twice where the other three seats converged on real
P1s, and in the r5 plan review its writeup actively MIS-verified the central defect —
it described the notice arithmetic as correct when codex/GLM/grok each proved it
dropped scanner-rejected and overflow topics.

**Why:** matches the known static-vs-executing calibration split, but this is stronger:
not just soft labels, affirmatively wrong verification prose on a checkable claim.

**How to apply:** on design/spec/plan panels, treat agy's stance AND its verification
claims as advisory; a 3-1 with agy as the lone approve is effectively unanimous
needs-changes. Its unique findings still count (it found real items in r2/r4). Keep it
on panels for lens diversity, not for certification weight. See
[[panel-chain-fold-verify-loop]].

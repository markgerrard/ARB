---
name: consult-before-retry
description: "Mark's rule — after TWO failed solo hypothesis tests on any ops/infra/debug problem, convene a seat consult; don't try-try-try alone"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: f70f91e2-f19d-4cc5-b55c-b479406bdb01
  modified: 2026-07-21T13:44:05.896Z
---

"Two or three or four heads are always better than one. Don't try, try, try again before asking
for advice." (Mark, 2026-07-21, after the D-4 launchd debugging.)

**Why:** the orchestrator ran five solo debug rounds on the launchd seat failure (volume-warm
theory, ad-hoc bypass, wrapper, lease-TTL theory, stderr capture) while a 3-seat consult —
which Mark had to order — was minutes-cheap, parallel, and the root cause (a self-inflicted
plutil argv mangle) was visible on the first `launchctl print` to fresh eyes. The whole
session's record shows decorrelated reviewers catching what the solo view misses, including
the orchestrator's own inline code (twice); debugging is not exempt.

**How to apply:** hard trigger — after TWO failed hypothesis tests on an ops/infra/debugging
problem, STOP solo iteration and dispatch a lightweight advisory consult (2-3 seats, evidence
bundle + explicit questions + own current hypothesis marked "verify me"). Seats are idle
between panel rounds anyway. Applies to the orchestrator's own work products and theories with
the same force as everyone else's ([[codex-pipeline-test-cadence]], playbook rule 6:
orchestrator-not-exempt).

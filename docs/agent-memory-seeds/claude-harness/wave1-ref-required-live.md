---
name: wave1-ref-required-live
description: "Wave-1 live 2026-07-29 — BRIDGE_TASK_REF_REQUIRED=1 on 7 shell-capable bridge-dev seats; 6 shell-less seats (asdk×4, pi-sdk×2) cannot live-hydrate and stay flag=0"
metadata: 
  node_type: memory
  type: project
  originSessionId: e1e02db0-c214-46b5-9095-b90494f64f72
  modified: 2026-07-29T04:29:26.972Z
---

Wave-1 canary executed 2026-07-29 (owner go): `BRIDGE_TASK_REF_REQUIRED=1` +
`BRIDGE_SUPERVISOR_PYTHON` pin live on codex-{sol,luna,terra}, agy, cursor, devin, grok
bridge-dev seats. Per-seat pos+neg verified (exact `invalid-payload-task-ref` in seat log;
legacy senders get silence-then-timeout, no reply envelope). Evidence + rollback backups:
ARB `.claude/wave1-evidence/2026-07-29/` (REPORT.md; plist-backups/).

The 6 remaining roster seats (asdk-bridge-dev-{haiku45,opus48,opus5,sonnet5},
pi-sdk-bridge-dev-{glm,minimax-m3}) run shell-less engine harnesses — they advertise
`brief_hydrate=v1` (in-process readiness) but structurally cannot execute the hydrate helper
on an engine turn (`engines/agent_sdk.py` "You have no shell"), so live ref dispatch fails
`brief_hydration_receipt_missing`. Fork RESOLVED (b) by Mark 2026-07-29: the six stay out,
no panel ahead of need; wave-2 zero-legacy window deferred accordingly. When a wave needs
them: class-based exit at brief creation, live probe matrix as FIRST instrument.
sonnet5 additionally can't host typed worktrees (`--workdir /Users/<user>` not a repo).

Owner review folded (ARB dev `afe5fa1f`, artefact v2): rival-instrument probe rule now
REQUIRED in `docs/pipeline-operating-manual.md` (probes after each remediation + n-of-n
against deployment claims); E26 logged as first occurrence of
`docs/defect-classes/claim-scope-exceeds-evidence-scope.md`. Backlog: ARB-B5 (retire old
clone's bridge-dev envs pre-wave-2), ARB-B6 (silence-then-timeout refusal reply), ARB-B7
(done). A ref-required timeout with no reply = check seat log for
`invalid-payload-task-ref` FIRST (ARB-B4c).

Related: [[harness-kills-long-waiters]], [[sol-seat-now-launchd-managed]].

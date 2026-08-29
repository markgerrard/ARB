---
name: devin-acp-seat-calibration
description: "Roster calibration for the devin-acp bridge seat (SWE-1.7) from its first live review cycle, 2026-07-18"
metadata: 
  node_type: memory
  type: project
  originSessionId: 0068beb4-005f-471f-944b-b5c1ea851794
  modified: 2026-07-18T14:54:26.296Z
---

devin-acp seat (SWE-1.7 via `devin acp`, seat `devin-bridge-dev`, engine on branch
feat/devin-acp) — first-cycle calibration, 2026-07-18. Authoritative record: ARB Memory
`art-81ef40a78683d2e9` **v3** (v1's "model resolution fatal" and v2's "widening is
Mark-gated" facts are stale):

- **Protocol-native strength**: only reviewer of 7 (six seats + orchestrator) to catch a
  cursor ACP test-fixture starving the `authenticate` handshake step (test passed via a
  silent 30s timeout burn). Derived COLD — no authoring context — via per-test timing
  measurement. Trust it hardest on ACP/protocol-level findings.
- **Executing seat → escalates labels** (voted `block` on a test-only P2 nit): findings
  count, labels advisory — same rule as grok/kimi but in the strict direction.
- **Non-certifying on Devin-authored subjects by LINEAGE correlation, not bias**: a fresh
  Devin session has no authoring context (nothing to defend) but shares SWE-1.7's blind
  spots — it verified handed findings fine; unknown whether it would find them cold. On
  non-Devin-authored work it needs no discount at all.
- Devin one-shotted the adapter itself (a1c2743): zero verified P1s from a 6-seat panel;
  its misses were all fleet-institutional-knowledge ports (grok D2/D3b, deny budget,
  cursor last-chance drain, env scrub), not competence errors.
- Seat standup: daemon from the <workspace> checkout (`--engine devin-acp`, <workspace>/.venv
  python + PYTHONPATH=<workspace>/src), `devin` CLI must be logged in; model config ids
  swe-1-7 / swe-1-7-medium. Related: [[seat-worktree-python-env-gap]].
- Since 2026-07-18 pm the seat is launchd-managed like the rest of the fleet:
  `com.example.arbseat.devin-bridge-dev.plist` (RunAtLoad/KeepAlive false, kickstart-driven,
  logs at ~/Library/Logs/agent-bridge/devin-bridge-dev.log); no --model flag (CLI default).
  Unresolvable-model start failure is best-effort since <workspace> dev 7ea5431 (logs +
  continues). Rostered as adjunct in using-agent-bridge SKILL.md (46deb6c) and promoted past
  experimental in pipeline-operating-manual (d00e78c), both Mark-approved 2026-07-18.
  Trusted-sender composition is NOT Mark-gated: per Mark's co-signed ruling 2026-07-18,
  sender policy is a spawn-time parameter administered by the spawning orchestrator
  (rail recorded in SKILL.md § "Seat trust is set at spawn"); currently claude-bridge-dev only.

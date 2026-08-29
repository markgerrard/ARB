---
name: diagnose-steer
description: Read-only declared-steer variant of the diagnosis harness, with steer-specific validation in steer_validators.py. Use when a diagnosis must carry an explicit declared steer. WARNING - the live decorrelated 3-model panel and isolated scribe dispatch are NOT wired yet. Runs are machine-marked panel_executed false, verified false, harness_only true, blocking_real_use live-panel-not-wired, and must not be treated as verified diagnoses.
---

# diagnose-steer

Read-only declared-steer diagnosis harness.

The live decorrelated 3-model panel and isolated scribe dispatch are not wired
yet. Current runs certify only the contamination-boundary harness and are
machine-marked `panel_executed: false`, `verified: false`, and
`harness_only: true` with `blocking_real_use: live-panel-not-wired`; they must
not be treated as verified diagnoses.

Steer-specific validation lives in this skill's `steer_validators.py`, not in
`skills._diagnose_common`.

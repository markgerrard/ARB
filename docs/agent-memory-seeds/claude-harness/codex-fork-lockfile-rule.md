---
name: codex-fork-lockfile-rule
description: "Cargo.lock rule CHANGED 2026-07-21 — keep lock aligned, minimal deliberate commits; old never-commit rule superseded"
metadata: 
  node_type: memory
  type: project
  originSessionId: f70f91e2-f19d-4cc5-b55c-b479406bdb01
  modified: 2026-07-21T09:22:13.046Z
---

The old "never commit Cargo.lock on fork branches" rule is SUPERSEDED (Mark's ruling D-9,
2026-07-21, commit `23fc3eaa0e` on `arb/auto-memory`). The rust-v0.144.4 tag rewrite had left
the committed lock stale vs manifests (`0.0.0` vs `0.144.4`), so `--locked` was broken
fork-wide; a one-time alignment commit fixed it.

**Why:** the old rule guarded against version-rewrite churn in diffs; with the lock aligned,
that churn no longer occurs and `--locked`/CI builds work from HEAD.

**How to apply:** keep the lock ALIGNED with manifests; commit only minimal deliberate lock
diffs (e.g. new-crate entries); label alignment/lock commits clearly so they can be dropped on
upstream cherry-picks (upstream regenerates the lock). See [[codex-pipeline-test-cadence]] for
the related test-cadence ruling.

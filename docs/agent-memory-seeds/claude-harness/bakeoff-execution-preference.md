---
name: bakeoff-execution-preference
description: "Mark's execution mode for significant impls: N parallel independent TDD implementors in isolated worktrees, wall-time measured, then synthesize/fuse"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 94eac80c-47e5-4588-98bf-d0c37be20a1c
  modified: 2026-07-23T20:18:49.414Z
---

For the automem Phase 1 implementation (2026-07-23) Mark chose — unprompted, over the
standard subagent-driven/inline options — a three-way parallel bake-off: cursor-acp +
codex-luna(high) + devin SWE-1.7, each independently TDD-executing the same reviewed
plan in a hard-isolated `--worktree`, with per-implementor wall-time measured, followed
by orchestrator synthesis/fusion of the best result.

**Why:** decorrelated implementations of one plan expose plan ambiguities and give a
quality/speed comparison the single-implementor path can't; fusion keeps the best of
each. Wall-time data feeds seat scorecards.

**How to apply:** when execution mode is being chosen for a substantial, well-specified
plan, offer the parallel bake-off as an option (seats in isolated worktrees off the
target branch, same brief, `--worktree-cleanup keep`, skip approval-gated gates in
seat runs — run those once at fusion). Record start/end epochs per seat. Requires
per-project seats whose workdir IS the target repo ([[panel-orchestration-host-wiring]]).

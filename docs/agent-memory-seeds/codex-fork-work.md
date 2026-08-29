---
name: "codex-fork-work"
description: "This machine's codex is a fork build: bg-wake + auto-memory v1/v2; branches, flags, soak state"
metadata:
  type: reference
  origin_session_id: "seeded-by-claude-orchestrator-20260721"
  last_write_session_id: "seeded-by-claude-orchestrator-20260721"
  source_project_key: "unresolved:/Volumes/<workspace>/repos/codex-arb-artifacts"
---

The codex binary you are running is built from the author's private fork of codex, not stock:
- background_terminal_wakes_turn: background terminal exits wake idle sessions as turns
  (branch arb/bg-wake-impl, in production soak since 2026-07-17).
- auto_memory (v1+v2): THIS memory system — project + global filesystem memory with
  memory_save/memory_forget, index injection, /memory TUI. Branches arb/auto-memory (v1),
  arb/auto-memory-v2 (v2, superset; binaries are built from its tip).
- Flags live in ~/.codex/config.toml: [features] auto_memory + background_terminal_wakes_turn;
  [auto_memory] enabled. Keying default "directory"; "repo" shares one store across a repo's
  git worktrees.
- Panel-reviewed and gate-clean; full audit trail in codex-arb-artifacts (DECISIONS.md D-1..D-13).

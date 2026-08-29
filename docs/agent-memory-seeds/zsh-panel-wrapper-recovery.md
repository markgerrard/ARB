---
name: "zsh-panel-wrapper-recovery"
description: "ARB panel recovery when a zsh wrapper dies after backgrounding seats: avoid reserved status, inspect audit once, never duplicate votes, supersede partial runs."
metadata:
  type: feedback
  origin_session_id: "019f8591-dad5-71a0-849e-5b60a1b2a4cc"
  last_write_session_id: "019f8591-dad5-71a0-849e-5b60a1b2a4cc"
  source_project_key: "mark-be695e9f393d"
---

# zsh panel-wrapper failure and append-only recovery

Provenance: Polisher v6 panel attempt `panel-arb-role-polisher-v6-remediation-20260722T032142Z-2923fd` on 2026-07-22.

## Failure

A parallel ARB panel shell wrapper backgrounded five `agent-dispatch` pipelines, then aborted immediately on `status=0` because `status` is a read-only special parameter in zsh. A follow-up diagnostic also failed from shell quoting. The cockpit stopped at the two-failure threshold and consulted AGY before recovery.

## How to apply

- In zsh panel wrappers, never assign to `status`; use a task-specific variable such as `panel_rc`.
- After a wrapper dies after backgrounding dispatches, do not blindly re-dispatch. Take one read-only snapshot of seat logs and append-only audit state.
- If the run contains only the sequence-1 manifest and no task ids/votes, write an explicit non-closable record, fold nothing, preserve the audit scar, then mint a fresh run-id whose manifest names `supersedes`.
- If any partial votes exist, never edit/delete/replay them into the same run. Mint a superseding run and emit a complete fresh roster there.
- Use one persistent event-driven watcher for the corrected parallel wrapper; do not poll seat/task state.

Why: audit votes are append-only and duplicate actor votes hard-refuse closure. Wrapper recovery must preserve evidence while avoiding accidental duplicate votes or a laundered partial round.

---
name: "operator-rules"
description: "Mark's standing rules: event-driven waits/no polling for normal ARB panels and FABA, test cadence, consult-before-retry, remote verification, lockfile discipline."
metadata:
  type: feedback
  origin_session_id: "seeded-by-claude-orchestrator-20260721"
  last_write_session_id: "019f8591-dad5-71a0-849e-5b60a1b2a4cc"
  source_project_key: "mark-be695e9f393d"
---

Standing rules from Mark, applying to any agent working on this machine:

- **Event-driven waits; never poll:** For every long-running operation—including a normal ARB panel round, individual panel-seat dispatches, bridge/FABA/agent-dispatch work, tests, builds, or terminal jobs—start each process once and immediately arm one persistent completion watcher. Ordinary panels are not an exception and do not need FABA to qualify. The watcher may loop internally over bounded blocking waits and must notify only on terminal completion. Yield control after arming it. While it is live, do not repeatedly call `write_stdin`, Redis/task status, `ps`, file-size checks, terminal status, or equivalent polling. At most one coarse watchdog may fire just beyond the bounded wait window to verify that the watcher itself still exists; it must not become task-state polling. Resume work from the completion notification. If Mark asks for status, report the last known event without taking a fresh poll unless he explicitly requests a fresh status snapshot.
  - **Why:** repeated foreground polling wastes turns/tokens, makes the cockpit appear blocked, and Mark has explicitly objected to this pattern multiple times.
  - **How to apply:** one process, one persistent event-driven watcher, one terminal notification. For a multi-seat normal panel, dispatch seats in parallel where permitted and attach event-driven completion watchers; do not serially or repeatedly poll their terminal sessions.

- **Test cadence:** never run the full workspace cargo test between review/remediation rounds—targeted crate/module suites only; ONE full workspace gate at the end after reviews pass, failures classified name-level against the baseline docs in codex-arb-artifacts.
- **Consult before retry:** after TWO failed solo hypothesis tests on any ops/infra/debugging problem, stop and ask other seats/models for advice with an evidence bundle. Many heads beat solo iteration; your own work and theories get the same scrutiny as everyone else's.
- **Verify remote at milestones:** committing is not backup. At milestones and session end, check every touched repo: `git rev-list <branch> --not --remotes` (per branch) and confirm a remote exists at all.
- **Cargo.lock on the fork:** keep it version-aligned; commit only minimal deliberate diffs (new-crate entries); label alignment commits so upstream cherry-picks can drop them.
- **"Checkpoint context" protocol:** when Mark says "checkpoint context", append a metrics entry to ARB `.claude/session-checkpoints.md` — it is NOT a request for a handoff.
- **Local session memory is cache, not record (2026-07-29):** any fact worth keeping lands in the repo (repo-scoped: docs, defect corpus, this seeds corpus) or ARB Memory (cross-repo/orchestration) FIRST; machine-local agent memory only duplicates it as pointers plus this-host ergonomics. A fact whose sole copy is in one machine's agent memory is misfiled — it is invisible to every other seat, harness, and host, and dies with the machine. Claude-harness stores re-mirror to `docs/agent-memory-seeds/claude-harness/` when ARB-relevant memories change.
  - **Why:** Mark works across multiple agents, harnesses, and hosts; the proving instance was the owner-set default panel roster (2026-07-20) whose only durable record sat in one machine's Claude local memory for nine days while the seed corpus carried a diverging seat catalogue.
  - **How to apply:** before saving an agent-local memory, ask where the durable copy lives; write that first, then save the local pointer.

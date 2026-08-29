---
name: harness-kills-long-waiters
description: Claude Code harness sporadically kills long-running background shells; recovery pattern for dispatch waiters and full-suite runs
metadata: 
  node_type: memory
  type: project
  originSessionId: fb152c4d-fab4-4fe4-9403-bfaa7d9a7d95
  modified: 2026-07-28T17:26:48.240Z
---

The Claude Code harness sporadically kills backgrounded Bash tasks mid-run (observed 4× on
2026-07-28: two `dispatch-dev` waiters, two full-suite pytest shells). The kill takes the child
process tree with it UNLESS detached.

**Why:** unknown/harness-internal; not load-correlated. Treat as routine, not an incident.

**How to apply:**
- **Dispatch waiters:** the bridge task survives its waiter. On a kill notification, read
  `task:<id>:status` (single-shot ctl), then arm a cheap poll loop (`state != running` → exit)
  as the completion signal, and read the durable `task:<id>:result` key — never re-dispatch.
  Afterwards DEL the stranded consume-once reply from the `claude-bridge-dev` inbox (safe: the
  result key is the durable record; see [[benchmark-harness-destroys-its-own-data]] for why
  un-drained replies matter).
- **Long test suites:** `nohup … & disown` with output to a scratchpad log, then a background
  monitor loop polling `kill -0 <pid>`; a plain backgrounded pytest dies at ~70% when the shell
  is killed and the log truncates silently.
- **Mutation probes:** always print/check the match count before trusting a probe's green — a
  probe that mutated zero lines proves nothing (bit the orchestrator live on 2026-07-28).

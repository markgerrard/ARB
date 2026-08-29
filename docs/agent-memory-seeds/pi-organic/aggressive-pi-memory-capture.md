---
name: "aggressive-pi-memory-capture"
description: "Save durable lessons by default; don't wait to be asked. Project scope unless true everywhere."
metadata:
  node_type: memory
  type: feedback
  originSessionId: "019f7ea1-4837-751b-9200-91b183949534"
  lastWriteSessionId: "019f7ef8-8245-7eed-b5d1-fcf66c6b70c9"
  sourceProjectKey: "mark-be695e9f393d"
---

# Aggressive pi-memory capture

**Why:** Mark confirmed (2026-07-20) that pi sessions should automatically capture durable session lessons into filesystem pi-memory going forward, not only when asked.

**How to apply:**
- At natural checkpoints and end of substantive work, save high-value operating lessons with `memory_save`.
- Prefer **project** scope for repo/branch/workflow specifics; **global** only for facts true on every project (identity, cross-tool prefs, machine-wide infra).
- For feedback/project topics include **Why:** and **How to apply:**.
- Update existing topics instead of duplicating; use `memory_forget` when obsolete.
- Still never store secrets, credentials, transient task state, or repo-derivable facts.
- Do not narrate routine memory writes unless useful.

Related: [[pi-memory-retrieval-heuristic]], [[pi-orch-startup]]

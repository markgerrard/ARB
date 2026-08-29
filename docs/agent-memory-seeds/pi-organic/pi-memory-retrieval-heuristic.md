---
name: "pi-memory-retrieval-heuristic"
description: "At turn 1 read feedback/user topic bodies; load reference/project on demand. Never blanket-read all bodies."
metadata:
  node_type: memory
  type: feedback
  originSessionId: "019f7ef3-345c-7eb5-b649-eeaaa7dfe6dd"
  lastWriteSessionId: "019f7ef8-8245-7eed-b5d1-fcf66c6b70c9"
  sourceProjectKey: "mark-be695e9f393d"
---

# Pi-memory retrieval heuristic

**Why:** Early-usage signal from the designing session (2026-07-20). Blanket "read all bodies at turn 1" fights token economy (caps allow 60 global + hundreds of project topics) and the poisoning ceiling (bodies stay out of auto-injection on purpose; only escaped 240-char descriptions are the injection surface).

**How to apply:**
- **feedback / user** — behavior rules and preferences. Read bodies at turn 1 so they shape the session without a manual nudge.
- **reference / project** — lookup facts. Read on demand when the task touches them.
- Never blanket-read all topic bodies every session.
- If a rule must fire every session, put the **actionable imperative in the description** (always injected, ≤240 chars). Keep rationale/detail in the body.
- Same Why/How-to-apply structure: imperative up top in the injected line, not buried in the body.

## Reliability ladder (do not confuse test targets)

Best → worst enforcement for turn-1 behavior:

1. **Code** — static line in `buildMemoryPrompt` (product default; always injected).
2. **Description** — imperative in the 240-char index line (always injected; this topic's description *is* the rule).
3. **Body** — only works if something already caused a read. Circular for bootstrapping turn-1 policy.

Operator preference lives at **tier 2** (this description), not tier 1, unless we deliberately productize it. Behavioral tests must prove the **description alone** drives action — never "apply your retrieval rules" spoon-feeding, and never n=1 on stochastic runs.

## Clean-session test shape

- **Plumbing (deterministic):** `buildMemoryPrompt` / `before_agent_start` injects names+descriptions only; body markers never appear; global before project. Covered by unit test.
- **Behavior (rate, 3–5 fresh processes, cwd = repo that owns the project key):**
  - **A** — unprompted: list only what was injected (index lines). No tools.
  - **E (core)** — description-imperative topic (e.g. reply token on line 1); body unread; does behavior fire?
  - **D** — unrelated ask → no mass body reads.
  - **C** — task that needs a project fact → that one project body opens.
  - **B reframed** — normal task that a feedback rule should change (e.g. proactive `memory_save` under aggressive-capture); never mention memory; score whether behavior appears. Not "open these files."
- cwd must match the project key under test (`/memory path` preflight). `cd ~` is wrong for project-tier probes.

**Design gap flag:** behavior-rule memories ineffective until something prompts a body read. Fix is description-carried directive (tier 2) or `buildMemoryPrompt` (tier 1), not eager full load.

Related: [[aggressive-pi-memory-capture]], [[pi-orch-startup]]

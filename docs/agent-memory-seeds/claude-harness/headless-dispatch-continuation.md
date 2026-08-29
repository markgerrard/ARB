---
name: headless-dispatch-continuation
description: "Headless orch turns: own sender identity only, arrange reply continuation, author-complete ≠ subject-staged; recover from durable result keys"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 5413c8b0-c7ae-458c-b4d5-fc68ef14eaa6
  modified: 2026-07-22T16:17:34.982Z
---

Field incident 2026-07-22 (chains A/B post-terminal setup): Sol's headless turn dispatched two author rounds with `FROM_AGENT_ID=claude-bridge-dev` (a DIFFERENT orchestrator's identity) and ended its turn. Replies routed to an inbox with no live consumer; both chains silently stalled with completed authors and nothing staged. Nothing was lost — outputs sat in durable `task:<id>:result` keys.

**Why:** the reply route is determined by sender identity, and reply consumption requires a live dispatcher (or arranged continuation). A borrowed identity + ended turn = orphaned replies at best, another daemon eating them at worst.

**How to apply:**
- Dispatches from any headless/bridge-seated orchestrator turn use that seat's OWN sender identity — never another orchestrator's.
- "Dispatch running" + turn end is not a workflow: keep the dispatcher alive as the wait, or arrange the bounded-rounds continuation pattern before ending the turn.
- Author task `ok=true` is transport success only. The round deliverable exists when the subject is published/staged with receipt + dual pins. Watch for the silent gap between author-complete and subject-staged; it produces no error anywhere.
- Recovery path: durable `task:<id>:result` (multi-reader, bridge-written) — never consume anyone's inbox to recover.

**Generalization (confirmed by a second stall the same day, votes→close phase):** the defect class is "phase boundary without a designated driver," not any specific phase. A headless orchestrator round has driver-required boundaries at author→stage, stage→panel, votes→adjudication, adjudication→close. Before ANY headless turn ends, every remaining boundary must have a driver: this turn completes it, a live process waits on it, or an explicit continuation is arranged. "The seats will finish on their own" only ever covers the current phase.

Related: [[panel-orchestration-host-wiring]], [[verify-remote-at-milestones]].

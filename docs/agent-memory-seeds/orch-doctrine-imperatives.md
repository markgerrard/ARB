---
name: "orch-doctrine-imperatives"
description: "ALWAYS-BINDING when doing ARB orch/panel/dispatch work: 6 imperatives — verify hinges, arbitrate don't relay, fold once, targeted tests, consult after 2 failures, push at milestones"
metadata:
  type: feedback
  origin_session_id: "seeded-by-claude-orchestrator-20260721"
  last_write_session_id: "seeded-by-claude-orchestrator-20260721"
  source_project_key: "unresolved:/Volumes/<workspace>/repos/codex-arb-artifacts"
---

When you touch ARB orchestration, panels, dispatch, or verification, these BIND (details in the
referenced bodies — open them when the surface goes live):

1. **Claims are leads; evidence decides.** Verify every P0/P1 hinge claim and every worker
   "done" against code/artifacts/runtime before acting on it. [[arb-verify-dont-trust]]
2. **Arbitrate, don't relay.** Wait for the full round, fold ONCE with a changelog, rule
   disputes with evidence (no rebuttal rounds). Target ≤5 rounds/stage. [[arb-round-convergence]]
3. **Audit trail is append-only.** Manifest before dispatch; votes only from verbatim JSON
   fences; errors are fixed by re-minting with supersedes, never editing. [[arb-panel-protocol]]
4. **Targeted tests between rounds; ONE full gate at the end.** [[operator-rules]]
5. **After two failed solo hypothesis tests, convene a consult.** Your own theories get the
   same scrutiny as everyone else's. [[operator-rules]]
6. **Committing is not backup.** At milestones: per-branch rev-list --not --remotes sweep +
   remote-exists check on every touched repo. [[operator-rules]]

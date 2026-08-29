---
name: "arb-verify-dont-trust"
description: "ARB’s verify-don’t-trust doctrine: claims are leads, artifacts/outcomes are evidence; verify worker completion, panel hinge claims, brief premises, and consequential actions."
metadata:
  type: reference
  origin_session_id: "019f8567-c622-7241-8941-df1d9a63d358"
  last_write_session_id: "019f8567-c622-7241-8941-df1d9a63d358"
  source_project_key: "mark-be695e9f393d"
---

# ARB: verify, don’t trust

## Principle

ARB treats agent output as testimony, not completion evidence. This applies equally to worker success reports, reviewer findings, review-brief premises, tests/verifiers, and the orchestrator’s own assumptions.

A claim can guide investigation, but it does not receive operational authority until the relevant artifact or outcome independently supports it. This is stronger than ordinary “trust but verify”: separate claims, evidence, and decisions.

## How to apply

- Worker “done” claims: independently verify the returned commit/SHA, diff, assigned-worktree isolation, dirty state, required artifacts, and exact test output before integration.
- Reviewer P0/P1/P2 claims: treat them as candidate findings. Isolate the empirical hinge claim and trace or reproduce it against actual code, artifact text, or runtime behavior before remediation. Polished false findings and real findings can look equally convincing.
- Panel consensus: do not equate agreement with proof. Count convergence only over claims seats actually grounded. One contradictory piece of direct evidence outweighs multiple unverified opinions.
- Brief premises: treat scene-setting descriptions of how the current system works as claims under test. Give exact baseline-verification pointers and require reviewers to report whether those sources confirm or contradict the brief.
- State changes: verify the result immediately before any dependent action. When the consequence matters, prefer independent evidence over repeating the same check.
- Structural enforcement: use isolated worktrees, commit and clean-tree gates, independent test reruns, decorrelated panels, reconciled audit closure, and re-paneling after folds. Self-verification alone does not freeze an artifact.
- Verification should be proportional: establish the required state, then stop. The doctrine is disciplined evidence handling, not infinite checking or generalized distrust.

## Concrete incident: literal-tilde ARB Secrets path

A fresh Codex session found that a literal repository path `./~/.arb-secrets/` came from passing `"~/.arb-secrets/..."` through `Path()` without `expanduser()`. The diagnosis was correct, but the proposed consequential action (“remove or safely relocate”) still required separate verification.

The operator verified:
- the private key had never been committed, so no history surgery was needed;
- it was the identity’s only copy, so deletion would destroy that identity;
- relocation into the real `~/.arb-secrets` with 0700/0600 modes preserved the identity and pins.

Evidence therefore selected relocation, not deletion. The producer was then fixed at all intake points, regression-tested, and `/~/` was added as a defense-in-depth ignore rule.

## Why this matters

Even a correct diagnosis does not automatically justify a specific action. Verify the action’s hinge facts—especially identity, data-loss, security, and history assumptions—before acting. Agent competence affects how useful a claim is as a lead; it never replaces outcome evidence.

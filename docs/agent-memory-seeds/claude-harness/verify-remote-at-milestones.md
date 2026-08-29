---
name: verify-remote-at-milestones
description: "Mark's rule — sweep every touched repo for remote-unreachable commits at each milestone and session end; committing is not backup"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: f70f91e2-f19d-4cc5-b55c-b479406bdb01
  modified: 2026-07-21T15:27:02.553Z
---

At every pipeline milestone (spec gate, panel pass, full gate, binary install) and session end,
sweep EVERY repo the work touched: per branch,
`git rev-list <branch> --not --remotes | wc -l` (remote-unreachable commits — the true backup
measure; tracking labels mislead in both directions), and check `git remote -v` is non-empty
(a repo with no remote is 100% single-disk regardless of commit discipline).

**Why:** 2026-07-21 — at believed-full-wrap-up, the codex-fork feature branches (147 commits
incl. the production binary's tip) and the entire audit-trail repo (129 commits, remote never
configured) were both local-only. Found only because Mark asked "is it pushed?".

**How to apply:** push feature/leg branches as-is; create PRIVATE repos for artifact/decision
logs lacking one; surface unpushable stashes to their owners. Seconds of wall time per repo.
Playbook rule 9 (v5 addendum, orchestrator-panel-arbitration-playbook). See also
[[consult-before-retry]], [[codex-pipeline-test-cadence]].

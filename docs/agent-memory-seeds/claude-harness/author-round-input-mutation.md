---
name: author-round-input-mutation
description: FABA author-round child overwrote its off-workspace input pointer file (adr-v17.md) with its own output — treat staged inputs as contaminated after any author round; verify by hash
metadata: 
  node_type: memory
  type: project
  originSessionId: fa825ef1-baad-46da-8586-7808baef1ddb
  modified: 2026-07-20T03:23:09.223Z
---

During the ADR v18 fold (2026-07-19, run `08fa03f2`), the sonnet author child overwrote the
staged prior file `/private/tmp/arb-orch/faba-v18/adr-v17.md` with its folded v18 output
(mtime flip + hash match with its workspace `artefact.md`), destroying the verification
baseline. The publish itself was correct; only the input was mutated.

**Why:** the author-round contract gives the child Write/Edit and its brief points at input
files by absolute path — nothing marks them read-only, so an "edit the prior in place, then
copy to artefact.md" strategy silently contaminates the cockpit's staging area. Pairs with
r5's F14 (revision guard is presence-only): the whole author-round input side lacks
integrity checks.

**How to apply:** after any author round, re-verify staged inputs by hash before diffing
against them (keep the fetch-time sha256); candidate driver fix is to copy inputs into the
round workspace and/or instruct the child that inputs are read-only. Recovery recipe:
refetch the pinned version via subagent (`memory_get` takes a version param) and hash-check.

**Fix landed 2026-07-19** (owner-approved, AgentRedisBridge dev `166c4c12`): the driver now
sha256s staged inputs (workspace copies + source file) after materialisation and re-verifies
post-round — workspace mutation blocks the publish, source mutation warns and is recorded in
the final JSON (`input_integrity`). The same commit closes F14 routes (a)/(b) (guard content
check); F14 route (c) — SubagentStop fold-awareness — remains open. Hash-check staged inputs
manually only for rounds run on drivers predating `166c4c12`.

**Verified live 2026-07-20:** both the v19 and v20 folds (runs `ee5b6e24`, `dbf4b51c`) ran
the automated check — `input_integrity.ok: true`, zero mutations, publishes proceeded. The
automation works; manual hash-checks are only for pre-`166c4c12` drivers. Related new guard
the same day: `validate_authored_artefact` tail-markup check (commit `6e2b0fae`), from the
recurring stray-`</content>` publish blemish (v17/v19) — root cause is stochastic child
emission at end of full-body Writes, not input contamination.

# Merge/close gate — redesign to ref observation (shapes (c)+(a))

**Status:** draft for panel review. Nothing implemented. Authored inline by the warm orchestrator
under the code-grounded-design rotation (`CLAUDE.md` § Workflow C); PUBLISH goes through the
harness round, not a direct store write.

**Owner decision already taken (2026-08-02, Mark):** the gate gets shape **(c)+(a)** — post-hoc ref
observation as the enforcement primitive, framed by (a) *gate the integration, not the string*.
Shape **(b)** (remove raw `Bash`, typed merge tool only) was considered and **not** chosen: it
revokes a shell capability the owner granted and called load-bearing, which is a constitution-layer
change. This note does not reopen that.

**Predecessors:** three panels, all closed and audited, all block —
`panel-warmorch-4slices-20260802T100544Z-222b63`,
`panel-warmorch-remediation-20260802T121813Z-06f457`,
`panel-warmorch-rem2-20260802T131437Z-6c5276` (unanimous).

## 1. Why a fourth parsing fix is the wrong move

`src/arb_warm_orch/gates.py:1-55` carries a MEASURED CAUGHT / NOT-CAUGHT contract, derived by
running the gate over the corpus in `tests/arb_warm_orch/test_merge_close_gate.py`, not from memory. Its own
closing paragraph names the root cause:

> this module re-implements shell parsing to predict whether a merge will happen, and the input
> space is a shell grammar.

Two of the current misses (`gh --verbose pr merge`, `bash -lc`) were **introduced by the fix that
closed the previous set**. That is a property of the approach, not bad luck: each round samples an
open, adversarial grammar rather than covering it.

**Live exposure — re-measured for this note, not quoted from the docstring.** Every specimen below
was run through the real `evaluate_merge_close` at `9d2ffb3c` with a resolver that never resolves,
so anything merge-shaped MUST deny. The canonical control denies, which is what makes the misses
readable as findings rather than as a broken probe:

| specimen | command | gated? | code |
|---|---|---|---|
| **control** | `git merge topic` | **True** | `merge-close-evidence-unresolved` |
| backslash continuation | `git \<newline>merge topic` | False | *(no opinion)* |
| `#` eats the `;` | `echo a#b; git merge topic` | False | *(no opinion)* |
| combined shell flags | `bash -lc "git merge topic"` | False | *(no opinion)* |
| combined shell flags | `sh -ic "git merge topic"` | False | *(no opinion)* |
| gh boolean global | `gh --verbose pr merge 12` | False | *(no opinion)* |
| gh boolean global | `gh -v pr merge 12` | False | *(no opinion)* |
| eval | `eval "git merge topic"` | False | *(no opinion)* |
| command substitution | `$(echo git) merge topic` | False | *(no opinion)* |

**8/8 confirmed live misses.** Two further docstring specimens — a merge inside a script file, and
an aliased or shadowed binary — are not machine-checkable without a filesystem/PATH fixture and are
carried forward unverified by this note.

## 2. What is actually wired today

The gate is one runtime-agnostic decision function with three wire adapters:

| Layer | Where | Shape |
|---|---|---|
| Decision | `gates.py:327` `evaluate_merge_close(evidence, tool_name, tool_input) -> GateDecision` | pure function of `(str, dict)` |
| Claude adapter | `gates.py:365-376` `build_merge_close_gate`, wired at `runner.py:126-130` as the **only** hook (`PreToolUse`) | deny / **no opinion**; ambient permission layers keep authority (`gates.py:304-311`) |
| Codex adapter | `codex_approvals.py:1-40` | **blocking** server→client approval; no ambient layer behind it, so "no opinion" is unanswerable — base policy `ApproveNonGated` is REQUIRED, never defaulted |
| Grok adapter | `grok_runner.py:19-26` | `_meta."x.ai/hooks"` `blockingEvents: ["pre_tool_use"]`, decisions `deny`/`block`; we are the CLIENT so `session/request_permission` is ours to answer |
| ACP server | `acp_server.py:14` | explicitly **not** a gate point — the consumer auto-approves `session/request_permission`; the decision "stays in the runner's PreToolUse hook" |

Refusal codes today: `merge-close-evidence-unresolved`, `merge-close-evidence-check-failed`
(fail-closed on resolver crash, `gates.py:335-338`).

**The load-bearing fact.** All three adapters can share one decision because that decision's entire
input is a command string. Ref observation's input is *repository state*, and **no approval wire
carries repository state**. The redesign is therefore not a rewrite of `gates.py` — it is a new
layer at a different seam.

## 3. The proposed shape

Two layers, with the enforcement claim moving to the second.

### Layer 1 — the pre-execution gate STAYS, and stops overclaiming

Keep `evaluate_merge_close` and all three adapters exactly as they are. Change what the module
*claims*: it is a cheap advisory that catches the canonical spellings and prompts for evidence
early. It is **not** the enforcement boundary and its docstring must say so.

Rationale: deleting it would be a strict downgrade on the **codex** path, whose blocking approval is
a *stronger* control point than anything post-hoc detection provides (`codex_approvals.py:3-7`).
Prevention where we have it; detection where we don't.

Per `CLAUDE.md` — *prose in code is a claim at the strength enforced, no more* — the CAUGHT list
stays as the guarantee, and the NOT-CAUGHT list is re-labelled from "live exposure" to "covered by
layer 2", **only once layer 2's tests are green**, never in the same commit as the intent.

### Layer 2 — ref observation, the actual enforcement

At the runtime-neutral post-execution seam (`turn_events.ToolCallCompleted`, already emitted at
`runner.py:191-195`), compare a snapshot of **integration refs** taken before the call against the
same set after:

- refs unchanged → no opinion, as now;
- an integration ref moved **and** an evidence artefact resolves for it → no opinion;
- an integration ref moved **without** resolved evidence → **violation**: refuse to proceed,
  quarantine, and surface the delta.

Every NOT-CAUGHT specimen in §1 collapses to the same observable here. `eval`, `$(…)`, `bash -lc`,
a backslash continuation, a script file and a shadowed binary are indistinguishable *by construction*
once you stop asking "will this command merge?" and start asking "did an integration ref move?".
The space stops being an open grammar and becomes a closed, enumerable set of refs.

**Do not implement this as a Claude `PostToolUse` hook.** The SDK supports one
(`claude_agent_sdk/types.py:262-263,318-321`), but it is Claude-only and would rebuild the
three-adapter split one layer down. `turn_events.py:1-12` is explicit that its vocabulary exists so
`gates.py` and `dispatch.py` move between runtimes unchanged; that property is worth preserving.

## 4. Forks the panel must resolve

These are genuinely open. The note takes no position on 4.2–4.4.

### 4.1 Detection is after the fact — what is the containment story?

A refusal cannot un-merge. "Refuse to proceed" only bites if the orchestrator's *next* action is
gated, and the merge has already landed in the object store. Options: hard refusal + operator
alert; automatic `git reset` to the snapshot; quarantine the branch and require an evidence artefact
to release. **A design that only detects and reports is weaker than what codex has today** — this
fork is the one that decides whether the redesign is a net gain on every path or only on the Claude
path.

### 4.2 What counts as an "integration ref"?

Dispatched workers commit inside their own worktree/branch **by design** (`CLAUDE.md` § warm Opus
owns integration). Worktrees share `refs/heads/*` with the main checkout, so a worker's perfectly
legitimate commit moves a ref that the orchestrator's repo can see. A naive "any ref moved" rule
fires constantly on authorised worker activity, and a gate that cries wolf is a gate that gets
bypassed. Candidate narrowings: only `main`/`dev` and their remote-tracking counterparts; only refs
the orchestrator's own worktree has checked out; an explicit allowlist.

### 4.3 Detached HEAD, fast-forwards, and non-commit integrations

A fast-forward moves a ref without creating a commit. A detached HEAD moves no branch ref at all. A
`git merge --no-commit` leaves the index dirty and the ref still. Which of these count?

### 4.4 The race is live, not theoretical

Another session is writing to `dev` concurrently — at handoff time `origin/dev` was `2e697142`,
touching `acp_server.py` / `runner.py` / `test_acp_server.py` (verified unchanged as of this note).
A ref can move between snapshot and compare for reasons that have nothing to do with the gated
call. Snapshotting *local* refs only, or attributing by commit author/reflog, are candidate answers.

### 4.5 Cost

A ref snapshot is a subprocess. Taking one around **every** tool call is likely unacceptable;
scoping to `execute`-kind calls (`turn_events.tool_kind` maps `Bash` → `execute`) is the obvious
narrowing, but note that narrowing by tool kind reintroduces a *prediction*, just a much coarser and
more defensible one than shell parsing.

## 5. Acceptance criteria

The bar is falsifiable. It does **not** already have its corpus — see §5.1:

1. **Every NOT-CAUGHT specimen in §1 is DETECTED by layer 2**, each as a test that fails against
   layer 2 removed. **These tests do not exist yet** — see §5.1; writing them is part of the work,
   not a repurposing of the existing corpus.
2. **Each test asserts the specific violation code**, never a bare refusal — the merge/close path is
   layered and default-deny-ish, so a bare refusal proves nothing
   (`docs/defect-classes/refusal-is-ambient-assert-the-code.md`).
3. **A negative corpus**: authorised worker commits on worker branches, and the concurrent-`dev`
   race of §4.4, produce **no** violation. Without this, criterion 1 is satisfiable by a layer that
   refuses everything.
4. **Layer 1's CAUGHT list is unchanged** — no regression on the codex blocking path.
5. `gates.py`'s docstring is re-derived by running the corpus, not edited by hand, and the
   measured date is updated.

### 5.1 The NOT-CAUGHT specimens have NO tests — verified, and it changes the estimate

`tests/arb_warm_orch/test_merge_close_gate.py` (392 lines) holds two corpora, `MERGE_SHAPED:309`
and `NOT_MERGE_SHAPED:337`, driven by `test_every_merge_shaped_command_is_denied:354` and
`test_ordinary_commands_are_not_over_denied:361`. **Every specimen in both is a CAUGHT case.** Not
one of the eight NOT-CAUGHT spellings in §1 appears anywhere in the file.

`gates.py:1-55` says each NOT-CAUGHT specimen was "found by a panel and reproduced" — reproduced
*by the panels*, in panel evidence, not committed as a regression test. The distinction is easy to
read past and it matters twice over:

- **Estimate.** Criterion 1 is eight new tests plus their harness, not a re-point of existing ones.
- **Standing exposure.** Because no test encodes the misses, nothing in CI notices if a future
  refactor silently changes which of them the gate catches — and nothing would have caught the
  regression pattern that produced two of them (a fix closing one spelling while opening another).
  **Even if layer 2 were abandoned entirely, these eight belong in the corpus as documented-miss
  tests**, asserting today's behaviour so the next change to `gates.py` cannot move it invisibly.

## 6. OPEN

- §4.1 containment is the decision that determines whether this is a net gain on the codex path.
- Whether layer 2's violation is expressible on the codex `decline` wire at all — `codex_approvals.py:22-29`
  records that the new-gen `CommandExecutionRequestApprovalResponse` has exactly one property
  (`decision`) with nowhere to put a reason, so a code cannot travel to the model on the live path.
  Layer 2 may have the same problem, and the same mitigation (append to `records` regardless).
- Sequencing against `main`, which is 7 commits behind `dev` and still carries the §1 bypasses.

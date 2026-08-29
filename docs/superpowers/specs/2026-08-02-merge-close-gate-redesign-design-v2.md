# Merge/close gate — redesign to ref observation (shapes (c)+(a)), v2: reflog-window attribution

**Change summary:** v2 revises the round-1 note against all ten required changes from the
round-1 design panel (verdict: needs-changes). It corrects two factual
errors in §2 (the decision is not pure; the "architectural barrier" against `PostToolUse` does not
exist, so the seam choice is re-derived from a temporal constraint instead) and one in §1 (the
parsing diagnosis covered only half the exposure — `MERGE_SUBCOMMANDS` scope is the other half); adds
the fourth (pi) adapter throughout; replaces ref-tip diffing with **reflog-window attribution** as
the owner directed on 2026-08-02; states plainly that layer 2 as sited reaches 2 of 4 runtimes and
does not reach `gh pr merge` at all; adds detached-HEAD/no-commit preconditions to acceptance
criterion 1; and records that the ten documented-miss tests landed at commit `fc1be35b`, ahead of any
layer-2 work, so acceptance now builds on that corpus rather than re-proposing it.

> **BANKED 2026-08-02 (owner decision). No panel is scheduled and no implementation is planned.**
> The round-1 panel was answered in full, but the honest v2 covers **2 of 4 runtimes** (not codex)
> and does **not** reach `gh pr merge` at all (§4.6). Whether that earns its build cost is a scoping
> question, not a technical one, and the owner parked it rather than spend a second panel deciding.
>
> What holds the line meanwhile: **layer 1 is unchanged** and still catches the canonical spellings
> on all four adapters, and the ten documented-miss tests landed at `fc1be35b` so the known exposure
> cannot drift silently. Anyone reviving this should re-verify §2 against the code first — the
> runtime wiring is exactly what moved between v1 and v2.

**Status:** ~~draft for panel re-review (round 2)~~ **banked, see the note above**. v1 (`5a089604`) is superseded by this draft, not
amended in place — the review contract treats a "needs-changes" verdict as requiring a full revised
artefact, not a diff.

**Owner decisions taken (2026-08-02, Mark):**

- Shape **(c)+(a)** stands: gate the integration, not the command string. Endorsed in direction by
  all four round-1 seats; v1's implementability, not its direction, was the block.
- Shape **(b)** (remove raw `Bash`, typed merge tool only) was considered and **rejected** as a
  constitution-layer change revoking a granted, load-bearing shell capability. Not reopened here.
  One note for completeness: grok's review observed that (b) would not even close `eval`/script-file
  indirection on its own, so it would not have been a full fix even if reopened.
- **Reflog-window attribution** replaces ref-tip diffing as layer 2's enforcement primitive, on the
  panel's recommendation (cold-Opus, verdict lines 53-58).
- The ten documented-miss tests (§5.1) are **done**, at commit `fc1be35b`, ahead of any layer-2 work.

**Predecessors:** three pre-design panels (all block/unanimous), then a round-1 design panel
on `5a089604` (needs-changes, four seats — verdict and three seat reports recorded separately).

## Round-1 finding coverage

| RC id | source seat | what it required | where v2 addresses it |
|---|---|---|---|
| RC1 | cold-opus F1 | Correct §2's "pure function" claim; re-derive the seam choice from the corrected (temporal, not architectural) premise instead of inheriting v1's `PostToolUse` dismissal | §2 ("Corrected claim" + "architectural claim... is false" paragraphs) |
| RC2 | grok F2 / codex F1 / cold-opus F2 | State the emission gap plainly: layer 2's seam reaches 2 of 4 runtimes (Claude, pi), not codex; hard-gate any docstring re-label on per-runtime evidence | §3 ("Layer 2 — reflog-window attribution" runtime-coverage table and binding rule); reinforced by §5 criterion 6 |
| RC3 | cold-opus F5/F7, codex F3 | Replace ref-tip diffing with reflog-window attribution as one mechanism answering forks §4.2/§4.3/§4.4 | §3 ("Mechanism" steps 1-6); applied at §4.2, §4.3, §4.4 |
| RC4 | all four seats | `gh pr merge` is invisible to local ref observation; reflog attribution does not fix it — state an explicit residual rather than claiming coverage | **residual, not closed — §4.6**, also flagged in §5 criterion 1's scoping and criterion 6 |
| RC5 | cold-opus F6 | Add preconditions to acceptance criterion 1: `--no-commit` and detached-HEAD merges move no ref, so layer 2 is not a superset of layer 1 | §4.3 (measurement + precondition), applied at §5 criterion 1 |
| RC6 | cold-opus F10 | There are four gate adapters, not three — add pi throughout, including acceptance criterion 4 | §2 (adapter table adds the Pi row); applied at §5 criterion 4 |
| RC7 | cold-opus F9 | The parsing diagnosis accounts for only half the exposure; `MERGE_SUBCOMMANDS` scope is the other half, and the strongest undeployed argument for shape (c) | §1 ("Correction to the diagnosis" paragraph) |
| RC8 | cold-opus F8/F3 | Correct §4.5 with the 13.6ms measurement and drop the tool-kind narrowing (not expressible at the chosen seam) | §4.5 |
| RC9 | cold-opus F4 | Containment hazard: a layer-2 refusal inside `stream_turn` cannot simply raise; recommend quarantine + operator alert + CAS rollback, never automatic reset | §4.1 |
| RC10 | panel unanimous | Record the ten documented-miss tests as landed (commit `fc1be35b`), ahead of layer-2 work; acceptance builds on that corpus rather than re-proposing it | §1 ("These are no longer un-pinned" paragraph) and §5.1 (rewritten as history, not a gap) |

## 1. Why a fourth parsing fix is the wrong move — and what has already landed

`src/arb_warm_orch/gates.py:1-56` carries a MEASURED CAUGHT / NOT-CAUGHT contract. Its closing
paragraph (`gates.py:50-55`) names the root cause: *"this module re-implements shell parsing to
predict whether a merge will happen, and the input space is a shell grammar."* That diagnosis is
correct but **incomplete** — see the correction below.

**Live exposure, as measured for v1 and unchanged by this revision.** Every specimen below was run
through `evaluate_merge_close` with a resolver that never resolves, so anything merge-shaped MUST
deny:

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

**These are no longer un-pinned.** `tests/arb_warm_orch/test_merge_close_gate.py:390-412` now carries
a `DOCUMENTED_MISSES` corpus of **ten** specimens — the eight above plus `zsh -lc "git merge topic"`
(same combined-flags class) and `gh --help pr merge 12` (same gh-boolean-global class) — landed at
commit `fc1be35b`, ahead of any layer-2 work, exactly as the panel required unanimously. Each is
asserted `denied is False` with an empty code by
`test_documented_miss_still_slips_through` (`test_merge_close_gate.py:415-425`), and the corpus is
proven capable of failing — not just vacuously green — by a positive control:
`test_the_documented_miss_corpus_can_actually_fail` (`test_merge_close_gate.py:428-440`) asserts the
canonical `git merge topic` denies with its specific code. The control exists precisely because a
corpus of ten negative assertions passes just as happily against a gate that has been deleted
entirely (`test_merge_close_gate.py:429-436`); with the gate stubbed to always return no-opinion, all
ten documented-miss tests still pass and only the control fails — which is what makes the corpus a
check that can fail, not a check that only records intent.

The general corpus is `MERGE_SHAPED` (`test_merge_close_gate.py:309-335`, 21 entries, all denied) and
`NOT_MERGE_SHAPED` (`test_merge_close_gate.py:337-350`, 11 entries, none denied). None of the ten
documented misses appears in either — the CAUGHT list and the NOT-CAUGHT list are disjoint sets of
tests, not one list annotated after the fact.

**Correction to the diagnosis — this is a scope gap, not only a parsing gap.**
`MERGE_SUBCOMMANDS: frozenset[str] = frozenset({"merge", "pull"})` (`gates.py:72`) is a *module-level
pattern set on purpose* (`gates.py:11-13` discloses this as a deliberate doctrine choice, not an
oversight). So `git rebase`, `git cherry-pick`, `git am`, `git reset --hard`, `git branch -f`,
`git update-ref` and `git push` all return NO OPINION **by scope**, not because the tokeniser failed
to parse them — verified by reading `gates.py:72` and the absence of any of those subcommands from
`MERGE_SUBCOMMANDS`. "The input space is a shell grammar" is true of the parsing half of the exposure;
the other half is that the module was never asked to recognise these subcommands as integration acts
at all. This is the **strongest argument for shape (c)** and v1 never made it: ref/reflog observation
reaches all seven of these for free, because it asks "did an integration ref move" rather than "did
this command spell a subcommand I was told to watch for."

Three panels have now each found more spellings than the last, and two of the eight original misses
were *introduced* by the fix that closed the previous set. Continuing to chase spellings inside
`gates.py` is the wrong move for the reason v1 gave (open grammar) and for the scope reason above —
both point at the same fix: stop predicting the command and start observing the outcome.

## 2. What is actually wired today — corrected

The gate is one runtime-agnostic decision function with **four** wire adapters (v1 said three and
omitted pi):

| Layer | Where | Shape |
|---|---|---|
| Decision | `gates.py:327-343` `evaluate_merge_close(evidence, tool_name, tool_input) -> GateDecision` | runtime-neutral, repository-state-free — **not pure**, see below |
| Claude adapter | `gates.py:365-376` `build_merge_close_gate`, wired at `runner.py:126-130` as the **only** hook (`PreToolUse`) | deny / **no opinion**; ambient permission layers keep authority |
| Codex adapter | `codex_approvals.py:132-192`, gate call at `codex_approvals.py:156` | **blocking** server→client approval (`codex_approvals.py:3-7`); no ambient layer behind it, so "no opinion" falls through to the required `base_policy` |
| Grok adapter | `grok_runner.py:242-256`, gate call at `grok_runner.py:256` | `_meta."x.ai/hooks"` `blockingEvents: ["pre_tool_use"]` (`grok_runner.py:19-20`); we are the CLIENT so `session/request_permission` is ours to answer |
| **Pi adapter** | `pi_runner.py:261-283`, gate call at `pi_runner.py:271` | blocking host→client `tool/approve`; its own input shape (`params.get("args")`, `pi_runner.py:272`) and its own tool-name remap `_GATE_TOOL_NAMES = {"bash": "Bash"}` (`pi_runner.py:42`) |
| ACP server | `acp_server.py:9-15` | explicitly **not** a gate point — the consumer auto-approves `session/request_permission`; the decision "stays in the runner's PreToolUse hook" |

Refusal codes today: `merge-close-evidence-unresolved`, `merge-close-evidence-check-failed`
(fail-closed on resolver crash, `gates.py:333-338`).

**Corrected claim (was: "a pure function of a string").** `evaluate_merge_close` takes an injected
`EvidenceResolver` and calls it (`gates.py:334`), branching on the result and converting a resolver
exception into a denial (`gates.py:335-338`). It is not pure. The accurate and load-bearing claim is
**runtime-neutral and repository-state-free**: all four adapters supply command/tool-input shape, not
a repository snapshot (verified at `codex_approvals.py:119-129`, `grok_runner.py:253-256`,
`pi_runner.py:268-273`, and the Claude hook at `gates.py:368-371`).

**The architectural claim v1 built on this is false, and the seam choice has to be re-derived.**
Because `EvidenceResolver` already injects out-of-band state into every one of the four adapters by
the identical mechanism (an injected object with a `resolve` method), a `RefStateResolver` could be
injected at `PreToolUse` in exactly the same way — there is no wire-format or architectural rule that
keeps repository state out of a pre-execution approval. v1's §2 said "no approval wire carries
repository state, [so] the redesign is... a new layer at a different seam"; that inference does not
hold. **The real constraint is temporal, not architectural**: a pre-execution resolver can *snapshot*
repository state, but it cannot *compare* a before/after pair, because the tool call it is gating has
not run yet. Layer 2 still belongs after execution — but because comparison needs two endpoints and
the call in between, not because the wire cannot carry a ref map. This changes the justification, not
the seam: §3 below keeps layer 2 post-execution, on the corrected ground.

## 3. The proposed shape

Two layers. Layer 1 stays pre-execution and advisory; layer 2 is the actual enforcement, now defined
by **reflog-window attribution** rather than endpoint ref-tip diffing.

### Layer 1 — the pre-execution gate stays, and keeps not overclaiming

Unchanged from v1: keep `evaluate_merge_close` and all four adapters. The module's own docstring
(`gates.py:15-56`) already states the CAUGHT/NOT-CAUGHT contract as measured fact, not aspiration —
per `CLAUDE.md`'s *prose in code is a claim at the strength enforced*, that is the correct posture and
this design does not ask it to change.

Rationale, reinforced by two facts §1-§2 add on top of v1's "codex path is stronger than post-hoc
detection" argument: (a) layer 1 reaches subcommands and scopes layer 2 cannot improve on by itself —
n/a, see §4.6 for the genuine residual — and (b) §4.3 below shows layer 2 is **not a superset** of
layer 1 even within its own scope (`git merge --no-commit` and a detached-HEAD merge), so keeping
layer 1 is not a stopgap pending layer 2's completion; it covers cases layer 2 structurally cannot.

### Layer 2 — reflog-window attribution, the actual enforcement (scoped honestly)

**Runtime coverage — state this plainly, not as a footnote.** The only seam layer 2 can ride today is
`turn_events.ToolCallStarted` / `ToolCallCompleted`. Emission is verified per runtime:

| runtime | emits `ToolCallStarted`/`ToolCallCompleted`? |
|---|---|
| `runner.py` (Claude) | yes — `ToolCallCompleted` at `runner.py:191-195` |
| `pi_runner.py` | yes — `turn/toolFinished` → `ToolCallCompleted` at `pi_runner.py:307-311` |
| `codex_runner.py` | **no** — imports only `TextDelta`/`TurnEvent` (`codex_runner.py:34`); `_stream_until_complete` yields only `TextDelta` (`codex_runner.py:311-314`) |
| `grok_runner.py` | **no** — imports only `TextDelta`/`TurnEvent` (`grok_runner.py:36`); `_as_turn_event` handles only `agent_message_chunk` (`grok_runner.py:309-321`) |

**Layer 2 as sited reaches 2 of 4 runtimes — Claude and pi — and explicitly does NOT reach codex**,
the path v1's §4.1 said the containment decision hinged on, nor grok. Building layer 2 only against
this seam and describing it as cross-runtime enforcement would rebuild the three/four-adapter split
§3 exists to avoid, one layer down. This design does not propose extending `codex_runner.py` or
`grok_runner.py` to emit an equivalent lifecycle pair — that is implementation work with its own cost,
and whether to fund it now is recorded as an open question in §6, not decided here.

**Binding rule for implementation:** any re-label of `gates.py`'s docstring, or any claim in code
comments, from "layer 1 only" to "covered by layer 2" must be hard-gated on PER-RUNTIME evidence — a
runtime with no test proving layer 2 fires on it may never be described as covered. This is stronger
than v1's "don't re-label before green tests" rule: it requires the green tests to exist *for that
specific runtime*, not for the mechanism in the abstract.

**Mechanism.** For each allowlisted integration ref (see §4.2):

1. At `ToolCallStarted` for a call worth watching, record wall-clock `t_start`.
2. Let the call run to `ToolCallCompleted`, recording `t_end`.
3. Read `git reflog show <ref>` for each watched ref and select entries whose timestamp falls inside
   `[t_start, t_end]`.
4. Each selected entry's reflog message names the operation that moved the ref — `merge`, `pull`,
   `commit`, `reset`, `fetch`, `push`, etc. (cold-Opus's measurement, verdict lines 53-58; this design
   does not re-derive git's reflog format from source, since `git` itself is outside this repo's
   sources — it takes the panel's measurement as the mechanism's basis).
5. If any selected entry is merge/pull-shaped **and** no evidence resolves for it → **violation**:
   refuse to proceed, quarantine (§4.1), and surface the reflog entry as the reason.
6. A selected entry that is NOT merge/pull-shaped (an ordinary local commit, say) is not a violation
   even though the ref moved — this is what answers the fast-forward-vs-ordinary-commit
   indistinguishability finding: reflog labels the *operation*, not just the tip.

This one mechanism is what the owner adopted to answer forks §4.2, §4.3 and §4.4 together (per the
panel's recommendation) rather than three separate patches, because all three forks are really one
question — "did an integration happen inside *this* call's window" — and endpoint OID diffing cannot
answer it (two endpoint OIDs cannot distinguish "this call integrated" from "a concurrent session
did," and cannot see a movement that was reversed before the second snapshot).

**A gap this design surfaces and does not paper over.** Reflog-window attribution needs `t_start` and
`t_end` bracketing the call. `turn_events.ToolCallStarted` and `ToolCallCompleted`
(`turn_events.py:42-62`) carry no timestamp field today — verified by reading the dataclasses in
full. Either the seam gains timestamp fields, or the observer takes its own wall-clock reading at the
moment it receives each event (looser, since event delivery can lag the underlying call, but usable
if the window is padded). This is new state, not existing infrastructure, and is scoped as
implementation work, not decided here.

## 4. Forks

### 4.1 Containment — resolved to halt + quarantine, never automatic reset

A refusal cannot un-merge; the merge is already in the object store by the time `ToolCallCompleted`
fires. `runner.py:200-214` documents a hazard directly relevant here: session-id persistence happens
*inside* `stream_turn`, and a caller that abandons the generator early never reaches it (this was a
previous panel's P0). A layer-2 refusal cannot simply raise out of the generator mid-stream without
risking exactly that class of failure again — it has to unwind through a path that still lets
`stream_turn` reach its `ResultMessage` handling, or accept that the channel's session id does not
advance on a quarantine turn.

Resolution, adopted from the converged containment proposal (codex-sol's refinement, endorsed in
direction by grok and agy):

1. Preserve the unexpected tip under `refs/arb/quarantine/<run>/<ref>` rather than discarding it.
2. Stop the orchestrator (halt further execute-kind tool calls this turn), with an operator-visible
   record carrying the *specific* violation code — never a bare refusal
   (`docs/defect-classes/refusal-is-ambient-assert-the-code.md`).
3. Roll a protected ref back only with **compare-and-swap** semantics (`git update-ref <ref> <old>
   <observed-new>`), so a concurrent legitimate update between detection and rollback is never
   silently overwritten.
4. Release only via a resolved evidence artefact — the same shape the resolver already uses.

**No automatic unconditional `git reset`.** The object store is shared across six worktrees; an
unconditional reset is a destructive action taken on a signal (§4.5 notwithstanding) that is not yet
proven reliable across concurrent writers.

For the remote (`gh`) case this containment story does not apply at all — see §4.6.

### 4.2 What counts as an "integration ref"

Reflog-window attribution answers *whether a movement inside the window is an integration*; it does
not by itself answer *which refs to watch*. Three of the four round-1 seats converged on the same
default independently (agy, grok, codex-sol): an explicit allowlist of protected integration targets
— `refs/heads/main`, `refs/heads/dev`, and their remote-tracking counterparts — with worker branches
(`refs/heads/worktree-*`, task branches) excluded by construction, not by a runtime check. This design
adopts that default. It remains a configuration surface at implementation time (whether release
branches join the allowlist, for instance), not a genuinely contested technical fork.

### 4.3 Detached HEAD, fast-forwards, and non-commit integrations — layer 2 is not a superset of layer 1

Measured (cold-Opus, verdict lines 49-52; not independently re-executed in this round since it
requires running `git` commands outside this repo's sources): `git merge --no-commit` moves **no**
ref, and a **detached-HEAD** merge moves no *branch* ref at all — at review time, 2 of 6 live
worktrees were on detached HEAD. Layer 1 **does** catch both spellings, because it matches the command
text regardless of ref/HEAD state (`git merge --no-commit topic` denies with
`merge-close-evidence-unresolved` the same as any other merge-shaped command). So **layer 2 is not a
superset of layer 1** — this is the correction to v1's implicit framing of layer 2 as "layer 1 plus
more," and it strengthens §3's decision to keep layer 1, for a better reason than v1 gave (which was
only "codex has no ambient layer behind it").

**Precondition, made explicit rather than silently assumed:** acceptance criterion 1 (§5) applies to a
*committing* merge, on a *non-detached* HEAD, on an allowlisted branch. A fast-forward counts (the
ref tip moves, and reflog records it as such). `--no-commit` and detached-HEAD merges are a residual
that layer 2 structurally cannot close — closing them, if wanted, is layer 1's job (string-shaped
detection already does it) or a distinct HEAD/index observer, which this design does not propose.

### 4.4 The race — closed by reflog-window attribution, not by endpoint comparison

v1 flagged this as live and unresolved. It is the fork reflog-window attribution most directly
answers: from two endpoint OIDs alone, "this call moved `dev`" and "a concurrent session moved `dev`"
are observationally identical, and a movement-then-reversion inside the interval is invisible to an
endpoint diff. Windowing by `[t_start, t_end]` and reading every reflog entry in that interval —
rather than only the final OID — sees every transition, and each transition's reflog label
distinguishes an integration from an unrelated commit, fetch, or push. `git push origin main` was
measured to move `refs/remotes/origin/main` locally (verdict lines 53-55), which is why "local refs
only" and "include remote-tracking" were both broken candidates in v1: the former is blind to push,
the latter re-opens the race on every fetch. Reflog-window attribution needs neither restriction,
because it attributes by *operation type within the window*, not by *which ref namespace*.

This does not eliminate every race — two genuine integrations by two different sessions landing in
the same window on the same ref would both appear as merge-shaped entries, and the design does not
propose adjudicating which one "owns" the window; both would need to resolve evidence, which is the
conservative (fail-closed) outcome.

### 4.5 Cost — corrected, and the tool-kind narrowing is dropped

v1 estimated a ref snapshot as "likely unacceptable" around every tool call and proposed narrowing to
`execute`-kind calls as mitigation. Measured (cold-Opus, verdict lines 59-60; not independently
re-executed in this round): a ref snapshot costs **13.6 ms mean over 20 runs** on this repository.
"Likely unacceptable" is unsupported by that measurement, and the narrowing it motivated should be
dropped for a second, independent reason: it is not expressible at the chosen seam.
`ToolCallCompleted` carries only `(tool_call_id, status)` (`turn_events.py:56-62`) — no `kind`, no
tool name, no input. Only `ToolCallStarted` carries `kind` (`turn_events.py:42-53`, and
`tool_kind` mapping `Bash` → `execute` at `turn_events.py:21-32`). Narrowing by tool kind at
completion time would require correlating each call's `ToolCallStarted.kind` forward to its matching
`ToolCallCompleted` and holding that association as new per-turn state — state that does not exist
today (the same gap noted for timestamps in §3). Given the cost is not the blocker, this design
proposes snapshotting reflog on every allowlisted-ref-adjacent completion rather than building that
correlation state solely for a cost optimisation that the measurement does not justify.

### 4.6 The residual this design does not close: `gh pr merge`

**State this plainly rather than let it read as covered, per all four round-1 seats.** `gh pr merge`
(and `gh pr close`) mutate authoritative state on GitHub's servers directly; the merge need not move
any local ref, and typically will not unless a subsequent `fetch`/`pull` runs inside the same tool
call. Reflog-window attribution observes **local** git operations — it does not fix this, because
there is nothing local to observe. v1's §3 claimed all eight NOT-CAUGHT specimens "collapse to the
same observable" once the question becomes "did an integration ref move"; that claim is **false** for
the two `gh`-family specimens and this revision withdraws it for that class.

What this design leaves as the honest state of `gh` coverage:

- **Layer 1** already catches the canonical spellings (`gh pr merge`, `gh pr close`, with or without
  `--repo`/`-R`) via string matching (`gates.py:264-288`, tested at
  `test_merge_close_gate.py:325-332`).
- The **boolean-global-flag misses** (`gh --verbose pr merge`, `gh -v pr merge`, `gh --help pr merge`)
  are in the landed documented-miss corpus (§1) and remain live exposure at layer 1; layer 2 does not
  reach them either.
- Closing this residual for real needs a **remote observer** — `git ls-remote` (or equivalent
  provider-state query) bracketing the call the same way reflog brackets local refs, or server-side
  branch protection, or an evidence-gated promotion workflow. All three are outside this design's
  frame; codex-sol's position (round-1 review) is that post-hoc *local* code cannot honestly claim
  containment for a remote-authoritative merge, and this revision agrees rather than reopens the
  argument.
- Whether to fund a remote observer now, defer it, or accept the residual as permanent (with the
  boolean-flag misses staying pinned-but-uncaught) is recorded as an open question in §6.

## 5. Acceptance criteria

The bar is falsifiable and now scoped honestly to what layer 2 can actually reach.

1. **Every NOT-CAUGHT specimen in §1 that is local-git-shaped (i.e. every one except the two `gh`
   boolean-global-flag families and the canonical `gh pr merge`/`close`, which are §4.6's residual) is
   DETECTED by layer 2 on the Claude and pi runtimes**, each as a test that fails against layer 2
   removed, subject to the precondition in §4.3: a committing merge, on a non-detached HEAD, on an
   allowlisted ref.
2. **Each test asserts the specific violation code**, never a bare refusal
   (`docs/defect-classes/refusal-is-ambient-assert-the-code.md`).
3. **A negative corpus** — authorised worker commits on worker branches, and the concurrent-`dev` race
   of §4.4 — produces **no** violation, verified against reflog-window attribution (not endpoint
   diffing, which cannot pass this criterion and criterion 1 simultaneously, per the round-1 panel's
   Converged P1 class 3).
4. **Layer 1's CAUGHT list is unchanged** — no regression on the codex blocking path **or the pi
   blocking path** (v1 named only codex; pi is a fourth adapter over the same `evaluate_merge_close`
   and belongs in this criterion too, per §2).
5. `gates.py`'s docstring stays re-derived by running the corpus, not edited by hand — this is already
   the case at the time of this revision (`gates.py:15-56` is dated 2026-08-02 and post-dates three
   panels); this criterion is a standing constraint on future edits, not new work.
6. **Any docstring or comment claiming layer-2 coverage is scoped per runtime** (§3's binding rule) —
   a runtime with no passing layer-2 test for it may not be described as covered, and `gh pr merge`
   may never be described as covered by layer 2 at all.
7. **Containment is exercised, not just documented**: a test drives a violation through to a quarantine
   ref under `refs/arb/quarantine/<run>/<ref>`, confirms the orchestrator halts further execute-kind
   calls that turn, and confirms a concurrent legitimate ref update during the CAS rollback window is
   not overwritten (§4.1).

### 5.1 The documented-miss tests are DONE — this section now records history, not a gap

v1's §5.1 reported, correctly, that none of the eight then-known misses had any test coverage and
estimated the work as eight new tests plus harness. That work landed first, ahead of any layer-2
implementation, exactly as the round-1 panel required unanimously ("split §5.1's eight documented-miss
tests into their own change and land them first"):

- `tests/arb_warm_orch/test_merge_close_gate.py:390-412` — `DOCUMENTED_MISSES`, ten specimens (the
  original eight plus `zsh -lc` and `gh --help pr merge`, same classes as their siblings).
- `test_merge_close_gate.py:415-425` — `test_documented_miss_still_slips_through`, parametrized over
  the ten, asserting `denied is False` and `code == ""` for each. The block comment immediately above
  it (`test_merge_close_gate.py:384-388`) instructs that a failure here is *probably good news* — the
  gate now catches that spelling — and directs the fix to move the entry to `MERGE_SHAPED`, never to
  relax the assertion.
- `test_merge_close_gate.py:428-440` — `test_the_documented_miss_corpus_can_actually_fail`, the
  positive control. Its own docstring states the reason for its existence: a corpus of ten negative
  assertions would stay green even against a gate stubbed out to always return no-opinion, so the
  control (asserting the canonical spelling denies, with its specific code) is what makes the corpus
  a check that can fail rather than a check that only records intent.

Acceptance criteria 1-4 above build on this corpus — the `MERGE_SHAPED` (21 entries) and
`NOT_MERGE_SHAPED` (11 entries) sets at `test_merge_close_gate.py:309-350` are the CAUGHT regression
net criterion 4 protects, and `DOCUMENTED_MISSES` is the residual criterion 1 must not silently shrink
without a corresponding entry moving to `MERGE_SHAPED`.

## 6. OPEN

These are genuinely open — decisions this design cannot make on its own, or facts this round could
not verify with the available tooling.

- **Fund codex/grok emission parity, or not?** Making layer 2 reach all four runtimes needs
  `codex_runner.py` and `grok_runner.py` to emit an equivalent start/completion lifecycle pair (or an
  internal before/after observer inside each runner's own execute path, which re-splits the adapter
  layer §3 wants to avoid). Cost: two runner modules gain new emission logic and new tests; until it
  lands, layer 2 provides no enforcement gain on the codex path, which is the path v1's own §4.1 said
  the decision hinges on. Options: (a) fund it now, sequenced before layer 2 ships on any runtime, so
  "covered by layer 2" is never claimed prematurely; (b) ship layer 2 for Claude+pi now, track codex
  and grok as a named follow-on, and hold the docstring-scoping rule (§3, §5 criterion 6) as the
  interim honesty mechanism; (c) decide layer 2 is not worth building until all four runtimes can
  carry it, and re-scope this design to (a) alone plus the residual documented-miss discipline. This
  design does not pick among them.
- **Fund a remote observer for `gh pr merge`, or accept the residual?** §4.6's options (`git
  ls-remote` bracketing, server-side branch protection, evidence-gated promotion) each carry design
  and operational cost this note has not sized. Accepting the residual means the two `gh`
  boolean-global-flag misses (and any future `gh` spelling gap) stay permanently in
  `DOCUMENTED_MISSES`, caught by nothing beyond layer 1's string match for canonical forms.
- **Timestamp fields on `turn_events`.** Reflog-window attribution needs `t_start`/`t_end` bracketing
  a call; `ToolCallStarted`/`ToolCallCompleted` carry neither today (`turn_events.py:42-62`). Adding
  them is a small, contained change, but it is new state this design surfaces rather than assumes.
- **Whether layer 2's violation is expressible on the codex `decline` wire at all**, carried over from
  v1 unresolved: `codex_approvals.py:22-30` records that the new-gen
  `CommandExecutionRequestApprovalResponse` has exactly one property (`decision`), with nowhere to put
  a reason, so a code cannot travel to the model on the live path even once codex emits the events
  layer 2 would need. The existing mitigation — append to `records` regardless (`codex_approvals.py:
  184-191`) — likely applies unchanged, but this is moot until the emission-parity question above is
  settled.
- **Integration-ref allowlist specifics** (§4.2) — `main`/`dev` plus remote-tracking counterparts is
  this design's default; whether release branches or other long-lived integration targets join it is
  a configuration decision for implementation, not a technical fork.
- **Sequencing against `main`.** v1 noted `main` was several commits behind `dev` and still carried the
  §1 bypasses at review time; this round did not re-verify `main`'s current state (no shell tooling
  available to this author round), so the gap between `main` and `dev` is unconfirmed as of this
  draft and should be re-checked before implementation is sequenced.

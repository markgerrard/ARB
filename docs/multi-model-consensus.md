# Multi-Model Consensus for Design Decisions

> Cross-references: `quorum-decision-taxonomy.md` (decision outcome states + override discipline),
> `evidence-first-remediation.md` (verify before you act on any finding), `orchestrator-patterns.md`
> (Pattern E dual-review / parallel dispatch).

## What this is

When you face a **design or remediation decision with more than one defensible option** — not a
mechanical task, not a verification pass — dispatch the *same decision brief* to several
independent models and synthesise their reasoning before committing to a fix. The value is not the
vote; it is (a) catching an option you didn't think of, and (b) surfacing where the reviewers
*disagree*, which is exactly where the real risk lives.

This is distinct from the review panels in `pipeline-operating-manual.md`: those review code that
already exists. This pattern is invoked **earlier** — to choose *what to build* when the path forks.

## The panel

Three independent seats, chosen so no two share a failure mode:

| Seat | How | Why it's there |
|---|---|---|
| **codex** | bridge dispatch (`--engine codex`) | strong implementation-grounded judgement; reads actual code when explicitly pointed to it |
| **agy** (Antigravity, Gemini-family) | `agy --print "$(cat brief.md)"` direct CLI | different model family → different priors; cheap |
| **cold-Opus subagent** | the Agent tool (`general-purpose`), NOT an inline self-review | fresh context, no investment in the work so far; the most likely to reject all your framed options |

Rationale for the third seat being a **fresh subagent, not your own inline opinion**: the
orchestrator has already spent context converging on a framing. An inline "and here's what I think"
inherits that bias. A cold subagent with only the brief is the one that says "all three of your
options are wrong, here's a fourth." (That is exactly what happened the session this doc was written
from — see Worked Example.)

> One model family per seat. Running the same family twice (e.g. agy *and* the since-deprecated
> gemini-acp bridge) is redundant — drop the duplicate. agy is the Gemini-family seat.

Family decorrelation is not the only axis that matters. In Worked Example 2, three different model
families shared the identical blind spot — what saved the panel was a seat whose *tool profile*
differed (no web-fetch, so it fell back to reading the code) rather than its model family. When the
subject is external-vs-current, consider deliberately including a seat without the external-fetch
path.

### Cold-Opus substitution when the orchestrator cannot spawn native subagents

A non-Claude orchestrator (codex, pi/glm, agy driving the pipeline) has no `Agent` tool, so the
panel's Anthropic seat becomes the **bridge-seated Opus** (`--engine agent-sdk`, target
`asdk-<project>-<workspace>-opus48`) instead of a native cold-Opus subagent. Two documented
caveats, both real:

1. **Fail-closed tool ceiling.** The agent-sdk seat cannot exec or write outside its repo, which
   caps verification depth below the native subagent. Findings still count; expect fewer
   reproduce-level confirmations. Prefer the native subagent whenever the orchestrator can spawn
   one.
2. **Child-env credential exposure (panel-p2-driver-engine-design-20260720T184935Z-74ae6c,
   codex F2).** `isolated_env` blanks only `ANTHROPIC_*`/`AGENT_SDK_*`
   (`engines/agent_sdk_models.py`) and passes the rest of the daemon environment through —
   including `*_REDIS_URL`/`*_BUS_URL`, which `scrubbed_child_env` strips for every other engine
   family. Until the scrub is extended to the agent-sdk lane and probe-verified, treat asdk seats
   as credential-bearing: acceptable as REVIEW seats, excluded from any role that publishes or
   whose containment story assumes a credential-free child (e.g. FABA production rounds).

## The brief

Write ONE decision brief to a file and give all seats the identical text (brief-to-file pattern,
`using-agent-bridge`). It must contain:

1. **System context** — enough that a cold reader understands the constraint.
2. **Baseline verification pointer** — treat every sentence in your system context that describes
   how the system behaves *today* as a claim under test, not scene-setting; if you're unsure
   whether a statement counts, treat it as one (don't leave the classification to the reviewer —
   that's the same judgement that fails). For each, name the specific file(s)/function(s) — not a
   directory, module, or "read the codebase" — that would prove or refute it, and require each
   reviewer's reply to state what they found there: confirms, contradicts, or couldn't check. "How
   we do X today" gets the same evidence-first bar as the problem statement; it just doesn't read
   like one, because it's phrased as background rather than the subject under test, so reviewers
   spend their verification budget on the visibly-novel material and take the rest on faith. See
   Worked Example 2.
3. **The problem, with MEASURED EVIDENCE** — not "it's flaky" but "~20% of first attempts emit
   out-of-range markers; retry masks to ~3%; here are the rates." (evidence-first.) A brief built
   on a summary produces opinions built on a summary.
4. **The options on the table** — each with its own stated risk, so reviewers critique your risk
   assessment, not just pick a letter.
5. **Explicit invitation to propose a 4th option** and to rank. Without this, models anchor to
   your list and you lose the main benefit.
6. **Specific questions** — "is option 1's risk real?", "what are we missing?" — so the replies are
   comparable, not free-form essays.

## Synthesis (the orchestrator's job)

- **Convergence on a NEW option** is the strongest signal. If all three independently invent the
  same 4th approach, it is almost certainly right — it means the obvious-to-an-outsider answer was
  invisible from inside the work.
- **Convergence on rejecting an option** is nearly as strong — especially a unanimous reject of one
  that touches a core invariant.
- **Check the baseline check before trusting convergence.** If the brief carried a baseline
  verification pointer (item 2), a reply that never states what it found in the named file(s)
  hasn't done the check — weight that seat as opinion, not evidence, on any claim about the current
  system, and say so in the roll-up rather than silently counting it. This is why convergence isn't
  automatically strong signal: if all seats share a factual premise none of them independently
  verified (the brief's account of the current system), their agreement measures your framing, not
  the truth. Count convergence only over what seats actually grounded (Worked Example 2).
- **Disagreement** is a STOP, not a tie to break by counting. Per evidence-first's disagreement
  rule, a single seat with contradictory *evidence* outweighs the other two's *opinion*. Investigate
  the disagreement before proceeding — Worked Example 2 is a case where a single seat's inspection
  of the actual code overturned a 3-seat convergence.
- **Read every seat's reasoning, not just its ranking.** Two seats can reach the same answer for
  different reasons; the union of their reasons is the actual decision input. Relay the synthesis to
  the human with each seat attributed — do not flatten it to "the panel said X".
- The orchestrator still decides and still owns it. Consensus is an input, not an authority
  (`quorum-decision-taxonomy.md`: the mechanism produces a safe decision, not a vote tally).

## When to use / not use

- **Use:** forked design choices, remediation-approach selection, "fix the cause vs ship the
  mitigation" calls, anything where picking wrong is expensive to unwind. Especially when you've
  already tried one fix and it didn't fully work (you are now biased; outside eyes help).
- **Don't use:** mechanical tasks, verification of existing code (that's a review panel), or
  decisions cheap to reverse. The panel costs 3 dispatches + synthesis; spend it where the decision
  is load-bearing.

## Review hygiene — keep reviewers independent (or you lose the whole point)

During an INDEPENDENT review phase, no reviewer may be able to read another reviewer's report. If
they can, the panel quietly collapses into an echo chamber and you've paid 3× for one opinion.

The non-obvious failure mode: **the bridge engines and any in-session subagent reviewer share the
same git checkout.** Bridge engines run with `AGENT_WORKDIR` pointing at the repo; a Claude
`general-purpose` subagent reads the same working tree. So if reviewer A writes its report *into the
repo under review* (e.g. `docs/.../review-A.md`) while reviewer B is still running, B can `ls`/glob
and read A's findings. Writing the report inside the repo-under-review is the leak — even though the
dispatch briefs never mention each other.

**The rule:**
- Each reviewer writes to a path OUTSIDE the repo under review (`/tmp/review-<engine>.md`) until
  *every* reviewer has finished. Then move the reports into the repo together. Put the out-of-repo
  destination in each dispatch brief.
- A subagent reviewer in the same session is subject to the same rule — give it an out-of-repo
  destination too, or run it strictly before/after the bridge engines with zero overlap.
- Relax this ONLY when the phase is explicitly *convergence* — i.e. you WANT reviewers reacting to
  each other (a second round debating the first round's findings). Independent first-pass and
  convergence are different phases; don't blur them.

**Structural fix — dispatch each reviewer with `--worktree`.** The leak's root is the *shared
checkout*. `agent-dispatch --worktree <name>` runs each bridge reviewer on its own git worktree
(cwd = a separate checkout), so a peer's report — written inside *its* worktree — isn't surfaced by
the ordinary workspace-scoped `ls`/glob/grep a reviewer runs from its own cwd. That closes the
*accidental* read-leak (the realistic failure: a reviewer globbing the repo's reviews dir and
stumbling on a peer's file). It is **not** a filesystem sandbox — the worktrees share a UID and
filesystem, so a reviewer that deliberately walks `../` or reads an absolute path can still reach a
sibling; treat it as accident-prevention, not a confidentiality boundary. Prefer it for any
concurrent independent-review panel: give each of the N reviewers `--worktree review-<engine>` off
the same base ref (keep the default `--worktree-cleanup keep` so the in-worktree reports survive to
be collected — `auto` would remove them). This is the *read-leak* sibling of the *write-collision*
hazard `--worktree` was built for — one primitive, both faces of the shared-checkout class closed.
The out-of-repo `/tmp` discipline above is then redundant for bridge reviewers writing inside their
own worktrees, and stays the documented fallback for any dispatch not using `--worktree`. Caveat: a
native in-session subagent reviewer (e.g. cold-Opus via the `Agent` tool) is **not** a bridge
dispatch and gets no worktree from the bridge — keep it on the out-of-repo rule, or brief it to read
only the diff and write to an out-of-repo path.

**Detecting a leak after the fact:** scan each reviewer's reasoning trace for mentions of another
reviewer's file. A leak doesn't always invalidate the panel — if each reviewer's *lead* finding is
independently derived (different top issues, grounded in the code not the peer report), the findings
still hold. But treat that as luck, not process.

Real incident (project-g M2 plan review, 2026-05-31): a cold-Opus subagent and a codex bridge
dispatch ran concurrently on a shared checkout; codex finished first and wrote its report into the
repo's reviews dir, and the subagent's trace showed it had seen codex's report. The panel's value
survived only because the subagent's lead finding (a "green-but-dead" wiring gap where a field was
never populated and no test could catch it) was one neither codex nor the third seat raised —
independently derived. The process was fixed afterward; don't rely on the same luck.

## Vote gaps — verify the gap is real, then re-fire

A seat with an apparently empty report or log is **not yet a vote gap** — declaring one
prematurely silently shrinks the panel, and a missing decorrelated seat is exactly the one
whose absence you must not paper over.

Before declaring a gap, run the re-check (from the 2026-07-05 pi-orchestrated run, where an
ASDK/Opus seat's stdout looked empty at panel close and a later re-read found a full review
and vote — a would-be false gap):

1. Check the log/report **file size**, not just its apparent content — seats can late-populate
   output after appearing done.
2. Check the seat's **process status** — a live process with a frozen output offset and no
   error is a tailer/state bug (the seat may still be working), not an absent seat.
3. Read **stderr**, then re-read stdout/the report file.

If after that the seat genuinely returned no verdict/vote: record a **named absent vote**
(who was missing and why, in the roll-up) and **re-fire the seat**. Only if the re-fire also
fails may you close on a reduced roster — and then the verdict states the reduction
explicitly. Never average an absent seat away or count the survivors as if they were the
panel.

## Worked example (project-g M1 citation flakiness, 2026-05-30)

- **Problem, with evidence:** Gemini 2.5 Flash-Lite emitted out-of-range citation markers (`[11]`,
  `[195]`) because retrieved chunk text contained "paragraph 11" etc.; the strict composer rejected
  → safe degrade. Measured: ~20% first-attempt fail; retry → ~3%; a `<source id>` structural label
  did NOT remove it (marker-level diagnostic still ~20% first-attempt).
- **Brief:** 3 options (neutralise chunk text / accept ~3% for the milestone / composer post-process
  remap), each with stated risk, plus an explicit "propose a 4th, rank them".
- **Panel:** codex (bridge) + agy + cold-Opus subagent, same brief.
- **Result:** all three INDEPENDENTLY rejected the three framed options and converged on a **4th**:
  the failure is a *namespace collision* (citation markers and content share the integer alphabet),
  so give sources a **disjoint marker namespace** — `[S1]..[SN]` (codex + cold-Opus, to dodge
  planning Use-Class letters A–F) or `[A]..[H]` (agy). Unanimous: that removes the cause without
  mutating grounding content or weakening the strict invariant; the text-mangling option was
  "avoid", the composer-remap option a "strong reject" (violates the grounding invariant).
- **Decision:** implement the 4th option; keep the existing retry as residual defence; merge only
  after the marker-level diagnostic shows first-attempt out-of-range ≈ 0. None of the orchestrator's
  three options shipped — the panel's value was the option none of them had written down.

## Worked example 2 (ARB-2026-07-OBSVAULT-R1, 2026-07-05) — the baseline-verification gap

- **Setup:** a 4-seat design-adoption panel (codex, agy, cold-Opus subagent, plus an ad-hoc 4th
  pi-GLM seat) reviewed four patterns proposed for adoption from an external repo. The brief's
  "system context" section described ARB's retrieval as "the current pgvector pipeline" and asked
  reviewers to judge one pattern's expected gain against it.
- **What happened:** codex, agy, and cold-Opus each spent their tool budget verifying the
  *external* repo (README, linked docs, WebFetch/search) — genuinely rigorous verification, just
  aimed entirely at the novel subject. None of the three read ARB's own retrieval code. The 4th
  seat (pi-GLM) had no web-fetch tool in its harness, so it fell back to reading ARB's own
  `src/arb_memory/store.py` to ground its answer — and found that ARB *already* fuses pgvector
  cosine similarity with PostgreSQL full-text (`tsvector`) search via reciprocal-rank fusion (RRF),
  which the brief's "current pgvector pipeline" framing had understated. This materially changes
  the pattern's cost/benefit: the marginal gain from adding another retrieval mechanism on top of an
  *already-fused* pipeline is a smaller bet than the brief's framing implied.
- **Why it wasn't caught by the brief's own instructions:** the brief already required each
  reviewer to "list any claim in this brief... your direct inspection contradicted or could not
  substantiate" — an instruction that, read literally, covered the system-context section too. All
  three capable seats still missed it. Claims phrased as scene-setting get a pass from
  verify-don't-trust discipline that claims phrased as the subject under test don't, even when the
  instruction's wording technically covers both.
- **Fix:** promoted "read the code, not the brief's description of it" to its own required brief
  element (item 2, above), rather than trusting it to be inferred from a general verify-don't-trust
  instruction — with two changes beyond just adding a pointer: the trigger defaults to "assume it's
  a claim" instead of relying on the author to recognise one (the author's recognition is exactly
  what failed here), and reviewers must report what they found at the pointer, not just be told to
  check it, so compliance is auditable at synthesis time instead of indistinguishable from a skip.
- **Lesson:** an instruction that technically covers a failure mode is not the same as a brief
  structured so reviewers actually apply it there. When a brief's own doctrine catches a real gap
  only once, by accident, that's the signal to make the missing check structural, not to trust the
  instruction to work harder next time.

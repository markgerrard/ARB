---
name: autonomous-mode
description: Use when the user wants hands-off, walk-away delivery of a change — define the feature, approve the spec, and review the executed run + decision log the next morning — or says "autonomous mode", "ship this overnight", "run unattended", "take this end-to-end and I'll review in the morning". Repos using the agent-redis-bridge multi-agent pipeline.
---

# Autonomous Mode

## Core principle

Autonomous mode is **hands-off after spec approval.** You define the feature and approve the spec, then walk away. The panel implements, resolves its own design forks, logs every non-trivial decision to `decisions.md`, and either **merges** (reversible work) or **stages on a branch** (irreversible / safety-posture work) — it does **not halt and wait for you.** You are a **next-morning auditor of an already-executed run**, not a participant in it.

The safety spine is three things — none of them a human in the loop:

1. **Worktree / branch reversibility** — most code changes are reversible by not merging.
2. **The decision log** (`decisions.md` — shorthand throughout for the **run-scoped** file `docs/decisions/<topic>.md`, one per feature/run, never a single shared file; see § Decision log) — every non-auto decision records the question, the options considered, the **rejected alternatives + why**, and the chosen direction. Reviewable cold, next day, with no memory of the run.
3. **Drift-against-spec check** — the failure mode hands-off is *most* exposed to is the run drifting from your spec over many locally-reasonable decisions. A cold-Opus spec-conformance pass over the full diff is the morning-review centerpiece (and a gate signal).

This is a stricter-gated, hands-off variant of the A/B pipeline in `docs/pipeline-operating-manual.md`. It runs on a single `feat/<topic>` branch; Workflow B's per-task branches are intentionally dropped.

## Decision procedure (quick reference — navigation aid; the prose governs on conflict)

A condensed index into the rules below, for the orchestrator mid-run. **It does not replace the prose — every node cites the § that governs, and on any conflict, or any case this list does not cover, the prose wins.** Running this list without the reasoning behind it (the asymmetries) will mishandle the novel case; when in doubt, fall back to the section.

**At kickoff (Stage 0):** declare the two **autonomy preconditions** (§ Stage 0 — Autonomy preconditions): `main-inert?` (default undeclared/unknown → **NOT inert**) and `decorrelated-seat?`. Surface the consequence to the user *before* walk-away.

**For every change AND every decision** (§ Change classification, § Posture detection):
1. **Posture-class?** — touches how-it-defends / who-can-do-what / how-the-pipeline-governs-itself / what-data-leaves / blast-radius (§ Posture detection). **Unsure → yes → park.** A guardrail/gate/quorum config edit is posture-class on sight.
2. **Irreversible signature?** — migrations, DDL, DROP/DELETE/TRUNCATE, deploys, publishes (§ table); **plus: if `main` is not inert, every merge to `main` is a deployment** → carries the signature. → park-don't-halt.
3. Neither → **auto-eligible**.

**For a parked posture decision** (§ Park-don't-halt matrix): hold-state? (unsure → NO) × dependents? → the binding cell (no hold-state + dependents) **blocks the subtree, builds nothing provisional, continues the independent rest, stages the feature whole.**

**Every review↔remediation loop** (§ Loop bound): 3 usual → park (unless logged-converging); 5 → park unconditionally.

**Budget** (§ Global budget): before every dispatch / merge / push, check the wall-clock ceiling; exceed → park everything (`parked-budget-exhausted`).

**At the gate (Stage 5):** 3/3 APPROVE over the *final* diff + **no** irreversible signature (incl. the main-not-inert promotion) + **no** parked task of any kind anywhere in the feature → merge `--no-ff`. Else stage whole. No decorrelated seat → judgment tier **parks** (`parked-unverified-posture-judgment`), never runs correlated.

## When to use

- User wants walk-away delivery: approve spec, leave, review the run next morning.
- The change has crisp, independently-reviewable acceptance criteria.

## When NOT to use

- Open-ended exploration ("what should we do about X?") → brainstorming.
- Production incident / time-critical fix → fix directly.
- Cross-system coordinated deploys a single agent cannot see end-to-end.
- Work that cannot be staged on a branch (a live cutover you must execute, not propose) → out of scope; needs a human present.

## Change classification — the gate that decides merge vs park (closed taxonomy)

Every change **and every design decision** is exactly one of:

| Bucket | Includes | Action | Morning review |
|---|---|---|---|
| **Auto-eligible** | reversible code; design decisions that don't touch safety posture | resolve → execute → log → tri-model gate → **merge** | record review (sample via drift check) |
| **Park-don't-halt** | irreversible-signature code (migrations, schema DDL, `DROP`/`DELETE`/`TRUNCATE`, re-encryption, deployments/launchd/systemd/plist, external publishes); **when `main` is not inert (Stage-0 declaration, or the undeclared/unknown default), every merge to `main`** (the merge *is* a deployment → it carries the deployment signature); **or decisions that touch safety posture** (per § Posture detection — the park trigger) | record **hold-state** + **dependents**; act per the **park matrix** (below); the run continues but **never merges around a hole** | sort by **park reason**; decide / debug / approve as appropriate |
| **Hard-halt** | proceeding wrong is unrecoverable **and** un-stage-able | stop, log, surface in morning digest | decide next steps |

**When in doubt, classify up** (auto → park; park → hard-halt). Design goal: keep hard-halt as close to empty as the work allows.

**Park-don't-halt continues; it does not stall.** If a parked item is on the critical path and blocks *all* remaining work, the run stalls for that item (surfaced in the morning digest) — that's the implicit hard-halt case, and it is acceptable. Never auto-merge a parked item just to unblock.

## Posture detection — what makes a decision posture-class (the park trigger)

The park machinery is only as good as this predicate: a posture decision that isn't *recognised* as one routes to auto-eligible and **merges unattended** — the matrix never fires. Unlike the irreversible-signature scan (a literal pattern match on the diff), posture lives in the *decision*, not the text, so it needs an explicit trigger list plus a conservative tie-break. **The miss-mode is silent (wrong merge), not loud (stall) — so this predicate breaks toward over-detection every time.**

This predicate catches **commission** — the run actively makes a posture-class decision. It does **not** catch omission, inherited defaults, or emergent composition; those are end-state failures, caught downstream by the § Drift-against-spec pass — its spec-relative posture-conformance check (to the extent the **Stage-0 posture-baseline gate** forced the posture to be *stated*) **plus** its spec-independent **posture oracle** (for the recurring classes, regardless of spec). Both halves — commission here, end-state there — are required; see the note at the end of this section.

A decision is **posture-class** if it plausibly changes any of:

- **How the system defends itself** — auth model, session/token handling, crypto choices, secret storage/rotation, TLS/bus auth, input-trust boundaries, `AGENT_TRUSTED_SENDERS` / sender-policy.
- **Who can do what** — permissions, roles, scopes, ACLs, tenancy isolation, privilege boundaries.
- **How the pipeline governs itself** — gate rules, quorum/voting seats, reviewer-model assignment, the change taxonomy itself, loop bounds, what counts as "approved." **A run editing its own guardrails is always posture-class, never auto** — even when it presents as an ordinary config edit. If a guardrail change can show up in a diff as plain config (not surfaced as a decision), the classifier must catch it from the change itself: treat any edit to gate/quorum/taxonomy/reviewer config as posture-class on sight.
- **What data leaves or is destroyed** — external egress targets, PII handling, retention/deletion policy, logging of sensitive fields. (Overlaps the irreversible scan; when both fire, park wins.)
- **A blast-radius widening** — anything that takes a change from reversible-by-not-merging to "affects state/systems outside this branch."

**Tie-break (mandatory): if a decision plausibly sits in any bucket above but you're unsure → it IS posture-class. Park it.** Wrongly parsing posture as ordinary = the silent wrong-merge this whole model exists to prevent; wrongly parking ordinary = a recoverable stall you clear at morning review. Asymmetric — break toward the park.

### The spec-elaboration boundary (the case you'll actually hit)

The hard call isn't the blatant new decision — it's the mid-run decision that *presents as implementing a settled spec decision* but is really making a new one. Example: the spec locks "money is `Decimal` end-to-end." Mid-run, codex hits rounding mode at a boundary the spec didn't pin, or how to coerce a `Decimal` from an external string. Is that *implementing settled posture* (proceed) or *establishing new posture* (park)?

**Rule: if a mid-run decision could be read either as implementing settled posture OR as establishing new posture, treat it as new — park it.** The cost is an occasional unnecessary stall on something that was really just elaboration; the saving is the silent case where a genuine posture choice slips through wearing implementation clothes. Same asymmetry, one level up.

**Provenance:** a posture decision **pre-settled in the approved spec** is already authorised — proceed (it was a human call at Stage 0). Only posture decisions *arising during the unattended run* park. The predicate classifies the *decision's nature*; provenance (spec-settled vs run-arising) decides whether parking is required. When unsure whether the spec actually settled it → treat as run-arising → park.

**Commission is half the surface.** This predicate fires on decisions. Posture is *also* breached without any decision — by **omission** (a posture requirement the spec assumed, silently not implemented), **inheritance** (a framework/library/default's posture adopted by scaffolding, e.g. CORS-open, non-TLS DB, permissive config), and **emergence** (two individually-fine changes composing into a posture change neither one is — a token-bearing field plus a logging hook = tokens in logs). None of those is a decision, so none trips this predicate. They are caught at end-state by the § Drift-against-spec pass — by spec-relative posture-conformance **to the extent the Stage-0 posture-baseline gate forced the posture to be stated** (an unstated assumption is invisible to a spec-relative check), and by the **spec-independent posture oracle** for the recurring classes regardless of spec. The gate makes the implicit explicit; the oracle backstops the enumerable classes the gate's enumeration still missed.

## Loop bound — 3 rounds usual, 5 max (anti-loop guarantee)

Every review↔remediation cycle — the per-task review loop (Stage 3–4), the merge-gate re-gate (Stage 5), and the design-panel re-prompt (Stage 1) — is capped at **3 rounds (usual stop)** and **5 rounds (absolute ceiling)**.

- **At round 3 → park** the item (log to `decisions.md`, stage on a branch, continue), unless the loop is *demonstrably converging* (each round resolved a distinct issue, not re-litigating the same one) and that convergence is logged.
- **At round 5 → park unconditionally.** No exceptions.
- A loop that doesn't converge in 5 is a **planning gap**, not an implementation bug — park it, surface in the morning digest. Never grind past 5; never let the models ping-pong on tokens and time.

## Global budget — wall-clock ceiling (an active pre-action guard, not a passive counter)

The loop bound caps *per-loop* rounds; it does **not** cap *total* overnight spend. A global ceiling bounds the whole run so an unattended multi-model run can't burn unboundedly.

- **Wall-clock is the hard ceiling** — a reliable tripwire. **Token spend is best-effort only:** aggregate token usage across bridge seats is not cleanly observable, so do **not** claim a hard token ceiling you can't measure — track it where visible and treat it as advisory. (Honest measurability beats a ceiling that silently doesn't hold.)
- **Check the budget as an *active pre-action guard* — before every dispatch, every merge, and every push — not as a passive after-the-fact counter.** A counter that notices the overrun *after* a merge or push has already happened has let the run cross the exact line the budget exists to hold. The check is a precondition of the action, not a report on it.
- **On exceed → park everything and stop, through the existing park machinery** (not a new stop semantics): stage all built work, write a **`parked-budget-exhausted`** park record (the § Park-don't-halt resumable fields) for every in-flight task, surface in the morning digest. Global-stop is a *bulk application* of park-don't-halt — the morning resume entry points already exist. Never cross an irreversible/merge/push line to "just finish" once the ceiling is hit.

## Park-don't-halt — the hold-state / dependents matrix

"Continue under current posture" is only safe when a **status-quo hold-state** exists — already-shipped behaviour the run can build against without adopting anything new. "Keep auth X or switch to Y" has a hold-state (X is shipped; building against X declines the change). A greenfield decision — "what auth model for this new service" — has **no** hold-state (nothing to decline to). The binding cell is precisely **no hold-state AND dependents exist.**

When the panel parks a posture decision it records two properties:

- **hold-state** — does a coherent status-quo exist that dependents can build against without adopting a new posture? **Tie-break is mandatory and conservative: if unsure → NO-HOLD-STATE.** (Wrongly assuming a hold-state = the silent-wrong-branch failure; wrongly assuming none = a recoverable stall. Asymmetric — break toward the stall.)
- **dependents** — the transitive set of plan task IDs that need this decision. **Append-only:** a task that discovers a dependency mid-implementation adds itself and self-blocks; it never guesses.

The matrix:

| | no dependents | has dependents |
|---|---|---|
| **hold-state exists** | log, continue, morning decides | build dependents against the hold-state, log that they were; morning may rework if it rejects the hold-state |
| **NO hold-state** | log, continue (nothing needs it yet) | **binding cell — below** |

**The binding cell (no hold-state + has dependents), done right:**

- **Block the dependent subtree** from implementation — its transitive closure, not just direct children.
- **Build nothing provisional** — not behind a flag, not against an "obvious" branch. For posture decisions, provisional *is* applying (provisional auth is auth; a flagged permission model is a permission model). "Provisional" is picking with a deniability label. The blocked subtree builds **never**, not built-and-swappable.
- **Continue the independent rest** of the feature — those tasks still implement and review, so morning gets a fully-built, fully-reviewed branch *minus* the blocked subtree, ready to complete the instant the decision resolves.
- **Do not merge the feature.** Build piecemeal, yes; merge piecemeal, never. **Any feature with a blocked-pending-decision task in its tree stages whole at Stage 5 and does not merge — even the parts that passed the gate 3/3.** The feature is the merge unit; a feature with a known hole is not mergeable. (Independent *features* / separate runs are unaffected — within-feature only.)

**The park record must be resumable from the record alone — no run memory, no filesystem archaeology.** It holds: the decision statement, the options + reasoning, the rejected alternatives + why, the **blocked subtree's task IDs**, the **feature branch name + its HEAD SHA at park time**, the **spec + plan paths (with their SHAs)**, the **completed sibling task IDs + their gate verdicts**, and the **last regression command + result** that defines the resume baseline. **`base ref` is provenance only** (`feat/<topic>@<sha-at-park>`); the resume worktree builds on the **current `feat/<topic>` HEAD** (the independent rest advanced it past the park point), *not* on `base ref` — basing the resume on `base ref` would discard the independently-built rest. **`dependents` is a living field on this record:** a task that self-blocks mid-run (append-only discovery) writes itself back into the record, so the morning resume scoped to the subtree doesn't miss a late self-blocker. Morning-you resolves the decision, then re-invokes autonomous mode **scoped to the blocked subtree only** — a defined resume entry point, not a fresh spec. The unattended run's output is "finished branch + one decision waiting + one-command resume." (The resume dispatch is standard `using-agent-bridge` — a worktree on `feat/<topic>` + a brief scoped to the blocked task IDs off the park record; no new bridge entry point. Autonomous mode defines the resume *semantics*; the bridge already provides the dispatch *mechanics*.)

## The pipeline

| Stage | What | Outcome |
|---|---|---|
| 0. Spec + sign-off | brainstorm → spec → plan; **human approves spec** (the one human touch) | spec on main |
| 1. Design panel (tri-model) | resolve design forks; non-auto decisions logged to `decisions.md` with alternatives | decision-log entries |
| 2. Implementation | codex TDD, isolated worktree on `feat/<topic>` | commits |
| 3. Review (per task) | scaled: **agy** (auto/simple) or **tri-model** (security/posture) | per-task APPROVE |
| 4. Remediation | codex fix → re-review changed scope; **loop rule (3 usual / 5 max)** | clean task |
| 5. Gate + disposition | tri-model gate, then classify outcome → merge / stage / halt | merged core + staged branches |
| 6. Morning review (human, next day) | read `decisions.md`; drift-against-spec check; merge/reject staged branches; sanity-check merges | accepted run |

### Stage 0 — Spec + sign-off
**REQUIRED SUB-SKILL:** `superpowers:brainstorming`, then `superpowers:writing-plans`. Commit spec + plan to `main`. The human's explicit "spec approved, proceed" is the **one** mandatory human touch — it happens *before* you walk away. "Autonomous" means hands-off after this. Fully-unattended-including-spec-sign-off is out of scope.

#### Autonomy preconditions (blocking — declared at the one human touch, consequence surfaced *before* walk-away)

Two host/repo facts flip the run's disposition wholesale, and the run **cannot reliably discover either on its own** — so both are *declared* at Stage 0 and their consequence is *surfaced to the user before walk-away*, not discovered at morning. They are the same shape: a kickoff precondition that, if bad, makes the run **stage rather than deliver** — so the user learns that at the point of *choice* (kickoff), not the point of *discovery* (next morning).

1. **`main-inert: yes | no | unknown`** — does merging to `main` trigger an auto-build / auto-deploy (CI-on-push, a rebuild daemon, a deploy webhook, a hosted platform)? If it does, a merge is no longer "reversible by not merging" — **the merge itself is a live deployment**, with an exposure window until morning review. **The run cannot detect this reliably** (triggers live *outside* the checkout — external CI, a poller, a webhook, an ops repo), so a clean repo scan ≠ inert. It is therefore a **human declaration**, with **fail-closed default: undeclared / `unknown` → treated as NOT inert.** An optional repo scan (`.github/workflows` push-to-`main` deploy jobs, deploy-platform config — Vercel/Netlify/Render/Fly/Railway — deploy scripts/daemons) is a **challenge layer that may *escalate only*:** a detected deploy hint forces NOT-inert even against a `yes` declaration; a clean scan **never** grants `inert` (absence of evidence is not evidence of inertness). Consequence when not inert: § Stage 5 disposition — every merge promotes to the **deployment irreversible-signature class** → stage, don't merge.
2. **`decorrelated-seat: yes | no`** — is a real **non-quorum bridge seat** available for the oracle's judgment tier (defined at § Drift-against-spec → *Oracle mechanism* — a non-quorum adjunct per `using-agent-bridge`; **not** bare-API, **not** a voting-seat model family: codex/agy/cold-Opus)? If `no`, the judgment tier **parks** (§ Stage 5 step 4) rather than running correlated.

**Surface the consequence at kickoff.** If `main-inert` is not `yes`: tell the user *"this run will stage reversible work, not merge it."* If `decorrelated-seat` is `no`: tell the user *"this run will park judgment-class posture work, not deliver it."* The user decides at kickoff whether to proceed — moving the staged-everything surprise from morning to kickoff. This is part of the one human touch; it is not automatable, because only the user knows the host's deploy topology and seat inventory.

**Posture-baseline gate (blocking — the run cannot start without it).** The single biggest hole in hands-off autonomy is *implicit* posture: a posture requirement the spec never stated, which is therefore invisible to every spec-relative check downstream (§ Drift-against-spec). The gate closes it by **forcing the implicit to be stated** at the one moment a human is present. For this specific feature, the spec must **affirmatively enumerate** its posture surface — what it **authenticates**, **who can do what** on it (permissions / roles / scopes on new surfaces), what it **exposes** (endpoints / surfaces / ports), what **inputs it trusts** (externally- or sender-controlled fields, deserialization / parsing, command / template execution), what it **logs**, what it **persists** (and retention), and what **leaves the system** (egress targets) — i.e. **every category § Posture detection names.** This is *affirmative enumeration, not a checkbox* — "posture reviewed ✔" is theatre; the gate wants the actual surface written down so the conformance pass has stated posture to verify against. **It catches the dimension no fixed list has** — a domain-specific trust boundary unique to this system that the posture oracle's enumerable list (§ Drift-against-spec, check #3) would sail past.

**Scale the gate to surface, or it rots into rubber-stamping** (and a rubber-stamped gate is *worse* than none — it manufactures false confidence). If the change plausibly touches **none** of the oracle categories (network / auth / authorization / input-trust / persistence / logging / egress), the gate is a one-line "no posture surface, confirmed" and you move on; a pure-internal-logic feature should clear it in seconds. If it touches **any**, that category demands real enumeration. The gate's weight tracks whether the change plausibly touches the posture surface at all — cheap when it can be, heavy only when it must be. This declaration is part of the one human touch; it is not automatable, because only the human can state the intent the spec omitted.

### Stage 1 — Design panel (tri-model)
Run a tri-model consensus pass on every design fork (mechanics: `docs/multi-model-consensus.md`). Every non-auto decision is logged to `decisions.md` (see § Decision log). A decision that touches safety posture is park-don't-halt here: log the proposal + alternatives, **do not apply it**, then **apply the § Park-don't-halt matrix** — if a coherent hold-state exists, continue under it; if there is **no** hold-state (a *greenfield* posture decision, e.g. "what auth model for this new service" — the matrix's binding cell), **block the dependent subtree and build nothing provisional.** Do **not** "continue under the current posture" when there is no current posture to continue under — that is the build-against-a-guessed-branch failure red-flag #4 forbids. Morning decides. Before classifying any design fork as auto-eligible, run it against § Posture detection; an unrecognised posture decision routed to auto is the one silent failure this pipeline cannot recover before merge.

### Stage 2 — Implementation
Pre-create a worktree on `feat/<topic>` off `main` (worktree-before-dispatch — non-negotiable). Dispatch codex per TDD task. **REQUIRED:** `using-agent-bridge`. Verify from git (SHA + diff + test run), not reply prose.

### Stage 3 — Review (per task, inside the Stage-2 loop)
For each task's commit, classify the task and review:
- **Auto-eligible** → single **agy** review (bridge seat).
- **Security/posture (tri-model-required)** → codex + agy + cold-Opus, independent.

**Review hygiene:** bridge reviewers (codex, agy) each get their own `--worktree review-<engine>`. cold-Opus is an in-process `Agent` subagent — it **cannot** take a bridge `--worktree`, so it writes to an out-of-repo `/tmp` path and must not overlap a bridge reviewer writing into the shared checkout. **Every out-of-repo report goes to a unique path** — `/tmp/<run-id>/review-<stage>-<seat>-<role>-<taskid>.md`, never a shared `/tmp/review-<engine>.md`. The `<role>` field is load-bearing: Stage 5 runs **two** cold-Opus dispatches that share `<stage>=5` and `<seat>=cold-Opus` with no per-task id — the step-2 voting seat and the step-4 conformance pass — so they must disambiguate as `review-5-coldopus-vote` vs `review-5-coldopus-conformance` or the template regenerates the very collision it exists to prevent (and step-4 would overwrite the step-2 output `§ Stage 5` insists must stay separate). Per-task reviews can also run concurrently; a non-unique path just relocates the collision/read-leak the out-of-repo move was meant to prevent. No report enters the repo until all first-pass reviews finish (`docs/multi-model-consensus.md § Review hygiene`). cold-Opus is reserved for tri-model + the gate — never the simple-path seat.

### Stage 4 — Remediation
Any `REQUEST CHANGES` → focused codex fix quoting the reviewer's exact prescription + the missing test that let the defect slip → re-review only the changed scope. Bound by the **loop rule (3 usual / 5 max)**. At the cap, park the task (log, stage, tag `parked-defective`, continue) — it's almost always a planning gap, not an impl bug. **Block its dependent subtree too** (transitive closure, same mechanism as the binding cell): a task built on a known-broken dependency inherits the defect, so its dependents build no further until morning. The independent rest of the feature continues; the parked task + its dependents do not.

**Disposition-separability — a judgment principle (NOT a mechanical gate).** The gate often produces
findings that need **differing downstream handling** — *including but not limited to*: different
*verification* (one finding correlated with its own verification needs an independent re-check, others
fine on convergence), different *merge-eligibility* (one ships, one must-park), *park-vs-proceed* (one
posture-class or irreversible-signature, the rest not), or a Stage-0-acknowledged *logged-note* downgrade
vs remediate. When findings differ this way, **prefer to keep them separable — separate commits — so the
disposition can be applied per finding.** Bundling differing-disposition findings forces a *uniform*
disposition and lets commit structure, not a decision, pick which: usually the cheapest handling
**under-applies to the one that needed more**, silently. **Coupling exception:** genuinely co-dependent or
atomic findings (one won't compile/test without the other) stay **bundled, under the *strictest*
disposition of the set** — forced separation that breaks the build is worse than the bundle. A re-commit
done purely to separate (structural, not a code fix) does **not** count against the loop bound.

> **This is enforced by reviewer judgment, not a mechanical check — a named limit, not an oversight.** An
> agent executing the Stage-5 checklist step-by-step is **not** stopped by this principle: the gate reviews
> `git diff main…feat`, which is commit-agnostic, and any "is this separable?" check rests on each
> finding's required disposition — which the orchestrator would otherwise *self-assert* (self-asserted
> premise → self-satisfying check: the exact stated-≠-enforced gap this principle is *about*, which is why
> it is honestly labeled judgment, not mechanism). It relies on reviewer attention; **that reliance is the
> known gap.** A *mechanical* version — seats tag each finding's required disposition in their report, the
> gate reads `git log` and compares tags↔commit boundaries — is **deferred, not rejected**: it grows the
> Stage-3 review contract + Stage-5 gate steps, which isn't worth it for a failure that so far has occurred
> once, with a fine outcome, and is reviewer-catchable. **If disposition-separability collapses again at
> higher stakes, that recurrence is the signal to build the mechanical control** (panel findings:
> `feat/skill-disposition-separability`). **The recurrence is surfaced, not left to memory:** when a
> remediation bundles differing-disposition findings (this principle unmet), Stage 6 flags it in the
> morning digest as a *disposition-separability recurrence-signal* — a deferred-until-recurrence trigger
> with no watcher never fires, which is the same discipline as the escaped-defect journal's record-at-the-
> moment rule. So the upgrade decision lands in front of a human when the failure repeats, not whenever
> someone happens to remember this rule exists.

*Revealing instance:* a fix correlated with its own verification (an NDJSON-authoritative rework — the
orchestrator can't independently vouch for the substrate it reads from) was committed with five
convergence-only fixes; the bundle forced "independently re-probe the one, merge all six," and the split
stayed safe **only because** the one needing the extra check happened to get it — structure, not the plan,
decided that.

### Stage 5 — Gate + disposition
1. If `main` advanced since the worktree was created, merge `main` into `feat/<topic>`, re-run regression, gate the integrated diff.
2. Dispatch **codex + agy + cold-Opus** full-feature review against the spec + `git diff main…feat/<topic>`, same review-hygiene as Stage 3 (each bridge seat its own `--worktree review-gate-<engine>`; cold-Opus to `/tmp`).
3. Apply the quorum rule (below).
4. **Conformance + drift + posture-oracle pass (blocking) — two dispatches, not one.** (a) A **cold-Opus** pass does **drift + spec-relative posture-conformance** over the integrated diff vs the spec. (b) The **posture oracle** runs as its two tiers (§ Drift-against-spec → *Oracle mechanism*): a deterministic, model-free **mechanical scan**, plus a **judgment pass routed to a non-quorum adjunct model — never a voting seat (codex/agy/cold-Opus), or it re-correlates with the panel.** Any drift, conformance, or oracle flag → REQUEST CHANGES: remediate (back to Stage 4) or park, per the flag — **except** a mechanical-tier flag on a Stage-0-acknowledged deviation, which downgrades to a logged note (citing the Stage-0 line). This runs **even on 3/3 APPROVE** — the quorum seats judge the diff; this pass judges it against the spec *and* against the fixed posture checklist regardless of spec, catching the omission/inheritance/emergence breaches the seats and the per-decision predicate both miss. A clean pass is necessary to proceed to disposition. **The cold-Opus conformance dispatch is separate from the step-2 voting seat — do not reuse the step-2 output.** **If no decorrelated seat exists** for the judgment tier — the Stage-0 `decorrelated-seat: no` case, already surfaced to the user at kickoff — the judgment-tier classes **park** under tag **`parked-unverified-posture-judgment`**; the mechanical tier still runs, and any feature carrying this park **stages whole** (§ Stage 5 disposition). The judgment tier is **never run correlated-and-logged** — a correlated oracle is no oracle (§ Drift-against-spec, third limitation). **This applies to the unattended Stage-5 gate only; at the Stage-6 morning pass the human *is* the decorrelated check and runs the judgment tier themselves** (parking a check on the desk of the person who performs it is incoherent).
5. **Disposition by classification** (this is the hands-off crux):
   - Auto-eligible + 3/3 APPROVE over the final diff + no irreversible signature (**including the main-not-inert promotion — if `main` is not inert (Stage-0 declaration, or the undeclared/unknown default), every merge carries the deployment irreversible-signature, so this bullet cannot fire**) + **no park of any kind anywhere in the feature** — `blocked-pending-decision` / `parked-defective` / `parked-unverified-posture-judgment` (task *or* check-class) / `parked-budget-exhausted` / 5th-round park (the staging bullets below override a clean 3/3) → `git merge --no-ff`.
   - **Park-don't-halt** (irreversible signature present — **including the main-not-inert deployment promotion** — OR the run touched a safety-posture question) → record the gate's verdict in `decisions.md`, **stage the change on a branch and do NOT merge** (even on 3/3), continue.
   - **Feature with a blocked-pending-decision task** (park-matrix binding cell) → the **entire feature stages whole and does not merge**, even the independently-built, 3/3-approved parts. A feature with a known hole is not mergeable; morning resolves the decision and resumes to complete it.
   - **Feature with any `parked-defective` task, OR any review/re-gate loop that hit the 5th-round park** → the **entire feature stages whole and does not merge**, even the 3/3-approved parts. A 3/3 APPROVE on the integrated diff does **not** override a task the loop already parked as known-broken or unconverged — the gate seats judge the diff, but they did not see the defect the loop cap already recorded. Known-broken (or unconverged) work is not mergeable; morning inspects/debugs/discards per § Stage 6 (`parked-defective` scrutiny).
   - **Feature carrying any `parked-unverified-posture-judgment` park (task *or* check-class)** (Stage-0 `decorrelated-seat: no`; § Stage 5 step 4) → the **entire feature stages whole and does not merge**, even the 3/3-approved parts. The judgment-tier posture classes were mechanically scanned but **not semantically verified** for lack of a decorrelated seat. A 3/3 gate does **not** substitute — the voting seats are correlated with the missing oracle by construction (that correlation is exactly why the oracle exists). Morning runs the judgment tier by hand (§ Stage 6). *Unverified, not known-broken* — distinct from `parked-defective`.
   - **Feature with any `parked-budget-exhausted` task** (§ Global budget) → the **entire feature stages whole and does not merge.** A resource stop, not a defect or a decision; morning reviews what completed and resumes with a fresh budget.
   - Hard-halt → stop, log, digest.
6. If a merge/push to `main` fails (branch protection), stage on the branch and continue — do not force.

**Quorum:** three voting seats (codex, agy, cold-Opus), all quorum. APPROVE from all three over the **final** diff → eligible to merge (prior approvals don't carry forward across remediation). Any REQUEST CHANGES → remediate, re-review, re-gate. **Re-gate bound:** the loop rule (3 usual / 5 max) — a 5th-rejection gate parks. NOTES are blocking if security-relevant or P0/P1 (quote each note's severity in the record). Verdict normalization: `SHIP`→APPROVE, `SHIP_WITH_NITS`→APPROVE-WITH-NOTES (triage), `FIX_BEFORE_MERGE`/`REJECT`→REQUEST CHANGES, timeout/crash/empty→REQUEST CHANGES (re-dispatch once, then park). **Silence is never APPROVE.**

**Deliberate deviation — cold-Opus is voting here.** `docs/quorum-decision-taxonomy.md § "Why the cold auditor is non-voting"` specifies the cold auditor as non-voting (to avoid anchoring on the option set). Autonomous mode keeps cold-Opus voting at the gate for one reason only: it catches **diff-level** defects the Stage-5/6 spec-conformance pass does not (the conformance pass judges against the spec; the voting seat judges the diff on its own terms). The drift/posture backstop no longer depends on this vote — so if a future review finds the voting seat's diff-level catch is redundant with codex+agy, make cold-Opus non-voting and restore doctrine. Until then the anchoring trade-off is accepted for that specific, named reason — **not** as a generic "more approvals = safer."

**Open limitation (unresolved):** the Stage-5 voting cold-Opus and the Stage-5 conformance cold-Opus are the **same model family**, so a posture blind spot in one is likely shared by the other — the conformance backstop is not a *model-independent* check of the vote, only a *brief-independent* one. Unique out-of-repo paths (§ Stage 3 hygiene) prevent the two dispatches from contaminating each other's *files*; they do **not** address this *correlation*. Closing it would require the conformance seat to be a different model from the voting seat (e.g. route the conformance pass to a non-Opus reviewer). Logged as a known gap, not silently accepted as closed.

### Stage 6 — Morning review (the human's actual job)

> **The morning audit is substantial, and it is itself a rubber-stamping surface** — the very Stage-0 risk this skill fears, relocated to the morning. Reading every decision, re-running the three-check pass, applying park-reason-specific scrutiny, sampling merges, and cherry-picking is *more* work than a normal PR review, and a tired reviewer nodding through it silently re-creates the rubber-stamp. Two mitigations, both below: approach it with **Stage-0-level scrutiny** (it is not a free walk-away payoff), and **consume the structured digest artifact (step 5) — you tick items with evidence, you do not nod at prose.** Nothing mechanically stops a human from rubber-stamping the morning; this naming, and the tick-list, are the honest best we have.

1. **Read `decisions.md`** — every non-auto decision with its rejected alternatives. You are reviewing cold; a decision whose discarded options you can't see is unauditable — flag it as a logging failure.
2. **Three-check pass (drift + spec-relative posture-conformance + posture oracle)** — re-run the **same checks as the Stage-5 gate** (§ Drift-against-spec) over the full merged diff: a cold-Opus drift+conformance pass, plus the two-tier oracle (model-free mechanical scan + non-quorum judgment pass). Does it satisfy the spec, is every stated posture requirement present, and does it pass the fixed posture oracle regardless of spec? This is your primary defense against collective drift over the unattended run. Treat any flag from any of the three as a signal to investigate. **At morning, *you* are the decorrelated check** — so the judgment tier runs **even when Stage 5 had `decorrelated-seat: no`**; (b)'s "no seat → park" is a Stage-5-gate rule and **does not apply here** (deferring the morning review to the person doing the morning review is incoherent). The `parked-unverified-posture-judgment` subtrees (step 3) are exactly where your hand-run judgment tier is the missing semantic check.
3. **Staged branches are tagged with their park reason — apply the matching scrutiny** (do not treat them identically):
   - **`blocked-pending-decision`** — greenfield posture call; *nothing was built* for this subtree. You make the design decision, then resume autonomous mode scoped to the subtree to build it.
   - **`staged-irreversible`** — built and gate-approved, withheld by the irreversible-signature scan (**includes the main-not-inert case: a finished, 3/3-approved, reversible change whose *merge* is the deployment**). You decide whether to merge-and-deploy a finished, risky change.
   - **`parked-defective`** — the Stage-4 loop-cap case; built but known-probably-broken. You inspect a defect — debug or discard. (**Do not** apply "approve finished change" scrutiny here; that merges a known-broken thing.) If you fix it, resume autonomous mode scoped to that task (same scoped re-invoke as `blocked-pending-decision`, off the park record's task IDs + base ref); if you discard it, the feature stages whole until the task is re-planned.
   - **`parked-unverified-posture-judgment`** — built and mechanically scanned, but the judgment-tier posture classes (input-trust, authz-correctness, PII) were **not semantically verified** (no decorrelated seat at Stage 5). **You run the judgment tier yourself** over this subtree's diff; clean → merge, flag → remediate or discard. (Distinct from `parked-defective`: nothing is known-broken — it is *unverified*, not *failed*; apply "verify then decide," not "debug a defect.")
   - **`parked-budget-exhausted`** — the global-budget tripwire fired mid-run (§ Global budget); built work was staged and in-flight tasks parked with resume records. Not a defect and not a decision — a resource stop. Review what completed, then resume autonomous mode (scoped to the unfinished tasks) with a fresh budget.
4. Sample the auto-merged items via the drift check.
5. **Consume the structured morning digest artifact (not prose).** The run emits a tick-list — one line per item, each with its evidence — that you check off, because a structured artifact is materially less rubber-stampable than a prose summary: **every park** (reason + resume command + last regression result), **every auto-merge** (+ its drift-check result), and **both Stage-0 autonomy-precondition declarations** (`main-inert`, `decorrelated-seat`) with what each caused. An item you can't tick with evidence is the signal to investigate.
6. **Rescue separable proceed-eligible work from a staged feature.** A feature stages *whole* when any task parks (§ Stage 5), but if the proceed-eligible fixes are in their own commits (§ Stage 4 disposition-separability), you can `git cherry-pick` those onto `main` and leave the parked commits on the branch. If the work is welded into one commit you can't — the whole feature waits. (This is the morning payoff of separability — and the reason it's worth keeping, even as a judgment principle.) **Caveat: if `main` is not inert, a cherry-pick onto `main` is itself a deployment** — apply the `staged-irreversible` decision, don't reflexively rescue.

## Decision log (`decisions.md`) — a safety property

Every non-auto decision is logged using the `quorum-decision-taxonomy.md` decision-doc structure: the question, each option + one-line reasoning (verbatim where possible), the **rejected alternatives + why**, the chosen direction, confidence, consensus state. **"Chose X" without "over Y because Z" is unreviewable and is a logging failure** — you're auditing cold next day with no memory of the run. A decision that redefines safety posture is park-don't-halt: log the proposal + alternatives, do not apply, then **apply the § Park-don't-halt matrix** (continue under a hold-state only if one exists; if greenfield / no hold-state, block the dependent subtree and build nothing), morning decides. **Park records additionally carry `hold-state`, `dependents` (the blocked subtree's task IDs), `base ref`, and the resume fields above** so they're resumable (see § Park-don't-halt matrix).

**Log to a run-scoped path** — `docs/decisions/<topic>.md`, one file per feature/run — not a single shared `decisions.md`. A shared log makes every concurrent run conflict on the same file at merge time, and an unattended run is forbidden from forcing the merge — so a logging-format choice would silently downgrade clean auto-eligible merges into staged branches. Per-topic files never collide; morning can consolidate if desired.

## Drift-against-spec + posture-conformance — the load-bearing end-state check

Hands-off autonomy makes orchestrator drift *more* consequential, not less — nobody catches it mid-run. The run resolves forks locally-reasonably; collectively they can build something you didn't ask for, with a coherent-looking trail. The defense is a cold-Opus pass over the **whole diff against the original spec only** (no run context, so it cannot be anchored by the run's own reasoning), checking **two spec-relative directions plus one spec-independent oracle**:

1. **Drift (added wrongness)** — anything the diff did *beyond or against* the spec.
2. **Posture-conformance (missing rightness)** — every posture requirement the spec states, verified present in the diff. This catches the non-decision breaches the per-decision predicate can't see — **omission** (required auth/retention/validation simply not implemented), **inheritance** (a framework default silently setting posture — CORS, TLS, permissive config), and **emergence** (a posture breach existing only in the *composition* of two fine changes) — **but only to the extent the spec states the posture.** The Stage-0 posture-baseline gate is what forces it to: an assumption the spec never stated is invisible to this spec-relative check (which is exactly what #3 backstops). The check is end-state, so *how* the posture got wrong doesn't matter — only whether the result satisfies the requirement.
3. **Posture oracle (spec-INdependent)** — a fixed, **maintained** checklist of the recurring posture classes — **CORS policy, transport/TLS, secrets-or-tokens-in-logs, auth on every exposed endpoint, authorization / permission scoping on new surfaces, input-trust (unsafe deserialization / parsing, command / template execution, trust of externally- or sender-controlled fields, sender-policy / `AGENT_TRUSTED_SENDERS`), PII / sensitive-field logging & retention, egress targets** — verified against the diff **regardless of whether the spec or the Stage-0 gate mentioned them.** **The oracle's class list must cover every category § Posture detection names — or carry an explicit "handled by X" justification for any it doesn't.** Three of the five are diff-detectable-by-content and so get checklist lines here: *how the system defends itself*, *who-can-do-what*, and *what-data-leaves-or-is-destroyed*. The other two are **deliberately handled elsewhere, not by this oracle**: *how the pipeline governs itself* is caught by the on-sight guardrail diff-scan (§ Posture detection — "treat any edit to gate/quorum/taxonomy/reviewer config as posture-class on sight"), and *blast-radius widening* by the irreversible-signature scan. Any future category is added here or gets the same explicit justification — silence is the drift this rule exists to prevent. Keeping list and taxonomy in sync is the maintenance contract; let them drift and the oracle silently narrows below the doc's own posture definition (exactly the input-trust omission a re-review caught). Checks #1/#2 are spec-relative and can only see posture the spec states; the oracle is not, so it catches the enumerable classes even when *both* the spec and the human's Stage-0 enumeration missed one. **Division of labour, not redundancy:** the Stage-0 gate forces enumeration of *this feature's* posture surface and so catches **novel, domain-specific dimensions no fixed list has** (unknown-unknowns within the feature); the oracle catches the **recurring classes the spec/gate omitted** (known-unknowns). Each closes a gap the other structurally cannot. **The oracle is a blocklist, so it is only as good as its last update:** it ships as a seed checklist with this skill — [`posture-oracle.seed.md`](./posture-oracle.seed.md) — which each adopting repo copies to `docs/posture-oracle.md` at adoption and thereafter owns, filling in `owner:` and keeping `last-reviewed:` current in its frontmatter (the skill seeds the class list above; the repo instantiates, owns, and dates it). Every posture issue that slips past it becomes a new line (grow-when-bitten). An oracle written once and trusted forever silently stops covering the real threat — the same failure a stale backup hides until the restore.

**Oracle mechanism — two tiers by checkability; route each class to the cheapest mechanism that is actually independent of what it checks.** Calling this an "oracle" only earns the name if it has an oracle property — *independence from the thing it backstops.* It has none if it's just another cold-Opus checklist pass, because cold-Opus is also the voting seat (§ Stage 5) and the conformance seat: it would then share their model blind spot (the correlation logged at § Stage 5 "Open limitation"). So the oracle is **not** a single model pass; it splits by what each class admits:

- **Mechanical tier (deterministic, model-INdependent).** The classes decidable by pattern / config / AST — **secrets-or-tokens-in-logs** (regex), **transport/TLS** (config scan for plaintext), **CORS** (config scan for wildcard / over-broad origins), **auth-on-exposed-endpoint** (route table vs auth middleware), **new-egress-host** (host scan) — run as a **deterministic scan with a documented false-positive profile.** No model; independent of every voting seat by construction. **Spec-acknowledged-exception path (required):** a deterministic flag on a deviation the **Stage-0 gate explicitly enumerated and accepted** downgrades from REQUEST CHANGES to a **logged note**, not a block — and the note must cite the Stage-0 enumeration line that accepted it (no bare override). A scanner can't read intent; without this path the mechanical tier hard-blocks intentional, human-approved, spec-stated posture (e.g. authorised plaintext on an internal mesh), and being deterministic it can't be reasoned with at the gate. Determinism buys independence and costs context; the exception path buys the context back.
- **Judgment tier (model-based, must be DECORRELATED).** The classes needing semantics — **input-trust** (is this deserialization / parse / exec actually reachable from untrusted input?), **authorization correctness** (do these scopes match intent?), **PII classification** (is this field sensitive?) — can't be regex'd, so they are a model pass. To be a real backstop it must run on a model **outside the Stage-5 voting quorum** — **not codex, agy, or cold-Opus.** All three are voting seats; routing the oracle to any of them only moves the correlation one seat over (a codex-run oracle shares voting-codex's blind spots — the oracle is meant to catch what the *panel* misses, so a panel member can't run it). Route to a genuine **non-quorum adjunct** (a pi-rpc / gemini seat per `using-agent-bridge`). **Admissibility — what counts as a "decorrelated adjunct" (so park can't be evaded by relabeling):** a real **non-quorum bridge seat** dispatched per `using-agent-bridge` — **not** a bare-API call (forbidden for real work; it bypasses the harness/audit and gives the raw model, not the model-in-its-harness), **not** any voting-seat model family (codex/agy/cold-Opus), **not** a same-family substitute reached for under park-pressure. Parking is *painful* (the run delivers less), which creates a perverse incentive to declare any available model "decorrelated" just to unblock — that is exactly the skill's own *"provisional is picking with a deniability label"* anti-pattern. A model that does not meet this bar is **not** a decorrelated adjunct: the judgment tier then has **no seat → it PARKS** (`parked-unverified-posture-judgment`, § Stage 5 step 4), it does **not** run correlated. This is the `decorrelated-seat` the Stage-0 autonomy preconditions declare.

**Open limitation (third — the oracle's judgment tier), restated as a *delivery* limit, not a *safety* gap.** If the harness has **no decorrelated seat** (Stage-0 `decorrelated-seat: no`), the judgment tier **does not run correlated** — it **parks** the judgment-class items and the feature stages whole (§ Stage 5 step 4). This *closes* the former safety gap (a correlated oracle masquerading as independent is gone), at a cost that is now honest and named: **judgment-class posture work cannot be *auto-delivered* on a host with no decorrelated seat — it stages for the morning human.** That is an **availability / delivery limitation, not a safety blind spot:** the check is not faked, it is *deferred* to the one decorrelated reviewer that always exists — the morning human (§ Stage 6), who runs the judgment tier by hand. The mechanical tier is unaffected (model-free). Closing the *delivery* limit requires standing up a non-quorum bridge seat (see the skill's open items / the host's seat inventory). (The other honest gap — the Stage-5 voting↔conformance *same-family* correlation — remains; the document carries its gaps, it does not launder them.)

**Why this is the structural answer to implicit posture, not a line edit.** Both #1 and #2 are spec-relative: **an unstated posture assumption cannot be conformance-checked**, so posture the spec leaves implicit is invisible to them. The defense is therefore *two-pronged and neither prong is optional*: the **Stage-0 gate forces the implicit to be stated** (converting invisible posture into checkable spec lines — the way "money is Decimal, float rejected" became a machine-checkable invariant), and the **oracle (#3) runs independently of the spec** for the recurring classes the human's enumeration can still miss. The spec is the real defense; the gate enforces that it is posture-complete; the oracle backstops the enumerable classes it isn't.

**Timing — this is detection at the gate, detection-and-revert at morning, not prevention:**
- **Stage 5 (gate):** run the three-check pass as a **blocking gate criterion** on the integrated diff *before* any auto-eligible merge. A drift, posture-conformance, or posture-oracle flag here → REQUEST CHANGES (remediate / park), so the breach is caught *before* it merges. This is the prevention path; use it for anything that would be costly to revert.
- **Stage 6 (morning):** re-run over the full merged diff as the review centerpiece. Anything that slipped the gate is caught here — recoverable by revert, since it's git. Treat every flag (any of the three checks) as a signal to investigate.

(Running it at *both* points is deliberate: the gate prevents the expensive cases; the morning pass is the safety net for what a single gate pass missed. Do not rely on Stage 6 alone — that is post-merge detection of things the gate could have blocked.)

## Red flags — STOP

- *"Irreversible op → halt and wait for the human."* No — there's no human there. **Park on a branch, log, continue.**
- *"Escalation → permanent halt until the human replies."* No — **park-and-log-and-continue**, unless it's a genuinely unrecoverable, un-stage-able hard-halt.
- *"The panel hit a safety-posture question — apply our resolution and keep going."* No — that's park-don't-halt: **propose + log, then apply the park matrix** (continue under a hold-state if one exists; if greenfield / no hold-state, block the subtree and build nothing), **morning decides.** Never let the run rewrite its own guardrails unattended.
- *"The posture decision is parked but one branch is obviously right — I'll build against it provisionally to keep moving."* No. Provisional implementation of a posture decision **is** applying it. The dependent subtree blocks and builds nothing until you resolve it; an "obvious" branch that's wrong is the exact silent-wrong-branch failure this whole model exists to prevent.
- *"Parked item blocks the only remaining work, so merge it to unblock."* No — a stall is acceptable; never auto-merge a parked item to keep moving.
- *"I'll self-review / inline-review instead of dispatching seats."* No. Reviewers must be independent.
- *"Two of three approve, close enough."* No — the gate is unanimous over the final diff.
- *"This touches auth but it's small — single agy review is fine."* No — security/posture is always tri-model.
- *"`main` just rebuilds on merge, but this change is reversible, so I'll merge it."* No — if `main` is not inert (Stage-0 declaration; default when undeclared), the **merge is the deployment** and carries the irreversible signature → **stage, don't merge.** "Reversible by not merging" is false once the merge deploys.
- *"No decorrelated seat for the judgment tier — I'll run the oracle correlated and just log it."* No — **park** the judgment-class items (`parked-unverified-posture-judgment`) and stage the feature whole; a correlated oracle is no oracle. (This is the Stage-5 gate. At *morning* the human runs the judgment tier — that is a different case, not this one.)
- *"I'll use M3-via-bare-API / another Opus / a voting-seat model as the decorrelated adjunct to unblock delivery."* No — the adjunct must be a **real non-quorum bridge seat** (`using-agent-bridge`); relabeling a correlated model is "provisional with a deniability label." If none qualifies → `decorrelated-seat: no` → park.
- *"The global budget is spent, but I'm one merge from done — I'll just finish."* No — the budget is an **active pre-action guard**: on exceed, **park everything** (`parked-budget-exhausted`) before the next dispatch/merge/push. Never cross an irreversible line to "just finish."

## Scenario traces (the acceptance bar for amendments to this skill)

These are the prose-analog of a deny-proof. An amendment to this skill's park/merge logic is accepted **only if a lazy/adversarial 2am reader — one actively hunting for a reading that lets them proceed — cannot route around the STOP.** "A careful reader would park" is **not** a pass; the failure mode of prose is ambiguity exploited under load, so the bar is *"no reading reaches proceed,"* not *"the right reading parks."* Each trace must terminate in an unambiguous § Red-flags STOP line.

**Trace 1 — no decorrelated seat + a judgment-tier (input-trust) diff.** Stage-0 `decorrelated-seat: no` (surfaced at kickoff). The diff adds deserialization of a sender-controlled field (an input-trust `[J]` class). → Mechanical tier runs (no `[M]` regex catch here). → Judgment tier has no admissible decorrelated seat → it **parks** `parked-unverified-posture-judgment` (§ Stage 5 step 4); it is **not** run correlated-and-logged (§ Red flags). → The feature carries a `parked-unverified-posture-judgment` task → it **stages whole, does not merge** (§ Stage 5 disposition), even on 3/3. → Morning: the human runs the judgment tier over the subtree (§ Stage 6 step 3). **No reading reaches "merge."** Relabeling a correlated model to dodge the park is closed by the admissibility bar + its Red-flags line.

**Trace 2 — `main-inert: unknown` + a reversible, 3/3-approved change.** Stage-0 left `main-inert` undeclared → default **NOT inert** (surfaced at kickoff: "this run will stage, not merge"). The change is ordinary reversible code; the gate returns 3/3 APPROVE. → Because `main` is not inert, **every merge carries the deployment irreversible-signature** (§ Change-classification table; § Stage 0 precondition 1) → the auto-eligible-merge bullet **cannot fire** (it requires *no* irreversible signature) → park-don't-halt → **stage, do not merge** (§ Stage 5 disposition). → Morning: the human decides whether to merge-and-deploy (§ Stage 6, `staged-irreversible`). **A 3/3 APPROVE does not reach "merge"** while `main` is not inert.

## Cold-Opus brief discipline

A cold-Opus reviewer is only cold if its brief contains **only**: spec path, the diff (or instruction to compute it), output format, report path. **No** plan rationale, deviations log, per-task reviews, or "we did X because Y". The **merge-gate** cold-Opus brief is the strictest — by Stage 5 you hold the most context, so contamination risk is highest: strip Stage-3 reports, remediation rounds, deviations, and any "we already addressed X". (`docs/pipeline-operating-manual.md § "Cold-opus dispatch mechanism"`.)

## See also

- `docs/pipeline-operating-manual.md` — the A/B pipeline this strictens (re: the gate).
- `docs/multi-model-consensus.md` — design-panel + review-hygiene rules.
- `docs/quorum-decision-taxonomy.md` — decision-doc format used by `decisions.md`; **this skill deliberately deviates from its non-voting cold-auditor rule** (see Stage 5).
- `using-agent-bridge` skill — dispatch mechanics (**REQUIRED**).
- `superpowers:brainstorming`, `superpowers:writing-plans` — spec/plan stages (**REQUIRED**).

# Autonomous-mode skill hardening — review disposition + execution plan

> Source: project-a warm-Opus orchestrator critique (it had just *run* the skill autonomously) + a
> Claude.AI Opus meta-analysis sorting the proposed fixes, brought to the bridge orchestrator. This
> doc is the bridge orchestrator's grounding of those four fixes against the **current** `SKILL.md`
> text, plus the agreed execution shape. Branch-only; user is the merge authority.

## Grounding the four fixes against current text

All four are **accurate against the skill as it stands** (`skills/autonomous-mode/SKILL.md`, 256
lines) — none is already-fixed, so taking them does not re-open settled ground:

- **(b) park-not-log** — confirmed real. The skill currently does exactly what the critique says, in
  three places: Stage 5 step 4 — *"(If no non-quorum model exists for the judgment tier, it runs
  correlated and is logged… not pretended independent.)"* (line 181); the third Open limitation
  (line 225, *"named, accepted gap, not a closed defense"*); and the seed oracle `[J]` line
  (`posture-oracle.seed.md` L30–32). So (b) is a genuine behavioral change at all three sites: no
  decorrelated model → **park the posture-judgment-class items**, not run-correlated-and-log.
- **(c) main-inert gate** — confirmed real. Core principle rests on *"reversible by not merging"*
  (line 14); Stage 5 disposition merges auto-eligible on 3/3 (line 183) with **no** precondition
  asking whether merging `main` triggers a build/deploy. "When NOT to use" (line 30) excludes *live
  cutovers you execute* but says nothing about a `main` that **auto-deploys on merge** — the exact
  exposure-window the critique names. Real gap.
- **(a) flowchart** — real; there is no distilled decision procedure, just the prose.
- **(d) global budget** — real; the loop bound is per-loop (3/5, lines 72–78), with no global
  token/wall-clock ceiling.

Disposition (agreed): **b and c are safety fixes** (they change park-vs-merge logic); **a and d are
hardening** (a = flowchart-*plus*-prose, not replace; d = benign-failure). Plus two doc-honesty
notes: point 4 (sweet-spot is reversible/posture-free; posture-heavy runs stage heavy **by design**)
and point 5 (the morning audit is substantial and itself a rubber-stamping surface).

## Wrinkle the analysis did not catch — "execution-primary" on a prose skill

The handoff asks to panel b/c **execution-primary** because "reading the prose won't verify whether
it actually parks / actually gates." Real tension: **the skill is a markdown instruction doc, not
code — there is no runtime to execute, no test to run.** So "execution-primary" here cannot mean "run
it"; it maps to **adversarial scenario-tracing**: construct the two failure scenarios (a host with no
decorrelated model; a `main` that auto-deploys on merge) and trace the *amended* text to confirm it
**unambiguously forces park/stage** and cannot be read by a 2am orchestrator as "proceed." That is
the prose analog of execution — and the right bar — but it is named here rather than pretending there
is a green/red test. (The eval pipeline has executable guards; this skill does not — different
artifact.)

## Substantive check on (b)

The "(b) is cheap now because P-1 stood up M3" point is right, with a caveat: the skill's `[J]` tier
wants a **non-quorum bridge seat** (pi-rpc/gemini), and the hard rule forbids raw-API for real work.
If M3 is wired as a *bare-API* normalizer, it may not yet count as the skill's decorrelated adjunct —
meaning the "no decorrelated model" case (b) guards might be *less* rare than assumed until M3 has a
proper seat. Does not change taking (b) — parking on absence is correct regardless — but verify M3's
seat status during implementation rather than assume it closes the case.

## Proposed execution shape

Per the skill's own self-amendment discipline (the property that makes it trustworthy) and the
kickoff-confirm rule:

- One `feat/autonomous-mode-hardening` branch, branch-only, **user reviews to merge** — orchestrator
  does not merge.
- **b + c**: full treatment — tri-model panel, adversarial scenario-trace as above.
- **a + d**: lighter review — they do not touch park-vs-merge logic.
- Points 4 + 5 fold in as named-gap notes (same register as the skill's existing three
  Open-limitations).

## Panel outcome (tri-model, independent — codex / agy / cold-Opus)

**Unanimous verdict: ENDORSE-WITH-CHANGES.** Take all four; none rejected; grounding citations
verified clean by all three seats. But **b and c must not be drafted as written** — both are
underspecified in the way that decides whether they are real controls or theatre. Refinements below
are pre-drafting requirements, deduped across seats (convergence noted).

### (c) main-inert gate — UNANIMOUS fatal gap: an unattended run cannot detect main-auto-deploy
Triggers live outside the checkout (external CI/poller, webhook, server cron, ops repo); a clean
repo scan ≠ inert, so a detection-only gate re-introduces the exact silent exposure (c) exists to
prevent, now labeled "checked." Required shape (all three agree):
- **Stage-0 human declaration** `main-inert: yes/no/unknown` (+ evidence), folded into the one human
  touch (the designated home for intent the run can't automate).
- **Fail-closed default: undeclared/unknown → NOT inert → stage.** (Asymmetry: assume-inert-but-deploys
  = catastrophic silent exposure; assume-deploys-but-inert = recoverable stall = point-4's accepted cost.)
- Optional repo/CI scan (`.github/workflows` push-to-main, deploy-platform config, deploy scripts) is a
  **challenge layer: may escalate (force stage) only, never downgrade (cannot grant "inert").**
- **Architecture (cold-Opus, adopt):** don't bolt on a parallel gate — the skill already classes
  "deployments" as irreversible-signature (SKILL.md:39) and blast-radius widening as posture-class
  (:58), both staging at :184. If main auto-deploys, **every merge IS a deployment** → it already meets
  the existing irreversible-signature. Wire (c) as "a Stage-0 `main auto-deploys` declaration promotes
  every merge to the deployment irreversible-signature class." New surface = one declaration + one
  wiring sentence, reusing proven logic ("division of labour, not redundancy", :218).

### (b) park-not-log — UNANIMOUS: needs a park reason + scope + consistency; sharp scoping from cold-Opus
- New park tag (e.g. `parked-unverified-posture-judgment`); **mechanical tier still runs**, only
  judgment-tier classes (input-trust, authz-correctness, PII) park; a feature carrying that park
  **stages whole** at Stage 5. Lands at SKILL.md:181, :225, seed L30–32 — consistent, and **must not
  read as "skip the check and proceed."**
- **Stage 5 only, NOT Stage 6** (cold-Opus): the judgment tier runs at both, but at morning the *human
  is* the decorrelated check — parking there is incoherent. Draft must affirmatively state Stage-6 is
  unaffected ("no non-quorum model at morning → the human runs the judgment tier"). Tempers agy's
  "add Stage-6 instructions": yes, but the instruction is *human-runs-it*, not *park*.
- **Rewrite, don't delete, the third open limitation** (:225) (cold-Opus): (b) closes the *safety* gap
  but opens a *delivery* limitation (no decorrelated seat → all judgment work stages, nothing
  auto-mergeable). Relocate the honesty; a drafter who deletes :225 makes the skill over-claim.
- **Admissibility bar for "decorrelated adjunct"** (cold-Opus, elevates the M3 wrinkle from note to
  rule): park-pain creates a perverse incentive to relabel a correlated model (bare-API M3, another
  Opus, same-family seat) as "decorrelated" to unblock — the skill's own "provisional is picking with a
  deniability label" anti-pattern (:99). Define it: a real non-quorum **bridge seat** per
  `using-agent-bridge`, not bare-API, not a voting-seat family. Verify M3's *seat* status, not just
  existence.

### THE missed structural insight (cold-Opus P1-6) — unify b and c as Stage-0 autonomy preconditions
(b) and (c) are the **same shape**: both a condition the unattended run must know up front
("is a decorrelated seat available?" / "does main auto-deploy?") that changes disposition wholesale if
bad. Neither prior voice framed them together. A walk-away user on a host that auto-deploys main or
lacks a decorrelated seat returns to a run that **staged everything and merged nothing, with no warning
at kickoff** — colliding with point 4 (staged-heavy) and point 5 (morning undersold). Fix: a single
**Stage-0 "autonomy preconditions" check, surfaced to the user as part of the one human touch** —
(1) main inert y/n, (2) decorrelated seat present y/n — and if either is bad, tell the user *"this run
will stage, not deliver"* so they decide at kickoff rather than discovering it next morning.

### (d), (a), point 5 — converging refinements
- **(d)** must be an **active pre-dispatch/pre-merge/pre-push guard, not a passive after-the-fact
  counter** (codex) — else budget expires *after* crossing the line it guards. Reuse the existing
  park-record machinery (bulk application, not new stop semantics; cold-Opus P2-2). **Wall-clock = hard
  ceiling; aggregate token spend across bridge seats may not be cleanly observable → token best-effort.**
- **(a)** flowchart = **derived index into the prose** with line/section anchors (incl. b/c/d
  decisions); mark **"navigation aid; prose governs on conflict"** (same discipline as `check-doc-drift`).
- **Point 5** needs more than a doc-note (codex): Stage 6 should require a **checklist/digest artifact**
  (each park reason + its command result), since the morning audit is silently rubber-stampable.

### Verification bar (prose skill → adversarial scenario-trace, strengthened)
- Embed **two explicit traces in the skill** (codex): (1) no decorrelated model + J-tier input-trust diff
  → mechanical runs, judgment parks, feature stages whole; (2) `main-inert: unknown` / deploy-hint
  detected → reversible 3/3-approved change stages, no merge.
- A trace passes **only if the amended text yields a matching Red-flag STOP line** (cold-Opus P2-1 — add
  STOP entries for b/c) and traces against a **lazy/adversarial 2am reader**, not a careful one.

**Net:** the panel turned "four edits" into "four edits + a unified Stage-0 autonomy-preconditions
concept + park-taxonomy additions + Red-flag STOP entries + two embedded traces." All within the skill,
branch-only. Bigger surface than the original plan, but it is what makes b/c real rather than theatre.

Reports: `/tmp/amh-panel/report-{codex,agy,coldopus}.md`.

## Open items (post-PR — not part of this change)

- **Promote M3 to a non-quorum bridge seat.** The admissibility bar means M3-as-bare-API-normalizer
  does **not** satisfy the judgment tier. So the `decorrelated-seat: no` case (b) guards is **live, not
  rare** until a proper non-quorum *bridge seat* exists — on such hosts the skill will genuinely park
  judgment-class work. That is correct behaviour (park-not-fake is the point), but it raises the
  adjacent decision: stand M3 up as a non-quorum bridge seat (distinct from its normalizer role,
  different access path) so the judgment tier has a real decorrelated adjunct and the park-case becomes
  rare. Separate from this skill PR.

## Draft status

Drafted on `feat/autonomous-mode-hardening` (branch-only). Changes to `skills/autonomous-mode/SKILL.md`
+ `posture-oracle.seed.md`, held to the two bars: **(b)/(c) wired as promotion-rules/tags into existing
machinery** (main-not-inert → existing deployment irreversible-signature class; no-decorrelated-seat →
new park *tag* through existing park-don't-halt — no parallel gates), and **two embedded scenario-traces**
whose pass condition is "no reading reaches proceed" for an adversarial 2am reader, each terminating in a
§ Red-flags STOP line. New sections: Decision procedure (quick-ref, prose-governs), Global budget,
Scenario traces; Stage-0 autonomy preconditions; new park tags `parked-unverified-posture-judgment` /
`parked-budget-exhausted`; third open-limitation rewritten (safety gap → delivery limit).

Status: **MERGED** to `main` at `70f8ade` (`--no-ff`), pushed to origin. Confirming adversarial
re-review passed both bars (codex SHIP, cold-Opus SHIP, agy FIX → clarity-only remediation `fc6ae6c`,
confine-checked: SHIP traces carry forward). Open follow-on: M3-as-non-quorum-bridge-seat (see Open items).

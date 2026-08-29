# Author bench — design/spec/plan authoring bake-off, systematized (design v2.1, panel rounds 1+2 absorbed)

> **Round 2** (`panel-capsuitecdesign-r2-20260709T010114Z-4c4fc2`): agy approve, GLM
> approve/P2, all round-1 dispositions verified real — but TWO new findings in the v2
> isolation rewrite itself, both absorbed in v2.1: **cold-Opus NEW P0** — the frozen brief
> bundle mounted `fact_key.yaml` (the R1/R6 answer sheet) into the AUTHOR workspace,
> turning the objective core into transcription → author-visible bundle now excludes the
> key, V2(d) exclusion deny-proof; **codex P1** — the Instrument-1 jail's engine
> auth/state mounts (persistent `agy-home`, `~/.codex`) are a cross-run
> persistence/leak surface for THIS bench → authorbench jail profile (fresh tmpfs HOME,
> read-only minimal auth, no persistent engine volume, all-mounts canary), V2(e).
>
> **Rounds 3+4:** r3 (`panel-capsuitecdesign-r3-20260709T011412Z-803194`) — convergent
> finding, all four seats (codex P1, three P2): V2(e) conflated author/judge mount
> surfaces (judges must mount the fact-key; an outcome canary would false-flag it) →
> per-role manifests + per-role canary token classes. r4 micro-recert
> (`panel-capsuitecdesign-r4-20260709T012126Z-bc0d9a`): certify quorum unanimous
> approve/none. **CERTIFIED at v2.1, zero P0/P1.**

Status: **v2 — round-1 panel findings absorbed** (run
`panel-capsuitecdesign-20260709T002811Z-418676`: codex BLOCK P0, cold-Opus needs-changes
P0, agy + GLM needs-changes P1; the bake-off hinge facts all verified accurate by GLM +
cold-Opus). v2 dispositions: **P0-α** (codex block; agy's judge-side variant) archive
export is not a confinement boundary — a same-host author/judge can read the canonical
outcome by absolute path; author AND judge turns now run inside the existing proven
confinement jail (`tools/eval/confinement/` — the Instrument-1 substrate) with only the
export mounted, which narrows v1's benchable authors to jail-runnable engines and makes
the claude-container expansion a NAMED PREREQUISITE for benching Anthropic-family authors
(D2, D3, V2); **P0-β** (cold-Opus; GLM same finding at P1) the license-to-exist cited
`learn-candidates.md` for an author-kernel claim that file does not make — re-grounded:
the rejection record is SILENT on authoring; distinctness is argued independently under
the §3 wall, and the /learn artefact's own round-3 verdict prose (GLM seat, artefact
`learn-arb-bench-role-scoped-red-team-benchmark-5ad6a61f` v4) separately noted the author
bench "has real precedent but is bundled with the foreclosed core" (§Why); **P1s:**
fact-key scoring no longer penalizes by-construction trap elimination (agy; R6 scoring
guide); blinding failure now quarantines + one mechanical redaction pass + rescan, and a
hot draft can never reach a judge (agy + GLM; D3, V1); baseline drafts are frozen per
corpus version — judged fresh each run, authored once (agy; D5, cost halved); fact-keys
gain a mechanical self-validator at `base_sha` (agy; V9); AB-D1 is explicitly
RECONSTRUCTED, not verbatim — claims downgraded, AB-S1/AB-P1 are the verbatim anchors
(codex + GLM; D1); the bench's construct is explicitly "authoring under tree-only access"
with the R2–R5 construct shift stated (codex + GLM; D2). P2 absorbs: §0-transposition
reworded as analogy not proof-transfer; `prior_exposure: possible` policy specified;
"N of M" notation replaces slash-ratios; per-seat report files separated.

Author: cold Fable seat (brief given as a standalone author brief, not included in this
copy), no session context; v2 remediation by the warm orchestrator.

Siblings in the same capability suite (separate designs, hard boundaries kept):
Design A = Instrument 1 completion (reviewer floor); Design B = implementor bench
(generation floor). This is Design C: the **authoring** floor — first-draft quality
for design / spec / plan artifacts.

## Problem — what exists, what is ad hoc (hinge facts, verified 2026-07-09)

There is exactly one live precedent: the 2026-07-03 arb-watch-history authoring
bake-off (report not included in this copy). Same brief, three independent authors — warm Opus inline, cold Sonnet and cold Fable in
isolated worktrees, "forbidden from reading Opus's draft" (`:3-4`). Verified facts about
that run:

- It produced real, decision-grade signal: the drafts converged on architecture and
  "separated on **how hard each verified the data source**" (`:9-11`); the judged
  strengths/misses table (`:15-17`) is per-dimension in substance (verification depth,
  elegance, specific catches, specific misses, cost) even though the headline was a
  head-to-head carry-forward decision.
- Its calibrated lesson (`:31-38`): author-perfection is not the goal — "the panel
  is the verification backstop"; default to the cheap author for code-grounded design,
  reserve Opus/Fable for "genuinely no-tools, wide-open, blank-page stages."
- Its pre-registered experiment resolved: the raw Sonnet draft went to the panel
  unmodified "to measure whether the decorrelated panel independently catches what
  Fable caught solo (the dead vote branch especially)" (`:41-45`). It did — codex's
  panel report finds `_emit_vote` bypasses `push_task_event`/`eval_event_raw` and the
  dead `voted` state, independently and with citations
  (`2026-07-03-arb-watch-history-design-panel-codex.md:34,39`, verdict
  `FIX_BEFORE_PLAN` at `:1`; remediated at `9edec6c`). The backstop thesis has one
  empirical confirmation, N=1.
- Everything else was ad hoc: the judge was the warm orchestrator (not blinded, not
  decorrelated — it was also Opus's *author*), there was no rubric, no stored scoring
  artifact beyond the prose record, no reproducibility (the judging cannot be re-run),
  no model-version keying, and the output shape (a winner carried forward) is exactly
  the comparative artifact the eval-suite wall forbids as standing machinery.

The decision this bench informs is real and recurring: the per-stage authoring
rotation (`skills/using-agent-bridge/SKILL.md:537-581`) asks the human ONCE per
pipeline run who authors design/spec/plan, with a cheap-author default justified by a
capability claim ("for tool-bound / code-grounded design Sonnet is at Opus-parity",
`:553-556`) that was calibrated on one bake-off and one model generation. Every model
change (new Sonnet, new Fable, a candidate non-Anthropic author) silently ages that
claim. Today re-checking it means re-improvising the 2026-07-03 run.

Why this design exists at all, given the arb-bench rejection: the /learn gate rejected
arb-bench because its core — a reviewer red-team bench feeding trust assignment — was the
foreclosed eval-suite v1 renamed. **The rejection record
(`docs/learn-candidates.md` Category 5) is SILENT on authoring — it neither forecloses
nor endorses an author bench, and its "legitimate kernel" is the reviewer floor
(Instrument 1), not this** (round-1 P0-β: v1 of this design claimed the record "treats
the author-bench kernel as distinct," which it does not — fabricated attribution,
corrected here). Distinctness is therefore argued on its own merits: authoring quality is
not a reviewer catch-rate (no seeded ground truth exists — §0's machinery doesn't even
transpose), and the consuming decision (who drafts first) is a human cost/capability
call, not a trust/quorum change — so the §3 wall governs the OUTPUT shape and this design
complies with it. Separately and accurately citable: the /learn eval's own round-3
verdict prose (GLM seat, artefact `learn-arb-bench-role-scoped-red-team-benchmark-5ad6a61f`
v4) noted "the author bench has real precedent (the 2026-07-03 bake-off) but is bundled
with the foreclosed core" — an observation about bundling, not an endorsement; this
design is the unbundled kernel, built inside the walls.

## What transfers from the reviewer-side proof — and what does not

`docs/eval-suite-design.md` §0 (`:11-17`) proves every internal catch-corpus blind to
the never-caught set: reviewer benches cannot measure a seat's marginal decorrelation
value. Engage it honestly for authoring:

**Does not transfer (the epistemics differ):** "good design" has no seedable ground
truth. There is no defect you can plant in a *brief* whose catch proves design
quality; the object of measurement is a generated artifact, not a detection event. So
Instrument 1's seeded-recall machinery does not transpose, and no part of this bench
claims a catch-rate.

**Transfers unchanged (the walls and the honesty disciplines):**

- A rubric-blindness limit **analogous to** §0 (GLM P2-3: this is an analogy, not a
  transfer of §0's proof — §0 proves a structural property of catch-corpora; the
  rubric point is the plainer observation that a finite checklist misses unlisted
  dimensions): a draft can be flawless on all six dimensions below and still embody a
  design error no judge sees. The bench is therefore a **floor + diligence read**,
  never a quality guarantee — the claim-limiting language stands on its own without
  borrowing §0's proof-force; the live panel on real work remains the backstop (the
  bake-off's own lesson, now with its empirical confirmation above).
- The §3 wall, verbatim in spirit (`eval-suite-design.md:57-70`): no ranking, no
  composite verdict, mechanical output separation, report informs a human only.
- `docs/measurement-principles.md` P1 (`:9-26`): briefs are the correlated-instance
  hazard — three briefs from one repo's domain are not three independent samples of
  "authoring ability." P2 (`:89-100`): a per-seat-per-dimension grid is
  reader-convertible into a ranking; minimize the convertible surface and state the
  residual, don't claim a wall that doesn't exist.

**Partially transfers — the one objective foothold:** briefs sourced from real past
work arrive with a **historical fact-key**: hinge facts that were verified during the
real run (e.g. "`ok` IS in `EVAL_ALLOWLIST`; `model`/`engine_model` are NOT; votes do
NOT reach `eval_event_raw`; no `sent_at` index" — bake-off `:21-22`) and **known
traps** the real process caught (the dead `vote→voted` branch, the run_id-gated eval
plane). Checking a bench draft against the key measures *verification diligence and
trap-avoidance* — objectively, reproducibly — without pretending to measure design
goodness. This is the authoring analogue of Instrument 1's floor-capability claim,
and it is the load-bearing objective core of the bench.

## Constraints (hard walls, restated as design invariants)

1. **Report-only.** No output field feeds trust, quorum, routing, or the rotation
   default automatically. The report is evidence attached to the human's ask-once
   rotation decision (`SKILL.md:550-558`), nothing else.
2. **No composite "best author" verdict, no ranked output.** Per-dimension evidence is
   what the human uses (the bake-off's carry-forward decision was made on the
   dimension table plus cost, not on a total score).
3. **Blinding is mandatory.** Judges never see author identity; identity-bearing
   surface is stripped mechanically, not by convention.
4. **Reproducible from stored artefacts.** A run's mechanical results must be exactly
   re-derivable from what was stored; the judged phase must be re-runnable as a new,
   separately-stored judging pass.
5. **Cost-shaped for one candidate.** Authoring runs are frontier-priced (bake-off
   cost column: Opus $5/$25 per M, Fable ≥ that, `:15-17`); the unit of use is ONE
   candidate against a small fixed baseline, not a fleet sweep.

## Design

### D1 — bench shape: one bench, stage-tagged briefs

One mechanism, not three benches. A bench **brief** is a frozen bundle:

- `stage`: `design | spec | plan` — the rotation asks the question per stage, so the
  bench answers per stage; but the run/judging/report machinery is identical, and a v1
  corpus of one brief per stage exercises all three for the price of three runs.
- `brief.md`: the authoring brief text, verbatim from the committed original where one
  exists (7 authoring briefs are already committed as review records, not included in
  this copy — e.g. the arb-visibility-web design authoring brief).
- `base_sha`: the pinned repo commit the brief was really authored against — for the
  arb-watch design brief that is `9de2850` (parent of `75d9f45`, the commit that added
  the Opus draft). Pinning at the *pre-outcome* SHA makes hinge facts stable and
  checkable forever, and (with D2's export isolation) puts the canonical outcome out
  of the author's reach.
- `fact_key.yaml`: the historical fact-key — `facts:` (each with the `file:line`
  evidence at `base_sha`) and `traps:` (each with the historical catch citation, e.g.
  panel-codex `:34`). Compiled once per brief by the orchestrator from the committed
  panel reports and bake-off/remediation records; versioned.
- `length_budget`: a soft cap (house exemplars run ~200–350 lines; the brief says
  "aim ≤ 300 lines") so drafts land in a comparable band and verbosity bias has less
  raw material.

**Comparability across runs and models** comes from freezing all of it: same brief
text, same `base_sha`, same fact-key version, same normalizer version ⇒ the
mechanical sub-scores are comparable across time by construction. Judged sub-scores
are comparable only *within* a run (judges drift too — see D5).

**Corpus v1 (three briefs, corpus version `v1.0`):**

| id | stage | source | base_sha | key source |
|---|---|---|---|---|
| AB-D1 | design | arb-watch-history design brief — **RECONSTRUCTED** (no committed original exists; codex verified `git log --all` shows none) | `9de2850` | bake-off `:21-22` + 3 panel reports + `9edec6c` remediation |
| AB-S1 | spec | `2026-07-03-arb-visibility-web-spec-authoring-brief.md` — **verbatim** | parent of the committed spec | spec panel reports |
| AB-P1 | plan | `2026-07-03-arb-visibility-web-controls-plan-authoring-brief.md` — **verbatim** | parent of the committed plan | plan panel reports + the live P0 catch record (`arb-visibility-web-controls-build-status`) |

**AB-D1 fidelity (round-1 codex P1-2 + GLM P2-2, the asymmetry named):** AB-D1's brief
is a reconstruction from `75d9f45`'s inputs + the bake-off record — it may not
reproduce the exact epistemic situation the 2026-07-03 authors faced. It is marked
`provenance: reconstructed(v<N>)` in the bundle with the reconstruction procedure
versioned alongside; claims from AB-D1 runs carry the reconstruction caveat, and
**AB-S1/AB-P1 (verbatim committed briefs) are the corpus's anchor samples.** If the
reconstruction proves contentious at the live gate, AB-D1 is swappable for another
verbatim-briefed design task without touching the machinery.

Sourcing stance: **real past briefs only in v1.** They are free (already written),
realistic by construction, and carry the fact-key for free. Synthetic briefs are a v2
option if/when the corpus burns (below) — they trade the fact-key's authority for
freshness and must earn their way in via their own design pass.

**Burn rule (does a brief burn?).** For the seats this bench is allowed to measure —
**cold, isolated authors** — a brief does not burn by mere reuse: a cold seat has no
cross-run persistence, and D2 removes every in-run channel to prior outcomes. A brief
IS burned for: (a) the warm orchestrator (session + auto-memory contamination — see
Non-goals: the warm inline author is structurally unbenchable), (b) any seat granted
ARB Memory or repo-history access at authoring time (D2 forbids this; an override is
recorded), (c) a model whose vendor training window could plausibly include this
repo's outcomes — noted per-run as `prior_exposure: none-known | possible(<reason>)`,
a named honesty field, not a guarantee (private repo, so the practical risk is (a)
and (b)). **Policy for `possible` (GLM P2-5, disposition specified):** reason class
`public-mirror|fork-known` REFUSES the run without an explicit recorded override
(same shape as V6's burned-pair refusal); any other reason class flags the artefact
and proceeds. The **exposure ledger** (part of the stored run artefact) records every
(seat-lineage, brief) authoring event so rule (a)/(b) violations are checkable.

### D2 — run mechanics: structural isolation, not configurational (rewritten per round-1 P0-α)

The 2026-07-03 run used worktrees. Worktrees are the wrong tool here:
`docs/multi-model-consensus.md:128-131` is explicit that a worktree "is **not** a
filesystem sandbox" — worktrees share the object DB and refs, so `git log --all` from a
pinned-SHA worktree reaches the *future* canonical design and every panel report. **And
v1's archive-export alone was not the fix either (the round-1 codex BLOCK): an export
removes in-tree leakage, but an author running on this host can still `cat` the canonical
outcome from the live checkout by absolute path — cwd is not a read boundary.** The leak
must be impossible by construction at the FILESYSTEM level:

- **Author turns run inside the existing confinement jail**
  (`tools/eval/confinement/` — the Instrument-1 substrate: container with no host mounts
  beyond the task directory, canary-verified, already held against an actively escaping
  seat, `docs/eval-seat-confinement.md:7-8`). The author's mounts are the **author
  workspace = `git archive <base_sha> | tar -x`** (no `.git`, no refs, no history) plus
  the **author-visible subset of the brief bundle: {`brief.md`, `length_budget`, steer
  blocks} — NEVER `fact_key.yaml`** (round-2 cold-Opus P0: the fact-key is the R1/R6
  answer sheet; mounting the whole frozen bundle handed the author the facts and traps it
  is being scored on discovering, turning the bench's objective core into transcription.
  The key goes only to judges and the mechanical checkers, post-authoring). The canonical
  outcome is absent from the container's universe — unreachable by path, not merely
  absent from cwd.
- **Authorbench jail profile (round-2 codex P1 — the Instrument-1 jail as-is mounts
  engine auth/state: `~/.codex/auth.json`+`config.toml` and a PERSISTENT `agy-home`
  volume, `confined-review.sh:31,42`, which the confinement README excludes from its
  absence proof; for THIS bench persistent engine state is a cross-run
  persistence/outcome-leak surface, not just an excluded path):** author and judge runs
  use an authorbench-specific runner profile — fresh per-run HOME on tmpfs, the minimal
  auth material copied in read-only (auth tokens only, no history/session/config state),
  NO writable persistent engine volume (no `agy-home` reuse across bench runs), and a
  pre-dispatch canary that greps EVERY mounted path for outcome/author-identity tokens
  (canonical design/panel-report globs + author-seat tells) before any author or judge
  turn starts. V2(e) pins this profile.
- **Scope consequence, stated honestly:** v1 benchable authors = engines runnable in the
  jail (codex, agy today). **Benching Anthropic-family authors — the rotation's main
  question — REQUIRES the containerized Claude Code seat first** (the sanctioned
  expansion, `eval-seat-confinement.md:117-123`; also named in Design A §7). That
  ordering is a prerequisite, not a footnote: the bench ships v1 proving the machinery on
  jail-runnable engines, and the claude-container lands before the first
  rotation-relevant run. A bare-API author (pi-GLM-class) needs no jail at all — it has
  no filesystem (the packet inlines the workspace listing + files on demand is NOT
  workable for authoring; bare-API authors are out of v1 scope, named).
- Named cost (round-1 codex P1-3 + GLM P2-1): the author loses history archaeology
  (`git log`/`blame`) — **the bench's construct is therefore "first-draft authoring under
  tree-only access," a deliberate narrowing from "authoring as the rotation does it."**
  The mechanical core (R1/R6 — everything checkable is checkable at `base_sha` in the
  tree) is unaffected; the judged dimensions R2–R5 may under-read authors whose diligence
  routes through history archaeology, and the report's construct-validity block says so
  per-dimension. A future-ref-pruned history export (shallow clone truncated at
  `base_sha`) is the named v2 upgrade if the narrowing proves material.
- **Author env: no ARB Memory MCP, no bridge inbox, no network beyond the model API.**
  The dispatch brief is self-contained. (ARB Memory contains the bake-off records and
  build-status notes — a memory-enabled author is an open book of answers.)
- **Independence:** authors in the same run never share a workspace and write drafts
  outside any shared checkout until all finish — the review-hygiene rule applied to
  authoring (`multi-model-consensus.md:114-119`).
- **Fable-author steering** (when a candidate is Fable-class): the bench brief carries
  the repo's brief-steer blocks — give the reason not only the request
  (`docs/prompting-claude-fable-5.md:128-131`), scope restraint (`:53`), grounded
  claims (`:77`) — and must NOT ask the author to transcribe its reasoning
  (`reasoning_extraction` refusal risk, `:180`). These blocks are part of the frozen
  brief bundle so all seats in a run receive byte-identical instructions.

### D3 — judging: blinded, absolute-per-draft, bias engaged by construction

**Rubric — six dimensions, derived from what the house exemplars actually do well**
(`2026-07-08-agy2-dark-channel-design.md`, `2026-07-08-cdx1-approval-handling-design.md`):

| dim | what it measures | how scored |
|---|---|---|
| R1 hinge-fact verification | claims cited to `file:line` and TRUE at `base_sha` | **mechanical** (all citations) + judge spot-read (5 sampled citations) |
| R2 constraint coverage | every brief constraint addressed, none silently dropped | judged, anchored, evidence-quoted |
| R3 rejected alternatives | alternatives named with reasons that engage real trade-offs (cf. AGY-2's rejected synthetic tick, CDX-1's four rejections) | judged, anchored |
| R4 verification obligations | obligations falsifiable; adversarial where guarding (delete-the-guard ⇒ red) | judged + mechanical count of obligations carrying a concrete red condition |
| R5 trade-off / unknown honesty | uncertainties named (cf. CDX-1's "wire-shape uncertainty (named)"), no fabricated confidence | judged; cross-checked against `fact_key.traps` |
| R6 trap avoidance | did the draft carry a known historical trap (dead vote branch class)? | judged **with the fact-key in hand** — near-mechanical. **Scoring guide (round-1 agy P1-2):** a trap is AVOIDED if the draft's structure renders it moot — an alternative design that eliminates the trap's precondition by construction scores avoided, explicitly REWARDED, not penalized for failing to match the historical layout; the fact-key states each trap's *precondition*, not its historical code shape, so the check is "does the precondition survive in this draft" |

Anchors are behavioral, 0/1/2 per dimension, and every judged point requires a quoted
evidence line from the draft — presence/absence of specific behaviors, never
"thoroughness," so length alone cannot earn points.

**Who judges:** a fixed per-run panel of three decorrelated non-author seats spanning
model families (default: codex + agy + pi-GLM — the standing quorum families). When
an Anthropic seat is among the *authors*, any Anthropic judge is admissible but
flagged non-certifying in the report metadata — the certify-quorum discipline
transposed to scoring confidence, and the self-preference residual named rather than
hidden. Judges receive: the normalized draft, the brief, the fact-key, the rubric,
and the `base_sha` tree export (so R1 spot-reads are checks against source, not
vibes). **Judge isolation (round-1 agy P1-1 — an unjailed judge defeats blinding by
construction):** the live repo contains the committed author drafts and bake-off
records, so a judge running in repo context can grep the normalized draft's content
back to its attributed original in seconds. Judge turns therefore run in the SAME
confinement shape as authors: filesystem-capable judges (codex, agy) run in the jail
with only {export, draft, fact-key, rubric} mounted; the bare-API judge (pi-GLM) gets
the same set inlined as a packet — it has no filesystem to leak through. A judge is
never dispatched into the live checkout. Judge dispatches follow review hygiene:
independent phase, reports outside any shared surface until all land
(`multi-model-consensus.md:114-119`).

**Bias classes, each with a concrete countermeasure**
(`docs/learn-candidates.md:37` names the classes; the redirect ruling at `:44-52`
scopes what we may build — no new calibration subsystem):

- **Position bias — eliminated structurally, not mitigated.** Judges score ONE draft
  per context, absolutely, against anchors. There is no comparative presentation, so
  there is no position to be biased by — and no pairwise preference for a ranking to
  leak out of. (Supporting evidence that presented-option sets anchor judgment:
  `docs/quorum-decision-taxonomy.md:41`.) The cost — absolute scores are noisier than
  pairwise — is paid deliberately: the anchors + evidence-quote requirement + the
  mechanical core (R1/R6) carry the discriminative load.
- **Verbosity bias:** length budget in the brief (D1); length reported as a separate
  descriptive stat next to every judged read; anchors keyed to specific behaviors
  with quotes. The report's construct-validity note instructs the human reader to eye
  the length column against the judged column.
- **Self-preference bias:** blinding (below) removes the label; style recognition
  survives blinding, so the residual is handled by family-spanning judges,
  per-judge (never pooled-only) reporting — a family-correlated inflation shows as
  visible judge disagreement — and the flagged same-family metadata above. Stated as
  accepted residual per P2 discipline, not claimed solved.
- **Judge drift over time:** judged scores are within-run only (D5's pairing rule);
  no ruling from the calibration redirect is violated because nothing here builds a
  judge-calibration mechanism — observed judge disagreement is simply stored, and MAY
  later be fed to the existing model-version-aware calibration loop
  (`2026-07-07-calibration-model-version-SPEC.md`) by its own rules; not in v1 scope.

**Blinding mechanics (mandatory wall):** a mechanical normalizer strips author
identity before any judge sees a draft: authorship/status headers, model
self-references, seat ids, dates/session markers, run labels; heading style and
list markers normalized. The normalizer is versioned; raw + normalized drafts are
both stored. A **blinding denylist scan** (model names, seat ids, harness tells)
runs post-normalization. **Disposition on a hit (round-1 agy P1-3 + GLM P2-4 — "fail
loud" alone made the bench abort on any stray model-name mention, and left the run
fate unspecified):** the draft is QUARANTINED (structurally unable to reach a judge —
judges read from a staging dir that only the scan populates); the harness applies ONE
mechanical redaction pass (matched tokens → neutral placeholders, edit logged) and
rescans; still-hot after redaction → the run aborts loudly naming the token class.
Denylist scope note: this repo's subject matter legitimately contains seat-id-like
strings — the denylist matches AUTHOR-identity tells (the authoring seat's own
name/model/lineage), not every seat id in technical content; the redaction log makes
over-fires visible and V1 tests both polarities (under-fire: seeded marker must
quarantine; over-fire: a fixture draft ABOUT seat ids must pass clean).

### D4 — scoring and report shape: per-dimension evidence, no winner

The 2026-07-03 bake-off DID compare head-to-head — and that was the right shape for
what it was: a one-off, human-judged carry-forward decision with full context, whose
comparative table fed a cost argument. As *standing machinery* the same shape is the
foreclosed artifact: a repeatable winner-emitter is exactly what the arb-bench
rejection and the §3 wall exist to prevent, and P2 says even honest grids are
reader-convertible. Decision:

- **Headline:** ONE REPORT FILE PER SEAT (`report-<seat>.md` — agy P2: side-by-side
  grids in one file invite the visual ranking the wall refuses to write; separate
  files keep the comparison a deliberate human act), each a `dimension × brief` grid
  of `MET / PARTIAL / NOT-MET` (from median judge anchor, with R1/R6 mechanical
  results able to cap the cell — a draft whose citations fail the mechanical check
  cannot show R1 MET regardless of judge reads). Counts written as prose ("7 verified
  of 9, 2 fabricated" — GLM P2-6: slash-ratios read as sortable rates), never
  percentages, in the headline.
- **Detail:** per-judge anchors + evidence quotes; mechanical records; length stats;
  judge-disagreement noted per cell.
- **Coded wall:** the reporter refuses to emit fields named
  `winner|rank|total|composite|score_sum` and refuses any cross-seat sort key —
  denylist in code, test-pinned, mirroring Instrument 1's report wall
  (`eval-suite-design.md:66-69`). Output namespace is the bench's own
  (`tools/authorbench/` results + ARB Memory `author-bench/*` keys), mechanically
  separate from Instrument 1 artifacts.
- **Named residual (P2):** the grid remains reader-convertible into a ranking by a
  motivated human. Stated in the report's standing construct-validity block as
  inherent-and-accepted; the human it informs is the same human the rotation decision
  belongs to.

### D5 — statistical honesty: enumerative evidence, paired baseline, no rates

One brief = one sample, and the samples are correlated (one repo, one domain — P1's
correlated-instance hazard verbatim). v1 runs 1 repeat per (seat, brief): with N≤3
correlated samples, **no rate, CI, or capability claim is honest**. The report
template hardcodes the claim grammar:

> Supported: "seat S (model M, corpus v1.0, run R): on AB-D1 verified 7/9 key facts,
> avoided trap T1, carried trap T2 (quote), NOT-MET on R3 (evidence)."
> Not supported: "S is a better design author than T"; "S has improved"; any
> sentence about S unqualified by brief and run. All reads are **indicative**, not
> capability measurements.

**Cross-time comparison is design-constrained, not prose-warned:** because judges
drift, a candidate is never compared against *stored judged scores*. **Baseline shape
(round-1 agy P1-4 — v1's regenerate-the-baseline-every-run doubled the dominant cost
for nothing):** baseline DRAFTS are **frozen** — authored once per corpus version by
the current rotation-default model (cold, jailed, same mechanics), stored in the run
bundle namespace, refreshed only when the rotation default or corpus version changes.
Each bench run authors the CANDIDATE only, then the run's judges score candidate +
frozen-baseline drafts fresh in the same sitting — judge-drift control is preserved
(same judges, same sitting) at half the authoring cost. Mechanical sub-scores (R1
citation checks, R6 trap hits, obligation counts) ARE comparable across runs at equal
corpus+key+normalizer versions — they are deterministic — and the report marks only
those columns cross-run-comparable.

### D6 — storage, trigger, cost

- **Storage:** append-only NDJSON per run under the bench's own namespace
  (authoritative, mirroring the eval suite's evidence rule,
  `eval-suite-design.md:78-79`), plus an ARB Memory artefact per run keyed
  `author-bench / <author-seat> / <author-model-version> / <corpus-version> /
  <run-id>`, containing or referencing: brief bundle hashes, `base_sha`, raw +
  normalized drafts, normalizer + fact-key versions, judge set with judge model
  versions, per-judge raw outputs, mechanical records, exposure-ledger entries, and
  the report. **Reproducibility contract:** the mechanical phase re-derives
  byte-identically from the stored bundle; a judged phase re-run stores under a new
  judging-run id and never overwrites (append-only, evidence-store no-silent-drop).
- **Trigger:** manual runbook/CLI invocation on a model-change event — a new
  candidate author model, a version bump of the rotation's default author, or the
  human revisiting the rotation. No watcher, no automation (consistent with the
  calibration SPEC's manual, report-only posture).
- **Cost envelope (design target, to be measured at the live gate):** one run on the
  v1 corpus = **3 candidate authoring turns** (baseline drafts are frozen — authored
  once per corpus version, not per run; agy P1-4) (frontier-priced: ~100–200k in /
  ~5–10k out each ⇒ order $1–5 per Opus-class turn at $5/$25 per M, bake-off `:16`;
  Fable higher) + 3 judges × 6 drafts (candidate + frozen baseline) = 18 judge reads
  on jailed/bare-API seats (draft + key + rubric + targeted source reads) + mechanical
  checks (negligible). **Order-of-magnitude: ~$10–35 API cost per run after the
  one-time baseline freeze; hours, not days, wall-clock.** Benching one candidate =
  one run; there is no fleet-sweep mode in v1.

## Rejected alternatives

- **Head-to-head comparative judging with a carried-forward winner (the 2026-07-03
  shape, made standing).** Rejected: position bias is structural in pairwise
  presentation; a repeatable winner-emitter is the arb-bench foreclosed core renamed;
  N=1-per-brief winner claims are statistically indefensible. The one-off human
  version was fine; the machine version is the wall's exact target.
- **Pairwise-preference LLM judging (the standard alignment-eval shape).** Same
  grounds, sharper: pairwise output IS a ranking; every published position-bias
  result applies; and it discards the anchored-evidence trail the human actually
  uses.
- **Seeded-defect briefs (plant flaws in the brief, score authors on catching
  them).** Rejected: transposes Instrument 1's mechanism into a domain where it
  measures brief-reading adversariality, not authoring; and the §0 blindness argument
  applies to the seeder verbatim. The historical fact-key achieves the objective
  foothold without a seeding fiction — its facts were verified by reality's process,
  not planted.
- **Synthetic brief corpus in v1.** Rejected for now: unknown realism, no fact-key,
  authoring cost to build; real committed briefs are free and keyed. Revisit as v2
  only if the burn rule ever actually bites (it does not, for cold seats).
- **Worktree isolation for authors.** Rejected on verified mechanism: shared object
  DB/refs make the canonical outcome reachable (`git log --all`) — accident-
  prevention where structural absence is available (`multi-model-consensus.md:128-131`;
  archive-export instead).
- **Judging by the live panel inside the normal pipeline.** Rejected: contaminates
  live calibration data with bench traffic, spends panel capacity, and panels review
  for shipping (verdict-shaped), not for anchored scoring.
- **Automatic trigger on model-version detection / auto-feed to the rotation
  default.** Rejected: report-only wall; the rotation choice is the human's
  (surface-forks-don't-pick-silently), and the calibration precedent is manual.
- **Building judge-bias measurement into the bench.** Rejected: the /learn
  judge-calibration ruling explicitly redirected that to the existing live
  calibration loop, "NOT a replay corpus or a new subsystem"
  (`learn-candidates.md:49-52`). The bench stores judge disagreement as data and
  builds nothing on it.

## Verification obligations (v1)

- **V1 — blinding red-green + quarantine (round-1 additions):** normalizer strips a
  fixture set of identity markers (model names, seat ids, headers, session labels);
  denylist scan then passes; seed one marker past the normalizer ⇒ draft is
  QUARANTINED and **provably cannot reach the judge staging dir**; the mechanical
  redaction pass + rescan path is exercised; a fixture draft ABOUT seat ids (this
  repo's legitimate subject matter) passes clean (over-fire polarity). Delete the
  scan ⇒ test red (deny-proofs-need-adversarial-verification).
- **V2 — isolation proof, both polarities (rewritten per P0-α):** (a) in-tree: the
  archive-export at `9de2850` contains no path matching the canonical
  design/panel-report globs (detector green); the same detector on an export at
  `33a09a6` MUST find them (detector not vacuous). (b) **filesystem: from inside the
  author jail, a scripted read attempt of the canonical outcome's ABSOLUTE host path
  fails (no such mount) — and the same attempt run unjailed on the host SUCCEEDS,
  proving the jail (not the cwd) is the boundary.** (c) author-env assembly asserts
  no ARB Memory MCP configured; judge staging contains only {export, draft, key,
  rubric}. **(d) author-mount exclusion (round-2 cold-Opus P0 deny-proof): the
  author's mounted set is asserted to contain NO `fact_key.yaml` (and no file matching
  the key schema); planting the key in the author staging dir ⇒ prep fails loud.
  (e) jail-profile proof (round-2 codex P1; scoped PER ROLE per r3 cold-Opus P2 — the
  judge legitimately mounts the fact-key, which contains outcome citations an
  outcome-token canary would false-flag): AUTHOR mounts asserted exactly {workspace,
  author-visible bundle, tmpfs HOME with read-only auth} with an outcome-token +
  author-identity canary over every mount; JUDGE mounts asserted exactly {export,
  draft, fact-key, rubric, tmpfs HOME} with an author-identity-token canary only.
  Seeding the respective forbidden token class into any mount ⇒ canary red; the agy
  persistent volume is asserted ABSENT from both profiles.**
- **V9 — fact-key self-validation (round-1 agy P1-5):** a mechanical validator runs
  every `fact_key.yaml` against its `base_sha` export: each `facts[].file:line` must
  exist and match its quoted pattern; each `traps[].precondition` must be checkable
  (its named file/symbol exists at `base_sha`). A key that fails validation blocks the
  run (a wrong key silently mis-scores every draft). Runs in CI on the frozen corpus.
- **V3 — citation checker red-green:** fixture draft with one fabricated
  `file:line` ⇒ flagged as mechanical R1 failure; all-true fixture passes; checker
  removed ⇒ test red.
- **V4 — report wall:** attempt to emit `winner`/`composite`/sort-key ⇒ reporter
  raises; headline cells carry counts, never rates; wall test mirrors Instrument 1's.
- **V5 — reproducibility:** from a stored run bundle, re-running the mechanical
  phase reproduces the NDJSON mechanical records byte-for-byte; a judged re-run
  lands under a new judging-run id with the original untouched.
- **V6 — exposure ledger:** authoring a (burned-seat-lineage, brief) pair refuses
  without an explicit recorded override.
- **V7 — hermeticity:** V1–V6 run in CI on fixture drafts with zero live engines
  (CI has none — standing scar).
- **V8 — live gate (REQUIRED, per live-verification-catches-cli-glue):** one real
  end-to-end run — AB-D1 only, candidate authored fresh in the jail + the frozen
  baseline draft, 3 judges (jailed/bare-API per D3) — executed before v1 is called
  done: draft produced in the jailed export, blinding scan green, judges return
  anchored scores with quotes, per-seat reports render inside the wall, run artefact
  stored and mechanically re-derived. Measured cost recorded into D6's envelope.
  Candidate/baseline for this gate = jail-runnable engines (codex or agy class) —
  the Anthropic-author question explicitly waits on the claude-container (D2).

## Non-goals

- Reviewer floor-capability and implementor generation floors (Designs A and B;
  separate docs, separate output namespaces).
- Any trust/quorum/routing/rotation-default change from bench output — report-only,
  forever, by code where code can hold it.
- **Benching the warm inline orchestrator.** The rotation's `inline` option cannot be
  measured cold — the warm seat's session and auto-memory contain this repo's
  outcomes, so any run is contaminated by construction. The cold-subagent baseline of
  the same model is the honest proxy; the report names the gap. (Open question for
  the panel: is the proxy close enough to inform the inline-vs-subagent branch of the
  rotation, or only the model-choice branch?)
- Judge-calibration mechanisms (redirected to the existing live loop by ruling).
- Synthetic briefs, brief-corpus growth policy beyond v1.0, and paneling bench drafts
  through the real pipeline (a v2 candidate for R6-by-reality: send one bench draft
  to a real panel and use its verdict as the trap detector — deferred, cost).
- Statistical capability claims at any N reachable by this corpus.
- Model-training-contamination detection (named per-run as `prior_exposure`, not
  solved).

## Open questions (round 1 resolved #2; the rest stand)

1. Should R6 (trap avoidance) gate the headline the way R1 does (mechanical cap), or
   is trap-carriage sometimes defensible enough to stay judge-scored? (The dead vote
   branch was carried by two of three real authors and still produced a shippable
   pipeline — the panel caught it.)
2. ~~Paired-baseline 2× cost~~ — **RESOLVED by round-1 agy P1-4:** baseline drafts are
   frozen per corpus version (authored once); every run still judges candidate +
   frozen baseline fresh in the same sitting, so judge-drift control survives at ~half
   the authoring cost. Mechanical-only solo re-runs remain sanctioned for the
   deterministic columns.
3. Corpus v1.0 picks (AB-S1/AB-P1 sourcing): confirm the two non-design briefs have
   fact-keys rich enough to be worth freezing, or swap for richer-keyed candidates
   (they are now the corpus ANCHORS given AB-D1's reconstructed provenance).
4. **Claude-container ordering (new, from P0-α):** build the containerized Claude Code
   seat before or after the v1 live gate on jail-runnable engines? (v1 machinery can
   ship and be live-gated on codex/agy authors; the rotation's Anthropic question
   waits on the container either way.)

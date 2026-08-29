# Instrument 1 completion — reviewer floor-capability, the pending increment (design v2, panel round 1 absorbed)

Status: **v2 — round-1 panel findings absorbed** (run `panel-capsuiteadesign-20260709T002811Z-f20055`,
unanimous needs-changes, max P1, no P0, no wall breach; all hinge facts verified accurate by
all four reviewers). v2 dispositions: **P1-α** (cold-Opus P1-1 + agy F1) infra failure was
routed to detection-miss and could fake a FAIL — replaced by incomplete-repeat → UNKNOWN(infra)
semantics (§1.4, §2 A1); **P1-β** (codex P1 + cold-Opus P1-2) model provenance was CLI-version
mislabeled as model identity — replaced by pinned-flag + readback capture with a
key-changes-on-model-bump deny-proof (§2 A2); **P1-γ** (GLM P1-2) `harness_version` mis-keyed
on dirty trees — `describe --dirty` + publish refuses dirty (§2 A2, §6); **P1-δ** (agy F2 +
GLM P2-1) gold rating had no automation and bare-API raters couldn't verdict — `gold rate`
verb + inlined-snippet packets (§4); **P1-ε** (GLM P1-1) publish "gold status" could leak
matcher recall/precision past the name-denylist — gate-outcome-only field + matcher_recall
injection deny-proof (§4, §6, H6). P2 absorbs: pairwise Cohen's κ named (agy P2); stale
confinement citations folded into the staleness cleanup (codex P2); `assert_verdict_row`
scoped to grid rows (GLM P2-2); harvested-control cluster adjudication named + anti-oversplit
lint (GLM P2-3, cold-Opus P2); marker-freedom extended to commit messages/tags/branches
(GLM P2-4); nonce-fenced engine-version markers + deny-proof (GLM P2-5).

Author: cold design author (standalone author brief, not included in this copy), no
session context; v2 remediation by the warm orchestrator.

> **Round 2** (`panel-capsuiteadesign-r2-20260709T010114Z-64c49e`): unanimous approve —
> certify quorum (codex+agy+GLM) all severity none; cold-Opus P2 (state the gold-field
> key-ALLOWLIST explicitly in §6/H6 — folded into the SPEC). **CERTIFIED at v2, zero
> P0/P1.**
Binding upstream: `docs/eval-suite-design.md` (v3 decision record — §0 proof, §3 Instrument 1),
`docs/eval-instrument1-v0-schema.md` (v0.2, settled), `docs/measurement-principles.md` (P1/P2),
`docs/eval-p3-fixture-corpus-design.md` (human resolution: effective-N DONE, real bases NEXT),
`docs/eval-seat-confinement.md` (human chose C — container/absence confinement; BUILT).
This design completes Instrument 1; it does not reopen any of those decisions.

## 0. Hinge facts — what is ACTUALLY built (verified against source, 2026-07-09)

The brief and `tools/eval/README.md` describe `pipeline.py` as "pending". **That is stale.**
Verified by reading the code and running the suite (58 tests green; README claims "13 tests"
at `tools/eval/README.md:37` and "`run` → NotImplementedError" at `:41` — both false today,
`cli.py:102-140` wires `run` to a working `pipeline.run_floor`). What exists:

- **Dispatch, two paths.** `BridgeDispatcher` (`pipeline.py:56-143`): builds the
  investigate-a-real-checkout review brief (`_review_prompt`, `pipeline.py:98-116` — points the
  seat at `git -C <repo> diff base..head`, demands tool investigation, never hands diff text),
  invokes `scripts/agent-dispatch` with `FROM_AGENT_ID`/`BRANCH`/`AGENT_ENV_FILE` env and
  `--run-id` threaded from the run's own id (`pipeline.py:78,129`, satisfying the run-id
  discipline in `skills/using-agent-bridge/SKILL.md`), parses the reply envelope, raises
  `DispatchError` on failure. **As built, a `DispatchError` is scored as detection-miss
  (`pipeline.py:687-710`) — the round-1 panel identified that as a faked verdict (an infra
  flake zeroes `caught_k` via worst-case pooling and can render FAIL); A1 CHANGES it to
  incomplete-repeat → UNKNOWN semantics.** **But this path is integrity-gated OFF:** `assert_read_confined`
  (`pipeline.py:80-96`) Parks every live bridge dispatch, because bridge/engine sandboxes
  restrict writes, not reads — an unconfined seat can read the answer-key
  (`docs/eval-seat-confinement.md:24-30`; a live seat was observed searching for it, `:17-20`).
  The only escape is the explicit integrity-UNGUARANTEED diagnostic flag.
- **The scored path is the `ContainerDispatcher`** (`pipeline.py:146-175` +
  `tools/eval/confinement/`): per-dispatch canary proves the answer-key absent from the jail
  before any scored read (`confined-review.sh:19`), codex and agy both verified confined
  (`confinement/README.md:26-32`; the jail held against an actively escaping agy —
  `docs/eval-seat-confinement.md:7-8`). Confinement is proven by the canary, not a settable
  var (`pipeline.py:154`, test `test_bridge_path_is_never_confined_and_env_var_cannot_spoof_it`).
- **Dispatch unit is seat × repeat, not seat × instance × repeat.** One review pass covers all
  seeds AND all control loci of the scenario (`run_floor` loop, `pipeline.py:667-676`); noise is
  the same pass crying wolf on controls — mandated by schema §0.5 ("the seat does ONE review
  pass"). The README's/brief's "per seat×instance×repeat" wording is the stale part; the built
  shape is the schema-correct one. This design keeps it.
- **Segment → normalize → match → NDJSON → report: built and pinned.** `segment_reply`
  (`pipeline.py:327-348`, bundle/no-op/JSON hardened); off-quorum M3 normalizer
  (`AnthropicNormalizer`, `pipeline.py:247-306` — temp=0, thinking-disabled, quorum-collision
  guard `pipeline.py:229,261`, fails loud to `class:"unknown"` → matcher-ambiguous, never
  drops); matcher with class + symbol/function/window precedence and the
  cross-function-boundary fix (`match_finding`, `pipeline.py:395-421`); seed-precedence +
  one-control-per-finding dedup (`pipeline.py:696-729`); append-only NDJSON authoritative
  (verdict re-derived from events — `_counts_from_events`, `pipeline.py:486-532`, test
  `test_verdict_is_derived_from_ndjson_events`); allowlist report wall + denylist tripwire
  (`report.py:22-29,49-96` — the coded denylist stays); effective-N pooling on BOTH axes
  (clusters, repeats pooled — `pipeline.py:493-499`, `schema.py:25-41`,
  measurement-principles P1 #5).
- **Power budget:** `arb-eval plan` derives T=19, 5 instances × 4 repeats, G_s=45 gold
  pairs/seat, 19 effective control clusters/class = full power (verified by running it).
- **In-band-leak pre-flight:** `_assert_no_answer_key_in_fixture` (`pipeline.py:535-574`)
  greps the full seat-readable surface (worktree incl. untracked + ALL git objects) for
  seed/control IDs; Parks on any hit.
- **Fixtures:** synthetic secrets-in-logs + correctness at nominal full power are BUILT but
  **relabeled pipeline-validation only** (effective clusters 11/13 < 19 — the P-3 panel
  resolution, `docs/eval-p3-fixture-corpus-design.md:143-167`). Real-base candidates are
  sourced and awaiting the human pick (`docs/eval-p3-real-base-candidates.md`).

**Therefore the honest pending increment is not "build pipeline.py".** It is the seven items
in §2–§6 below, each verified missing.

## 1. Constraints (binding)

1. **Hard walls (design errors if violated):** no ranking, no seat-drop/trust/quorum verdict,
   no composite seat score — unordered PASS/FAIL/UNKNOWN-by-class only; the coded denylist in
   `report.py` stays; output informs a HUMAN and nothing downstream consumes it to change
   trust automatically; the escaped-defect journal is untouched; §0 of eval-design v3 and the
   closed v2-panel confounds are not reopened.
2. **A scored floor seat must be a confinable EXTERNAL process** — bridge-unconfined and
   in-session subagents are disqualified (`docs/eval-seat-confinement.md:106-123`). This is a
   prior human decision, not this design's to revisit.
3. **Seats investigate a real checkout with tools, never diff text**
   (`docs/eval-instrument1-live-dispatch-scope.md` §Access patterns — measuring snippet-review
   would measure the wrong construct). The normalizer is deliberately the opposite (bare API,
   no tools).
4. **Fails loud, never fakes a verdict.** Dispatch failure/timeout → the repeat is recorded
   `dispatch_error` and marked **incomplete**; incomplete repeats are EXCLUDED from both
   caught and noise counts, and any class touched by an incomplete repeat renders
   **UNKNOWN with a named `infra_incomplete` line** — an infra flake is never scored as a
   capability miss and can never render FAIL (round-1 P1-α; the pre-v2 behavior at
   `pipeline.py:687-710` + worst-case pooling at `:494-524` did exactly that). Unparseable
   normalizer output → matcher-ambiguous with the error attached; unadjudicated gold →
   labeled; missing power → UNKNOWN, never a silently weakened bar (reject-C is settled:
   fix data/accounting, never the bar).
5. **Everything hermetically testable without live dispatch must be** (CI has no engines, no
   docker guarantees — the V6 hermeticity scar from CDX-1 applies: pin env explicitly).
6. **Dispatch cost is real:** design for one seat at a time being benchable (`--seat` exists,
   `cli.py:166-167`); v1 names a small representative seat set with a cost estimate (§7).

## 2. Increment A — pipeline completion (the small, load-bearing gaps)

### A1. ContainerDispatcher timeout + incomplete-repeat failure semantics (round-1 P1-α)
`confined-review.sh` runs `docker run --rm` with **no timeout** (verified: no timeout anywhere
in the script); `BridgeDispatcher` has one (`pipeline.py:67,133`). A wedged confined seat
hangs `run_floor` forever. Add `timeout` (env `ARB_EVAL_DISPATCH_TIMEOUT`, default 900s, same
as the bridge path) to `ContainerDispatcher.dispatch` via `subprocess.run(timeout=...)`.

**Failure semantics (REPLACES the built detection-miss route — the panel's strongest
finding):** `TimeoutExpired`/`DispatchError` → the repeat is recorded as a
`dispatch_error` NDJSON event with `kind: timeout|canary|review` (so a boundary-breach Park
is never mistaken for a slow seat) and the repeat is marked **incomplete**.
`_counts_from_events` excludes incomplete repeats from BOTH `caught` and `noise` counts —
they contribute nothing, in either direction. Any class whose evidence includes an
incomplete repeat renders **UNKNOWN** with a named `infra_incomplete(kind, repeat)` line in
the report; the run completes (other seats/classes unaffected) and the runbook says: fix the
infra, re-run the seat. A canary failure additionally Parks the run (unchanged — that is an
integrity breach, not a flake). Rationale: the pre-v2 route scored a timeout as `caught_k=0`
misses, and worst-case pooling (`pipeline.py:494-524`) then zeroed the seat/class — an infra
flake rendered a false FAIL, violating constraint 4. Verification: H1 pins the new shape and
its deny-proof (delete the exclusion → red).

### A2. Run provenance capture (feeds §6 change-event keying; round-1 P1-β/P1-γ)
Schema §6 requires per-run provenance; today `run_floor` records budget/claim-levels but not
versions. Add a `provenance` event (and `detail.json` block) at run start:
- **`model_*` — the P1-β fix (CLI version is NOT model identity):** the model is an
  **explicit input** to the confined run — the runner passes each engine's model flag
  (`codex exec --model <m>`; agy's equivalent) from the scenario/CLI, never relying on the
  engine's default. Provenance captures `model_requested` (the flag value), the **full
  confined command line**, and `model_reported` where the engine supports readback inside
  the jail; `model_source: pinned-flag|readback|cli-version-only` records which. A
  `cli-version-only` run (engine can't pin or report) renders a LOUD warning and §6
  `publish` labels the artifact `model_unverified: true`. Deny-proof (H2): changing the
  model input changes the provenance key; a config-only bump changes the captured command
  evidence.
- `engine_versions` (CLI versions, still captured — they are part of harness identity, just
  not model identity): emitted inside the jail on a **nonce-fenced marked line**
  (`ARB_PROV_<nonce)> ... </ARB_PROV_<nonce>` where the nonce is per-run) and stripped from
  the review reply — a seat reply that echoes the marker text cannot corrupt the parse
  because it cannot know the nonce (GLM P2-5; H2 deny-proofs a reply-embedded fake marker).
- **`harness_version` — the P1-γ fix:** `git -C tools/eval describe --always --dirty` PLUS a
  content hash (sha256) of the `arb_eval/` package files. A `-dirty` harness may run (dev
  iteration is legitimate) but §6 `publish` REFUSES a dirty-tree run — cross-version
  comparison keys must name committed code.
- `image_digest`: `docker image inspect arb-eval-seat:latest -f '{{.Id}}'` (confined runs);
- `corpus_version`: fixture builder script SHA + scenario-file sha256 + fixture repo
  base/head SHAs (deterministic since the P-3 fix);
- normalizer model + endpoint, matcher window, boundary-oracle + language coverage, gold-set
  version (or `GOLD_UNADJUDICATED`).
All fields pass `report.guard` before writing (they are names/hashes, not seat values).
Hermetic tests stub the subprocess calls.

### A3. Boundary oracle: ctags/tree-sitter, per schema §3
`_enclosing_function` (`pipeline.py:365-392`) is a flat-`def` Python regex walker. Adequate
for the synthetic single-file-per-function fixtures; **not** for real bases (methods in
classes, nested defs, decorators, async routes — exactly what a FastAPI RealWorld app is made
of). A wrong enclosing-function conflates loci (the exact bug the cross-boundary fix killed,
`pipeline.py:412-418`). Implement the schema's stated precedence: **tree-sitter where a
grammar exists, else ctags, else the current heuristic**, behind one
`enclosing_symbol(repo, file, line)` seam:
- optional imports, fail-closed to the next tier with the tier RECORDED per
  `matcher_decision` (new `boundary_oracle: tree-sitter|ctags|heuristic` field) — a silent
  tier downgrade would corrupt matcher-miss accounting invisibly;
- language coverage stated per fixture in the scenario (`subject.languages`), and `plan`
  warns when the available oracle tier for a fixture's language is `heuristic`;
- hermetic tests: fixture files with methods/nested/decorated defs, assert each tier's answer
  and the tier-fallback chain (tree-sitter/ctags absence simulated by injection, not by
  host-state — V6 scar).
Dependency posture: `tree_sitter` + `tree_sitter_python` as optional extras; `ctags` binary
probed at runtime. CI runs the heuristic tier tests unconditionally, the ctags/tree-sitter
tests behind availability skips that ASSERT the skip reason (no vacuous green).

### A4. agy output-format wrapper (named remaining item, `confinement/README.md:31-32`)
agy emits prose around the finding lines more often than codex; today mis-formatted lines
become segmenter candidates that normalize to `unknown` → matcher-ambiguous, which conflates
**format failure** with **detection failure** for exactly the seat most prone to it. Fix at
the segmentation layer, not the seat: `segment_reply` already strips bullets; add a
per-reply `format_conformance` NDJSON field (fraction of candidate lines matching the
`<class> | <file>:<line> | <desc>` shape) so a low-conformance reply is visible in detail
output — and the gold set (§4) is what quantifies the resulting matcher penalty per seat.
No re-prompt loop in v1 (a retry-on-format loop changes what is measured: one pass, as
scored). The detection/format split is thus REPORTED, not silently absorbed.

### A5. Scenario portability
`scenarios/floor-secrets-full.json` hardcodes `subject.repo` as an absolute path inside a
DIFFERENT clone's worktree (`/Users/<user>/AgentRedisBridge/.claude/worktrees/p3-corpus/...`) —
a committed scenario that only runs on one host state. Fix: `subject.repo` may be relative to
the scenario file's directory; builders emit relative paths; `run` resolves and validates
(repo exists, base/head SHAs resolvable) at load with a clean `ScenarioError`. Regenerate the
committed scenarios via their generators.

### A6. Where bridge mechanics still apply (and where they don't)
The scored path bypasses the bus by design (constraint 2): `ContainerDispatcher` is a local
subprocess, so `--run-id`/notify-inbox/`--worktree` do not apply to it — the NDJSON `run_id`
(`pipeline.py:612`) is the label of record, and the operator runbook (§6) says to quote it in
any ARB Memory artifact. The bridge path (`BridgeDispatcher`) remains for
integrity-UNGUARANTEED diagnostics only and already implements the SKILL.md rails (run-id
threading, timeout, env overrides). Nothing in this increment adds a new bridge surface.

## 3. Increment B — real-codebase fixture bases (the eval-p3 "next" step)

Human resolution being executed here: effective-N accounting is DONE; real bases are NEXT so
effective control counts rise toward nominal and the budget clears honestly
(`eval-p3-fixture-corpus-design.md:150-157`). Candidates already sourced for sign-off
(`docs/eval-p3-real-base-candidates.md`): primary rec `nsidnev/fastapi-realworld-example-app`
(MIT, FastAPI, ownership/JWT/pagination surfaces hosting all three classes in one base).

**Sourcing + seeding mechanics (clone-at-build, per the candidates doc's default):**
1. `fixtures/build_floor_real_<class>.sh` clones the pinned upstream SHA into
   `fixtures/repos/` (gitignored, like the synthetic repos), then **re-inits history to a
   single neutral base commit** (squash — pipeline hygiene per
   `eval-seat-confinement.md:59-60`; also removes upstream history as an answer-key-adjacent
   surface and keeps `git cat-file --batch-all-objects` cheap for the pre-flight).
2. Seeds are applied as one head commit from **tracked patch files**
   (`fixtures/src/real-<class>/*.patch` + a generator, mirroring the existing
   builder+generator pattern) — 5 seeds per class, each a **distinct mechanism** (the P-3
   tables: S1–S5, C1–C5, A1–A5), each tagged `cluster: <mechanism>`; deterministic SHAs via
   the existing pinned author/committer dates (`build_floor_secrets_full.sh:24`).
3. Controls are **harvested from existing clean upstream code** (locations recorded, code
   untouched), ≥19 per class where the base genuinely supplies them, each tagged with its
   why-clean `cluster`; the generator computes effective counts and the scenario description
   records nominal vs effective — `plan` already warns when effective < T. **The accounting
   is carried through unchanged; if a real base yields only 15 effective clusters, the run
   reports small-n honestly rather than padding with correlated loci** (reject-C stands).
   **Cluster adjudication (GLM P2-3 + cold-Opus P2):** harvested-control cluster tags get
   the same span-judgment the P-3 panel applied to synthetic controls
   (`eval-p3-fixture-corpus-design.md:148-153`) — the human adjudicates distinctness at
   corpus sign-off, each cluster carries a recorded why-clean rationale, and a generator
   lint enforces anti-oversplit: effective cluster count may never exceed the count a named
   rationale supports (a cluster with no distinct rationale merges into its neighbor). The
   failure mode this kills: over-granular tags silently re-inflating effective-N back to
   nominal.
4. Every seed and control in its **own enclosing function** where feasible; where a real-app
   control shares a function with unrelated code, the A3 boundary oracle is the guard —
   record any seed/control pair closer than the matcher window as a scenario-lint error
   (extend the load-time D3-style checks in `schema.py:177-206`).
5. Marker-freedom: seed/control IDs live ONLY in the scenario (outside the repo); the
   existing pre-flight (`pipeline.py:535-574`) + canary enforce it per run. Patch files must
   not embed IDs in code comments, **and the generator lint also greps commit messages, tag
   names, and branch names** (GLM P2-4 — `git cat-file --batch-all-objects` dumps commit
   messages, so an ID there Parks the run; the lint catches it at build time instead).
6. Class order: **secrets-in-logs + correctness first** (proven-authorable), then
   **authorization-scoping on the same base** — the class the real base exists for (the
   ~19-distinct-controls strain past ~10 in minimal fixtures,
   `eval-p3-fixture-corpus-design.md:84-99`). If the chosen base cannot yield ≥5 distinct
   authz seed mechanisms + enough distinct controls, that is a finding to surface, not to
   pad.
7. License hygiene: pinned-SHA clone at build time, attribution note in the builder header,
   nothing vendored into git (the human's clone-vs-vendor question resolves to
   clone-at-build unless they say otherwise — flagged in §8).

Confinement fit: the fixture mounts read-only into the existing jail unchanged; the image
already contains git + node + engines. A real app is bigger, so the canary's grep surface
grows — bounded, still seconds.

## 4. Increment C — the gold matcher-validation set (~135 pairs, concrete plan)

Purpose (schema §3): per-seat matcher recall/precision with a per-seat error band; a seat
whose matcher recall lower-CI < `matcher_gate` (0.85) has ITS floor rows suppressed. Until
adjudicated, every report carries `GOLD_UNADJUDICATED` (`pipeline.py:26,648,792`) — currently
a permanent placeholder with **no gold module at all**. Build:

**Sizing.** G_s = 45/seat (derived, verified via `plan`). v1 seats codex + agy → 90 pairs;
the containerized-claude seat (§7 expansion) brings it to ~135. Pairs are drawn **per seat,
stratified** (P0-C / P1 instance 4): a global set would average away seat-correlated
normalizer bias.

**Sourcing.** Pairs are real pipeline traversals, not authored prose: sample raw findings
from pilot-run NDJSON (`finding_emitted`/`segmented`/`normalized`/`matcher_decision` events
give the full raw→normalize→match trace per finding). Stratify each seat's 45 across:
detected / detection-miss / matcher-ambiguous outcomes, all three match bases
(symbol/function/window), both classes, and no-op ("no issues") replies — the strata where
matcher error hides. Pilot runs on the relabeled synthetic fixtures are admissible sources
(pipeline-validation is exactly their job); real-base pilot findings are added as they exist.

**Storage.** `tools/eval/gold/<seat>/pairs.ndjson`, append-only, versioned
(`gold_version` = sha256 of the file, recorded in run provenance):
`{pair_id, seat, raw_finding, context: {repo, base, head, scenario_sha}, pipeline: {normalized,
match_outcome, basis}, adjudications: [{rater, verdict, ts}], final_verdict, notes}`.
The `pipeline` block is the SYSTEM's answer; adjudication packets exported for raters OMIT it
(blindness is structural: `arb-eval gold export` emits packets without the pipeline verdict;
a test asserts the exported packet contains no outcome fields). **Packets inline the cited
code snippet ± context lines** (round-1 P1-δ / GLM P2-1): a bare-API rater cannot `git diff`
a repo path, so the packet must carry the ground-truth code itself — inlining code does not
break blindness (code is evidence, not the pipeline's verdict).

**Adjudication protocol (P1-H, made concrete; round-1 P1-δ adds automation).**
- ≥2 raters, blind, **disjoint from panel/quorum and not Claude-family** (quorum = codex,
  cold-Opus, agy, pi-GLM) and **not the normalizer** (M3 adjudicating its own normalization
  is self-validation). Proposed pool: **the human (Mark) + model-alias-1 (tencent, pi bare-API)** as
  the two raters, **grok-acp as the third/tie-break**. Named risk: model-alias-1/cursor-class raters
  have fabrication records (memory: non-certifying) — mitigated because adjudication is
  verifiable-by-inspection (each verdict cites the fixture line) and every machine-rater
  disagreement routes to the human. This rater pool is a **human fork** (§8), not settled
  here.
- **`arb-eval gold rate --rater <model-alias-1|grok> --packets <file>` (new verb, the automation the
  panel required):** runs a machine rater over exported packets — one bare-API call per
  packet with a fixed rubric prompt, parses the structured verdict line, retries once on
  parse failure then records `rater_error` (never a silent skip), and emits the rater-file
  `gold ingest` consumes. Manual adjudication (Mark) uses the same packet/rater-file format.
  Without this verb the 90-pair protocol was hand-carried and practically unexecutable
  (agy F2).
- `arb-eval gold ingest <rater-file>` merges verdicts; `arb-eval gold score` computes
  per-seat matcher precision/recall (Wilson, existing `stats.py`) + **inter-rater agreement
  as raw % + pairwise Cohen's κ between the two primary raters** (pure-stdlib addition to
  `stats.py`; κ is defined for exactly two raters — the tie-break rater participates in
  verdicts, not in κ; agy P2), and writes `gold/<seat>/summary.json`.
- **Gate wiring:** `run_floor` loads `gold/<seat>/summary.json` when present; recall
  lower-CI ≥ gate → that seat's rows render with the per-seat error band in detail;
  lower-CI < gate → that seat's floor rows are SUPPRESSED (per-seat, not global) with a
  named line in the report; absent → today's `GOLD_UNADJUDICATED` warning, verdicts labeled
  not-trusted. All three states hermetically tested; delete-the-gate must go red
  (deny-proofs-need-adversarial-verification).

**Labor estimate (stated so the human can budget):** 90 pairs × ~1–2 min/pair/rater ≈ 1.5–3 h
per rater for the v1 set; incremental thereafter (new pairs only on seat/harness change).

## 5. Increment D — matcher-miss vs detection-miss separation (closing the loop)

The outcome vocabulary exists (`detected` / `detection-miss` / `matcher-ambiguous`,
`pipeline.py:395-421`), and A4 adds format-conformance visibility. What the gold set (§4)
adds is the **quantitative** separation: per-seat matcher recall says how much of a seat's
miss column is plausibly the matcher's fault. Report change (detail file only, never
headline): per seat, alongside `caught_k/caught_n`, emit `matcher_ambiguous_n` and the
gold-derived matcher band; the headline grid stays PASS/FAIL/UNKNOWN, untouched. No new
verdict category (a MATCHER-SUSPECT verdict would be a new convertible surface).

## 6. Increment E — change-event packaging (how a human runs this, v1 = manual CLI)

**Trigger events:** a seat's engine/model version changes (codex/agy CLI upgrade, image
rebuild), a new seat is added, the harness changes (arb_eval code), or the corpus changes
(new base/scenario version). No scheduler, no automation — a human decides to bench.

**Runbook (one seat at a time, per the cost constraint):**
1. `confinement/build.sh` (rebuild image; canary smoke runs inside it);
2. `fixtures/build_floor_real_*.sh` (deterministic repo rebuild);
3. `python3 -m arb_eval.cli run --scenario scenarios/<s>.json --confined --seat <seat>
   --normalizer anthropic:MiniMax-M3` — per class-scenario;
4. inspect `floor/runs/<run-id>/report.txt` (+ detail.json);
5. `python3 -m arb_eval.cli publish --run <run-id>` (new verb): assembles the **change-event
   artifact** and prints it for storage.

**Artifact (report-only, wall-guarded):** JSON keyed exactly as the brief requires —
`{seat, model_version, harness_version, corpus_version}` (all from the A2 provenance event,
so the keys are captured facts, not hand-typed claims — `model_unverified: true` when A2's
`model_source` is `cli-version-only`) plus `run_id`, the PASS/FAIL/UNKNOWN grid, claim
levels, small-n/orphan lines, `events.ndjson` sha256, and the FULL disclaimer text
(`report.py:31-38`). **The gold field is gate-outcome ONLY (round-1 P1-ε):**
`{gold_adjudicated: bool, gold_version, suppressed_seats: [...]}` — per-seat matcher
recall/precision are explicitly EXCLUDED from the artifact and live in `detail.json` only;
the name-denylist in `report.guard` structurally cannot catch a `matcher_recall`-shaped key
(its own docstring concedes synonyms, `report.py:6-9`), so the exclusion is spec + deny-proof,
not denylist. `publish` REFUSES a `-dirty` `harness_version` (round-1 P1-γ: comparison keys
name committed code) and runs `report.guard` over the whole artifact plus
`assert_verdict_row` **scoped to the grid rows only** (GLM P2-2 — it would false-raise on
`claim_levels`) before emitting — a fabricated ranking field is a `WallBreach`, and H6 also
injects a `matcher_recall`-shaped field and asserts `WallBreach`.

**Storage: ARB Memory artefact.** The orchestrator (or human) stores the printed artifact via
the deployed `memory_store` write path, tagged
`instr1-floor / seat:<seat> / model:<ver> / harness:<sha> / corpus:<ver> / run:<run-id>`, so
`memory_search` answers "what was codex's floor at model X / corpus Y". `publish` itself does
NOT write to ARB Memory in v1 (the CLI stays dependency-free and the write stays a deliberate
human/orchestrator act — consistent with "output informs a human"); it emits the exact
payload to store. NDJSON + report files remain the authoritative local evidence; the memory
artefact is a pointer-carrying summary, never the measurement source.

**Consumption wall restated:** nothing may read these artefacts to change trust/quorum/seat
assignment automatically. They exist so a HUMAN, at a change event, can compare grids across
versions with the residual-convertibility caveat in view.

## 7. Seat set + cost/wall-clock estimate (v1)

**Representative seat set (v1): codex + agy, confined** — the two seats already verified in
the jail (`confinement/README.md:26-32`); the fleet's ~23 bridge seats are NOT floor
candidates as a population (most are role-profile duplicates of the same engines, and only
confinable external processes qualify). **Named expansion, in order:** containerized
Claude Code CLI seat (the sanctioned way to floor-score a Claude-family reviewer —
`eval-seat-confinement.md:117-123`), then pi-harness engines if/when added to the image.
Each expansion is +45 gold pairs (§4) and an image change (a §6 change event).

**Per full run (one seat, three classes, defaults):**
- dispatches: 3 scenarios × 4 repeats = **12 confined review passes** (one pass covers all
  loci of a scenario — §0); one pass = canary + engine review of a small-app checkout,
  estimated 3–10 min (assumption to validate on the first real-base run; A1's 900s timeout
  bounds the tail);
- normalizer: ≤ ~40 M3 calls per scenario (findings × repeats), 512-token cap, temp 0 —
  negligible cost (cents);
- **wall-clock ≈ 0.6–2 h per seat, sequential** — one seat at a time is the design point;
  the two-seat v1 full run ≈ 1.5–4 h. Repeats add conservatism, not Wilson n (pooling), so
  there is no temptation to cut repeats to save cost without also losing the worst-case
  pooling they feed.

## 8. Open forks for the human (surfaced, not resolved here)

1. **Real base pick** — approve `nsidnev/fastapi-realworld-example-app` (primary rec) or an
   alternative; confirm clone-at-build (this design's default) vs vendoring.
2. **Gold rater pool** — Mark + model-alias-1, grok tie-break (proposed §4), or a different disjoint
   pool; and whether ~1.5–3 h of human adjudication for v1 is acceptable now or staged.
3. **Order:** first real-base scored run before vs after gold adjudication. Default proposed:
   run first with `GOLD_UNADJUDICATED` labeling (honest, and it SOURCES the gold pairs), then
   adjudicate, then re-render — the pairs must come from real traversals anyway.
4. **Claude-container seat timing** (expansion, not v1 critical path).

## 9. Rejected alternatives

- **Unconfined bridge dispatch as the scored path** — rejected by prior decision and by code
  (`assert_read_confined` Parks; a settable "confined" env var was explicitly refused as
  spoofable, `pipeline.py:87-91`). This design does not weaken it.
- **Bare-API reviewer seats (AnthropicDispatcher-as-reviewer)** — rejected on construct
  validity: a harness-less model can't investigate a checkout, so it would be the crippled
  seat measured as the seat (`eval-instrument1-live-dispatch-scope.md` Piece B).
- **In-session cold-Opus as a scored seat** — structurally unconfinable
  (`eval-seat-confinement.md:111-115`); stays meta-reviewer.
- **Lowering the power target to fit ~12–14 effective controls** — reject-C, settled: fix
  the data (real bases) and the accounting (effective-N), never the bar.
- **Re-prompt/retry loop for format-nonconforming seats (A4)** — changes the measured
  construct (one pass, as scored); format cost is reported instead.
- **M3 as a gold rater** — self-validation of the pipeline stage it implements.
- **`publish` auto-writing to ARB Memory** — kept a human/orchestrator act; the CLI emitting
  directly would put a network write inside the measurement tool and blur "informs a human".
- **A MATCHER-SUSPECT headline verdict** — new convertible surface; detail-file band instead.

## 10. Verification obligations

Hermetic (CI, no engines/docker — env pinned per the V6 scar):
- **H1 (A1):** fake-docker `confined-review.sh` stub that sleeps → `DispatchError(kind=timeout)`
  recorded, repeat marked incomplete, **affected class renders UNKNOWN with the
  `infra_incomplete` line and contributes ZERO miss/noise counts**; canary-fail stub →
  `kind=canary`, distinct, Parks. **Deny-proof: delete the incomplete-repeat exclusion →
  the UNKNOWN test goes red** (the pre-v2 fake-FAIL behavior must be impossible to
  silently restore).
- **H2 (A2):** provenance event present with all keys; subprocess calls stubbed; passes
  `report.guard`. **Model-key deny-proof: changing the model input changes the provenance
  key; a reply embedding a fake nonce-marker line does not corrupt `engine_versions`;
  `model_source: cli-version-only` renders the loud warning.**
- **H3 (A3):** boundary-oracle tier tests (methods/nested/decorated defs); forced-absence
  injection exercises each fallback tier; tier recorded in `matcher_decision`; availability
  skips assert their reason.
- **H4 (A5):** relative-path scenario resolves; missing repo/SHA → clean `ScenarioError`.
- **H5 (§4):** gold export packets contain NO pipeline-outcome fields (blindness structural)
  AND inline the cited code snippet; `gold rate` stub-rater path parses verdicts, retries
  once, records `rater_error` on double failure; ingest+score computes recall/precision +
  pairwise κ; **gate suppression**: seat below gate → its rows suppressed, other seats
  intact; gate absent → `GOLD_UNADJUDICATED` warning; **delete the gate wiring → the
  suppression test goes red** (adversarial deny-proof).
- **H6 (§6):** `publish` artifact carries all four keys + disclaimer verbatim; injected
  `rank`-shaped field → `WallBreach`; **injected `matcher_recall`-shaped field →
  `WallBreach` (the P1-ε deny-proof — the denylist alone cannot catch it)**; gold field is
  gate-outcome-only shape; dirty `harness_version` → refusal; artifact grid re-validates via
  `assert_verdict_row` scoped to grid rows.
- **H7 (B):** real-base generator lint — seed/control ID appears in a patch → build fails;
  seed/control within matcher window in one function → `ScenarioError`.

Live gates (required — live-verification-catches-cli-glue; the untested CLI/subprocess glue
is where bugs survive static review):
- **L1:** first real-base confined scored run, one seat, canary green per dispatch, answer-key
  pre-flight green, report labeled honestly (small-n lines where effective < T).
- **L2 (negative control):** plant a seed ID into the fixture worktree → pre-flight Parks;
  plant into the image/jail surface → canary Parks. Both must go red or the guards are
  hollow.
- **L3:** gold pilot — export ≥10 real pairs, two raters adjudicate blind, κ computed,
  per-seat recall renders into a report with the band (or suppression) visibly applied.
- **L4 (change-event drill):** bump one provenance input (image rebuild), rerun one seat on
  one scenario, `publish`, store via `memory_store`, retrieve via `memory_search` by the
  seat+corpus key — the artefact round-trips and matches the local report.

## 11. Non-goals

Decorrelation/marginal-contribution/seat-drop instruments (ill-posed internally, v3 §0);
implementer/adjunct generation-quality oracle; the escaped-defect journal (a journal, not a
harness); automated/scheduled benching or CI-run live floors; per-engine trust changes of any
kind; arb-watch/visibility rendering of floor runs; the drift track (v3 §8); expanding beyond
three classes in this increment; README rewrite beyond the staleness fixes implied here
(update `tools/eval/README.md` status section, `confinement/README.md`'s stale REMAINING
lines, and the `ContainerDispatcher` docstring's stale "agy needs adding" as part of the
build, so doc and code stop drifting — the §0 finding + codex P2 + GLM P2-6, folded in as a
one-line obligation; `docs/eval-seat-confinement.md:7-8` is the current source for
agy-confined).

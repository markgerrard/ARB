# Decision log — eval dispatch pipeline (autonomous run, 2026-06-16)

Run-scoped log (per measurement decision J: run-scoped path, not shared `decisions.md`). Branch:
`feat/eval-dispatch-pipeline`, base ref `0ec755c`. Disposition policy (human, Stage 0): **branch-only,
never merge to main even at 3/3** — this is the first run where the skill is the sole authority between
work and main, and the artifact is the measurement harness (its errors bias everything downstream
silently), so it gets a human eye before it's load-bearing.

## Stage 0 — spec + sign-off
- **Decision:** implement the Instrument 1 dispatch pipeline (`run` path) per schema §3–§6. Approved by the
  human as the one mandatory touch. Disposition: branch-only.
- **Posture-baseline gate (affirmative enumeration):** auth/exposes/who-can-do-what — none new (reuses
  bridge sender policy). input-trust — parses model replies, **parse-only, never executes**. logs/persists
  — local NDJSON + reports, fixtures only (no real secrets). egress — dispatch to existing bridge seats.
- **Hard acceptance criterion (human flag):** the *running pipeline* must relabel results **instance-level
  when I_min isn't met** — the minimal fixture is exactly the input that triggers the P0-A bug's runtime
  reappearance; a schema-level fix does not guarantee a pipeline-level one. Required test + explicit verify.

## Stage 1 — design decisions

### D1 — Dispatcher interface (auto-eligible)
- **Chosen:** a `Dispatcher` protocol with (a) a real bridge-backed impl shelling to `scripts/agent-dispatch`
  and (b) a `MockDispatcher` for deterministic tests. **Over** direct inline `subprocess` calls (untestable
  without live seats) — because deterministic tests are mandatory and live dispatch from inside a build is
  fragile (nested bridge dispatch). Reversible. No posture.

### D2 — Function-boundary oracle for the matcher (auto-eligible)
- **Chosen:** stdlib regex/indentation heuristic for enclosing-function detection in v0. **Over** tree-sitter
  (adds a dependency; the package is deliberately stdlib-only so it runs anywhere the bridge runs) and ctags
  (external binary). tree-sitter logged as a future option, not adopted. Reversible; not posture.

## Parks (posture-class or human-labor — NOT resolved unattended)

### P-1 — off-quorum normalizer seat  [posture-class → park]  ✅ RESOLVED 2026-06-16
- **RESOLUTION:** resolved *not* as a bridge seat but as a **direct-API normalizer**. The off-quorum
  requirement is "a non-quorum model with temperature control," and the pi harness cannot forward
  temperature (no CLI flag, no `set_temperature` RPC, no `models.json` field — verified). The Agent SDK is
  the same dead end. So the normalizer is an `AnthropicNormalizer` (pipeline.py) calling **MiniMax-M3** on
  its Anthropic-compatible endpoint (`https://api.minimax.io/anthropic`) at **temperature=0,
  `thinking:{"type":"disabled"}`** — both first-class request params M3's endpoint accepts (M2.x cannot
  disable thinking; M3 can — so the model choice was load-bearing). Off-quorum by construction (M3 ∉
  codex/agy/cold-Opus), with a cheap quorum-model collision guard as defense-in-depth.
  **Deciding test passed** (6× same finding, temp=0, thinking-off): class+location identical across all
  runs — the *only* fields the matcher reads (verified: `match_finding` uses class+location only;
  confidence/statement are descriptive NDJSON fields, never in a match/viability decision) — so M3's
  residual MoE variance (confined to confidence ±0.04 and wording) is **provably inert**. Output is 5/6
  bare JSON, 1/6 fenced → the normalizer does robust fence-tolerant extraction and **fails loud into
  `unknown`→matcher-ambiguous on unparseable output (never silent-drops)**. Use: `arb-eval run --scenario X
  --normalizer anthropic:MiniMax-M3`.
- **Original park record (kept for provenance):**
- **Decision deferred:** which seat runs the off-quorum normalizer. **No valid choice exists on this host**
  — only codex+agy are up, both quorum seats; routing the normalizer to a quorum seat defeats the off-quorum
  independence the design requires (a quorum-seat normalizer shares blind spots with the seats it normalizes).
- **hold-state:** none for the *seat choice* (greenfield — no shipped off-quorum routing). The pipeline is
  built with the normalizer seat as **config**, defaulting to an explicit `UNRESOLVED_OFFQUORUM` sentinel
  that the runner refuses to proceed past for a real run (parks rather than silently using a quorum seat).
- **dependents:** the live-run normalize stage (not the code structure — that builds against the config seam).
- **base ref:** `0ec755c`. **Resolve:** stand up a non-quorum adjunct seat (pi-rpc kimi/minimax or gemini),
  then set the normalizer seat config and run.

### P-2 — gold matcher-validation set adjudication  [human disjoint raters → park]
- **Decision deferred:** the gold set's "correct" labels require **blind, multi-rater adjudication by raters
  disjoint from the panel/quorum** (schema §3, P1-H). An unattended run cannot supply disjoint human raters.
- **hold-state:** none (no gold set exists yet). Pipeline builds the **gate mechanism** + a tiny clearly-marked
  placeholder; the matcher-gate runs but is flagged `GOLD_UNADJUDICATED` so no floor verdict is trusted.
- **dependents:** trusting any floor PASS/FAIL (the matcher error-band hangs on the gold set).
- **base ref:** `0ec755c`. **Resolve:** human-adjudicate ~135 pairs (45/seat) blind, off-quorum.

### P-3 — full fixture corpus (≥ I_min seeds/class)  [human labor → park]
- **Decision deferred:** a real fixture with ≥5 distinct instances/class. The run builds a **minimal proving
  fixture** (enough to exercise the pipeline end-to-end), which necessarily yields **instance-level** results.
- **hold-state:** the minimal fixture is the hold-state for *pipeline validation*; class-level claims block.
- **dependents:** any class-level PASS/FAIL claim.
- **base ref:** `0ec755c`. **Resolve:** author ≥5 seeds/class per target class.

## Stage 3-5 — gate + disposition (autonomous)
Execution-primary panel (codex+agy+cold-Opus, all RAN): codex REJECT, agy FIX_BEFORE_MERGE, cold-Opus
SHIP_WITH_NITS → REQUEST CHANGES. 6 findings (F1 NDJSON-not-authoritative [core], F2 wall dead-code, F3
matcher loose-boundary, F4 segmentation drops, F5 off-quorum seat unvalidated, F6 class_level_ok counts
rows). Remediation round 1 (commit a53e3c6) fixed all 6; orchestrator independently re-ran the panel probes
(F1/F5/F6/F2 directly verified; F3/F4 via required tests); 38 tests green. **Disposition: branch-only —
NOT merged (human Stage-0 decision).** Final diff orchestrator-verified, not re-paneled (logged in the
morning digest as an honest disclosure). Loop: 1 remediation round (well within 3/5).

## Stage 6 — morning review (human, pending)
See docs/MORNING-DIGEST-eval-dispatch.md. Parks P-1/P-2/P-3 open for resolution. Hard criterion
(instance-level relabeling) verified at runtime. main untouched.

## Standing constraints (permanent properties of the floor suite — not one-offs)

### SC-1 — cold-opus-inclusive floor runs must be orchestrator-driven, never standalone CLI
A *reviewing* seat must investigate with its harness+tools against a real checkout (measurement
correctness — see SC-2). cold-opus is an in-process `Agent` subagent; only a Claude Code session can spawn
subagents. Therefore: **codex+agy floor runs MAY be standalone `arb-eval` CLI; any floor run that includes
cold-opus MUST be launched from within an orchestrating Claude Code session.** Failure mode if unwritten: a
future session runs a cold-opus-inclusive floor from a bare CLI, silently gets no cold-opus (or a crippled
bare-API one), and can't tell why. Costs nothing here (we always run under an orchestrator, no CI) — but it's
the kind of constraint obvious now and invisible later.

### SC-2 — reviewers investigate (harness+tools+real checkout); the normalizer is bare-API-no-tools
Role-correct, not inconsistent. Reviewers (codex/agy/cold-opus) get a real checkout to Read/Grep/Bash and
verify against ground truth (DC-001: the catch was a config check, not diff-reasoning). The normalizer (M3)
gets bare API with no tools — it *transforms* a finding, doesn't review; tools would contaminate it with its
own opinion of the code. A model reachable *only* by bare API cannot be a faithful reviewer seat.

## D3 — control-locus design (#3, settled before the first verdict-path run)
A **control locus** measures the seat's false-positive rate on clean code → its noise floor ν_s (the viability
verdict is "does the seat flag seed locations provably above the rate it flags clean locations").

- **Definition:** a control locus = **a plausible-but-clean instance of the target defect class** — a location
  where the class *could* appear and a reviewer *might* flag it, but where there is genuinely no defect (a flag
  there is a false positive). E.g. a `secrets-in-logs` control locus is a logging call that logs a **request
  ID, not a token**. This makes ν_s measure "how often does this seat cry wolf on defect-*adjacent* clean
  code" — the FP rate that matters.
- **REJECTED (degenerate):** clean lines unrelated to the class (blank line, comment, import). Those make ν_s
  artificially ≈0 → signal-to-noise looks huge → a trigger-happy seat looks precise → every seed-catch looks
  viable. The noise floor would measure the wrong thing.
- **Distinct in the F6 sense:** N near-identical clean logging calls are NOT N independent control loci (same
  trap as duplicate seeds). They must span how clean instances of the class actually appear, or ν_s is
  estimated from correlated samples and its CI is a lie.
- **Power:** control_loci ≥ T (the budget's `control_loci_required`), or ν_s can't be estimated tightly enough.
- **First-run expectation (label it so it isn't misread):** one seed + a few control loci almost certainly
  lacks the power to clear above-noise → the verdict will be **UNKNOWN, and that is a PASS** —
  UNKNOWN-because-honestly-underpowered-with-the-noise-machinery-working (ν_s computed, signal compared,
  honestly reported "insufficient data"). That is categorically different from UNKNOWN-by-construction (no
  noise machinery). The first run's success criterion is "a *legitimate* verdict came out," and on one seed
  the legitimate verdict is UNKNOWN. The run output must SAY this, so a future reader doesn't read the UNKNOWN
  as broken.

## D3 gate — execution-primary panel of the rework (✅ closed)
The D3 rework touches the verdict path, so it went through an execution-primary panel (run-it-don't-read-it)
before the first live run. Core confirmed sound by all three seats *by execution*: dual-match keying correct,
NDJSON authoritative under the rework (forge a control event → verdict follows the log; drop seed events →
collapses), noise moves the verdict (clean seat PASSes where a cry-wolf-on-controls seat goes UNKNOWN),
empty-controls → conservative UNKNOWN (never a false PASS), dispatch-error routing, instance-level/wall/
gold-flag preserved. Two real accounting defects found (codex FIX_BEFORE_MERGE; agy + cold-opus
SHIP_WITH_NITS) and remediated TDD (`78f3c59`):
- **Matcher double-count** — the seed loop and control loop scanned all findings independently, so one finding
  co-located with both a seed and a nearby control counted as BOTH a detection and noise, and a finding near
  several controls over-counted noise. Fix: a finding is classified ONCE — seed detection takes precedence
  over a control flag, and a finding flags at most one control.
- **Control-on-seed** — schema accepted a control locus at a seed's exact (class, file, symbol|line). A control
  must be a clean instance; fix: `schema.from_dict` rejects it.
- **Orphan control class** — a control locus in a class with no seed was logged then silently dropped from
  scoring. Fix: the report surfaces an ORPHAN line.

## First live run — chain proven end-to-end (✅ `floor-secrets-001`)
Fixture: a standalone git repo (`fixtures/build_floor_secrets_001.sh`) with ONE legible `secrets-in-logs` seed
(a bearer token logged in plaintext on login, `auth.py:10`) + ONE plausible-but-clean control locus (a
non-secret rotating session id logged on logout, `auth.py:16` — a flag there is a false positive). Panel
`[codex, agy]`, normalizer `anthropic:MiniMax-M3` (off-quorum, temp 0), `--repeats 1`.

Result — the full path ran against real models (`review (tool-equipped checkout investigation) → segment →
M3 normalize → dual-match → viability`) and the substance was correct:
- **codex**: seed detected (symbol match), control clean (not flagged).
- **agy**: seed detected (enclosing-function match), control clean (not flagged).
- **Verdict: legitimate UNKNOWN** — 1 seed is INSTANCE-LEVEL (< I_min=5) and controls are underpowered
  (1 < required 19), so the suite emitted NO class-level PASS/FAIL. The honest "ran end-to-end, underpowered"
  result, not a manufactured number — exactly the first-run success criterion above.

Both seats caught the legible defect and neither false-positived the clean control; M3 produced deterministic
class+location (the matcher's only inputs) while its MoE-variable fields (severity, confidence) — which the
matcher ignores — varied harmlessly. The increment is proven; the branch (`feat/m3-normalizer`) is staged for
review/merge. Open parks: P-2 (gold adjudication), P-3 (full power-budget fixture corpus).

# Defect-detection skill (held-axis hunts) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans.

**Goal:** Promote H1 config-drift from corpus *reference* into an operative procedure a code-review panel
runs on the diff, and build H2 unviolated-environmental-assumption as a schema-enforcement gate. H2 is not
operative doctrine until a review-brief composer or phase-input authoring convention emits `h2_section` on
every review-phase run.

**Design of record:** `docs/superpowers/specs/2026-06-21-defect-detection-skill-promotion-design.md` (v2).

## Global constraints (carry into every task)
- **Home = the review/DIFF path**, NOT `skills/diagnose` (failure-triggered; held-axis bugs PASS the suite).
- **The eval is a purpose-built deterministic check-runner**, NOT `tools/eval/arb_eval` (statistical, closed
  taxonomy). It must NOT share machinery with the hunts (circularity).
- **HARD GATE A — harness deny-proof (Mark):** the eval can't be the primary gate until the harness is proven
  to **fail** on (a) a no-op detector (flags nothing), (b) a catch-all detector (flags everything), (c) a
  label-matcher (passes only by reading names the sealing removed). If a broken detector passes the harness,
  the harness is the hollow thing.
- **HARD GATE B — suspect-resolution (Mark):** every manifest suspect closes as a **verified clean negative
  OR a logged finding** before merge — never an assumption (esp. `BRIDGE_MAX_PARALLEL`).
- **Positives = independent CLUSTERS** (boot-race + redis share `5e46276` → ~3), class-seed not certification.
- **Sealed + clean blind:** scenarios are a clean-worktree checkout of the pre-fix SHA, names/filenames that
  leak the class stripped.

---

### Task 1: Hunt interface + structured output schema
**Files:** Create `skills/defect_hunts/__init__.py`, `skills/defect_hunts/types.py`; Test `tests/defect_hunts/test_types.py`.
- Produces: `Scenario` (a sealed diff + file contents, no class labels); `Verdict` = list of
  `Finding{subject, kind: "H1"|"H2", decision: "FLAG"|"CLEAR", evidence: str}`. A hunt is
  `Callable[[Scenario], Verdict]`. **The eval recognizes a flag by `decision=="FLAG"`** (M3: pin the format so
  the eval can't false-negative a correct-but-unrecognized flag).
- TDD: round-trip a Verdict; a Finding with a missing required field is rejected.

### Task 2: H1 — AST kernel (config-drift), with its blind spot NAMED
**Files:** Create `skills/defect_hunts/h1_config_drift.py`; Test `tests/defect_hunts/test_h1.py`.
- Procedure: over the constants the **diff changes** to env-derived (`os.environ.get(NAME, default)` bound to a
  module global), find **sibling readers of the same env NAME** and assert co-movement; **FLAG** a literal/
  hardcoded sibling left behind (the `audit.PREFIX=""` shape). AST-based, not regex.
- **NAMED BLIND SPOT (Mark — H1 must name its own held axis):** H1 detects **direct literal readers**
  (`os.environ[...]`/`.get(...)`). It **may miss indirected readers** (a wrapper, a config object, a re-export,
  a runtime-built key). The audit-prefix bug was catchable *because both sites read the env var the same literal
  way.* H1 ships with this limitation **documented in the module + the brief output** ("H1 covers direct env
  readers; indirected readers are a known coverage edge") so a future indirected miss is a logged
  known-limitation finding, not a surprise. A detector silent about what it can't see is the defect this slice
  exists to prevent.
- TDD: FLAG on `audit.PREFIX=""` while `bus` is env-derived (the reconstructed positive); CLEAR on
  `ARB_MEMORY_PREFIX` co-moving; **inject-revert: a hunt that always FLAGs or always CLEARs fails its tests.**

### Task 3: H2 — gate-enforced brief schema (unviolated environmental assumption)
**Files:** Create `skills/defect_hunts/h2_assumptions.py`; Test `tests/defect_hunts/test_h2.py`.
- H2 has no pure static kernel → a **forced-answer schema**: for each environmental assumption the diff relies
  on (a dep importable, a process up, a real workdir, single-process, on-default config), the brief MUST emit
  `{assumption, violating_run, evidence}` **or** `{decision: "FLAG", assumption, evidence}`. **Missing fields
  fail the gate** (anti-doneness-laundering: a present brief section is structurally invalid unless the hunt
  was done). No producer currently emits `h2_section`; absence reports dormant instead of enforcement.
- TDD: a brief missing a `violating_run` for a load-bearing assumption fails; a complete brief passes.

### Task 4: Sealed + clean scenario builder
**Files:** Create `skills/defect_hunts/scenarios.py`; Test `tests/defect_hunts/test_scenarios.py`.
- Produces: `seal(pre_fix_sha) -> Scenario` — checks out the pre-fix tree in a **clean** temp worktree, builds
  the diff/context, and **strips class names + tell-tale filenames** so the hunt can't label-match.
- TDD: a sealed scenario contains the pre-fix code but NOT the strings "held-axis"/"audit-prefix"/etc.

### Task 5: Positives — reconstruct from pre-fix git (independent clusters)
**Files:** Create `tests/defect_hunts/eval/positives.json`; Test `tests/defect_hunts/test_positives.py`.
- The ~3 independent clusters with their pre-fix SHAs + which hunt must FLAG: audit-prefix (pre-`113fc89`, H1);
  seat-import (pre-`ac502ae`, H2); the `5e46276` cluster (boot-race + redis-per-seat, H2) counted **once**.
- TDD: each positive scenario seals cleanly and is labelled with its expected FLAG.

### Task 6: Negative MANIFEST — clean negatives + BAITED traps + suspect closeout
**Files:** Create `tests/defect_hunts/eval/negatives.json`, `docs/defect-classes/eval-manifest.md`; Test `tests/defect_hunts/test_negatives.py`.
- **Clean negatives (must CLEAR):** `ARB_MEMORY_PREFIX` (bus+audit co-move), `BRIDGE_NOTIFY_INBOX`.
- **BAITED traps (Mark — specify what each BAITS; traps prove precision against engineered cases, not just
  easy ones):** at least 2 —
  - **H1 alias trap (must CLEAR):** a constant whose sibling reads it through an **alias / re-export** so the
    surface diff *looks like* a missed reader but the readers genuinely co-move. (Baits "two literal sites or
    flag.")
  - **H1 divergence trap (must FLAG):** `BRIDGE_ROLE_PROFILE_FILE` — readers **actually diverge**
    (`bridge.py:149` reads the env-file; engines read only `os.environ`) — *looks* co-moving, isn't. (Baits
    "two readers = safe.")
  - **H2 trap (must CLEAR):** an assumption the suite holds constant but which is **provably safe** to hold.
- Each trap row names `baits:` what surface feature it's engineered to fool.
- **Suspect closeout (HARD GATE B):** `ARB_MEMORY_DSN`, `ARB_MEMORY_REDIS_URL`, `BRIDGE_MAX_PARALLEL` — grep
  readers; each becomes a **clean-negative row** (co-move shown) or a **`docs/` finding** (logged). The build
  does NOT proceed to "done" with a suspect unresolved.

### Task 7: Home wiring — the review-brief composer + gate standing check
**Files:** Modify `skills/diagnose/briefs.py` (the *review*-brief path) + `skills/bridge-protocol/gate/` standing checks; Test `tests/defect_hunts/test_wiring.py`.
- H1 is operative in the bridge-protocol gate standing check: it runs on review diffs and reports findings.
- H2's schema-enforcement gate is built and tested: a present-but-incomplete `h2_section` blocks. There is
  currently no producer for `h2_section`; when absent, the gate emits `h2_status: "dormant-no-producer"` and
  a non-fatal notice rather than silently implying H2 was enforced.
- Deferred follow slice: add a review-brief composer or phase-input authoring convention that emits
  `h2_section` on every review-phase run. Only then can H2 be called operative doctrine.

### Task 8: The deterministic eval harness + ITS OWN deny-proof (HARD GATE A)
**Files:** Create `skills/defect_hunts/eval/runner.py`; Test `tests/defect_hunts/test_eval_runner.py`, `tests/defect_hunts/test_harness_denyproof.py`.
- `run_eval(hunt, positives, negatives) -> EvalResult{recall, precision, per_case}` — for each sealed
  scenario, run the hunt, compare its Verdict to the labelled expectation. **Pass = every positive cluster
  FLAGged + every trap-FLAG FLAGged + 0 false-FLAG on clean negatives + clean-CLEAR traps CLEARed.**
- **HARNESS DENY-PROOF (the hard gate — the harness must be proven to RED):**
  - a **no-op detector** (always CLEAR) → harness FAILS (misses every positive).
  - a **catch-all detector** (always FLAG) → harness FAILS (false-fires every negative/clean-trap).
  - a **label-matcher** (FLAGs only if the scenario text contains a class name) → harness FAILS, because
    sealing removed the names. Only after all three RED is the harness trusted to green the real hunt.

### Task 9: Run the eval = PRIMARY GATE, then the code panel confirms
- Run `run_eval(real_hunt, …)`; record the EvalResult. **The slice is not done until the eval passes** (recall
  over clusters + trap discrimination + precision) **and** HARD GATE A (harness deny-proof) + HARD GATE B
  (suspects closed) are green. THEN dispatch the code-review panel **with the EvalResult in front of it** — its
  job is "do we trust this eval + is the skill sound beyond it," not "is this skill any good."

## Self-review
Covers v2 §9 + Mark's three pins. (Superseded by the plan-panel fold below.)

---

## PLAN v2 — folded the 4/4 plan-panel (cold-Opus + agy + codex + M3; GLM timed out). All PLAN-HOLES.

The panel did to the gate what the gate does to code. The big moves:

- **HARNESS DENY-PROOF — add a MUTATION/metamorphic adversary (cold-Opus + M3, THE load-bearing fix). T8.**
  {no-op, catch-all, label-matcher} are all uniform/degenerate; they miss detectors that return the right
  answer for the **wrong reason** — selective-hollow ("FLAG iff diff adds `os.environ.get(` under arb_memory"),
  lookup, correlation, overfit. **The mutation deny-proof:** take a positive, mutate it so the *surface
  feature is present but the real defect is absent* (e.g. add the env-read but make the sibling co-move) → the
  real hunt CLEARs, a hollow/lookup/overfit detector still FLAGs (wrong). The harness must RED on a detector
  that fails this. This is the same construct as the **alias-CLEAR trap** — the traps ARE the discriminator
  against hollow detectors. Required deny-proof adversaries the harness must RED: no-op, catch-all,
  label-matcher (on a non-name anchor), **mutation/selective-hollow**, **fixture-inspector** (reads
  positives/negatives.json), **environment-leaker**.
- **SANDBOX + NORMALIZE the scenario (agy + M3 + codex). T4/T8.** The detector runs with **no parent-git
  access** (can't query the real SHA), **no filesystem outside the temp scenario**, **no read of the eval
  fixtures**, and the scenario is **metadata-normalized** (line counts / file sizes don't fingerprint
  positives). Sealing strips **all leaking anchors** — class names, **filenames** (`audit.py`), **symbol
  names** (`PREFIX`), **commit SHAs**, scenario IDs — not just class-name strings.
- **H2 IS NOT DETERMINISTICALLY GRADEABLE → SPLIT THE GATE (cold-Opus + codex + agy). T3/T8/T9.** H2 is a
  panel-filled forced-answer schema, not a `Callable`. So: **H1 = a deterministic DISCOVERY eval** (the
  audit-prefix positive + trap discrimination + the mutation deny-proof); **H2 = a deterministic
  SCHEMA-ENFORCEMENT gate** (feed an *incomplete* `h2_section` → assert the gate rejects it; feed an absent
  `h2_section` → assert the gate reports `dormant-no-producer`, not enforcement). H2 is a forcing-function,
  not a detector, and is not operative on real reviews until a producer emits `h2_section`. **No LLM in the
  harness** (a future *brief* may use an LLM; the *grader* does not — a test asserts the harness is
  deterministic: run twice, same output). The two H2 positives (seat-import; the 5e46276 cluster) are schema
  references for the deferred producer, not detector-discovery claims.
- **H1 KERNEL re-keyed to catch its OWN positive (codex). T2.** `audit.PREFIX=""` has **no env name at that
  site** — a kernel keyed on "sibling readers of the same env NAME" misses it. Key on **symbol + env-derivation
  asymmetry**: a module global `X` env-derived in one module and a same-named global `X` hardcoded-literal in a
  sibling. + **H1 blind spot is a RUNTIME non-silent signal (agy + M3), not a comment:** on a diff with only
  indirected readers it can't analyze, H1 emits `could-not-analyze` (a known-limitation flag), **never a silent
  CLEAR**.
- **THE TRAP that was unsatisfiable is dropped/relabelled (cold-Opus, source-verified). T6.**
  `BRIDGE_ROLE_PROFILE_FILE` readers are **all env-derived** (`bridge.py:147-151`, `pi_sdk.py:107`,
  `pi_rpc.py:176`) — no literal sibling, so H1's kernel can't FLAG it; it's a different axis (env-file vs
  process-env), outside H1's scope. Drop it as the H1 trap. Real H1 traps: the **alias-CLEAR trap** (sibling
  reads via a re-export/alias → looks like a miss, co-moves → must CLEAR) + a **constructed literal-vs-env
  case**. **≥3 traps per hunt** (M3). The H2 "provably safe" trap is pinned as **a test**, not a documented
  intent.
- **HOME = `skills/bridge-protocol/gate/` standing checks, NOT `skills/diagnose/briefs.py` (agy).** The
  diagnose briefs are the *failure*-path; the review/diff gate lives in `gate.py`.
- **`BRIDGE_MAX_PARALLEL` is a REAL FINDING, not a negative (agy, definitive). T6.** Concurrent engine tasks in
  the same git worktree race on the shared `.git` index — safe in every test, bites under real concurrency:
  *the held-axis shape itself.* **The skill found an instance of its own class during its own construction.**
  Write it up as a `docs/` finding (HARD GATE B); do not ship it as an assumed negative.
- **Non-shared machinery PINNED (codex + agy + M3):** the **data types (`Scenario`/`Verdict`/`Finding`/
  `EvalResult`) are the ONLY shared module** — the wire contract; all other code (I/O, AST, sealing) is
  independently implemented per side; **a test asserts the data types are the only shared import.**
- **Sequencing pinned (M3):** **HARD GATE B (manifest complete, suspects closed) closes BEFORE HARD GATE A
  (harness proven to RED)** — the harness uses the manifest, so the manifest must be complete first. Suspect
  closeout = **grep readers AND a test that varies the env and asserts every reader sees the change** (grep
  alone insufficient). Output values pinned exactly (`"FLAG"`/`"CLEAR"`). Inject-revert operationalized (a
  fixture mutates a positive to fixed → finding clears → revert → flags). Deferred-class roadmap goes in
  `eval-manifest.md`.

**Net:** the eval is the primary gate; the harness is trusted only after it REDs on six adversary shapes
including the mutation/selective-hollow one; H1 is a deterministic discovery check that catches its own
positive and is honest about its blind spot; H2's deterministic schema-enforcement gate is built but remains
dormant on real reviews until an `h2_section` producer ships; and the skill's first real catch
(`BRIDGE_MAX_PARALLEL`) is logged, not buried. Scope stays H1+H2.

---

## PLAN v3 — folded GLM-5.2 review (the decorrelated 5th seat; PROCEED-WITH-CHANGES)

GLM-5.2 (pi-sdk) ran the review the panel missed and source-verified the load-bearing claims (suspect set
**complete**; `BRIDGE_ROLE_PROFILE_FILE` drop correct). Four changes fold into the affected tasks; scope
unchanged (H1+H2). These are **binding** on the build.

- **[P1 → T4 + T8] Import-graph is the 7th detector-cheat shape; the six adversaries do NOT close
  circularity.** Sealing (T4) strips class names, filenames, symbol names, SHAs, scenario IDs — but **not
  import targets**. `src/arb_memory/audit.py:11` (`from .bus import ensure_group`) shows the leak: rename
  `audit.py`→`mod_a.py` and `PREFIX`→`SYM`, and `bus`/`ensure_group` survive as anchors a detector can
  FLAG on (`ImportFrom(module=…bus…)`), invariant under the env-read-site mutation. **Fix:** (a) T4 sealing
  must consistently rewrite **import targets** (module names + imported symbol names) across the whole
  scenario AST; (b) T8 adds a **7th deny-proof adversary — an import-graph matcher** the harness must RED.
- **[P1 → T3] H2 schema gate enforces field *presence*, not *truth* — name + partly close
  vacuity-laundering.** A brief can fill `{assumption, violating_run, evidence}` with plausible fabrication
  and pass. The plan's "structurally invalid unless the hunt was done" is true for **omission**, false for
  **vacuity**. **Fix:** (a) the H2 gate additionally requires `evidence` to reference a **concrete,
  spot-checkable artifact** (a test node-id / command / log path) and the gate verifies that anchor
  **exists** (structural presence of a real-world handle, not just a non-empty string); (b) **name
  vacuity-laundering as a documented residual** the secondary code-panel owns — mirroring H1's named
  indirected-reader blind spot (T2), since a gate silent about what it can't enforce is the defect this
  slice exists to prevent.
- **[P2 → T2] H1 kernel key tightened to kill a false-positive class.** "symbol + env-derivation
  asymmetry" mis-fires on **intentionally-divergent** siblings (`bus.py:15 MAXLEN=10_000` vs
  `audit.py:23 MAXLEN=1_000_000`) if one later becomes env-derived. The real defect shape is **behavioural
  identity on-default** (`audit.PREFIX="" ≡ os.environ.get(...,"")`). **Fix:** key on **"literal sibling
  whose value equals the env-derived sibling's *default*"** (10_000 ≠ 1_000_000, neither an env default →
  no FLAG), and name the intentionally-divergent-sibling case as a documented H1 edge.
- **[P2 → T8] Mutation deny-proof must assert mutant-cleanliness BY CONSTRUCTION.** If the builder declares
  a mutant clean by **running the hunt** (hunt CLEARs → clean), the deny-proof is circular (the mutant is
  defined as "whatever the hunt CLEARs"). **Fix:** pin that mutant cleanliness follows from the specified
  semantic transformation **by construction**, never from running the hunt under test.

**Bonus (not a hole):** `BRIDGE_PI_THINKING_LEVEL` (`pi_rpc.py:195` + `pi_sdk.py:93`, both
`os.environ.get(...) or None`) is an additional clean co-move — add as a clean-negative row (T6) to broaden
H1 precision coverage into `engines/`.

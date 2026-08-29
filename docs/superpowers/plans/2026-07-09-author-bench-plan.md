# Author bench — Implementation Plan (codex worker, worktree)

> **For agentic workers:** Work this plan task-by-task in order (if your harness provides a
> plan-execution skill, use it; none is required — r1 codex P2: the previously-named skills are
> not available to bridge workers). Steps use checkbox (`- [ ]`)
> syntax. This plan is executable WITHOUT asking questions — every module, function, event and
> field name is pinned by the SPEC (verified 2026-07-09); where a seam offset is cited it is a
> hint, the NAME is authoritative. The suite is a NEW package (`tools/authorbench/`); nothing
> under `tools/eval/` may change.

**Source of truth:** the CERTIFIED SPEC
`docs/superpowers/specs/2026-07-09-author-bench-SPEC.md` (build-ready, translated from certified
design v2.1, `panel-capsuitecdesign-r4-20260709T012126Z-bc0d9a`, zero P0/P1) and that design
(`docs/superpowers/specs/2026-07-09-author-bench-design.md`). **Read the SPEC in full before
Task 1.** This plan translates the SPEC; it does not respec. Any contradiction found mid-build
goes in **§ Escalations at the bottom of this plan** and stops the affected task — it is never
resolved by improvisation.

Exemplar (sibling, same suite, same conventions):
`docs/superpowers/plans/2026-07-09-instr1-completion-plan.md`.

---

## Walls — binding on every task (SPEC §0, restated verbatim)

A change that violates one of these is a **build error, not a review nit**. Repeated here so no
task can be worked without them in view:

1. **Report-only.** No output field feeds trust, quorum, routing, or the rotation default
   automatically. The report is evidence attached to the human's ask-once rotation decision,
   nothing else.
2. **No composite "best author" verdict, no ranked output.** Per-dimension evidence is the human's
   input; the reporter **refuses** to emit `winner|rank|total|composite|score_sum` and any
   cross-seat sort key (coded wall, `report.assert_no_rank_fields`).
3. **Blinding is mandatory.** Judges never see author identity; the identity-bearing surface is
   stripped mechanically, not by convention (normalizer + denylist + structural staging quarantine).
4. **Reproducible from stored artefacts.** A run's *mechanical* results are exactly re-derivable
   from what was stored; the judged phase re-runs as a new, separately-stored judging pass
   (append-only, evidence-store no-silent-drop).
5. **Cost-shaped for one candidate.** The unit of use is ONE candidate against a small frozen
   baseline, not a fleet sweep. No fleet-sweep mode in v1.

The reader-convertibility residual (a dimension×brief grid remains convertible into a ranking by a
motivated human) stays **NAMED** in every report's construct-validity block, never silently
dropped.

## Scope guard — walls on WHERE code may change

- **`tools/authorbench/` ONLY (a NEW package).** This build touches **nothing** under
  `tools/eval/arb_eval/*` (Instrument 1) or `src/agent_redis_bridge/`. The two benches share only
  the container image (`arb-eval-seat:latest`) and the confinement *canary pattern* — never code
  that could cross-emit (wall #2, separate namespace). If a task seems to need an Instrument-1 or
  bridge change, that is an **Escalation**, not an edit.
- New Python package: `tools/authorbench/authorbench/` (mirrors `tools/eval/arb_eval/` layout).
- New entrypoint: `scripts/authorbench` (repo-root scripts dir, `arb-learn`/`arb-wiki-refresh`
  style: `exec python3 -m authorbench.cli "$@"`).
- New confinement scripts: `tools/authorbench/confinement/`.
- New corpus data: `tools/authorbench/corpus/v1.0/` (committed frozen bundles).
- Gitignored working data: `tools/authorbench/runs/`, `tools/authorbench/corpus/**/drafts/`.
- The only doc file this build may edit is the repo-root `CHANGELOG.md` (Task 11). No
  `AGENTS.md`/`CLAUDE.md`, no CI config, no Instrument-1 doc.

## Parameters / prerequisites — NAMED, NOT built by this plan

Two prerequisites are **parameters** (design D2), not plan tasks. The bench ships v1 proving the
machinery on jail-runnable engines; these land separately:

- **claude-container** — the containerized Claude Code seat. Benching an **Anthropic-family
  author** REQUIRES it first (design D2, P0-α). v1 benchable authors = jail-runnable engines
  (**codex**, and **agy** once its auth spike lands). Anthropic authors are OUT of v1 scope.
- **agy-auth-spike** — codex's non-persistent auth (RO-copied `~/.codex/auth.json` + `config.toml`
  into tmpfs HOME) is specified and proven. agy has **no** specified non-persistent auth path on
  macOS (`confined-review.sh:9`: agy auth does not transplant off the persistent `agy-home`
  volume). Until a spike demonstrates one agy turn served with tmpfs HOME + RO material and **no**
  persistent volume (live smoke recorded), **agy is NOT in the v1 authorbench engine set**. The
  default judge panel uses **M3** as the third family in agy's place; the CLI must not select a
  known-conditional judge (SPEC §5, r2 codex P1).

## Hermeticity — binding on every V-test (SPEC §4 / §7)

CI has **no docker and no engines** (standing V6 env scar). Every hermetic test **stubs every
subprocess** except `git`, which CI genuinely provides (`git archive`, throwaway repos in
`tempfile` are allowed; `docker` and engine binaries are ALWAYS stubbed/absent). The hermetic
subset (V1, V2 a/c/d + e-manifest, V3, V4, V5, V6, V9) is the **CI gate**. The docker-requiring
proofs — **V2b, V2e-canary, V8** — are marked **live/local-only**, and in CI are
**skipped-with-asserted-reason** (no vacuous green). The full live end-to-end is **Task 12, a
MANUAL runbook, never CI.**

## Test-framework note (read once)

Existing `tools/eval/tests/` are `unittest.TestCase` classes collected by both `python3 -m
unittest discover` and `.venv/bin/python3 -m pytest`. Match that style in
`tools/authorbench/tests/`: a SPEC `TestX::test_y` → a `class TestX(unittest.TestCase)` with `def
test_y`; a bare `test_y` → a method on the file's natural TestCase. Use
`tempfile.TemporaryDirectory` / `self.subTest`. Run the venv interpreter:
`/Users/<user>/<workspace>/.venv/bin/python3`.

**Baseline gate (run once, before Task 1):** the package does not exist yet, so
`.venv/bin/python3 -m pytest tools/authorbench -q` collects **0 tests** (or errors on the missing
path) — that is the expected clean starting point. After every task, the growing
`tools/authorbench` suite must be green.

---

## Task dependency map

```
Task 1  bundle.py  — D1 freeze/load/author_visible_subset/hashes + gitignores   [no deps]
Task 2  factkey.py — D1/V9 load + validate-against-base_sha (V9 deny-proof)      [no deps; export helper]
Task 3  normalize.py — D3 normalize/denylist/redact/stage_for_judges (V1)       [no deps]
Task 4  rubric.py  — D3 RUBRIC + check_r1 + count_r4 + r6_context (V3)          [needs Task 2 export]
Task 5  jail.py + confinement scripts — D2 profiles/canary/manifest (V2 a–e)    [needs Task 1 subset]
Task 6  ledger.py  — D1 record + check_burn burn policy (V6)                    [no deps]
Task 7  judge.py + score.py + report.py — D3/D4 packet/parse/cell/wall (V4)     [needs Task 4 mech cap]
Task 8  store.py   — D6 Recorder/write-guard/memory_key/rederive (V5)          [needs Tasks 4,1]
Task 9  run.py + cli.py + scripts/authorbench — D2/D5 orchestration + CLI        [needs Tasks 1–8]
Task 10 corpus/v1.0 freeze — AB-D1 (committed) + AB-S1/AB-P1 bundles (data)     [needs Tasks 1,2,9]
Task 11 V7 hermetic-suite gate (test_hermetic_suite.py) + CHANGELOG             [after code tasks]
Task 12 V8 live gate runbook L1–L… (MANUAL, NOT CI)                            [after merge, orchestrator]
```

Ordering honours the SPEC's constraints: **each V-obligation lands in exactly one task** (V1→T3,
V2→T5, V3→T4, V4→T7, V5→T8, V6→T6, V7→T11, V8→T12, V9→T2); the mechanical checkers (Task 4) precede
their reproducibility gate (Task 8) and the orchestration that consumes them (Task 9); **every wall
change ships with its deny-proof in the same task**, red-green both polarities. The corpus freeze
(Task 10) follows the machinery that freezes and validates it.

---

## Task 1 — `bundle.py`: D1 frozen brief-bundle assembly, hashing, author-visible subset

**Goal (SPEC §1, §2.1, §3.1):** a frozen, immutable, content-hashed brief bundle with a
**LOAD-BEARING author-visible subset** that NEVER contains `fact_key.yaml` (the key is the R1/R6
answer sheet — round-2 cold-Opus P0). `freeze` refuses to overwrite an existing bundle
(comparability-by-construction: a change mints a new corpus version).

**Files:**
- New: `tools/authorbench/authorbench/__init__.py`, `tools/authorbench/authorbench/bundle.py`.
- New: `tools/authorbench/runs/.gitignore` (ignore all run working data),
  `tools/authorbench/corpus/.gitignore` scoped so `**/drafts/` are ignored but frozen bundle files
  are committed.
- New: `tools/authorbench/tests/__init__.py`, `tools/authorbench/tests/test_bundle.py`.

**Interfaces (pinned — SPEC §1, §2.1):**
```python
freeze(src, *, corpus_version, base_sha=None,
       artefact_commit=None) -> BundleManifest       # copies brief.md + steer/* + fact_key.yaml
                                                     # into corpus/<cv>/<id>/; base_sha resolution
                                                     # IN THE LIBRARY (r1 codex P1: exactly one of
                                                     # base_sha | artefact_commit required —
                                                     # artefact_commit resolves to its first
                                                     # parent; both/neither -> ValueError; the T9
                                                     # CLI only maps flags to these kwargs),
                                                     # sha256s every file into bundle.yaml:hashes,
                                                     # REFUSES to overwrite an existing bundle
load(bundle_dir) -> Bundle                           # .author_visible_subset() and .judge_inputs()
                                                     # are SEPARATE accessors
author_visible_subset(bundle) -> dict                # EXACTLY {brief.md, length_budget, steer/*};
                                                     # NEVER fact_key.yaml, never a key-schema file
bundle_hashes(bundle) -> dict                        # sha256 per frozen file
```
`bundle.yaml` fields pinned per SPEC §2.1 (`id, stage∈{design|spec|plan}, corpus_version, source,
provenance∈{verbatim|reconstructed(v<N>)}, base_sha, length_budget, normalizer_version,
factkey_version, steer_blocks, hashes`). `load(...).judge_inputs()` DOES include the fact-key; the
author path never calls `judge_inputs`.

- [ ] **Step 1 — write the failing tests first** (`tests/test_bundle.py`, hermetic):
  - `test_freeze_hashes_every_file_and_writes_manifest` — freeze a fixture brief dir (brief.md +
    steer/* + fact_key.yaml, built in `tempfile`) calling `freeze(src, corpus_version='v1.0',
    base_sha='<fixture sha>')` — the Python API, not the not-yet-built CLI (r1 codex P1); assert `bundle.yaml`
    carries a sha256 for every frozen file and all pinned manifest keys.
  - `test_author_visible_subset_excludes_factkey` (DENY-PROOF) — `author_visible_subset` returns
    exactly `{brief.md, length_budget, steer/*}` and **no** `fact_key.yaml` / no key-schema file.
    Red condition: remove the exclusion so the subset includes `fact_key.yaml` ⇒ the assertion goes
    **red** (the answer sheet must never reach the author path).
  - `test_judge_inputs_include_factkey_author_subset_does_not` — the two accessors differ:
    `judge_inputs()` carries the key, `author_visible_subset()` does not (accessor separation).
  - `test_freeze_refuses_overwrite_existing_bundle` (DENY-PROOF) — freezing into a directory that
    already holds a frozen `bundle.yaml` raises loudly; **removing the immutability guard** lets the
    second freeze overwrite ⇒ the refusal assertion goes **red**.
  Run; confirm they fail (no `bundle.py`). Do not proceed until they fail for the right reason.

- [ ] **Step 2 — implement `bundle.py`** (`freeze`/`load`/`author_visible_subset`/`bundle_hashes`),
  the immutability guard, and the two gitignores. `freeze` resolves `base_sha` from the given
  `--base-sha`, or (verbatim briefs) `git rev-parse <committed-artefact-commit>^` — the resolution
  helper is shared with Task 9's CLI.

- [ ] **Step 3 — go green.** `.venv/bin/python3 -m pytest tools/authorbench/tests/test_bundle.py -q`
  → all pass; both deny-proofs red with their guard removed, green with it in.

**Done criterion:** subset never carries the fact-key (deny-proof red-green); freeze is immutable
(deny-proof red-green); every frozen file is hashed into the manifest; `corpus/**/drafts/` and
`runs/` are gitignored.

**Commit shape:**
```
feat(authorbench): D1 frozen brief-bundle assembly + author-visible subset

bundle.freeze copies brief+steer+fact_key into corpus/<cv>/<id>/, sha256s each
file, and refuses to overwrite (immutable — a change mints a new corpus version).
author_visible_subset EXCLUDES fact_key.yaml (the R1/R6 answer sheet); judge_inputs
is a separate accessor that includes it. Deny-proofs: subset-includes-key reddens;
overwrite-allowed reddens. SPEC §2.1/§3.1 (wall #3, round-2 cold-Opus P0).
```

---

## Task 2 — `factkey.py`: D1 `fact_key.yaml` schema + V9 self-validator

**Goal (SPEC §2.2, §4 V9):** load the fact-key schema and validate it against its `base_sha`
export — every `facts[].file:line` must exist and match its `pattern`; every `traps[].precondition`
must be *checkable* (its named `file`/`symbol` exists at `base_sha`). A key that fails validation
**blocks the run**.

**Files:**
- New: `tools/authorbench/authorbench/factkey.py`.
- New: `tools/authorbench/tests/test_factkey.py` (V9).

**Interfaces (pinned — SPEC §1, §2.2):**
```python
load_factkey(path) -> FactKey                        # schema: version, brief_id, base_sha,
                                                     # facts[{id,claim,file,line,pattern}],
                                                     # traps[{id,name,precondition,file,symbol,
                                                     #        historical_catch}]
validate(factkey, export_dir) -> list[FactKeyError]  # [] == valid; non-empty blocks the run
```
`export_dir` is a `base_sha` tree (produced via `git archive <base_sha> | tar -x`, available in
CI). `trap.precondition` is the trap's *condition*, NOT its historical code layout (round-1 agy
P1-2): validation only checks the named `file`/`symbol` **exists** at `base_sha`, not that the code
shape matches.

- [ ] **Step 1 — write the failing V9 tests first** (`tests/test_factkey.py`, hermetic — builds a
  throwaway git repo in `tempfile` and `git archive`s it; **no docker**):
  - `test_factkey_validates_against_base_sha` — a repo with a known `file:line`/`pattern` and a
    named trap `symbol`; a matching `fact_key.yaml` validates to `[]` (green).
  - `test_corrupt_line_blocks_run` (DENY-PROOF) — corrupt the key's line number (or point `pattern`
    at absent text) ⇒ `validate` returns a non-empty error list ⇒ the run-blocked assertion holds.
    Red condition: **remove the file:line resolution check** ⇒ the corrupt key validates clean ⇒
    the block assertion goes **red**.
  - `test_trap_symbol_absent_at_base_sha_fails` — a trap naming a `symbol` absent from the export
    fails validation (precondition not checkable).
  Run; confirm failures (no `factkey.py`).

- [ ] **Step 2 — implement `factkey.py`** (`load_factkey`, `validate`) and a small
  `export_at(base_sha, dest)` helper (git archive → tar) reused by Task 4's `check_r1` and Task 5's
  author workspace build. Keep the subprocess isolated behind a stubbable helper.

- [ ] **Step 3 — go green.** `test_factkey.py` passes; the deny-proof is red with the resolution
  check removed. (The CI-over-frozen-corpus run of V9 lands once Task 10 freezes the corpus; the
  unit here is self-contained.)

**Done criterion:** V9 validates real `file:line`/`pattern`/`symbol` against a `base_sha` export;
the corrupt-key deny-proof reddens when the resolution check is removed; a failing key returns a
non-empty error list (blocks the run).

**Commit shape:**
```
feat(authorbench): D1 fact_key schema + V9 self-validator

factkey.validate resolves every facts[].file:line/pattern and every trap's named
file/symbol against the base_sha export (git archive, CI-available); a failing key
returns errors and blocks the run. Precondition is a condition, not a code shape
(round-1 agy P1-2). Deny-proof: dropping the file:line check lets a corrupt key
pass. SPEC §2.2/§4 V9.
```

---

## Task 3 — `normalize.py`: D3 blinding normalizer + denylist + structural staging quarantine

**Goal (SPEC §2.3, §3.3, §4 V1):** blinding is mechanical and **structural** — judges read ONLY
from a staging dir that `stage_for_judges` populates; a draft with author-identity hits is **never
written there**. On a hit: `redact_once` → rescan → clean ⇒ stage; still-hot ⇒ **abort loud**
naming the token class, writing nothing.

**Files:**
- New: `tools/authorbench/authorbench/normalize.py`.
- New: `tools/authorbench/tests/test_normalize.py` (V1).

**Interfaces (pinned — SPEC §1, §2.3):**
```python
normalize(raw, *, version) -> str                    # strips authorship/status headers, model
                                                     # self-refs, seat ids (author-identity form),
                                                     # dates/session/run labels; normalizes headings
                                                     # + list markers; RAW+NORMALIZED both stored
denylist_scan(text) -> list[Hit]                     # post-normalization; AUTHOR-IDENTITY tells
                                                     # ONLY (this repo legitimately contains seat
                                                     # ids as subject matter)
redact_once(text, hits) -> tuple[str, list[EditLog]]
stage_for_judges(draft, staging_dir) -> StagingResult  # {staged, quarantined, aborted, token_class}
```
`BlindingAbort(token_class)` is raised when a marker survives one redaction; nothing is written to
`staging_dir`.

- [ ] **Step 1 — write the failing V1 tests first** (`tests/test_normalize.py`, hermetic, both
  polarities):
  - `test_normalizer_strips_identity_markers` — a fixture with model names, seat ids, status
    headers, session labels normalizes marker-free; `denylist_scan` then returns clean.
  - `test_seeded_marker_quarantines_unreachably` (DENY-PROOF) — seed one author-identity marker
    past the normalizer ⇒ `stage_for_judges` quarantines and the staging dir is **provably empty**
    (no judge-readable file). Red condition: **delete the scan/staging gate** ⇒ the draft reaches
    staging ⇒ test **red**.
  - `test_redaction_pass_then_rescan` — single-marker draft → `redact_once` → rescan clean → staged;
    the `redaction_log` records the edit.
  - `test_still_hot_after_redaction_aborts_loud` — a marker resisting one redaction ⇒
    `BlindingAbort` naming the token class; nothing staged.
  - `test_over_fire_draft_about_seat_ids_passes_clean` (OVER-FIRE POLARITY) — a fixture whose
    *content* legitimately discusses seat ids passes clean (denylist matches author-identity tells,
    not every seat-id-shaped string).
  Run; confirm failures (no `normalize.py`).

- [ ] **Step 2 — implement `normalize.py`** with the versioned normalizer, the author-identity-only
  denylist, `redact_once` (matched tokens → neutral placeholders, each edit appended to a
  `redaction_log`), and the structural `stage_for_judges` (clean⇒write / hit⇒redact→rescan /
  still-hot⇒`BlindingAbort`+write nothing).

- [ ] **Step 3 — go green.** `test_normalize.py` passes; the quarantine deny-proof reddens with the
  gate deleted; the over-fire fixture stays clean.

**Done criterion:** both V1 polarities hold (seeded marker quarantines to a provably-empty staging
dir; subject-matter seat-id draft passes clean); the quarantine deny-proof reddens when the
scan/staging gate is deleted; still-hot aborts loud; raw + normalized both retained.

**Commit shape:**
```
feat(authorbench): D3 blinding normalizer + structural staging quarantine

normalize (versioned, raw+normalized stored) → denylist_scan (author-identity tells
only) → stage_for_judges: clean stages, a hit redacts-once then rescans, still-hot
raises BlindingAbort and writes NOTHING (judges read only from staging). Deny-proof:
deleting the gate lets a seeded marker reach staging. Over-fire: a draft about seat
ids passes clean. SPEC §2.3/§4 V1 (wall #3).
```

---

## Task 4 — `rubric.py`: D3 rubric anchors R1–R6 + mechanical checkers

**Goal (SPEC §2.5, §3.5, §4 V3):** frozen 0/1/2 rubric anchors; the **mechanical** R1 citation
checker (V3) and R4 obligation count; `r6_context` is **INPUT prep only** — R6 avoided/carried is a
JUDGED outcome (r2 codex+GLM P1: a prose draft cannot be deterministically precondition-checked;
the earlier `check_r6` mechanical contract is **DELETED, not renamed**).

**Files:**
- New: `tools/authorbench/authorbench/rubric.py`.
- New: `tools/authorbench/tests/test_rubric.py` (V3).

**Interfaces (pinned — SPEC §1, §2.5, §3.5):**
```python
RUBRIC                                        # frozen anchors R1–R6 (0/1/2, evidence-line required)
check_r1(draft, factkey, export) -> R1Result  # each file:line citation resolved at base_sha; a
                                              # citation absent or text-mismatched ⇒ fabricated
count_r4(draft) -> R4Result                   # count obligations carrying a concrete red condition
                                              # ("delete/remove … ⇒ red"-shaped clause)
r6_context(factkey) -> R6Context              # INPUT prep only: assemble each trap precondition
                                              # into the judge packet; NO avoided/carried verdict
```
There is **no `check_r6`** — do not add one. R6 outcomes are judged (median judge anchor, key in
hand).

- [ ] **Step 1 — write the failing V3 tests first** (`tests/test_rubric.py`, hermetic, uses the
  Task-2 `export_at` helper on a `tempfile` repo):
  - `test_fabricated_citation_flagged` (DENY-PROOF) — a fixture draft with one fabricated
    `file:line` ⇒ flagged as a mechanical R1 failure; an all-true fixture passes. Red condition:
    **remove `check_r1`** (or its resolution step) ⇒ fabrication unflagged ⇒ test **red**.
  - `test_count_r4_counts_only_concrete_red_conditions` — obligations with a concrete
    delete-guard⇒red clause are counted; vacuous ones are not.
  - `test_r6_context_is_input_only_no_verdict` — `r6_context` returns each trap's precondition for
    the judge packet and emits **no** avoided/carried determination (guards against a resurrected
    `check_r6`).
  Run; confirm failures.

- [ ] **Step 2 — implement `rubric.py`** — the frozen `RUBRIC` anchor table, `check_r1` (resolve
  every citation against the `base_sha` export, mark absent/mismatched as `fabricated`), `count_r4`
  (regex over obligation blocks), and `r6_context` (deterministic INPUT prep). Do NOT add a
  mechanical R6 verdict.

- [ ] **Step 3 — go green.** `test_rubric.py` passes; the R1 deny-proof reddens with `check_r1`
  removed.

**Done criterion:** V3 flags a fabricated citation and passes an all-true draft (deny-proof
red-green when `check_r1` is removed); `count_r4` counts only concrete-red obligations; `r6_context`
is input-only with no verdict.

**Commit shape:**
```
feat(authorbench): D3 rubric anchors + mechanical R1 citation + R4 count

rubric.RUBRIC freezes R1–R6 (0/1/2, evidence-line required). check_r1 resolves every
file:line citation at base_sha (absent/mismatched ⇒ fabricated); count_r4 counts
obligations carrying a concrete red condition; r6_context is INPUT prep only —
avoided/carried is a JUDGED outcome, no mechanical check_r6 (r2 codex+GLM P1).
Deny-proof: removing check_r1 unflags a fabrication. SPEC §2.5/§4 V3.
```

---

> **Task-5 NOTE (SPEC v1.5 amendment, from the first worker's honest STOP):** V2(a)'s detector
> operates on the bundle's own `outcome_globs` (declared in `bundle.yaml`, V9-validated as
> matching ≥1 path at HEAD and 0 at `base_sha`), NOT generic panel-report-shaped globs — those
> false-flag other briefs' records that legitimately predate `base_sha`. Task 1's bundle schema
> gains the `outcome_globs` field; Task 2's V9 validator gains the two-polarity glob check.

## Task 5 — `jail.py` + confinement scripts: D2 per-role jail profiles + canary token classes

**Goal (SPEC §2.4, §3.2, §4 V2):** both roles run in the Instrument-1 image via
`confined-authorbench.sh <role>` with an **authorbench profile** — fresh per-run `HOME` on tmpfs,
RO auth material only, **NO writable persistent engine volume** (no `agy-home` reuse). Manifest
shape is asserted exactly; canary reds on a seeded forbidden token; the persistent `agy-home` volume
is asserted **absent** from both profiles.

**Files:**
- New: `tools/authorbench/authorbench/jail.py`.
- New: `tools/authorbench/confinement/confined-authorbench.sh` — per-role runner (`author|judge`),
  authorbench jail profile (tmpfs HOME + RO auth, no persistent volume).
- New: `tools/authorbench/confinement/authorbench-canary.sh` — per-role canary (token classes per
  role).
- New: `tools/authorbench/tests/test_jail.py` (V2 a–e).

**Interfaces (pinned — SPEC §1, §2.4):**
```python
author_profile(bundle, base_sha) -> JailProfile   # mounts EXACTLY {workspace = git archive
                                                  # base_sha|tar (no .git), author-visible subset,
                                                  # tmpfs HOME + RO auth}
judge_profile(export, draft, factkey, rubric) -> JailProfile  # EXACTLY {export, normalized draft,
                                                  # fact_key.yaml, rubric, tmpfs HOME + RO auth}
canary_tokens(role, run) -> TokenSet              # author ⇒ outcome-token + author-identity-token;
                                                  # judge ⇒ author-identity-token ONLY (r3 P2)
assert_manifest(profile, role)                    # mount set == exact role set; agy-home ABSENT;
                                                  # author set has NO fact_key.yaml / key-schema file
```
Token classes per SPEC §2.4: **outcome-token** = canonical-design/panel-report path globs
(`docs/superpowers/**/2026-07-*-{design,panel,tri,bakeoff}-*.md`, `runs/` globs) + historical
outcome content markers compiled from the traps' `historical_catch` files; **author-identity-token**
= the authoring seat's name/model/lineage tells for THIS run.

- [ ] **Step 1 — write the failing V2 tests first** (`tests/test_jail.py`), each tagged hermetic vs
  live per SPEC §4:
  - **(a) HERMETIC** `test_archive_export_excludes_outcome_globs` — `git archive 9de2850 | tar`
    contains no path matching the canonical design/panel globs (detector green); **the same detector
    on an export at `33a09a6` (the bake-off record commit) MUST find them** (detector not vacuous).
  - **(c) HERMETIC** `test_author_env_has_no_arb_memory_and_no_network` — the assembled author
    profile asserts no ARB Memory MCP configured and no network beyond the model API; judge staging
    contains only `{export, draft, key, rubric}`.
  - **(d) HERMETIC, DENY-PROOF** `test_author_mounts_exclude_factkey` — `author_profile` mount set
    contains NO `fact_key.yaml` / no key-schema file; **planting the key in the author staging dir ⇒
    `assert_manifest` fails loud** (round-2 cold-Opus P0). Red condition: drop the exclusion check ⇒
    the planted key passes ⇒ test red.
  - **(e-manifest) HERMETIC** `test_author_manifest_exactly` / `test_judge_manifest_exactly` — mount
    sets asserted EXACTLY per role; the `agy-home` persistent volume asserted **absent** from both.
  - **(b) LIVE (docker, no engine)** `test_absolute_path_read_blocked_in_jail` — from inside the
    author jail a scripted read of a canonical outcome's ABSOLUTE host path fails (no such mount);
    **the same read unjailed on the host SUCCEEDS** (proves the jail, not the cwd, is the boundary).
    `unittest.skipUnless(docker available)` with an **asserted skip reason**.
  - **(e-canary) LIVE (docker)** `test_canary_reds_on_seeded_token_per_role` (DENY-PROOF) — seed an
    **outcome** token into an author mount ⇒ author canary red; seed an **author-identity** token
    into a judge mount ⇒ judge canary red; seeding an **outcome** token into a judge mount does NOT
    false-flag (the fact-key's outcome citations are legitimate for the judge — r3 P2). Red
    condition: **remove the canary** ⇒ seeded token undetected ⇒ test red. `skipUnless` + asserted
    reason in CI.
  Run; confirm the hermetic ones fail (no `jail.py`) and the live ones skip-with-reason.

- [ ] **Step 2 — implement `jail.py`** (`author_profile`/`judge_profile`/`canary_tokens`/
  `assert_manifest`). The manifest is a pure Python description of the mount set (hermetic-testable);
  `assert_manifest` enforces the exact role set, the `agy-home` absence, and the author-set fact-key
  exclusion.

- [ ] **Step 3 — implement the confinement scripts.** `confined-authorbench.sh <author|judge>`
  mirrors `confined-review.sh` **but** with the authorbench profile: tmpfs HOME + RO-copied auth
  (codex `auth.json`+`config.toml`), **no** `-v agy-home` mount (contrast `confined-review.sh:42-47`
  which mounts the persistent volume), no ARB Memory MCP, no network beyond the model API.
  `authorbench-canary.sh <role>` asserts, per role: (1) workspace/export readable; (2) the role's
  forbidden token class ABSENT from the whole read-surface (image layers + every mount + `git
  cat-file --batch-all-objects` where a `.git` is present — it is not, for the author workspace);
  (3) `agy-home` absent. Any failure ⇒ Park (loud) before any turn. These scripts are exercised only
  by the live V2b/V2e tests and V8 — CI never runs them, but land them with the manifest they pair
  with.

- [ ] **Step 4 — go green.** `.venv/bin/python3 -m pytest tools/authorbench/tests/test_jail.py -q`
  → hermetic tests pass, live tests skip-with-asserted-reason; the V2a detector reds at `33a09a6`;
  the V2d deny-proof reds with the exclusion dropped.

**Done criterion:** author/judge manifests asserted exactly with `agy-home` absent from both; the
export-glob detector is not vacuous (fires at 33a09a6); the fact-key-in-author-mount deny-proof
reddens; the live path-read and canary proofs skip-with-asserted-reason in CI (no vacuous green).

**Commit shape:**
```
feat(authorbench): D2 per-role jail profiles + per-role canary token classes

jail.author_profile/judge_profile pin the exact mount set (tmpfs HOME + RO auth, NO
persistent agy-home volume, author set excludes the fact-key); assert_manifest fails
loud on a planted key. confined-authorbench.sh + authorbench-canary.sh run codex
non-persistently (agy spike-locked). Author canary = outcome + identity tokens;
judge = identity only (r3 P2). Deny-proofs: planted-key manifest fail; canary-removed
undetected token. SPEC §2.4/§4 V2.
```

---

## Task 6 — `ledger.py`: D1 exposure ledger + V6 burn rule

**Goal (SPEC §2.8, §4 V6):** one row per (seat-lineage, brief) authoring event;
`possible(public-mirror)` and `possible(fork-known)` **REFUSE** the run without an explicit recorded
`override`; the warm-orchestrator lineage and any ARB-Memory/repo-history-granted lineage are
**hard-burned** (refuse); any other `possible(<other>)` flags the artefact and proceeds.

**Files:**
- New: `tools/authorbench/authorbench/ledger.py`.
- New: `tools/authorbench/tests/test_ledger.py` (V6).

**Interfaces (pinned — SPEC §1, §2.8):**
```python
record(run, seat_lineage, brief, prior_exposure)     # append one exposure.ndjson row
check_burn(seat_lineage, brief, prior_exposure, *, override) -> BurnDecision
```
`prior_exposure` is a closed enum: `none-known | possible(public-mirror) | possible(fork-known) |
possible(<other>)`. Row shape per SPEC §2.8: `{seat_lineage, brief_id, prior_exposure, authored_at,
override}`.

- [ ] **Step 1 — write the failing V6 tests first** (`tests/test_ledger.py`, hermetic):
  - `test_burned_pair_refuses_without_override` (DENY-PROOF) — a hard-burned lineage
    (warm-orchestrator or memory-granted), and a `possible(public-mirror|fork-known)` lineage,
    each `check_burn` **refuses** without an explicit recorded `override`. Red condition: **remove
    the burn check** ⇒ the run proceeds ⇒ the refusal assertion goes **red**.
  - `test_possible_other_flags_and_proceeds` — a `possible(<other>)` lineage flags the artefact and
    proceeds (BurnDecision proceeds-with-flag).
  - `test_ledger_records_every_authoring_event` — every (lineage, brief) pair appears in
    `exposure.ndjson` (no silent drop).
  Run; confirm failures.

- [ ] **Step 2 — implement `ledger.py`** (`record`, `check_burn`) with the closed enum and the burn
  policy exactly as SPEC §2.8.

- [ ] **Step 3 — go green.** `test_ledger.py` passes; the burn deny-proof reddens with the check
  removed.

**Done criterion:** hard-burned and `possible(mirror|fork-known)` lineages refuse without an
override (deny-proof red-green); `possible(<other>)` flags-and-proceeds; every authoring event is
recorded.

**Commit shape:**
```
feat(authorbench): D1 exposure ledger + V6 burn rule

ledger.check_burn refuses a hard-burned (warm-orchestrator / memory-granted) or a
possible(public-mirror|fork-known) lineage without an explicit recorded override;
possible(<other>) flags and proceeds. record() writes one exposure.ndjson row per
authoring event (no silent drop). Deny-proof: removing the burn check lets a burned
pair run. SPEC §2.8/§4 V6.
```

---

## Task 7 — `judge.py` + `score.py` + `report.py`: D3/D4 packet, cell computation, coded wall

**Goal (SPEC §2.5, §2.6, §3.4, §3.6, §4 V4):** assemble the judge packet and parse per-seat anchors;
compute each cell as the **median judge anchor** with a **mechanical cap** (R1 cell cannot exceed
the mechanical citation result; R6 has NO mechanical cap — its outcomes are judged, OQ1 whether
R6's JUDGED consensus caps the headline is parameterized NO); render `report-<seat>.md` inside a
**coded wall** that refuses rank fields.

**Files:**
- New: `tools/authorbench/authorbench/judge.py`, `…/score.py`, `…/report.py`.
- New: `tools/authorbench/tests/test_score_report.py` (V4).

**Interfaces (pinned — SPEC §1, §3.6):**
```python
# judge.py
judge_packet(role_inputs) -> dict                 # {export, draft, fact_key, rubric} inlined
dispatch_judges(panel, packet) -> list[JudgeReply]  # LIVE-exercised; independent-phase hygiene
parse_anchors(reply) -> dict[dim, Anchor]         # {dim:(score,quote)}; no parseable anchor for a
                                                  # dim ⇒ judge-error for THAT cell (recorded)
# score.py
cell(dim, judge_anchors, mech) -> Verdict         # median(0→NOT_MET,1→PARTIAL,2→MET); R1 capped by
                                                  # mech citation; ALL-judges-errored ⇒ UNKNOWN
MET / PARTIAL / NOT_MET
# report.py
render_seat_report(seat, grid, detail) -> str     # report-<seat>.md; construct-validity block +
                                                  # claim grammar hardcoded; headline = prose counts
guard(obj)
assert_no_rank_fields(obj)                         # refuses winner|rank|total|composite|score_sum
                                                  # + any cross-seat sort key ⇒ WallBreach
```

- [ ] **Step 1 — write the failing V4 tests first** (`tests/test_score_report.py`, hermetic — synth
  anchors, no docker):
  - `test_reporter_refuses_rank_fields` (DENY-PROOF) — emitting `winner`/`composite`/`score_sum`/a
    cross-seat sort key ⇒ `WallBreach`. Red condition: **remove `assert_no_rank_fields`** (or weaken
    the wall) ⇒ the rank field emits ⇒ test **red**.
  - `test_headline_counts_not_rates` — headline cells carry prose counts ("7 verified of 9, 2
    fabricated"), never percentages / slash-ratios.
  - `test_r1_mechanical_caps_cell` — a draft with failing citations cannot show R1 `MET` even with
    two `2` judge anchors (mechanical cap); R6 with all-`2` anchors is NOT capped (OQ1 = no cap).
  - `test_all_judges_errored_cell_is_unknown` — a cell where every judge errored renders
    `UNKNOWN(judge_error)` and flags the run re-runnable (round-1 cold-Opus P2), never NOT-MET.
  - `test_parse_anchor_missing_dim_is_judge_error_not_dropped` — a judge returning no parseable
    anchor for a dim is recorded `judge-error` for that cell, never silently dropped.
  Run; confirm failures.

- [ ] **Step 2 — implement `judge.py`** (`judge_packet`, `parse_anchors`; `dispatch_judges` is the
  live seam — thread the panel through but keep the parse/packet hermetic-testable), **`score.py`**
  (`cell` median + R1 mechanical cap, no R6 cap, all-errored ⇒ UNKNOWN), and **`report.py`**
  (`render_seat_report` with the verbatim construct-validity block — tree-only-access narrowing,
  reader-convertibility residual, AB-D1 reconstruction caveat, same-family non-certifying flag — and
  the hardcoded claim grammar; `guard` + `assert_no_rank_fields`). Output namespace is the bench's
  own (`runs/` + ARB Memory `author-bench/*`), mechanically separate from Instrument 1.

- [ ] **Step 3 — go green.** `test_score_report.py` passes; the rank-field deny-proof reddens with
  the wall removed.

**Done criterion:** the coded wall refuses every rank/cross-seat-sort field (deny-proof red-green);
headline is prose counts not rates; R1 is mechanically capped and R6 is not; an all-errored cell is
UNKNOWN(judge_error); a missing anchor is judge-error, never dropped; the construct-validity block
and claim grammar render verbatim.

**Commit shape:**
```
feat(authorbench): D4 cell computation + coded report wall

score.cell = median judge anchor (0/1/2 → NOT-MET/PARTIAL/MET) with R1 capped by the
mechanical citation result (R6 has no mech cap — judged, OQ1=no headline cap); an
all-judges-errored cell is UNKNOWN(judge_error). report.render_seat_report emits
prose counts (never rates), the verbatim construct-validity block + claim grammar,
and refuses rank/cross-seat-sort fields via assert_no_rank_fields. Deny-proof:
removing the wall lets a rank field emit. SPEC §2.6/§4 V4 (walls #1,#2).
```

---

## Task 8 — `store.py`: D6 append-only NDJSON run store + V5 reproducibility + ARB Memory keying

**Goal (SPEC §2.7, §3.7, §4 V5):** append-only NDJSON per run is authoritative; every event passes
`report.guard` before write; `rederive_mechanical` re-runs the mechanical phase from the stored
bundle + raw drafts and reproduces the `mechanical_*` events **byte-for-byte (comparing event PAYLOADS with the non-deterministic `{ts, run_id}` envelope fields excluded — r1 cold-Opus P2)**; a judged re-run
lands under a NEW judging-run id and never overwrites (append-only, no silent drop). R6 outcomes are
JUDGED and **excluded** from the byte-for-byte set.

**Files:**
- New: `tools/authorbench/authorbench/store.py`.
- New: `tools/authorbench/tests/test_reproducibility.py` (V5).

**Interfaces (pinned — SPEC §1, §2.7, §3.7):**
```python
Recorder(run_dir)                          # .write(event) guards then appends to events.ndjson
write(event)                               # every event carries {event, ts, run_id}; guard-first
memory_key(...)                            # author-bench/<author-seat>/<author-model-version>/
                                           #   <corpus-version>/<run-id>
rederive_mechanical(run_dir) -> dict       # re-runs mechanical phase; MUST reproduce mechanical_*
                                           #   records byte-for-byte (V5)
```
The byte-for-byte set = R1 citation checks, counts, staging records only. `factkey_r6_context` is
INPUT (deterministic), but avoided/carried is JUDGED and NOT in the byte-for-byte set (SPEC §2.7).

- [ ] **Step 1 — write the failing V5 tests first** (`tests/test_reproducibility.py`, hermetic):
  - `test_mechanical_rederives_byte_for_byte` (DENY-PROOF) — from a stored run bundle,
    `rederive_mechanical` reproduces the `mechanical_*` NDJSON records byte-for-byte. Red condition:
    **perturb a mechanical checker input** (or drop a stored field) ⇒ the re-derivation diverges ⇒
    test **red** (the mechanical phase is not reproducible).
  - `test_judged_rerun_new_id_original_untouched` — a judged re-run lands under a new judging-run id
    and the original run's files are unmodified (append-only, no overwrite).
  - `test_write_guards_every_event` — an event carrying a denylisted (rank) key raises `WallBreach`
    at `Recorder.write` (routes through `report.guard`), never reaching the NDJSON.
  Run; confirm failures.

- [ ] **Step 2 — implement `store.py`** (`Recorder.write` guard-first append, `memory_key`,
  `rederive_mechanical` re-running Task-4 `check_r1`/`count_r4` + Task-3 staging records from the
  stored bundle + raw drafts). The ARB Memory artefact is a pointer-carrying summary; the NDJSON +
  report files are authoritative.

- [ ] **Step 3 — go green.** `test_reproducibility.py` passes; the byte-for-byte deny-proof reddens
  under a perturbed mechanical input.

**Done criterion:** mechanical records re-derive byte-for-byte (deny-proof red-green); a judged
re-run is a new id leaving the original untouched; every event is guarded before write; R6
avoided/carried is excluded from the reproducible set.

**Commit shape:**
```
feat(authorbench): D6 append-only run store + V5 byte-for-byte re-derivation

store.Recorder.write guards every event (report.guard) then appends to events.ndjson;
rederive_mechanical re-runs the mechanical phase from the stored bundle+drafts and
reproduces the mechanical_* records byte-for-byte (R6 judged outcomes excluded). A
judged re-run mints a new judging-run id, original untouched. memory_key =
author-bench/<seat>/<model>/<cv>/<run-id>. Deny-proof: perturbing a checker input
breaks re-derivation. SPEC §2.7/§4 V5 (walls #4).
```

---

## Task 9 — `run.py` + `cli.py` + `scripts/authorbench`: D2/D5 orchestration + CLI surface

**Goal (SPEC §2.9, §3.2, §3.4, §3.8):** wire the modules into `author_turn` / `judge_phase` /
`run_bench` and the CLI. The author turn: burn-check → build the author jail profile → canary →
dispatch via `confined-authorbench.sh author` (no ARB Memory, no bridge inbox, no network beyond the
model API) → store raw draft. The judge phase scores candidate + FROZEN baseline fresh in the same
sitting, independent-phase hygiene, judge-error never dropped.

**Files:**
- New: `tools/authorbench/authorbench/run.py`, `…/cli.py`.
- New: `scripts/authorbench` (repo-root, `exec python3 -m authorbench.cli "$@"`, `chmod +x`).
- New: `tools/authorbench/tests/test_cli.py` (argv-routing, hermetic).

**Interfaces (pinned — SPEC §1, §2.9, §3.2):**
```python
# run.py
author_turn(...)        # ledger.check_burn → jail.author_profile → canary → confined dispatch →
                        #   author_draft stored (bare-API + Anthropic-family authors OUT of v1)
judge_phase(...)        # candidate + frozen baseline judged fresh; role profiles; parse_anchors;
                        #   judge-error recorded, never dropped
run_bench(candidate, corpus, *, baseline) -> RunResult   # freeze-check→author→judge→score→report
# cli.py
main(argv)
# subparsers: freeze | factkey-validate | author | judge | score | report | run | ledger
```
CLI surface (SPEC §2.9) — pinned flags:
- `freeze <src-brief> --stage <design|spec|plan> --corpus-version v1.0 [--artefact-commit <sha> |
  --base-sha <sha>]` — `--artefact-commit` names the commit that added the historical artifact
  (`base_sha` = its first parent); `--base-sha` gives it explicitly (the round-1 cold-Opus P2 CLI
  gap — both forms must exist).
- `factkey-validate --brief <id> [--corpus-version v1.0]`
- `author --candidate <seat> --brief <id> [--corpus-version v1.0] [--override <reason>]`
- `judge --run <run-id> [--panel codex,pi-glm,m3]` — **default panel has M3** while agy is
  spike-locked; `--panel codex,agy,pi-glm` is accepted ONLY when the agy auth spike has landed. The
  CLI must **not** default-select a known-conditional judge (SPEC §5, r2 codex P1). PINNED
  target-ids: `codex→codex-bridge-dev`, `pi-glm→pi-sdk-bridge-dev-glm`,
  `m3→pi-sdk-bridge-dev-minimax-m3`, `agy→agy-bridge-dev` (when unlocked).
- `score --run <run-id>` · `report --run <run-id>` · `run --candidate <seat> --corpus-version v1.0
  [--panel …] [--baseline-seat <seat>]` · `ledger --run <run-id>`.
When an Anthropic seat is among the authors, any Anthropic judge is admissible but **flagged
non-certifying** in report metadata.

- [ ] **Step 1 — write the failing tests first** (`tests/test_cli.py`, hermetic — argv routing only,
  every dispatch/subprocess stubbed, **no docker**):
  - `test_freeze_accepts_artefact_commit_and_base_sha` — both `--artefact-commit` and `--base-sha`
    forms route to `bundle.freeze` with the resolved `base_sha` (deny-proof for the round-1 P2 gap:
    dropping the `--artefact-commit` branch ⇒ a verbatim brief cannot be frozen ⇒ red).
  - `test_default_judge_panel_excludes_agy_while_spike_locked` — `judge` with no `--panel` selects
    `codex,pi-glm,m3` and **never** `agy`; passing `--panel codex,agy,pi-glm` before the spike lands
    errors loudly (do not select a conditional judge).
  - `test_author_refuses_burned_lineage_without_override` — the CLI author path calls
    `ledger.check_burn` and refuses a burned lineage without `--override` (wires Task 6 into the CLI).
  - `test_run_pipeline_calls_each_stage_in_order` — `run` calls freeze-check → author → judge →
    score → report (stubbed) in order for one candidate over the corpus.
  Run; confirm failures.

- [ ] **Step 2 — implement `run.py`** (`author_turn` per SPEC §3.2 steps 1–5: burn-check, build
  profile via `git archive base_sha | tar`, `assert_manifest`, canary, confined dispatch, store raw
  draft outside any shared checkout until all finish; `judge_phase` per §3.4: candidate + frozen
  baseline, role profiles, independent-phase hygiene, `parse_anchors`, judge-error recorded;
  `run_bench` chaining freeze-check→author→judge→score→report). Bare-API authors (pi-GLM-class) and
  Anthropic-family authors are **out of v1 scope** (named — the former has no filesystem, the latter
  needs the claude-container prerequisite).

- [ ] **Step 3 — implement `cli.py`** (the eight subparsers + both `freeze` base_sha forms + the
  agy-spike-locked default panel guard + the Anthropic-judge non-certifying flag) and the
  `scripts/authorbench` entrypoint (`chmod +x`).

- [ ] **Step 4 — go green.** `test_cli.py` passes; full `tools/authorbench` suite green.

**Done criterion:** both `freeze` base_sha forms route (deny-proof for the P2 gap); the default
judge panel excludes agy while spike-locked and rejects a conditional judge; the CLI author path
refuses a burned lineage without `--override`; `run` chains the five stages in order; the trigger is
manual only (no watcher).

**Commit shape:**
```
feat(authorbench): D2/D5 orchestration + CLI surface + scripts/authorbench

run.author_turn (burn-check→jail profile→canary→confined dispatch→raw draft) /
judge_phase (candidate+frozen baseline, role profiles, judge-error recorded) /
run_bench chain the pipeline. cli exposes freeze (--artefact-commit|--base-sha, the
round-1 P2 gap)/factkey-validate/author/judge/score/report/run/ledger; the default
judge panel excludes agy while its auth spike is locked (M3 stands in) and refuses a
conditional judge. Bare-API + Anthropic authors are v1 out-of-scope (named). SPEC §2.9/§3.
```

---

## Task 10 — Corpus v1.0 freeze: AB-D1 (committed) + AB-S1 / AB-P1 bundles (data)

**Goal (SPEC §2.1, §5):** freeze the v1.0 corpus so the bench has real briefs to author against.
AB-D1 is committed with a fact-key that **validates at `9de2850`** (round-1 GLM+cold-Opus P2: the
earlier exemplar cited nonexistent files and failed its own V9 — every `facts[].file:line` and every
`traps[].file/symbol` MUST be REAL at `base_sha`). AB-S1/AB-P1 are the verbatim corpus anchors.

**Files (committed frozen bundles — data, produced via the Task-1/9 machinery):**
- New: `tools/authorbench/corpus/v1.0/AB-D1/{bundle.yaml, brief.md, steer/*, fact_key.yaml}`.
- New: `tools/authorbench/corpus/v1.0/AB-S1/{…}` and `…/AB-P1/{…}`.

**Corpus table (pinned — SPEC §2.1):**

| id | stage | source | base_sha | provenance |
|---|---|---|---|---|
| AB-D1 | design | arb-watch-history design brief (no committed original) | `9de2850` | `reconstructed(v1)` |
| AB-S1 | spec | `2026-07-03-arb-visibility-web-spec-authoring-brief.md` | parent of the committed spec (resolved at freeze) | `verbatim` |
| AB-P1 | plan | `2026-07-03-arb-visibility-web-controls-plan-authoring-brief.md` | parent of the committed plan (resolved at freeze) | `verbatim` |

- [ ] **Step 1 — author the AB-D1 fact-key against `9de2850`.** For each hinge fact and known trap,
  verify the cited `file:line`/`pattern` and `file`/`symbol` are REAL at `9de2850` (e.g. the SPEC's
  worked examples: `src/agent_redis_bridge/eval_tee.py` `EVAL_ALLOWLIST`;
  `src/agent_redis_bridge/bridge.py:_emit_vote` at `9de2850:2072`; `historical_catch` → a prior
  codex design-panel review of the arb-watch-history design). Write the
  author-visible `brief.md` (reconstructed(v1)) + byte-identical steer blocks
  (`give-the-reason`, `scope-restraint`, `grounded-claims`, `no-reasoning-extraction`).
- [ ] **Step 2 — freeze + validate AB-D1.** `scripts/authorbench freeze <ab-d1-src> --stage design
  --corpus-version v1.0 --base-sha 9de2850`; then `scripts/authorbench factkey-validate --brief
  AB-D1` → **green** (V9). Commit `corpus/v1.0/AB-D1/`. **Then add
  `tests/test_frozen_corpus_validates.py::test_every_committed_bundle_passes_v9` — iterates every
  bundle under `corpus/v1.0/` and runs the V9 validator against its `base_sha` (hermetic: CI
  checks out full history, `ci.yml fetch-depth: 0` verified) — the CI arm of V9 the r1 panel
  found unassigned (cold-Opus+agy P1: Task 10 confirmed a test no task wrote); plus
  `test_verbatim_brief_base_sha_resolves_real_tree` — resolves AB-S1's committed artefact commit
  parent against the REAL repo history and asserts it matches the frozen `base_sha` (agy P1).**
- [ ] **Step 3 — freeze AB-S1 / AB-P1** from their committed authoring briefs, resolving `base_sha`
  = `git rev-parse <committed-artefact-commit>^` via `--artefact-commit`; author each fact-key
  against its `base_sha`; `factkey-validate` → green; commit. **OQ3 STOP condition, falsifiable (r1 codex P1: "too thin" was taste):** a fact-key is
  freezable iff it carries **≥3 `facts` entries each with `file:line` + `pattern` verified at
  `base_sha`, AND ≥1 `traps` entry with a `precondition` + a `historical_catch` citation into a
  committed panel/remediation record**. Below either threshold → STOP and escalate with the
  counts; at/above → freeze. Do not weaken V9; the corpus table is data and the candidate is
  swappable.
- [ ] **Step 4 — confirm the CI-over-corpus V9** (Task 2 / Task 11) runs green over the three frozen
  bundles.

**Done criterion:** three frozen v1.0 bundles committed; each `factkey-validate` green at its
`base_sha` (no nonexistent-file citation); `corpus/**/drafts/` gitignored; AB-D1 carries the
`reconstructed(v1)` caveat, AB-S1/AB-P1 are `verbatim`.

**Commit shape:**
```
feat(authorbench): freeze corpus v1.0 — AB-D1 (reconstructed) + AB-S1/AB-P1 (verbatim)

Three frozen brief bundles authored against real base_shas; every fact_key.yaml
validates via factkey-validate at its base_sha (round-1 P2: no nonexistent-file
citations). AB-D1 reconstructed(v1) @9de2850; AB-S1/AB-P1 verbatim, base_sha = parent
of the committed artefact. Drafts gitignored. SPEC §2.1/§5.
```

---

## Task 11 — V7 hermetic-suite gate + CHANGELOG

**Goal (SPEC §4 V7):** the hermetic subset runs in CI on fixture drafts with **zero live engines and
no docker**; the docker-requiring proofs are explicitly marked live and skipped-with-asserted-reason
(no vacuous green). Plus the repo CHANGELOG obligation.

**Files:**
- New: `tools/authorbench/tests/test_hermetic_suite.py` (V7).
- Modify: `CHANGELOG.md` (repo root) — one entry (what AND why).

- [ ] **Step 1 — write the V7 gate test.** `test_no_docker_no_engine_imports_in_hermetic_paths` —
  assert the hermetic suite (V1, V2 a/c/d + e-manifest, V3, V4, V5, V6, V9) never shells to
  `docker` or an engine binary (scan the hermetic test modules / assert the stubs are in place);
  `test_live_proofs_skip_with_asserted_reason` — V2b, V2e-canary declare a skip reason string when
  docker is absent (not vacuous green).
- [ ] **Step 2 — full-suite gate.** `.venv/bin/python3 -m pytest tools/authorbench -q` → all
  hermetic tests green, live tests skip-with-reason. This is the required CI gate.
- [ ] **Step 3 — CHANGELOG entry** (what AND why) covering the author-bench v1 machinery + corpus
  v1.0 freeze, per repo discipline. Merge into `CHANGELOG.md` in place — do not replace surrounding
  content.

**Done criterion:** the hermetic subset never shells docker/an engine; the live proofs
skip-with-asserted-reason; `pytest tools/authorbench` is green; a CHANGELOG entry exists.

**Commit shape:**
```
test(authorbench): V7 hermetic-suite CI gate + CHANGELOG

test_hermetic_suite asserts the hermetic subset (V1,V2a/c/d/e-manifest,V3,V4,V5,V6,V9)
shells no docker/engine and the live proofs (V2b, V2e-canary, V8) skip-with-asserted-
reason (no vacuous green). CHANGELOG records the author-bench v1 build. SPEC §4 V7.
```

---

## Task 12 — V8 live gate runbook (MANUAL — NOT CI, orchestrator/human-run)

Needs **docker + real engines**; excluded from CI (SPEC §5, §7). Run after the code tasks merge, one
candidate at a time. This is a checklist for the integrating orchestrator, not a dispatched-worker
step. Per live-verification-catches-cli-glue, one real end-to-end run before v1 is called done.
Candidate + baseline = **jail-runnable engines (codex, or agy once its spike lands)**; the
Anthropic-author question waits on the claude-container (a NAMED PREREQUISITE, NOT built here).

- [ ] **L1 — prereq + freeze/validate.** Image `arb-eval-seat:latest` built
  (`tools/eval/confinement/build.sh`); codex authed into the authorbench profile (RO auth, tmpfs
  HOME). `authorbench factkey-validate --brief AB-D1` → green (V9).
- [ ] **L2 — baseline (one-time).** Author the frozen baseline draft for AB-D1 with the current
  rotation-default model, cold+jailed, stored in the run bundle namespace.
- [ ] **L3 — author.** `authorbench author --candidate codex --brief AB-D1` → canary green (author
  profile), draft produced in the jailed export, `author_draft` stored.
- [ ] **L4 — blind.** Blinding scan green (or quarantine→redact→clean); normalized draft staged.
- [ ] **L5 — judge.** `authorbench judge --run <run-id> --panel codex,pi-glm,m3` (agy replaces m3
  ONLY when its auth spike has landed — the gate must not select a known-conditional judge, r2 codex
  P1) → each judge canary green (judge profile / bare-API packet), anchored scores with quotes
  returned, per-judge replies captured.
- [ ] **L6 — score + report.** `authorbench score` then `authorbench report` → per-seat reports
  render inside the coded wall; headline counts (not rates); construct-validity block present.
- [ ] **L7 — store + re-derive.** Run artefact stored (NDJSON + ARB Memory `author-bench/*` key);
  `store.rederive_mechanical` reproduces the mechanical records byte-for-byte.
- [ ] **L8 — negative control (guards must fire).** Plant an outcome token into the author mount ⇒
  author canary **Parks**; plant the fact-key into the author staging dir ⇒ prep **fails loud** (V2d
  live). **Both must go red** or the guards are hollow.
- [ ] **L9 — record cost.** Measured $ + wall-clock into design D6's envelope; a live-gate record
  filed (ARB Memory + a review-panel brief); CHANGELOG confirmed.

**Done criterion:** L1–L9 all green; the negative control (L8) fires both ways; a live-gate record
filed, per the live-verification discipline (untested CLI/subprocess glue is where bugs survive
static review).

---

## Final integration gate (orchestrator)

- `.venv/bin/python3 -m pytest tools/authorbench -q` → the full hermetic suite green (V1, V2
  a/c/d/e-manifest, V3, V4, V5, V6, V7, V9); live proofs (V2b, V2e-canary, V8) skip-with-asserted-
  reason. Every deny-proof demonstrated red-green both polarities (Task 1 subset+overwrite, Task 2
  V9, Task 3 quarantine, Task 4 R1, Task 5 V2d/V2e, Task 6 burn, Task 7 rank-wall, Task 8
  re-derivation).
- `git status` shows nothing committed under `tools/authorbench/runs/` or
  `tools/authorbench/corpus/**/drafts/`, and **no change under `tools/eval/arb_eval/*` or
  `src/agent_redis_bridge/`** (scope-guard proof).
- Three frozen v1.0 bundles committed under `corpus/v1.0/`, each `factkey-validate`-green.
- CHANGELOG entry present.

---

## Escalations

**None block the build.** The SPEC §8 items are specification choices resolving design
under-determination (not reopened decisions), carried faithfully here:

1. **Blinded draft handle (SPEC §8.1).** The per-run opaque `draft_handle` (handle→seat map stored
   outside every judge mount) keeps the author label out of the judge-visible event stream —
   additive, strengthens wall #3. Implemented in the store/judge event shapes (Tasks 7, 8).
2. **`base_sha` resolution for verbatim anchors (SPEC §8.2, design OQ3).** AB-S1/AB-P1 `base_sha` =
   parent of the committed artefact's commit, resolved at `freeze` via `--artefact-commit` (Task 9,
   used in Task 10). If a verbatim brief's fact-key proves too thin to freeze richly, Task 10 Step 3
   STOPS and escalates — the corpus table is data (swappable), so no decision is foreclosed.
3. **Design open forks left parameterized, not resolved (SPEC §8.3):** OQ1 (whether R6's JUDGED
   consensus caps the headline — default NO cap, since R6 has no mechanical result to cap with;
   Task 7); OQ3 (AB-S1/AB-P1 fact-key richness — corpus table is data; Task 10); OQ4
   (claude-container ordering — a prerequisite parameter, v1 gates on codex/agy; Parameters section
   + Task 12). Each default is a runnable value; changing it is a config/data change, not a
   redesign.

No contradiction between the SPEC and the certified design was found while translating it into these
tasks.

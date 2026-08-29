# Instrument 1 completion — SPEC (build-ready)

**Design:** `docs/superpowers/specs/2026-07-09-instr1-completion-design.md` (v2, panel-certified
`panel-capsuiteadesign-r2-20260709T010114Z-64c49e`, zero P0/P1). This SPEC translates that design
into named files, function seams, wire shapes, and a test map. It does not redesign. Where the
design left a human fork (§8) the SPEC parameterizes it with the design's default and names the
fork; genuine gaps are in **§ Escalations** (none block the build).

All paths are under `tools/eval/` unless noted. Line references are to the source as it stands
today (verified 2026-07-09); they name the seam, not a frozen offset.

---

## 0. Walls — restated verbatim, binding on every increment

These are the design's hard walls (§1.1); a change that violates one is a build error, not a
review nit:

- **No ranking, no seat-drop/trust/quorum verdict, no composite seat score.** Output is an
  unordered `PASS / FAIL / UNKNOWN`-by-class grid only. The allowlist wall in
  `report.py` (`render_grid` / `assert_verdict_row`) and its secondary denylist (`guard`) stay.
- **Report-only.** Nothing downstream consumes a floor artifact to change trust, quorum, or seat
  assignment automatically. `publish` (§E) emits a payload for a HUMAN to store and read.
- **UNKNOWN-not-FAIL on infra.** An infra failure (dispatch timeout, canary/review error) NEVER
  scores as a capability miss and can NEVER render FAIL. It renders UNKNOWN with a named
  `infra_incomplete` line (§A1). This is the round-1 P1-α wall and its deny-proof (H1) is
  mandatory.
- The `report.py` disclaimer text is emitted verbatim wherever a grid is rendered or published;
  the reader-convertibility residual stays NAMED, never silently dropped.

---

## 1. Modules & files

New modules under `tools/eval/arb_eval/`:

| file | purpose | key public surface |
|---|---|---|
| `boundary.py` | A3 boundary oracle (tree-sitter / ctags / heuristic) | `enclosing_symbol(repo, file, line) -> SymbolResult` |
| `provenance.py` | A2 run-provenance capture | `collect(scenario, *, dispatcher, normalizer, oracle_by_language, gold_versions, image, harness_root) -> dict`; `strip_prov_fence(stdout, nonce) -> tuple[str, dict]`; `provenance_key(prov) -> str` |
| `gold.py` | C gold matcher-validation set | `export`, `rate`, `ingest`, `score` (module fns the CLI verbs call) + `load_summary(seat) -> GoldSummary | None` |
| `publish.py` | E change-event artifact | `build_artifact(run_dir, seat=None) -> dict` (imports `assert_gold_field_shape` FROM `report.py` — defined there beside the wall helpers, agy P2) |

Modified modules:

- `arb_eval/pipeline.py` — A1 (incomplete-repeat semantics), A2 wiring (provenance event),
  A3 wiring (matcher records tier), A4 (`format_conformance`), C gold-gate wiring in `run_floor`,
  D detail-file fields.
- `arb_eval/cli.py` — `run` unchanged flags plus new subparsers `gold` (with `export|rate|ingest|score`)
  and `publish`; `plan` gains the A3 oracle-tier warning.
- `arb_eval/schema.py` — A5 relative-repo resolution + subject validation; B load-time
  seed/control-proximity D3 check; `subject.languages` field.
- `arb_eval/report.py` — E: `assert_gold_field_shape` lives here beside the other wall helpers.

New scripts / fixtures (B):

- `confinement/confined-review.sh` — A1 canary-vs-review exit-code contract; A2 nonce-fenced
  engine-version emission.
- `fixtures/build_floor_real_secrets.sh`, `…_correctness.sh`, `…_authz.sh` — clone-at-build.
- `fixtures/src/real-secrets/*.patch`, `real-correctness/*.patch`, `real-authz/*.patch` — tracked
  seed patches.
- `fixtures/gen_floor_real_secrets_scenario.py`, `…_correctness_…`, `…_authz_…` — scenario emitters
  + the anti-oversplit / marker-freedom lint.
- `gold/<seat>/pairs.ndjson`, `gold/<seat>/summary.json` (gitignored working data; `gold/.gitignore`).

New / extended test files:

- `tests/test_pipeline.py` — A1, A4, A5, gold-gate, D (extend).
- `tests/test_provenance.py` — A2 / H2.
- `tests/test_boundary.py` — A3 / H3.
- `tests/test_gold.py` — C / H5.
- `tests/test_publish.py` — E / H6.
- `tests/test_real_corpus_lint.py` — B / H7.

Docs (§11 non-goal carve-out, one-line obligation): update `tools/eval/README.md` status section
(kill the stale "13 tests" / "`run` → NotImplementedError"), `confinement/README.md` REMAINING
lines, and the `ContainerDispatcher` docstring's stale "agy needs adding". Doc-only; no behavior.

---

## 2. Increment A — pipeline completion

### A1. ContainerDispatcher timeout + incomplete-repeat semantics (round-1 P1-α)

**`ContainerDispatcher.dispatch`** (`pipeline.py:162-175`) gains a timeout and a classified error:

- `subprocess.run([...], timeout=_dispatch_timeout(), ...)` where
  `_dispatch_timeout()` reads env `ARB_EVAL_DISPATCH_TIMEOUT` (int seconds, **default 900**), same
  as the bridge path.
- **`DispatchError` grows a `kind` attribute** (`"timeout" | "canary" | "review"`), default
  `"review"`. Signature: `DispatchError(message, *, kind: str = "review")`; `self.kind = kind`.
- Classification in `dispatch`:
  - `subprocess.TimeoutExpired` → `raise DispatchError(f"confined dispatch {tid} timed out after {t}s", kind="timeout")`.
  - `proc.returncode == 43` → `raise DispatchError(..., kind="canary")` — **exit code 43 is the
    canary-breach sentinel** (43 chosen over small codes an engine might exit with; and the
    review stage REMAPS its own rc 43→1 so an engine coincidentally exiting 43 can never
    masquerade as a canary breach — aspec round-1 cold-Opus P2; see script contract below).
  - any other non-zero → `raise DispatchError(..., kind="review")` (message keeps the truncated
    stderr/stdout tail as today).

**`confined-review.sh` exit-code contract (new):** the script runs the canary first
(`canary.sh`); on canary failure it exits **43** (a boundary breach, integrity-fatal), distinct
from a review-stage non-zero exit. Concretely: `"$HERE/canary.sh" "$FIXTURE" "$SCENARIO" || exit 43`;
the review stage runs in an rc-captured block that remaps its own exit 43→1
(`rc=$?; [ "$rc" -eq 43 ] && exit 1; exit "$rc"`), so only the canary can produce 43. `set -euo
pipefail` stays for the review stage (its natural non-zero propagates as `kind=review`). No
timeout lives in the script — the Python `subprocess.run(timeout=…)` owns the wall clock (A1).

**`run_floor` scoring change (REPLACES the built detection-miss route, `pipeline.py:687-729`):**

On `DispatchError` during a `(seat, repeat)` dispatch:

- `kind == "canary"` → `raise Parked(...)` immediately: a boundary breach fails the whole run
  (unchanged integrity semantics; NOT a flake).
- `kind in {"timeout","review"}` → the repeat is **incomplete**:
  1. write `dispatch_error` event (shape below) with `kind`, `repeat`, `seat`;
  2. write `incomplete_repeat` event `{seat, repeat, kind, classes: <all scenario classes>}` — the
     authoritative signal the counter and verdict step read;
  3. **do NOT emit any `matcher_decision` rows for that repeat** (the old code wrote
     detection-miss for every seed and clean for every control — that is exactly the fake-verdict
     route; it is deleted). Skipping the rows is how the repeat "contributes nothing to caught or
     noise".

**`_counts_from_events` (`pipeline.py:486-532`)** is unchanged in its pooling math (it already
only sees `matcher_decision` rows, and incomplete repeats emit none). Add a sibling helper:

```
def _incomplete_seats(path: Path) -> dict[str, list[tuple[str, int]]]:
    # seat -> [(kind, repeat), …] from incomplete_repeat events
```

**Verdict step (`pipeline.py:736-769`):** after `classify(...)`, if the seat appears in
`_incomplete_seats`, **force every class verdict for that seat to `UNKNOWN`** (override the
computed verdict) and emit report lines:
`infra_incomplete(<seat>, kind=<kind>, repeat=<n>)` — one per incomplete `(kind, repeat)`. Other
seats and classes score normally; the run completes. Runbook line: "fix the infra, re-run the
seat." The forced-UNKNOWN override is the exclusion H1 deny-proofs (delete it → the class scores
the completed repeats and can render PASS/FAIL → H1 red).

**NDJSON `dispatch_error` event (field names pinned):**
```
{"event":"dispatch_error","ts":<float>,"run_id":<str>,"seat":<str>,"repeat":<int>,
 "kind":"timeout"|"review","error":<str>,"task":<task-dict>}
```
**NDJSON `incomplete_repeat` event:**
```
{"event":"incomplete_repeat","ts":<float>,"run_id":<str>,"seat":<str>,"repeat":<int>,
 "kind":"timeout"|"review","classes":[<class>,…]}
```
All payloads pass `report.guard` before write (as every event already does at `_Recorder.write`).

### A2. Run provenance capture (round-1 P1-β / P1-γ; feeds §E keying)

**`provenance.py`** collects a provenance dict; `run_floor` writes it as a `provenance` NDJSON
event at run start and a `detail["provenance"]` block, then merges per-seat engine data at run end.

`collect(scenario, *, dispatcher, normalizer, oracle_by_language, gold_versions, image, harness_root)`
returns (all string/hash values — names, never seat values; passes `report.guard`):

```
{
  "model": {                       # per requested seat; filled from scenario/CLI, not engine default
     "<seat>": {"model_requested": <str>, "confined_command": <str>,
                "model_reported": <str|null>, "model_source": "pinned-flag"|"readback"|"cli-version-only"}
  },
  "engine_versions": {},           # per-seat CLI versions, filled at run end from the fenced marker
  "harness_version": {"describe": <git-describe --always --dirty>, "package_sha256": <hex>},
  "image_digest": <str|null>,      # docker image inspect … -f '{{.Id}}'  (confined runs)
  "corpus_version": {"builder_sha": <hex>, "scenario_sha256": <hex>,
                     "repo_base": <sha>, "repo_head": <sha>},
  "normalizer": {"model": <str>, "endpoint": <str>},
  "matcher": {"window": <int>},
  "boundary_oracle": {"oracle_by_language": {<language>: "tree-sitter"|"ctags"|"heuristic"}, "coverage": [<language>,…]},  # KEY NAMING IS LOAD-BEARING: report.guard rejects any key containing the substring "tier" (round-1 codex P0, verified empirically: {"tiers":…} raises WallBreach). All guard-traversed surfaces use "oracle_by_language"; H2 adds test_provenance_dict_passes_report_guard_verbatim.
  "gold_versions": {"<seat>": <hex|"GOLD_UNADJUDICATED">},  # PER-SEAT map (SPEC v1.3 amendment,
                     # plan-panel r2 codex: gold sets are per-seat gold/<seat>/pairs.ndjson and a
                     # multi-seat run cannot carry one scalar; §6 publish reads its --seat entry.
                     # Amended via recert, never plan-side.
  "run_id": <str>
}
```

Field mechanics (each the fix it is named for):

- **`model_*` (P1-β — CLI version ≠ model identity), WIRED end-to-end (aspec round-1 cold-Opus
  P1: policy without wiring lets an implementer fill `model_requested` while the container runs
  the engine default):** `ContainerDispatcher.dispatch` passes `SeatSpec.model` to
  `confined-review.sh` as env **`ARB_EVAL_MODEL`**; the script's engine invocation lines consume
  it (`codex exec --model "$ARB_EVAL_MODEL" …`; agy's model flag likewise) whenever it is
  non-empty. `model_requested` is copied from the SAME `SeatSpec.model` field the argv is built
  from — divergence is impossible by construction — and `confined_command` = the full argv the
  container ran (captured from the script via the nonce fence). If `SeatSpec.model` is set but
  the seat's engine line has no model slot, the script exits 2 (config error) rather than
  silently running the default. `model_reported` = in-jail readback where the engine supports
  it; `model_source` records which. A `cli-version-only` seat (no flag, no readback) emits a
  LOUD warning line into the report and sets `model_unverified: true` on the §E artifact. H2
  deny-proof extension: with `SeatSpec.model` set, the recorded `confined_command` MUST contain
  the flag value, else the test is red.
- **`harness_version` (P1-γ):** `git -C tools/eval describe --always --dirty` PLUS
  `package_sha256` = sha256 over the sorted `arb_eval/*.py` file bytes. A `-dirty` harness may RUN
  (dev iteration); `publish` REFUSES it (§E).
- **`engine_versions` (nonce-fenced, GLM P2-5):** per run, mint `nonce = uuid4().hex`; pass it to
  `confined-review.sh` as env `ARB_PROV_NONCE`. Inside the jail, after the review, the script emits
  one line `ARB_PROV_<nonce>{"<engine>":"<version>"}</ARB_PROV_<nonce>>`.
  `strip_prov_fence(stdout, nonce)` parses ONLY the exactly-nonced fence, returns
  `(review_text_without_fence, engine_versions_dict)`. `ContainerDispatcher.dispatch` calls it and
  stores `self.last_engine_versions`; `run_floor` reads it on that seat's first successful confined
  dispatch and emits a `provenance_engine` event `{seat, engine_versions, model_reported,
  model_source}`, merged into `detail["provenance"]` at run end. A reply that echoes a marker with
  any other nonce cannot corrupt the parse (H2 deny-proof).
- `image_digest`, `corpus_version`, `normalizer`, `matcher`, `boundary_oracle` as above;
  `corpus_version.builder_sha` = sha256 of the fixture builder script; `scenario_sha256` = sha256
  of the scenario JSON bytes.

**`provenance_key(prov) -> str`** = sha256 over the stable tuple
`(model_requested per seat, harness_version.describe, corpus_version, image_digest)`. Changing any
model input changes the key; a config-only bump (same model, different command) changes
`confined_command` in the captured evidence (H2). Hermetic tests stub every subprocess call
(`git`, `docker`, engine invocations).

**NDJSON `provenance` event:** `{"event":"provenance", …, "provenance": <the dict above>}`.
**`provenance_engine` event:** `{"event":"provenance_engine","seat":<str>,"engine_versions":{…},
"model_reported":<str|null>,"model_source":<str>}`.

### A3. Boundary oracle — tree-sitter / ctags / heuristic (schema §3)

**`boundary.py`**:

```
@dataclass(frozen=True)
class SymbolResult:
    symbol: str | None
    tier: str            # "tree-sitter" | "ctags" | "heuristic"

def enclosing_symbol(repo: Path | None, file: str | None, line: int | None) -> SymbolResult:
    ...
```

Precedence, **each tier fail-closed to the next with the tier RECORDED**:

1. **tree-sitter** where a grammar for the file's language exists (optional imports `tree_sitter`
   + `tree_sitter_python`; import failure or absent grammar → fall through, do NOT crash).
2. **ctags** — probe the `ctags` binary at runtime (`shutil.which("ctags")`); absent → fall
   through.
3. **heuristic** — the current `_enclosing_function` walker (moved into `boundary.py` unchanged, or
   imported by it). Always available.

`match_finding` (`pipeline.py:395-421`) calls `enclosing_symbol` in place of the two
`_enclosing_function` calls, and the emitted `matcher_decision` event gains a
**`boundary_oracle` field** = the tier that produced the enclosing symbol used for the decision
(when both seed and finding resolve, record the tier of the finding's resolution; a mixed-tier
match records the weaker/lower tier so a silent downgrade is visible). A silent tier downgrade must
never be invisible — the field is mandatory on every `matcher_decision` row whose basis is
`function`.

**Scenario field `subject.languages`** (list of language ids). `plan` (`cli.py:_plan`) warns
`WARNING: boundary oracle for <language> is 'heuristic' (methods/nested/decorated defs may
mis-resolve)` when the best available tier for a fixture language is heuristic. `provenance`
records the per-language tier map (A2 `boundary_oracle.oracle_by_language`).

Dependency posture: `tree_sitter`, `tree_sitter_python` are **optional extras** (not hard deps);
`ctags` is a runtime-probed binary. CI runs heuristic-tier tests unconditionally; ctags/tree-sitter
tests sit behind availability skips that **assert the skip reason** (no vacuous green — H3).

### A4. agy output-format wrapper — report the format/detection split (never re-prompt)

`segment_reply` (`pipeline.py:327-348`) is unchanged (it already strips bullets). Add:

```
def format_conformance(candidates: list[str]) -> tuple[float, int, int]:
    # returns (fraction, conforming_lines, candidate_lines);
    # a line conforms iff it matches  ^\s*<class>\s*\|\s*<file>:<line>\s*\|\s*<desc>$
    # with <class> in TAXONOMY.  Empty candidate list -> (1.0, 0, 0).
```

`run_floor` enriches the existing `segmented` event:
```
{"event":"segmented", …, "findings":[…], "format_conformance": <float>,
 "conforming_lines": <int>, "candidate_lines": <int>}
```
`detail.json` surfaces a per-seat mean `format_conformance` under `details["oracle"][seat]`
(D-adjacent, detail-only). **No re-prompt/retry loop** — a retry changes what is measured (one
pass, as scored). The detection-vs-format split is REPORTED here and QUANTIFIED by the gold set
(§C); it is never silently absorbed into detection-miss.

### A5. Scenario portability — relative subject.repo

- **`schema.load(path)`** resolves a **relative** `subject.repo` against `Path(path).parent` and
  stores the absolute path back into `subject["repo"]` (keeping the original in
  `subject["repo_declared"]`). An absolute `subject.repo` is used as-is. `plan`/`list` do NOT
  require the repo to exist (they stay runnable offline).
- **`validate_subject(scenario)`** (new, called by **`cli._run` ONLY** — NOT inside `run_floor` (round-1 agy P1: existing unit tests drive `run_floor` directly with mock SHAs like "base-sha" and must stay green; the CLI path, which every real invocation uses, always validates)):
  the repo dir must exist and be a git checkout, and `subject.base` / `subject.head` must resolve
  (`git -C <repo> rev-parse --verify <sha>^{commit}`). Any failure → `raise
  ScenarioError("subject repo/SHA unresolved: …")` (clean one-line, no traceback — `cli._run`
  wraps the NEW `validate_subject` call in its own `except ScenarioError → _die` (the existing
  mapping at `cli.py:44` covers scenario LOAD only — aspec round-1 cold-Opus P2).
- **Generators** (`gen_floor_*_scenario.py`) emit `subject.repo` **relative to the scenario file's
  directory** (e.g. `../fixtures/repos/floor-secrets-full`). Regenerate the committed scenarios;
  `scenarios/floor-secrets-full.json`'s absolute cross-clone path
  (`/Users/<user>/AgentRedisBridge/.claude/worktrees/p3-corpus/…`) is replaced by a relative path.

### A6. Bridge mechanics scope (no new surface)

Documentation-only, folded into the README/docstring obligation (§1). The scored path
(`ContainerDispatcher`) is a local subprocess: `--run-id`/notify-inbox/`--worktree` do NOT apply to
it; the NDJSON `run_id` (`pipeline.py:612`) is the label of record and the §E runbook quotes it in
any ARB Memory artifact. `BridgeDispatcher` remains integrity-UNGUARANTEED-diagnostic only and
already carries the SKILL.md rails. This increment adds no bridge surface.

---

## 3. Increment B — real-codebase fixture bases (eval-p3 "next")

**Base (a §8 fork; the SHA is a REQUIRED pre-build human input — round-1 codex P1: no pinned
SHA exists anywhere in the record, so the SPEC must not claim a runnable default):** proposed
repo `nsidnev/fastapi-realworld-example-app` (MIT, FastAPI) via `ARB_EVAL_REAL_BASE_URL`;
`ARB_EVAL_REAL_BASE_SHA` has **NO default** — the builder REFUSES to run without it, and Mark
pins it at corpus sign-off (runbook step 0). Clone-at-build is the default; vendoring is the
human's call (§8 fork 1).

**`fixtures/build_floor_real_<class>.sh`** (one per class: `secrets`, `correctness`, `authz`):

1. Clone the pinned upstream SHA into `fixtures/repos/floor-real-<class>` (gitignored, like the
   synthetic repos), attribution + license note in the builder header, **nothing vendored into
   git**.
2. **Re-init history to a single neutral base commit** (squash): remove upstream history (both a
   pipeline-hygiene step per `eval-seat-confinement.md:59-60` and an answer-key-adjacent-surface
   removal that keeps `git cat-file --batch-all-objects` cheap for the pre-flight).
3. Apply the seeds as **one head commit** from tracked patch files
   `fixtures/src/real-<class>/*.patch` via a generator (mirroring the existing builder+generator
   pattern) — **5 seeds/class, each a distinct MECHANISM**, each tagged `cluster: <mechanism>`.
   Deterministic SHAs via the pinned `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE`
   (`build_floor_secrets_full.sh:24`).
4. **Controls harvested from existing clean upstream code** (locations recorded, code untouched),
   **≥19/class where the base genuinely supplies them**, each tagged with its why-clean `cluster`.
   The generator computes effective (distinct-cluster) counts; the scenario `description` records
   **nominal vs effective**; `plan` already warns when effective < T (`cli.py:73-80`). If a base
   yields only e.g. 15 effective clusters, the run reports small-n honestly — never pad with
   correlated loci (reject-C stands).

**Cluster adjudication + anti-oversplit lint (GLM P2-3 + cold-Opus P2):** harvested-control cluster
tags get the same span-judgment the P-3 panel applied to synthetic controls; the human adjudicates
distinctness at corpus sign-off; **each cluster carries a recorded `why_clean` rationale string**.
The generator lint enforces: **effective cluster count may never exceed the number of distinct
`why_clean` rationales** — a cluster whose rationale duplicates a neighbor's merges into it. This
kills over-granular tags silently re-inflating effective-N back to nominal.

**Boundary guard (uses A3):** every seed and control in its own enclosing function where feasible;
where a real-app control shares a function with unrelated code, A3's oracle is the guard. Extend the
load-time D3 checks (`schema.py:177-206`): **a seed and a control of the same class in the same file
within `matcher_window` (10) lines → `ScenarioError`** (declared-line proximity check, no repo
needed at load; the generator does the richer same-enclosing-function check at build using
`boundary.enclosing_symbol`). This is H7's second red condition.

**Marker-freedom (GLM P2-4):** seed/control IDs live ONLY in the scenario (outside the repo); the
per-run pre-flight (`_assert_no_answer_key_in_fixture`, `pipeline.py:535-574`) + the canary enforce
it. **Patch files must not embed IDs in code comments**, AND the generator lint greps **commit
messages, tag names, and branch names** for any seed/control ID (because
`git cat-file --batch-all-objects` dumps commit messages, an ID there would Park the run — the lint
catches it at build time). Any hit → build fails (H7's first red condition).

**Class order:** secrets-in-logs + correctness first (proven-authorable), then authorization-scoping
on the same base (the class the real base exists for). If the chosen base cannot yield ≥5 distinct
authz seed mechanisms + enough distinct controls, that is a finding to SURFACE (in the scenario
description + `plan` warning), not to pad.

Confinement fit: the real fixture mounts read-only into the existing jail unchanged; the canary's
grep surface grows with app size but stays bounded (seconds).

---

## 4. Increment C — gold matcher-validation set (~135 pairs)

**Purpose (schema §3):** per-seat matcher recall/precision with a per-seat error band; a seat whose
recall lower-CI < `matcher_gate` (default **0.85**, from `power.PowerTarget`) has ITS floor rows
suppressed. Until adjudicated, every report carries `GOLD_UNADJUDICATED`.

**Storage:** `tools/eval/gold/<seat>/pairs.ndjson`, append-only. `gold_version` = sha256 of the
file (recorded in A2 provenance). Pair record (field names pinned):
```
{"pair_id": <str>, "seat": <str>, "raw_finding": <str>,
 "context": {"repo": <str>, "base": <sha>, "head": <sha>, "scenario_sha": <hex>},
 "pipeline": {"normalized": <obj>, "match_outcome": <str>, "basis": <str|null>},
 "adjudications": [{"rater": <str>, "verdict": "match"|"nonmatch"|"ambiguous", "ts": <float>}],
 "final_verdict": <str|null>, "notes": <str>}
```
The `pipeline` block is the SYSTEM's answer; exported adjudication packets OMIT it (blindness is
structural).

**Sizing.** `G_s = 45/seat` (derived; verified via `plan`). v1 seats **codex + agy → 90 pairs**;
the containerized-claude seat (§7) brings it to ~135. Pairs are drawn **per seat, stratified** (a
global set would average away seat-correlated normalizer bias): stratify each seat's 45 across
`detected / detection-miss / matcher-ambiguous`, all three match bases (symbol/function/window),
both classes, and no-op ("no issues") replies. **Sourced from real pipeline traversals** —
sampled from pilot-run NDJSON (`finding_emitted`/`segmented`/`normalized`/`matcher_decision` events
give the full raw→normalize→match trace); pilot runs on the relabeled synthetic fixtures are
admissible sources; real-base pilot findings added as they exist.

**CLI verbs (`arb-eval gold <sub>`), backed by `gold.py`:**

- **`gold export --seat <s> --out <packets.ndjson>`** — emits one packet per pair, **WITHOUT the
  `pipeline` block** and **WITH the cited code snippet ± context lines inlined** (round-1 P1-δ /
  GLM P2-1 — a bare-API rater cannot `git diff` a repo path, so the packet carries the ground-truth
  code itself; inlining code does not break blindness — code is evidence, not the pipeline's
  verdict). Packet: `{pair_id, seat, raw_finding, snippet, context:{repo,base,head}}`. A test
  asserts the exported packet contains NO outcome fields (`pipeline`, `match_outcome`, `basis`,
  `final_verdict`) — H5.
- **`gold rate --rater <model-alias-1|grok> --packets <file> --out <rater-file>`** — the automation round-1
  required. Runs a machine rater over exported packets: one bare-API call per packet with a fixed
  rubric prompt; parses the structured verdict line
  (`VERDICT: match|nonmatch|ambiguous`); **retries once on parse failure, then records
  `rater_error` (never a silent skip)**. Emits the rater-file `gold ingest` consumes. Manual
  adjudication (Mark) uses the same packet/rater-file format. Rater-file row:
  `{pair_id, rater, verdict|"rater_error", ts, raw_response?}`.
- **`gold ingest <rater-file>`** — merges each row's verdict into the matching pair's
  `adjudications` list; recomputes `final_verdict` (majority of the two primary raters; the
  tie-break rater breaks a split).
- **`gold score --seat <s>`** — computes per-seat matcher **precision/recall (Wilson, existing
  `stats.py:wilson`)** + **inter-rater agreement as raw %** + **pairwise Cohen's κ between the two
  PRIMARY raters** (κ is defined for exactly two raters; the tie-break rater participates in
  verdicts, NOT in κ — agy P2). κ is a pure-stdlib addition to `stats.py`:
  `def cohen_kappa(a: list[str], b: list[str]) -> float`. Writes `gold/<seat>/summary.json`:
  ```
  {"seat": <s>, "gold_version": <hex>, "n_pairs": <int>,
   "recall": {"k":…, "n":…, "lo":…, "hi":…}, "precision": {…},
   "kappa": <float>, "raw_agreement": <float>, "matcher_gate": 0.85}
  ```

**Adjudication protocol (parameterized; a §8 fork).** Default rater pool: **the human (Mark) +
model-alias-1 (tencent, pi bare-API)** as the two primary raters, **grok-acp as the third/tie-break** —
disjoint from quorum (codex, cold-Opus, agy, pi-GLM), **not Claude-family**, and **not the
normalizer (M3)** (self-validation). Named risk: model-alias-1/cursor-class raters have fabrication records
(non-certifying) — mitigated because adjudication is verifiable-by-inspection (each verdict cites
the fixture line) and every machine-rater disagreement routes to the human. The pool is settable
via `gold rate --rater` and the summary's rater set; the SPEC pins the default, not the choice.

**Gate wiring in `run_floor` (per-seat, not global):** load `gold/<seat>/summary.json` when
present:
- recall `lo` **≥ matcher_gate** → that seat's rows render with the per-seat error band in
  `detail.json` (D);
- recall `lo` **< matcher_gate** → that seat's floor rows are **SUPPRESSED** (removed from the
  grid; other seats intact) with a named report line
  `SUPPRESSED: <seat> matcher recall lo=<x> < gate=0.85 — rows withheld`;
- **absent** → today's `GOLD_UNADJUDICATED` warning; verdicts labeled not-trusted.

All three states hermetically tested; **deleting the gate wiring must turn the suppression test
red** (H5 deny-proof).

**Labor estimate (for the human to budget):** 90 pairs × ~1–2 min/pair/rater ≈ 1.5–3 h per rater
for v1; incremental thereafter (new pairs only on seat/harness change).

---

## 5. Increment D — matcher-miss vs detection-miss separation

Detail-file only; **the headline grid stays `PASS/FAIL/UNKNOWN`, untouched; no new verdict
category** (a MATCHER-SUSPECT verdict would be a new convertible surface — rejected). Per seat,
alongside `caught_k/caught_n`, `details["oracle"][seat][cls]` gains:
```
"matcher_ambiguous_n": <int>,          # count of matcher-ambiguous outcomes for this seat/class
"matcher_band": {"recall_lo":…, "recall_hi":…} | null   # from gold summary, null when GOLD_UNADJUDICATED
"format_conformance_mean": <float>     # A4
```
`matcher_ambiguous_n` is derived from the `matcher_decision` events (outcome == `matcher-ambiguous`)
during the existing count pass. The gold-derived band quantifies how much of a seat's miss column is
plausibly the matcher's fault.

---

## 6. Increment E — change-event packaging (`publish`, v1 = manual CLI)

**Trigger events:** a seat's engine/model version changes, a new seat is added, the harness changes,
or the corpus changes. No scheduler, no automation — a human decides to bench (§11 non-goal).

**CLI `arb-eval publish --run <run-id> [--seat <s>] [--output-root floor]`**, backed by
`publish.build_artifact(run_dir, seat)`. Seat resolution (agy P2: multi-seat runs were
under-determined): if the run's NDJSON contains exactly one seat, it is derived; if several,
`--seat` is REQUIRED and `publish` errors loudly naming the seats present — one artifact per
seat, never a merged one.
Assembles the change-event artifact from the run's `detail.json` + `events.ndjson` +
`provenance` block (so keys are captured facts, not hand-typed claims):

```
{
  "seat": <str>, "run_id": <str>,
  "model_version": <A2 model_requested / model_reported>,
  "model_unverified": <true when A2 model_source == "cli-version-only">,
  "harness_version": <A2 harness_version.describe>,
  "corpus_version": <A2 corpus_version>,
  "grid": { "<seat>": { "<class>": "PASS"|"FAIL"|"UNKNOWN" } },
  "claim_levels": { "<class>": "CLASS-LEVEL"|"INSTANCE-LEVEL" },
  "small_n_lines": [ <str>, … ], "orphan_lines": [ <str>, … ],
  "infra_incomplete_lines": [ <str>, … ],
  "events_sha256": <hex of events.ndjson>,
  "gold": { "gold_adjudicated": <bool>, "gold_version": <hex|"GOLD_UNADJUDICATED">,
            "suppressed_seats": [ <seat>, … ] },
  "disclaimer": <report.DISCLAIMER verbatim>
}
```

**Gold field is gate-outcome ONLY (round-1 P1-ε + round-2 build note):** the `gold` sub-object is a
**KEY-ALLOWLIST** — exactly and only `{gold_adjudicated, gold_version, suppressed_seats}`.
**Per-seat matcher recall/precision are EXCLUDED from the artifact** and live in `detail.json`
only. Because `report.guard`'s denylist cannot catch a `matcher_recall`-shaped synonym (its own
docstring concedes synonyms), the exclusion is enforced by an ALLOWLIST, not the denylist:

```
# report.py
_GOLD_ALLOWED = frozenset({"gold_adjudicated", "gold_version", "suppressed_seats"})
def assert_gold_field_shape(gold: dict) -> None:
    extra = set(gold) - _GOLD_ALLOWED
    if extra:
        raise WallBreach(f"gold artifact field carries non-allowlisted keys: {sorted(extra)}")
```

**`publish` refusals & wall checks, in order, before emit:**
1. **dirty harness refusal** — if `harness_version` ends in `-dirty` → refuse with a clean non-zero
   line (round-1 P1-γ: comparison keys must name committed code).
2. `assert_gold_field_shape(artifact["gold"])` — a `matcher_recall`-shaped key → `WallBreach`
   (H6's P1-ε deny-proof; the denylist alone cannot catch it).
3. `report.guard(artifact)` over the whole artifact — a `rank`-shaped field → `WallBreach`.
4. `assert_verdict_row(seat, artifact["grid"][seat])` **scoped to the grid rows ONLY** (GLM P2-2 —
   it would false-raise on `claim_levels` if run over the whole artifact).

On success `publish` **prints the exact payload** to stdout for storage. It does **NOT** write to
ARB Memory (the CLI stays dependency-free; the write is a deliberate human/orchestrator act —
consistent with "output informs a human"; auto-write is a rejected alternative).

**Runbook (one seat at a time, per the cost constraint):**
1. `confinement/build.sh` (rebuild image; canary smoke runs inside it);
2. `fixtures/build_floor_real_*.sh` (deterministic repo rebuild);
3. `python3 -m arb_eval.cli run --scenario scenarios/<s>.json --confined --seat <seat>
   --normalizer anthropic:MiniMax-M3` — per class-scenario;
4. inspect `floor/runs/<run-id>/report.txt` (+ `detail.json`);
5. `python3 -m arb_eval.cli publish --run <run-id>` → prints the artifact;
6. store via the deployed `memory_store` write path, tagged
   `instr1-floor / seat:<seat> / model:<ver> / harness:<sha> / corpus:<ver> / run:<run-id>` (so
   `memory_search` answers "what was codex's floor at model X / corpus Y"). NDJSON + report files
   remain the authoritative local evidence; the memory artefact is a pointer-carrying summary,
   never the measurement source.

**Consumption wall restated:** nothing may read these artefacts to change trust/quorum/seat
assignment automatically.

---

## 7. Seat set + cost (v1)

**Representative seat set (v1): codex + agy, confined** — the two seats already verified in the
jail (`confinement/README.md:26-32`). The fleet's ~23 bridge seats are NOT floor candidates as a
population (role-profile duplicates; only confinable external processes qualify). **Named
expansion, in order:** containerized Claude Code CLI seat, then pi-harness engines if added to the
image. Each expansion is +45 gold pairs (§C) and an image change (a §E change event).

**Per full run (one seat, three classes, defaults):** 3 scenarios × 4 repeats = **12 confined
review passes**; one pass ≈ 3–10 min (validate on the first real-base run; A1's 900s timeout bounds
the tail); normalizer ≤ ~40 M3 calls/scenario, 512-token cap, temp 0 — cents. **Wall-clock ≈
0.6–2 h per seat, sequential**; two-seat v1 full run ≈ 1.5–4 h. Repeats add conservatism via
pooling (not Wilson n), so there is no incentive to cut them to save cost.

---

## 8. Human forks — specified as parameterized defaults (NOT resolved here)

Per the brief, these are the design's §8 forks; the SPEC pins the default and names the fork:

| # | fork | parameter | default (design's proposal) |
|---|---|---|---|
| 1 | Real base pick + clone-vs-vendor + **SHA pin (REQUIRED, no default — builder refuses)** | `ARB_EVAL_REAL_BASE_URL`/`_SHA` in `build_floor_real_*.sh`; vendor flag | repo `nsidnev/fastapi-realworld-example-app` proposed; SHA pinned by Mark at corpus sign-off; **clone-at-build** |
| 2 | Gold rater pool + labor timing | `gold rate --rater <id>`; primary-vs-tiebreak set in summary | **Mark + model-alias-1** primary, **grok** tie-break; ~1.5–3 h v1 adjudication |
| 3 | Run-vs-gold order | operational sequencing (no code gate) | **run first** with `GOLD_UNADJUDICATED` labeling (it sources the gold pairs), then adjudicate, then re-render |
| 4 | Claude-container seat timing | seat set in `--seat` / scenario `panel` | **expansion, not v1 critical path** |

Each default is a real, runnable value; changing it is a config/flag change, not a redesign. The
build ships the default; the human flips the flag.

---

## 9. Verification obligations → named tests

**Hermetic (CI: no engines, no docker — env pinned per the V6 scar). Each names its red
condition.**

- **H1 (A1) — `tests/test_pipeline.py::TestIncompleteRepeat`:**
  - `test_confined_timeout_renders_unknown_not_fail` — fake `confined-review.sh` stub that sleeps
    past a tiny `ARB_EVAL_DISPATCH_TIMEOUT` → `DispatchError(kind="timeout")`, `dispatch_error` +
    `incomplete_repeat` events recorded, the affected class renders **UNKNOWN** with the
    `infra_incomplete(kind=timeout, …)` line, and contributes **zero** to caught/noise.
  - `test_canary_failure_parks` — stub exits **43** → `DispatchError(kind="canary")` → `Parked`
    (distinct from a slow seat).
  - **Deny-proof `test_delete_incomplete_exclusion_would_fake_fail`** — with the forced-UNKNOWN
    override removed (patched out in the test), the incomplete class scores the completed repeats
    and can render PASS/FAIL → the UNKNOWN assertion goes **red**. This pins that the pre-v2
    fake-FAIL cannot be silently restored.
  - CI-runnable: uses a shell stub + `MockNormalizer`; no docker/engine.

- **H2 (A2) — `tests/test_provenance.py`:**
  - `test_provenance_event_has_all_keys_and_passes_guard` — all model/harness/corpus/image/
    normalizer/matcher/boundary/gold keys present; `report.guard` passes; every subprocess
    (`git`, `docker`, engine) stubbed.
  - **`test_model_input_change_changes_key`** — two collects differing only in `model_requested`
    yield different `provenance_key`; two differing only in the command (config bump) yield the
    same key but a changed `confined_command`.
  - **`test_reply_fake_nonce_marker_does_not_corrupt_engine_versions`** — a review reply embedding
    `ARB_PROV_<other-nonce>{…}</…>` leaves `strip_prov_fence(stdout, real_nonce)` engine_versions
    untouched and the review text intact.
  - `test_cli_version_only_renders_loud_warning` — `model_source == "cli-version-only"` → warning
    line emitted + `model_unverified` propagates.
  - CI-runnable: fully stubbed subprocess.

- **H3 (A3) — `tests/test_boundary.py`:**
  - `test_heuristic_tier_methods_nested_decorated` — fixture files with a method-in-class, a nested
    def, and a decorated/async route; assert the enclosing symbol and `tier == "heuristic"` (runs
    unconditionally).
  - `test_forced_absence_falls_through_and_records_tier` — inject tree-sitter/ctags **absence** (via
    monkeypatching the import / `shutil.which`, NOT host state — V6 scar) and assert the fallback
    chain + the recorded tier per step.
  - `test_ctags_tier_when_available` / `test_treesitter_tier_when_available` — behind availability
    skips that **assert the skip reason** (no vacuous green).
  - `test_matcher_decision_records_boundary_oracle_field` — a `function`-basis `matcher_decision`
    row carries `boundary_oracle`.
  - CI-runnable: heuristic + injection tests always; ctags/tree-sitter skip-with-reason.

- **H4 (A5) — `tests/test_pipeline.py::TestScenarioPortability`:**
  - `test_relative_repo_resolves_against_scenario_dir` — a scenario with a relative `subject.repo`
    resolves to an absolute path under the scenario's directory.
  - `test_missing_repo_or_sha_raises_scenario_error` — `validate_subject` on an absent repo, and on
    an unresolvable base/head SHA, each raises a clean `ScenarioError`.
  - CI-runnable: builds a throwaway git repo in `tempfile` (git is available in CI); no docker.

- **H5 (§C) — `tests/test_gold.py`:**
  - `test_export_packet_has_no_outcome_fields_and_inlines_snippet` — exported packet contains no
    `pipeline`/`match_outcome`/`basis`/`final_verdict`, and DOES carry the inlined code snippet.
  - `test_rate_stub_parses_retries_once_then_records_rater_error` — a stub rater whose first
    response is unparseable → one retry → on second failure a `rater_error` row (never a silent
    skip).
  - `test_ingest_and_score_recall_precision_and_pairwise_kappa` — ingest merges verdicts; score
    computes Wilson recall/precision + raw agreement + **pairwise κ over the two primary raters
    only** (tie-break excluded from κ).
  - `test_gate_suppresses_below_gate_seat_only` — a below-gate summary suppresses that seat's rows,
    leaving other seats intact; an at/above-gate summary renders the band; absent summary →
    `GOLD_UNADJUDICATED`.
  - **Deny-proof `test_delete_gate_wiring_would_unsuppress`** — with the gate wiring removed
    (patched), the below-gate seat's rows appear → the suppression assertion goes **red**.
  - CI-runnable: stub rater (no bare-API call), `stats.py` is pure stdlib.

- **H6 (§E) — `tests/test_publish.py`:**
  - `test_artifact_has_four_keys_and_verbatim_disclaimer` — `seat/model_version/harness_version/
    corpus_version` present; `disclaimer == report.DISCLAIMER`.
  - `test_injected_rank_field_raises_wallbreach` — a `rank`-shaped field anywhere → `WallBreach`
    (denylist).
  - **`test_injected_matcher_recall_field_raises_wallbreach`** — a `matcher_recall`-shaped key in
    the `gold` sub-object → `WallBreach` **via `assert_gold_field_shape` (allowlist)**; the P1-ε
    deny-proof — assert the denylist alone would NOT catch it (i.e. `report.guard` passes it, the
    allowlist rejects it).
  - `test_dirty_harness_refuses_publish` — a `-dirty` `harness_version` → clean refusal, non-zero.
  - `test_gold_field_is_gate_outcome_only_shape` — the emitted `gold` object has exactly
    `{gold_adjudicated, gold_version, suppressed_seats}`.
  - `test_assert_verdict_row_scoped_to_grid_not_claim_levels` — a `claim_levels` value that is not a
    verdict literal does NOT false-raise (the row check is scoped to `grid`).
  - CI-runnable: operates on a synthetic `detail.json`/`events.ndjson` fixture; no docker.

- **H7 (B) — `tests/test_real_corpus_lint.py`:**
  - `test_seed_id_in_patch_or_commit_message_fails_build` — a seed/control ID embedded in a patch
    comment, a commit message, a tag name, or a branch name → the generator lint fails the build.
  - `test_seed_and_control_within_window_in_one_function_raises_scenario_error` — a seed and a
    same-class control within the matcher window (same file, ≤10 lines) → `ScenarioError` at load;
    the generator's same-enclosing-function check (via `boundary.enclosing_symbol`) also flags it.
  - `test_anti_oversplit_lint_caps_effective_clusters_at_rationale_count` — clusters exceeding the
    number of distinct `why_clean` rationales → merged/flagged.
  - CI-runnable: builds throwaway git repos + patch files in `tempfile`; no docker/engine.

**Full python suite (`python3 -m pytest tools/eval`) green is a gate; the existing 58 tests must
stay green.**

**Live gates (required — the untested CLI/subprocess glue is where bugs survive static review;
these need docker + real engines and are NOT CI):**

- **L1** — first real-base confined scored run, one seat: canary green per dispatch, answer-key
  pre-flight green, report labeled honestly (small-n lines where effective < T).
- **L2 (negative control)** — plant a seed ID into the fixture worktree → pre-flight **Parks**;
  plant into the image/jail surface → canary **Parks**. Both must go red or the guards are hollow.
- **L3 (gold pilot)** — export ≥10 real pairs, two raters adjudicate blind, κ computed, per-seat
  recall renders into a report with the band (or suppression) visibly applied.
- **L4 (change-event drill)** — bump one provenance input (image rebuild), rerun one seat on one
  scenario, `publish`, store via `memory_store`, retrieve via `memory_search` by the seat+corpus
  key — the artefact round-trips and matches the local report.

CHANGELOG.md entry required (what AND why) per repo discipline.

---

## 10. Rejected alternatives (carried from design §9, binding)

Unconfined bridge dispatch as the scored path; bare-API reviewer seats; in-session cold-Opus as a
scored seat; lowering the power target to fit ~12–14 effective controls (reject-C); a re-prompt
loop for format-nonconforming seats; M3 as a gold rater; `publish` auto-writing to ARB Memory; a
MATCHER-SUSPECT headline verdict. None are to be introduced by this build.

---

## 11. Non-goals (carried from design §11)

Decorrelation/marginal-contribution/seat-drop instruments; implementer/adjunct generation-quality
oracle; the escaped-defect journal; automated/scheduled benching or CI-run live floors; per-engine
trust changes; arb-watch/visibility rendering of floor runs; the drift track; expanding beyond
three classes. README/docstring staleness fixes ARE in-scope as the one-line doc obligation (§1).

---

## Escalations

**None.** The four §8 items are human forks the brief instructed to parameterize (done, §8), not
design contradictions. Two specification choices resolve design under-determination without
reopening a decision, and are called out so a reviewer can check them: (a) A2 says the provenance
event is written "at run start", but `engine_versions` are only observable inside the jail during
dispatch — the SPEC writes the static provenance at run start and merges per-seat engine data via a
`provenance_engine` event captured on each seat's first successful confined dispatch (design intent
preserved: all fields land in `detail["provenance"]`); (b) canary-vs-review-vs-timeout
classification needs a machine-distinguishable signal — the SPEC pins `confined-review.sh` exit
code **43** as the canary-breach sentinel (review stage remaps its own rc 43→1) rather than
string-matching stderr. Both are additive and
consistent with the certified design; neither weakens a wall.

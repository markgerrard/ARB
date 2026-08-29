# Instrument 1 completion — Implementation Plan (codex worker, worktree)

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (or
> superpowers:executing-plans) to work this plan task-by-task. Steps use checkbox (`- [ ]`)
> syntax. This plan is executable WITHOUT asking questions — every seam is a real file:line in
> `tools/eval/` verified 2026-07-09; where the source has moved, the names (functions, events,
> fields) are authoritative, the offsets are hints.

**Source of truth:** the CERTIFIED SPEC
`docs/superpowers/specs/2026-07-09-instr1-completion-SPEC.md` (v1.2, certified
`panel-capsuiteaspec-r3-20260709T015345Z-f5abd7`, zero P0/P1) and its certified design v2
(`docs/superpowers/specs/2026-07-09-instr1-completion-design.md`). Read the SPEC in full before
Task 1. This plan translates the SPEC; it does not respec. Any contradiction found mid-build goes
in **§ Escalations at the bottom of this plan** and stops the affected task — it is not resolved
by improvisation.

---

## Walls — binding on every task (SPEC §0, restated verbatim)

A change that violates one of these is a **build error, not a review nit**. They are repeated here
so no task can be worked without them in view:

- **No ranking / no seat-drop / no trust / no quorum verdict / no composite seat score.** Output
  is an unordered `PASS / FAIL / UNKNOWN`-by-class grid only. The allowlist wall in `report.py`
  (`render_grid` / `assert_verdict_row`) and its secondary denylist (`guard`) stay and are never
  weakened.
- **Report-only.** Nothing downstream consumes a floor artifact to change trust, quorum, or seat
  assignment automatically. `publish` (Task 8) emits a payload for a HUMAN to store and read.
- **UNKNOWN-not-FAIL on infra.** An infra failure (dispatch timeout, canary/review error) NEVER
  scores as a capability miss and can NEVER render FAIL. It renders UNKNOWN with a named
  `infra_incomplete` line (Task 1). This is the round-1 P1-α wall; its deny-proof (H1) is
  mandatory and lands in the same task.
- **Disclaimer emitted verbatim** wherever a grid is rendered or published; the
  reader-convertibility residual stays NAMED, never silently dropped (`report.DISCLAIMER`).

## Scope guard — walls on WHERE code may change

- **`tools/eval/` ONLY.** This build touches nothing under `src/agent_redis_bridge/`. If a task
  seems to need a bridge change, that is an Escalation, not an edit.
- New modules land under `tools/eval/arb_eval/`; new scripts/fixtures under
  `tools/eval/confinement/` and `tools/eval/fixtures/`; tests under `tools/eval/tests/`.
- The **only** doc files this build may edit are the named staleness cleanups (Task 9):
  `tools/eval/README.md`, `tools/eval/confinement/README.md`, the `ContainerDispatcher` docstring
  in `pipeline.py`, and the repo-root `CHANGELOG.md`. No other doc, no `AGENTS.md`/`CLAUDE.md`, no
  CI config.

## Hermeticity — binding on every Hn test (SPEC §9)

CI has **no docker and no engines** (the V6 env-pinning scar). Every H1–H7 test is CI-runnable and
**stubs every subprocess** (`git` only where CI genuinely provides it — Task 5/9 build throwaway
git repos in `tempfile`, which is allowed; `docker` and engine invocations are ALWAYS stubbed).
Availability-gated tests (ctags / tree-sitter) **assert their skip reason** — no vacuous green.
The live gates that need docker + real engines are **Task 10, a MANUAL runbook, never CI.**

## Test-framework note (read once)

Existing tests are `unittest.TestCase` classes in `tools/eval/tests/`, collected by both
`python3 -m unittest discover -s tests` and `.venv/bin/python3 -m pytest tools/eval`. Match that
style: where the SPEC names a `TestX::test_y`, write a `class TestX(unittest.TestCase)` with a
`def test_y`; where it names a bare `test_y`, add it as a method on the file's natural TestCase.
Use `tempfile.TemporaryDirectory` / `self.subTest`, and the existing `pipeline.MockNormalizer`,
`pipeline.MockDispatcher`, `pipeline.Parked`, `pipeline.DispatchError` helpers — do not reinvent
them. Run the venv interpreter: `/Users/<user>/<workspace>/.venv/bin/python3`.

**Baseline gate (run once, before Task 1):**
`.venv/bin/python3 -m pytest tools/eval -q` → **58 passed**. If it is not 58-green on a clean
checkout, stop and report — do not build on an unknown-red baseline. The 58 existing tests must
stay green after every task.

---

## Task dependency map

```
Task 1  A1 dispatch timeout + classified DispatchError + confined-review 43-contract
        + run_floor incomplete-repeat → forced-UNKNOWN (H1)          [no deps]
Task 2  A3 boundary.py oracle + match_finding wiring + plan warning (H3)   [no deps]
Task 3  A2 provenance.py + run_floor wiring + confined-review model/nonce (H2)  [needs Task 2 tier map]
Task 4  A5 relative subject.repo + validate_subject + regen scenarios (H4)  [no deps]
Task 5  C  gold.py + stats.cohen_kappa + cli gold verbs + gold-gate wiring (H5)  [no deps; gate wiring after gold module, same task]
Task 6  A4+D format_conformance + detail-file matcher-miss fields          [needs Task 5 gold summary shape, Task 1 events]
Task 7  E  publish.py + report.assert_gold_field_shape + cli publish (H6)   [needs Tasks 3,5,6]
Task 8  B  real-corpus builders + generators + lint + schema D3 window (H7) [needs Task 2 boundary]
Task 9  Docs staleness cleanup + CHANGELOG                                  [after code tasks]
Task 10 Live gates L1–L4 (MANUAL runbook, NOT CI)                          [after merge, orchestrator]
```

Ordering honours the brief's constraints: **A1 timeout semantics (Task 1) before the pipeline
changes that consume them; provenance (Task 3) before publish (Task 7); the gold module before its
gate wiring (both inside Task 5, step-ordered); every report/wall change ships with its deny-proof
in the same task** (Task 1 forced-UNKNOWN override + H1 deny-proof; Task 5 gate wiring + H5
deny-proof; Task 7 `assert_gold_field_shape` allowlist + H6 matcher-recall deny-proof).

---

## Task 1 — A1: dispatch timeout, classified `DispatchError`, confined-review 43-contract, incomplete-repeat → forced-UNKNOWN

**Goal (SPEC §2 A1):** an infra failure (timeout/canary/review) can never render FAIL. Timeout and
review failures make the `(seat, repeat)` *incomplete* — it emits **no** `matcher_decision` rows
and forces every class verdict for that seat to UNKNOWN with a named `infra_incomplete` line. A
canary breach (exit 43) Parks the run. This deletes the pre-v2 fake-verdict route (the old code
wrote detection-miss for every seed / clean for every control on dispatch error).

**Files:**
- Modify: `arb_eval/pipeline.py` — `DispatchError` (class ~`:52`), `ContainerDispatcher.dispatch`
  (`:162-175`), `run_floor` scoring block (`:679-729`), the verdict step (`:731-769`); add helper
  `_incomplete_seats`.
- Modify: `confinement/confined-review.sh` — canary-first `|| exit 43`; review stage in an
  rc-captured block that remaps its own 43→1.
- Modify: `tests/test_pipeline.py` — new `class TestIncompleteRepeat`; DELETE/replace the existing
  `test_dispatch_error_records_misses_and_clean_controls` (`:370`) — that test pins the OLD
  fake-verdict behavior this task removes; it must be rewritten to assert the new
  incomplete/UNKNOWN semantics, not left asserting deleted behavior.

**Interfaces / wire shapes (pinned by SPEC — do not paraphrase):**
- `DispatchError(message, *, kind: str = "review")`; `self.kind = kind`; `kind ∈ {"timeout",
  "canary","review"}`.
- `_dispatch_timeout()` reads env `ARB_EVAL_DISPATCH_TIMEOUT` (int seconds, **default 900**).
- `subprocess.run([...], timeout=_dispatch_timeout(), ...)`; classify:
  `TimeoutExpired → kind="timeout"`; `returncode == 43 → kind="canary"`; any other non-zero →
  `kind="review"` (keep the truncated stderr/stdout tail as today).
- `dispatch_error` event: `{"event":"dispatch_error","ts":…,"run_id":…,"seat":…,"repeat":…,
  "kind":"timeout"|"review","error":…,"task":…}`.
- `incomplete_repeat` event: `{"event":"incomplete_repeat","ts":…,"run_id":…,"seat":…,
  "repeat":…,"kind":"timeout"|"review","classes":[…all scenario classes…]}`.
- `_incomplete_seats(path: Path) -> dict[str, list[tuple[str,int]]]` — seat → `[(kind, repeat),…]`
  from `incomplete_repeat` events.
- Report line format: `infra_incomplete(<seat>, kind=<kind>, repeat=<n>)`, one per incomplete
  `(kind, repeat)`.
- `confined-review.sh` contract: `"$HERE/canary.sh" "$FIXTURE" "$SCENARIO" || exit 43`; the review
  stage runs rc-captured and remaps its own 43 (`rc=$?; [ "$rc" -eq 43 ] && exit 1; exit "$rc"`),
  so **only the canary can produce 43**. No timeout in the script — Python owns the wall clock.

- [ ] **Step 1 — write the failing H1 tests first.** In `tests/test_pipeline.py` add
  `class TestIncompleteRepeat(unittest.TestCase)` with:
  - `test_confined_timeout_renders_unknown_not_fail` — point a `ContainerDispatcher` at a
    `tempfile` dir holding a stub `confined-review.sh` that `sleep`s past a tiny
    `ARB_EVAL_DISPATCH_TIMEOUT=1`; run one scenario/seat. Assert: `DispatchError(kind="timeout")`
    is raised inside dispatch; the run **completes**; `dispatch_error` + `incomplete_repeat` events
    are in the NDJSON; the affected class renders **UNKNOWN** with an `infra_incomplete(kind=timeout,
    …)` line; and that class contributes **zero** to caught/noise (assert no `matcher_decision`
    rows exist for that `(seat, repeat)`).
  - `test_canary_failure_parks` — stub exits **43** → `DispatchError(kind="canary")` →
    `pipeline.Parked` raised (distinct from a slow/incomplete seat).
  - `test_delete_incomplete_exclusion_would_fake_fail` (DENY-PROOF) — **fixture MUST be
    mixed-completion (plan-panel r1 cold-Opus P1 + GLM P1-2: an all-timeout single-repeat
    fixture is vacuously UNKNOWN even with the override deleted, because zero
    `matcher_decision` rows classify UNKNOWN anyway): ≥2 repeats, one COMPLETING with
    scoreable rows (misses) + one timed-out.** With the forced-UNKNOWN override
    monkeypatched out, the incomplete class scores the completed repeat's rows and renders
    FAIL → the UNKNOWN assertion goes **red**; with the override present → UNKNOWN. This
    pins that the pre-v2 fake-FAIL cannot be silently restored, non-vacuously.
  All three use a shell stub + `MockNormalizer`; **no docker/engine**. Run them, confirm the first
  and third FAIL for the right reason (current code has no timeout/kind and still writes the old
  detection-miss rows), the Park test currently mis-behaves (returncode 43 today just raises the
  generic `DispatchError` → no Park). Do not proceed until they fail for the expected reason.

- [ ] **Step 2 — rewrite the obsolete test.** Replace
  `test_dispatch_error_records_misses_and_clean_controls` (`:370`) with a test asserting a *review*
  (`kind="review"`) dispatch error now yields an `incomplete_repeat` event + forced UNKNOWN and
  **no** `matcher_decision` rows for that repeat (the old "records misses and clean controls"
  contract is deleted by design). Keep the same fixture skeleton.

- [ ] **Step 3 — implement `DispatchError.kind` + `ContainerDispatcher` timeout/classification.**
  Add the `kind` kwarg + attribute; add `_dispatch_timeout()`; wrap `subprocess.run` with `timeout=`
  and the three-way classification above. On `TimeoutExpired` the message names the seconds; on
  rc==43 raise `kind="canary"`; otherwise `kind="review"`.

- [ ] **Step 4 — implement the `confined-review.sh` 43-contract.** Change the canary invocation to
  `|| exit 43`; wrap the per-seat review invocation so its own rc is captured and a coincidental 43
  is remapped to 1 before propagating. `set -euo pipefail` stays for the review stage. (This hunk
  is exercised only by the live gates L1/L2 — CI never runs it — but land it now with the Python
  classification it pairs with.)

- [ ] **Step 5 — implement the `run_floor` scoring change.** In the `(seat, repeat)` loop
  (`:679-729`): on `DispatchError e`:
  - `e.kind == "canary"` → `raise Parked(...)` immediately (integrity-fatal, not a flake).
  - `e.kind in {"timeout","review"}` → write `dispatch_error` (with `kind`, `repeat`, `seat`) then
    `incomplete_repeat` (`{seat, repeat, kind, classes: scenario.classes()}`), and **`continue`
    without emitting any `matcher_decision` rows** for that repeat. DELETE the old block that wrote
    detection-miss for every seed and clean for every control on dispatch failure (the
    `dispatch_failed`/`"dispatch-error"` basis path at `:679, :699, :714`).
  `_counts_from_events` is unchanged (it only ever sees `matcher_decision` rows; incomplete repeats
  emit none). Add the `_incomplete_seats` helper beside it.

- [ ] **Step 6 — implement the forced-UNKNOWN verdict override.** In the verdict step
  (`:736-769`), after `classify(...)`, if the seat appears in `_incomplete_seats(events_path)`,
  override every class verdict for that seat to `UNKNOWN` (both in `details["oracle"][seat][cls]`
  and in `verdicts[seat][cls]`), and append one `infra_incomplete(<seat>, kind=<kind>, repeat=<n>)`
  line per incomplete `(kind, repeat)` into the report text (alongside the small-n/orphan lines
  block at `:772-794`). Other seats/classes score normally; the run completes.

- [ ] **Step 7 — go green.** `.venv/bin/python3 -m pytest tools/eval/tests/test_pipeline.py -q`
  → `TestIncompleteRepeat` all pass, the rewritten review test passes, full file green. Then
  `.venv/bin/python3 -m pytest tools/eval -q` → 58 prior + new, all green.

**Done criterion:** H1's three tests pass; the deny-proof is **red** with the override patched out
and **green** with it in; no `matcher_decision` row is ever written for an incomplete repeat; canary
(43) Parks. `dispatch_error`/`incomplete_repeat` payloads pass `report.guard` (they route through
`_Recorder.write`, which already guards).

**Commit shape:**
```
feat(eval): A1 incomplete-repeat → UNKNOWN(infra); dispatch timeout + canary-43 contract

Infra failure (timeout/review error) no longer fakes detection-miss: the repeat
emits no matcher_decision rows and forces the seat's classes to UNKNOWN with a
named infra_incomplete line. Canary breach (confined-review exit 43) Parks the run.
Deny-proof: deleting the forced-UNKNOWN override reddens the timeout test.
SPEC §2 A1 (round-1 P1-α wall), H1.
```

---

## Task 2 — A3: boundary oracle (tree-sitter / ctags / heuristic), matcher wiring, `plan` warning

**Goal (SPEC §2 A3):** replace the single heuristic `_enclosing_function` with a tiered oracle that
**records the tier it used**; every `function`-basis `matcher_decision` row carries a
`boundary_oracle` field; `plan` warns when a fixture language's best tier is heuristic.

**Files:**
- New: `arb_eval/boundary.py`.
- Modify: `arb_eval/pipeline.py` — `match_finding` (`:395-421`) calls `boundary.enclosing_symbol`
  in place of the two `_enclosing_function` calls; the `matcher_decision` writes (`:708-710`,
  `:727-729`) gain the `boundary_oracle` field on `function`-basis rows. Move (or import) the
  existing `_enclosing_function` walker (`:365-392`) into `boundary.py` as the heuristic tier.
- Modify: `arb_eval/schema.py` — accept a `subject.languages` list field (default `[]`).
- Modify: `arb_eval/cli.py` — `_plan` (`:69-80`) emits the heuristic warning per fixture language.
- New: `tests/test_boundary.py` (H3).

**Interfaces (pinned):**
```python
@dataclass(frozen=True)
class SymbolResult:
    symbol: str | None
    tier: str            # "tree-sitter" | "ctags" | "heuristic"

def enclosing_symbol(repo: Path | None, file: str | None, line: int | None) -> SymbolResult: ...
```
Precedence, each tier fail-closed to the next **with the tier recorded**: (1) tree-sitter where a
grammar exists (optional imports `tree_sitter` + `tree_sitter_python`; import failure or absent
grammar → fall through, never crash); (2) ctags — `shutil.which("ctags")`; absent → fall through;
(3) heuristic — the moved `_enclosing_function` walker, always available.

`match_finding`: resolve seed and finding via `enclosing_symbol`; the emitted `boundary_oracle`
field = the tier of the **finding's** resolution when both resolve; a **mixed-tier** match records
the **weaker/lower** tier (heuristic < ctags < tree-sitter) so a silent downgrade is visible. The
field is **mandatory on every `matcher_decision` row whose `basis == "function"`**.

`plan` warning (verbatim shape): `WARNING: boundary oracle for <language> is 'heuristic'
(methods/nested/decorated defs may mis-resolve)` when the best available tier for a fixture language
is heuristic.

- [ ] **Step 1 — write H3 tests first** in `tests/test_boundary.py`:
  - `test_heuristic_tier_methods_nested_decorated` — fixture text (write to `tempfile`) with a
    method-in-class, a nested def, and a decorated/async route; assert the enclosing symbol AND
    `tier == "heuristic"`. Runs unconditionally.
  - `test_forced_absence_falls_through_and_records_tier` — monkeypatch the tree-sitter import to
    raise and `shutil.which` to return `None` (NOT host state — V6 scar); assert the fallback chain
    lands on heuristic and the recorded tier per step is correct.
  - `test_ctags_tier_when_available` / `test_treesitter_tier_when_available` — behind
    `unittest.skipUnless(...)` availability guards that **assert the skip reason string** (so the
    skip is visible, never vacuous green).
  - `test_matcher_decision_records_boundary_oracle_field` — drive `run_floor` (MockNormalizer,
    fixture repo with two functions) so a `function`-basis row is emitted; assert the row carries
    `boundary_oracle`. Add to `test_pipeline.py` if it needs `run_floor` plumbing, but keep the
    boundary-unit tests in `test_boundary.py`.
  Run; confirm they fail (no `boundary.py` yet).

- [ ] **Step 2 — implement `boundary.py`.** Move the heuristic walker in unchanged; add the ctags
  and tree-sitter tiers with fail-closed fall-through; return `SymbolResult`. Keep `report.guard`
  compatibility in mind: **no key or value produced here may contain the substring `tier`** on a
  guard-traversed surface — the field NAME on the event is `boundary_oracle` and its VALUE is the
  tier string (`"heuristic"` etc.), which is fine (values aren't key-checked), but never emit a dict
  key like `{"tier": …}` anywhere that reaches `_Recorder.write` (the guard rejects any key
  containing `tier` — round-1 codex P0, verified: `{"tiers":…}` raises `WallBreach`).

- [ ] **Step 3 — wire `match_finding`** to call `enclosing_symbol`; thread the resolved tier out so
  the two `matcher_decision` writes attach `boundary_oracle` on `function`-basis rows. Add
  `subject.languages` handling in `schema.py`. Add the `_plan` warning loop over
  `scenario.subject.get("languages", [])`, computing the best available tier per language (probe
  `shutil.which("ctags")` and the optional imports at plan time).

- [ ] **Step 4 — go green.** `.venv/bin/python3 -m pytest tools/eval/tests/test_boundary.py -q`
  then full `.venv/bin/python3 -m pytest tools/eval -q`. The existing `TestMatch` function-boundary
  tests (`test_function_match_respects_block_scope`, `test_window_does_not_cross_function_boundary`)
  **must stay green** — the heuristic tier is behaviour-identical to the moved walker.

**Done criterion:** heuristic + injection tests pass unconditionally; ctags/tree-sitter tests
skip-with-asserted-reason when the tool is absent; every `function`-basis `matcher_decision` row
carries `boundary_oracle`; `report.guard` still passes on all events (no `tier`-substring keys).

**Commit shape:**
```
feat(eval): A3 tiered boundary oracle (tree-sitter/ctags/heuristic) w/ recorded tier

enclosing_symbol() fails closed tier→tier and records which produced the symbol;
matcher_decision function-basis rows carry boundary_oracle so a silent downgrade
is visible; plan warns on heuristic-only languages. Guard-safe key naming
(no 'tier' substring on guarded surfaces). SPEC §2 A3, H3.
```

---

## Task 3 — A2: run-provenance capture (`provenance.py`), run_floor wiring, model/nonce env

**Goal (SPEC §2 A2):** capture a provenance dict (names/hashes only — never seat values) as a
`provenance` NDJSON event at run start, merge per-seat engine data at run end, and derive a
`provenance_key`. Model identity is WIRED end-to-end (pinned flag → captured argv), not
policy-only. Depends on Task 2 for the `boundary_oracle.oracle_by_language` tier map.

**Files:**
- New: `arb_eval/provenance.py`.
- Modify: `arb_eval/pipeline.py` — `run_floor` writes the `provenance` event at run start
  (near `:665 scenario_loaded`) and a `detail["provenance"]` block; on each seat's first successful
  confined dispatch, read `dispatcher.last_engine_versions` and emit `provenance_engine`, merged
  into `detail["provenance"]` at run end. `ContainerDispatcher.dispatch` passes `SeatSpec.model` as
  env `ARB_EVAL_MODEL` **only when `SeatSpec.model` is set AND not the `"?"` placeholder
  (r1 cold-Opus P2: the default `"?"` must never leak into the engine's model flag)**
  and the run nonce as `ARB_PROV_NONCE`, and calls `strip_prov_fence` on
  stdout, storing `self.last_engine_versions`.
- Modify: `confinement/confined-review.sh` — consume `ARB_EVAL_MODEL` on each seat's engine line
  (`codex exec --model "$ARB_EVAL_MODEL" …`; agy's model flag likewise) when non-empty; exit **2**
  (config error) if `ARB_EVAL_MODEL` is set but the seat has no model slot; emit the nonce fence
  `ARB_PROV_<nonce>{"<engine>":"<version>"}</ARB_PROV_<nonce>>` after the review.
- New: `tests/test_provenance.py` (H2).

**Interfaces (pinned — field names are load-bearing):**
`collect(scenario, *, dispatcher, normalizer, oracle_by_language, gold_versions, image,
harness_root) -> dict` returns exactly the SPEC §2 A2 dict: `model` (per-seat
`{model_requested, confined_command, model_reported, model_source}` where `model_source ∈
{"pinned-flag","readback","cli-version-only"}`), `engine_versions` (`{}` at start, filled at run
end), `harness_version` (`{describe: git describe --always --dirty, package_sha256: sha256 over
sorted arb_eval/*.py bytes}`), `image_digest`, `corpus_version` (`{builder_sha, scenario_sha256,
repo_base, repo_head}`), `normalizer` (`{model, endpoint}`), `matcher` (`{window}`),
`boundary_oracle` (`{oracle_by_language: {<language>: tier}, coverage: [<language>,…]}` — **KEY
NAMING IS LOAD-BEARING: use `oracle_by_language`, never a `*tier*` key; `report.guard` rejects any
key containing `tier`**), `gold_versions` as a **PER-SEAT map**
`{"<seat>": <hex|"GOLD_UNADJUDICATED">}` — **this is the certified shape: SPEC v1.3
amendment (recert-applied, r2 codex P1: a plan must never respec a certified wire shape;
the SPEC now pins the map)**; Task 7's per-seat `publish` reads its seat's entry — the
artifact's scalar `gold_version` is that seat's value, `run_id`.

`strip_prov_fence(stdout, nonce) -> tuple[str, dict]` — parses ONLY the exactly-nonced fence,
returns `(review_text_without_fence, engine_versions_dict)`. A reply echoing any OTHER nonce cannot
corrupt the parse.

`provenance_key(prov) -> str` = sha256 over the stable tuple `(model_requested per seat,
harness_version.describe, corpus_version, image_digest)`. Model-input change ⇒ key changes; a
config-only bump (same model, different command) ⇒ same key but changed `confined_command`.

`model_*` wiring: `model_requested` is copied from the SAME `SeatSpec.model` the argv is built
from (divergence impossible by construction); `confined_command` = the full argv the container ran
(captured via the nonce fence / a companion fence line). A `cli-version-only` seat emits a LOUD
warning line into the report and sets `model_unverified: true` for the §E artifact.

`provenance` event: `{"event":"provenance", …, "provenance": <dict>}`.
`provenance_engine` event: `{"event":"provenance_engine","seat":…,"engine_versions":{…},
"model_reported":…,"model_source":…}`.

- [ ] **Step 1 — write H2 tests first** in `tests/test_provenance.py`, **all subprocess
  (`git`, `docker`, engine) stubbed**:
  - `test_provenance_event_has_all_keys_and_passes_guard` (a.k.a.
    `test_provenance_dict_passes_report_guard_verbatim`) — every model/harness/corpus/image/
    normalizer/matcher/boundary/gold key present; `report.guard(prov)` passes; assert specifically
    that a `boundary_oracle` sub-dict keyed `oracle_by_language` passes and that a mutated variant
    keyed `{"tiers":…}` **raises `WallBreach`** (proves the load-bearing naming).
  - `test_model_input_change_changes_key` — two `collect`s differing only in `model_requested`
    yield different `provenance_key`; two differing only in the command (config bump) yield the
    SAME key but a changed `confined_command`.
  - `test_reply_fake_nonce_marker_does_not_corrupt_engine_versions` — a review reply embedding
    `ARB_PROV_<other-nonce>{…}</…>` leaves `strip_prov_fence(stdout, real_nonce)` engine_versions
    untouched and the review text intact.
  - `test_cli_version_only_renders_loud_warning` — `model_source == "cli-version-only"` → warning
    line emitted + `model_unverified` propagates.
  - H2 deny-proof extension: with `SeatSpec.model` set, the recorded `confined_command` MUST
    contain the flag value (assert this inside `test_model_input_change_changes_key` or a sibling).
  Run; confirm failures (no `provenance.py`).

- [ ] **Step 2 — implement `provenance.py`** (`collect`, `strip_prov_fence`, `provenance_key`) with
  every subprocess call isolated behind a small helper the tests can stub. Guard-safe keys only.

- [ ] **Step 3 — wire `run_floor` + `ContainerDispatcher`.** Mint `nonce = uuid4().hex` per run;
  pass `ARB_PROV_NONCE` and `ARB_EVAL_MODEL` (from the seat's `SeatSpec.model`) to
  `confined-review.sh`; call `strip_prov_fence` on stdout in `dispatch`, store
  `self.last_engine_versions` and the captured `confined_command`. Write the `provenance` event at
  run start and merge `provenance_engine` per seat's first successful dispatch into
  `detail["provenance"]` at run end. Add the `ARB_EVAL_MODEL` consumption + exit-2 + nonce-fence
  emission to `confined-review.sh` (live-gate-exercised only; CI stubs it).

- [ ] **Step 4 — go green.** `test_provenance.py` passes; full suite green;
  `report.guard(details)` at `:797` still passes with the new `provenance` block present.

**Done criterion:** H2's four tests + the `confined_command`-contains-flag deny-proof pass; a
fake-nonce reply cannot corrupt engine versions; the provenance dict passes `report.guard` and a
`*tier*` key variant raises. `provenance_key` changes iff a model input changes.

**Commit shape:**
```
feat(eval): A2 run-provenance capture (model wired, nonce-fenced engine versions)

provenance.collect() records names/hashes only; model_requested is copied from the
same SeatSpec.model the argv is built from (ARB_EVAL_MODEL → confined-review.sh),
so CLI-version can't masquerade as model identity. Nonce-fenced engine-version
markers resist reply spoofing. provenance_key changes on model-input change only.
Guard-safe key naming (oracle_by_language, never *tier*). SPEC §2 A2, H2.
```

---

## Task 4 — A5: relative `subject.repo`, `validate_subject`, regenerate scenarios

**Goal (SPEC §2 A5):** scenarios become portable — a relative `subject.repo` resolves against the
scenario file's directory; the CLI run path validates the repo/SHAs with a clean `ScenarioError`;
generators emit relative repos and the committed scenarios lose their absolute cross-clone paths.

**Files:**
- Modify: `arb_eval/schema.py` — `load(path)` resolves a relative `subject.repo` against
  `Path(path).parent`, stores the absolute back into `subject["repo"]`, keeps the original in
  `subject["repo_declared"]`; absolute repos used as-is. `load`/`list` do NOT require the repo to
  exist. Add `validate_subject(scenario)`.
- Modify: `arb_eval/cli.py` — `_run` (`:102-135`) wraps a NEW `schema.validate_subject(sc)` call in
  its own `except schema.ScenarioError → _die` (the existing mapping at `:107` covers scenario LOAD
  only — round-1 cold-Opus P2). **`validate_subject` is called by `cli._run` ONLY, never inside
  `run_floor`** (round-1 agy P1: existing unit tests drive `run_floor` with mock SHAs like
  `"base-sha"` and must stay green).
- Modify: `fixtures/gen_floor_secrets_full_scenario.py`,
  `fixtures/gen_floor_correctness_full_scenario.py` — emit `subject.repo` relative to the scenario
  file's directory (e.g. `../fixtures/repos/floor-secrets-full`).
- Regenerate: `scenarios/floor-secrets-full.json`, `scenarios/floor-correctness-full.json`,
  `scenarios/floor-secrets-001.json` — replace the absolute
  `/Users/<user>/AgentRedisBridge/.claude/worktrees/…` paths with relative ones.
- Modify: `tests/test_pipeline.py` — add `class TestScenarioPortability` (H4).

**Interfaces (pinned):** `validate_subject(scenario)` — repo dir must exist and be a git checkout;
`subject.base`/`subject.head` must resolve (`git -C <repo> rev-parse --verify <sha>^{commit}`); any
failure → `raise ScenarioError("subject repo/SHA unresolved: …")` (clean one-line, no traceback).

- [ ] **Step 1 — write H4 tests first** (`class TestScenarioPortability` in `test_pipeline.py`):
  - `test_relative_repo_resolves_against_scenario_dir` — a scenario JSON (written to `tempfile`)
    with a relative `subject.repo` resolves to an absolute path under the scenario's directory;
    `subject["repo_declared"]` keeps the original.
  - `test_missing_repo_or_sha_raises_scenario_error` — `validate_subject` on an absent repo, and on
    a real throwaway git repo (built in `tempfile` — git IS available in CI) with an unresolvable
    base/head, each raises a clean `ScenarioError`.
  Run; confirm failures.

- [ ] **Step 2 — implement** the `load` resolution + `validate_subject`, and the `cli._run`
  wrapping. Scenario updates (r1 codex P2 — only TWO generators exist:
  `gen_floor_secrets_full_scenario.py` + `gen_floor_correctness_full_scenario.py`):
  regenerate `floor-secrets-full.json` + `floor-correctness-full.json` by RUNNING those two
  (update them to emit relative `subject.repo` first); `floor-secrets-001.json` and
  `floor-001.example.json` have NO generator — hand-edit their `subject.repo` to the
  relative form in the same commit. Verify no absolute cross-clone path remains:
  `grep -rn "AgentRedisBridge\|/Users/<user>" tools/eval/scenarios/*.json` → empty.

- [ ] **Step 3 — go green.** Full suite green; the existing `run_floor`-driving tests (which use
  mock SHAs) untouched and passing (proves `validate_subject` did not leak into `run_floor`).

**Done criterion:** H4 both pass; committed scenarios are path-portable; `run_floor` unit tests with
mock SHAs stay green; `plan`/`list` still run offline (no repo required).

**Commit shape:**
```
feat(eval): A5 portable scenarios — relative subject.repo + CLI-only validate_subject

schema.load resolves a relative subject.repo against the scenario dir (keeps
repo_declared); cli._run validates repo+SHAs with a clean ScenarioError, scoped
to the CLI path so run_floor unit tests with mock SHAs stay green. Committed
scenarios drop absolute cross-clone paths. SPEC §2 A5, H4.
```

---

## Task 5 — C: gold matcher-validation set (`gold.py`), Cohen's κ, CLI verbs, gold-gate wiring

**Goal (SPEC §4):** per-seat matcher recall/precision with an error band; a seat whose recall
lower-CI < `matcher_gate` (default **0.85**) has ITS floor rows suppressed; absent gold →
`GOLD_UNADJUDICATED`. The gold MODULE is built first, then the run_floor gate wiring (both in this
task, step-ordered — module before wiring).

**Files:**
- New: `arb_eval/gold.py` — `export`, `rate`, `ingest`, `score` module fns + `load_summary(seat)
  -> GoldSummary | None`.
- Modify: `arb_eval/stats.py` — add `def cohen_kappa(a: list[str], b: list[str]) -> float`
  (**edge pins, r1 P2s: p_e == 1.0 ⇒ return 1.0 if observed agreement is also 1.0 else 0.0,
  never divide; empty inputs ⇒ ValueError; and `wilson(k, 0)` returns the degenerate (0.0,
  1.0) interval, tested**)
  (pure stdlib; κ defined for exactly two raters).
- Modify: `arb_eval/cli.py` — new `gold` subparser with `export|rate|ingest|score`.
- Modify: `arb_eval/pipeline.py` — `run_floor` loads `gold/<seat>/summary.json` per seat and applies
  the three-state gate (band / suppress / GOLD_UNADJUDICATED) into the grid + report.
- New: `gold/.gitignore` (ignore `gold/<seat>/pairs.ndjson`, `summary.json` — working data).
- New: `tests/test_gold.py` (H5).

**Interfaces / shapes (pinned):**
- Pair record and packet shapes per SPEC §4. `gold_version` = sha256 of `pairs.ndjson`.
- `gold export --seat <s> --out <packets.ndjson>` — one packet per pair, **WITHOUT the `pipeline`
  block**, **WITH the cited code snippet ± context lines inlined**. Packet:
  `{pair_id, seat, raw_finding, snippet, context:{repo,base,head}}`.
- `gold rate --rater <model-alias-1|grok> --packets <file> --out <rater-file>` — one bare-API call per packet
  with a fixed rubric; parse `VERDICT: match|nonmatch|ambiguous`; **retry once on parse failure,
  then record `rater_error` (never a silent skip)**. Rater-file row: `{pair_id, rater,
  verdict|"rater_error", ts, raw_response?}`.
- `gold ingest <rater-file>` — merge each row's verdict into the pair's `adjudications`; recompute
  `final_verdict` (majority of the two primary raters; tie-break rater breaks a split).
- `gold score --seat <s>` — Wilson precision/recall (existing `stats.wilson`) + inter-rater raw %
  + **pairwise Cohen's κ between the two PRIMARY raters** (tie-break rater participates in verdicts,
  NOT in κ). Writes `gold/<seat>/summary.json`: `{seat, gold_version, n_pairs, recall:{k,n,lo,hi},
  precision:{…}, kappa, raw_agreement, matcher_gate:0.85}`.
- Gate in `run_floor` (per-seat): `recall.lo ≥ matcher_gate` → render with the per-seat band in
  `detail.json`; `recall.lo < matcher_gate` → **suppress** that seat's floor rows (remove from grid,
  other seats intact) with a named line `SUPPRESSED: <seat> matcher recall lo=<x> < gate=0.85 —
  rows withheld`; **absent** → today's `GOLD_UNADJUDICATED` warning.

- [ ] **Step 1 — write H5 tests first** (`tests/test_gold.py`), **stub rater (no bare-API call),
  `stats` is pure stdlib**:
  - `test_export_packet_has_no_outcome_fields_and_inlines_snippet` — exported packet contains no
    `pipeline`/`match_outcome`/`basis`/`final_verdict`, and DOES carry the inlined snippet.
  - `test_rate_stub_parses_retries_once_then_records_rater_error` — a stub rater whose first
    response is unparseable → one retry → on the second failure a `rater_error` row (never a silent
    skip).
  - `test_ingest_and_score_recall_precision_and_pairwise_kappa` — ingest merges verdicts; score
    computes Wilson recall/precision + raw agreement + **pairwise κ over the two primary raters
    only** (assert the tie-break rater is excluded from κ).
  - `test_gate_suppresses_below_gate_seat_only` — a below-gate summary suppresses that seat's rows,
    other seats intact; an at/above-gate summary renders the band; absent summary →
    `GOLD_UNADJUDICATED`.
  - `test_delete_gate_wiring_would_unsuppress` (DENY-PROOF) — with the gate wiring patched out, the
    below-gate seat's rows appear → the suppression assertion goes **red**.
  Run; confirm failures.

- [ ] **Step 2 — implement `stats.cohen_kappa`** and its own unit assertion inside
  `test_ingest_and_score_…` (κ of perfect agreement = 1.0; κ of chance ≈ 0).

- [ ] **Step 3 — implement `gold.py`** (`export`/`rate`/`ingest`/`score`/`load_summary`) — the
  MODULE first, fully, before touching `run_floor`. The `rate` verb's bare-API call sits behind an
  injectable callable the stub replaces (no network in CI).

- [ ] **Step 4 — wire the CLI `gold` subparser** (`export|rate|ingest|score`) to the module fns.

- [ ] **Step 5 — wire the run_floor per-seat gate** (three states). This is the step the deny-proof
  targets — keep it isolated enough that `test_delete_gate_wiring_would_unsuppress` can patch it
  out. The existing `GOLD_UNADJUDICATED` warning path stays as the absent-summary branch.

- [ ] **Step 6 — go green.** `test_gold.py` passes; the deny-proof is red with the gate patched out;
  full suite green.

**Done criterion:** all three gold states hermetically tested; deleting the gate wiring reddens the
suppression test; κ is over the two primary raters only; `gold/` working data is gitignored (nothing
under `gold/<seat>/` is committed).

**Commit shape:**
```
feat(eval): C gold matcher-validation set + per-seat recall gate

gold.py export(blind, snippet-inlined)/rate(retry-once→rater_error)/ingest/score
(Wilson recall+precision, pairwise Cohen's κ over the two primary raters only).
run_floor suppresses a below-gate seat's rows (others intact); absent gold stays
GOLD_UNADJUDICATED. Deny-proof: deleting the gate unsuppresses. SPEC §4, H5.
```

---

## Task 6 — A4 + D: `format_conformance`, matcher-miss vs detection-miss detail fields

**Goal (SPEC §2 A4 + §5 D):** report (never re-prompt) the format/detection split, and surface the
matcher-miss separation in the DETAIL file only — the headline grid stays PASS/FAIL/UNKNOWN,
untouched, no new verdict category. Depends on Task 5 (gold band) and Task 1 (events).

**Files:**
- Modify: `arb_eval/pipeline.py` — add `format_conformance`; enrich the `segmented` event
  (`:685`); add per-seat detail fields under `details["oracle"][seat]` (`:750-764`).
- Modify: `tests/test_pipeline.py` — extend (A4 unit + D detail assertions). *(A4 has no dedicated
  Hn gate in the SPEC test map; add a focused unit test here — it is required behaviour, just not a
  named live gate.)*

**Interfaces (pinned):**
```python
def format_conformance(candidates: list[str]) -> tuple[float, int, int]:
    # (fraction, conforming_lines, candidate_lines); a line conforms iff it matches
    #   ^\s*<class>\s*\|\s*<file>:<line>\s*\|\s*<desc>$   with <class> in TAXONOMY.
    # Empty candidate list -> (1.0, 0, 0).
```
`segmented` event gains `format_conformance` (float), `conforming_lines` (int), `candidate_lines`
(int). `details["oracle"][seat]` gains (D §5): `matcher_ambiguous_n` (count of
`matcher-ambiguous` outcomes for that seat/class, derived from `matcher_decision` events during the
existing count pass), `matcher_band` (`{recall_lo, recall_hi}` from the gold summary, `null` when
GOLD_UNADJUDICATED), `format_conformance_mean` (per-seat mean over the run's `segmented` events).
**No re-prompt/retry loop** (a retry changes what is measured). **No new headline verdict category**
(a MATCHER-SUSPECT verdict is a rejected convertible surface — SPEC §10).

- [ ] **Step 1 — write the tests first:** a unit test for `format_conformance` (conforming line,
  non-conforming line, empty → `(1.0, 0, 0)`, unknown class → non-conforming); a D test that drives
  `run_floor` and asserts `details["oracle"][seat][cls]` carries `matcher_ambiguous_n`,
  `matcher_band` (null under GOLD_UNADJUDICATED, populated when a gold summary is present), and
  `format_conformance_mean`; and an assertion that the **headline grid still contains only PASS/FAIL/
  UNKNOWN** (no new column/value). Run; confirm failures.

- [ ] **Step 2 — implement** `format_conformance`, the `segmented` enrichment, and the three detail
  fields. `matcher_ambiguous_n` folds into the existing `_counts_from_events`-adjacent pass or a
  sibling count over `matcher_decision` rows with `outcome == "matcher-ambiguous"`. `matcher_band`
  reads the Task-5 gold summary via `gold.load_summary(seat)`.

- [ ] **Step 3 — go green.** Full suite green; `report.guard(details)` still passes (the new keys
  carry no denylisted substrings — verify `matcher_ambiguous_n`, `matcher_band`,
  `format_conformance_mean`, `conforming_lines`, `candidate_lines` are all guard-clean).

**Done criterion:** `format_conformance` unit-correct; the three detail fields present and
guard-clean; the headline grid is byte-unchanged in shape (only PASS/FAIL/UNKNOWN); no re-prompt
loop exists.

**Commit shape:**
```
feat(eval): A4 format-conformance + D matcher-miss/detection-miss detail split

format_conformance() quantifies the format/detection split (reported, never
re-prompted); detail.json gains matcher_ambiguous_n, matcher_band (gold-derived,
null when unadjudicated), format_conformance_mean. Headline grid unchanged —
no MATCHER-SUSPECT category. SPEC §2 A4 + §5 D.
```

---

## Task 7 — E: change-event packaging (`publish.py`), `assert_gold_field_shape`, CLI `publish`

**Goal (SPEC §6):** `arb-eval publish` assembles a change-event artifact from a run's captured facts
(detail.json + events.ndjson + provenance), enforces the walls in order, and prints the payload for
a HUMAN to store — it never auto-writes to ARB Memory. Depends on Task 3 (provenance keys), Task 5
(gold), Task 6 (detail fields). **This task carries the `assert_gold_field_shape` wall + its
matcher-recall deny-proof together (brief: report/wall change ships with its deny-proof).**

**Files:**
- New: `arb_eval/publish.py` — `build_artifact(run_dir, seat=None) -> dict` (imports
  `assert_gold_field_shape` FROM `report.py`).
- Modify: `arb_eval/report.py` — add `_GOLD_ALLOWED` + `assert_gold_field_shape` beside the other
  wall helpers.
- Modify: `arb_eval/cli.py` — new `publish` subparser (`--run`, `--seat`, `--output-root floor`).
- New: `tests/test_publish.py` (H6).

**Interfaces / artifact shape (pinned — SPEC §6):** artifact keys exactly as the SPEC lists
(`seat, run_id, model_version, model_unverified, harness_version, corpus_version, grid,
claim_levels, small_n_lines, orphan_lines, infra_incomplete_lines, events_sha256, gold, disclaimer`).
```python
# report.py
_GOLD_ALLOWED = frozenset({"gold_adjudicated", "gold_version", "suppressed_seats"})
def assert_gold_field_shape(gold: dict) -> None:
    extra = set(gold) - _GOLD_ALLOWED
    if extra:
        raise WallBreach(f"gold artifact field carries non-allowlisted keys: {sorted(extra)}")
```
Seat resolution: one seat in the run → derived; several → `--seat` REQUIRED and `publish` errors
loudly naming the seats present (one artifact per seat, never merged).
**Refusals & wall checks, IN ORDER, before emit:** (1) dirty-harness refusal (`harness_version` ends
`-dirty` → clean non-zero); (2) `assert_gold_field_shape(artifact["gold"])`; (3)
`report.guard(artifact)` over the whole artifact; (4) `assert_verdict_row(seat,
artifact["grid"][seat])` **scoped to grid rows ONLY** (GLM P2-2 — running it over the whole artifact
false-raises on `claim_levels`). On success, **print the exact payload to stdout** (no ARB Memory
write).

- [ ] **Step 1 — write H6 tests first** (`tests/test_publish.py`), operating on a synthetic
  `detail.json`/`events.ndjson` fixture (no docker):
  - `test_artifact_has_four_keys_and_verbatim_disclaimer` — `seat/model_version/harness_version/
    corpus_version` present; `disclaimer == report.DISCLAIMER`.
  - `test_injected_rank_field_raises_wallbreach` — a `rank`-shaped field anywhere → `WallBreach`
    (denylist, via `report.guard`).
  - `test_injected_matcher_recall_field_raises_wallbreach` (DENY-PROOF, P1-ε) — a
    `matcher_recall`-shaped key in the `gold` sub-object → `WallBreach` **via
    `assert_gold_field_shape`**; assert the denylist ALONE would NOT catch it (i.e.
    `report.guard({"matcher_recall":…})` passes, the allowlist rejects it).
  - `test_dirty_harness_refuses_publish` — a `-dirty` `harness_version` → clean refusal, non-zero.
  - `test_gold_field_is_gate_outcome_only_shape` — the emitted `gold` object has exactly
    `{gold_adjudicated, gold_version, suppressed_seats}`.
  - `test_assert_verdict_row_scoped_to_grid_not_claim_levels` — a `claim_levels` value that is not a
    verdict literal does NOT false-raise (the row check is scoped to `grid`).
  - (seat-resolution) a multi-seat run without `--seat` errors loudly naming the seats.
  Run; confirm failures.

- [ ] **Step 2 — add `_GOLD_ALLOWED` + `assert_gold_field_shape` to `report.py`** (beside the wall
  helpers). Confirm `test_injected_matcher_recall_field_raises_wallbreach` now distinguishes
  allowlist from denylist.

- [ ] **Step 3 — implement `publish.build_artifact`** + the four ordered wall checks + seat
  resolution, and the CLI `publish` subparser. `events_sha256` = sha256 of `events.ndjson`;
  `model_unverified` = `A2 model_source == "cli-version-only"`.

- [ ] **Step 4 — go green.** `test_publish.py` passes; full suite green.

**Done criterion:** H6's six tests + the seat-resolution test pass; the P1-ε deny-proof proves the
allowlist (not the denylist) catches `matcher_recall`; the four wall checks run in order; a dirty
harness refuses; `publish` prints and never writes to ARB Memory.

**Commit shape:**
```
feat(eval): E publish change-event artifact + gold-field allowlist wall

publish.build_artifact assembles the artifact from captured provenance/detail/
events; refuses dirty harness; enforces assert_gold_field_shape (allowlist —
the denylist can't catch a matcher_recall synonym), report.guard, and grid-scoped
assert_verdict_row, in order; prints the payload (no ARB Memory auto-write).
Deny-proof: a matcher_recall gold key raises via the allowlist. SPEC §6, H6.
```

---

## Task 8 — B: real-codebase fixture bases, generators, anti-oversplit / marker-freedom lint, schema D3 window

**Goal (SPEC §3):** clone-at-build real-app fixture bases (secrets, correctness, authz), seeds as
one head commit from tracked patches, controls harvested from clean upstream code, with a generator
lint that enforces marker-freedom (commit messages/tags/branches too) and caps effective clusters
at the distinct-`why_clean` count; plus a schema D3 within-window proximity check. Depends on Task 2
(`boundary.enclosing_symbol` for the generator's same-enclosing-function check).

**Human input required (SPEC §8 fork 1 — NOT resolved here, parameterized):** the builder REFUSES
to run without `ARB_EVAL_REAL_BASE_SHA` (no default); `ARB_EVAL_REAL_BASE_URL` default proposal
`nsidnev/fastapi-realworld-example-app` (MIT). This task builds the *machinery and its hermetic
lint tests*; the actual real-base build runs in the live gates (Task 10) once Mark pins the SHA.
**The corpus-authoring work lives in Task 10's CHECKLIST as steps L1.0–L1.6** (r2/r3 codex
P1: it must sit where the orchestrator executes, not in this explanatory paragraph).

**Files:**
- New: `fixtures/build_floor_real_secrets.sh`, `…_correctness.sh`, `…_authz.sh` — clone the pinned
  SHA into `fixtures/repos/floor-real-<class>` (gitignored), re-init to a single neutral base commit
  (squash), apply seeds as one head commit from `fixtures/src/real-<class>/*.patch`, deterministic
  SHAs via pinned `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` (mirror `build_floor_secrets_full.sh:24`).
  Attribution + license note in each builder header; **nothing vendored into git**.
- New: `fixtures/src/real-secrets/`, `real-correctness/`, `real-authz/` — **placeholder
  READMEs only in this task** (plan-panel r1 agy P0: authoring REAL seed patches and
  harvesting REAL controls requires the cloned upstream at the pinned SHA — network + a
  human input a codex worker does not have). The patch-authoring + control-harvest work is
  a Task 10 RUNBOOK step (orchestrator/human, post SHA-pin, with the corpus sign-off
  adjudication). This task builds and hermetically tests the MACHINERY against throwaway
  stand-in repos; patch schema documented in the READMEs (5 seeds/class, distinct
  mechanisms, `cluster:` tags, no IDs in comments).
- New: `fixtures/gen_floor_real_secrets_scenario.py`, `…_correctness_…`, `…_authz_…` — scenario
  emitters + the anti-oversplit / marker-freedom lint; emit `subject.repo` **relative** (Task 4
  convention); controls harvested from clean upstream code (locations recorded, code untouched),
  **≥19/class where the base genuinely supplies them**, each with a recorded `why_clean` rationale.
- Modify: `arb_eval/schema.py` — extend the load-time D3 checks (`:177-206`): **a seed and a
  control of the same class in the same file within `matcher_window` (10) declared lines →
  `ScenarioError` — SCOPED to scenarios declaring `schema_rev: 2`** (emitted by the new
  real-base generators). Plan-panel r1 P0 (cold-Opus + GLM + agy convergent): an unscoped
  check breaks the baseline test
  `test_finding_at_seed_and_control_overlap_counts_once_as_detection`, which deliberately
  co-locates a seed and control to pin runtime seed-precedence — legacy (rev-absent)
  scenarios are exempt, and a new test pins BOTH polarities (rev-2 within-window raises;
  legacy within-window loads and the baseline test stays green).
- New: `gold/.gitignore` already covers gold; add/confirm `fixtures/repos/` is gitignored.
- New: `tests/test_real_corpus_lint.py` (H7).

**Lint rules (pinned):**
- **Marker-freedom (GLM P2-4):** the generator greps **commit messages, tag names, and branch
  names** for any seed/control ID (because `git cat-file --batch-all-objects` dumps commit
  messages, an ID there would Park the run). Any hit → build fails. Patch comments carrying an ID →
  build fails.
- **Anti-oversplit (GLM P2-3 + cold-Opus P2):** effective cluster count may never exceed the number
  of distinct `why_clean` rationales — a cluster whose rationale duplicates a neighbour's merges
  into it. The generator computes effective (distinct-cluster) counts; the scenario `description`
  records **nominal vs effective**; small-n is surfaced honestly, never padded with correlated loci.
- **Boundary guard (uses A3):** the generator's richer same-enclosing-function check uses
  `boundary.enclosing_symbol`; the load-time D3 check is the declared-line proximity guard.

- [ ] **Step 1 — write H7 tests first** (`tests/test_real_corpus_lint.py`), building throwaway git
  repos + patch files in `tempfile` (**no docker/engine**):
  - `test_seed_id_in_patch_or_commit_message_fails_build` — a seed/control ID embedded in a patch
    comment, a commit message, a tag name, or a branch name → the generator lint fails the build
    (assert each of the four surfaces independently).
  - `test_seed_and_control_within_window_in_one_function_raises_scenario_error` — a seed and a
    same-class control within the matcher window (same file, ≤10 declared lines) → `ScenarioError`
    at load; the generator's same-enclosing-function check (via `boundary.enclosing_symbol`) also
    flags it.
  - `test_anti_oversplit_lint_caps_effective_clusters_at_rationale_count` — clusters exceeding the
    number of distinct `why_clean` rationales → merged/flagged.
  Run; confirm failures.

- [ ] **Step 2 — implement the schema D3 within-window extension** (the smallest surface; unblocks
  the second H7 test). Extend `schema.py:177-206`'s control-vetting loop with the declared-line
  proximity check.

- [ ] **Step 3 — implement the generators + lint** (marker-freedom over messages/tags/branches +
  anti-oversplit + the boundary same-function check). Then the three builders (clone → squash →
  seed-commit), each refusing to run without `ARB_EVAL_REAL_BASE_SHA`; **hermetic builder tests
  run the full clone→squash→seed path against a LOCAL throwaway stand-in repo (file:// URL) —
  never the network** (agy P0 resolution: machinery proven hermetically; real corpus authored
  in Task 10's runbook). The builders run against the real upstream only in Task 10.

- [ ] **Step 4 — go green.** `test_real_corpus_lint.py` passes; full suite green. Confirm
  `fixtures/repos/` and any real-clone output are gitignored (`git status` shows no clone content).

**Done criterion:** H7's three tests pass; both H7 red conditions (ID-in-history; seed+control
within-window) fire; the anti-oversplit lint caps effective clusters; the builders refuse without a
pinned SHA; no cloned repo content is committed.

**Commit shape:**
```
feat(eval): B real-codebase fixture builders + marker-freedom / anti-oversplit lint

Clone-at-build real bases (secrets/correctness/authz): squash to a neutral base,
seeds as one head commit from tracked patches (5 distinct mechanisms/class),
controls harvested from clean upstream. Generator lint greps commit messages/tags/
branches for IDs and caps effective clusters at distinct why_clean count; schema
D3 flags a same-class seed+control within the matcher window. Builder refuses
without a pinned ARB_EVAL_REAL_BASE_SHA. SPEC §3, H7.
```

---

## Task 9 — Doc-staleness cleanup + CHANGELOG (SPEC §1 one-line obligation)

Doc-only; no behaviour. **Merge, do not replace** — edit the named stale lines in place, preserving
surrounding content.

**Files & edits:**
- `tools/eval/README.md` — kill the stale `13 tests` claim (`:37`) and the `run →
  NotImplementedError` line (`:41`); state the real status (58+ tests green; `run` wired to
  `pipeline.run_floor` via `cli.py`).
- `tools/eval/confinement/README.md` — update the `REMAINING:` line (`:30`) that says
  `confined-review.sh` still needs wiring (it is wired via `ContainerDispatcher`).
- `arb_eval/pipeline.py` — the `ContainerDispatcher` docstring's stale `agy needs adding` (`:152`).
- `CHANGELOG.md` (repo root) — one entry (what AND why) covering the Instrument 1 completion
  increments A–E + B, per repo discipline.

- [ ] **Step 1** — make the four edits (in-place merges, no wholesale rewrites).
- [ ] **Step 2** — `.venv/bin/python3 -m pytest tools/eval -q` (docs don't affect tests; confirm
  still green) and re-read each edited region to confirm no unrelated content was deleted.

**Done criterion:** no stale `13 tests` / `NotImplementedError` / `agy needs adding` /
un-wired-`confined-review.sh` text remains; a CHANGELOG entry exists.

**Commit shape:**
```
docs(eval): retire stale status text (13-tests / NotImplementedError / agy-add) + CHANGELOG

README/confinement-README/ContainerDispatcher docstring described a pre-dispatch
pipeline that shipped long ago; corrected to the built state. CHANGELOG records the
Instrument 1 completion increments. Doc-only, no behaviour. SPEC §1 obligation.
```

---

## Task 10 — Live gates L1–L4 (MANUAL runbook — NOT CI, orchestrator/human-run)

These need **docker + real engines** and are excluded from CI (SPEC §9). They are run after the code
tasks merge, one seat at a time (SPEC §6 runbook, §8 fork 1 SHA pinned by Mark first). This task is a
checklist for the integrating orchestrator, not a dispatched-worker step.

**Pre-req (human):** Mark pins `ARB_EVAL_REAL_BASE_SHA` (+ confirms `ARB_EVAL_REAL_BASE_URL`) at
corpus sign-off, and adjudicates harvested-control cluster distinctness (SPEC §3, §8).

- [ ] **L1.0 — SHA pin (Mark):** `ARB_EVAL_REAL_BASE_SHA` pinned + `ARB_EVAL_REAL_BASE_URL`
  confirmed at corpus sign-off.
- [ ] **L1.1 — clone the pinned SHA locally** (network, orchestrator host).
- [ ] **L1.2 — author 5 seed patches/class** (distinct mechanisms per the design's P-3 tables)
  into `fixtures/src/real-<class>/` per the README schema; commit.
- [ ] **L1.3 — harvest ≥19 controls/class** from clean upstream code, each with a recorded
  `why_clean` rationale.
- [ ] **L1.4 — generator + lint green** (marker-freedom over messages/tags/branches;
  anti-oversplit caps effective clusters); commit scenario JSON.
- [ ] **L1.5 — cluster-distinctness adjudication (Mark)** — the design §3 span-judgment,
  recorded in the corpus sign-off note.
- [ ] **L1.6 — first real-base confined scored run, one seat.** `confinement/build.sh` (rebuild
  image; canary smoke inside it) → `fixtures/build_floor_real_*.sh` (deterministic rebuild) →
  `python3 -m arb_eval.cli run --scenario scenarios/<s>.json --confined --seat <seat> --normalizer
  anthropic:MiniMax-M3` per class. Expect: canary green per dispatch, answer-key pre-flight green,
  report labelled honestly (small-n lines where effective < T).
- [ ] **L2 — negative control (guards must fire).** Plant a seed ID into the fixture worktree →
  pre-flight **Parks**; plant into the image/jail surface → canary **Parks**. Both MUST go red, else
  the guards are hollow (deny-proofs need adversarial verification).
- [ ] **L3 — gold pilot.** `gold export` ≥10 real pairs → two raters adjudicate blind (`gold rate` +
  Mark) → `gold ingest` → `gold score` → per-seat recall renders into a report with the band (or
  suppression) visibly applied. κ computed.
- [ ] **L4 — change-event drill.** Bump one provenance input (image rebuild) → rerun one seat/one
  scenario → `arb-eval publish --run <run-id>` → store via the deployed `memory_store` write path
  tagged `instr1-floor / seat:<seat> / model:<ver> / harness:<sha> / corpus:<ver> / run:<run-id>` →
  retrieve via `memory_search` by seat+corpus key → the artefact round-trips and matches the local
  report.

**Done criterion:** L1–L4 all green; a live-gate record filed (memory + a review-panel brief), per
the live-verification discipline (untested CLI/subprocess glue is where bugs survive static
review).

---

## Final integration gate (orchestrator)

- `.venv/bin/python3 -m pytest tools/eval -q` → **58 prior + all new H1–H7 tests green**; every
  deny-proof red-green both polarities demonstrated (H1 forced-UNKNOWN override, H5 gate wiring, H6
  matcher-recall allowlist).
- `git status` shows nothing committed under `fixtures/repos/`, `gold/<seat>/`, or
  `src/agent_redis_bridge/` (scope-guard proof).
- `grep -rn "AgentRedisBridge\|/Users/<user>" tools/eval/scenarios/*.json` → empty (Task 4).
- CHANGELOG entry present.

---

## Escalations

**None.** The four SPEC §8 items are human forks (real-base SHA, gold rater pool, run-vs-gold order,
Claude-container-seat timing) — parameterized with runnable defaults in the SPEC and surfaced to the
human at corpus sign-off / Task 10, not design contradictions. No contradiction between the SPEC and
the certified design was found while translating it into these tasks. Two SPEC-level
under-determination resolutions (provenance written at run-start with per-seat engine data merged
via `provenance_engine`; the `confined-review.sh` exit-43 canary sentinel) are already documented in
the SPEC's own Escalations section and are carried faithfully here — they are additive and weaken no wall.

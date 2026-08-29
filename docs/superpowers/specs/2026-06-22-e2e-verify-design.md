# e2e-verify — design / spec

> **Status:** design-of-record (spec). Derived from Mark's `e2e-verify` brief + the first design
> panel (cold-Opus, GLM, codex-contributor; M3 no-verdict, agy absent). Incorporates the panel's
> P0/P1s. Next gate: spec panel (Workflow B).
>
> **Threat model:** ARB's standing model — operator **mistakes**, not a malicious operator (solo,
> trusted infra). Adversarial-disposition / artifact-forgery concerns are named out-of-scope
> (productization-era), not built — same basis as the log hash-chain and the from→task ledger.

## 1. Purpose (the autonomy framing)

ARB's goal is to move the human gate from *every step* to *the edges*: supervised-by-logs work by
day, unsupervised long-runs at night reviewed in the morning. The tri-model review panel verifies a
change is **sound by inspection** — it reasons about code/spec/diff. It structurally **cannot see
runtime reality** (real boot, real adapter behaviour, real integration, the actual rendered UI, the
real persisted record). Those are empirical properties invisible to analytical review *by
construction* — and they are exactly the class that masked reality repeatedly this session (the
FakeEngine instant-start vs real boot; the pgvector adapter gap; and — live, on this very slice's
design panel — **three analytical seats agreeing a duplicate disposition row was "handled" when
running it proved it wasn't**).

`e2e-verify` is the **empirical, ground-truth gate that runs AFTER tri-model review and BEFORE
merge.** Analytical review filters (is it sound?); E2E confirms on the real thing (does it actually
work?). Both required to merge. E2E closes the panel's structural blind spot — the gap that has no
human backstop during an unsupervised overnight run.

**The property it generalises:** "loop until it works in the browser" found real bugs because the
browser is a ground truth the model cannot author around. The model can make a test green (sometimes
by making the test wrong); it cannot make the real rendered button work, or the real record
round-trip, without making the real code work. This skill generalises that to every surface.

## 2. The one non-negotiable: real-boundary crossing (+ the detector)

> **An E2E verification is only worth what it really exercises. It must cross a real boundary the
> model cannot author around — and the skill must PROVE it does.**

The boundary per surface:
- **UI** → the real renderer (real browser, real backend behind it). Assert the *feature*, not page-load.
- **Memory (ARB-memory)** → real write-then-read-back through real retrieval against the real store
  (Postgres/pgvector), not a double.
- **API** → real HTTP to the running service; assert response *and* the side-effect (the row, the
  memory), not "the handler returned without throwing."
- **Verification-mechanism (the H2 producer — §6, the first built surface)** → the real persistence
  + the gate's real disk-read + the real graduation math against really-written records.

General rule: **the real boundary is whatever the model cannot reach inside** — the renderer, the
real DB, the real transport/process, the real persisted log. Find it; cross it; prove it was crossed.

### 2a. The real-boundary detector (highest-value part)

The trap: under pressure for speed/reliability an "E2E" quietly mocks the boundary it is supposed to
cross — a headless browser against a mocked backend, a memory "E2E" against a test-double store, an
API test against a stubbed service. It **keeps the name and loses the property** — a unit test in an
E2E costume, the fixture-masks-reality defect wearing the costume of the thing meant to defeat it.

The skill ships a **runnable detector**, not a manual attestation: *name the real boundary this test
crosses; if it resolves to a mock/double/stub at that boundary, it is NOT E2E — fail it as
miscategorised.* A generator that writes E2E tests is commodity; the detector that keeps E2E *real*
is the reason to build this. (Panel note: by-inspection "monkeypatches NOTHING" is unprovable —
implement the detector as a **runtime canary**: assert the exact function objects used are the real
module symbols by identity, and assert the real side-effect occurred at the real boundary — see §6.6.)

## 3. The fail-closed three-state model (load-bearing safety property)

When the E2E **cannot run** — flaky, env broke, couldn't reach the real store/service, or detected as
mocked-boundary — it must **BLOCK, not pass.** Unverified ≠ passed. Three explicit states:

- **`pass`** — ran, real boundary crossed, feature works. *Only this merges.*
- **`block-fail`** — ran, real boundary crossed, feature broken. Return for fix.
- **`block-unrun`** — could not run / boundary unreachable / **miscategorised-as-mock** / **vacuous
  (zero real cases)**. Return / block. **NEVER passes.**

**Pinned representation (GLM P2):** the three states are a concrete `E2EStatus` enum
(`PASS`/`BLOCK_FAIL`/`BLOCK_UNRUN`) carried on an `E2EResult` dataclass (status + the per-case
detail + the workload counts of §6.7), so the result type is not left to builder guess. The
pipeline decision (merge / return) is computed *from* `E2EResult`, kept **distinct** from the
`pass` state itself (codex P2: `pass` is an E2E verdict, not merge permission).

This is the essay-#1 oracle discipline: fail *safe* (unreachable→blocked), never *open*
(unreachable→passed). The worst outcome the skill could produce is a fail-open E2E that merges a
change *because the test couldn't run* — and the operator wakes to "merged" when the truth was
"untested." The `block-unrun` state must be **unsatisfiable as a pass**, including the **zero-case
vacuous-green** twin of the empty-run hole (GLM P0): a green result from a 0-case run is `block-unrun`,
not `pass`.

## 4. Flakiness vs realness (design against, don't wish away)

E2E is the trustworthy tier *and* the flaky/slow tier, and as the final gate it throttles the whole
pipeline. A flaky-but-fail-closed E2E blocks on test-infra problems — frustrating but *correct*; a
flaky-fail-open E2E is *dangerous*. So: reliability gets as much design attention as realness, and
reliability must **not** be bought by weakening the real boundary (that re-opens §2a). Make
*real-boundary* tests *reliable* — hermetic fixtures, deterministic seeding, proper teardown,
retries-with-real-boundary — never *reliable-by-faking*. Any reliability mechanism that works by
weakening the boundary is a hole (flag it).

## 5. Architecture: spine + surfaces

Two layers, deliberately decoupled:

- **The spine (BUILT this slice, in H2-coupled form):** (a) the real-boundary **detector** (§2a) as a
  runtime canary; (b) the **fail-closed three-state result model** (§3, the `E2EStatus`/`E2EResult`
  types); (c) the **notice→block graduation** scaffold (§9). **Clarification (codex/GLM P2):**
  "surface-agnostic" is the *design intent*; this slice builds the spine **coupled to the H2 surface**
  (the canary names H2 symbols), NOT a generic detector framework. The reusable/surface-agnostic
  extraction is deferred to §13's stop-condition — building a generic framework now would be the
  premature-abstraction this slice's own discipline warns against.
- **Surfaces (pluggable):** each surface provides {how to drive the real boundary, how to assert the
  feature, how to produce the legible real signal}. **BUILT this slice: the H2 producer surface
  (§6)** — self-contained (no browser/Postgres), and the surface we have a live failure-mode corpus
  for (the duplicate-id incident). **DESIGNED, not built: UI / memory / API** (need real envs; YAGNI
  until a real change needs them; extract-from-instance — §13).

Decoupling rationale: the corpus and the drive-level are separate artifacts (a corpus case is
drive-level-agnostic), so a heavier tier (e.g. Level C for a surface) stays available later without a
corpus rewrite.

## 6. First built surface: the H2 producer E2E (Level B)

**Drive level B** (settled, panel-affirmed): corpus stores portable data; the harness drives the
**real** gate entry + real persistence + real graduation query. Level A (drive `derive()` only) was
rejected — it skips the real persistence boundary (a unit test in an E2E costume). Level C (real git
repo per case) was rejected — its only extra coverage is git-diff computation (infrastructure, not
producer logic), and per-case git cost would make the suite too slow to stay armed as a tripwire.

### 6.1 The real boundary, named precisely (panel P0-1 correction)

`h2_standing_check(phase_input, repo, changed_paths, diff)` returns `{"record": H2Record, ...}`
and does **no WRITE I/O** — it does **not** append the shadow log (the *caller* does). But it is
**not pure** (GLM spec-panel P1): it performs a real **disk READ** via
`_h2_candidate_files(repo_path, changed_paths)` → `path.read_text(...)`. So the harness crosses
*two* real boundaries through `h2_standing_check` — the candidate-file **read** and (via the caller)
the log **write**. There is **no producer log-reader**: `is_graduation_ready` takes an in-memory
record iterable, not a log path. The honest seam:

```
seed tmpdir with corpus `files`  → real _h2_candidate_files read         [REAL persistence/disk READ]
derive/validate/is_complete      → via real h2_standing_check            [REAL producer logic, incl. the read]
result["record"]                 → real h2_collector.append_record(rec)  [REAL persistence WRITE — caller-side]
read JSONL back                  → harness JSONL-parse                   [HARNESS GLUE — no producer reader]
parsed records                   → real is_graduation_ready(records)     [REAL graduation math, record-iterable API]
```

Boundary-honesty ledger: **real** = derivation, disk-read (`_h2_candidate_files`), validation,
`is_complete`, the persistence **write** (`append_record`), graduation math. **Harness glue** = the
JSONL **read** (the producer ships no reader — asserted at `is_graduation_ready`'s record-iterable
boundary). **Supplied (genuine external inputs)** = `diff`, `changed_paths`, and `phase_input`
(the real `h2_standing_check` signature takes all three). The design must not claim a real reader
exists, and §6.6's canary covers only real symbols that exist: `derive`, `h2_standing_check`,
`_h2_candidate_files` (the real reader the producer DOES have), `append_record`, `shadow_log_path`,
`is_graduation_ready` — there is no *log*-reader (the JSONL parse is harness glue).

### 6.2 Corpus schema (panel P0: must express the incident)

`tests/e2e/h2_corpus/<category>/<case-id>/case.json`:

```json
{
  "files": { "<relpath>": "<file content>" },
  "diff": "<unified diff text>",
  "changed_paths": ["<relpath>", "..."],
  "phase_input": { "h2_section": { "coverage_acknowledgment": {...}, "rows": [ {disposition row} ] } },
  "expected": {
    "derived": ["<candidate-id>", "..."],
    "status": "<shadow|enforced|flagged|static-only-unacknowledged>",
    "record": { "complete": true, "dispositions": [ {"candidate_id": "...", "disposition": "...", "valid": true} ] },
    "graduation": { "counted": true, "disposition_counts": {...} }
  },
  "notes": "human-readable: what this case is, why bad/clean, which guard it pairs with"
}
```

**`phase_input`/`h2_section` is FIRST-CLASS** (GLM P0): the disposition rows under test —
including the `discovered/duplicate-id` rows — live in `phase_input["h2_section"]["rows"]`, NOT in
`files`/`diff`. Without this field the incident is *unencodable* and the build slice would be wasted.
The corpus `phase_input` is a **minimal direct input to `h2_standing_check`** (which reads only
`phase_input["h2_section"]` via `_h2_section`) — **not** a full gate `phase_input` document; it would
not satisfy `PHASE_INPUT_REQUIRED` because the seam calls `h2_standing_check` directly, bypassing
`evaluate()` (GLM P2 — don't over-build a schema-valid phase_input). `changed_paths` is supplied and
**explicit in every v1 case** (codex P2), with one helper test asserting diff-header paths match
`changed_paths` (the mismatch is itself a real failure mode). `record.dispositions` is a **`list[dict]`**
(`H2Record.dispositions` — agy/codex/GLM P2), and `expected` asserts the serialized record shape via
**exact JSONL equality** on the fields under test (`derived`, `dispositions`, `complete`, graduation
counts) — directly because the incident was a denominator skew. `status` uses the **real enum**
`{shadow, enforced, flagged, static-only-unacknowledged}` (GLM P2), not a coined `pass/flag/incomplete`.

### 6.3 Three categories

- **`enumerated/`** — hand-seeded from the H2 seven-round failure modes + the heuristic triggers
  (redis/psycopg/subprocess/socket calls; module-level; the exclusions). Include ≥1 no-candidate diff
  (the distinct "no derived ids" path — codex P2).
- **`discovered/`** — empirically-found holes; **seeded now with `duplicate-id`** (two valid rows for
  one derived candidate → run `complete=False` → excluded from the graduation **window**). Each
  `discovered/` case keeps its incident identity (not silently duplicated into `enumerated/` — GLM/codex).
- **`clean/`** — known-good diffs that must pass (test-only edits, noop-guarded calls, `__main__` calls).

Each entry is **paired with its deny-proof** (§6.5) — a case without a guard it exercises is a sample,
not a regression.

### 6.4 Graduation assertion (panel P1: the levels)

`is_graduation_ready` needs ≥10 complete records, ≥20 disposed, discrimination, fp<0.10 — so a
*single* corpus case makes it **vacuously False always**. Therefore:
- Per-case, assert at the **counting layer**: use the public `fp_rate` and the `H2Record` counts;
  `_disposition_counts` is **private** (cold-Opus P1), so if a test imports it, mark that an
  intentional test-only internal-API dependency (prefer `fp_rate` + record fields where possible).
- For the **five** `h2_graduation.GUARDS` (`min_runs`, `min_disposed`, `discrimination`,
  `fp_threshold`, `complete_only` — codex P2: five, not four) at graduation scale, add a
  category-level **multi-record fixture** (≥10 synthesized records), and — because one all-green
  10-record fixture makes every guard *true* without proving any guard *bites* — add a **per-guard
  N−1 boundary fixture** (GLM P2): e.g. 9 records to prove `min_runs` reds-with-guard / greens-
  without, exactly at the off-by-one. Each graduation guard must be shown to bite, not merely be
  satisfiable. (This is the graduation-guard counterpart of §6.5's mutation layer.)
- **Framing fix (GLM):** a post-fix duplicate makes `is_complete` return False → the record is
  `complete=False` → **excluded from the graduation window entirely** (not "from the denominator";
  denominator-inflation is the *broken*-state behaviour). Corpus `expected` asserts window-exclusion.

### 6.5 Guard-deletion mutation layer (E2E analog of the deny-proofs)

A registry `{guard-clause-location → corpus-case-id that must red when it's deleted}`. For each
load-bearing guard: delete the clause → confirm its paired case reds through the **real Level-B
path**; restore → green. Panel-hardened (cold-Opus, codex, GLM):
- **Completeness source (concrete — agy/codex P1):** a checked-in `tests/e2e/h2_guard_registry.json`
  (`{guard_id → {file, locator, paired_case_id}}`) plus a completeness test that asserts the registry
  covers exactly: the `is_complete` guards in `h2_assumptions.py` (coverage-acknowledged, derived≥1,
  validity, rows⊆derived, uniqueness, every-derived-has-a-row) **and** `h2_graduation.GUARDS`
  (`min_runs`, `min_disposed`, `discrimination`, `fp_threshold`, `complete_only`) — use the real
  `GUARDS` set for that family (cold-Opus P1: don't conflate the two guard shapes via a single
  AST-scan). A new guard with no registry entry fails the completeness test (else silently deletable).
- **Mutation mechanism (concrete — agy P0):** deletion is a **line/locator-based** edit applied to a
  **copy** of the source in a tmp worktree, run in an **isolated subprocess** (no cached module).
  Verify the *mutated bytes are the ones imported*; assert the paired case reds on the **intended
  assertion/mechanism**, not "any red" (a SyntaxError reds everything trivially — excluded by
  asserting the specific failing assertion).
- **Minimal-pair isolation** — each case isolates its guard so deleting *other* guards doesn't also
  red it (else the mutation proof is ambiguous about which guard the case proves).

### 6.6 Boundary-honesty canary (runtime, not inspection — panel P1)

Implemented as a runtime check, not a by-inspection "monkeypatches NOTHING" claim (unprovable):
- **Function-object origin (robust — agy P1):** identity alone is bypassed by a `sys.modules`-level
  mock, so assert each invoked symbol's **origin**: `func.__module__` and
  `func.__code__.co_filename` resolve to the real codebase paths, and `not isinstance(func,
  unittest.mock.Mock)`. Covers `h2_standing_check`, `_h2_candidate_files`, `append_record`,
  `shadow_log_path`, `is_graduation_ready` (no reader symbol exists).
- **Real side-effects on BOTH boundaries (GLM P1):** assert the **write** occurred at the resolved
  `shadow_log_path()` (file grew; JSONL parses back to the written record) **and** the **read**
  occurred — the derived candidate ids correspond to files actually seeded in the tmpdir (a
  mock-the-reader edit feeding canned contents would break this). The read boundary must be in the
  tripwire set, not just the write.
- **No-module-monkeypatch convention** — enforced for the harness package; the canary is a runtime
  tripwire that a future edit mocking the reader **or** writer fails loudly.

### 6.7 Hermeticity / determinism (panel P1 — tripwire must stay armed)

Each case runs in an **isolated tmpdir** with `ARB_H2_SHADOW_LOG`, `XDG_STATE_HOME`, **and `HOME`**
redirected to tmp (`shadow_log_path()` reads all three), **no network, no git**. Pin/assert
`H2_MODE == "shadow"` (a module global at `gate.py`) so corpus status-expectations don't drift with a
global toggle. **Enforcement hook (codex/agy P1):** an autouse fixture for the `tests/e2e/` package
redirects the three env vars to the tmpdir and blocks `socket` connections and `git`/`subprocess`
calls — with an explicit **allowlist** for the §6.5 mutation-subprocess runner (which legitimately
spawns a subprocess). **Runner output (agy P1):** the runner writes the final three-state result to a
local JSON (`e2e_status.json`) and exits `0` on `pass`, `1` on `block-fail`, `2` (non-zero) on
`block-unrun` (including the zero-case vacuous run). (This is also what makes the §7 flip-gate's "ran
against the code being flipped" well-defined — see §7's paradox resolution.)

## 7. Flip-gate DESIGN (designed-now, BUILT-later — the trust-root-adjacent piece)

The graduation flip (shadow→block) is an operator action that re-pins the trust root. The flip-gate
is a precondition: `flip_gate_blocks(repo) -> list[str]` (empty = may flip), mirroring the existing
`trust_root_blocks` / `rotation_blocks` family. It **reads a pinned artifact and recomputes hashes
itself** — it does NOT trust caller-passed values (cold-Opus/GLM/codex P1; parity with
`trust_root_blocks`). Properties:

1. **Ran-and-passed against the code being flipped (paradox resolution + ATOMIC mode-pin).**
   cold-Opus's P0: today `H2_MODE` is a constant *in `gate.py`*, which is in `logic_set_paths`, so
   flipping it mutates the certified SHA — making "E2E ran against the flipped code" circular.
   **Resolution: the flip-gate slice moves `H2_MODE` out of the hashed code into a mode artifact**
   (`skills/bridge-protocol/gate/h2_mode.json`), so the flip edits *data*, not the certified logic
   set. Then `logic_set_sha` is **stable across the flip**, and the gate checks
   `artifact.logic_set_sha == certified_object_sha(repo)` with `logic_set_sha :=
   certified_object_sha(repo_at_run_time)` (the SAME function's return — not a re-derived second
   list — GLM P1). **But moving `H2_MODE` out of the hash REMOVES it from integrity protection
   (cold-Opus/codex/agy unanimous P0): an honest mode edit would rotate no SHA and trip no gate, and
   the flip-gate is build-later — the interim would be STRICTLY WEAKER than today.** So the move and
   the pin are **ATOMIC**: the same slice that moves `H2_MODE` out (a) pins the mode artifact —
   the `e2e_result` carries `mode_artifact_sha`, `mode_at_run`, `flip_target_mode`, the gate
   recomputes `sha256(h2_mode.json)` and validates the `shadow→block` transition; (b) makes
   `gate.py` **fail-closed** if `h2_mode.json` is missing/unreadable/corrupt (default `"block"` is
   safe; an unreadable mode must not silently fall back to `"shadow"` — agy P1); (c) carries a guard
   that `h2_mode.json` is **never re-added to `logic_set_paths`** (which would resurrect the paradox
   — GLM P2). Net invariant: **never weaker than today** — the mode bit stays integrity-governed,
   just by the flip-gate's recompute rather than the logic-set hash.
2. **Full input coverage (codex/GLM P0).** `certified_object_sha` covers gate.py/SKILL.md/schemas/
   the three H2 files — NOT `tests/e2e`/corpus/runner/collector. The artifact carries `e2e_suite_sha`,
   `corpus_sha`, `runner_sha`, **and `collector_sha` (MANDATORY** — not "ideally"; §6's real boundary
   includes `append_record` — codex/agy/GLM P1). Defined path sets the gate recomputes against current
   bytes: `e2e_suite_sha` = `tests/e2e/` excluding `h2_corpus/`; `corpus_sha` = `tests/e2e/h2_corpus/`;
   `runner_sha` = the runner module(s); `collector_sha` = `skills/bridge-protocol/gate/h2_collector.py`.
   Any mismatch → block.
3. **Anti-vacuity — EXACT, not a floor (GLM P1 — the sharp one).** A 0-case run → `case_count=0`
   → block ✓. But a **minimum** `registry_size` floor cannot catch a *guard deletion*: the count can
   stay ≥ floor, or shrink and pass `≥` *more* easily (a stale larger artifact beats a shrunk
   registry). So the gate uses **exact equality / content hash**, not a floor: the artifact carries
   `registry_sha` (content hash of `h2_guard_registry.json`) and `registry_size`; the gate
   **recomputes both from current bytes** and requires `artifact.registry_sha == current` and
   `artifact.registry_size == current`. Workload realness uses **exact state totals**
   (codex P0): `passed_count`, `block_fail_count`, `block_unrun_count`, `case_count`; require
   `status==green`, `block_fail_count==0`, `block_unrun_count==0`, `passed_count==case_count`,
   `case_count >= current_registry_size`. A degenerate nonzero run (skipped/unrun cases) cannot pass.
4. **Provenance — prefer INVOKE-THE-RUNNER (cold-Opus/codex P1).** A `producer_digest` over the
   artifact defends *out-of-scope* forgery, not *in-scope* staleness — so the **preferred** design is:
   the flip-gate **invokes the runner itself** during the operator action and consumes its fresh
   output, so there is no stored artifact to trust. If an artifact is used instead, `producer_digest`
   is defined as `sha256` of the canonical JSON (keys sorted, the `producer_digest` field excluded)
   of the result, and `suite_id`/`runner_version`/`schema_version`/`generated_at` are validated — this
   catches the honest-mistake class (stale reuse, wrong suite, forgotten recompute), which is all the
   threat model requires.
5. **Fail-closed (§3 applied here).** Artifact absent / unreadable / malformed JSON / stale-SHA (any
   of logic/suite/corpus/runner/collector/mode) / `registry_sha`-or-`registry_size` mismatch /
   status≠green / nonzero `block_*` / `passed_count≠case_count` / bad provenance → **block**. Never
   "didn't object → proceed."

**Pinned `e2e_result` schema (design-fixed so the build can't weaken it):**
`{logic_set_sha, e2e_suite_sha, corpus_sha, runner_sha, collector_sha, mode_artifact_sha, mode_at_run,
flip_target_mode, status, generated_at, case_count, passed_count, block_fail_count, block_unrun_count,
registry_sha, registry_size, suite_id, runner_version, schema_version, producer_digest}`.
`flip_gate_blocks(repo)` reads the pinned artifact, **recomputes every `*_sha` and `registry_*` from
current bytes**, and enumerates exactly which field-condition maps to which named block reason (block
constant `BLOCK_H2_FLIP_UNVERIFIED`).

**Named deny-proofs (built with the gate):** (a) deleted/absent artifact → block; (b) logic-SHA
mismatch → block; (c) **0-case / `block_*`≠0 / `passed≠case_count` → block**; (d) **registry-entry
deleted (a guard goes unexercised) → block** — via `registry_sha`/exact-count mismatch, NOT a floor;
(e) stale suite/corpus/runner/collector hash → block; (f) **stale/wrong mode artifact** (`mode_at_run`
or `mode_artifact_sha` mismatch, or bad `shadow→block` transition) → block; (g) wrong-suite-id /
wrong-schema / forgotten-recompute artifact → block. Each mutation-verified (delete the gate clause →
the deny-proof reds).

**Runbook contradiction (cold-Opus P0-2 — must be flagged):** this design **supersedes** the current
flip procedure in `docs/runbooks/h2-graduation.md:61-73` (which still says "edit `H2_MODE` in
`gate.py`"). The flip-gate build slice MUST update that runbook to the `h2_mode.json` + `flip_gate_blocks`
procedure; until then the runbook is stale and the spec records the discrepancy.

**§7 threat-class line (GLM P1 — the flip-gate has its own threat axis, distinct from §10's producer
axis):** honest-mistake on the artifact/corpus/mode path (truncated write, stale read, **gutted
corpus**, accidental mode edit) is **in scope** and defended by properties 1–5; **deliberate artifact
forgery** by a malicious operator is the §9-equivalent **out-of-scope** non-goal (productization-era).

## 8. CI placement & marker

Register an `e2e` marker in `pyproject.toml`. The suite runs under `pytest -m e2e`, kept fast enough
to stay armed (Level B, no git per case — the tripwire-viability constraint that killed Level C).
Runs automatically in CI (the latent-tripwire property); the marker keeps it off the fast unit loop.
**Pipeline placement (brief §7):** the e2e-verify gate runs **after** the tri-model review panel and
**before** merge — cheap-analytical-filter then expensive-empirical-confirm, with the unfakeable
check **last** (the correct final word before merge). `pass → merge-hold for human dev review`;
`block-fail → return for fix`; `block-unrun → return / block (never pass)`.

## 9. Graduation / rollout (consistency with the H2 precedent)

A new automated gate runs **notice before block** — observation before it gates — and graduates only
on **earned evidence**, with the criterion defined **up front** so it actually graduates rather than
living in permanent shadow (the H2 lesson). Per the seven-round H2 doctrine: harden the criterion's
integrity against **honest-mistake gaming (in-threat-model)**, **name adversarial-disposition gaming
as an explicit out-of-scope non-goal (out-of-threat-model)**, and **do not spiral** — a hole that
needs an adversary is out of model; close the mistake-class, name the rest.

## 10. Threat-class termination (Murphy, not adversary)

Corpus-authoring rule (carried verbatim from the H2 spec §9): a case is **in scope** if its
construction is an honest mistake or a heuristic blind spot; **out of scope** if it requires a
malicious author deliberately shaping inputs to evade derivation. The §7 artifact/corpus threat axis
(§7's threat-class line) is the second instance of this discipline.

## 11. Scope (what this slice builds vs designs)

- **BUILD:** the spine (detector §2a / fail-closed three-state §3 / graduation-notice scaffold §9) +
  the **H2 producer surface** (§6, Level B, full panel fixes) + the `-m e2e` marker (§8) + the three
  committed deny-proofs (§12).
- **DESIGN-ONLY (not built):** the flip-gate (§7 — dormant: graduation is ≥10 runs + operator action
  away, and trust-root-adjacent, earns its own panel) and the UI / memory / API surfaces (§5 — need
  real envs; YAGNI; extract-from-instance).
- **Terminal state:** **merge-hold for Mark's →dev review.** No autonomous merge to dev (matches the
  brief's own §7 and standing discipline).

## 12. The three committed deny-proofs (brief §9 — captured as code that can't regress)

1. A **mocked-boundary** test is detected and **fails as miscategorised** (proves §2a is enforced,
   not asserted) — e.g. a surface wired to a test-double store fails the detector.
2. An **unrunnable / unreachable** E2E **blocks**, does not pass (proves §3 fail-closed, incl. the
   zero-case vacuous-green → `block-unrun`).
3. A **real-boundary** test that the real feature breaks **blocks** (proves it catches empirical
   breakage the analytical panel can't see — the duplicate-id case run through the real Level-B path).

Each is a real inject-revert, **mutation-verified** (delete the guard → the deny-proof reds), per the
deny-proofs-need-adversarial-verification doctrine.

## 13. Extraction stop-condition (recorded, not executed)

Extract the reusable `e2e-verify` skill from the H2 instance LATER, only after: (a) it caught ≥1 real
thing the unit/panel missed, (b) the harness is stable over N runs (the *harness* not changing, not
just the feature), and (c) a **second surface** instance exists (so extraction generalizes from two
points, not one). The earlier brief is design-intent, not the skill. Extraction is NOT this slice.

## 14. Honest residuals / non-goals

- The skill makes E2E **real and fail-closed**; it does not make E2E **exhaustive**. It verifies the
  surface's real-boundary behaviour for the change under test — not total coverage. Bound the
  confidence honestly (the `coverage_acknowledgment` pattern); the gate must not manufacture
  "fully verified" confidence it didn't earn.
- **No producer log-reader** (§6.1): the JSONL read is harness glue; if production later ships a
  reader that dedupes/filters differently, the E2E won't catch that divergence (it uses its own
  reader). Recorded as a known boundary; revisit when a real consumer exists.
- **`h2_collector.py` outside `logic_set_paths`** (codex/GLM P2): the collector is heavily exercised
  here but not in the certified SHA, so a change to append/read semantics doesn't rotate the trust
  root. Pre-existing; §7 property-2 (`collector_sha`) closes it *for the flip-gate* but not for the
  general gate. Flagged for the trust-root path-set owner.
- **Out-of-scope (named, not built):** adversarial-author / malicious-disposition hardening; artifact
  forgery by a malicious operator (§7 threat line); a general test-framework rewrite (use existing
  Playwright/pytest/HTTP machinery — the value is the discipline layer).
